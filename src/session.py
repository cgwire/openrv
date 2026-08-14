"""The review model: all state and all workflow, with no Qt in sight.

:class:`ReviewSession` owns who is logged in, which revisions were fetched and
which one is selected, and it sequences the multi-step workflows (download +
apply annotations, gather + export annotations). The panel is expected to do
nothing but read these results and put them on screen.

Every method raises :class:`kitsu_service.KitsuError` on a failure worth
telling the user about.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import zou as kitsu
import rvpaint

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")


@dataclass
class DownloadResult:
    file_path: str
    annotations_applied: int = 0
    #: Set when the media loaded but its annotations could not be painted.
    paint_warning: Optional[str] = None


@dataclass
class ExportResult:
    records_sent: int = 0
    annotated_frames: List[int] = field(default_factory=list)
    canvas: Tuple[float, float] = (0.0, 0.0)
    #: None when Kitsu would not tell us; the caller can fall back to its view.
    comment_count: Optional[int] = None


class ReviewSession:
    def __init__(self, download_dir: str = DOWNLOAD_DIR):
        self.download_dir = download_dir
        self.user: Optional[Dict[str, Any]] = None
        self.revisions: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None

    # -- state ------------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return self.user is not None

    @property
    def display_name(self) -> str:
        return kitsu.person_display_name(self.user) if self.user else "Unknown user"

    def _require_current(self) -> Dict[str, Any]:
        if not self.current:
            raise kitsu.KitsuError("No revision is selected.")
        return self.current

    def fps_for(self, revision: Dict[str, Any]) -> float:
        """Kitsu's fps for a revision, cached on the revision record.

        Kitsu stores annotation times in seconds, so the fps is what decides
        which frame each annotation lands on. It lives on the project (the
        entity can override it), not on the preview file, so resolving it
        costs one lookup -- hence the cache.
        """
        if revision.get("fps") is None:
            revision["fps"] = kitsu.resolve_fps(
                revision.get("preview_file"),
                revision.get("task"),
                revision.get("entity"),
            )
        return revision["fps"]

    # -- auth -------------------------------------------------------------

    def log_in(self, server: str, email: str, password: str) -> Dict[str, Any]:
        self.user = kitsu.log_in(server, email, password)
        return self.user

    def log_out(self) -> None:
        kitsu.log_out()
        self.user = None
        self.revisions = []
        self.current = None

    # -- revisions --------------------------------------------------------

    def load_revisions(self) -> List[Dict[str, Any]]:
        self.revisions = kitsu.fetch_revisions(self.user) if self.logged_in else []
        self.current = None
        return self.revisions

    def select(self, index: int) -> Optional[Dict[str, Any]]:
        if 0 <= index < len(self.revisions):
            self.current = self.revisions[index]
        else:
            self.current = None
        return self.current

    def clear_selection(self) -> None:
        self.current = None

    # -- comments ---------------------------------------------------------

    def comments(self) -> List[Dict[str, Any]]:
        current = self._require_current()
        return kitsu.fetch_comments(current["task"])

    def comment_count(self) -> Optional[int]:
        try:
            return len(self.comments())
        except kitsu.KitsuError as exc:
            kitsu.warn(f"Could not fetch comment count for export summary: {exc}")
            return None

    def add_comment(self, text: str) -> None:
        current = self._require_current()
        kitsu.post_comment(current["task"], text, self.user)

    # -- workflows --------------------------------------------------------

    def download_current(
        self, progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> DownloadResult:
        """Download the selected revision, load it into RV, and repaint its
        existing Kitsu annotations onto the new source's paint node."""
        current = self._require_current()
        preview_file = current["preview_file"]

        annotations = kitsu.openrv_annotations_for(
            preview_file, fps=self.fps_for(current)
        )
        file_path = kitsu.download_preview(
            preview_file, self.download_dir, progress_callback
        )
        current["local_path"] = file_path

        result = DownloadResult(file_path=file_path)

        source_node = rvpaint.add_source(file_path)
        if source_node and annotations:
            paint_node = rvpaint.paint_node_for_source(source_node)
            try:
                result.annotations_applied = rvpaint.apply_annotations(
                    paint_node, annotations
                )
            except Exception as exc:
                kitsu.warn(
                    f"Could not apply Kitsu annotations to {paint_node}: {exc}"
                )
                result.paint_warning = str(exc)

        return result

    def export_current(self) -> ExportResult:
        """Send everything painted in the session back up to Kitsu."""
        current = self._require_current()
        preview_file = current["preview_file"]

        comment_count = self.comment_count()

        annotations = rvpaint.gather_annotations()
        records, canvas_width, canvas_height = kitsu.kitsu_records_for(
            annotations,
            preview_file,
            author=kitsu.annotation_author(self.user, preview_file),
            fps=self.fps_for(current),
        )

        kitsu.push_annotations(preview_file, records)

        return ExportResult(
            records_sent=len(records),
            annotated_frames=sorted({a["frame"] for a in annotations}),
            canvas=(canvas_width, canvas_height),
            comment_count=comment_count,
        )