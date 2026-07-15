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
  6. Export comments + annotations (parsed from the RV session graph) back to Kitsu

This plugin is designed to run only inside OpenRV: the panel is docked
below the review viewport rather than shown as a standalone window.

Login, task listing, preview downloading, comments, and annotation export
now all use the real Kitsu Python SDK (`gazu`):
  - gazu.log_in / gazu.log_out                            -- authentication
  - gazu.task.all_tasks_for_person(person)                 -- tasks assigned to the user
  - gazu.entity.get_entity(entity_id)                      -- shot/asset info for a task
  - gazu.files.get_all_preview_files_for_task(task)        -- revisions (preview files) for a task
  - gazu.files.download_preview_file(preview_file, path)   -- actual media download
  - gazu.task.all_comments_for_task(task)                  -- comment history for a task
  - gazu.task.get_task_status(task_status_id)              -- resolve a task's current status
  - gazu.task.add_comment(task, task_status, comment=...)  -- post a new comment
  - gazu.files.update_preview_annotations(preview_file,
        additions=..., updates=..., deletions=...)         -- sync annotations for a preview

Annotation export (`_on_export_clicked` / `_gather_rv_annotations` /
`_extract_frame_paint_data`) reads real data out of the RV session graph
via RV's command API and diffs it against whatever is already stored on
the Kitsu preview file, then sends only the additions/updates/deletions
needed. The exact per-frame paint property paths (pen points, text,
etc.) can vary between RV versions/builds, so the extraction code stays
defensive -- double check the property layout against the RV build you
are targeting and extend `_extract_frame_paint_data` if you want more
detail (e.g. stroke color/width) carried over to Kitsu.

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


def _annotation_signature(kind, data):
    """Stable signature for an annotation's content, used to detect whether
    a previously-synced annotation needs to be re-sent as an update."""
    return json.dumps({"type": kind, "data": data}, sort_keys=True, default=str)


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
                # Populated lazily (see _sync_known_annotations) once the
                # revision is selected and we know what Kitsu already has.
                "known_annotations": None,
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
        self._sync_known_annotations(rev)

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

    def _index_kitsu_annotations(self, preview_file):
        """Build a {frame: {"id":..., "signature":...}} index from whatever
        annotations Kitsu already has stored on this preview file.

        Kitsu's annotation objects are free-form beyond an 'id', so this
        plugin writes its own 'frame'/'type'/'data' fields into each
        annotation it sends (see _gather_rv_annotations) and reads them
        back the same way here to figure out what's changed since the
        last export.
        """
        index = {}
        for anno in (preview_file or {}).get("annotations") or []:
            frame = anno.get("frame")
            anno_id = anno.get("id")
            if frame is None or not anno_id:
                continue
            index[frame] = {
                "id": anno_id,
                "signature": _annotation_signature(anno.get("type"), anno.get("data")),
            }
        return index

    def _sync_known_annotations(self, rev):
        """Refresh rev['known_annotations'] from the preview file's current
        state on Kitsu, re-fetching the preview file if needed."""
        preview_file = rev.get("preview_file")
        preview_id = (preview_file or {}).get("id")
        if preview_id:
            try:
                preview_file = gazu.files.get_preview_file(preview_id)
                rev["preview_file"] = preview_file
            except Exception as exc:
                print(f"[KitsuReview] Could not refresh preview file {preview_id}: {exc}")
        rev["known_annotations"] = self._index_kitsu_annotations(preview_file)

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

    def _extract_frame_paint_data(self, node, frame):
        """Pull whatever pen/text data RV exposes for one paint node/frame
        into a plain, JSON-serializable dict.

        NOTE: RV's exact paint property layout (pen stroke point arrays,
        text properties, color/width, etc.) can vary between RV
        versions/builds. This stays defensive -- it records what it can
        find under the node's `frame:<N>.*` properties rather than
        assuming a fixed schema. Extend this if you need stroke color/
        width or other detail carried over to Kitsu.
        """
        prefix = f"{node}.frame:{frame}."
        strokes = []
        texts = []

        try:
            props = [p for p in rvc.properties(node) if p.startswith(prefix)]
        except Exception as exc:
            print(f"[KitsuReview] Could not read properties for {node} frame {frame}: {exc}")
            return {"strokes": strokes, "texts": texts}

        for prop in props:
            if ".pen:" in prop and prop.endswith(".points"):
                try:
                    points = rvc.getFloatProperty(prop)
                except Exception:
                    points = []
                if points:
                    strokes.append(list(points))
            elif ".text:" in prop and prop.endswith(".text"):
                try:
                    text_value = rvc.getStringProperty(prop)
                except Exception:
                    text_value = []
                if text_value:
                    texts.append(list(text_value))

        return {"strokes": strokes, "texts": texts}

    def _gather_rv_annotations(self):
        """Collect the current per-frame paint annotations from the RV
        session graph, in a shape ready to diff against and send to
        Kitsu's `update_preview_annotations`.

        Returns a list of dicts: {"frame", "type", "data", "signature"}.
        """
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

            frames_with_content = set()
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
                    frames_with_content.add(frame)

            for frame in sorted(frames_with_content):
                data = self._extract_frame_paint_data(node, frame)
                data["node"] = node
                annotations.append({
                    "frame": frame,
                    "type": "drawing",
                    "data": data,
                    "signature": _annotation_signature("drawing", data),
                })

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
        """Sync RV paint annotations to the selected revision's preview file
        on Kitsu via gazu.files.update_preview_annotations, and report the
        current comment count (comments themselves are posted immediately,
        see _on_add_comment_clicked)."""
        if not self.current_revision:
            return
        rev = self.current_revision
        task = rev["task"]
        preview_file = rev["preview_file"]

        try:
            n_comments = len(gazu.task.all_comments_for_task(task) or [])
        except Exception as exc:
            print(f"[KitsuReview] Could not fetch comment count for export summary: {exc}")
            n_comments = self.comments_list.count()

        current_annotations = self._gather_rv_annotations()
        known = rev.get("known_annotations")
        if known is None:
            self._sync_known_annotations(rev)
            known = rev["known_annotations"]
            preview_file = rev["preview_file"]

        additions = []
        updates = []
        seen_frames = set()

        for anno in current_annotations:
            frame = anno["frame"]
            seen_frames.add(frame)
            payload = {"frame": frame, "type": anno["type"], "data": anno["data"]}
            existing = known.get(frame)
            if existing is None:
                additions.append(payload)
            elif existing["signature"] != anno["signature"]:
                updates.append({**payload, "id": existing["id"]})
            # else: unchanged, nothing to send

        deletions = [
            info["id"] for frame, info in known.items() if frame not in seen_frames
        ]

        if not (additions or updates or deletions):
            QtWidgets.QMessageBox.information(
                self, "Kitsu",
                "Export complete!\n\n"
                f"Shot: {rev['shot']}\n"
                f"Task: {rev['task_type']}\n"
                f"Revision: v{rev['revision']:03d}\n"
                f"Comments on Kitsu: {n_comments}\n"
                "Annotations: no changes to sync (RV annotations already match Kitsu)."
            )
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            updated_preview_file = gazu.files.update_preview_annotations(
                preview_file,
                additions=additions or None,
                updates=updates or None,
                deletions=deletions or None,
            )
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(
                self, "Kitsu", f"Failed to sync annotations to Kitsu: {exc}"
            )
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        # Adopt whatever Kitsu handed back as the new source of truth so the
        # next export only sends further deltas.
        rev["preview_file"] = updated_preview_file or preview_file
        rev["known_annotations"] = self._index_kitsu_annotations(rev["preview_file"])

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task_type']}\n"
            f"Revision: v{rev['revision']:03d}\n"
            f"Comments on Kitsu: {n_comments}\n"
            f"Annotations added: {len(additions)}\n"
            f"Annotations updated: {len(updates)}\n"
            f"Annotations removed: {len(deletions)}\n\n"
            "Comments and annotations are both now synced to Kitsu."
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