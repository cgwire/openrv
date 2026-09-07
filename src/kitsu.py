#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
import time as _time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


try:
    from PySide6 import QtCore, QtGui, QtWidgets
    import requests
    import rv
    import rv.rvtypes
    import rv.commands as rvc
    import rv.qtutils
    import gazu

    _INSIDE_OPENRV = True
except ImportError:
    _INSIDE_OPENRV = False


MS_PER_STROKE_POINT = 8

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

_RESOLUTION_RE = re.compile(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$")

_NO_COLOR_VALUES = {"none", "null", "transparent", ""}

_STILL_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff",
    "exr", "dpx", "tga", "webp",
}


def rv_normalized_to_pixel(
    nx: float, ny: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    """OpenRV -> Kitsu: RV paint-space normalized point -> Fabric.js canvas
    pixels.
    """
    m = max(width, height)
    px = (nx * (m / width) + 1.0) / 2.0 * canvas_width
    py = (1.0 - ny * (m / height)) / 2.0 * canvas_height
    return px, py


def pixel_to_rv_normalized(
    px: float, py: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    """Kitsu -> OpenRV: Fabric.js canvas pixels -> RV paint-space
    normalized point. Inverse of `rv_normalized_to_pixel`.
    """
    m = max(width, height)
    nx = (2.0 * px / canvas_width - 1.0) * (width / m)
    ny = (1.0 - 2.0 * py / canvas_height) * (height / m)
    return nx, ny


def rv_color_to_hex(color_rows: Sequence[Sequence[float]]) -> str:
    """OpenRV -> Kitsu: RV stores color as a list of [r, g, b, a] floats
    in 0..1. Kitsu's "stroke"/"fill" fields are plain hex strings, e.g.
    "#ff3860"."""
    if not color_rows:
        return "#ffffff"
    r, g, b, _a = color_rows[0]
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def rv_alpha(color_rows: Sequence[Sequence[float]]) -> float:
    """OpenRV -> Kitsu: pull the alpha channel out of an RV color row."""
    if not color_rows:
        return 1.0
    return color_rows[0][3]


def _is_hex_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value.strip()))


def _is_no_color(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NO_COLOR_VALUES)


def _has_color(value: Any) -> bool:
    """True if `value` is an actual color (rgba array or hex string), as
    opposed to a "none"/"transparent"/None sentinel."""
    if _is_no_color(value):
        return False
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return True
    return _is_hex_color(value)


def color_to_rv_color(value: Any, opacity: float = 1.0) -> List[List[int]]:
    """Kitsu -> OpenRV: a Kitsu/Fabric "stroke" or "fill" value -> RV's
    [[r, g, b, a]] color row, each channel an INTEGER 0..255 (this is
    RV's actual on-disk format). Inverse of `rv_color_to_hex`/`rv_alpha`.

    Accepts whatever form the real data hands back:
      * an rgba array, e.g. [255, 56, 96, 255] or [255, 56, 96] (3 or 4
        numbers, each already on a 0..255 scale) -- this is what real
        Kitsu annotation objects actually contain.
      * a "#rrggbb" hex string, kept for backwards compatibility.
      * Fabric.js/CSS "none"/"transparent"/None -> treated as opaque
        white (i.e. "no color set").

    `opacity` is Fabric's separate 0..1 "opacity" field, scaled to
    0..255 and used as the alpha channel ONLY when `value` doesn't
    already carry its own 4th (alpha) element.

    Falls back to opaque white (with a stderr warning) instead of
    raising if `value` doesn't match any recognized shape.
    """
    fallback_alpha = round(max(0.0, min(1.0, opacity)) * 255)

    if _is_no_color(value):
        return [255, 255, 255, fallback_alpha]

    if isinstance(value, (list, tuple)) and len(value) >= 3:
        r, g, b = (int(round(c)) for c in value[:3])
        a = int(round(value[3])) if len(value) >= 4 else fallback_alpha
        return [r, g, b, a]

    if _is_hex_color(value):
        h = value.strip().lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return [r, g, b, fallback_alpha]

    print(f"warning: unrecognized color value {value!r}, falling back to white", file=sys.stderr)
    return [255, 255, 255, fallback_alpha]


hex_to_rv_color = color_to_rv_color


def _fabric_base(
    obj_type: str,
    left: float, top: float, width: float, height: float,
    stroke_hex: str, stroke_width: float, opacity: float,
    author: str, canvas_width: float, canvas_height: float,
    source_uuid: Optional[str],
) -> Dict[str, Any]:
    """The Fabric.js boilerplate every converted object shares, regardless
    of shape type."""
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


def _pen_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    points_px = [
        rv_normalized_to_pixel(nx, ny, width, height, canvas_width, canvas_height)
        for nx, ny in props["points"]
    ]

    xs = [p[0] for p in points_px]
    ys = [p[1] for p in points_px]
    left, top = min(xs), min(ys)
    bbox_w, bbox_h = max(xs) - left, max(ys) - top

    widths = props.get("width", [])
    stroke_width_norm = widths[0] if widths else 0.01
    stroke_width_px = stroke_width_norm * canvas_height

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
    (sx, sy), (ex, ey) = props["startPos"][0], props["endPos"][0]
    x1, y1 = rv_normalized_to_pixel(sx, sy, width, height, canvas_width, canvas_height)
    x2, y2 = rv_normalized_to_pixel(ex, ey, width, height, canvas_width, canvas_height)

    left, top = min(x1, x2), min(y1, y2)
    bbox_w, bbox_h = abs(x2 - x1), abs(y2 - y1)
    stroke_width_px = props.get("borderWidth", 0.01) * canvas_height

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
    (minx, miny) = props["min"][0]
    (maxx, maxy) = props["max"][0]

    p0 = rv_normalized_to_pixel(minx, miny, width, height, canvas_width, canvas_height)
    p1 = rv_normalized_to_pixel(maxx, maxy, width, height, canvas_width, canvas_height)
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))

    left, top = x0, y0
    bbox_w, bbox_h = x1 - x0, y1 - y0
    rx, ry = bbox_w / 2.0, bbox_h / 2.0
    stroke_width_px = props.get("borderWidth", 0.01) * canvas_height

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
    inner_alpha = rv_alpha(props.get("innerColor", [[0, 0, 0, 0]]))
    obj["fill"] = rv_color_to_hex(props.get("innerColor", [])) if inner_alpha > 0 else None
    return obj


def _infer_canvas_size(
    kitsu_records: Sequence[Dict[str, Any]],
    fallback_width: float,
    fallback_height: float,
) -> Tuple[float, float]:
    """Best-effort recovery of the Fabric.js canvas size Kitsu is actually
    using for a preview's annotations, read off any existing object rather
    than assumed to match the raw video resolution (they can differ --
    see the module docstring). Falls back to (fallback_width,
    fallback_height) only if no existing object carries the field, e.g. a
    preview with no annotations yet."""
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
) -> List[Dict[str, Any]]:
    author = author or str(uuid.uuid4())
    canvas_width = canvas_width or float(width)
    canvas_height = canvas_height or float(height)

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
            objects.append(converter(
                shape, width, height, canvas_width, canvas_height,
                author, clock_ms,
            ))

        if not objects:
            continue

        records.append({
            "time": round((frame_num - frame_base) / fps * 1000),
            "frame": frame_num,
            "drawing": {"objects": objects},
        })

    return records


def _pen_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    stroke_points = obj.get("strokePoints", [])
    points_norm = [
        pixel_to_rv_normalized(p["x"], p["y"], width, height, canvas_width, canvas_height)
        for p in stroke_points
    ]

    stroke_width_px = obj.get("strokeWidth", 0.01 * canvas_height)
    stroke_width_norm = stroke_width_px / canvas_height

    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "points": points_norm,
            "width": [stroke_width_norm],
            "color": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
        },
    }


def _line_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    x1, y1 = obj.get("x1", obj["left"]), obj.get("y1", obj["top"])
    x2, y2 = obj.get("x2", obj["left"] + obj["width"]), obj.get("y2", obj["top"] + obj["height"])

    start = pixel_to_rv_normalized(x1, y1, width, height, canvas_width, canvas_height)
    end = pixel_to_rv_normalized(x2, y2, width, height, canvas_width, canvas_height)

    stroke_width_px = obj.get("strokeWidth", 0.01 * canvas_height)
    border_width_norm = stroke_width_px / canvas_height

    return {
        "type": "line",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "startPos": [start],
            "endPos": [end],
            "borderWidth": border_width_norm,
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
        },
    }


def _ellipse_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    left, top = obj["left"], obj["top"]
    bbox_w, bbox_h = obj["width"], obj["height"]

    c0 = pixel_to_rv_normalized(left, top, width, height, canvas_width, canvas_height)
    c1 = pixel_to_rv_normalized(left + bbox_w, top + bbox_h, width, height, canvas_width, canvas_height)

    min_x, max_x = sorted((c0[0], c1[0]))
    min_y, max_y = sorted((c0[1], c1[1]))

    stroke_width_px = obj.get("strokeWidth", 0.01 * canvas_height)
    border_width_norm = stroke_width_px / canvas_height

    fill = obj.get("fill")
    inner_color = color_to_rv_color(fill, 1.0) if _has_color(fill) else [[0, 0, 0, 0]]

    return {
        "type": "ellipse",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "min": [(min_x, min_y)],
            "max": [(max_x, max_y)],
            "borderWidth": border_width_norm,
            "borderColor": color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0)),
            "innerColor": inner_color,
        },
    }


_TYPE_CONVERTERS = {
    "PSStroke": _pen_from_fabric,
    "line": _line_from_fabric,
    "ellipse": _ellipse_from_fabric,
}


def convert_kitsu_annotations(
    kitsu_records: Sequence[Dict[str, Any]],
    width: int,
    height: int,
    canvas_width: Optional[float] = None,
    canvas_height: Optional[float] = None,
    frame_offset: int = 0,
    default_frame: int = 1,
) -> List[Dict[str, Any]]:

    default_canvas_width = canvas_width or float(width)
    default_canvas_height = canvas_height or float(height)

    shapes: List[Dict[str, Any]] = []

    for record in kitsu_records or []:
        raw_frame = record.get("frame")
        frame_num = (default_frame if raw_frame is None else int(raw_frame)) - frame_offset
        objects = record.get("drawing", {}).get("objects", [])

        for obj in objects:
            obj_type = obj.get("type")
            converter = _TYPE_CONVERTERS.get(obj_type)
            if converter is None:
                print(
                    f"warning: skipping unsupported Kitsu object type "
                    f"'{obj_type}' (id={obj.get('id')!r})",
                    file=sys.stderr,
                )
                continue

            obj_canvas_width = obj.get("canvasWidth", default_canvas_width)
            obj_canvas_height = obj.get("canvasHeight", default_canvas_height)

            shapes.append(converter(
                obj, width, height, obj_canvas_width, obj_canvas_height, frame_num,
            ))

    return shapes


def extract_authors(kitsu_records: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Convenience helper: map each object's Fabric "id" -> its Kitsu
    "createdBy" person id, since OpenRV has no field to carry that
    through. Useful if the caller wants to preserve authorship
    out-of-band alongside ``convert_kitsu_annotations``'s output."""
    authors: Dict[str, str] = {}
    for record in kitsu_records or []:
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("id") and obj.get("createdBy"):
                authors[obj["id"]] = obj["createdBy"]
    return authors


def _fnum(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return f"{n:.9f}".rstrip("0").rstrip(".") or "0"


def _flat(arr: Sequence[float]) -> str:
    return "[ " + " ".join(_fnum(n) for n in arr) + " ]"


def _nested(pairs: Sequence[Point]) -> str:
    return "[ " + " ".join(f"[ {_fnum(x)} {_fnum(y)} ]" for x, y in pairs) + " ]"


def _pen_block(pen: Dict[str, Any], pen_id: int, frame: int) -> Tuple[str, str]:
    name = f'"pen:{pen_id}:{frame}:Kitsu"'
    color = pen["color"]
    width = pen["width"]
    if isinstance(width, (int, float)):
        width = [width] * len(pen["points"])

    lines = [f"    {name}", "    {"]
    lines.append(f"        float[4] color = {_flat(color)}")
    lines.append(f"        float width = {_flat(width)}")
    lines.append(f'        string brush = "{pen.get("brush", "circle")}"')
    lines.append(f"        float[2] points = {_nested(pen['points'])}")
    lines.append(f"        int debug = {int(pen.get('debug', 0))}")
    lines.append(f"        int join = {int(pen.get('join', 3))}")
    lines.append(f"        int cap = {int(pen.get('cap', 1))}")
    lines.append(f"        int splat = {int(pen.get('splat', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


def _text_block(txt: Dict[str, Any], text_id: int, frame: int) -> Tuple[str, str]:
    name = f'"text:{text_id}:{frame}:Kitsu"'
    escaped = txt["text"].replace('"', '\\"').replace("\n", "\\n")

    lines = [f"    {name}", "    {"]
    lines.append(f"        float[2] position = {_flat(txt['position'])}")
    lines.append(f"        float[4] color = {_flat(txt.get('color', (1, 1, 1, 1)))}")
    lines.append(f"        float spacing = {_fnum(txt.get('spacing', 0.8))}")
    lines.append(f"        float size = {_fnum(txt.get('size', 0.05))}")
    lines.append(f"        float scale = {_fnum(txt.get('scale', 1))}")
    lines.append(f"        float rotation = {_fnum(txt.get('rotation', 0))}")
    lines.append('        string font = ""')
    lines.append(f'        string text = "{escaped}"')
    lines.append('        string origin = ""')
    lines.append(f"        int debug = {int(txt.get('debug', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


def build_paint_gto(paint_node_name: str, openrv_annotations: List[Dict[str, Any]]) -> Optional[str]:
    """
    openrv_annotations: list of {
        "frame": int,
        "pens":  [ {color, width, brush, points, join, cap, splat, debug}, ... ],
        "texts": [ {position, color, spacing, size, scale, rotation, text, debug}, ... ],
    }

    Returns the RVPaint GTO fragment as text, or None if there's nothing
    to write (no pens or texts on any frame).
    """
    blocks = []
    frame_order: Dict[int, List[str]] = {}
    next_id = 0

    for frame_data in openrv_annotations:
        frame = int(frame_data["frame"])
        for pen in frame_data.get("pens", []):
            block, cname = _pen_block(pen, next_id, frame)
            blocks.append(block)
            frame_order.setdefault(frame, []).append(cname)
            next_id += 1
        for txt in frame_data.get("texts", []):
            block, cname = _text_block(txt, next_id, frame)
            blocks.append(block)
            frame_order.setdefault(frame, []).append(cname)
            next_id += 1

    if not blocks:
        return None

    lines = ["GTOa (4)", ""]
    lines.append(f"{paint_node_name} : RVPaint (3)")
    lines.append("{")
    lines.append("    paint")
    lines.append("    {")
    lines.append(f"        int nextId = {next_id}")
    lines.append("        int nextAnnotationId = 0")
    lines.append("        int show = 1")
    lines.append("        string exclude = [ ]")
    lines.append("        string include = [ ]")
    lines.append("    }")
    lines.extend(blocks)
    for frame, names in sorted(frame_order.items()):
        order_str = " ".join(f'"{n}"' for n in names)
        lines.append(f'    "frame:{frame}"')
        lines.append("    {")
        lines.append(f"        string order = [ {order_str} ]")
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


_QWidgetBase = QtWidgets.QWidget if _INSIDE_OPENRV else object
_MinorModeBase = rv.rvtypes.MinorMode if _INSIDE_OPENRV else object


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


def _thumbnail_url(preview_file_id):
    """Best-effort Kitsu thumbnail URL for a preview file.

    Thumbnails are served from Kitsu's picture routes, which live at the
    server root rather than under "/api" (unlike the rest of the gazu
    API), so the "/api" suffix on the configured host has to be
    stripped first.
    """
    host = gazu.client.get_host()
    return f"{host}/pictures/originals/preview-files/{preview_file_id}.png"


def _is_still(preview_file):
    """True if this preview is a single picture rather than a movie.

    Kitsu records "extension" without a leading dot.
    """
    return str(preview_file.get("extension") or "").lower().lstrip(".") in _STILL_EXTENSIONS


def _dimensions_from_file(file_path):
    """Read pixel dimensions off a downloaded file, or None if that isn't
    possible (unsupported/unreadable format, or running outside OpenRV
    where Qt's image loaders aren't available)."""
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
            canvas_w, canvas_h = obj.get("canvasWidth"), obj.get("canvasHeight")
            if canvas_w and canvas_h:
                return int(round(float(canvas_w))), int(round(float(canvas_h)))

    raise ValueError(
        f"Could not determine the pixel dimensions of preview file "
        f"{preview_file.get('id')!r} (extension "
        f"{preview_file.get('extension')!r})."
    )


class KitsuReviewPanel(_QWidgetBase):
    """Main widget for the Kitsu Review plugin.

    Intended to be embedded as a dock widget below RV's review
    viewport (see KitsuReviewMode), not shown as its own top-level
    window.
    """

    THUMBNAIL_SIZE = (96, 54)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.logged_in = False
        self.current_user = None
        self.current_revision = None
        self.revisions = []
        self._thumbnail_cache: Dict[str, Any] = {}

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

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Thumbnail", "Shot", "Task", "Rev", "Status", "Date"]
        )
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
            self._thumbnail_cache.clear()
            self._clear_detail_panel()

    def _on_login_clicked(self):
        if self.logged_in:
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

    def _fetch_thumbnail_pixmap(self, preview_file_id):
        """Fetch (and cache) the QPixmap for a preview file's thumbnail.

        Kitsu's thumbnail route needs the same auth as the rest of the
        API, so this goes through `requests` directly with gazu's own
        auth header rather than e.g. Qt's network stack, which wouldn't
        carry the session token. Returns None (and caches that) if the
        thumbnail can't be fetched -- a preview with no rendered
        thumbnail yet is an expected, non-fatal case.
        """
        if not preview_file_id:
            return None
        if preview_file_id in self._thumbnail_cache:
            return self._thumbnail_cache[preview_file_id]

        pixmap = None
        try:
            url = _thumbnail_url(preview_file_id)
            headers = gazu.client.make_auth_header()
            response = requests.get(url, headers=headers, timeout=10)
            if response.ok:
                image = QtGui.QImage()
                if image.loadFromData(response.content):
                    pixmap = QtGui.QPixmap.fromImage(image)
        except Exception as exc:
            print(f"[KitsuReview] Could not fetch thumbnail for preview "
                  f"{preview_file_id}: {exc}")

        self._thumbnail_cache[preview_file_id] = pixmap
        return pixmap

    def _make_thumbnail_widget(self, preview_file_id):
        label = QtWidgets.QLabel()
        label.setAlignment(QtCore.Qt.AlignCenter)

        pixmap = self._fetch_thumbnail_pixmap(preview_file_id)
        if pixmap and not pixmap.isNull():
            tw, th = self.THUMBNAIL_SIZE
            label.setPixmap(
                pixmap.scaled(tw, th, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            )
        else:
            label.setText("(no thumbnail)")
            label.setStyleSheet("color: #888; font-style: italic;")
        return label

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
                "width": None,
                "height": None,
                "base_frame": 1,
            })

        self.revisions = revisions

        self.table.setRowCount(0)
        for row, rev in enumerate(self.revisions):
            self.table.insertRow(row)
            self.table.setRowHeight(row, self.THUMBNAIL_SIZE[1] + 10)

            thumb_widget = self._make_thumbnail_widget(rev["preview_file"].get("id"))
            self.table.setCellWidget(row, 0, thumb_widget)

            values = [
                rev["shot"], rev["task_type"], f"v{rev['revision']:03d}",
                rev["status"], rev["date"],
            ]
            for offset, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                self.table.setItem(row, offset + 1, item)

        QtWidgets.QApplication.restoreOverrideCursor()

        self.table.setColumnWidth(0, self.THUMBNAIL_SIZE[0] + 8)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, self.THUMBNAIL_SIZE[0] + 8)
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

        for comment in reversed(comments):
            author = _person_display_name(comment.get("person"))
            date = _format_date(comment.get("created_at"))
            text = comment.get("text") or ""
            self.comments_list.addItem(f"[{date}] {author}: {text}")

    def _set_prop(self, full_name, ptype, width, values):
        if not rvc.propertyExists(full_name):
            rvc.newProperty(full_name, ptype, width)
        if ptype == rvc.FloatType:
            rvc.setFloatProperty(full_name, values, True)
        elif ptype == rvc.IntType:
            rvc.setIntProperty(full_name, values, True)
        elif ptype == rvc.StringType:
            rvc.setStringProperty(full_name, values, True)

    def apply_annotations_live(self, paint_node, openrv_annotations):
        """openrv_annotations: flat list of shapes as returned by
        convert_kitsu_annotations(), i.e. [{"type", "frame", "properties"}, ...] --
        NOT the {"frame", "pens", "texts"} grouping build_paint_gto() uses."""
        self._set_prop(f"{paint_node}.paint.show", rvc.IntType, 1, [1])

        next_id = 0
        frame_order: Dict[int, List[str]] = {}

        for shape in openrv_annotations:
            if shape.get("type") != "pen":
                print(f"[KitsuReview] Live-apply: skipping unsupported shape "
                    f"type {shape.get('type')!r}", file=sys.stderr)
                continue

            frame = int(shape["frame"])
            props = shape["properties"]
            points = props["points"]

            color = props.get("color") or [255, 255, 255, 255]
            if isinstance(color[0], (list, tuple)):
                color = color[0]
            color_float = [c / 255.0 for c in color]

            width = props.get("width", [0.003])
            if not isinstance(width, list):
                width = [width]
            if len(width) != len(points):
                width = [width[0]] * len(points)

            cname = f"pen:{next_id}:{frame}:Kitsu"
            base = f"{paint_node}.{cname}"

            self._set_prop(f"{base}.color", rvc.FloatType, 4, color_float)
            self._set_prop(f"{base}.width", rvc.FloatType, 1, width)
            self._set_prop(f"{base}.brush", rvc.StringType, 1, ["circle"])
            self._set_prop(f"{base}.points", rvc.FloatType, 2,
                    [c for xy in points for c in xy])
            self._set_prop(f"{base}.debug", rvc.IntType, 1, [0])
            self._set_prop(f"{base}.join", rvc.IntType, 1, [3])
            self._set_prop(f"{base}.cap", rvc.IntType, 1, [1])
            self._set_prop(f"{base}.splat", rvc.IntType, 1, [0])

            frame_order.setdefault(frame, []).append(cname)
            next_id += 1

        for frame, names in frame_order.items():
            self._set_prop(f"{paint_node}.frame:{frame}.order", rvc.StringType, 1, names)

        rvc.redraw()

    def _source_start_frame(self, source_node, default=1):
        """First frame of a source in the current session. A still image
        occupies exactly one frame, which is where Kitsu's frameless
        annotations have to land."""
        try:
            info = rvc.nodeRangeInfo(source_node) or {}
        except Exception as exc:
            print(f"[KitsuReview] Could not query frame range for {source_node}: {exc}")
            return default
        start = info.get("start") if isinstance(info, dict) else None
        try:
            return int(start)
        except (TypeError, ValueError):
            return default

    def _on_download_clicked(self):
        """Download the selected revision's preview file from Kitsu and
        load it into the current RV session, then re-apply any annotations
        Kitsu already has for it."""
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
            pct = int(min(100, max(0, (size / total_size) * 100))) if total_size else 0
            progress.setValue(pct)
            QtWidgets.QApplication.processEvents()

        try:
            gazu.files.download_preview_file(
                preview_file, file_path, progress_callback=_progress_callback
            )
        except TypeError:
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
            source_node = rvc.addSourceVerbose([file_path])
        except Exception as exc:
            print(f"[KitsuReview] Skipped adding source to RV session: {exc}")
            source_node = None

        is_still = _is_still(preview_file)
        base_frame = self._source_start_frame(source_node) if (source_node and is_still) else 1
        rev["base_frame"] = base_frame

        try:
            width, height = _preview_dimensions(preview_file, rev.get("entity"), file_path)
        except ValueError as exc:
            rev["width"] = rev["height"] = None
            openrv_annotations = []
            print(f"[KitsuReview] {exc} Existing annotations will not be loaded.")
            QtWidgets.QMessageBox.warning(
                self, "Kitsu",
                "Could not determine this preview's pixel dimensions, so its "
                "existing annotations were skipped.\n\nThe media itself has "
                "still been loaded into RV."
            )
        else:
            rev["width"], rev["height"] = width, height
            openrv_annotations = convert_kitsu_annotations(
                preview_file.get("annotations") or [],
                width=width,
                height=height,
                canvas_width=None,
                canvas_height=None,
                frame_offset=0,
                default_frame=base_frame,
            )
            if is_still:
                for shape in openrv_annotations:
                    shape["frame"] = base_frame

        if source_node and openrv_annotations:
            group_name = rvc.nodeGroup(source_node)
            paint_node_name = f"{group_name}_paint"
            self.apply_annotations_live(paint_node_name, openrv_annotations)

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
                    continue

                for item_name in order:
                    kind = item_name.split(":")[0] if ":" in item_name else item_name
                    item_prefix = f"{node}.{item_name}."

                    properties = {}
                    for p in all_props:
                        if not p.startswith(item_prefix):
                            continue
                        attr = p[len(item_prefix):]
                        value = self._get_rv_property_value(p)

                        group_size = self._GROUPED_KEYS.get(attr)
                        if group_size and isinstance(value, list) and len(value) % group_size == 0 and len(value) > 0:
                            value = [list(value[i:i + group_size]) for i in range(0, len(value), group_size)]
                        elif isinstance(value, list) and len(value) == 1:
                            value = value[0]

                        properties[attr] = value

                    annotations.append({
                        "frame": frame,
                        "node": node,
                        "name": item_name,
                        "type": kind,
                        "properties": properties,
                    })

        annotations.sort(key=lambda a: a["frame"])

        return annotations

    def _push_to_kitsu(self, preview_file, additions, updates, deletions):
        return gazu.files.update_preview_annotations(
            preview_file,
            additions=additions,
            updates=updates,
            deletions=deletions,
        )

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

        self._reload_comments()

    def _on_export_clicked(self):
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

        annotations = self._gather_rv_annotations()
        annotated_frames = sorted({a["frame"] for a in annotations})
        if annotated_frames:
            frames_note = f"{len(annotated_frames)} annotated frame(s): {annotated_frames}"
        else:
            frames_note = "0 annotated frames"

        width, height = rev.get("width"), rev.get("height")
        if not (width and height):
            try:
                width, height = _preview_dimensions(preview_file, rev.get("entity"))
            except ValueError as exc:
                QtWidgets.QMessageBox.critical(
                    self, "Kitsu",
                    f"Cannot export annotations: {exc}\n\n"
                    "Download the revision first so its size can be read from "
                    "the file."
                )
                return
            rev["width"], rev["height"] = width, height

        canvas_width, canvas_height = _infer_canvas_size(
            preview_file.get("annotations"), width, height,
        )

        base_frame = rev.get("base_frame") or 1
        if _is_still(preview_file) and annotated_frames:
            base_frame = annotated_frames[0]

        records = convert_openrv_annotations(
            annotations,
            width=width,
            height=height,
            fps=24.0,
            author=preview_file.get("person_id"),
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            frame_base=base_frame,
        )

        try:
            self._push_to_kitsu(preview_file["id"], records, [], [])
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Kitsu", f"Annotation export failed: {exc}")
            return

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task_type']}\n"
            f"Revision: v{rev['revision']:03d}\n"
            f"Comments on Kitsu: {n_comments}\n"
            f"Annotations exported: {frames_note}"
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


def _main() -> None:
    """CLI entry point: convert a Kitsu preview-annotation JSON dump into
    OpenRV paint shapes. Only exercises `convert_kitsu_annotations`, so it
    works without OpenRV, PySide6, or gazu installed."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert Kitsu preview-annotation JSON into OpenRV paint shapes."
    )
    parser.add_argument("kitsu_json", help="Path to a JSON file containing the Kitsu annotation records list")
    parser.add_argument("--width", type=int, required=True, help="Source video/image width in px")
    parser.add_argument("--height", type=int, required=True, help="Source video/image height in px")
    parser.add_argument("--canvas-width", type=float, default=None, help="Fabric.js canvas width, if different from --width")
    parser.add_argument("--canvas-height", type=float, default=None, help="Fabric.js canvas height, if different from --height")
    parser.add_argument("--frame-offset", type=int, default=0, help="Subtracted from each Kitsu frame number")
    parser.add_argument("--default-frame", type=int, default=1, help="Frame to use for records with no frame field (still previews)")
    parser.add_argument("-o", "--output", default=None, help="Where to write the OpenRV shapes JSON (default: stdout)")
    args = parser.parse_args()

    with open(args.kitsu_json) as f:
        records = json.load(f)

    shapes = convert_kitsu_annotations(
        records,
        width=args.width,
        height=args.height,
        canvas_width=args.canvas_width,
        canvas_height=args.canvas_height,
        frame_offset=args.frame_offset,
        default_frame=args.default_frame,
    )

    output = json.dumps(shapes, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        print(output)


if __name__ == "__main__":
    _main()