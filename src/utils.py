#!/usr/bin/env python3
"""Conversion logic between OpenRV's RVPaint annotation shapes and the
Kitsu/Fabric.js annotation JSON format used by preview files.

This module is intentionally free of any PySide6 / rv / gazu dependency so
it can be imported and unit tested outside of an OpenRV session.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

MS_PER_STROKE_POINT = 8

DEFAULT_PEN_WIDTH = 0.003
DEFAULT_BORDER_WIDTH = 0.005
DEFAULT_ARROW_THICKNESS = 0.01
DEFAULT_TEXT_SIZE = 0.05
DEFAULT_TEXT_SPACING = 0.8

TEXT_BASELINE_RATIO = 1.0

#: Kitsu's own default when a project does not set one (Zou:
#: ``preview_files_service.get_preview_file_fps``).
KITSU_DEFAULT_FPS = 25.0

#: Kitsu rounds playback times to 4 decimals, both in the web player and
#: server-side in ``_round_time_to_frame``.
KITSU_TIME_PRECISION = 10000

#: Kitsu numbers frames from 1, so the annotation at t=0 is frame 1.
KITSU_FRAME_BASE = 1


def _safe_fps(fps: Any) -> float:
    """Kitsu stores fps as a string, sometimes with a comma decimal mark."""
    if isinstance(fps, str):
        fps = fps.replace(",", ".").strip()
    try:
        value = float(fps)
    except (TypeError, ValueError):
        return KITSU_DEFAULT_FPS
    return value if value > 0 else KITSU_DEFAULT_FPS


def _frame_duration(fps: Any) -> float:
    """Length of one frame on the grid Kitsu snaps annotation times to.

    Kitsu computes this as ``1 / fps`` rounded (half-up) to 4 decimals and
    then works in multiples of *that*, so at 24fps a frame is 0.0417s, not
    0.041666... Following the same grid is what keeps our times matching the
    ones the player writes, which in turn lets Zou merge our additions into
    an existing entry instead of appending a duplicate.
    """
    fps = _safe_fps(fps)
    return math.floor(1.0 / fps * KITSU_TIME_PRECISION + 0.5) / KITSU_TIME_PRECISION


def kitsu_time_to_frame(
    time_value: Any, fps: Any, frame_base: int = KITSU_FRAME_BASE
) -> Optional[int]:
    """Kitsu annotation ``time`` (seconds) -> frame number.

    Returns None when the time cannot be read, so callers can skip the
    record rather than silently painting it onto frame 1.
    """
    try:
        seconds = float(time_value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        seconds = 0.0
    duration = _frame_duration(fps)
    if duration <= 0:
        return None
    return max(frame_base, math.floor(seconds / duration + 0.5) + frame_base)


def frame_to_kitsu_time(
    frame: Any, fps: Any, frame_base: int = KITSU_FRAME_BASE
) -> float:
    """Frame number -> the Kitsu annotation ``time`` in SECONDS.

    The inverse of :func:`kitsu_time_to_frame`. Kitsu identifies annotation
    entries by this value (Zou keys its additions/updates map on ``time``),
    so it has to land exactly on the grid.
    """
    try:
        frame_number = int(frame)
    except (TypeError, ValueError):
        frame_number = frame_base
    offset = max(0, frame_number - frame_base)
    raw = offset * _frame_duration(fps)
    return math.floor(raw * KITSU_TIME_PRECISION + 0.5) / KITSU_TIME_PRECISION


import re

_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

_RGB_FUNC_RE = re.compile(r"^rgba?\(([^)]*)\)$", re.IGNORECASE)

_NO_COLOR_VALUES = {"none", "null", "transparent", ""}

_FALSEY_STRINGS = {"false", "0", "no", "off", "none", "null", ""}


# ---------------------------------------------------------------------------
# Debug logging
#
# When enabled, the two public conversion entry points (convert_openrv_
# annotations / convert_kitsu_annotations) dump their input and output JSON
# to files so a failing/odd conversion can be inspected after the fact.
#
# Enable it either by:
#   - setting the RVKITSU_ANNOTATION_DEBUG_DIR environment variable to a
#     directory path, or
#   - passing debug_dir=<path> explicitly to either conversion function.
#
# Logging is best-effort: any failure to write a debug file is reported as a
# warning on stderr and never raises, so it can't break a real conversion.
# ---------------------------------------------------------------------------

_DEBUG_DIR_ENV_VAR = "RVKITSU_ANNOTATION_DEBUG_DIR"


def _resolve_debug_dir(explicit: Optional[str]) -> Optional[str]:
    return explicit if explicit is not None else os.environ.get(_DEBUG_DIR_ENV_VAR)


def _json_safe(value: Any) -> Any:
    """Recursively coerce a value into something json.dump can handle,
    without raising on odd/unexpected types encountered in real-world
    annotation data (e.g. tuples, sets, bytes, custom objects)."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    return str(value)


def _debug_dump(
    debug_dir: Optional[str],
    func_name: str,
    direction: str,
    payload: Any,
    call_id: str,
) -> None:
    """Best-effort write of `payload` as pretty-printed JSON to
    `{debug_dir}/{timestamp}_{func_name}_{call_id}_{direction}.json`."""
    if not debug_dir:
        return
    try:
        os.makedirs(debug_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        filename = f"{ts}_{func_name}_{call_id}_{direction}.json"
        path = os.path.join(debug_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_json_safe(payload), fh, indent=2, sort_keys=False)
        print(f"debug: wrote {func_name} {direction} to {path}", file=sys.stderr)
    except Exception as exc:
        print(
            f"warning: failed to write debug {direction} for {func_name}: {exc}",
            file=sys.stderr,
        )


def rv_normalized_to_pixel(
    nx: float, ny: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    m = max(width, height)
    px = (nx * (m / width) + 1.0) / 2.0 * canvas_width
    py = (1.0 - ny * (m / height)) / 2.0 * canvas_height
    return px, py


def pixel_to_rv_normalized(
    px: float, py: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    m = max(width, height)
    nx = (2.0 * px / canvas_width - 1.0) * (width / m)
    ny = (1.0 - 2.0 * py / canvas_height) * (height / m)
    return nx, ny


def norm_size_to_px(value: float, canvas_width: float, canvas_height: float) -> float:
    return value * canvas_height


def px_size_to_norm(value: float, canvas_width: float, canvas_height: float) -> float:
    return value / canvas_height if canvas_height else 0.0


def _first_pair(value: Any, default: Point = (0.0, 0.0)) -> Point:
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            try:
                return float(first[0]), float(first[1])
            except (TypeError, ValueError):
                return tuple(default)  # type: ignore[return-value]
        if len(value) >= 2:
            try:
                return float(value[0]), float(value[1])
            except (TypeError, ValueError):
                return tuple(default)  # type: ignore[return-value]
    return tuple(default)  # type: ignore[return-value]


def _first_scalar(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and value:
        return _first_scalar(value[0], default)
    return float(default)


def _pairs(value: Any) -> List[Point]:
    if not value:
        return []
    if isinstance(value[0], (list, tuple)):
        out: List[Point] = []
        for p in value:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                try:
                    out.append((float(p[0]), float(p[1])))
                except (TypeError, ValueError):
                    continue
        return out
    try:
        flat = [float(v) for v in value]
    except (TypeError, ValueError):
        return []
    return list(zip(flat[0::2], flat[1::2]))


def _rv_color_floats(
    color_rows: Any, default: Sequence[float] = (1.0, 1.0, 1.0, 1.0),
) -> Tuple[float, float, float, float]:
    if not color_rows or isinstance(color_rows, (int, float, str)):
        return tuple(default)  # type: ignore[return-value]
    try:
        row = color_rows[0] if isinstance(color_rows[0], (list, tuple)) else color_rows
        vals = [float(c) for c in list(row)[:4]]
    except (TypeError, ValueError, IndexError):
        return tuple(default)  # type: ignore[return-value]
    if len(vals) < 3:
        return tuple(default)  # type: ignore[return-value]
    if len(vals) == 3:
        vals.append(255.0 if max(vals) > 1.0 else 1.0)
    if max(vals) > 1.0:
        vals = [v / 255.0 for v in vals]
    return tuple(min(1.0, max(0.0, v)) for v in vals)  # type: ignore[return-value]


def rv_color_to_hex(color_rows: Any) -> str:
    r, g, b, _a = _rv_color_floats(color_rows)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def rv_alpha(color_rows: Any) -> float:
    return _rv_color_floats(color_rows)[3]


def _css_channel(part: str) -> int:
    part = part.strip()
    if part.endswith("%"):
        return int(round(max(0.0, min(100.0, float(part[:-1]))) * 255.0 / 100.0))
    return int(round(max(0.0, min(255.0, float(part)))))


def _css_alpha(part: str) -> int:
    part = part.strip()
    if part.endswith("%"):
        return int(round(max(0.0, min(100.0, float(part[:-1]))) * 255.0 / 100.0))
    return int(round(max(0.0, min(1.0, float(part))) * 255.0))


def _parse_css_color(value: Any) -> Optional[Tuple[int, int, int, Optional[int]]]:
    if not isinstance(value, str):
        return None
    text = value.strip()

    match = _RGB_FUNC_RE.match(text)
    if match:
        raw = match.group(1).replace("/", ",")
        parts = [p for p in (p.strip() for p in raw.split(",")) if p]
        if len(parts) < 3:
            return None
        try:
            r, g, b = (_css_channel(p) for p in parts[:3])
        except ValueError:
            return None
        alpha: Optional[int] = None
        if len(parts) >= 4:
            try:
                alpha = _css_alpha(parts[3])
            except ValueError:
                alpha = None
        return r, g, b, alpha

    if _HEX_COLOR_RE.match(text):
        h = text.lstrip("#")
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        try:
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
        alpha = int(h[6:8], 16) if len(h) == 8 else None
        return r, g, b, alpha

    return None


def _is_no_color(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NO_COLOR_VALUES)


def _color_channels(value: Any) -> Optional[Tuple[int, int, int, Optional[int]]]:
    if isinstance(value, (list, tuple)) and value:
        row = value[0] if isinstance(value[0], (list, tuple)) else value
        try:
            channels = [float(c) for c in list(row)[:4]]
        except (TypeError, ValueError):
            return None
        if len(channels) < 3:
            return None
        r, g, b = (int(round(c)) for c in channels[:3])
        a = int(round(channels[3])) if len(channels) >= 4 else None
        return r, g, b, a
    return _parse_css_color(value)


def _has_color(value: Any) -> bool:
    if _is_no_color(value):
        return False
    return _color_channels(value) is not None


def color_to_rv_color(value: Any, opacity: float = 1.0) -> List[List[int]]:
    scale = max(0.0, min(1.0, float(opacity)))
    fallback_alpha = int(round(scale * 255))

    if _is_no_color(value):
        return [[255, 255, 255, fallback_alpha]]

    channels = _color_channels(value)
    if channels is None:
        print(f"warning: unrecognized color value {value!r}, falling back to white", file=sys.stderr)
        return [[255, 255, 255, fallback_alpha]]

    r, g, b, a = channels
    a = fallback_alpha if a is None else int(round(a * scale))
    clamp = lambda c: max(0, min(255, int(c)))
    return [[clamp(r), clamp(g), clamp(b), clamp(a)]]


def _fabric_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY_STRINGS
    return bool(value)


def _fabric_scale(obj: Dict[str, Any]) -> Tuple[float, float]:
    def one(key: str) -> float:
        try:
            value = abs(float(obj.get(key, 1) or 1))
        except (TypeError, ValueError):
            return 1.0
        return value or 1.0
    return one("scaleX"), one("scaleY")


def _fabric_bbox(
    obj: Dict[str, Any],
    width_px: Optional[float] = None,
    height_px: Optional[float] = None,
) -> Tuple[float, float, float, float]:
    scale_x, scale_y = _fabric_scale(obj)

    def number(value: Any, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    w = number(width_px if width_px is not None else obj.get("width", 0)) * scale_x
    h = number(height_px if height_px is not None else obj.get("height", 0)) * scale_y
    left = number(obj.get("left", 0))
    top = number(obj.get("top", 0))

    origin_x = str(obj.get("originX") or "left").strip().lower()
    origin_y = str(obj.get("originY") or "top").strip().lower()
    if origin_x == "center":
        left -= w / 2.0
    elif origin_x == "right":
        left -= w
    if origin_y == "center":
        top -= h / 2.0
    elif origin_y == "bottom":
        top -= h

    return left, top, w, h


def _fabric_segment(obj: Dict[str, Any]) -> Tuple[Point, Point]:
    left, top, w, h = _fabric_bbox(obj)

    def number(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    x1, y1 = number(obj.get("x1")), number(obj.get("y1"))
    x2, y2 = number(obj.get("x2")), number(obj.get("y2"))

    flip_x = x1 is not None and x2 is not None and x2 < x1
    flip_y = y1 is not None and y2 is not None and y2 < y1
    if _fabric_bool(obj.get("flipX")):
        flip_x = not flip_x
    if _fabric_bool(obj.get("flipY")):
        flip_y = not flip_y

    start = (left + w if flip_x else left, top + h if flip_y else top)
    end = (left if flip_x else left + w, top if flip_y else top + h)
    return start, end


def _warn_dropped_rotation(obj: Dict[str, Any], kind: str) -> None:
    try:
        angle = float(obj.get("angle") or 0)
    except (TypeError, ValueError):
        return
    if abs(angle) > 1e-6:
        print(
            f"warning: {kind} {obj.get('id')!r} is rotated by {angle} degrees; "
            f"RVPaint's {kind} component is axis-aligned, so the rotation is "
            "dropped and the bounding box is used as-is",
            file=sys.stderr,
        )


def _fabric_base(
    obj_type: str,
    left: float, top: float, width: float, height: float,
    stroke_hex: Optional[str], stroke_width: float, opacity: float,
    author: str, canvas_width: float, canvas_height: float,
    source_uuid: Optional[str],
) -> Dict[str, Any]:
    return {
        "id": source_uuid or str(uuid.uuid4()),
        "type": obj_type,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "fill": None,
        "angle": 0,
        "flipX": False,
        "flipY": False,
        "skewX": 0,
        "skewY": 0,
        "scaleX": 1,
        "scaleY": 1,
        "shadow": None,
        "stroke": stroke_hex,
        "opacity": opacity,
        "originX": "left",
        "originY": "top",
        "version": "6.9.1",
        "visible": True,
        "erasable": True,
        "fillRule": "nonzero",
        "createdBy": author,
        "paintFirst": "fill",
        "canvasWidth": canvas_width,
        "strokeWidth": stroke_width,
        "canvasHeight": canvas_height,
        "strokeLineCap": "round",
        "strokeUniform": False,
        "strokeLineJoin": "round",
        "backgroundColor": "",
        "strokeDashArray": None,
        "strokeDashOffset": 0,
        "strokeMiterLimit": 10,
        "globalCompositeOperation": "source-over",
    }


def _inner_fill(props: Dict[str, Any]) -> Optional[str]:
    inner = props.get("innerColor")
    if not inner or rv_alpha(inner) <= 0:
        return None
    return rv_color_to_hex(inner)


def _pen_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    points_px = [
        rv_normalized_to_pixel(nx, ny, width, height, canvas_width, canvas_height)
        for nx, ny in _pairs(props.get("points"))
    ]
    if not points_px:
        raise ValueError("pen shape has no points")

    xs = [p[0] for p in points_px]
    ys = [p[1] for p in points_px]
    left, top = min(xs), min(ys)
    bbox_w, bbox_h = max(xs) - left, max(ys) - top

    stroke_width_norm = _first_scalar(props.get("width"), DEFAULT_PEN_WIDTH)
    stroke_width_px = norm_size_to_px(stroke_width_norm, canvas_width, canvas_height)

    start_time = clock_ms[0]
    duration = max(1, len(points_px) * MS_PER_STROKE_POINT)
    end_time = start_time + duration
    clock_ms[0] = end_time

    obj = _fabric_base(
        "PSStroke", left, top, bbox_w, bbox_h,
        rv_color_to_hex(props.get("color", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("color", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["strokePoints"] = [
        {"x": px, "y": py, "type": "PSPoint", "pressure": 1}
        for (px, py) in points_px
    ]
    return obj


def _line_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    sx, sy = _first_pair(props.get("startPos"))
    ex, ey = _first_pair(props.get("endPos"))
    x1, y1 = rv_normalized_to_pixel(sx, sy, width, height, canvas_width, canvas_height)
    x2, y2 = rv_normalized_to_pixel(ex, ey, width, height, canvas_width, canvas_height)

    left, top = min(x1, x2), min(y1, y2)
    bbox_w, bbox_h = abs(x2 - x1), abs(y2 - y1)
    stroke_width_px = norm_size_to_px(
        _first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH),
        canvas_width, canvas_height,
    )

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * 2
    clock_ms[0] = end_time

    obj = _fabric_base(
        "line", left, top, bbox_w, bbox_h,
        rv_color_to_hex(props.get("borderColor", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["x1"], obj["y1"], obj["x2"], obj["y2"] = x1, y1, x2, y2
    return obj


def _ellipse_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    minx, miny = _first_pair(props.get("min"))
    maxx, maxy = _first_pair(props.get("max"), (0.1, 0.1))

    p0 = rv_normalized_to_pixel(minx, miny, width, height, canvas_width, canvas_height)
    p1 = rv_normalized_to_pixel(maxx, maxy, width, height, canvas_width, canvas_height)
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))

    left, top = x0, y0
    bbox_w, bbox_h = x1 - x0, y1 - y0
    rx, ry = bbox_w / 2.0, bbox_h / 2.0
    stroke_width_px = norm_size_to_px(
        _first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH),
        canvas_width, canvas_height,
    )

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * 4
    clock_ms[0] = end_time

    obj = _fabric_base(
        "ellipse", left, top, bbox_w, bbox_h,
        rv_color_to_hex(props.get("borderColor", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["rx"] = rx
    obj["ry"] = ry
    obj["fill"] = _inner_fill(props)
    return obj


def _circle_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    cx, cy = _first_pair(props.get("center"))
    r = _first_scalar(props.get("radius"), 0.05)

    center_px = rv_normalized_to_pixel(cx, cy, width, height, canvas_width, canvas_height)
    edge_x_px = rv_normalized_to_pixel(cx + r, cy, width, height, canvas_width, canvas_height)
    edge_y_px = rv_normalized_to_pixel(cx, cy + r, width, height, canvas_width, canvas_height)

    rx = abs(edge_x_px[0] - center_px[0])
    ry = abs(edge_y_px[1] - center_px[1])
    radius_px = (rx + ry) / 2.0

    left, top = center_px[0] - radius_px, center_px[1] - radius_px
    stroke_width_px = norm_size_to_px(
        _first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH),
        canvas_width, canvas_height,
    )

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * 4
    clock_ms[0] = end_time

    obj = _fabric_base(
        "circle", left, top, radius_px * 2, radius_px * 2,
        rv_color_to_hex(props.get("borderColor", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["radius"] = round(radius_px, 2)
    obj["fill"] = _inner_fill(props)
    return obj


def _rectangle_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    minx, miny = _first_pair(props.get("min"))
    maxx, maxy = _first_pair(props.get("max"), (0.1, 0.1))

    p0 = rv_normalized_to_pixel(minx, miny, width, height, canvas_width, canvas_height)
    p1 = rv_normalized_to_pixel(maxx, maxy, width, height, canvas_width, canvas_height)
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))

    left, top = x0, y0
    bbox_w, bbox_h = x1 - x0, y1 - y0
    stroke_width_px = norm_size_to_px(
        _first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH),
        canvas_width, canvas_height,
    )

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * 4
    clock_ms[0] = end_time

    obj = _fabric_base(
        "rect", left, top, bbox_w, bbox_h,
        rv_color_to_hex(props.get("borderColor", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["rx"] = 0
    obj["ry"] = 0
    obj["fill"] = _inner_fill(props)
    return obj


def _arrow_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    sx, sy = _first_pair(props.get("startPos"))
    ex, ey = _first_pair(props.get("endPos"), (0.1, 0.0))
    x1, y1 = rv_normalized_to_pixel(sx, sy, width, height, canvas_width, canvas_height)
    x2, y2 = rv_normalized_to_pixel(ex, ey, width, height, canvas_width, canvas_height)

    left, top = min(x1, x2), min(y1, y2)
    bbox_w, bbox_h = abs(x2 - x1), abs(y2 - y1)
    stroke_width_px = norm_size_to_px(
        _first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH),
        canvas_width, canvas_height,
    )
    head_size_px = norm_size_to_px(
        _first_scalar(props.get("thickness"), DEFAULT_ARROW_THICKNESS),
        canvas_width, canvas_height,
    )

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * 2
    clock_ms[0] = end_time

    obj = _fabric_base(
        "arrow", left, top, bbox_w, bbox_h,
        rv_color_to_hex(props.get("borderColor", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["x1"], obj["y1"], obj["x2"], obj["y2"] = x1, y1, x2, y2
    obj["arrowHeadSize"] = round(head_size_px, 2)
    obj["arrowHeadWidth"] = round(head_size_px * 0.8, 2)
    obj["fill"] = _inner_fill(props)
    return obj


def _text_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    pos_x, pos_y = _first_pair(props.get("position"))
    px, py = rv_normalized_to_pixel(pos_x, pos_y, width, height, canvas_width, canvas_height)

    text = props.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    size_norm = _first_scalar(props.get("size"), DEFAULT_TEXT_SIZE)
    font_size_px = norm_size_to_px(size_norm, canvas_width, canvas_height)
    spacing = _first_scalar(props.get("spacing"), DEFAULT_TEXT_SPACING)
    rotation_deg = _first_scalar(props.get("rotation"), 0.0)

    top = py - font_size_px * TEXT_BASELINE_RATIO

    approx_char_w = font_size_px * 0.55
    longest_line = max((len(line) for line in text.split("\n")), default=0)
    bbox_w = max(font_size_px, approx_char_w * longest_line)
    line_count = max(1, text.count("\n") + 1)
    bbox_h = font_size_px * max(spacing, 1.0) * line_count

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * max(1, len(text))
    clock_ms[0] = end_time

    obj = _fabric_base(
        "textbox", px, top, bbox_w, bbox_h,
        None, 0,
        rv_alpha(props.get("color", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["angle"] = -rotation_deg
    obj["fill"] = rv_color_to_hex(props.get("color", []))
    obj["text"] = text
    obj["fontSize"] = round(font_size_px, 2)
    obj["fontFamily"] = props.get("font") or "Arial"
    obj["fontWeight"] = "normal"
    obj["fontStyle"] = "normal"
    obj["textAlign"] = "left"

    obj["lineHeight"] = spacing
    obj["underline"] = False
    obj["overline"] = False
    obj["linethrough"] = False
    return obj


def _infer_canvas_size(
    kitsu_records: Sequence[Dict[str, Any]],
    fallback_width: float,
    fallback_height: float,
) -> Tuple[float, float]:
    for record in kitsu_records or []:
        for obj in record.get("drawing", {}).get("objects", []):
            cw, ch = obj.get("canvasWidth"), obj.get("canvasHeight")
            if cw and ch:
                return float(cw), float(ch)
    return float(fallback_width), float(fallback_height)


_SHAPE_CONVERTERS = {
    "pen": _pen_to_fabric,
    "line": _line_to_fabric,
    "ellipse": _ellipse_to_fabric,
    "circle": _circle_to_fabric,
    "rect": _rectangle_to_fabric,
    "rectangle": _rectangle_to_fabric,
    "arrow": _arrow_to_fabric,
    "text": _text_to_fabric,
}


def convert_openrv_annotations(
    openrv_shapes: List[Dict[str, Any]],
    width: int,
    height: int,
    fps: float = 24.0,
    author: Optional[str] = None,
    canvas_width: Optional[float] = None,
    canvas_height: Optional[float] = None,
    frame_offset: int = 0,
    frame_base: int = 1,
    skip_soft_deleted: bool = True,
    debug_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    debug_dir = _resolve_debug_dir(debug_dir)
    call_id = uuid.uuid4().hex[:8]
    _debug_dump(
        debug_dir, "convert_openrv_annotations", "input",
        {
            "openrv_shapes": openrv_shapes,
            "width": width,
            "height": height,
            "fps": fps,
            "author": author,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "frame_offset": frame_offset,
            "frame_base": frame_base,
            "skip_soft_deleted": skip_soft_deleted,
        },
        call_id,
    )

    author = author or str(uuid.uuid4())
    canvas_width = float(canvas_width or width)
    canvas_height = float(canvas_height or height)

    by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for shape in openrv_shapes:
        if skip_soft_deleted and shape.get("properties", {}).get("softDeleted"):
            continue
        frame_num = shape["frame"] + frame_offset
        by_frame.setdefault(frame_num, []).append(shape)

    clock_ms = [int(_time.time() * 1000)]
    records: List[Dict[str, Any]] = []

    for frame_num in sorted(by_frame):
        objects: List[Dict[str, Any]] = []
        for shape in by_frame[frame_num]:
            shape_type = shape.get("type")
            converter = _SHAPE_CONVERTERS.get(shape_type)
            if converter is None:
                print(
                    f"warning: skipping unsupported OpenRV shape type "
                    f"'{shape_type}' (name={shape.get('name')!r})",
                    file=sys.stderr,
                )
                continue

            try:
                objects.append(converter(
                    shape, width, height, canvas_width, canvas_height,
                    author, clock_ms,
                ))
            except Exception as exc:
                print(
                    f"warning: could not convert OpenRV shape "
                    f"{shape.get('name') or shape.get('type')!r} on frame "
                    f"{frame_num}: {exc}",
                    file=sys.stderr,
                )

        if not objects:
            continue

        records.append({
            # Kitsu stores the annotation time in SECONDS, snapped to its
            # frame grid, and Zou keys additions/updates on this exact value.
            # Sending milliseconds put every annotation far past the end of
            # the clip and stopped them merging with existing entries.
            "time": frame_to_kitsu_time(frame_num, fps, frame_base),
            # Not read by Kitsu; kept as a convenience for tooling and to let
            # older exports from this plugin still be re-imported.
            "frame": frame_num,
            "drawing": {"objects": objects},
        })

    _debug_dump(debug_dir, "convert_openrv_annotations", "output", records, call_id)
    return records


def _border_from_fabric(obj: Dict[str, Any], canvas_width: float, canvas_height: float) -> float:
    default_px = norm_size_to_px(DEFAULT_BORDER_WIDTH, canvas_width, canvas_height)
    stroke_width_px = obj.get("strokeWidth")
    if stroke_width_px is None:
        stroke_width_px = default_px

    if not _fabric_bool(obj.get("strokeUniform")):
        scale_x, scale_y = _fabric_scale(obj)
        stroke_width_px = float(stroke_width_px) * (scale_x + scale_y) / 2.0
    return px_size_to_norm(float(stroke_width_px), canvas_width, canvas_height)


def _fill_from_fabric(obj: Dict[str, Any], default_transparent: bool = True) -> List[List[int]]:
    fill = obj.get("fill")
    if _has_color(fill):
        return color_to_rv_color(fill, obj.get("opacity", 1.0))
    return [[0, 0, 0, 0]] if default_transparent else [[255, 255, 255, 255]]


def _pen_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    points_norm = []
    for p in obj.get("strokePoints", []) or []:
        if not isinstance(p, dict) or "x" not in p or "y" not in p:
            continue
        points_norm.append(
            pixel_to_rv_normalized(p["x"], p["y"], width, height, canvas_width, canvas_height)
        )

    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "points": points_norm,
            "width": [_border_from_fabric(obj, canvas_width, canvas_height)],
            "color": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
        },
    }


def _line_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    (x1, y1), (x2, y2) = _fabric_segment(obj)

    start = pixel_to_rv_normalized(x1, y1, width, height, canvas_width, canvas_height)
    end = pixel_to_rv_normalized(x2, y2, width, height, canvas_width, canvas_height)

    return {
        "type": "line",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "startPos": [start],
            "endPos": [end],
            "borderWidth": _border_from_fabric(obj, canvas_width, canvas_height),
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
        },
    }


def _ellipse_bbox_from_fabric(obj: Dict[str, Any]) -> Tuple[float, float, float, float]:
    rx, ry = obj.get("rx"), obj.get("ry")
    if rx is None and ry is None:
        return _fabric_bbox(obj)
    try:
        rx_f = float(rx if rx is not None else ry)
        ry_f = float(ry if ry is not None else rx)
    except (TypeError, ValueError):
        return _fabric_bbox(obj)
    return _fabric_bbox(obj, rx_f * 2.0, ry_f * 2.0)


def _ellipse_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    _warn_dropped_rotation(obj, "ellipse")
    left, top, bbox_w, bbox_h = _ellipse_bbox_from_fabric(obj)

    c0 = pixel_to_rv_normalized(left, top, width, height, canvas_width, canvas_height)
    c1 = pixel_to_rv_normalized(left + bbox_w, top + bbox_h, width, height, canvas_width, canvas_height)

    min_x, max_x = sorted((c0[0], c1[0]))
    min_y, max_y = sorted((c0[1], c1[1]))

    return {
        "type": "ellipse",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "min": [(min_x, min_y)],
            "max": [(max_x, max_y)],
            "borderWidth": _border_from_fabric(obj, canvas_width, canvas_height),
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
            "innerColor": _fill_from_fabric(obj),
        },
    }


def _circle_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    _warn_dropped_rotation(obj, "circle")
    radius = obj.get("radius")
    if radius is None:
        left, top, bbox_w, bbox_h = _fabric_bbox(obj)
    else:
        try:
            diameter = float(radius) * 2.0
        except (TypeError, ValueError):
            diameter = 0.0
        left, top, bbox_w, bbox_h = _fabric_bbox(obj, diameter, diameter)

    c0 = pixel_to_rv_normalized(left, top, width, height, canvas_width, canvas_height)
    c1 = pixel_to_rv_normalized(left + bbox_w, top + bbox_h, width, height, canvas_width, canvas_height)

    min_x, max_x = sorted((c0[0], c1[0]))
    min_y, max_y = sorted((c0[1], c1[1]))

    return {
        "type": "ellipse",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "min": [(min_x, min_y)],
            "max": [(max_x, max_y)],
            "borderWidth": _border_from_fabric(obj, canvas_width, canvas_height),
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
            "innerColor": _fill_from_fabric(obj),
        },
    }


def _rectangle_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    _warn_dropped_rotation(obj, "rectangle")
    left, top, bbox_w, bbox_h = _fabric_bbox(obj)

    c0 = pixel_to_rv_normalized(left, top, width, height, canvas_width, canvas_height)
    c1 = pixel_to_rv_normalized(left + bbox_w, top + bbox_h, width, height, canvas_width, canvas_height)

    min_x, max_x = sorted((c0[0], c1[0]))
    min_y, max_y = sorted((c0[1], c1[1]))

    return {
        "type": "rectangle",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "min": [(min_x, min_y)],
            "max": [(max_x, max_y)],
            "borderWidth": _border_from_fabric(obj, canvas_width, canvas_height),
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
            "innerColor": _fill_from_fabric(obj),
        },
    }


def _arrow_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    (x1, y1), (x2, y2) = _fabric_segment(obj)

    start = pixel_to_rv_normalized(x1, y1, width, height, canvas_width, canvas_height)
    end = pixel_to_rv_normalized(x2, y2, width, height, canvas_width, canvas_height)

    head_size_px = obj.get("arrowHeadSize")
    if head_size_px is None:
        head_size_px = obj.get("headSize")
    if head_size_px is None:
        thickness_norm = DEFAULT_ARROW_THICKNESS
    else:
        thickness_norm = px_size_to_norm(float(head_size_px), canvas_width, canvas_height)

    if "fill" in obj:
        inner_color = _fill_from_fabric(obj)
    else:
        inner_color = color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0))

    return {
        "type": "arrow",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "startPos": [start],
            "endPos": [end],
            "borderWidth": _border_from_fabric(obj, canvas_width, canvas_height),
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
            "thickness": thickness_norm,
            "innerColor": inner_color,
        },
    }


def _text_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    _scale_x, scale_y = _fabric_scale(obj)

    font_size_px = obj.get("fontSize")
    if font_size_px is None:
        font_size_px = norm_size_to_px(DEFAULT_TEXT_SIZE, canvas_width, canvas_height)

    font_size_px = float(font_size_px) * scale_y
    size_norm = px_size_to_norm(font_size_px, canvas_width, canvas_height)

    left, top, _bbox_w, _bbox_h = _fabric_bbox(obj)

    baseline_y = top + font_size_px * TEXT_BASELINE_RATIO
    position_norm = pixel_to_rv_normalized(left, baseline_y, width, height, canvas_width, canvas_height)

    fill = obj.get("fill")
    color = color_to_rv_color(fill, obj.get("opacity", 1.0)) if _has_color(fill) else [[255, 255, 255, 255]]

    try:
        angle = float(obj.get("angle") or 0)
    except (TypeError, ValueError):
        angle = 0.0

    return {
        "type": "text",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "position": [position_norm],
            "text": obj.get("text", ""),
            "size": size_norm,
            "rotation": -angle,
            "spacing": obj.get("lineHeight", DEFAULT_TEXT_SPACING),
            "font": obj.get("fontFamily") or "",
            "color": color,
        },
    }


#: Points sampled per Bezier segment when flattening a fabric path. Fabric
#: emits freehand strokes as many short quadratic segments, so a low count
#: is already visually indistinguishable from the original curve.
PATH_CURVE_SAMPLES = 8


def _parse_path_commands(raw: Any) -> List[List[Any]]:
    """Normalise a fabric ``path`` into a list of ``[op, *params]``.

    Fabric serialises the path either as a nested list or, when it has been
    round-tripped through SVG, as a command string.
    """
    if isinstance(raw, (list, tuple)):
        commands = []
        for cmd in raw:
            if not cmd:
                continue
            if isinstance(cmd, str):
                commands.extend(_parse_path_string(cmd))
                continue
            try:
                op = str(cmd[0]).upper()
            except (IndexError, TypeError):
                continue
            params = []
            for value in list(cmd)[1:]:
                try:
                    params.append(float(value))
                except (TypeError, ValueError):
                    params = None
                    break
            if params is None:
                continue
            commands.append([op] + params)
        return commands
    if isinstance(raw, str):
        return _parse_path_string(raw)
    return []


_PATH_CMD_RE = re.compile(r"([MLQCZmlqcz])\s*([^MLQCZmlqcz]*)")
_PATH_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _parse_path_string(text: str) -> List[List[Any]]:
    """Parse an SVG-style path string.

    Only the command set fabric actually emits (M/L/Q/C/Z) is handled, and
    lowercase is treated as absolute — the same simplification Zou's own
    renderer makes, since fabric always serialises absolute commands.
    """
    commands: List[List[Any]] = []
    for match in _PATH_CMD_RE.finditer(text):
        op = match.group(1).upper()
        params = [float(n) for n in _PATH_NUM_RE.findall(match.group(2) or "")]
        commands.append([op] + params)
    return commands


def _path_anchor_points(commands: Sequence[Sequence[Any]]) -> List[Point]:
    """Destination point of each M/L/Q/C — enough to bracket the bbox."""
    points: List[Point] = []
    for cmd in commands:
        op, params = cmd[0], list(cmd[1:])
        if op in ("M", "L") and len(params) >= 2:
            points.append((params[0], params[1]))
        elif op == "Q" and len(params) >= 4:
            points.append((params[2], params[3]))
        elif op == "C" and len(params) >= 6:
            points.append((params[4], params[5]))
    return points


def _quadratic_point(p0: Point, c: Point, p1: Point, t: float) -> Point:
    inv = 1.0 - t
    return (
        inv * inv * p0[0] + 2 * inv * t * c[0] + t * t * p1[0],
        inv * inv * p0[1] + 2 * inv * t * c[1] + t * t * p1[1],
    )


def _cubic_point(p0: Point, c1: Point, c2: Point, p1: Point, t: float) -> Point:
    inv = 1.0 - t
    return (
        inv ** 3 * p0[0] + 3 * inv ** 2 * t * c1[0]
        + 3 * inv * t ** 2 * c2[0] + t ** 3 * p1[0],
        inv ** 3 * p0[1] + 3 * inv ** 2 * t * c1[1]
        + 3 * inv * t ** 2 * c2[1] + t ** 3 * p1[1],
    )


def _flatten_path(
    commands: Sequence[Sequence[Any]], samples: int = PATH_CURVE_SAMPLES
) -> List[Point]:
    """Flatten path commands into a polyline in the path's local space."""
    points: List[Point] = []
    current: Optional[Point] = None
    start: Optional[Point] = None

    def push(point: Point) -> None:
        if not points or points[-1] != point:
            points.append(point)

    for cmd in commands:
        op, params = cmd[0], list(cmd[1:])
        if op == "M" and len(params) >= 2:
            current = (params[0], params[1])
            start = current
            push(current)
        elif op == "L" and len(params) >= 2:
            current = (params[0], params[1])
            push(current)
        elif op == "Q" and len(params) >= 4 and current is not None:
            control = (params[0], params[1])
            end = (params[2], params[3])
            for step in range(1, samples + 1):
                push(_quadratic_point(current, control, end, step / samples))
            current = end
        elif op == "C" and len(params) >= 6 and current is not None:
            c1 = (params[0], params[1])
            c2 = (params[2], params[3])
            end = (params[4], params[5])
            for step in range(1, samples + 1):
                push(_cubic_point(current, c1, c2, end, step / samples))
            current = end
        elif op == "Z" and start is not None:
            push(start)
            current = start
    return points


def _fabric_object_transform(obj: Dict[str, Any], pivot: Point):
    """The affine transform fabric applies to a shape's local coordinates.

    ``T(center) . R(angle) . S(scaleX, scaleY) . T(-pivot)``, matching
    fabric's ``getRelativeCenterPoint()``: the un-rotated bbox centre is
    itself rotated around ``(left, top)``, because fabric rewrites
    ``left``/``top`` on rotation to keep the centre visually put.
    """
    def number(key: str, fallback: float = 0.0) -> float:
        try:
            value = obj.get(key, fallback)
            return float(fallback if value is None else value)
        except (TypeError, ValueError):
            return fallback

    angle = math.radians(number("angle", 0.0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    scale_x = number("scaleX", 1.0) or 1.0
    scale_y = number("scaleY", 1.0) or 1.0
    left, top = number("left", 0.0), number("top", 0.0)
    width, height = number("width", 0.0), number("height", 0.0)

    naive_cx = left + width / 2.0 * scale_x
    naive_cy = top + height / 2.0 * scale_y
    cx = left + (naive_cx - left) * cos_a - (naive_cy - top) * sin_a
    cy = top + (naive_cx - left) * sin_a + (naive_cy - top) * cos_a

    pivot_x, pivot_y = pivot

    def transform(px: float, py: float) -> Point:
        dx, dy = (px - pivot_x) * scale_x, (py - pivot_y) * scale_y
        return (dx * cos_a - dy * sin_a + cx, dx * sin_a + dy * cos_a + cy)

    return transform


def _path_pivot(obj: Dict[str, Any], anchors: Sequence[Point]) -> Point:
    """Fabric's rotation/scale pivot for a path.

    ``pathOffset`` is authoritative when present; otherwise fall back to the
    bbox centre of the path's own anchor points, which is what fabric
    recomputes on deserialisation.
    """
    offset = obj.get("pathOffset")
    if isinstance(offset, dict):
        try:
            return float(offset.get("x") or 0.0), float(offset.get("y") or 0.0)
        except (TypeError, ValueError):
            pass
    if anchors:
        xs = [p[0] for p in anchors]
        ys = [p[1] for p in anchors]
        return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    return _fabric_bbox(obj)[0], _fabric_bbox(obj)[1]


def _path_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    """A fabric ``path`` (Kitsu's freehand pen tool) -> an RVPaint pen stroke."""
    commands = _parse_path_commands(obj.get("path"))
    if not commands:
        raise ValueError("path object has no usable path commands")

    local_points = _flatten_path(commands)
    if not local_points:
        raise ValueError("path object flattened to no points")

    anchors = _path_anchor_points(commands)

    # Fabric recomputes left/top/width/height from the path's own bbox on
    # deserialisation, so fill them in when the JSON omits them; otherwise
    # the transform centre and the pivot disagree and the stroke shifts.
    obj = _with_default_bbox(obj, anchors)

    transform = _fabric_object_transform(obj, _path_pivot(obj, anchors))

    points_norm = []
    for px, py in local_points:
        cx, cy = transform(px, py)
        points_norm.append(
            pixel_to_rv_normalized(cx, cy, width, height, canvas_width, canvas_height)
        )

    stroke = obj.get("stroke")
    # A freehand path is drawn with its stroke; fabric still records a fill,
    # which must not win, or the whole stroke comes back the wrong colour.
    color = color_to_rv_color(stroke, obj.get("opacity", 1.0)) if _has_color(stroke) \
        else _fill_from_fabric(obj, default_transparent=False)

    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "points": points_norm,
            "width": [_border_from_fabric(obj, canvas_width, canvas_height)],
            "color": color,
        },
    }


def _polyline_anchors(obj: Dict[str, Any]) -> List[Point]:
    """Fabric stores polyline/polygon points as [{"x": .., "y": ..}, ...]."""
    points: List[Point] = []
    for p in obj.get("points") or []:
        if isinstance(p, dict):
            try:
                points.append((float(p.get("x") or 0.0), float(p.get("y") or 0.0)))
            except (TypeError, ValueError):
                continue
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                points.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    return points


def _points_to_pen(
    obj: Dict[str, Any], anchors: Sequence[Point], close: bool,
    width: int, height: int, canvas_width: float, canvas_height: float,
    frame_num: int, kind: str,
) -> Dict[str, Any]:
    """Shared body for polyline/polygon -> an RVPaint pen stroke."""
    if len(anchors) < 2:
        raise ValueError(f"{kind} has fewer than two points")

    local = list(anchors)
    if close and local[0] != local[-1]:
        local.append(local[0])

    obj = _with_default_bbox(obj, anchors)
    transform = _fabric_object_transform(obj, _path_pivot(obj, anchors))

    points_norm = []
    for px, py in local:
        cx, cy = transform(px, py)
        points_norm.append(
            pixel_to_rv_normalized(cx, cy, width, height, canvas_width, canvas_height)
        )

    stroke = obj.get("stroke")
    color = color_to_rv_color(stroke, obj.get("opacity", 1.0)) if _has_color(stroke) \
        else _fill_from_fabric(obj, default_transparent=False)

    if close and _has_color(obj.get("fill")):
        print(
            f"warning: polygon {obj.get('id')!r} has a fill, which RVPaint's pen "
            "component cannot reproduce; only its outline is imported",
            file=sys.stderr,
        )

    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "points": points_norm,
            "width": [_border_from_fabric(obj, canvas_width, canvas_height)],
            "color": color,
        },
    }


def _polyline_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    return _points_to_pen(
        obj, _polyline_anchors(obj), False,
        width, height, canvas_width, canvas_height, frame_num, "polyline",
    )


def _polygon_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    return _points_to_pen(
        obj, _polyline_anchors(obj), True,
        width, height, canvas_width, canvas_height, frame_num, "polygon",
    )


def _with_default_bbox(
    obj: Dict[str, Any], anchor_points: Sequence[Point]
) -> Dict[str, Any]:
    """Fill in left/top/width/height from the anchors when the JSON omits them.

    Fabric recomputes these from ``calcDim`` on deserialisation, and the
    transform's centre and pivot only agree once they are present.
    """
    if not anchor_points:
        return obj
    if all(obj.get(k) is not None for k in ("left", "top", "width", "height")):
        return obj
    xs = [p[0] for p in anchor_points]
    ys = [p[1] for p in anchor_points]
    out = dict(obj)
    out.setdefault("left", min(xs))
    out.setdefault("top", min(ys))
    out.setdefault("width", max(xs) - min(xs))
    out.setdefault("height", max(ys) - min(ys))
    return out


def _group_center(obj: Dict[str, Any]) -> Point:
    """Canvas position of a group's origin.

    A fabric group's children are expressed in the group's *centered* local
    frame, so the group's centre is where the child origin lands.
    """
    return _fabric_object_transform(obj, (0.0, 0.0))(0.0, 0.0)


def _group_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> List[Dict[str, Any]]:
    """A fabric group -> one RVPaint shape per child.

    Children live in the group's centered frame, so they are translated onto
    the group's centre before being run through the normal converters. A
    rotated or scaled group is only approximated by that translation, since
    RVPaint has no equivalent grouping to carry the transform.
    """
    children = obj.get("objects") or []
    if not children:
        raise ValueError("group has no child objects")

    scale_x, scale_y = _fabric_scale(obj)
    try:
        angle = float(obj.get("angle") or 0)
    except (TypeError, ValueError):
        angle = 0.0
    if abs(angle) > 1e-6 or abs(scale_x - 1.0) > 1e-6 or abs(scale_y - 1.0) > 1e-6:
        print(
            f"warning: group {obj.get('id')!r} is rotated or scaled; its "
            "children are imported at their translated positions only, so "
            "the group transform is not reproduced",
            file=sys.stderr,
        )

    offset_x, offset_y = _group_center(obj)
    shapes: List[Dict[str, Any]] = []

    for child in children:
        if not isinstance(child, dict):
            continue
        converter = _lookup_type_converter(child.get("type"))
        if converter is None:
            print(
                f"warning: skipping unsupported grouped object type "
                f"{child.get('type')!r} (id={child.get('id')!r})",
                file=sys.stderr,
            )
            continue

        moved = dict(child)
        for key, delta in (("left", offset_x), ("top", offset_y)):
            try:
                moved[key] = float(moved.get(key) or 0.0) + delta
            except (TypeError, ValueError):
                moved[key] = delta
        moved.setdefault("canvasWidth", obj.get("canvasWidth"))
        moved.setdefault("canvasHeight", obj.get("canvasHeight"))

        try:
            produced = converter(
                moved, width, height, canvas_width, canvas_height, frame_num
            )
        except Exception as exc:
            print(
                f"warning: could not convert grouped object "
                f"{child.get('id')!r}: {exc}",
                file=sys.stderr,
            )
            continue
        shapes.extend(produced if isinstance(produced, list) else [produced])

    if not shapes:
        raise ValueError("group produced no convertible children")
    return shapes


def _warn_dropped_eraser(obj: Dict[str, Any]) -> None:
    """Kitsu's eraser is a per-shape mask RVPaint cannot reproduce.

    It rides along on the shape as an ``eraser`` dict of paths that punch
    holes via destination-out compositing. RVPaint has no equivalent, so the
    shape imports whole and the erased parts reappear -- worth saying out
    loud rather than letting the artist wonder why.
    """
    eraser = obj.get("eraser")
    if isinstance(eraser, dict) and (eraser.get("objects") or []):
        print(
            f"warning: annotation {obj.get('id')!r} has erased regions that "
            "RVPaint cannot represent; it is imported without them, so the "
            "erased parts will be visible again in RV",
            file=sys.stderr,
        )


_TYPE_CONVERTERS = {
    "PSStroke": _pen_from_fabric,
    "path": _path_from_fabric,
    "Path": _path_from_fabric,
    "polyline": _polyline_from_fabric,
    "Polyline": _polyline_from_fabric,
    "polygon": _polygon_from_fabric,
    "Polygon": _polygon_from_fabric,
    "group": _group_from_fabric,
    "Group": _group_from_fabric,
    "line": _line_from_fabric,
    "PSLine": _line_from_fabric,
    "ellipse": _ellipse_from_fabric,
    "PSEllipse": _ellipse_from_fabric,
    "circle": _circle_from_fabric,
    "PSCircle": _circle_from_fabric,
    "rect": _rectangle_from_fabric,
    "rectangle": _rectangle_from_fabric,
    "PSRect": _rectangle_from_fabric,
    "PSRectangle": _rectangle_from_fabric,
    "arrow": _arrow_from_fabric,
    "PSArrow": _arrow_from_fabric,
    "textbox": _text_from_fabric,
    "text": _text_from_fabric,
    "i-text": _text_from_fabric,
    "PSText": _text_from_fabric,
}

_TYPE_CONVERTERS_LOWER = {key.lower(): value for key, value in _TYPE_CONVERTERS.items()}


def _lookup_type_converter(obj_type: Any):
    if not isinstance(obj_type, str):
        return None
    key = obj_type.strip()
    return _TYPE_CONVERTERS.get(key) or _TYPE_CONVERTERS_LOWER.get(key.lower())


def _record_frame(
    record: Dict[str, Any], fps: Any, frame_offset: int
) -> Optional[int]:
    """Frame number for one Kitsu annotation record.

    Kitsu identifies an annotation by its playback ``time`` in seconds --
    the records the web player writes have no ``frame`` key at all, and
    Zou keys its own merge/update logic on ``time``. Earlier versions of
    this plugin wrote a ``frame`` key, so it is still honoured as a
    fallback, but ``time`` wins: it is what the player uses to decide
    which frame to show the drawing on.
    """
    frame = kitsu_time_to_frame(record.get("time"), fps)
    if frame is None:
        try:
            frame = int(record["frame"])
        except (KeyError, TypeError, ValueError):
            print(
                f"warning: skipping annotation record with neither a usable "
                f"'time' nor 'frame': {record!r}",
                file=sys.stderr,
            )
            return None
    return frame - frame_offset


def convert_kitsu_annotations(
    kitsu_records: Sequence[Dict[str, Any]],
    width: int,
    height: int,
    canvas_width: Optional[float] = None,
    canvas_height: Optional[float] = None,
    frame_offset: int = 0,
    fps: Any = KITSU_DEFAULT_FPS,
    debug_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    debug_dir = _resolve_debug_dir(debug_dir)
    call_id = uuid.uuid4().hex[:8]
    _debug_dump(
        debug_dir, "convert_kitsu_annotations", "input",
        {
            "kitsu_records": kitsu_records,
            "width": width,
            "height": height,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "frame_offset": frame_offset,
            "fps": fps,
        },
        call_id,
    )

    default_canvas_width = float(canvas_width or width)
    default_canvas_height = float(canvas_height or height)

    shapes: List[Dict[str, Any]] = []
    for record in kitsu_records or []:
        if not isinstance(record, dict):
            continue
        frame_num = _record_frame(record, fps, frame_offset)
        if frame_num is None:
            continue
        objects = (record.get("drawing") or {}).get("objects", []) or []

        for obj in objects:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("type")
            converter = _lookup_type_converter(obj_type)
            if converter is None:
                print(
                    f"warning: skipping unsupported Kitsu object type "
                    f"'{obj_type}' (id={obj.get('id')!r}); known types are "
                    f"{sorted(_TYPE_CONVERTERS)}",
                    file=sys.stderr,
                )
                continue

            obj_canvas_width = float(obj.get("canvasWidth") or default_canvas_width)
            obj_canvas_height = float(obj.get("canvasHeight") or default_canvas_height)

            _warn_dropped_eraser(obj)

            try:
                produced = converter(
                    obj, width, height, obj_canvas_width, obj_canvas_height, frame_num,
                )
            except Exception as exc:
                print(
                    f"warning: could not convert Kitsu object {obj.get('id')!r} "
                    f"of type '{obj_type}' on frame {frame_num}: {exc}",
                    file=sys.stderr,
                )
                continue

            # A group converter yields one shape per child.
            shapes.extend(produced if isinstance(produced, list) else [produced])

    _debug_dump(debug_dir, "convert_kitsu_annotations", "output", shapes, call_id)
    return shapes