#!/usr/bin/env python3
"""
Kitsu <-> OpenRV review bridge
==============================

Single-file combination of what used to be four separate modules:

    * map_annotations.py    -- OpenRV paint shapes  --> Kitsu Fabric.js JSON
    * kitsu_to_openrv.py    -- Kitsu Fabric.js JSON  --> OpenRV paint shapes
    * openrv_paint_gto.py   -- OpenRV paint shapes   --> RVPaint ".gto" text
    * kitsu.py              -- the OpenRV "Kitsu Review" dock-panel plugin

They're kept together because the plugin (`KitsuReviewPanel`, at the
bottom of this file) is the only real consumer of the conversion
helpers, and previously had to import across three sibling modules that
only ever made sense as a set.

--------------------------------------------------------------------------
What this file does
--------------------------------------------------------------------------
Inside OpenRV, `createMode()` docks a "Kitsu Review" panel that lets an
artist:

    1. Log in to Kitsu (real gazu login)
    2. Browse their assigned tasks and pick a revision (preview file)
    3. Download that revision and load it into the RV session
    4. Annotate the frame using RV's own Paint tools
    5. Post review comments back to Kitsu
    6. Export annotations + a comment-count summary back to Kitsu

Outside OpenRV, the conversion functions (`convert_openrv_annotations`,
`convert_kitsu_annotations`, `build_paint_gto`) are plain, dependency-
free functions you can import and test on their own -- see "Running
standalone" below. PySide6 / rv / gazu are only imported by the plugin
classes, and only if they're actually available.

--------------------------------------------------------------------------
Coordinate systems
--------------------------------------------------------------------------
OpenRV paint annotations store points in RV's normalized "paint" space:

    * origin (0, 0) is the CENTER of the image
    * Y is UP, spans roughly [-1, 1] for the full frame height
    * X is scaled by the image aspect ratio, spans [-aspect, aspect]
      where aspect = width / height

Kitsu/Fabric.js works in plain PIXEL space relative to the annotation
canvas:

    * origin (0, 0) is the TOP-LEFT corner
    * X grows right, Y grows DOWN
    * "canvasWidth" / "canvasHeight" define the pixel space that points
      and left/top/width/height are expressed in (this can differ
      slightly from the actual video resolution)

    OpenRV -> Kitsu:  px = (nx / aspect + 1) / 2 * canvas_width
                      py = (1 - ny) / 2 * canvas_height
    Kitsu -> OpenRV:  nx = aspect * (2 * px / canvas_width - 1)
                      ny = 1 - 2 * py / canvas_height

--------------------------------------------------------------------------
Round-tripping notes
--------------------------------------------------------------------------
* Pure Fabric.js/CSS boilerplate (angle, flipX/Y, skewX/Y, scaleX/Y,
  version, shadow, erasable, fillRule, paintFirst, strokeLineCap/Join,
  strokeUniform, strokeDashArray/Offset, strokeMiterLimit,
  globalCompositeOperation, backgroundColor, ...) has no OpenRV
  equivalent in either direction and is simply discarded.
* "startTime"/"endTime" on a PSStroke are wall-clock telemetry of when
  the artist drew it -- cosmetic, not structural. Going OpenRV->Kitsu we
  synthesize monotonically increasing values; going Kitsu->OpenRV we
  drop them.
* "createdBy" (a Kitsu person id) has no OpenRV field. Going
  OpenRV->Kitsu every shape gets the same `author`; going Kitsu->OpenRV
  it's dropped from the shape but still available via `extract_authors`.
* Fabric's "id" round-trips as OpenRV's "uuid" property, so shape
  identity survives a full OpenRV -> Kitsu -> OpenRV trip.
* Kitsu color arrays ("color"/"borderColor"/"innerColor") are
  [r, g, b, a] with each channel an INTEGER 0..255 (confirmed against
  real RV round-tripping); RV's own color rows are FLOATS 0..1.
* Only "PSStroke" (freehand pen) has a confirmed real Kitsu sample.
  "line" and "ellipse" are mapped onto Fabric.js's native "line" /
  "ellipse" object types -- if your Kitsu deployment uses custom
  "PSLine" / "PSEllipse" subclasses instead, the `_line_*` /
  `_ellipse_*` converters below are intentionally isolated so you can
  adjust them without touching anything else.

--------------------------------------------------------------------------
Running standalone
--------------------------------------------------------------------------
    python3 kitsu.py annotations.json --width 1920 --height 1080

reads a JSON file of Kitsu per-frame annotation records and prints the
equivalent OpenRV paint shapes. This works without OpenRV, PySide6, or
gazu installed -- see "OpenRV-only dependencies" below.
"""

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


# ----------------------------------------------------------------------------
# OpenRV-only dependencies
# ----------------------------------------------------------------------------
# `rv` / `rv.commands` / `rv.qtutils` only exist inside OpenRV's embedded
# Python interpreter, and `gazu` requires `pip install gazu` in that same
# environment. They're imported once, defensively, so that everything above
# the "OpenRV plugin" section (the conversion helpers and the CLI at the
# bottom of this file) stays importable and testable without either.
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


# ----------------------------------------------------------------------------
# Shared constants
# ----------------------------------------------------------------------------

MS_PER_STROKE_POINT = 8  # rough authoring-speed estimate for PSStroke startTime/endTime

# Where downloaded preview files are written before being loaded into RV.
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# Fabric.js/CSS spellings that mean "no color here" rather than an actual
# hex value. Kitsu (and hand-edited/older annotation data) can hand back
# any of these for "stroke" or "fill" instead of a real "#rrggbb" string.
_NO_COLOR_VALUES = {"none", "null", "transparent", ""}


# ============================================================================
# 1. Coordinate & color conversion
# ============================================================================
# `rv_normalized_to_pixel` / `pixel_to_rv_normalized` and their color
# counterparts below are exact inverses of each other, so they're kept side
# by side here instead of split across two files.

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


# kept as an alias -- some callers/older code may still import this name
hex_to_rv_color = color_to_rv_color


# ============================================================================
# 2. OpenRV -> Kitsu  (paint shapes -> Fabric.js annotation records)
# ============================================================================
# Kitsu's annotation tool is built on Fabric.js. Annotations for a preview
# file are stored as a list of *per-frame* records:
#
#     [
#       {
#         "time": 0,          # this frame's offset into the timeline, in ms
#         "frame": 1,
#         "drawing": {
#           "objects": [ <fabric-style object>, <fabric-style object>, ... ]
#         }
#       },
#       ...
#     ]
#
# Each object inside "drawing.objects" is a serialized Fabric.js object.
# For freehand pen strokes Kitsu uses a custom subclass called "PSStroke"
# (Paint-Stroke), e.g.:
#
#     {
#       "id": "...", "type": "PSStroke",
#       "left": <bbox left px>, "top": <bbox top px>,
#       "width": <bbox width px>, "height": <bbox height px>,
#       "stroke": "#ff3860", "strokeWidth": 20, "opacity": 1,
#       "canvasWidth": 1697.77, "canvasHeight": 955,
#       "strokePoints": [{"x":.., "y":.., "type":"PSPoint", "pressure":1}, ...],
#       "createdBy": "<user id>",
#       "startTime": <ms>, "endTime": <ms>,
#       ... (Fabric.js boilerplate -- see the module docstring)
#     }
#
# Only "PSStroke" has a confirmed real sample; "line" and "ellipse" below
# are mapped onto Fabric.js's own native object types of the same name.
#
# "time" (per frame record) is meaningful -- Kitsu uses it to scrub/seek:
# (frame - frame_base) / fps * 1000. "startTime"/"endTime" (per PSStroke)
# are cosmetic wall-clock telemetry; since OpenRV doesn't record them, we
# synthesize monotonically increasing values rather than inventing fake
# "real" timestamps.

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
    clock_ms[0] = end_time  # advance the shared clock so strokes don't overlap

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
    # NOTE: no confirmed Kitsu sample for lines. Mapped onto Fabric.js's
    # native "line" object type (x1/y1/x2/y2 + the same stroke styling
    # PSStroke uses). Adjust here if your Kitsu build uses a custom
    # "PSLine" type instead.
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
    # NOTE: no confirmed Kitsu sample for ellipses either. Mapped onto
    # Fabric.js's native "ellipse" object type (rx/ry + left/top/width/
    # height bbox). Adjust here if your Kitsu build uses a custom
    # "PSEllipse" type instead.
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
    for record in kitsu_records:
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
    """Convert raw OpenRV annotation shapes into a list of Kitsu per-frame
    annotation records, ready for the ``additions`` argument of
    ``gazu.files.update_preview_annotations``.

    Args:
        openrv_shapes: parsed OpenRV/RV paint-annotation shapes (e.g. the
            result of ``json.load()`` on an RV annotation export).
        width: source video/image width in pixels (defines the aspect
            ratio RV normalized its coordinates against).
        height: source video/image height in pixels.
        fps: playback fps, used to compute the "time" field.
        author: Kitsu person ID to record as "createdBy" on each stroke.
            Defaults to a freshly generated UUID if not provided.
        canvas_width: Kitsu annotation canvas width, if different from
            `width`. Defaults to `width`.
        canvas_height: Kitsu annotation canvas height, if different from
            `height`. Defaults to `height`.
        frame_offset: added to every OpenRV frame number before
            grouping/sending (use if RV frame numbering != Kitsu frame
            numbering).
        frame_base: the OpenRV frame number that corresponds to Kitsu
            "time": 0 (i.e. the first frame of the shot/clip on Kitsu's
            timeline).
        skip_soft_deleted: if True (default), shapes with
            softDeleted=1 are dropped; pass False to keep them.
    """
    author = author or str(uuid.uuid4())
    canvas_width = canvas_width or float(width)
    canvas_height = canvas_height or float(height)

    print(canvas_width)
    print(canvas_height)
    print(width)
    print(height)

    # group OpenRV shapes by (converted) frame number, preserving order
    by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for shape in openrv_shapes:
        if skip_soft_deleted and shape.get("properties", {}).get("softDeleted"):
            continue
        frame_num = shape["frame"] + frame_offset
        by_frame.setdefault(frame_num, []).append(shape)

    clock_ms = [int(_time.time() * 1000)]  # mutable shared "wall clock"
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


# ============================================================================
# 3. Kitsu -> OpenRV  (Fabric.js annotation records -> paint shapes)
# ============================================================================
# This is a best-effort inverse of section 2. A few notes specific to this
# direction (see the module docstring's "Round-tripping notes" for the
# shared ones):
#
# * The ellipse's "min"/"max" bookkeeping only ever stored an axis-aligned
#   bounding box in OpenRV, so reconstructing it from the Fabric bbox
#   (left/top/width/height) round-trips exactly -- there's no information
#   about which literal corner was "min" vs "max" to lose in the first
#   place.

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
    # NOTE: mirrors _line_to_fabric -- assumes the native Fabric.js "line"
    # type (x1/y1/x2/y2). If your Kitsu deployment emits a custom
    # "PSLine" subclass instead, adjust the field lookups here.
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
    # NOTE: mirrors _ellipse_to_fabric -- assumes the native Fabric.js
    # "ellipse" type (left/top/width/height bbox). Adjust here if your
    # Kitsu deployment uses a custom "PSEllipse" subclass instead.
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
) -> List[Dict[str, Any]]:
    """Convert Kitsu per-frame preview annotation records back into a
    flat list of OpenRV/RV paint-annotation shapes.

    Args:
        kitsu_records: the list of ``{"time", "frame", "drawing":
            {"objects": [...]}}`` records, e.g. as returned by
            ``gazu.files.get_preview_file_annotations`` or read back
            from a preview file's annotations field.
        width: source video/image width in pixels (the aspect ratio RV
            normalizes coordinates against). Should match whatever was
            passed to ``convert_openrv_annotations`` originally.
        height: source video/image height in pixels.
        canvas_width: the Fabric.js annotation canvas width the
            records' pixel coordinates are expressed in. Defaults to
            `width` (pass the actual value if it differs, e.g. Kitsu's
            own canvasWidth on the objects, which takes precedence
            per-object when present).
        canvas_height: same as `canvas_width`, for height.
        frame_offset: subtracted from each Kitsu "frame" number to
            recover the original OpenRV frame numbering (inverse of the
            `frame_offset` passed to ``convert_openrv_annotations``).

    Returns:
        A flat list of OpenRV shape dicts (``{"type", "frame",
        "properties"}``), sorted by frame then by original object order
        within each frame.
    """
    default_canvas_width = canvas_width or float(width)
    default_canvas_height = canvas_height or float(height)

    shapes: List[Dict[str, Any]] = []

    for record in kitsu_records:
        frame_num = record["frame"] - frame_offset
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

            # per-object canvas size takes precedence if Kitsu recorded
            # one (it can differ slightly from the nominal video res).
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
    for record in kitsu_records:
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("id") and obj.get("createdBy"):
                authors[obj["id"]] = obj["createdBy"]
    return authors


# ============================================================================
# 4. OpenRV RVPaint GTO serialization
# ============================================================================
# Serializes converted Kitsu annotations (the same {"frame", "pens",
# "texts"} structure `KitsuReviewPanel.apply_annotations_live` below
# consumes) into a standalone RVPaint GTO text fragment -- an alternative
# to poking RV's live property API when you just want the .gto text (e.g.
# to write directly into a session file).

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
    color = pen["color"]              # (r, g, b, a)
    width = pen["width"]              # list, one per point (or a single float)
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


# ============================================================================
# 5. OpenRV plugin: "Kitsu Review" dock panel
# ============================================================================
# Login, task listing, preview downloading, and comments use the real
# Kitsu Python SDK (`gazu`):
#   - gazu.log_in / gazu.log_out                          -- authentication
#   - gazu.task.all_tasks_for_person(person)               -- tasks assigned to the user
#   - gazu.entity.get_entity(entity_id)                    -- shot/asset info for a task
#   - gazu.files.get_all_preview_files_for_task(task)      -- revisions (preview files) for a task
#   - gazu.files.download_preview_file(preview_file, path) -- actual media download
#   - gazu.task.all_comments_for_task(task)                -- comment history for a task
#   - gazu.task.get_task_status(task_status_id)            -- resolve a task's current status
#   - gazu.task.add_comment(task, task_status, comment=...) -- post a new comment
#
# Annotation export (the "Annotations exported" part of `_on_export_clicked`)
# is still simulated -- the RV-side node parsing in `_gather_rv_annotations`
# uses the real RV command API where possible, but the exact per-frame paint
# property paths can vary between RV versions/builds -- double check those
# against the RV build you are targeting before shipping. Only the comment
# count in the export summary is backed by real data; comments themselves
# are posted to Kitsu immediately when added (`_on_add_comment_clicked`)
# rather than being batched up for export.
#
# Make sure `gazu` is installed in RV's Python environment: pip install gazu

# Base classes fall back to `object` when PySide6/rv aren't available, so
# these classes stay *importable* (e.g. for tooling or static analysis)
# even outside OpenRV. Actually instantiating them still requires the real
# environment -- see `createMode` at the end of this section.
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


class KitsuReviewPanel(_QWidgetBase):
    """Main widget for the Kitsu Review plugin.

    Intended to be embedded as a dock widget below RV's review
    viewport (see KitsuReviewMode), not shown as its own top-level
    window.
    """

    THUMBNAIL_SIZE = (96, 54)  # roughly 16:9, matches typical shot plates

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
                # Nothing has been published for this task yet -- skip it,
                # there's no revision to review.
                continue

            latest_preview = max(previews, key=_preview_revision)

            print('---')
            print(task)
            print(entity)

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

        # Kitsu typically returns comments newest-first; show oldest-first
        # so the conversation reads top to bottom.
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
                # "line"/"ellipse" have no live-property group wired up yet.
                print(f"[KitsuReview] Live-apply: skipping unsupported shape "
                    f"type {shape.get('type')!r}", file=sys.stderr)
                continue

            frame = int(shape["frame"])
            props = shape["properties"]
            points = props["points"]

            color = props.get("color") or [255, 255, 255, 255]
            if isinstance(color[0], (list, tuple)):     # tolerate nested [[r,g,b,a]] too
                color = color[0]
            color_float = [c / 255.0 for c in color]     # RV wants 0..1 floats here

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

    def _on_download_clicked(self):
        """Download the selected revision's preview file from Kitsu and
        load it into the current RV session."""
        if not self.current_revision:
            return
        rev = self.current_revision
        preview_file = rev["preview_file"]
        kitsu_annotations = preview_file["annotations"]

        openrv_annotations = convert_kitsu_annotations(
            kitsu_annotations,
            width=rev["preview_file"]["width"],
            height=rev["preview_file"]["height"],
            canvas_width=None,
            canvas_height=None,
            frame_offset=0,
        )

        # print(openrv_annotations)

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
            source_node = rvc.addSourceVerbose([file_path])
        except Exception as exc:
            print(f"[KitsuReview] Skipped adding source to RV session: {exc}")
            source_node = None

        if source_node and openrv_annotations:
            group_name = rvc.nodeGroup(source_node)
            paint_node_name = f"{group_name}_paint"
            # print("paint node:", paint_node_name, "exists:", paint_node_name in rvc.nodesOfType("RVPaint"))
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

    # known vector-valued properties -> number of components per vector
    _GROUPED_KEYS = {
        "points": 2,       # (x, y)
        "color": 4,        # (r, g, b, a) - may be int or float
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
                    continue  # empty -> no strokes/text on this frame

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
                            # flat [a0,b0,c0,...,a1,b1,c1,...] -> [[a0,b0,c0,...], [a1,b1,c1,...], ...]
                            value = [list(value[i:i + group_size]) for i in range(0, len(value), group_size)]
                        elif isinstance(value, list) and len(value) == 1:
                            # unwrap true scalars (debug, join, cap, startFrame, uuid, brush, borderWidth, ...)
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

        canvas_width, canvas_height = _infer_canvas_size(
            rev["preview_file"].get("annotations") or [],
            rev["preview_file"]["width"],
            rev["preview_file"]["height"],
        )

        records = convert_openrv_annotations(
            annotations,
            width=rev["preview_file"]["width"],
            height=rev["preview_file"]["height"],
            fps=24.0,
            author=rev["preview_file"]["person_id"],
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )

        self._push_to_kitsu(rev["preview_file"]["id"], records, [], [])

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


# ============================================================================
# 6. Standalone CLI: Kitsu annotation JSON -> OpenRV shapes
# ============================================================================

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
    )

    output = json.dumps(shapes, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        print(output)


if __name__ == "__main__":
    _main()