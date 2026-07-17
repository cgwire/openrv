#!/usr/bin/env python3
"""
OpenRV plugin: "Kitsu Review"
-----------------------------------
Proof-of-concept plugin for OpenRV that lets a user:
  1. Connect to Kitsu (real gazu login)
  2. Browse their real assigned tasks and pick a revision (preview file) to review
  3. Download the revision from Kitsu (real gazu download) and load it into the RV session
  4. Annotate directly on the RV frame using RV's own Paint tools
  5. Add review comments (real gazu comments)
  6. "Export" comments + annotations (parsed from the RV session graph)
     back to Kitsu (simulated -- see note below)

This plugin is designed to run only inside OpenRV: the panel is docked
below the review viewport rather than shown as a standalone window.

Login, task listing, preview downloading, and comments now use the real
Kitsu Python SDK (`gazu`):
  - gazu.log_in / gazu.log_out                          -- authentication
  - gazu.task.all_tasks_for_person(person)               -- tasks assigned to the user
  - gazu.entity.get_entity(entity_id)                    -- shot/asset info for a task
  - gazu.files.get_all_preview_files_for_task(task)      -- revisions (preview files) for a task
  - gazu.files.download_preview_file(preview_file, path) -- actual media download
  - gazu.task.all_comments_for_task(task)                -- comment history for a task
  - gazu.task.get_task_status(task_status_id)            -- resolve a task's current status
  - gazu.task.add_comment(task, task_status, comment=...) -- post a new comment

Annotation export (the "Annotations exported" part of `_on_export_clicked`)
is still simulated -- the RV-side node parsing in `_gather_rv_annotations`
uses the real RV command API where possible, but the exact per-frame paint
property paths can vary between RV versions/builds -- double check those
against the RV build you are targeting before shipping. Only the comment
count in the export summary is now backed by real data (comments are
posted to Kitsu immediately when added, via `_on_add_comment_clicked`,
rather than being batched up for export).

Make sure `gazu` is installed in RV's Python environment:
    pip install gazu
"""

import os
import re
import json
from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

import rv
import rv.rvtypes
import rv.commands as rvc
import rv.qtutils

import gazu

from map_annotations import convert_openrv_annotations

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

# Where downloaded preview files get written to on disk before being
# loaded into the RV session.
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")


def _preview_revision(preview_file):
    """Best-effort revision number for a gazu preview file dict."""
    return preview_file.get("revision", 0) or 0


def _format_date(value):
    """Kitsu timestamps are ISO 8601 strings (or None) -- normalize for display."""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(value)


def _person_display_name(person):
    """Best-effort display name for a gazu person dict (comments embed one)."""
    if not person or not isinstance(person, dict):
        return "Unknown"
    first = person.get("first_name", "") or ""
    last = person.get("last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or person.get("email", "Unknown")


class KitsuReviewPanel(QtWidgets.QWidget):
    """Main widget for the Kitsu Review plugin.

    Intended to be embedded as a dock widget below RV's review
    viewport (see KitsuReviewMode), not shown as its own top-level
    window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.logged_in = False
        self.current_user = None
        self.current_revision = None
        self.revisions = []

        self._build_ui()
        self._refresh_login_state()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)

        top_bar = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("Not connected to Kitsu")
        self.status_label.setStyleSheet("font-weight: bold;")
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()

        self.server_field = QtWidgets.QLineEdit("http://localhost/api")
        self.server_field.setFixedWidth(260)
        self.user_field = QtWidgets.QLineEdit("admin@example.com")
        self.user_field.setPlaceholderText("email")
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

    def _display_name(self):
        """Best-effort display name from the gazu user dict."""
        return _person_display_name(self.current_user) if self.current_user else "Unknown user"

    def _refresh_login_state(self):
        connected = self.logged_in
        self.status_label.setText(
            f"Connected to Kitsu as {self._display_name()}" if connected
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
            self.revisions = []
            self.table.setRowCount(0)
            self._clear_detail_panel()

    def _on_login_clicked(self):
        if self.logged_in:
            # --- Logout ---
            try:
                gazu.log_out()
            except Exception as exc:
                print(f"[KitsuReview] gazu.log_out() failed (continuing anyway): {exc}")
            self.logged_in = False
            self.current_user = None
            self._refresh_login_state()
            QtWidgets.QMessageBox.information(self, "Kitsu", "Logged out of Kitsu.")
            return

        server = self.server_field.text().strip()
        email = self.user_field.text().strip()
        password = self.pass_field.text()

        if not server or not email or not password:
            QtWidgets.QMessageBox.warning(
                self, "Kitsu", "Please enter a server URL, email, and password."
            )
            return

        # Real Kitsu login via gazu.
        self.login_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            gazu.set_host(server)
            user = gazu.log_in(email, password)
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

        # gazu.log_in() typically returns {"user": {...}, "ldap": bool} -- but
        # be defensive in case the SDK version in use returns the user dict
        # directly.
        if isinstance(user, dict) and "user" in user:
            self.current_user = user["user"]
        else:
            self.current_user = user

        self.logged_in = True
        self._refresh_login_state()
        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            f"Logged in to Kitsu successfully!\n\nServer: {server}\nUser: {self._display_name()}"
        )
        self._on_refresh_clicked()

    def _on_refresh_clicked(self):
        """Pull the current user's real tasks from Kitsu and list any
        revisions (preview files) available to review for each one."""
        if not self.logged_in or not self.current_user:
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            tasks = gazu.task.all_tasks_for_person(self.current_user)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Failed to fetch tasks: {exc}")
            return

        revisions = []
        for task in tasks:
            entity_id = task.get("entity_id")
            entity = {}
            if entity_id:
                try:
                    entity = gazu.entity.get_entity(entity_id) or {}
                except Exception as exc:
                    print(f"[KitsuReview] Could not fetch entity {entity_id}: {exc}")

            try:
                previews = gazu.files.get_all_preview_files_for_task(task) or []
            except Exception as exc:
                print(f"[KitsuReview] Could not fetch previews for task {task.get('id')}: {exc}")
                previews = []

            if not previews:
                # Nothing has been published for this task yet -- skip it,
                # there's no revision to review.
                continue

            latest_preview = max(previews, key=_preview_revision)

            revisions.append({
                "task": task,
                "entity": entity,
                "preview_file": latest_preview,
                "shot": entity.get("name", task.get("entity_name", "Unknown")),
                "task_type": task.get("task_type_name", "Unknown"),
                "revision": _preview_revision(latest_preview),
                "status": task.get(
                    "task_status_name", task.get("task_status_short_name", "Unknown")
                ),
                "artist": self._display_name(),
                "date": _format_date(latest_preview.get("created_at") or task.get("updated_at")),
            })

        QtWidgets.QApplication.restoreOverrideCursor()

        self.revisions = revisions
        self.table.setRowCount(0)
        for row, rev in enumerate(self.revisions):
            self.table.insertRow(row)
            values = [
                rev["shot"], rev["task_type"], f"v{rev['revision']:03d}",
                rev["status"], rev["date"],
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self._clear_detail_panel()

        if not self.revisions:
            QtWidgets.QMessageBox.information(
                self, "Kitsu", "No revisions with preview files found for your tasks."
            )

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._clear_detail_panel()
            return
        index = rows[0].row()
        if index >= len(self.revisions):
            self._clear_detail_panel()
            return
        self.current_revision = self.revisions[index]
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
            f"<b>{rev['shot']}</b> &nbsp;|&nbsp; {rev['task_type']} &nbsp;|&nbsp; "
            f"Revision v{rev['revision']:03d} &nbsp;|&nbsp; Status: {rev['status']}"
            f"<br>Artist: {rev['artist']} &nbsp;|&nbsp; Submitted: {rev['date']}"
        )

        self._reload_comments()

        self.download_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self.add_comment_btn.setEnabled(True)

    def _reload_comments(self):
        """Fetch the real comment history for the selected revision's task
        from Kitsu (`gazu.task.all_comments_for_task`) and populate the list."""
        self.comments_list.clear()
        if not self.current_revision:
            return
        task = self.current_revision["task"]

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            comments = gazu.task.all_comments_for_task(task) or []
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            print(f"[KitsuReview] Could not fetch comments for task {task.get('id')}: {exc}")
            self.comments_list.addItem("(Failed to load comments from Kitsu)")
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        # Kitsu typically returns comments newest-first; show oldest-first
        # so the conversation reads top to bottom.
        for comment in reversed(comments):
            author = _person_display_name(comment.get("person"))
            date = _format_date(comment.get("created_at"))
            text = comment.get("text") or ""
            self.comments_list.addItem(f"[{date}] {author}: {text}")

    def _on_download_clicked(self):
        """Download the selected revision's preview file from Kitsu and
        load it into the current RV session."""
        if not self.current_revision:
            return
        rev = self.current_revision
        preview_file = rev["preview_file"]

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        original_name = preview_file.get("original_name") or preview_file.get("id", "revision")
        extension = preview_file.get("extension") or "mov"
        file_name = str(original_name)
        if not file_name.lower().endswith(f".{extension.lower()}"):
            file_name = f"{file_name}.{extension}"
        file_path = os.path.join(DOWNLOAD_DIR, file_name)

        progress = QtWidgets.QProgressDialog("Downloading revision from Kitsu...", None, 0, 100, self)
        progress.setWindowTitle("Kitsu")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()

        def _progress_callback(size, total_size):
            # gazu reports raw byte counts for the transfer; guard against
            # an unknown/zero total.
            pct = int(min(100, max(0, (size / total_size) * 100))) if total_size else 0
            progress.setValue(pct)
            QtWidgets.QApplication.processEvents()

        try:
            gazu.files.download_preview_file(
                preview_file, file_path, progress_callback=_progress_callback
            )
        except TypeError:
            # Some gazu versions don't accept progress_callback -- fall
            # back to a plain download.
            try:
                gazu.files.download_preview_file(preview_file, file_path)
            except Exception as exc:
                progress.close()
                QtWidgets.QMessageBox.critical(self, "Kitsu", f"Download failed: {exc}")
                return
        except Exception as exc:
            progress.close()
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Download failed: {exc}")
            return

        progress.setValue(100)
        progress.close()

        try:
            rvc.addSourceVerbose([file_path])
        except Exception as exc:
            print(f"[KitsuReview] Skipped adding source to RV session: {exc}")

        self.export_btn.setEnabled(True)

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            f"Downloaded and loaded into RV:\n\n{rev['shot']} - {rev['task_type']} v{rev['revision']:03d}\n"
            f"(saved to: {file_path})\n\n"
            "Use RV's Paint tools to annotate frames directly on the viewport."
        )

    def _get_rv_property_value(self, prop):
        try:
            info = rvc.propertyInfo(prop)
        except Exception:
            return None

        ptype = info.get("type") if isinstance(info, dict) else getattr(info, "type", None)

        if ptype == rvc.FloatType:
            return rvc.getFloatProperty(prop)
        elif ptype == rvc.IntType:
            return rvc.getIntProperty(prop)
        elif ptype == rvc.StringType:
            return rvc.getStringProperty(prop)
        else:
            return None

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

                if isinstance(order, str):
                    order = [order]
                if not order:
                    continue  # empty -> no strokes/text on this frame

                for item_name in order:
                    kind = item_name.split(":")[0] if ":" in item_name else item_name
                    item_prefix = f"{node}.{item_name}."

                    properties = {}

                    _PAIRWISE_KEYS = {"points"}

                    for p in all_props:
                        if p.startswith(item_prefix):
                            attr = p[len(item_prefix):]
                            value = self._get_rv_property_value(p)
                            if attr in _PAIRWISE_KEYS and isinstance(value, list) and len(value) % 2 == 0:
                                value = list(zip(value[0::2], value[1::2]))
                            properties[attr] = value

                    print(properties)

                    annotations.append({
                        "frame": frame,
                        "node": node,
                        "name": item_name,
                        "type": kind,
                        "properties": properties,
                    })

        annotations.sort(key=lambda a: a["frame"])
        return annotations

    def _on_add_comment_clicked(self):
        """Post a real comment to Kitsu for the selected revision's task,
        leaving the task's current status unchanged."""
        text = self.comment_input.text().strip()
        if not text or not self.current_revision:
            return

        task = self.current_revision["task"]
        status_id = task.get("task_status_id")
        if not status_id:
            QtWidgets.QMessageBox.critical(
                self, "Kitsu", "Could not determine the task's current status; comment not sent."
            )
            return

        self.add_comment_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            # Resolve the full task-status object so we can pass it back in
            # unchanged -- add_comment() requires a status even when the
            # comment shouldn't change it.
            task_status = gazu.task.get_task_status(status_id)
            gazu.task.add_comment(
                task,
                task_status,
                comment=text,
                person=self.current_user,
            )
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.add_comment_btn.setEnabled(True)
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Failed to post comment: {exc}")
            return

        QtWidgets.QApplication.restoreOverrideCursor()
        self.add_comment_btn.setEnabled(True)
        self.comment_input.clear()

        # Reload from Kitsu so the list reflects exactly what's stored there.
        self._reload_comments()

    def _on_export_clicked(self):
        # NOTE: comments are now posted to Kitsu immediately (see
        # _on_add_comment_clicked), so this just reports what's already
        # there. Annotation export is still simulated -- swap for a real
        # gazu call (e.g. attaching frame data via a preview/attachment
        # endpoint) when ready.
        if not self.current_revision:
            return
        rev = self.current_revision
        task = rev["task"]

        try:
            n_comments = len(gazu.task.all_comments_for_task(task) or [])
        except Exception as exc:
            print(f"[KitsuReview] Could not fetch comment count for export summary: {exc}")
            n_comments = self.comments_list.count()

        annotations = self._gather_rv_annotations()
        annotated_frames = sorted({a["frame"] for a in annotations})

        if annotated_frames:
            frames_note = f"{len(annotated_frames)} annotated frame(s): {annotated_frames}"
        else:
            frames_note = "0 annotated frames"

        # print(annotations)
        print(annotations)

        records = convert_openrv_annotations(
            annotations,
            width=1920,
            height=1080,
            fps=24.0,
            author="5e0ecd69-1559-41a3-b4da-dc1c9d1e0b5c",
        )

        # print(json.dumps(records, indent=2))

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task_type']}\n"
            f"Revision: v{rev['revision']:03d}\n"
            f"Comments on Kitsu: {n_comments}\n"
            f"Annotations exported: {frames_note}\n\n"
            "(Comments are real and already on Kitsu. Annotation export is still "
            "mock data -- no annotation request was actually sent to Kitsu.)"
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
