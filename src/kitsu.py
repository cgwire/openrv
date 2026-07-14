#!/usr/bin/env python3
"""
Mock OpenRV plugin: "Kitsu Review"
-----------------------------------
Proof-of-concept plugin for OpenRV that lets a user:
  1. "Connect" to Kitsu (fake login)
  2. Browse mock shots/tasks and pick a revision to review
  3. "Download" the revision (simulated) and load it into the RV session
  4. Annotate directly on the RV frame using RV's own Paint tools
  5. Add review comments
  6. "Export" comments + annotations (parsed from the RV session graph)
     back to Kitsu (simulated)

This plugin is designed to run only inside OpenRV: the panel is docked
below the review viewport rather than shown as a standalone window.

Replace the MOCK_* sections and the `# MOCK:` blocks with real Kitsu
API calls when you're ready to move past the POC. The RV-side node
parsing in `_gather_rv_annotations` uses the real RV command API where
possible, but the exact per-frame paint property paths can vary between
RV versions/builds -- double check those against the RV build you are
targeting before shipping.
"""

from datetime import datetime, timedelta

from PySide6 import QtCore, QtGui, QtWidgets

import rv
import rv.rvtypes
import rv.commands as rvc
import rv.qtutils

import re

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

# ===========================================================================
# MOCK DATA
# ===========================================================================

MOCK_USER = {"full_name": "Alex Rivera", "email": "alex.rivera@studio.example"}

def _fake_date(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")

MOCK_REVISIONS = [
    {
        "shot": "SEQ010_SH0010", "task": "Lighting", "revision": 4,
        "status": "Pending Review", "artist": "J. Chen",
        "date": _fake_date(1), "comments": [],
    },
    {
        "shot": "SEQ010_SH0020", "task": "Compositing", "revision": 2,
        "status": "Approved", "artist": "M. Duarte",
        "date": _fake_date(3), "comments": [
            {"author": "S. Okafor", "date": _fake_date(2), "text": "Looks good, just tighten the rim light."},
        ],
    },
    {
        "shot": "SEQ020_SH0005", "task": "Animation", "revision": 7,
        "status": "In Progress", "artist": "R. Novak",
        "date": _fake_date(0), "comments": [],
    },
    {
        "shot": "SEQ020_SH0030", "task": "FX", "revision": 1,
        "status": "Pending Review", "artist": "A. Rivera",
        "date": _fake_date(5), "comments": [
            {"author": "P. Lindqvist", "date": _fake_date(4), "text": "Sim reads a bit slow, can we speed up 10%?"},
        ],
    },
]


class KitsuReviewPanel(QtWidgets.QWidget):
    """Main widget for the Kitsu Review plugin.

    Intended to be embedded as a dock widget below RV's review
    viewport (see KitsuReviewMode), not shown as its own top-level
    window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.logged_in = False
        self.current_revision = None

        self._build_ui()
        self._refresh_login_state()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)

        top_bar = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("Not connected to Kitsu")
        self.status_label.setStyleSheet("font-weight: bold;")
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()

        self.server_field = QtWidgets.QLineEdit("https://kitsu.studio.example/api")
        self.server_field.setFixedWidth(260)
        self.user_field = QtWidgets.QLineEdit("alex.rivera")
        self.user_field.setPlaceholderText("username")
        self.pass_field = QtWidgets.QLineEdit()
        self.pass_field.setPlaceholderText("password")
        self.pass_field.setEchoMode(QtWidgets.QLineEdit.Password)

        top_bar.addWidget(QtWidgets.QLabel("Server:"))
        top_bar.addWidget(self.server_field)
        top_bar.addWidget(QtWidgets.QLabel("User:"))
        top_bar.addWidget(self.user_field)
        top_bar.addWidget(self.pass_field)

        self.login_btn = QtWidgets.QPushButton("Log In")
        self.login_btn.clicked.connect(self._on_login_clicked)
        top_bar.addWidget(self.login_btn)

        root.addLayout(top_bar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.addWidget(QtWidgets.QLabel("Revisions available for review"))

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Shot", "Task", "Rev", "Status", "Date"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.table)

        self.refresh_btn = QtWidgets.QPushButton("Refresh Revisions")
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        left_layout.addWidget(self.refresh_btn)

        splitter.addWidget(left)

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
        right_layout.addWidget(self.comments_list, stretch=1)

        comment_row = QtWidgets.QHBoxLayout()
        self.comment_input = QtWidgets.QLineEdit()
        self.comment_input.setPlaceholderText("Write a review comment...")
        self.add_comment_btn = QtWidgets.QPushButton("Add Comment")
        self.add_comment_btn.clicked.connect(self._on_add_comment_clicked)
        self.add_comment_btn.setEnabled(False)
        comment_row.addWidget(self.comment_input)
        comment_row.addWidget(self.add_comment_btn)
        right_layout.addLayout(comment_row)

        splitter.addWidget(right)
        splitter.setSizes([420, 480])

    def _refresh_login_state(self):
        connected = self.logged_in
        self.status_label.setText(
            f"Connected to Kitsu as {MOCK_USER['full_name']}" if connected
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
            self._clear_detail_panel()

    def _on_login_clicked(self):
        if self.logged_in:
            # --- Logout ---
            self.logged_in = False
            self._refresh_login_state()
            QtWidgets.QMessageBox.information(self, "Kitsu", "Logged out of Kitsu.")
            return

        server = self.server_field.text().strip()
        user = self.user_field.text().strip()
        if not server or not user:
            QtWidgets.QMessageBox.warning(self, "Kitsu", "Please enter a server URL and username.")
            return

        self.logged_in = True
        self._refresh_login_state()
        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            f"Logged in to Kitsu successfully!\n\nServer: {server}\nUser: {MOCK_USER['full_name']}"
        )
        self._on_refresh_clicked()

    def _on_refresh_clicked(self):
        self.table.setRowCount(0)
        for row, rev in enumerate(MOCK_REVISIONS):
            self.table.insertRow(row)
            values = [rev["shot"], rev["task"], f"v{rev['revision']:03d}", rev["status"], rev["date"]]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._clear_detail_panel()
            return
        index = rows[0].row()
        self.current_revision = MOCK_REVISIONS[index]
        self._update_detail_panel()

    def _clear_detail_panel(self):
        self.current_revision = None
        self.detail_label.setText("Select a revision on the left.")
        self.comments_list.clear()
        for b in (self.download_btn, self.export_btn, self.add_comment_btn):
            b.setEnabled(False)

    def _update_detail_panel(self):
        rev = self.current_revision
        self.detail_label.setText(
            f"<b>{rev['shot']}</b> &nbsp;|&nbsp; {rev['task']} &nbsp;|&nbsp; "
            f"Revision v{rev['revision']:03d} &nbsp;|&nbsp; Status: {rev['status']}"
            f"<br>Artist: {rev['artist']} &nbsp;|&nbsp; Submitted: {rev['date']}"
        )
        self.comments_list.clear()
        for c in rev["comments"]:
            self.comments_list.addItem(f"[{c['date']}] {c['author']}: {c['text']}")

        self.download_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self.add_comment_btn.setEnabled(True)

    def _on_download_clicked(self):
        if not self.current_revision:
            return
        rev = self.current_revision

        # fake_path = f"/tmp/kitsu_review/{rev['shot']}_{rev['task']}_v{rev['revision']:03d}.mov".replace(" ", "_")
        fake_path = f"/home/bazamel/Videos/testimonials/fees/kitsu-summit2026-4-25fps.mkv"

        progress = QtWidgets.QProgressDialog("Downloading revision from Kitsu...", None, 0, 100, self)
        progress.setWindowTitle("Kitsu")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        for pct in range(0, 101, 20):
            progress.setValue(pct)
            QtWidgets.QApplication.processEvents()
            QtCore.QThread.msleep(80)
        progress.close()

        try:
            rvc.addSourceVerbose([fake_path])
        except Exception as exc:
            print(f"[KitsuReview] Skipped adding source to RV session: {exc}")

        self.export_btn.setEnabled(True)

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            f"Downloaded and loaded into RV:\n\n{rev['shot']} - {rev['task']} v{rev['revision']:03d}\n"
            f"(mock file: {fake_path})\n\n"
            "Use RV's Paint tools to annotate frames directly on the viewport."
        )

    def _gather_rv_annotations(self):
        annotations = []

        try:
            paint_nodes = rvc.nodesOfType("RVPaint")
        except Exception as exc:
            print(f"[KitsuReview] Could not query paint nodes: {exc}")
            return annotations

        for node in paint_nodes:
            try:
                all_props = rvc.properties(node)
            except Exception as exc:
                print(f"[KitsuReview] Skipped paint node {node}: {exc}")
                continue

            for prop in all_props:
                match = _FRAME_ORDER_RE.search(prop)
                if not match:
                    continue
                frame = int(match.group(1))
                try:
                    order = rvc.getStringProperty(prop)
                except Exception:
                    order = []
                if order:  # non-empty -> at least one stroke/text on this frame
                    annotations.append({"node": node, "frame": frame})

        return annotations

    def _on_add_comment_clicked(self):
        text = self.comment_input.text().strip()
        if not text or not self.current_revision:
            return
        comment = {"author": MOCK_USER["full_name"], "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text}
        self.current_revision["comments"].append(comment)
        self.comments_list.addItem(f"[{comment['date']}] {comment['author']}: {comment['text']}")
        self.comment_input.clear()

    def _on_export_clicked(self):
        if not self.current_revision:
            return
        rev = self.current_revision
        n_comments = len(rev["comments"])
        annotations = self._gather_rv_annotations()
        annotated_frames = sorted({a["frame"] for a in annotations})

        if annotated_frames:
            frames_note = f"{len(annotated_frames)} annotated frame(s): {annotated_frames}"
        else:
            frames_note = "0 annotated frames"

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task']}\n"
            f"Revision: v{rev['revision']:03d}\n"
            f"Comments exported: {n_comments}\n"
            f"Annotations exported: {frames_note}\n\n"
            "(This is mock data - no request was actually sent to Kitsu.)"
        )


class KitsuReviewMode(rv.rvtypes.MinorMode):
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
    return KitsuReviewMode()