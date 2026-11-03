"""
Core logic for the Kitsu Review OpenRV plugin.

This module holds all logic that is not UI. It uses gazu to talk to Kitsu. It
saves the login session. It holds the task status rules. It reads and writes
annotations in the paint nodes of the RV session.

This module creates no widgets. `kitsu.py` owns the panel. It calls
`KitsuReviewModel` from its handlers.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from annotation_converter import (
    DEFAULT_FPS,
    convertToKitsu,
    convertToORV,
    split_annotation_changes,
)
from kitsu_review_log import log
from kitsu_rv_session import RvSession
from kitsu_session_store import TOKEN_KINDS, SessionStore

try:
    import gazu
    import gazu.exception
    import requests
    import rv.commands as rvc
    from PySide6 import QtGui

    _INSIDE_OPENRV = True
except ImportError:
    _INSIDE_OPENRV = False

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")

DEFAULT_SERVER = "http://localhost/api"
_RESOLUTION_RE = re.compile(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$")

_STILL_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "bmp",
    "tif",
    "tiff",
    "exr",
    "dpx",
    "tga",
    "webp",
}

# These Kitsu roles can comment only with statuses that have the flag
# "is_artist_allowed" or "is_client_allowed". Supervisors, managers and admins
# can use any status. The server also enforces this. The filter here keeps the
# dropdown free of statuses that the user cannot set.
_ARTIST_ROLES = {"user", "vendor"}
_CLIENT_ROLES = {"client"}

STATUS_FALLBACK_COLOR = "#9e9e9e"

# Safety limit for the number of pages read from the paged Kitsu route for the
# playlists of a project.
_PLAYLIST_MAX_PAGES = 50


class DevView:
    """
    Keys for the views of the Dev tab. The view maps them to labels.
    """

    RV = "rv"  # raw OpenRV paint properties
    CONVERTED = "converted"  # data in Kitsu format (export preview)
    SERVER = "server"  # data that Kitsu stores for the selected revision


class SessionRejected(Exception):
    """
    A saved Kitsu session that the server does not accept any more.
    """


class DimensionsUnknownError(ValueError):
    """
    The pixel size of a preview could not be found from any source.
    """


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def _preview_revision(preview_file):
    """
    Return a best-effort revision number for a gazu preview file dict.
    """
    return preview_file.get("revision", 0) or 0


def _format_date(value):
    """
    Kitsu timestamps are ISO 8601 strings (or None). Convert them for display.
    """
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(value)


def person_display_name(person):
    """
    Return a best-effort display name for a gazu person dict. Comments contain
    such a dict.
    """
    if not person or not isinstance(person, dict):
        return "Unknown"
    first = person.get("first_name", "") or ""
    last = person.get("last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or person.get("email", "Unknown")


def status_short_label(status):
    """
    Return a short tag for a task status, for example "WIP" or "RETAKE".
    """
    if not status:
        return "?"
    short = status.get("short_name") or ""
    return short.upper() if short else (status.get("name") or "?")


def status_full_name(status):
    if not status:
        return "Unknown"
    return status.get("name") or status.get("short_name") or "Unknown"


def normalize_server(url):
    return (url or "").strip().rstrip("/")


def _thumbnail_url(preview_file_id):
    """
    Return a best-effort Kitsu thumbnail URL for a preview file.

    The URL uses the configured host, as the rest of the gazu API does.
    """
    host = gazu.client.get_host()
    return f"{host}/pictures/originals/preview-files/{preview_file_id}.png"


def _parse_fps(value):
    """
    Return the frame rate of a production as a float. Return None if it is not
    set or is not a usable number.

    Kitsu stores it as a string, for example "25" or "23.976".
    """
    if value is None:
        return None
    try:
        fps = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return fps if math.isfinite(fps) and fps > 0 else None


def format_fps(fps):
    """
    Return a short display form: 25 -> "25", 23.976 -> "23.976".
    """
    return f"{fps:g}" if fps else "?"


def _is_still(preview_file):
    """
    Return True if this preview is a single picture and not a movie.

    Kitsu records "extension" without a leading dot.
    """
    return (
        str(preview_file.get("extension") or "").lower().lstrip(".")
        in _STILL_EXTENSIONS
    )


def _normalize_full_task(task):
    """
    `gazu.task.get_task()` returns the "full" task. Its type, status, entity
    and project are nested dicts. Set the flat keys that the rest of the plugin
    reads (`task_type_name`, `task_status_id`, ...) from those dicts.
    """
    task = dict(task or {})
    flat_from_nested = (
        ("task_type_id", "task_type", "id"),
        ("task_type_name", "task_type", "name"),
        ("task_status_id", "task_status", "id"),
        ("task_status_name", "task_status", "name"),
        ("task_status_short_name", "task_status", "short_name"),
        ("entity_id", "entity", "id"),
        ("entity_name", "entity", "name"),
        ("project_id", "project", "id"),
    )
    for key, nested, field in flat_from_nested:
        if not task.get(key) and isinstance(task.get(nested), dict):
            task[key] = task[nested].get(field)
    return task


def _entity_label(task, entity):
    """
    Return the shot label as Kitsu shows it in playlists, for example "SQ010 /
    SH0040".
    """
    name = (entity or {}).get("name") or task.get("entity_name") or "Unknown"
    sequence = task.get("sequence")
    sequence_name = (
        sequence.get("name") if isinstance(sequence, dict) else None
    )
    return f"{sequence_name} / {name}" if sequence_name else name


def _json_default(value):
    """
    Fallback for `json.dumps` for values from RV or gazu that JSON cannot
    serialize natively (sets, custom objects, ...).
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return str(value)


def _auth_error_types():
    """
    Return the gazu exceptions that mean "the server rejected these
    credentials". They do not mean "the server could not be reached".

    Look them up by name, because the set is a little different in each gazu
    version. `KeyError` covers a refresh response that has no new access token.
    """
    names = (
        "NotAuthenticatedException",
        "NotAllowedException",
        "ParameterException",
    )
    found = tuple(
        exc_type
        for exc_type in (getattr(gazu.exception, n, None) for n in names)
        if isinstance(exc_type, type)
    )
    return found + (KeyError,)


def _current_gazu_tokens():
    """
    Return the current session tokens of the gazu client as a plain dict.

    gazu has no public getter for the whole token dict. Newer versions have
    only `get_access_token` and `get_refresh_token`. So read the `tokens`
    attribute of the default client, which every version has. If it is not
    available, use the getter for each token.
    """
    client = getattr(gazu.client, "default_client", None)
    tokens = getattr(client, "tokens", None)
    if isinstance(tokens, dict):
        return dict(tokens)

    result = {}
    for kind, getter_name in (
        ("access_token", "get_access_token"),
        ("refresh_token", "get_refresh_token"),
    ):
        getter = getattr(gazu.client, getter_name, None)
        if callable(getter):
            value = getter()
            if value:
                result[kind] = value
    return result


def _dimensions_from_file(file_path):
    """
    Read the pixel dimensions from a downloaded file. Return None if this is
    not possible. This happens with an unsupported or unreadable format. It
    also happens outside OpenRV, where the Qt image loaders are not available.
    """
    if not _INSIDE_OPENRV or not file_path or not os.path.exists(file_path):
        return None
    image = QtGui.QImage(file_path)
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return None
    return image.width(), image.height()


def _preview_dimensions(preview_file, entity=None, file_path=None):
    width, height = preview_file.get("width"), preview_file.get("height")
    if width and height:
        return int(width), int(height)

    resolution = ((entity or {}).get("data") or {}).get("resolution") or ""
    match = _RESOLUTION_RE.match(str(resolution))
    if match:
        return int(match.group(1)), int(match.group(2))

    from_file = _dimensions_from_file(file_path)
    if from_file:
        return from_file

    for record in preview_file.get("annotations") or []:
        for obj in record.get("drawing", {}).get("objects", []):
            canvas_w = obj.get("canvasWidth")
            canvas_h = obj.get("canvasHeight")
            if canvas_w and canvas_h:
                return int(round(float(canvas_w))), int(round(float(canvas_h)))

    raise DimensionsUnknownError(
        f"Could not determine the pixel dimensions of preview file "
        f"{preview_file.get('id')!r} (extension "
        f"{preview_file.get('extension')!r})."
    )


# ----------------------------------------------------------------------
# The model
# ----------------------------------------------------------------------


class KitsuReviewModel:
    """
    State and operations behind the Kitsu Review panel.

    Methods raise an error for failures that the user must know about. The view
    reports them. For failures that need only a log line, the method prints the
    line here and does not raise.
    """

    def __init__(self):
        self.store = SessionStore()
        self.rv = RvSession()

        self.logged_in = False
        self.current_user = None
        self.current_revision = None
        # "My Checks" tab: the latest revision of each task of the user.
        self.check_revisions: List[Dict[str, Any]] = []
        # "Playlists" tab: one summary for each playlist of the open
        # productions of the user. Also the revisions of each opened playlist,
        # by playlist id.
        self.playlists: List[Dict[str, Any]] = []
        self._playlist_revisions: Dict[str, List[Dict[str, Any]]] = {}
        self._thumbnail_cache: Dict[str, Optional[bytes]] = {}

        # Catalogs of task statuses and task types (id -> dict). Also the
        # project (production) records. Each project record has the subset of
        # statuses and the frame rate of the project. A failed fetch is cached
        # as None.
        self._task_statuses: Dict[str, Dict[str, Any]] = {}
        self._task_types: Dict[str, Dict[str, Any]] = {}
        self._project_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._person_cache: Dict[str, str] = {}

        self._session_server = None
        self._session_email = None
        self._saved_tokens = None

        self.remember = (
            self.store.secure_available() and self.store.remember_enabled()
        )

    # ------------------------------------------------------------------
    # Login / session persistence
    # ------------------------------------------------------------------

    @property
    def secure_storage_available(self):
        return self.store.secure_available()

    def saved_identity(self):
        return self.store.load_identity()

    def display_name(self):
        """
        Return a best-effort display name from the gazu user dict.
        """
        return (
            person_display_name(self.current_user)
            if self.current_user
            else "Unknown user"
        )

    def authenticate(self, server, email, password):
        """
        Exchange a password for a Kitsu session. Return the user.
        """
        gazu.set_host(server)
        return gazu.log_in(email, password)

    def complete_login(self, user, server, email):
        """
        Do the shared last steps of a password login and of a restored session.
        """
        if isinstance(user, dict) and "user" in user:
            user = user["user"]

        # Keep at most one saved session. When a different user or server logs
        # in, delete the tokens of the previous identity.
        prev_server, prev_email = self.store.load_identity()
        if (prev_server, prev_email) != (server, email):
            self.store.clear_tokens(prev_server, prev_email)

        self.current_user = user
        self.logged_in = True
        self._session_server, self._session_email = server, email
        self._saved_tokens = None

        self._enable_gazu_auto_refresh()
        self.store.save_identity(server, email)
        self.persist_session()

    def log_out(self):
        # Remove the saved session first, so a failed server call cannot leave
        # it. Then `log_out()` revokes the token on the server.
        if self._session_server and self._session_email:
            self.store.clear_tokens(self._session_server, self._session_email)
        try:
            gazu.log_out()
        except Exception as exc:
            log(f"gazu.log_out() failed (continuing anyway): {exc}")
        self.logged_in = False
        self.current_user = None
        self._session_server = self._session_email = None
        self._saved_tokens = None
        self._clear_data()

    def _clear_data(self):
        self.check_revisions = []
        self.playlists = []
        self._playlist_revisions.clear()
        self.current_revision = None
        self._thumbnail_cache.clear()
        self._task_statuses.clear()
        self._task_types.clear()
        self._project_cache.clear()
        self._person_cache.clear()

    @staticmethod
    def _enable_gazu_auto_refresh():
        """
        Let gazu renew an expired access token by itself during a session, on
        the gazu versions that support it.
        """
        client = getattr(gazu.client, "default_client", None)
        if client is not None and hasattr(client, "automatic_refresh_token"):
            client.automatic_refresh_token = True

    @staticmethod
    def _reset_gazu_tokens():
        try:
            gazu.client.set_tokens({})
        except Exception:
            pass

    def persist_session(self):
        """
        Save the current gazu tokens in the credential store. If "Remember me"
        is off, remove them. It is cheap to call many times: it writes only
        when the tokens changed, for example after a refresh.
        """
        if not self.logged_in or not self._session_server:
            return
        if not (self.remember and self.store.secure_available()):
            self.store.clear_tokens(self._session_server, self._session_email)
            self._saved_tokens = None
            return
        try:
            tokens = _current_gazu_tokens()
        except Exception as exc:
            log(f"Could not read the current gazu session tokens: {exc}")
            return
        # Keep only the tokens that we store. This makes the "unchanged?" check
        # stable.
        tokens = {
            k: str(v) for k, v in tokens.items() if k in TOKEN_KINDS and v
        }
        if not tokens.get("access_token"):
            return  # no usable token to save yet

        # Some gazu versions replace the token dict on refresh with a dict that
        # has only the new access token. Keep the refresh token that we already
        # have. If not, `save_tokens()` deletes it from the store.
        if not tokens.get("refresh_token"):
            previous = (
                self._saved_tokens
                or self.store.load_tokens(
                    self._session_server, self._session_email
                )
                or {}
            )
            if previous.get("refresh_token"):
                tokens["refresh_token"] = previous["refresh_token"]

        if tokens == self._saved_tokens:
            return
        if self.store.save_tokens(
            self._session_server, self._session_email, tokens
        ):
            self._saved_tokens = tokens

    def set_remember(self, enabled):
        self.remember = bool(enabled)
        self.store.set_remember_enabled(enabled)
        if self.logged_in:
            self._saved_tokens = None
            self.persist_session()

    def saved_tokens(self, server, email):
        """
        Return the tokens that "Remember me" saved for this identity, or None.
        """
        return self.store.load_tokens(server, email)

    def _resume_session(self, server, tokens):
        """
        Reconnect with saved tokens and return the current user.

        Try the access token first, then the refresh token. Raise
        `SessionRejected` if the server refuses both. Network and server errors
        propagate unchanged, so the caller can keep the session for later.
        """
        auth_errors = _auth_error_types()
        gazu.set_host(server)
        gazu.client.set_tokens(dict(tokens))

        if tokens.get("access_token"):
            try:
                return gazu.client.get_current_user()
            except auth_errors:
                pass  # expired: use the refresh token next

        if not tokens.get("refresh_token"):
            raise SessionRejected()
        try:
            gazu.refresh_token()
            return gazu.client.get_current_user()
        except auth_errors as exc:
            raise SessionRejected() from exc

    def restore_session(self, server, email, tokens):
        """
        Resume a saved session and return the user.

        If the server rejects the session (expired or revoked), delete it so
        the app does not retry it at each launch. Then raise `SessionRejected`.
        If the server cannot be reached, keep the session for next time and
        raise the original error.
        """
        try:
            return self._resume_session(server, tokens)
        except SessionRejected:
            self.store.clear_tokens(server, email)
            self._reset_gazu_tokens()
            raise
        except Exception as exc:
            log(f"Could not restore the saved Kitsu session: {exc}")
            self._reset_gazu_tokens()
            raise

    # ------------------------------------------------------------------
    # Revisions
    # ------------------------------------------------------------------

    def _refresh_catalogs(self):
        """
        Do the shared first steps of a list refresh. gazu can renew the access
        token during the refresh, so update the saved session. Statuses, task
        types and project setups can change on the server, so load them again.
        """
        self.persist_session()
        self._load_task_statuses()
        self._load_task_types()
        self._project_cache.clear()

    def _make_revision(self, task, entity, preview_file, artist, **extra):
        """
        Return the revision dict that both tabs list and the detail panel uses.
        """
        fps, fps_from_project = self.project_fps(task.get("project_id"))
        rev = {
            "task": task,
            "entity": entity,
            "preview_file": preview_file,
            "shot": entity.get("name", task.get("entity_name", "Unknown")),
            "task_type": task.get("task_type_name", "Unknown"),
            "revision": _preview_revision(preview_file),
            "status": task.get(
                "task_status_name",
                task.get("task_status_short_name", "Unknown"),
            ),
            "artist": artist,
            "date": _format_date(
                preview_file.get("created_at") or task.get("updated_at")
            ),
            "width": None,
            "height": None,
            "base_frame": 1,
            "fps": fps,
            "fps_from_project": fps_from_project,
            "loaded": False,
            "playlist": None,
        }
        rev.update(extra)
        return rev

    def load_my_checks(self):
        """
        Get the tasks of the current user from Kitsu. Collect the latest
        preview file of each task as a reviewable revision. Raise an error if
        the task list cannot be fetched.
        """
        tasks = gazu.task.all_tasks_for_person(self.current_user)
        self._refresh_catalogs()

        revisions = []
        for task in tasks:
            entity_id = task.get("entity_id")
            entity = {}
            if entity_id:
                try:
                    entity = gazu.entity.get_entity(entity_id) or {}
                except Exception as exc:
                    log(f"Could not fetch entity {entity_id}: {exc}")

            try:
                previews = (
                    gazu.files.get_all_preview_files_for_task(task) or []
                )
            except Exception as exc:
                log(
                    f"Could not fetch previews for task {task.get('id')}: "
                    f"{exc}"
                )
                previews = []

            if not previews:
                continue

            latest_preview = max(previews, key=_preview_revision)
            revisions.append(
                self._make_revision(
                    task, entity, latest_preview, self.display_name()
                )
            )

        self.check_revisions = revisions
        if (
            self.current_revision
            and self.current_revision.get("playlist") is None
        ):
            self.current_revision = None
        return revisions

    def select_revision(self, rev):
        """
        Make `rev` (a dict from the list of either tab) the current revision.
        None clears it.
        """
        self.current_revision = rev
        return rev

    def all_loaded_revisions(self):
        """
        Yield every revision dict that is listed now, on either tab.
        """
        yield from self.check_revisions
        for revisions in self._playlist_revisions.values():
            yield from revisions

    def is_pending_review(self, task):
        """
        Return True if the task is in a status that asks for feedback (the
        Kitsu flag "is_feedback_request", for example WFA). Then the task waits
        for a reviewer.
        """
        return (task or {}).get("task_status_id") in self.feedback_status_ids()

    def feedback_status_ids(self):
        return {
            sid
            for sid, status in self._task_statuses.items()
            if status.get("is_feedback_request")
        }

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    def load_playlists(self):
        """
        Return every playlist of the open productions of the user, most
        recently updated first, as summaries:

            {"id", "name", "project", "task_type", "for_client", "updated",
             "entries", "entry_count", "pending_count", "pending_exact"}

        pending_count is the number of entries that wait for review. When the
        playlists are listed, it is an estimate from the tasks in
        feedback-request statuses. It is exact after the revisions of the
        playlist are loaded. None means that the count could not be found.

        Raise an error if the project list cannot be fetched.
        """
        projects = gazu.user.all_open_projects() or []
        self._refresh_catalogs()
        self._playlist_revisions.clear()
        if (
            self.current_revision
            and self.current_revision.get("playlist") is not None
        ):
            self.current_revision = None

        feedback_ids = self.feedback_status_ids()
        summaries = []
        for project in projects:
            playlists = self._project_playlists(project)
            if not playlists:
                continue
            awaiting = (
                self._entities_awaiting_review(project["id"], feedback_ids)
                if feedback_ids
                else None
            )
            for light in playlists:
                summary = self._playlist_summary(light, project, awaiting)
                if summary is not None:
                    summaries.append(summary)

        summaries.sort(
            key=lambda s: str(s.get("_updated_raw") or ""), reverse=True
        )
        self.playlists = summaries
        return summaries

    def _project_playlists(self, project):
        """
        Return all playlists of one project. The Kitsu route is paged, so read
        pages until one page brings nothing new. Log a project whose playlists
        cannot be read, and skip it.
        """
        seen, result = set(), []
        for page in range(1, _PLAYLIST_MAX_PAGES + 1):
            try:
                batch = (
                    gazu.playlist.all_playlists_for_project(project, page=page)
                    or []
                )
            except TypeError:
                # Older gazu versions have no page argument. They return all
                # playlists.
                if page > 1:
                    break
                batch = gazu.playlist.all_playlists_for_project(project) or []
            except Exception as exc:
                log(
                    f"Could not fetch playlists for project "
                    f"{project.get('name')!r}: {exc}"
                )
                break
            new = [p for p in batch if p.get("id") and p["id"] not in seen]
            if not new:
                break
            seen.update(p["id"] for p in new)
            result.extend(new)
        return result

    def _entities_awaiting_review(self, project_id, feedback_ids):
        """
        Return {entity_id: {task_type_id, ...}} for the tasks of the project
        that are in a feedback-request status. Return None if they could not be
        fetched.
        """
        index: Dict[str, set] = {}
        for status_id in feedback_ids:
            try:
                tasks = (
                    gazu.client.fetch_all(
                        "tasks",
                        {
                            "project_id": project_id,
                            "task_status_id": status_id,
                        },
                    )
                    or []
                )
            except Exception as exc:
                log(
                    f"Could not fetch tasks awaiting review for "
                    f"project {project_id}: {exc}"
                )
                return None
            for task in tasks:
                index.setdefault(task.get("entity_id"), set()).add(
                    task.get("task_type_id")
                )
        return index

    def _playlist_summary(self, light, project, awaiting):
        """
        Fetch the entries of one playlist and summarize it. Return None if it
        cannot be read.
        """
        entries = light.get("shots")
        playlist = light
        if not isinstance(entries, list):
            try:
                playlist = gazu.playlist.get_playlist(light["id"]) or light
            except Exception as exc:
                log(f"Could not fetch playlist {light.get('name')!r}: {exc}")
                return None
            entries = playlist.get("shots") or []
        entries = [e for e in entries if isinstance(e, dict)]
        reviewable = [e for e in entries if e.get("preview_file_id")]

        task_type_id = playlist.get("task_type_id")
        pending = None
        if awaiting is not None:
            pending = sum(
                1
                for e in reviewable
                if (awaiting.get(e.get("entity_id")) or set())
                and (
                    not task_type_id
                    or task_type_id in awaiting[e["entity_id"]]
                )
            )

        updated_raw = playlist.get("updated_at") or playlist.get("created_at")
        return {
            "id": playlist["id"],
            "name": playlist.get("name") or "(unnamed playlist)",
            "project": project.get("name") or "?",
            "project_id": project.get("id"),
            "task_type": (self._task_types.get(task_type_id) or {}).get("name")
            or "",
            "for_client": bool(playlist.get("for_client")),
            "updated": _format_date(updated_raw),
            "_updated_raw": updated_raw,
            "entries": entries,
            "entry_count": len(reviewable),
            "pending_count": pending,
            "pending_exact": False,
        }

    def load_playlist_revisions(self, summary):
        """
        Return the revisions that a playlist puts up for review, in playlist
        order. They are cached until the next playlist refresh. Return
        (revisions, skipped). `skipped` is the number of entries that have no
        revision, or whose revision could not be read.
        """
        cached = self._playlist_revisions.get(summary["id"])
        if cached is not None:
            return cached, summary.get("_skipped", 0)

        revisions, skipped = [], 0
        for entry in summary["entries"]:
            rev = self._playlist_entry_revision(entry, summary)
            if rev is None:
                skipped += 1
            else:
                revisions.append(rev)

        self._playlist_revisions[summary["id"]] = revisions
        summary["_skipped"] = skipped
        self._recount_pending(summary)
        return revisions, skipped

    def _playlist_entry_revision(self, entry, summary):
        preview_id = entry.get("preview_file_id")
        if not preview_id:
            return None
        try:
            preview_file = gazu.files.get_preview_file(preview_id) or {}
            task = _normalize_full_task(
                gazu.task.get_task(preview_file["task_id"])
            )
        except Exception as exc:
            log(
                f"Skipped playlist entry {entry.get('entity_id')} "
                f"(preview {preview_id}): {exc}"
            )
            return None

        entity = (
            task.get("entity") if isinstance(task.get("entity"), dict) else {}
        )
        return self._make_revision(
            task,
            entity,
            preview_file,
            self._person_name(preview_file.get("person_id")),
            shot=_entity_label(task, entity),
            playlist=summary["id"],
        )

    def _person_name(self, person_id):
        """
        Return the display name of the person who uploaded a revision (cached).
        """
        if not person_id:
            return "Unknown"
        if person_id not in self._person_cache:
            try:
                name = person_display_name(gazu.person.get_person(person_id))
            except Exception as exc:
                log(f"Could not fetch person {person_id}: {exc}")
                name = "Unknown"
            self._person_cache[person_id] = name
        return self._person_cache[person_id]

    def _recount_pending(self, summary):
        """
        Set the exact pending count for a playlist whose revisions are loaded.
        """
        revisions = self._playlist_revisions.get(summary["id"])
        if revisions is None:
            return
        if self.feedback_status_ids():
            summary["pending_count"] = sum(
                1 for rev in revisions if self.is_pending_review(rev["task"])
            )
            summary["pending_exact"] = True

    def playlist_for_revision(self, rev):
        playlist_id = (rev or {}).get("playlist")
        return next(
            (s for s in self.playlists if s["id"] == playlist_id), None
        )

    def thumbnail_bytes(self, preview_file_id):
        """
        Fetch the encoded thumbnail image for a preview file, and cache it.

        The thumbnail route of Kitsu needs the same authentication as the rest
        of the API. So this method uses `requests` directly, with the auth
        header of gazu. It returns None if the thumbnail cannot be fetched, and
        caches that. A preview with no rendered thumbnail yet is an expected
        case and is not fatal.
        """
        if not preview_file_id:
            return None
        if preview_file_id in self._thumbnail_cache:
            return self._thumbnail_cache[preview_file_id]

        data = None
        try:
            url = _thumbnail_url(preview_file_id)
            headers = gazu.client.make_auth_header()
            response = requests.get(url, headers=headers, timeout=10)
            if response.ok:
                data = response.content
        except Exception as exc:
            log(
                f"Could not fetch thumbnail for preview "
                f"{preview_file_id}: {exc}"
            )

        self._thumbnail_cache[preview_file_id] = data
        return data

    # ------------------------------------------------------------------
    # Task statuses
    # ------------------------------------------------------------------

    def _load_task_statuses(self):
        """
        Fetch the task status catalog of the studio. If the fetch fails, keep
        the previous catalog (if any). Comments then use the status information
        that the server embeds in them.
        """
        try:
            statuses = gazu.task.all_task_statuses() or []
        except Exception as exc:
            log(f"Could not fetch task statuses: {exc}")
            return
        self._task_statuses = {s["id"]: s for s in statuses if s.get("id")}

    def _load_task_types(self):
        """
        Fetch the task type catalog (playlists name their type by id). If the
        fetch fails, keep the previous catalog.
        """
        try:
            task_types = gazu.task.all_task_types() or []
        except Exception as exc:
            log(f"Could not fetch task types: {exc}")
            return
        self._task_types = {t["id"]: t for t in task_types if t.get("id")}

    def status_for_id(self, status_id):
        return self._task_statuses.get(status_id) if status_id else None

    def comment_status(self, comment):
        """
        Return the status that a comment was posted with. Prefer the catalog
        entry, because it has the color. If there is none, use the status that
        Kitsu embeds in comments.
        """
        embedded = comment.get("task_status")
        embedded = embedded if isinstance(embedded, dict) else None
        status_id = comment.get("task_status_id") or (embedded or {}).get("id")
        return self.status_for_id(status_id) or embedded

    def _project(self, project_id):
        """
        Return the Kitsu project (production) record, fetched once for each
        refresh. Return None if it could not be fetched.
        """
        if not project_id:
            return None
        if project_id not in self._project_cache:
            try:
                project = gazu.project.get_project(project_id) or None
            except Exception as exc:
                log(f"Could not fetch project {project_id}: {exc}")
                project = None
            self._project_cache[project_id] = project
        return self._project_cache[project_id]

    def _project_status_ids(self, project_id):
        """
        Return the ids of the statuses that a project is set up to use. Return
        None if the project does not restrict them (or could not be fetched).
        """
        raw = (self._project(project_id) or {}).get("task_statuses") or []
        return {s.get("id") if isinstance(s, dict) else s for s in raw} or None

    def project_fps(self, project_id):
        """
        Return (fps, from_project): the frame rate of the production as set in
        Kitsu. If it is missing or not usable, return (DEFAULT_FPS, False).
        """
        project = self._project(project_id) or {}
        fps = _parse_fps(project.get("fps"))
        if fps is None:
            log(
                f"Project {project.get('name') or project_id!r} has no "
                f"usable FPS ({project.get('fps')!r}); using "
                f"{format_fps(DEFAULT_FPS)}."
            )
            return DEFAULT_FPS, False
        return fps, True

    def selectable_statuses(self, task):
        """
        Return the statuses that the current user can put on a comment for
        `task`. The current status of the task is always included.
        """
        role = (self.current_user or {}).get("role") or ""
        project_ids = self._project_status_ids(task.get("project_id"))
        current_id = task.get("task_status_id")

        result = []
        for status in self._task_statuses.values():
            sid = status.get("id")
            if sid == current_id:
                result.append(status)
                continue
            if status.get("archived") or status.get("for_concept"):
                continue
            if project_ids and sid not in project_ids:
                continue
            if role in _ARTIST_ROLES and not status.get(
                "is_artist_allowed", True
            ):
                continue
            if role in _CLIENT_ROLES and not status.get(
                "is_client_allowed", True
            ):
                continue
            result.append(status)

        def sort_key(status):
            priority = status.get("priority")
            priority = priority if isinstance(priority, (int, float)) else 0
            return (priority, (status.get("name") or "").lower())

        result.sort(key=sort_key)
        return result

    def _apply_task_status(self, task_id, status_id):
        """
        Copy a status change from a new comment to the local data, on every
        listed revision of that task. The same task can be in My Checks and in
        several playlists. Then the revisions match the server without a
        reload. Recount the pending counts of the loaded playlists.
        """
        status = self.status_for_id(status_id)
        touched_playlists = set()
        for rev in self.all_loaded_revisions():
            task = rev["task"]
            if task.get("id") != task_id:
                continue
            task["task_status_id"] = status_id
            if status:
                task["task_status_name"] = status.get("name")
                task["task_status_short_name"] = status.get("short_name")
                rev["status"] = status_full_name(status)
            if rev.get("playlist"):
                touched_playlists.add(rev["playlist"])
        for summary in self.playlists:
            if summary["id"] in touched_playlists:
                self._recount_pending(summary)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def load_comments(self, task):
        """
        Return the comment history of a task, oldest first, as display records:
        {"date", "author", "text", "status"}. Raise an error if Kitsu cannot be
        read.
        """
        comments = gazu.task.all_comments_for_task(task) or []
        return [
            {
                "date": _format_date(comment.get("created_at")),
                "author": person_display_name(comment.get("person")),
                "text": comment.get("text") or "",
                "status": self.comment_status(comment),
            }
            for comment in reversed(comments)
        ]

    def add_comment(self, rev, text, status_id):
        """
        Post a comment with the given status on the task of the revision. Kitsu
        sets the status of the task to the status of the comment. Return True
        if the status of the task changed (the revision is updated locally).
        """
        task = rev["task"]
        task_status = self.status_for_id(
            status_id
        ) or gazu.task.get_task_status(status_id)
        comment = gazu.task.add_comment(
            task,
            task_status,
            comment=text,
            person=self.current_user,
        )
        new_status_id = (comment or {}).get("task_status_id") or status_id
        if new_status_id != task.get("task_status_id"):
            self._apply_task_status(task.get("id"), new_status_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Download / load into RV
    # ------------------------------------------------------------------

    def download_preview(
        self, rev, on_progress: Optional[Callable[[int], None]] = None
    ):
        """
        Download the preview file of the revision and return the local path.
        `on_progress` receives a percentage from 0 to 100. Raise an error on
        failure.
        """
        preview_file = rev["preview_file"]

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        original_name = preview_file.get("original_name") or preview_file.get(
            "id", "revision"
        )
        extension = preview_file.get("extension") or "mov"
        file_name = str(original_name)
        if not file_name.lower().endswith(f".{extension.lower()}"):
            file_name = f"{file_name}.{extension}"
        file_path = os.path.join(DOWNLOAD_DIR, file_name)

        def _progress_callback(size, total_size):
            pct = (
                int(min(100, max(0, (size / total_size) * 100)))
                if total_size
                else 0
            )
            if on_progress:
                on_progress(pct)

        try:
            gazu.files.download_preview_file(
                preview_file, file_path, progress_callback=_progress_callback
            )
        except TypeError:
            # Older gazu versions do not support `progress_callback`.
            gazu.files.download_preview_file(preview_file, file_path)
        return file_path

    def load_into_rv(self, rev, file_path):
        """
        Add a downloaded preview to the RV session. Apply again the annotations
        that Kitsu already has for it. Return True if those annotations were
        skipped because the size is unknown.
        """
        preview_file = rev["preview_file"]
        fps = rev.get("fps") or DEFAULT_FPS
        source_node = self.rv.add_source(file_path)
        self.rv.set_fps(fps, source_node)
        # Export reads only the paint nodes of this source (see
        # `RvSession.paint_nodes_for_source`).
        rev["source_group"] = None
        if source_node:
            try:
                rev["source_group"] = rvc.nodeGroup(source_node)
            except Exception as exc:
                log(f"Could not find the group of {source_node}: {exc}")

        is_still = _is_still(preview_file)
        base_frame = (
            self.rv.source_start_frame(source_node)
            if (source_node and is_still)
            else 1
        )
        rev["base_frame"] = base_frame

        skipped = False
        try:
            width, height = _preview_dimensions(
                preview_file, rev.get("entity"), file_path
            )
        except ValueError as exc:
            rev["width"] = rev["height"] = None
            openrv_annotations = []
            skipped = True
            log(f"{exc} Existing annotations will not be loaded.")
        else:
            rev["width"], rev["height"] = width, height
            openrv_annotations = convertToORV(
                preview_file.get("annotations") or [],
                width=width,
                height=height,
                default_frame=base_frame,
                fps=fps,
                frame_base=base_frame,
            )
            if is_still:
                for shape in openrv_annotations:
                    shape["frame"] = base_frame

        if source_node and openrv_annotations:
            self.rv.apply_annotations_live(
                self.rv.paint_node_for(source_node), openrv_annotations
            )

        rev["loaded"] = True
        return skipped

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def resolve_dimensions(self, rev):
        """
        Return the pixel size for a revision, cached on the revision dict.
        Raise `DimensionsUnknownError` if it cannot be found.
        """
        width, height = rev.get("width"), rev.get("height")
        if not (width and height):
            width, height = _preview_dimensions(
                rev["preview_file"], rev.get("entity")
            )
            rev["width"], rev["height"] = width, height
        return width, height

    def kitsu_records_for(self, rev, annotations):
        """
        Convert the gathered RV annotations to the Kitsu records that Export
        sends for `rev`. Raise `DimensionsUnknownError` if the pixel dimensions
        of the revision are unknown.
        """
        preview_file = rev["preview_file"]
        width, height = self.resolve_dimensions(rev)

        annotated_frames = sorted({a["frame"] for a in annotations})
        base_frame = rev.get("base_frame") or 1
        if _is_still(preview_file) and annotated_frames:
            base_frame = annotated_frames[0]

        return convertToKitsu(
            annotations,
            width=width,
            height=height,
            fps=rev.get("fps") or DEFAULT_FPS,
            author=preview_file.get("person_id"),
            frame_base=base_frame,
            reference_records=preview_file.get("annotations"),
        )

    def export_paint_nodes(self, rev):
        """
        Return the paint nodes that Export reads for `rev`: the nodes that draw
        on the source where the revision was loaded, upstream first. Return
        None if that source is not known or has no paint node. Then Export
        reads every paint node in the session, in pipeline order.
        """
        source_group = rev.get("source_group")
        if not source_group:
            return None
        return self.rv.paint_nodes_for_source(source_group) or None

    def export_annotations(self, rev, fallback_comment_count=0):
        """
        Send the annotations of the paint nodes that `export_paint_nodes`
        returns to the preview file of the revision on Kitsu. New objects go as
        additions. Existing objects that changed (for example erased in RV) go
        as updates, because Kitsu ignores an addition whose id it already has.

        Return {"n_comments", "frames_note", "n_added", "n_updated"} for the
        summary. Raise
        `DimensionsUnknownError` if the size is unknown. Raise the error of the
        server if the push fails.
        """
        try:
            n_comments = len(
                gazu.task.all_comments_for_task(rev["task"]) or []
            )
        except Exception as exc:
            log(f"Could not fetch comment count for export summary: {exc}")
            n_comments = fallback_comment_count

        annotations = self.rv.gather_annotations(self.export_paint_nodes(rev))
        annotated_frames = sorted({a["frame"] for a in annotations})
        if annotated_frames:
            frames_note = (
                f"{len(annotated_frames)} annotated frame(s): "
                f"{annotated_frames}"
            )
        else:
            frames_note = "0 annotated frames"

        records = self.kitsu_records_for(rev, annotations)
        preview_file = rev["preview_file"]
        additions, updates = split_annotation_changes(
            records, preview_file.get("annotations")
        )
        if additions or updates:
            result = gazu.files.update_preview_annotations(
                preview_file["id"],
                additions=additions,
                updates=updates,
                deletions=[],
            )
            # Compare the next export with the data that Kitsu stores now, not
            # with the data from the load of the revision list.
            if isinstance(result, dict) and isinstance(
                result.get("annotations"), list
            ):
                preview_file["annotations"] = result["annotations"]

        def count_objects(groups):
            return sum(len(g["drawing"]["objects"]) for g in groups)

        return {
            "n_comments": n_comments,
            "frames_note": frames_note,
            "n_added": count_objects(additions),
            "n_updated": count_objects(updates),
        }

    # ------------------------------------------------------------------
    # Dev view
    # ------------------------------------------------------------------

    def _revision_summary(self):
        rev = self.current_revision
        if not rev:
            return None
        playlist = self.playlist_for_revision(rev)
        return {
            "source": (
                f"playlist {playlist['name']!r}" if playlist else "My Checks"
            ),
            "shot": rev["shot"],
            "task_type": rev["task_type"],
            "revision": rev["revision"],
            "preview_file_id": rev["preview_file"].get("id"),
            "extension": rev["preview_file"].get("extension"),
            "width": rev.get("width"),
            "height": rev.get("height"),
            "base_frame": rev.get("base_frame"),
            "fps": rev.get("fps"),
            "fps_from_project": rev.get("fps_from_project"),
        }

    def build_dev_view(self, view, current_source_only=True):
        """
        Return (json_text, status_text) for one of the `DevView` views.
        """
        try:
            payload, status = self._dev_payload(view, current_source_only)
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}
            status = "Error building view"
        return json.dumps(payload, indent=2, default=_json_default), status

    def _dev_payload(self, view, current_source_only):
        if view == DevView.SERVER:
            return self._dev_server_payload()

        frame = self.rv.current_frame()

        if current_source_only:
            sources, paint_nodes = self.rv.current_source_paint_nodes()
            scope = {
                "scope": "current source",
                "sources": sources,
                "paint_nodes": paint_nodes,
            }
        else:
            # The nodes that Export reads for the selected revision.
            rev = self.current_revision
            paint_nodes = self.export_paint_nodes(rev) if rev else None
            scope = (
                {
                    "scope": "export (selected revision's source)",
                    "paint_nodes": paint_nodes,
                }
                if paint_nodes
                else {"scope": "all paint nodes"}
            )

        annotations = self.rv.gather_annotations(paint_nodes)
        frames = sorted({a["frame"] for a in annotations})
        context = {"current_frame": frame, **scope, "annotated_frames": frames}

        if view == DevView.RV:
            payload = {
                **context,
                "shape_count": len(annotations),
                "annotations": annotations,
            }
            status = f"{len(annotations)} shape(s) on {len(frames)} frame(s)"
            return payload, status

        # DevView.CONVERTED
        rev = self.current_revision
        if not rev:
            return (
                {
                    **context,
                    "error": "Select a revision on the Review tab -- the "
                    "conversion needs its pixel dimensions and frame base.",
                },
                "No revision selected",
            )
        try:
            records = self.kitsu_records_for(rev, annotations)
        except ValueError as exc:
            return (
                {
                    **context,
                    "revision": self._revision_summary(),
                    "error": str(exc),
                },
                "Could not convert (unknown dimensions)",
            )
        payload = {
            **context,
            "revision": self._revision_summary(),
            "record_count": len(records or []),
            "records": records,
        }
        status = (
            f"{len(records or [])} Kitsu record(s) from {len(annotations)} "
            f"RV shape(s)"
        )
        return payload, status

    def _dev_server_payload(self):
        rev = self.current_revision
        if not self.logged_in:
            return {"error": "Not logged in to Kitsu."}, "Not logged in"
        if not rev:
            return {
                "error": "Select a revision on the Review tab."
            }, "No revision selected"

        preview_file = rev["preview_file"]
        source = "server"
        try:
            fresh = gazu.files.get_preview_file(preview_file["id"]) or {}
            annotations = fresh.get("annotations")
        except Exception as exc:
            log(
                f"Could not re-fetch preview file {preview_file.get('id')}: "
                f"{exc}"
            )
            annotations = preview_file.get("annotations")
            source = (
                f"cached at last Refresh Revisions (re-fetch failed: {exc})"
            )

        annotations = annotations or []
        payload = {
            "revision": self._revision_summary(),
            "source": source,
            "record_count": len(annotations),
            "annotations": annotations,
        }
        return payload, f"{len(annotations)} record(s) stored on Kitsu"
