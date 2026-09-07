"""OpenRV Kitsu Review plugin: browse task revisions, download/load them into
RV, view and post comments, and round-trip RVPaint annotations to and from
Kitsu preview files.

This module is deliberately thin: it builds widgets, forwards user actions to
:class:`review_session.ReviewSession`, and renders whatever comes back. All
Kitsu access lives in ``kitsu_service``, all RVPaint access in ``rvpaint``,
and all coordinate/colour maths in ``utils``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Optional

import zou as kitsu
import rvpaint
from zou import KitsuError
from session import DOWNLOAD_DIR, ReviewSession

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    import rv
    import rv.rvtypes
    import rv.qtutils

    _QT_AVAILABLE = True
except ImportError:
    _QT_AVAILABLE = False

_INSIDE_OPENRV = _QT_AVAILABLE and rvpaint.RV_AVAILABLE and kitsu.KITSU_AVAILABLE

_QWidgetBase = QtWidgets.QWidget if _QT_AVAILABLE else object
_MinorModeBase = rv.rvtypes.MinorMode if _QT_AVAILABLE else object


@contextmanager
def _busy():
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
    try:
        yield
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()


class KitsuReviewPanel(_QWidgetBase):
    THUMBNAIL_SIZE = (96, 54)
    COLUMNS = ["Thumbnail", "Shot", "Task", "Rev", "Status", "Date"]

    def __init__(self, parent=None, session: Optional[ReviewSession] = None):
        super().__init__(parent)

        self.session = session or ReviewSession(DOWNLOAD_DIR)
        self._thumbnail_cache: Dict[str, Any] = {}

        self._build_ui()
        self._refresh_login_state()

    # -- construction -----------------------------------------------------

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.addLayout(self._build_top_bar())

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self._build_revision_list())
        splitter.addWidget(self._build_detail_pane())
        splitter.setSizes([420, 480])
        root.addWidget(splitter, stretch=1)

    def _build_top_bar(self):
        bar = QtWidgets.QHBoxLayout()

        self.status_label = QtWidgets.QLabel("Not connected to Kitsu")
        self.status_label.setStyleSheet("font-weight: bold;")
        bar.addWidget(self.status_label)
        bar.addStretch()

        self.server_field = QtWidgets.QLineEdit("http://localhost/api")
        self.server_field.setFixedWidth(260)
        self.user_field = QtWidgets.QLineEdit("admin@example.com")
        self.user_field.setPlaceholderText("email")
        self.pass_field = QtWidgets.QLineEdit()
        self.pass_field.setPlaceholderText("password")
        self.pass_field.setEchoMode(QtWidgets.QLineEdit.Password)

        bar.addWidget(QtWidgets.QLabel("Server:"))
        bar.addWidget(self.server_field)
        bar.addWidget(QtWidgets.QLabel("User:"))
        bar.addWidget(self.user_field)
        bar.addWidget(self.pass_field)

        self.login_btn = QtWidgets.QPushButton("Log In")
        self.login_btn.clicked.connect(self._on_login_clicked)
        bar.addWidget(self.login_btn)
        return bar

    def _build_revision_list(self):
        left = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(left)
        layout.addWidget(QtWidgets.QLabel("Revisions available for review"))

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table)

        self.refresh_btn = QtWidgets.QPushButton("Refresh Revisions")
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        layout.addWidget(self.refresh_btn)
        return left

    def _build_detail_pane(self):
        right = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(right)

        self.detail_label = QtWidgets.QLabel("Select a revision on the left.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.detail_label)

        action_row = QtWidgets.QHBoxLayout()
        self.download_btn = QtWidgets.QPushButton("Download + Load in RV")
        self.download_btn.clicked.connect(self._on_download_clicked)
        self.export_btn = QtWidgets.QPushButton("Export to Kitsu")
        self.export_btn.clicked.connect(self._on_export_clicked)
        for button in (self.download_btn, self.export_btn):
            button.setEnabled(False)
            action_row.addWidget(button)
        layout.addLayout(action_row)

        hint = QtWidgets.QLabel(
            "Tip: use RV's own Paint tools to annotate the frame directly. "
            "Annotations are picked up automatically on export."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-style: italic; color: #666;")
        layout.addWidget(hint)

        layout.addWidget(QtWidgets.QLabel("Comments"))
        self.comments_list = QtWidgets.QListWidget()
        layout.addWidget(self.comments_list, stretch=1)

        comment_row = QtWidgets.QHBoxLayout()
        self.comment_input = QtWidgets.QLineEdit()
        self.comment_input.setPlaceholderText("Write a review comment...")
        self.add_comment_btn = QtWidgets.QPushButton("Add Comment")
        self.add_comment_btn.clicked.connect(self._on_add_comment_clicked)
        self.add_comment_btn.setEnabled(False)
        comment_row.addWidget(self.comment_input)
        comment_row.addWidget(self.add_comment_btn)
        layout.addLayout(comment_row)
        return right

    # -- small view helpers -----------------------------------------------

    def _info(self, message: str):
        QtWidgets.QMessageBox.information(self, "Kitsu", message)

    def _warning(self, message: str):
        QtWidgets.QMessageBox.warning(self, "Kitsu", message)

    def _error(self, message: str):
        QtWidgets.QMessageBox.critical(self, "Kitsu", message)

    def _thumbnail_pixmap(self, preview_file_id):
        if preview_file_id in self._thumbnail_cache:
            return self._thumbnail_cache[preview_file_id]

        pixmap = None
        data = kitsu.fetch_thumbnail(preview_file_id)
        if data:
            image = QtGui.QImage()
            if image.loadFromData(data):
                pixmap = QtGui.QPixmap.fromImage(image)

        self._thumbnail_cache[preview_file_id] = pixmap
        return pixmap

    def _thumbnail_widget(self, preview_file_id):
        label = QtWidgets.QLabel()
        label.setAlignment(QtCore.Qt.AlignCenter)

        pixmap = self._thumbnail_pixmap(preview_file_id)
        if pixmap and not pixmap.isNull():
            width, height = self.THUMBNAIL_SIZE
            label.setPixmap(
                pixmap.scaled(
                    width, height,
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )
            )
        else:
            label.setText("(no thumbnail)")
            label.setStyleSheet("color: #888; font-style: italic;")
        return label

    # -- rendering --------------------------------------------------------

    def _refresh_login_state(self):
        connected = self.session.logged_in
        self.status_label.setText(
            f"Connected to Kitsu as {self.session.display_name}" if connected
            else "Not connected to Kitsu"
        )
        self.status_label.setStyleSheet(
            "font-weight: bold; color: #2e7d32;" if connected
            else "font-weight: bold; color: #b71c1c;"
        )
        self.login_btn.setText("Log Out" if connected else "Log In")
        for widget in (self.server_field, self.user_field, self.pass_field):
            widget.setEnabled(not connected)
        self.refresh_btn.setEnabled(connected)

        if not connected:
            self.table.setRowCount(0)
            self._thumbnail_cache.clear()
            self._clear_detail_panel()

    def _populate_table(self):
        self.table.setRowCount(0)
        thumb_width, thumb_height = self.THUMBNAIL_SIZE

        for row, rev in enumerate(self.session.revisions):
            self.table.insertRow(row)
            self.table.setRowHeight(row, thumb_height + 10)
            self.table.setCellWidget(
                row, 0, self._thumbnail_widget(rev["preview_file"].get("id"))
            )

            values = [
                rev["shot"],
                rev["task_type"],
                kitsu.revision_label(rev),
                rev["status"],
                rev["date"],
            ]
            for offset, value in enumerate(values):
                self.table.setItem(
                    row, offset + 1, QtWidgets.QTableWidgetItem(str(value))
                )

        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, thumb_width + 8)

    def _clear_detail_panel(self):
        self.session.clear_selection()
        self.detail_label.setText("Select a revision on the left.")
        self.comments_list.clear()
        for button in (self.download_btn, self.export_btn, self.add_comment_btn):
            button.setEnabled(False)

    def _update_detail_panel(self):
        rev = self.session.current
        self.detail_label.setText(
            f"<b>{rev['shot']}</b> &nbsp;|&nbsp; {rev['task_type']} &nbsp;|&nbsp; "
            f"Revision {kitsu.revision_label(rev)} &nbsp;|&nbsp; Status: {rev['status']}"
            f"<br>Artist: {rev['artist']} &nbsp;|&nbsp; Submitted: {rev['date']}"
        )

        self._reload_comments()

        self.download_btn.setEnabled(True)
        self.export_btn.setEnabled(bool(rev.get("local_path")))
        self.add_comment_btn.setEnabled(True)

    def _reload_comments(self):
        self.comments_list.clear()
        if not self.session.current:
            return
        try:
            with _busy():
                comments = self.session.comments()
        except KitsuError as exc:
            kitsu.warn(str(exc))
            self.comments_list.addItem("(Failed to load comments from Kitsu)")
            return

        for comment in comments:
            self.comments_list.addItem(kitsu.format_comment_line(comment))

    # -- slots ------------------------------------------------------------

    def _on_login_clicked(self):
        if self.session.logged_in:
            self.session.log_out()
            self._refresh_login_state()
            self._info("Logged out of Kitsu.")
            return

        server = self.server_field.text().strip()
        email = self.user_field.text().strip()

        self.login_btn.setEnabled(False)
        try:
            with _busy():
                self.session.log_in(server, email, self.pass_field.text())
        except KitsuError as exc:
            self._error(f"Server: {server}\nUser: {email}\n\n{exc}")
            return
        finally:
            self.login_btn.setEnabled(True)

        self._refresh_login_state()
        self._info(
            "Logged in to Kitsu successfully!\n\n"
            f"Server: {server}\nUser: {self.session.display_name}"
        )
        self._on_refresh_clicked()

    def _on_refresh_clicked(self):
        if not self.session.logged_in:
            return

        try:
            with _busy():
                revisions = self.session.load_revisions()
        except KitsuError as exc:
            self._error(str(exc))
            return

        self._populate_table()
        self._clear_detail_panel()

        if not revisions:
            self._info("No revisions with preview files found for your tasks.")

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows or self.session.select(rows[0].row()) is None:
            self._clear_detail_panel()
            return
        self._update_detail_panel()

    def _on_download_clicked(self):
        if not self.session.current:
            return
        rev = self.session.current

        progress = QtWidgets.QProgressDialog(
            "Downloading revision from Kitsu...", None, 0, 100, self
        )
        progress.setWindowTitle("Kitsu")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()

        def on_progress(size, total_size):
            progress.setValue(kitsu.download_percent(size, total_size))
            QtWidgets.QApplication.processEvents()

        try:
            result = self.session.download_current(on_progress)
        except KitsuError as exc:
            self._error(str(exc))
            return
        finally:
            progress.close()

        if result.paint_warning:
            self._warning(
                "The media loaded, but the existing Kitsu annotations could "
                f"not be applied to the paint node.\n\nError: {result.paint_warning}"
            )

        self.export_btn.setEnabled(True)
        self._info(
            "Downloaded and loaded into RV:\n\n"
            f"{rev['shot']} - {rev['task_type']} {kitsu.revision_label(rev)}\n"
            f"(saved to: {result.file_path})\n"
            f"Existing Kitsu annotations applied: {result.annotations_applied}\n\n"
            "Use RV's Paint tools to annotate frames directly on the viewport."
        )

    def _on_export_clicked(self):
        if not self.session.current:
            return
        rev = self.session.current

        try:
            with _busy():
                result = self.session.export_current()
        except KitsuError as exc:
            self._error(str(exc))
            return

        frames = result.annotated_frames
        frames_note = (
            f"{len(frames)} annotated frame(s): {frames}" if frames
            else "0 annotated frames"
        )
        comment_count = (
            result.comment_count if result.comment_count is not None
            else self.comments_list.count()
        )
        canvas_width, canvas_height = result.canvas

        self._info(
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task_type']}\n"
            f"Revision: {kitsu.revision_label(rev)}\n"
            f"Comments on Kitsu: {comment_count}\n"
            f"Annotations exported: {frames_note}\n"
            f"Canvas: {canvas_width:g} x {canvas_height:g}"
        )

    def _on_add_comment_clicked(self):
        text = self.comment_input.text().strip()
        if not text or not self.session.current:
            return

        self.add_comment_btn.setEnabled(False)
        try:
            with _busy():
                self.session.add_comment(text)
        except KitsuError as exc:
            self._error(str(exc))
            return
        finally:
            self.add_comment_btn.setEnabled(True)

        self.comment_input.clear()
        self._reload_comments()


class KitsuReviewMode(_MinorModeBase):
    def __init__(self):
        rv.rvtypes.MinorMode.__init__(self)
        self._panel = None
        self._dock = None
        self.init(
            "kitsu-review-mode",
            None,
            None,
            [("Kitsu Review", [("Toggle Review Panel", self.toggle_panel, None, None)])],
        )

    def _ensure_panel(self):
        if self._panel is not None:
            return

        self._panel = KitsuReviewPanel()

        main_window = rv.qtutils.sessionWindow()
        self._dock = QtWidgets.QDockWidget("Kitsu Review", main_window)
        self._dock.setWidget(self._panel)
        self._dock.setAllowedAreas(
            QtCore.Qt.BottomDockWidgetArea | QtCore.Qt.TopDockWidgetArea
        )
        self._dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetClosable
        )
        main_window.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self._dock)

    def toggle_panel(self, event=None):
        self._ensure_panel()
        visible = not self._dock.isVisible()
        self._dock.setVisible(visible)
        if visible:
            self._dock.raise_()


def createMode():
    if not _INSIDE_OPENRV:
        raise RuntimeError(
            "createMode() requires OpenRV's embedded Python environment "
            "(PySide6 + rv + gazu); this module was imported standalone."
        )
    return KitsuReviewMode()