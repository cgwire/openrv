"""UI for the Kitsu Review OpenRV plugin.

The docked panel, its event handlers (the controller layer), and the RV
MinorMode that hosts it. Handlers read widget state, call into
KitsuReviewModel (kitsu_review_model.py) for all Kitsu, session and RV
annotation work, and turn the results and errors into widget updates
and message boxes.
"""

from __future__ import annotations

import html
from datetime import datetime

from kitsu_review_model import (
    DEFAULT_SERVER,
    STATUS_FALLBACK_COLOR,
    DevView,
    DimensionsUnknownError,
    KitsuReviewModel,
    SessionRejected,
    format_fps,
    normalize_server,
    status_full_name,
    status_short_label,
)

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    import rv
    import rv.rvtypes
    import rv.qtutils

    _INSIDE_OPENRV = True
except ImportError:
    _INSIDE_OPENRV = False


_QWidgetBase = QtWidgets.QWidget if _INSIDE_OPENRV else object
_MinorModeBase = rv.rvtypes.MinorMode if _INSIDE_OPENRV else object


def _status_qcolor(status):
    color = QtGui.QColor((status or {}).get("color") or STATUS_FALLBACK_COLOR)
    return color if color.isValid() else QtGui.QColor(STATUS_FALLBACK_COLOR)


class KitsuReviewPanel(_QWidgetBase):
    """Main widget for the Kitsu Review plugin.

    Intended to be embedded as a dock widget below RV's review
    viewport (see KitsuReviewMode), not shown as its own top-level
    window.
    """

    THUMBNAIL_SIZE = (96, 54)
    REVISION_COLUMNS = ("Thumbnail", "Shot", "Task", "Rev", "Status", "Date")
    SHOT_COLUMN = 1
    STATUS_COLUMN = 4
    STATUS_ICON_SIZE = 12

    PLAYLIST_COLUMNS = ("Playlist", "Production", "To review", "Task", "Updated")
    PLAYLIST_PENDING_COLUMN = 2

    # Indices of the source tabs.
    PLAYLISTS_TAB = 0
    CHECKS_TAB = 1

    DEV_VIEWS = (
        ("OpenRV paint properties (raw)", DevView.RV),
        ("Converted to Kitsu format (export preview)", DevView.CONVERTED),
        ("Stored on Kitsu (selected revision)", DevView.SERVER),
    )
    DEV_REFRESH_MS = 1000

    def __init__(self, model=None, parent=None):
        super().__init__(parent)

        self.model = model or KitsuReviewModel()
        # Colored status swatches keyed by color.
        self._status_icon_cache = {}
        # Revision dict behind each row of the two revision tables.
        self._checks_rows = []
        self._playlist_rows = []
        # Which source tabs have been fetched since logging in (each is
        # fetched the first time it's shown).
        self._tab_loaded = {self.PLAYLISTS_TAB: False, self.CHECKS_TAB: False}

        self._build_ui()
        self._refresh_login_state()

        # After the event loop has shown the panel, try the saved session.
        QtCore.QTimer.singleShot(0, self._try_restore_session)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)

        top_bar = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("Not connected to Kitsu")
        self.status_label.setStyleSheet("font-weight: bold;")
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()

        saved_server, saved_email = self.model.saved_identity()

        self.server_field = QtWidgets.QLineEdit(saved_server or DEFAULT_SERVER)
        self.server_field.setFixedWidth(260)
        self.user_field = QtWidgets.QLineEdit(saved_email or "admin@example.com")
        self.user_field.setPlaceholderText("email")
        self.pass_field = QtWidgets.QLineEdit()
        self.pass_field.setPlaceholderText("password")
        self.pass_field.setEchoMode(QtWidgets.QLineEdit.Password)
        self.pass_field.returnPressed.connect(self._on_login_clicked)

        top_bar.addWidget(QtWidgets.QLabel("Server:"))
        top_bar.addWidget(self.server_field)
        top_bar.addWidget(QtWidgets.QLabel("User:"))
        top_bar.addWidget(self.user_field)
        top_bar.addWidget(self.pass_field)

        self.remember_check = QtWidgets.QCheckBox("Remember me")
        if self.model.secure_storage_available:
            self.remember_check.setChecked(self.model.remember)
            self.remember_check.setToolTip(
                "Stay logged in between OpenRV sessions. The Kitsu session "
                "token is kept in your system's credential store (Keychain / "
                "Credential Manager / Secret Service); your password is never "
                "saved. Log Out removes it."
            )
        else:
            self.remember_check.setChecked(False)
            self.remember_check.setEnabled(False)
            self.remember_check.setToolTip(
                "Unavailable: no secure credential store was found. Install "
                "the 'keyring' package into OpenRV's Python to enable it. "
                "Server and email are still remembered."
            )
        # Connected after setChecked so building the UI doesn't fire it.
        self.remember_check.toggled.connect(self._on_remember_toggled)
        top_bar.addWidget(self.remember_check)

        self.login_btn = QtWidgets.QPushButton("Log In")
        self.login_btn.clicked.connect(self._on_login_clicked)
        top_bar.addWidget(self.login_btn)

        root.addLayout(top_bar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # Where revisions come from: the Playlists tab (main) or My Checks.
        # Both feed the same detail panel on the right.
        self.source_tabs = QtWidgets.QTabWidget()
        self.source_tabs.addTab(self._build_playlists_tab(), "Playlists")
        self.source_tabs.addTab(self._build_checks_tab(), "My Checks")
        self.source_tabs.currentChanged.connect(self._on_source_tab_changed)
        splitter.addWidget(self.source_tabs)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)

        self.detail_label = QtWidgets.QLabel("Select a revision on the left.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("font-size: 13px;")
        right_layout.addWidget(self.detail_label)

        action_row = QtWidgets.QHBoxLayout()
        self.download_btn = QtWidgets.QPushButton("Download + Load in RV")
        self.download_btn.clicked.connect(self._on_download_clicked)
        self.export_btn = QtWidgets.QPushButton("Export to Kitsu")
        self.export_btn.clicked.connect(self._on_export_clicked)
        for b in (self.download_btn, self.export_btn):
            b.setEnabled(False)
            action_row.addWidget(b)
        right_layout.addLayout(action_row)

        hint = QtWidgets.QLabel(
            "Tip: use RV's own Paint tools to annotate the frame directly. "
            "Annotations are picked up automatically on export."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-style: italic; color: #666;")
        right_layout.addWidget(hint)

        right_layout.addWidget(QtWidgets.QLabel("Comments"))
        self.comments_list = QtWidgets.QListWidget()
        self.comments_list.setWordWrap(True)
        icon_px = self.STATUS_ICON_SIZE
        self.comments_list.setIconSize(QtCore.QSize(icon_px, icon_px))
        right_layout.addWidget(self.comments_list, stretch=1)

        comment_row = QtWidgets.QHBoxLayout()
        self.comment_input = QtWidgets.QLineEdit()
        self.comment_input.setPlaceholderText("Write a review comment...")
        self.comment_input.returnPressed.connect(self._on_add_comment_clicked)

        self.status_combo = QtWidgets.QComboBox()
        self.status_combo.setToolTip(
            "Status the task will have after this comment. Leave it on the "
            "current status to comment without changing it."
        )
        self.status_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.status_combo.setEnabled(False)
        self.status_combo.currentIndexChanged.connect(self._update_comment_button)

        self.add_comment_btn = QtWidgets.QPushButton("Add Comment")
        self.add_comment_btn.clicked.connect(self._on_add_comment_clicked)
        self.add_comment_btn.setEnabled(False)
        comment_row.addWidget(self.comment_input, stretch=1)
        comment_row.addWidget(QtWidgets.QLabel("Status:"))
        comment_row.addWidget(self.status_combo)
        comment_row.addWidget(self.add_comment_btn)
        right_layout.addLayout(comment_row)

        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.addTab(right, "Review")
        self.dev_tab = self._build_dev_tab()
        self.right_tabs.addTab(self.dev_tab, "Dev")
        self.right_tabs.currentChanged.connect(self._on_right_tab_changed)

        splitter.addWidget(self.right_tabs)
        splitter.setSizes([420, 480])

    def _make_revision_table(self):
        table = QtWidgets.QTableWidget(0, len(self.REVISION_COLUMNS))
        table.setHorizontalHeaderLabels(list(self.REVISION_COLUMNS))
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.itemSelectionChanged.connect(lambda: self._on_revision_selected(table))
        return table

    @staticmethod
    def _make_info_label():
        label = QtWidgets.QLabel("")
        label.setWordWrap(True)
        label.setStyleSheet("color: #888;")
        return label

    def _build_playlists_tab(self):
        """Main tab: the productions' playlists, and the shots of the
        selected one."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Playlists awaiting review"))
        controls.addStretch()
        self.show_all_playlists_check = QtWidgets.QCheckBox("Show all playlists")
        self.show_all_playlists_check.setToolTip(
            "Also list playlists with nothing left to review, i.e. no shot "
            "whose task is in a feedback-request status (WFA and the like)."
        )
        self.show_all_playlists_check.toggled.connect(lambda _c: self._apply_playlist_filter())
        controls.addWidget(self.show_all_playlists_check)
        self.refresh_playlists_btn = QtWidgets.QPushButton("Refresh Playlists")
        self.refresh_playlists_btn.clicked.connect(self._on_refresh_playlists_clicked)
        controls.addWidget(self.refresh_playlists_btn)
        layout.addLayout(controls)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.playlist_table = QtWidgets.QTableWidget(0, len(self.PLAYLIST_COLUMNS))
        self.playlist_table.setHorizontalHeaderLabels(list(self.PLAYLIST_COLUMNS))
        self.playlist_table.horizontalHeader().setStretchLastSection(True)
        self.playlist_table.verticalHeader().setVisible(False)
        self.playlist_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.playlist_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.playlist_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.playlist_table.itemSelectionChanged.connect(self._on_playlist_selected)
        split.addWidget(self.playlist_table)

        self.playlist_rev_table = self._make_revision_table()
        split.addWidget(self.playlist_rev_table)
        split.setSizes([160, 320])
        layout.addWidget(split, stretch=1)

        self.playlists_info = self._make_info_label()
        layout.addWidget(self.playlists_info)
        return tab

    def _build_checks_tab(self):
        """Secondary tab: the latest revision of each of the user's tasks."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.addWidget(QtWidgets.QLabel("Latest revision of each of your tasks"))

        self.checks_table = self._make_revision_table()
        layout.addWidget(self.checks_table, stretch=1)

        self.checks_info = self._make_info_label()
        layout.addWidget(self.checks_info)

        self.refresh_checks_btn = QtWidgets.QPushButton("Refresh My Checks")
        self.refresh_checks_btn.clicked.connect(self._on_refresh_checks_clicked)
        layout.addWidget(self.refresh_checks_btn)
        return tab

    def _build_dev_tab(self):
        """Developer view: the annotations of whatever is in the RV
        viewport right now, dumped as JSON."""
        dev = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(dev)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("View:"))
        self.dev_view_combo = QtWidgets.QComboBox()
        for label, key in self.DEV_VIEWS:
            self.dev_view_combo.addItem(label, key)
        self.dev_view_combo.currentIndexChanged.connect(lambda _i: self._refresh_dev_view())
        controls.addWidget(self.dev_view_combo, stretch=1)

        self.dev_scope_check = QtWidgets.QCheckBox("Current source only")
        self.dev_scope_check.setChecked(True)
        self.dev_scope_check.setToolTip(
            "Only show annotations from the paint node(s) of the source(s) "
            "under the playhead, including the sequence-level ones RV draws "
            "into. Uncheck to show exactly what Export to Kitsu reads: every "
            "paint node drawing on the selected revision's source."
        )
        self.dev_scope_check.toggled.connect(lambda _c: self._refresh_dev_view())
        controls.addWidget(self.dev_scope_check)

        self.dev_auto_check = QtWidgets.QCheckBox("Auto-refresh")
        self.dev_auto_check.setChecked(True)
        self.dev_auto_check.setToolTip(
            "Re-read the RV session every second while this tab is open "
            "(not used for the Kitsu server view, to avoid polling the server)."
        )
        self.dev_auto_check.toggled.connect(self._update_dev_timer)
        controls.addWidget(self.dev_auto_check)

        dev_refresh_btn = QtWidgets.QPushButton("Refresh")
        dev_refresh_btn.clicked.connect(self._refresh_dev_view)
        controls.addWidget(dev_refresh_btn)

        dev_copy_btn = QtWidgets.QPushButton("Copy JSON")
        dev_copy_btn.clicked.connect(self._on_dev_copy_clicked)
        controls.addWidget(dev_copy_btn)

        layout.addLayout(controls)

        self.dev_status_label = QtWidgets.QLabel("")
        self.dev_status_label.setStyleSheet("color: #888;")
        layout.addWidget(self.dev_status_label)

        self.dev_json_view = QtWidgets.QPlainTextEdit()
        self.dev_json_view.setReadOnly(True)
        self.dev_json_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.dev_json_view.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        layout.addWidget(self.dev_json_view, stretch=1)

        self._dev_timer = QtCore.QTimer(self)
        self._dev_timer.setInterval(self.DEV_REFRESH_MS)
        self._dev_timer.timeout.connect(self._on_dev_timer)

        return dev

    # ------------------------------------------------------------------
    # Dev tab handlers
    # ------------------------------------------------------------------

    def _dev_tab_active(self):
        return (
            hasattr(self, "right_tabs")
            and self.right_tabs.currentWidget() is self.dev_tab
            and self.isVisible()
        )

    def _on_right_tab_changed(self, _index):
        if self.right_tabs.currentWidget() is self.dev_tab:
            self._refresh_dev_view()
        self._update_dev_timer()

    def _update_dev_timer(self, *_args):
        should_run = (
            self.dev_auto_check.isChecked()
            and self.right_tabs.currentWidget() is self.dev_tab
        )
        if should_run and not self._dev_timer.isActive():
            self._dev_timer.start()
        elif not should_run and self._dev_timer.isActive():
            self._dev_timer.stop()

    def _on_dev_timer(self):
        # The dock can be hidden while the tab stays selected; skip work then.
        if not self.isVisible():
            return
        if self.dev_view_combo.currentData() == DevView.SERVER:
            return
        self._refresh_dev_view()

    def _refresh_dev_view_if_active(self):
        if self._dev_tab_active():
            self._refresh_dev_view()

    def _on_dev_copy_clicked(self):
        QtWidgets.QApplication.clipboard().setText(self.dev_json_view.toPlainText())
        self.dev_status_label.setText("JSON copied to clipboard.")

    def _refresh_dev_view(self):
        if not hasattr(self, "dev_json_view"):
            return
        text, status = self.model.build_dev_view(
            self.dev_view_combo.currentData(), self.dev_scope_check.isChecked()
        )

        # Only touch the widget when the content changed, so auto-refresh
        # doesn't reset the scroll position or text selection every second.
        if text != self.dev_json_view.toPlainText():
            v_bar = self.dev_json_view.verticalScrollBar()
            h_bar = self.dev_json_view.horizontalScrollBar()
            v_pos, h_pos = v_bar.value(), h_bar.value()
            self.dev_json_view.setPlainText(text)
            v_bar.setValue(v_pos)
            h_bar.setValue(h_pos)

        self.dev_status_label.setText(f"{status}  ·  last read {datetime.now():%H:%M:%S}")

    # ------------------------------------------------------------------
    # Login handlers
    # ------------------------------------------------------------------

    def _refresh_login_state(self):
        connected = self.model.logged_in
        self.status_label.setText(
            f"Connected to Kitsu as {self.model.display_name()}" if connected
            else "Not connected to Kitsu"
        )
        self.status_label.setStyleSheet(
            "font-weight: bold; color: #2e7d32;" if connected
            else "font-weight: bold; color: #b71c1c;"
        )
        self.login_btn.setText("Log Out" if connected else "Log In")
        for widget in (self.server_field, self.user_field, self.pass_field):
            widget.setEnabled(not connected)
        self.refresh_playlists_btn.setEnabled(connected)
        self.refresh_checks_btn.setEnabled(connected)
        if not connected:
            self._checks_rows, self._playlist_rows = [], []
            for table in (self.playlist_table, self.playlist_rev_table, self.checks_table):
                table.setRowCount(0)
            self.playlists_info.setText("")
            self.checks_info.setText("")
            self._tab_loaded = dict.fromkeys(self._tab_loaded, False)
            self._clear_detail_panel()

    def _on_login_clicked(self):
        if self.model.logged_in:
            self.model.log_out()
            self._refresh_login_state()
            QtWidgets.QMessageBox.information(self, "Kitsu", "Logged out of Kitsu.")
            return

        server = normalize_server(self.server_field.text())
        email = self.user_field.text().strip()
        password = self.pass_field.text()

        if not server or not email or not password:
            QtWidgets.QMessageBox.warning(
                self, "Kitsu", "Please enter a server URL, email, and password."
            )
            return

        self.login_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            user = self.model.authenticate(server, email, password)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.login_btn.setEnabled(True)
            QtWidgets.QMessageBox.critical(
                self, "Kitsu",
                f"Login failed.\n\nServer: {server}\nUser: {email}\n\nError: {exc}"
            )
            return

        QtWidgets.QApplication.restoreOverrideCursor()
        self.login_btn.setEnabled(True)
        # Don't keep the password around in the widget once it's been used.
        self.pass_field.clear()

        self.model.complete_login(user, server, email)
        self._on_logged_in(server, announce=True)

    def _on_logged_in(self, server, announce):
        self._refresh_login_state()
        if announce:
            QtWidgets.QMessageBox.information(
                self, "Kitsu",
                f"Logged in to Kitsu successfully!\n\nServer: {server}\n"
                f"User: {self.model.display_name()}"
            )
        self._refresh_source_tab(self.source_tabs.currentIndex())

    def _on_remember_toggled(self, checked):
        self.model.set_remember(checked)

    def _try_restore_session(self):
        """Log back in with the session saved by "Remember me", if any."""
        if self.model.logged_in or not self.remember_check.isChecked():
            return
        server = normalize_server(self.server_field.text())
        email = self.user_field.text().strip()
        if not server or not email:
            return
        tokens = self.model.saved_tokens(server, email)
        if not tokens:
            return

        self.status_label.setText("Restoring Kitsu session...")
        self.login_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            user = self.model.restore_session(server, email, tokens)
        except SessionRejected:
            self.status_label.setText("Saved Kitsu session expired -- please log in")
            return
        except Exception:
            self.status_label.setText(
                "Could not reach Kitsu to restore your session -- log in or try again later"
            )
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.login_btn.setEnabled(True)

        self.model.complete_login(user, server, email)
        self._on_logged_in(server, announce=False)

    # ------------------------------------------------------------------
    # Status presentation
    # ------------------------------------------------------------------

    def _status_icon(self, status):
        """Small rounded swatch in the status's Kitsu color (cached)."""
        color = _status_qcolor(status)
        key = color.name()
        icon = self._status_icon_cache.get(key)
        if icon is None:
            size = self.STATUS_ICON_SIZE
            pixmap = QtGui.QPixmap(size, size)
            pixmap.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(pixmap)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtGui.QPen(color.darker(150), 1))
            painter.setBrush(color)
            painter.drawRoundedRect(QtCore.QRectF(0.5, 0.5, size - 1, size - 1), 3, 3)
            painter.end()
            icon = QtGui.QIcon(pixmap)
            self._status_icon_cache[key] = icon
        return icon

    def _populate_status_combo(self):
        """Fill the status dropdown for the selected revision's task and
        preselect the task's current status."""
        self.status_combo.blockSignals(True)
        self.status_combo.clear()
        rev = self.model.current_revision
        if rev:
            task = rev["task"]
            current_id = task.get("task_status_id")
            for status in self.model.selectable_statuses(task):
                label = status_full_name(status)
                if status.get("id") == current_id:
                    label += "  (current)"
                self.status_combo.addItem(self._status_icon(status), label, status.get("id"))
            index = self.status_combo.findData(current_id)
            if index >= 0:
                self.status_combo.setCurrentIndex(index)
        self.status_combo.setEnabled(bool(rev) and self.status_combo.count() > 0)
        self.status_combo.blockSignals(False)
        self._update_comment_button()

    def _selected_status_id(self):
        """Status chosen in the dropdown, or the task's current status when
        the dropdown is empty (e.g. the status catalog failed to load)."""
        rev = self.model.current_revision
        if not rev:
            return None
        return self.status_combo.currentData() or rev["task"].get("task_status_id")

    def _status_change_pending(self):
        rev = self.model.current_revision
        if not rev:
            return False
        selected = self._selected_status_id()
        return bool(selected) and selected != rev["task"].get("task_status_id")

    def _update_comment_button(self, *_args):
        self.add_comment_btn.setText(
            "Add Comment + Set Status" if self._status_change_pending() else "Add Comment"
        )

    def _set_status_cell(self, table, row, rev):
        """Status text and swatch; revisions waiting on review are shown
        in bold."""
        item = table.item(row, self.STATUS_COLUMN)
        if item is None:
            item = QtWidgets.QTableWidgetItem()
            table.setItem(row, self.STATUS_COLUMN, item)
        item.setText(str(rev["status"]))
        status = self.model.status_for_id(rev["task"].get("task_status_id"))
        tooltip = status_full_name(status) if status else str(rev["status"])
        if status:
            item.setIcon(self._status_icon(status))

        pending = self.model.is_pending_review(rev["task"])
        if pending:
            tooltip += " -- awaiting review"
        item.setToolTip(tooltip)
        for column in (self.SHOT_COLUMN, self.STATUS_COLUMN):
            cell = table.item(row, column)
            if cell is not None:
                font = cell.font()
                font.setBold(pending)
                cell.setFont(font)

    def _refresh_status_cells(self):
        """Redraw every listed revision's status (after a status change,
        which can touch the same task on both tabs) and the playlists'
        to-review counts."""
        for table, rows in (
            (self.checks_table, self._checks_rows),
            (self.playlist_rev_table, self._playlist_rows),
        ):
            for row, rev in enumerate(rows):
                self._set_status_cell(table, row, rev)
        for row in range(self.playlist_table.rowCount()):
            self._set_playlist_pending_cell(row)

    # ------------------------------------------------------------------
    # Revision table
    # ------------------------------------------------------------------

    def _make_thumbnail_widget(self, preview_file_id):
        label = QtWidgets.QLabel()
        label.setAlignment(QtCore.Qt.AlignCenter)

        pixmap = None
        data = self.model.thumbnail_bytes(preview_file_id)
        if data:
            image = QtGui.QImage()
            if image.loadFromData(data):
                pixmap = QtGui.QPixmap.fromImage(image)

        if pixmap and not pixmap.isNull():
            tw, th = self.THUMBNAIL_SIZE
            label.setPixmap(
                pixmap.scaled(tw, th, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            )
        else:
            label.setText("(no thumbnail)")
            label.setStyleSheet("color: #888; font-style: italic;")
        return label

    def _fill_revision_table(self, table, revisions):
        table.setRowCount(0)
        for row, rev in enumerate(revisions):
            table.insertRow(row)
            table.setRowHeight(row, self.THUMBNAIL_SIZE[1] + 10)

            thumb_widget = self._make_thumbnail_widget(rev["preview_file"].get("id"))
            table.setCellWidget(row, 0, thumb_widget)

            values = [
                rev["shot"], rev["task_type"], f"v{rev['revision']:03d}",
                rev["status"], rev["date"],
            ]
            for offset, value in enumerate(values):
                table.setItem(row, offset + 1, QtWidgets.QTableWidgetItem(str(value)))
            self._set_status_cell(table, row, rev)

        table.resizeColumnsToContents()
        table.setColumnWidth(0, self.THUMBNAIL_SIZE[0] + 8)

    # ------------------------------------------------------------------
    # Source tabs (Playlists / My Checks)
    # ------------------------------------------------------------------

    def _current_revision_table(self):
        if self.source_tabs.currentIndex() == self.CHECKS_TAB:
            return self.checks_table
        return self.playlist_rev_table

    def _rows_of(self, table):
        return self._checks_rows if table is self.checks_table else self._playlist_rows

    def _refresh_source_tab(self, index):
        if index == self.CHECKS_TAB:
            self._on_refresh_checks_clicked()
        else:
            self._on_refresh_playlists_clicked()

    def _on_source_tab_changed(self, index):
        """Fetch a tab the first time it's shown, and point the detail panel
        at whatever is selected on the tab now in front."""
        if self.model.logged_in and not self._tab_loaded.get(index):
            self._refresh_source_tab(index)
            return
        self._on_revision_selected(self._current_revision_table())

    def _on_revision_selected(self, table):
        # A table on the hidden tab keeps its selection but doesn't drive
        # the detail panel; switching tabs re-syncs it.
        if table is not self._current_revision_table():
            return
        rows = table.selectionModel().selectedRows()
        revisions = self._rows_of(table)
        if not rows or rows[0].row() >= len(revisions):
            self._clear_detail_panel()
            return
        rev = revisions[rows[0].row()]
        if rev is self.model.current_revision:
            return
        self.model.select_revision(rev)
        self._update_detail_panel()

    # -- My Checks ------------------------------------------------------

    def _on_refresh_checks_clicked(self):
        """Pull the current user's tasks from Kitsu and list the latest
        revision (preview file) of each."""
        if not self.model.logged_in or not self.model.current_user:
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            revisions = self.model.load_my_checks()
            self._checks_rows = revisions
            self._fill_revision_table(self.checks_table, revisions)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Failed to fetch tasks: {exc}")
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self._tab_loaded[self.CHECKS_TAB] = True
        self.checks_info.setText(
            f"{len(revisions)} revision(s)." if revisions
            else "No revisions with preview files found for your tasks."
        )
        if self.source_tabs.currentIndex() == self.CHECKS_TAB:
            self._clear_detail_panel()

    # -- Playlists ------------------------------------------------------

    def _on_refresh_playlists_clicked(self):
        """List the playlists of the user's open productions."""
        if not self.model.logged_in or not self.model.current_user:
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            playlists = self.model.load_playlists()
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Failed to fetch playlists: {exc}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        self._tab_loaded[self.PLAYLISTS_TAB] = True
        self._playlist_rows = []
        self.playlist_rev_table.setRowCount(0)

        table = self.playlist_table
        table.blockSignals(True)
        table.setRowCount(0)
        for row, summary in enumerate(playlists):
            table.insertRow(row)
            name = summary["name"] + ("  (client)" if summary["for_client"] else "")
            values = {
                0: name,
                1: summary["project"],
                3: summary["task_type"] or "-",
                4: summary["updated"],
            }
            for column, value in values.items():
                table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
            self._set_playlist_pending_cell(row)
        table.blockSignals(False)
        table.resizeColumnsToContents()

        self._apply_playlist_filter()
        if self.source_tabs.currentIndex() == self.PLAYLISTS_TAB:
            self._clear_detail_panel()

    def _set_playlist_pending_cell(self, row):
        """"To review" cell: pending / reviewable entries. Before a playlist
        is opened the pending count is an estimate (from the tasks awaiting
        review on its shots); opening it makes it exact."""
        if row >= len(self.model.playlists):
            return
        summary = self.model.playlists[row]
        pending = summary["pending_count"]
        text = f"{'?' if pending is None else pending} / {summary['entry_count']}"
        if pending is None:
            tip = "Couldn't work out how many shots are awaiting review."
        elif summary["pending_exact"]:
            tip = f"{pending} of {summary['entry_count']} revision(s) awaiting review."
        else:
            tip = (f"About {pending} of {summary['entry_count']} shot(s) awaiting "
                   "review (exact once the playlist is opened).")
            text = "~" + text

        item = self.playlist_table.item(row, self.PLAYLIST_PENDING_COLUMN)
        if item is None:
            item = QtWidgets.QTableWidgetItem()
            self.playlist_table.setItem(row, self.PLAYLIST_PENDING_COLUMN, item)
        item.setText(text)
        item.setToolTip(tip)
        bold = bool(pending)
        for column in range(self.playlist_table.columnCount()):
            cell = self.playlist_table.item(row, column)
            if cell is not None:
                font = cell.font()
                font.setBold(bold)
                cell.setFont(font)

    @staticmethod
    def _playlist_is_pending(summary):
        # Unknown counts stay visible rather than hiding work.
        return summary["pending_count"] is None or summary["pending_count"] > 0

    def _apply_playlist_filter(self):
        """Hide playlists with nothing to review unless "Show all" is on.
        The selected playlist always stays visible, so a review that brings
        its count to zero doesn't pull it out from under the reviewer."""
        show_all = self.show_all_playlists_check.isChecked()
        selected = {i.row() for i in self.playlist_table.selectionModel().selectedRows()}
        playlists = self.model.playlists
        visible = 0
        for row, summary in enumerate(playlists):
            hide = not (show_all or self._playlist_is_pending(summary) or row in selected)
            self.playlist_table.setRowHidden(row, hide)
            visible += not hide

        if not playlists:
            text = "No playlists found in your open productions."
        else:
            pending = sum(1 for s in playlists if self._playlist_is_pending(s))
            text = f"{pending} of {len(playlists)} playlist(s) awaiting review."
            if not show_all and visible < len(playlists):
                text += f" {len(playlists) - visible} hidden (nothing to review)."
            if not self.model.feedback_status_ids():
                text += (" No task status is flagged as a feedback request in Kitsu, "
                         "so awaiting-review counts are unknown.")
        self.playlists_info.setText(text)

    def _on_playlist_selected(self):
        rows = self.playlist_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.model.playlists):
            self._playlist_rows = []
            self.playlist_rev_table.setRowCount(0)
            return
        row = rows[0].row()
        summary = self.model.playlists[row]

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            revisions, skipped = self.model.load_playlist_revisions(summary)
            self._playlist_rows = revisions
            self._fill_revision_table(self.playlist_rev_table, revisions)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, "Kitsu", f"Failed to load playlist {summary['name']!r}: {exc}"
            )
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self._set_playlist_pending_cell(row)
        pending = sum(1 for rev in revisions if self.model.is_pending_review(rev["task"]))
        text = f"{summary['name']}: {len(revisions)} revision(s), {pending} awaiting review."
        if skipped:
            text += (f" Skipped {skipped} shot(s) with no readable revision "
                     "(see the console for details).")
        self.playlists_info.setText(text)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _clear_detail_panel(self):
        self.model.select_revision(None)
        self.detail_label.setText("Select a revision on the left.")
        self.comments_list.clear()
        for b in (self.download_btn, self.export_btn, self.add_comment_btn):
            b.setEnabled(False)
        self._populate_status_combo()
        self._refresh_dev_view_if_active()

    def _update_detail_label(self):
        rev = self.model.current_revision
        status = self.model.status_for_id(rev["task"].get("task_status_id"))
        if status:
            color = _status_qcolor(status).name()
            status_html = (
                f"<span style='background-color: {color}; color: #111; "
                f"padding: 0 4px;'>&nbsp;{status_short_label(status)}&nbsp;</span> "
                f"{rev['status']}"
            )
        else:
            status_html = str(rev["status"])
        fps_html = f"{format_fps(rev.get('fps'))} fps"
        if not rev.get("fps_from_project"):
            fps_html += " <i>(default; production FPS not set in Kitsu)</i>"
        playlist = self.model.playlist_for_revision(rev)
        playlist_html = (
            f" &nbsp;|&nbsp; Playlist: {html.escape(playlist['name'])}" if playlist else ""
        )
        self.detail_label.setText(
            f"<b>{rev['shot']}</b> &nbsp;|&nbsp; {rev['task_type']} &nbsp;|&nbsp; "
            f"Revision v{rev['revision']:03d} &nbsp;|&nbsp; Status: {status_html}"
            f"<br>Artist: {rev['artist']} &nbsp;|&nbsp; Submitted: {rev['date']}"
            f" &nbsp;|&nbsp; FPS: {fps_html}{playlist_html}"
        )

    def _update_detail_panel(self):
        self._update_detail_label()
        self._reload_comments()
        self._populate_status_combo()

        self.download_btn.setEnabled(True)
        # Export needs the revision loaded into RV (its frame base and size).
        self.export_btn.setEnabled(bool(self.model.current_revision.get("loaded")))
        self.add_comment_btn.setEnabled(True)
        self._refresh_dev_view_if_active()

    def _reload_comments(self):
        """Show the selected revision's task comments, each tagged with the
        status it was posted with."""
        self.comments_list.clear()
        rev = self.model.current_revision
        if not rev:
            return
        task = rev["task"]

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            comments = self.model.load_comments(task)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            print(f"[KitsuReview] Could not fetch comments for task {task.get('id')}: {exc}")
            self.comments_list.addItem("(Failed to load comments from Kitsu)")
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        for comment in comments:
            status = comment["status"]
            item = QtWidgets.QListWidgetItem(
                f"[{comment['date']}] [{status_short_label(status)}] "
                f"{comment['author']}: {comment['text']}"
            )
            if status:
                item.setIcon(self._status_icon(status))
                item.setToolTip(f"Status: {status_full_name(status)}")
            self.comments_list.addItem(item)

        self.comments_list.scrollToBottom()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add_comment_clicked(self):
        """Post a comment with the status chosen in the dropdown. Picking a
        different status changes the task status; leaving it on the current
        one keeps it as is. A status change may be posted without any text,
        as in Kitsu's own UI."""
        rev = self.model.current_revision
        if not rev:
            return
        text = self.comment_input.text().strip()
        if not text and not self._status_change_pending():
            return

        status_id = self._selected_status_id()
        if not status_id:
            QtWidgets.QMessageBox.critical(
                self, "Kitsu", "Could not determine a status for this comment; comment not sent."
            )
            return

        self.add_comment_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            status_changed = self.model.add_comment(rev, text, status_id)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.add_comment_btn.setEnabled(True)
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Failed to post comment: {exc}")
            return

        QtWidgets.QApplication.restoreOverrideCursor()
        self.add_comment_btn.setEnabled(True)
        self.comment_input.clear()

        if status_changed:
            self._refresh_status_cells()
            self._update_detail_label()

        self._populate_status_combo()
        self._reload_comments()

    def _on_download_clicked(self):
        """Download the selected revision, load it into RV and re-apply
        the annotations Kitsu already has for it."""
        rev = self.model.current_revision
        if not rev:
            return

        progress = QtWidgets.QProgressDialog("Downloading revision from Kitsu...", None, 0, 100, self)
        progress.setWindowTitle("Kitsu")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()

        def _on_progress(pct):
            progress.setValue(pct)
            QtWidgets.QApplication.processEvents()

        try:
            file_path = self.model.download_preview(rev, _on_progress)
        except Exception as exc:
            progress.close()
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Download failed: {exc}")
            return

        progress.setValue(100)
        progress.close()

        if self.model.load_into_rv(rev, file_path):
            QtWidgets.QMessageBox.warning(
                self, "Kitsu",
                "Could not determine this preview's pixel dimensions, so its "
                "existing annotations were skipped.\n\nThe media itself has "
                "still been loaded into RV."
            )

        self.export_btn.setEnabled(True)
        self._refresh_dev_view_if_active()

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            f"Downloaded and loaded into RV:\n\n{rev['shot']} - {rev['task_type']} v{rev['revision']:03d}\n"
            f"(saved to: {file_path})\n"
            f"Playback set to {format_fps(rev.get('fps'))} fps"
            f"{' (production setting)' if rev.get('fps_from_project') else ' (default)'}\n\n"
            "Use RV's Paint tools to annotate frames directly on the viewport."
        )

    def _on_export_clicked(self):
        rev = self.model.current_revision
        if not rev:
            return

        try:
            summary = self.model.export_annotations(
                rev, fallback_comment_count=self.comments_list.count()
            )
        except DimensionsUnknownError as exc:
            QtWidgets.QMessageBox.critical(
                self, "Kitsu",
                f"Cannot export annotations: {exc}\n\n"
                "Download the revision first so its size can be read from "
                "the file."
            )
            return
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Annotation export failed: {exc}")
            return

        self._refresh_dev_view_if_active()

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task_type']}\n"
            f"Revision: v{rev['revision']:03d}\n"
            f"Comments on Kitsu: {summary['n_comments']}\n"
            f"Annotations exported: {summary['frames_note']}"
        )


class KitsuReviewMode(_MinorModeBase):
    """OpenRV MinorMode that docks a 'Kitsu Review' panel below the viewport."""

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
        self._dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea | QtCore.Qt.TopDockWidgetArea)
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
    """Entry point OpenRV calls to instantiate this plugin's mode."""
    if not _INSIDE_OPENRV:
        raise RuntimeError(
            "createMode() requires OpenRV's embedded Python environment "
            "(PySide6 + rv + gazu); this module was imported standalone."
        )
    return KitsuReviewMode()