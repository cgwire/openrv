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

try:
    import gazu
    import gazu.exception
    import requests
    import rv.commands as rvc
    from PySide6 import QtCore, QtGui

    _INSIDE_OPENRV = True
except ImportError:
    _INSIDE_OPENRV = False

# Optional: the OS credential store (macOS Keychain, Windows Credential
# Manager, Secret Service / KWallet on Linux). Without it, "Remember me" is
# unavailable. The app remembers only the server URL and the email.
try:
    import keyring
    import keyring.errors
except ImportError:
    keyring = None


DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")

DEFAULT_SERVER = "http://localhost/api"
SETTINGS_ORG = "KitsuReview"
SETTINGS_APP = "OpenRVPlugin"
KEYRING_SERVICE = "kitsu-review-openrv"
# Store the tokens as separate credential entries. Windows Credential Manager
# limits a single secret to about 1280 characters. Two JWTs together can be
# more than that.
_TOKEN_KINDS = ("access_token", "refresh_token")

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

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


def log(message):
    print(f"[KitsuReview] {message}")


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
# Session persistence
# ----------------------------------------------------------------------


class _SessionStore:
    """
    Save the login details between OpenRV sessions.

    The server URL, the email and the "Remember me" choice are not secret. They
    go in QSettings. The Kitsu session tokens go in the OS credential store
    through `keyring`.

    The app never stores the password. A login exchanges it for tokens, and the
    app reuses those tokens. Tokens expire on the server. Log Out revokes them.
    """

    def __init__(self):
        self._settings = QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)

    # -- non-secret settings -------------------------------------------

    def load_identity(self):
        server = self._settings.value("server", "", type=str) or ""
        email = self._settings.value("email", "", type=str) or ""
        return server, email

    def save_identity(self, server, email):
        self._settings.setValue("server", server)
        self._settings.setValue("email", email)
        self._settings.sync()

    def remember_enabled(self):
        return bool(self._settings.value("remember", True, type=bool))

    def set_remember_enabled(self, enabled):
        self._settings.setValue("remember", bool(enabled))
        self._settings.sync()

    # -- secrets (OS credential store) ---------------------------------

    @staticmethod
    def secure_available():
        """
        Return True if keyring is installed and has a real backend. Return
        False for the "fail" backend that keyring uses on, for example,
        headless Linux.
        """
        if keyring is None:
            return False
        try:
            return float(getattr(keyring.get_keyring(), "priority", 0)) > 0
        except Exception:
            return False

    @staticmethod
    def _account(server, email, kind):
        return f"{email} @ {server} [{kind}]"

    @staticmethod
    def _delete(account):
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass  # nothing is stored under that name

    def load_tokens(self, server, email):
        if not self.secure_available():
            return None
        tokens = {}
        for kind in _TOKEN_KINDS:
            try:
                value = keyring.get_password(
                    KEYRING_SERVICE, self._account(server, email, kind)
                )
            except Exception as exc:
                log(
                    f"Could not read the saved session from the "
                    f"credential store: {exc}"
                )
                return None
            if value:
                tokens[kind] = value
        return tokens or None

    def save_tokens(self, server, email, tokens):
        if not self.secure_available():
            return False
        try:
            for kind in _TOKEN_KINDS:
                account = self._account(server, email, kind)
                value = (tokens or {}).get(kind)
                if value:
                    keyring.set_password(KEYRING_SERVICE, account, str(value))
                else:
                    self._delete(account)
        except Exception as exc:
            log(f"Could not save the session to the credential store: {exc}")
            return False
        return True

    def clear_tokens(self, server, email):
        if not server or not email or not self.secure_available():
            return
        for kind in _TOKEN_KINDS:
            try:
                self._delete(self._account(server, email, kind))
            except Exception as exc:
                log(f"Could not remove a saved session entry: {exc}")


# ----------------------------------------------------------------------
# RV session (sources, frames, paint nodes)
# ----------------------------------------------------------------------


class RvSession:
    """
    Read and write the open RV session: sources, frame ranges and the
    paint-node properties where RV stores annotations.
    """

    _GROUPED_KEYS = {
        "points": 2,
        "color": 4,
        "innerColor": 4,
        "borderColor": 4,
        "startPos": 2,
        "endPos": 2,
        "min": 2,
        "max": 2,
    }

    @staticmethod
    def current_frame():
        try:
            return rvc.frame()
        except Exception:
            return None

    @staticmethod
    def add_source(file_path):
        try:
            return rvc.addSourceVerbose([file_path])
        except Exception as exc:
            log(f"Skipped adding source to RV session: {exc}")
            return None

    @staticmethod
    def set_fps(fps, source_node=None):
        """
        Make RV play at `fps`. If a source is given, also pin its media to that
        rate (the `group.fps` override of the RVFileSource). Then RV counts
        frames as Kitsu does. The setting also stays when RV derives the
        playback rate again after the view changes.
        """
        if source_node:
            prop = f"{source_node}.group.fps"
            try:
                if rvc.propertyExists(prop):
                    rvc.setFloatProperty(prop, [float(fps)], True)
            except Exception as exc:
                log(f"Could not set {prop} to {fps}: {exc}")
        try:
            rvc.setFPS(float(fps))
        except Exception as exc:
            log(f"Could not set RV playback FPS to {fps}: {exc}")
            return False
        return True

    @staticmethod
    def paint_node_for(source_node):
        return f"{rvc.nodeGroup(source_node)}_paint"

    @staticmethod
    def source_start_frame(source_node, default=1):
        """
        Return the first frame of a source in the current session. A still
        image uses exactly one frame. The frameless annotations of Kitsu must
        go on that frame.
        """
        try:
            info = rvc.nodeRangeInfo(source_node) or {}
        except Exception as exc:
            log(f"Could not query frame range for {source_node}: {exc}")
            return default
        start = info.get("start") if isinstance(info, dict) else None
        try:
            return int(start)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def paint_nodes_for_source(source_group):
        """
        Return every RVPaint node that draws on one source, upstream first.

        First comes the paint node in the group of the source. Then come the
        per-input paint nodes that RV puts in each sequence, retime or switch
        group that the source feeds ("<group>_p_<sourceGroup>").

        The annotate mode of RV draws on the first paint node that it finds
        from the view downward. So in a sequence view, the strokes made in RV
        go in "defaultSequence_p_..." and not in the paint node of the source.
        Those per-input nodes are before the retime, so their frames are source
        frames too.

        RV renders the strokes of all paint nodes into one list, upstream node
        first. So an eraser in a downstream node erases strokes from the node
        of the source. Read the nodes in this order to keep this true.
        """
        try:
            all_paint = list(rvc.nodesOfType("RVPaint") or [])
        except Exception as exc:
            log(f"Could not query paint nodes: {exc}")
            return []

        own, downstream = [], []
        suffix = f"_p_{source_group}"
        for node in all_paint:
            try:
                group = rvc.nodeGroup(node)
            except Exception:
                group = None
            if group == source_group:
                own.append(node)
            elif node.endswith(suffix):
                downstream.append(node)
        return own + sorted(downstream)

    @staticmethod
    def _pipeline_order(paint_nodes):
        """
        Return the paint nodes with those in source groups first. They are
        upstream of any view-level paint node, so RV draws their strokes first
        (see `paint_nodes_for_source`).
        """

        def is_view_level(node):
            try:
                return rvc.nodeType(rvc.nodeGroup(node)) != "RVSourceGroup"
            except Exception:
                return True

        return sorted(paint_nodes, key=is_view_level)

    def current_source_paint_nodes(self):
        """
        Return (sources, paint_nodes) for the sources under the playhead.

        In a stack or layout view, several sources can be visible at once. So
        return all of them, with every paint node that draws on each (see
        `paint_nodes_for_source`).
        """
        try:
            sources = list(rvc.sourcesAtFrame(rvc.frame()) or [])
        except Exception as exc:
            log(f"Could not query sources at current frame: {exc}")
            return [], []

        paint_nodes: List[str] = []
        for source in sources:
            try:
                group = rvc.nodeGroup(source)
            except Exception as exc:
                log(f"Could not inspect group of {source}: {exc}")
                continue
            for node in self.paint_nodes_for_source(group):
                if node not in paint_nodes:
                    paint_nodes.append(node)
        return sources, paint_nodes

    @staticmethod
    def _set_prop(full_name, ptype, width, values):
        if not rvc.propertyExists(full_name):
            rvc.newProperty(full_name, ptype, width)
        if ptype == rvc.FloatType:
            rvc.setFloatProperty(full_name, values, True)
        elif ptype == rvc.IntType:
            rvc.setIntProperty(full_name, values, True)
        elif ptype == rvc.StringType:
            rvc.setStringProperty(full_name, values, True)

    @staticmethod
    def _color_float(color, default=(255, 255, 255, 255)):
        """
        Convert a color from `convertToORV` (flat [r, g, b, a], 0..255) to the
        0..1 floats that RV paint properties hold.
        """
        color = color or list(default)
        if isinstance(color[0], (list, tuple)):
            color = color[0]
        return [c / 255.0 for c in color]

    def _set_props(self, base, specs):
        """
        Write the (attribute, type, width, values) specs in order.
        """
        for attr, ptype, width, values in specs:
            self._set_prop(f"{base}.{attr}", ptype, width, values)

    @staticmethod
    def _timing_specs(frame, props):
        return [
            (
                "startFrame",
                rvc.IntType,
                1,
                [int(props.get("startFrame", frame))],
            ),
            ("duration", rvc.IntType, 1, [int(props.get("duration", 1))]),
            ("eye", rvc.IntType, 1, [int(props.get("eye", 2))]),
        ]

    @staticmethod
    def _uuid_specs(props):
        if not props.get("uuid"):
            return []
        return [("uuid", rvc.StringType, 1, [str(props["uuid"])])]

    @staticmethod
    def _soft_deleted_spec(props):
        return (
            "softDeleted",
            rvc.IntType,
            1,
            [int(props.get("softDeleted", 0))],
        )

    def _write_pen(self, base, frame, props):
        points = props["points"]

        width = props.get("width", [0.003])
        if not isinstance(width, list):
            width = [width]
        if len(width) != len(points):
            width = [width[0]] * len(points)

        self._set_props(
            base,
            [
                (
                    "color",
                    rvc.FloatType,
                    4,
                    self._color_float(props.get("color")),
                ),
                ("width", rvc.FloatType, 1, width),
                ("brush", rvc.StringType, 1, ["circle"]),
                ("points", rvc.FloatType, 2, [c for xy in points for c in xy]),
                ("debug", rvc.IntType, 1, [0]),
                ("join", rvc.IntType, 1, [3]),
                ("cap", rvc.IntType, 1, [1]),
                ("splat", rvc.IntType, 1, [0]),
                # 0 paints. 1 erases what the earlier strokes drew (Kitsu
                # erasers).
                ("mode", rvc.IntType, 1, [int(props.get("mode") or 0)]),
                *self._uuid_specs(props),
            ],
        )

    def _write_box(self, base, frame, props):
        """
        Write the properties that RV writes for its own ellipse or rect.

        RV writes them when you draw with its annotate tools. To see them,
        look at a gathered RV shape in the Dev tab.

        The two shapes share one property set. They differ only in the prefix
        "ellipse:" or "rect:" of the component name.
        """
        inner = self._color_float(
            props.get("innerColor"), default=(255, 255, 255, 0)
        )
        border = self._color_float(props.get("borderColor"))
        border_width = float(props.get("borderWidth", 0.003))
        self._set_props(
            base,
            [
                *self._timing_specs(frame, props),
                ("min", rvc.FloatType, 2, [float(v) for v in props["min"][0]]),
                ("max", rvc.FloatType, 2, [float(v) for v in props["max"][0]]),
                ("innerColor", rvc.FloatType, 4, inner),
                ("borderColor", rvc.FloatType, 4, border),
                ("borderWidth", rvc.FloatType, 1, [border_width]),
                *self._uuid_specs(props),
                self._soft_deleted_spec(props),
            ],
        )

    def _write_arrow(self, base, frame, props):
        """
        Write the same properties that RV writes for an arrow drawn with its
        own annotate tool. The tail is at `startPos` and the head is at
        `endPos`. The line width is both `thickness` and `borderWidth`. The
        color is in `borderColor` and `innerColor`.
        """
        border = self._color_float(props.get("borderColor"))
        inner = (
            self._color_float(props["innerColor"])
            if props.get("innerColor")
            else border
        )
        thickness = float(
            props.get("thickness", props.get("borderWidth", 0.003))
        )
        border_width = float(props.get("borderWidth", thickness))
        start = [float(v) for v in props["startPos"][0]]
        end = [float(v) for v in props["endPos"][0]]
        self._set_props(
            base,
            [
                *self._timing_specs(frame, props),
                ("startPos", rvc.FloatType, 2, start),
                ("endPos", rvc.FloatType, 2, end),
                ("borderColor", rvc.FloatType, 4, border),
                ("borderWidth", rvc.FloatType, 1, [border_width]),
                ("innerColor", rvc.FloatType, 4, inner),
                ("thickness", rvc.FloatType, 1, [thickness]),
                *self._uuid_specs(props),
                self._soft_deleted_spec(props),
            ],
        )

    def _write_text(self, base, frame, props):
        """
        Write the same properties that RV writes for text placed with its own
        annotate tool. `position` is the left end of the first baseline.
        `fontSize` is the text height as a fraction of the image height. `size`
        is the legacy equivalent that older RV builds read (`fontSize` / 12.5
        at scale 1).
        """
        position = props["position"]
        if isinstance(position[0], (list, tuple)):
            position = position[0]
        scale = float(props.get("scale", 1.0)) or 1.0
        font_size = float(props.get("fontSize", 0.05))
        size = float(props.get("size", font_size / scale / 12.5))

        def text_spec(attr, default):
            return (attr, rvc.StringType, 1, [str(props.get(attr, default))])

        def number_spec(attr, ptype, cast, default):
            return (attr, ptype, 1, [cast(props.get(attr, default))])

        self._set_props(
            base,
            [
                (
                    "position",
                    rvc.FloatType,
                    2,
                    [float(v) for v in position[:2]],
                ),
                (
                    "color",
                    rvc.FloatType,
                    4,
                    self._color_float(props.get("color")),
                ),
                number_spec("spacing", rvc.FloatType, float, 0.8),
                ("size", rvc.FloatType, 1, [size]),
                ("scale", rvc.FloatType, 1, [scale]),
                number_spec("rotation", rvc.FloatType, float, 0.0),
                text_spec("font", ""),
                text_spec("text", ""),
                text_spec("origin", ""),
                number_spec("debug", rvc.IntType, int, 0),
                number_spec("startFrame", rvc.IntType, int, frame),
                number_spec("duration", rvc.IntType, int, 1),
                number_spec("mode", rvc.IntType, int, 0),
                text_spec("fontFamily", ""),
                ("fontSize", rvc.FloatType, 1, [font_size]),
                text_spec("fontWeight", "normal"),
                text_spec("fontStyle", "normal"),
                text_spec("textDecoration", "none"),
                text_spec("textAlign", "left"),
                *self._uuid_specs(props),
                self._soft_deleted_spec(props),
            ],
        )

    _SHAPE_WRITERS = {
        "pen": "_write_pen",
        "ellipse": "_write_box",
        "rect": "_write_box",
        "arrow": "_write_arrow",
        "text": "_write_text",
    }

    def apply_annotations_live(self, paint_node, openrv_annotations):
        """
        openrv_annotations: flat list of shapes as returned by
        `convertToORV()`, that is [{"type", "frame", "properties"}, ...].
        """
        self._set_prop(f"{paint_node}.paint.show", rvc.IntType, 1, [1])

        next_id = 0
        frame_order: Dict[int, List[str]] = {}

        for shape in openrv_annotations:
            kind = shape.get("type")
            writer = self._SHAPE_WRITERS.get(kind)
            if writer is None:
                log(
                    f"Live-apply: skipping unsupported shape "
                    f"type {kind!r}",
                )
                continue

            frame = int(shape["frame"])
            cname = f"{kind}:{next_id}:{frame}:Kitsu"
            getattr(self, writer)(
                f"{paint_node}.{cname}", frame, shape["properties"]
            )

            frame_order.setdefault(frame, []).append(cname)
            next_id += 1

        for frame, names in frame_order.items():
            self._set_prop(
                f"{paint_node}.frame:{frame}.order", rvc.StringType, 1, names
            )

        rvc.redraw()

    @staticmethod
    def _property_value(prop):
        try:
            info = rvc.propertyInfo(prop)
        except Exception:
            return None

        ptype = (
            info.get("type")
            if isinstance(info, dict)
            else getattr(info, "type", None)
        )

        if ptype == rvc.FloatType:
            return rvc.getFloatProperty(prop)
        if ptype == rvc.IntType:
            return rvc.getIntProperty(prop)
        if ptype == rvc.StringType:
            return rvc.getStringProperty(prop)
        return None

    @classmethod
    def _shaped_value(cls, attr, value):
        """
        Convert a raw flat property value to the form that the shape dicts
        hold. Group it (points, colors, ...), or unwrap it if it is a single
        value.
        """
        if not isinstance(value, list):
            return value
        group_size = cls._GROUPED_KEYS.get(attr)
        if group_size and value and len(value) % group_size == 0:
            return [
                list(value[i : i + group_size])
                for i in range(0, len(value), group_size)
            ]
        return value[0] if len(value) == 1 else value

    def _shape_properties(self, node, item_name, all_props):
        prefix = f"{node}.{item_name}."
        return {
            p[len(prefix) :]: self._shaped_value(
                p[len(prefix) :], self._property_value(p)
            )
            for p in all_props
            if p.startswith(prefix)
        }

    @staticmethod
    def _frame_order(prop):
        try:
            order = rvc.getStringProperty(prop)
        except Exception:
            return []
        return [order] if isinstance(order, str) else order

    def gather_annotations(self, paint_nodes=None):
        """
        Read annotation shapes from RV paint nodes.

        paint_nodes: list of RVPaint node names to read, upstream first (as
        `paint_nodes_for_source` returns them). If it is None, read every paint
        node in the session.

        Shapes come out per frame in drawing order: the nodes in the given
        order, and each node in its own frame order. The order matters for
        erasers, which cut only what is drawn before them.
        """
        annotations = []

        if paint_nodes is None:
            try:
                paint_nodes = self._pipeline_order(
                    rvc.nodesOfType("RVPaint") or []
                )
            except Exception as exc:
                log(f"Could not query paint nodes: {exc}")
                return annotations

        for node in paint_nodes:
            try:
                all_props = rvc.properties(node)
            except Exception as exc:
                log(f"Skipped paint node {node}: {exc}")
                continue

            for prop in all_props:
                match = _FRAME_ORDER_RE.search(prop)
                if not match:
                    continue
                for item_name in self._frame_order(prop) or []:
                    annotations.append(
                        {
                            "frame": int(match.group(1)),
                            "node": node,
                            "name": item_name,
                            "type": item_name.split(":")[0],
                            "properties": self._shape_properties(
                                node, item_name, all_props
                            ),
                        }
                    )

        annotations.sort(key=lambda a: a["frame"])

        return annotations


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
        self.store = _SessionStore()
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
            k: str(v) for k, v in tokens.items() if k in _TOKEN_KINDS and v
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

        Return {"n_comments", "frames_note"} for the summary. Raise
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

        return {"n_comments": n_comments, "frames_note": frames_note}

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
