"""Stateless helpers around the Kitsu (gazu) API.

Nothing in this module holds state, imports Qt, or talks to OpenRV. Every
function takes the records it needs as arguments and returns plain Python
data, so the UI layer can stay a thin wrapper and this layer stays testable
without a running RV session.

Failures that the user needs to know about are raised as :class:`KitsuError`
with a message that is safe to put straight into a dialog. Failures that are
merely degraded service (a missing thumbnail, an entity we cannot resolve)
are logged and skipped.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from utils import (
    _infer_canvas_size,
    convert_kitsu_annotations,
    convert_openrv_annotations,
)

try:
    import gazu
    import requests

    KITSU_AVAILABLE = True
except ImportError:  # imported standalone, e.g. from a test runner
    gazu = None
    requests = None
    KITSU_AVAILABLE = False


DEFAULT_SOURCE_SIZE = (1920, 1080)
#: Kitsu's own fallback when a project sets no fps (Zou:
#: ``preview_files_service.get_preview_file_fps``). Annotation times are
#: stored in seconds, so guessing this wrong shifts every imported
#: annotation onto the wrong frame.
DEFAULT_FPS = 25.0
THUMBNAIL_TIMEOUT = 10


class KitsuError(RuntimeError):
    """A Kitsu failure with a message suitable for showing to the user."""


def warn(message: str) -> None:
    """Log a degraded-service problem that the user does not need to act on."""
    print(f"[KitsuReview] {message}", file=sys.stderr)


def _call(description: str, func: Callable, *args, **kwargs):
    """Run a gazu call, re-raising anything it throws as a KitsuError."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # gazu raises a wide variety of exception types
        raise KitsuError(f"{description}: {exc}") from exc


# ---------------------------------------------------------------------------
# Pure formatting / record shaping
# ---------------------------------------------------------------------------


def preview_revision(preview_file: Dict[str, Any]) -> int:
    return preview_file.get("revision", 0) or 0


def format_date(value: Any) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(value)


def person_display_name(person: Optional[Dict[str, Any]]) -> str:
    if not person or not isinstance(person, dict):
        return "Unknown"
    first = person.get("first_name", "") or ""
    last = person.get("last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or person.get("email", "Unknown")


def revision_label(revision: Dict[str, Any]) -> str:
    return f"v{revision['revision']:03d}"


def format_comment_line(comment: Dict[str, Any]) -> str:
    author = person_display_name(comment.get("person"))
    date = format_date(comment.get("created_at"))
    return f"[{date}] {author}: {comment.get('text') or ''}"


def build_revision(
    task: Dict[str, Any],
    entity: Dict[str, Any],
    preview_file: Dict[str, Any],
    artist: str,
) -> Dict[str, Any]:
    """Flatten a task + entity + preview file into one row-shaped record."""
    entity = entity or {}
    return {
        "task": task,
        "entity": entity,
        "preview_file": preview_file,
        "shot": entity.get("name", task.get("entity_name", "Unknown")),
        "task_type": task.get("task_type_name", "Unknown"),
        "revision": preview_revision(preview_file),
        "status": task.get(
            "task_status_name", task.get("task_status_short_name", "Unknown")
        ),
        "artist": artist,
        "date": format_date(
            preview_file.get("created_at") or task.get("updated_at")
        ),
    }


def source_size(
    preview_file: Dict[str, Any],
    default: Tuple[int, int] = DEFAULT_SOURCE_SIZE,
) -> Tuple[int, int]:
    default_w, default_h = default
    try:
        width = int(preview_file.get("width") or default_w)
        height = int(preview_file.get("height") or default_h)
    except (TypeError, ValueError):
        return default_w, default_h
    if width <= 0 or height <= 0:
        return default_w, default_h
    return width, height


def _coerce_fps(value: Any) -> Optional[float]:
    """Kitsu stores fps as a string, sometimes with a comma decimal mark."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", ".").strip()
    try:
        fps = float(value)
    except (TypeError, ValueError):
        return None
    return fps if fps > 0 else None


def source_fps(preview_file: Dict[str, Any], default: float = DEFAULT_FPS) -> float:
    """Best-effort fps from the preview file alone.

    Note that Kitsu's ``preview_file`` has no ``fps`` column: the real value
    lives on the project (overridden per entity). Prefer :func:`resolve_fps`,
    which consults those; this stays as the last-resort reader.
    """
    data = preview_file.get("data") or {}
    for candidate in (preview_file.get("fps"), data.get("fps")):
        fps = _coerce_fps(candidate)
        if fps is not None:
            return fps
    return default


def fetch_project_fps(task: Optional[Dict[str, Any]]) -> Optional[float]:
    """The project's fps, or None. Degraded service, never fatal."""
    project_id = (task or {}).get("project_id")
    if not project_id:
        return None
    try:
        project = gazu.project.get_project(project_id) or {}
    except Exception as exc:
        warn(f"Could not fetch project {project_id} for its fps: {exc}")
        return None
    return _coerce_fps(project.get("fps"))


def resolve_fps(
    preview_file: Optional[Dict[str, Any]] = None,
    task: Optional[Dict[str, Any]] = None,
    entity: Optional[Dict[str, Any]] = None,
    default: float = DEFAULT_FPS,
) -> float:
    """The fps Kitsu itself would use, in Kitsu's own order of precedence.

    Zou takes the project's fps and lets the entity's ``data.fps`` override
    it. Annotation times are stored in seconds, so this is what decides
    which frame an imported annotation lands on.
    """
    entity_data = (entity or {}).get("data") or {}
    for candidate in (entity_data.get("fps"), fetch_project_fps(task)):
        fps = _coerce_fps(candidate)
        if fps is not None:
            return fps
    return source_fps(preview_file or {}, default)


def download_path(preview_file: Dict[str, Any], download_dir: str) -> str:
    original_name = preview_file.get("original_name") or preview_file.get(
        "id", "revision"
    )
    extension = preview_file.get("extension") or "mov"
    file_name = str(original_name)
    if not file_name.lower().endswith(f".{extension.lower()}"):
        file_name = f"{file_name}.{extension}"
    return os.path.join(download_dir, file_name)


def download_percent(size: Any, total_size: Any) -> int:
    if not total_size:
        return 0
    return int(min(100, max(0, (size / total_size) * 100)))


# ---------------------------------------------------------------------------
# Annotation round-tripping (delegates the real maths to utils)
# ---------------------------------------------------------------------------


def canvas_size(preview_file: Dict[str, Any]) -> Tuple[float, float]:
    width, height = source_size(preview_file)
    return _infer_canvas_size(
        preview_file.get("annotations") or [], width, height
    )


def openrv_annotations_for(
    preview_file: Dict[str, Any],
    frame_offset: int = 0,
    fps: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Kitsu annotations on a preview file -> RVPaint-shaped shapes."""
    width, height = source_size(preview_file)
    canvas_width, canvas_height = canvas_size(preview_file)
    return convert_kitsu_annotations(
        preview_file.get("annotations") or [],
        width=width,
        height=height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        frame_offset=frame_offset,
        fps=fps if fps is not None else source_fps(preview_file),
    )


def kitsu_records_for(
    annotations: Sequence[Dict[str, Any]],
    preview_file: Dict[str, Any],
    author: Optional[str],
    fps: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], float, float]:
    """RVPaint shapes -> Kitsu annotation records, plus the canvas used."""
    width, height = source_size(preview_file)
    canvas_width, canvas_height = canvas_size(preview_file)
    records = convert_openrv_annotations(
        annotations,
        width=width,
        height=height,
        fps=fps if fps is not None else source_fps(preview_file),
        author=author,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    return records, canvas_width, canvas_height


def annotation_author(
    user: Optional[Dict[str, Any]], preview_file: Dict[str, Any]
) -> Optional[str]:
    return (user or {}).get("id") or preview_file.get("person_id")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def log_in(server: str, email: str, password: str) -> Dict[str, Any]:
    if not server or not email or not password:
        raise KitsuError("Please enter a server URL, email, and password.")
    _call(f"Could not set host to {server}", gazu.set_host, server)
    user = _call("Login failed", gazu.log_in, email, password)
    if isinstance(user, dict) and "user" in user:
        return user["user"]
    return user


def log_out() -> None:
    """Best-effort logout: a failure here should never block the UI."""
    try:
        gazu.log_out()
    except Exception as exc:
        warn(f"gazu.log_out() failed (continuing anyway): {exc}")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_entity(entity_id: Optional[str]) -> Dict[str, Any]:
    if not entity_id:
        return {}
    try:
        return gazu.entity.get_entity(entity_id) or {}
    except Exception as exc:
        warn(f"Could not fetch entity {entity_id}: {exc}")
        return {}


def fetch_preview_files(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        return gazu.files.get_all_preview_files_for_task(task) or []
    except Exception as exc:
        warn(f"Could not fetch previews for task {task.get('id')}: {exc}")
        return []


def fetch_revisions(person: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Latest reviewable revision for each of a person's tasks."""
    tasks = _call("Failed to fetch tasks", gazu.task.all_tasks_for_person, person)
    artist = person_display_name(person)

    revisions = []
    for task in tasks or []:
        previews = fetch_preview_files(task)
        if not previews:
            continue
        revisions.append(
            build_revision(
                task,
                fetch_entity(task.get("entity_id")),
                max(previews, key=preview_revision),
                artist,
            )
        )
    return revisions


def fetch_comments(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    comments = _call(
        f"Could not fetch comments for task {task.get('id')}",
        gazu.task.all_comments_for_task,
        task,
    )
    return list(reversed(comments or []))


def post_comment(
    task: Dict[str, Any], text: str, person: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    status_id = task.get("task_status_id")
    if not status_id:
        raise KitsuError(
            "Could not determine the task's current status; comment not sent."
        )
    task_status = _call(
        "Failed to look up the task status", gazu.task.get_task_status, status_id
    )
    return _call(
        "Failed to post comment",
        gazu.task.add_comment,
        task,
        task_status,
        comment=text,
        person=person,
    )


def thumbnail_url(preview_file_id: str) -> str:
    return f"{gazu.client.get_host()}/pictures/originals/preview-files/{preview_file_id}.png"


def fetch_thumbnail(preview_file_id: Optional[str]) -> Optional[bytes]:
    """Raw PNG bytes for a preview thumbnail, or None. Never raises."""
    if not preview_file_id:
        return None
    try:
        response = requests.get(
            thumbnail_url(preview_file_id),
            headers=gazu.client.make_auth_header(),
            timeout=THUMBNAIL_TIMEOUT,
        )
        if response.ok:
            return response.content
        warn(
            f"Thumbnail for preview {preview_file_id} returned "
            f"HTTP {response.status_code}"
        )
    except Exception as exc:
        warn(f"Could not fetch thumbnail for preview {preview_file_id}: {exc}")
    return None


def download_preview(
    preview_file: Dict[str, Any],
    download_dir: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Download a preview file, returning the local path it landed at."""
    os.makedirs(download_dir, exist_ok=True)
    file_path = download_path(preview_file, download_dir)

    if progress_callback is not None:
        try:
            gazu.files.download_preview_file(
                preview_file, file_path, progress_callback=progress_callback
            )
            return file_path
        except TypeError:
            # gazu too old to report progress; fall through to the plain call.
            pass
        except Exception as exc:
            raise KitsuError(f"Download failed: {exc}") from exc

    _call("Download failed", gazu.files.download_preview_file, preview_file, file_path)
    return file_path


def push_annotations(
    preview_file: Dict[str, Any],
    additions: Sequence[Dict[str, Any]],
    updates: Optional[Sequence[Dict[str, Any]]] = None,
    deletions: Optional[Sequence[Dict[str, Any]]] = None,
):
    return _call(
        "Failed to send annotations to Kitsu",
        gazu.files.update_preview_annotations,
        preview_file,
        additions=list(additions),
        updates=list(updates or []),
        deletions=list(deletions or []),
    )