#!/usr/bin/env python3
"""Kitsu <-> OpenRV annotation conversion.

Pure Python with no OpenRV, Qt or gazu dependency, so it can be unit
tested and run standalone (see the CLI at the bottom).

Public API:
    convertToKitsu(openrv_shapes, width, height, ...) -> Kitsu annotation records
    convertToORV(kitsu_records, width, height, ...)   -> OpenRV paint shapes
    split_annotation_changes(records, reference)      -> (additions, updates)

Erasers work differently on the two sides. In RV an eraser is a pen with
mode 1 that clears everything drawn before it on its frame. In Kitsu
(Fabric.js) an eraser stroke is stored on each object it erased, as a
path inside that object's "eraser" group, and later objects aren't
affected. The converters translate between the two (see "Erasers").
"""

from __future__ import annotations

import copy
import datetime as _dt
import heapq
import json
import math
import re
import sys
import time as _time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "convertToKitsu", "convertToORV", "extract_authors", "build_paint_gto",
    "shapes_to_paint_gto", "frame_to_time", "time_to_frame",
    "frame_to_time_ms", "time_ms_to_frame", "split_annotation_changes",
    "DEFAULT_FPS",
]

Point = Tuple[float, float]

MS_PER_STROKE_POINT = 8

# Fabric.js version Kitsu currently writes into annotation objects.
FABRIC_VERSION = "7.4.0"

# How close (canvas px) a converted stroke must be to an existing Kitsu
# stroke to be treated as the same, unchanged stroke.
MATCH_TOLERANCE_PX = 0.05

# Kitsu draws ellipses as Fabric.js "circle" objects, stretched with
# scaleX/scaleY when they aren't round. Fabric's own "ellipse" class is
# read as well.
KITSU_ELLIPSE_TYPES = ("circle", "ellipse")

# Kitsu rectangles are Fabric.js "rect" objects. Fabric's rx/ry on a rect
# are corner radii, which RV rects don't have.
KITSU_RECT_TYPES = ("rect",)

# Kitsu arrows are a Fabric.js Line subclass ("arrow"): x1/y1 is the tail,
# x2/y2 the head, plus a head size/width in canvas px. RV arrows have no
# separate head size (it follows the line thickness), so arrows coming
# from RV get the head Kitsu draws by default.
KITSU_ARROW_HEAD_SIZE = 15
KITSU_ARROW_HEAD_WIDTH = 12

# Kitsu text is a Fabric.js "i-text" ("text" and "textbox" are read too).
# Its color is the "fill", its fontSize is in canvas px, and Kitsu always
# draws it in Arial on a translucent white background.
KITSU_TEXT_TYPES = ("i-text", "text", "textbox")
KITSU_TEXT_FONT_FAMILY = "arial"
KITSU_TEXT_LINE_HEIGHT = 1.16
KITSU_TEXT_BACKGROUND = "rgba(255,255,255, 0.8)"

# Fabric.js text metrics (FabricText._fontSizeMult / _fontSizeFraction): a
# line is fontSize * 1.13 tall, and its baseline sits 22.2% of that height
# above the line's bottom.
FABRIC_FONT_SIZE_MULT = 1.13
FABRIC_FONT_SIZE_FRACTION = 0.222

# Rough Arial advance per character, in em. Only used for the box width
# stored on text coming from RV; Fabric re-measures the text when Kitsu
# loads it, and the width doesn't affect where the text is drawn.
_ARIAL_AVG_ADVANCE_EM = 0.55

# RV annotate mode also writes a legacy "size" for pre-QFont RV builds,
# which draw text size * scale * 12.5 tall. Current RV reads "fontSize".
RV_LEGACY_TEXT_SIZE_FACTOR = 12.5

# RV's annotate mode keeps an editing cursor (0x01) at the end of a text
# while it's being typed; it and other control characters aren't content.
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

_NO_COLOR_VALUES = {"none", "null", "transparent", ""}

# Fully transparent RV color, in the flat 0..255 form `convertToORV` emits.
_RV_NO_COLOR = [255, 255, 255, 0]

# RV pen "mode": 0 paints, 1 erases what was drawn before it on the frame.
RV_PEN_MODE_PAINT = 0
RV_PEN_MODE_ERASE = 1

# RV's own eraser pens are white; the color isn't drawn.
_RV_ERASER_COLOR = [255, 255, 255, 255]

# Kitsu erasers: a Fabric "eraser" group on the erased object, holding one
# "path" per eraser stroke, drawn with "destination-out" so it cuts the
# object. Kitsu's eraser brush paints them black.
KITSU_ERASER_TYPE = "eraser"
KITSU_ERASE_OPERATION = "destination-out"
KITSU_ERASER_STROKE = "#000"

# Fabric paths are curves; RV pens are polylines. Curves are sampled about
# every FLATTEN_STEP_PX canvas px (Kitsu's brush curves are only a few px
# long, so most become one or two points).
FLATTEN_STEP_PX = 2.0
FLATTEN_MAX_STEPS = 16

# Namespace for the ids given to RV eraser pens made from Kitsu erasers,
# so the same Kitsu eraser gets the same id every time it's loaded.
_ERASER_UUID_NAMESPACE = uuid.UUID("5d7e1c2a-3b4f-4e8a-9c61-2f0d8a7b4e19")


# ---------------------------------------------------------------------------
# Coordinate and color helpers
# ---------------------------------------------------------------------------

# RV paint space is OpenRV's normalized image space: the image is centred on
# the origin and is 1 unit tall, so y runs -0.5 (bottom) .. +0.5 (top) and x
# runs -aspect/2 .. +aspect/2. Pen widths are expressed in the same units,
# so 1 canvas pixel of stroke width is 1 / canvas_height RV units.

def _aspect(width: int, height: int) -> float:
    return float(width) / float(height) if width and height else 1.0


def rv_normalized_to_pixel(
    nx: float, ny: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    """OpenRV -> Kitsu: RV paint-space normalized point -> Fabric.js canvas
    pixels (origin top-left, y down).
    """
    px = (nx / _aspect(width, height) + 0.5) * canvas_width
    py = (0.5 - ny) * canvas_height
    return px, py


def pixel_to_rv_normalized(
    px: float, py: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    """Kitsu -> OpenRV: Fabric.js canvas pixels -> RV paint-space
    normalized point. Inverse of `rv_normalized_to_pixel`.
    """
    nx = (px / canvas_width - 0.5) * _aspect(width, height)
    ny = 0.5 - py / canvas_height
    return nx, ny


def px_width_to_rv(width_px: float, canvas_height: float) -> float:
    """Kitsu -> OpenRV: a stroke width in canvas pixels -> RV units."""
    return float(width_px) / float(canvas_height)


def rv_width_to_px(width_rv: float, canvas_height: float) -> float:
    """OpenRV -> Kitsu: a stroke width in RV units -> canvas pixels."""
    return float(width_rv) * float(canvas_height)


def _clean(value: float, digits: int = 3) -> Any:
    """Round away float32 noise picked up inside RV (489.98999954 -> 489.99)
    and write whole numbers as ints, the way Kitsu stores them."""
    r = round(float(value), digits)
    return int(r) if r == int(r) else r


def _rv_rgba(color: Any) -> Optional[Tuple[float, float, float, float]]:
    """An RV color as 0..1 floats. Accepts RV's own [[r, g, b, a]] rows
    (0..1) and the flat [r, g, b, a] that `convertToORV` emits (0..255)."""
    if not color:
        return None
    row = color[0] if isinstance(color[0], (list, tuple)) else color
    if not row:
        return None
    vals = [float(c) for c in row[:4]] + [1.0] * (4 - min(len(row), 4))
    if any(c > 1.0 for c in vals):
        vals = [c / 255.0 for c in vals]
    return vals[0], vals[1], vals[2], vals[3]


def rv_color_to_hex(color_rows: Any) -> str:
    """OpenRV -> Kitsu: RV stores color as a list of [r, g, b, a] floats
    in 0..1. Kitsu's "stroke"/"fill" fields are plain hex strings, e.g.
    "#ff3860"."""
    rgba = _rv_rgba(color_rows)
    if rgba is None:
        return "#ffffff"
    r, g, b, _a = rgba
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def rv_alpha(color_rows: Any) -> float:
    """OpenRV -> Kitsu: pull the alpha channel out of an RV color row."""
    rgba = _rv_rgba(color_rows)
    return 1.0 if rgba is None else rgba[3]


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


def color_to_rv_color(value: Any, opacity: float = 1.0) -> List[int]:
    """Kitsu -> OpenRV: a Kitsu/Fabric "stroke" or "fill" value -> an RV
    [r, g, b, a] color, each channel an INTEGER 0..255. Inverse of
    `rv_color_to_hex`/`rv_alpha`.

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


# ---------------------------------------------------------------------------
# Frame <-> time helpers
# ---------------------------------------------------------------------------

DEFAULT_FPS = 24.0


def _check_fps(fps: float) -> float:
    fps = float(fps)
    if not fps > 0:
        raise ValueError(f"fps must be a positive number, got {fps!r}")
    return fps


def kitsu_frame_duration(fps: float) -> float:
    """Length of one frame in seconds, as Kitsu's player computes it:
    1 / fps rounded to 4 decimals (30 fps -> 0.0333, 24 fps -> 0.0417)."""
    return round(1.0 / _check_fps(fps), 4)


def frame_to_time(frame_num: int, fps: float, frame_base: int = 1) -> float:
    """Kitsu record "time", in SECONDS from the first frame, for a frame
    number at the production's frame rate. Uses Kitsu's rounded frame
    duration so the values match what Kitsu writes itself (frame 34 at
    30 fps -> 1.0989)."""
    return round((frame_num - frame_base) * kitsu_frame_duration(fps), 4)


def time_to_frame(time_s: float, fps: float, frame_base: int = 1) -> int:
    """Inverse of `frame_to_time`: a Kitsu "time" in seconds -> frame."""
    return frame_base + round(float(time_s) / kitsu_frame_duration(fps))


def frame_to_time_ms(frame_num: int, fps: float, frame_base: int = 1) -> int:
    """Deprecated: Kitsu stores "time" in seconds, use `frame_to_time`."""
    return round((frame_num - frame_base) / _check_fps(fps) * 1000)


def time_ms_to_frame(time_ms: float, fps: float, frame_base: int = 1) -> int:
    """Deprecated: Kitsu stores "time" in seconds, use `time_to_frame`."""
    return frame_base + round(float(time_ms) / 1000.0 * _check_fps(fps))


# ---------------------------------------------------------------------------
# Fabric.js box geometry (ellipses and rects)
# ---------------------------------------------------------------------------

_ORIGIN_FACTOR = {"left": 0.0, "top": 0.0, "center": 0.5, "right": 1.0, "bottom": 1.0}


def _origin_factor(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return _ORIGIN_FACTOR.get(str(value), 0.0)


def _size_fallback(obj: Dict[str, Any], key: str) -> float:
    """Full width/height of an object with no "width"/"height" field. Only
    an ellipse can be sized from its radii; a rect's rx/ry are corner
    radii, not its size."""
    if obj.get("type") not in KITSU_ELLIPSE_TYPES:
        return 0.0
    return 2.0 * float(obj.get(key, obj.get("radius", 0)) or 0)


def _fabric_box_geometry(obj: Dict[str, Any]) -> Tuple[float, float, float, float, float]:
    """(cx, cy, half width, half height, rendered stroke width), in canvas
    px, of a Fabric "circle", "ellipse" or "rect", measured along the
    stroke's centre line (for an ellipse, its radii).

    Fabric positions an object by its stroke-inclusive box, which is
    (width + strokeWidth) * scale, or width * scale + strokeWidth with
    strokeUniform. left/top is that box's origin point (originX/originY)
    and the box is rotated by `angle` about it.
    """
    sx = abs(float(obj.get("scaleX", 1) or 1))
    sy = abs(float(obj.get("scaleY", 1) or 1))
    sw = float(obj.get("strokeWidth") or 0)

    w = obj.get("width")
    h = obj.get("height")
    if w is None:
        w = _size_fallback(obj, "rx")
    if h is None:
        h = _size_fallback(obj, "ry")
    w, h = float(w), float(h)

    if obj.get("strokeUniform"):
        box_w, box_h = w * sx + sw, h * sy + sw
        rendered_sw = sw
    else:
        box_w, box_h = (w + sw) * sx, (h + sw) * sy
        # The outline is scaled with the object; RV only has one width.
        rendered_sw = sw * (sx + sy) / 2.0

    cx, cy = _fabric_center(obj, box_w, box_h)
    return cx, cy, w * sx / 2.0, h * sy / 2.0, rendered_sw


def _fabric_center(obj: Dict[str, Any], box_w: float, box_h: float) -> Point:
    """Centre, in canvas px, of a Fabric object whose stroke-inclusive,
    scaled box is box_w x box_h. left/top is that box's origin point
    (originX/originY), and the box is rotated by `angle` about it."""
    vx = (0.5 - _origin_factor(obj.get("originX", "left"))) * box_w
    vy = (0.5 - _origin_factor(obj.get("originY", "top"))) * box_h
    a = math.radians(float(obj.get("angle") or 0))
    cx = float(obj.get("left", 0)) + vx * math.cos(a) - vy * math.sin(a)
    cy = float(obj.get("top", 0)) + vx * math.sin(a) + vy * math.cos(a)
    return cx, cy


# ---------------------------------------------------------------------------
# Text geometry
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    """Annotation text without RV's typing cursor or other control
    characters (line breaks and tabs are kept)."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _TEXT_CONTROL_RE.sub("", text)


def _fabric_text_height(font_px: float, line_height: float, n_lines: int) -> float:
    """FabricText.calcTextHeight for a uniformly styled text: every line
    but the last is spaced by lineHeight."""
    line = font_px * FABRIC_FONT_SIZE_MULT
    return line * (line_height * (max(1, n_lines) - 1) + 1)


def _fabric_baseline_offset(font_px: float) -> float:
    """Distance from the top of a Fabric text's first line to its baseline."""
    return font_px * FABRIC_FONT_SIZE_MULT * (1.0 - FABRIC_FONT_SIZE_FRACTION)


def _fabric_text_anchor(obj: Dict[str, Any]) -> Tuple[float, float, float]:
    """(x, y, rendered font size), in canvas px, of a Fabric text: x/y is
    the left end of the first line's baseline, which is where RV anchors
    text.

    Fabric positions text like any object, by its box padded with
    strokeWidth (applied even when the text has no stroke), and draws the
    first line's baseline a fixed fraction of the line height below the
    unpadded box's top.
    """
    sx = abs(float(obj.get("scaleX", 1) or 1))
    sy = abs(float(obj.get("scaleY", 1) or 1))
    sw = float(obj.get("strokeWidth") or 0)
    font_px = float(obj.get("fontSize") or 16)
    lines = _clean_text(obj.get("text")).split("\n")

    w = obj.get("width")
    h = obj.get("height")
    if w is None:
        w = max(len(line) for line in lines) * _ARIAL_AVG_ADVANCE_EM * font_px
    if h is None:
        h = _fabric_text_height(
            font_px, float(obj.get("lineHeight") or KITSU_TEXT_LINE_HEIGHT), len(lines))
    w, h = float(w), float(h)

    if obj.get("strokeUniform"):
        box_w, box_h = w * sx + sw, h * sy + sw
    else:
        box_w, box_h = (w + sw) * sx, (h + sw) * sy
    cx, cy = _fabric_center(obj, box_w, box_h)

    # Offset of the anchor from the centre, in unscaled object space.
    dx = -w / 2.0
    dy = -h / 2.0 + _fabric_baseline_offset(font_px)
    dx, dy = dx * sx, dy * sy
    a = math.radians(float(obj.get("angle") or 0))
    return (
        cx + dx * math.cos(a) - dy * math.sin(a),
        cy + dx * math.sin(a) + dy * math.cos(a),
        font_px * sy,
    )


def _rv_point(value: Any) -> Point:
    """An RV 2D point given flat ([x, y], as RV hands text positions back)
    or nested ([[x, y]], as for other shapes' positions)."""
    if value and isinstance(value[0], (list, tuple)):
        value = value[0]
    return float(value[0]), float(value[1])


def _rv_scalar(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return default if value is None else float(value)


def _rv_text_font_size(props: Dict[str, Any]) -> float:
    """An RV text's height in RV units: its "fontSize", or for sessions
    written before RV had one, the legacy size * scale * 12.5."""
    font_size = _rv_scalar(props.get("fontSize"))
    if font_size is not None and font_size > 0:
        return font_size
    size = _rv_scalar(props.get("size"), 0.01)
    scale = _rv_scalar(props.get("scale"), 1.0)
    return size * scale * RV_LEGACY_TEXT_SIZE_FACTOR


def _fabric_line_endpoints(obj: Dict[str, Any]) -> Tuple[Point, Point, float]:
    """((tail x, y), (head x, y), rendered stroke width), in canvas px, of
    a Fabric Line-based object such as Kitsu's "arrow".

    Fabric keeps a line's true position in its box (left/top/width/height,
    scale, angle, flips); x1..y2 only say which corner is which end and
    aren't updated when the object is moved, scaled or rotated. So the ends
    are rebuilt from the box, and x1..y2 are used directly only when the
    object has no box fields.
    """
    x1, y1 = float(obj.get("x1", 0)), float(obj.get("y1", 0))
    x2, y2 = float(obj.get("x2", 0)), float(obj.get("y2", 0))
    sw = float(obj.get("strokeWidth") or 0)
    if obj.get("left") is None or obj.get("top") is None:
        return (x1, y1), (x2, y2), sw

    sx = float(obj.get("scaleX", 1) or 1)
    sy = float(obj.get("scaleY", 1) or 1)
    w = float(obj["width"]) if obj.get("width") is not None else abs(x2 - x1)
    h = float(obj["height"]) if obj.get("height") is not None else abs(y2 - y1)

    if obj.get("strokeUniform"):
        box_w, box_h = w * abs(sx) + sw, h * abs(sy) + sw
        rendered_sw = sw
    else:
        box_w, box_h = (w + sw) * abs(sx), (h + sw) * abs(sy)
        rendered_sw = sw * (abs(sx) + abs(sy)) / 2.0

    a = math.radians(float(obj.get("angle") or 0))
    cos_a, sin_a = math.cos(a), math.sin(a)

    def rotate(dx: float, dy: float) -> Point:
        return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a

    ox, oy = rotate(
        (0.5 - _origin_factor(obj.get("originX", "left"))) * box_w,
        (0.5 - _origin_factor(obj.get("originY", "top"))) * box_h,
    )
    cx, cy = float(obj["left"]) + ox, float(obj["top"]) + oy

    # Tail offset from the centre in object space; the head is opposite.
    dx = (-1.0 if x1 <= x2 else 1.0) * w / 2.0 * sx
    dy = (-1.0 if y1 <= y2 else 1.0) * h / 2.0 * sy
    if obj.get("flipX"):
        dx = -dx
    if obj.get("flipY"):
        dy = -dy
    tx, ty = rotate(dx, dy)
    return (cx + tx, cy + ty), (cx - tx, cy - ty), rendered_sw


# ---------------------------------------------------------------------------
# Fabric.js transforms
# ---------------------------------------------------------------------------

# Affine matrices in Fabric's [a, b, c, d, e, f] form:
#   x' = a*x + c*y + e,   y' = b*x + d*y + f
Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m: Matrix, n: Matrix) -> Matrix:
    """m * n: apply n first, then m (Fabric's multiplyTransformMatrices)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + c * b2, b * a2 + d * b2,
        a * c2 + c * d2, b * c2 + d * d2,
        a * e2 + c * f2 + e, b * e2 + d * f2 + f,
    )


def _mat_inv(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if not det:
        return _IDENTITY
    return (
        d / det, -b / det, -c / det, a / det,
        (c * f - d * e) / det, (b * e - a * f) / det,
    )


def _mat_apply(m: Matrix, x: float, y: float) -> Point:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _mat_translate(x: float, y: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, float(x), float(y))


def _mat_scale_factor(m: Matrix) -> float:
    """How much `m` scales lengths on average (what a stroke width gets)."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def _qr_decompose(m: Matrix) -> Tuple[float, float, float, float, float, float]:
    """(angle, scaleX, scaleY, skewX, translateX, translateY) of `m`, angles
    in degrees, as Fabric's qrDecompose splits a matrix into an object's
    transform options."""
    a, b, c, d, e, f = m
    denom = a * a + b * b
    if not denom:
        return 0.0, 1.0, 1.0, 0.0, e, f
    scale_x = math.sqrt(denom)
    scale_y = (a * d - c * b) / scale_x
    skew_x = math.atan2(a * c + b * d, denom)
    return math.degrees(math.atan2(b, a)), scale_x, scale_y, math.degrees(skew_x), e, f


def _fabric_size(obj: Dict[str, Any]) -> Point:
    """Unscaled width/height of a Fabric object, without its stroke."""
    w, h = obj.get("width"), obj.get("height")
    if w is None or h is None:
        obj_type = obj.get("type")
        if obj_type == "path":
            _, _, fw, fh = _path_bounds(_parse_fabric_path(obj.get("path")))
        elif obj_type in ("line", "arrow"):
            fw = abs(float(obj.get("x2", 0)) - float(obj.get("x1", 0)))
            fh = abs(float(obj.get("y2", 0)) - float(obj.get("y1", 0)))
        else:
            fw, fh = _size_fallback(obj, "rx"), _size_fallback(obj, "ry")
        w = fw if w is None else w
        h = fh if h is None else h
    return float(w), float(h)


def _fabric_box_dims(obj: Dict[str, Any], w: float, h: float) -> Point:
    """Width/height of an object's scaled, stroke-inclusive box, which is
    what Fabric positions it by."""
    sx = abs(float(obj.get("scaleX", 1) or 1))
    sy = abs(float(obj.get("scaleY", 1) or 1))
    sw = float(obj.get("strokeWidth") or 0)
    if obj.get("strokeUniform"):
        return w * sx + sw, h * sy + sw
    return (w + sw) * sx, (h + sw) * sy


def _fabric_matrix(obj: Dict[str, Any], size: Optional[Point] = None) -> Matrix:
    """Fabric's calcOwnMatrix: maps an object's own plane (origin at its
    centre) into its parent's (the canvas, or the group it sits in).

    `size` overrides the object's width/height (paths are sized by their
    path data). Skew isn't counted in the box size, which only matters for
    skewed objects, which Kitsu doesn't make."""
    w, h = size if size is not None else _fabric_size(obj)
    cx, cy = _fabric_center(obj, *_fabric_box_dims(obj, w, h))
    sx = float(obj.get("scaleX", 1) or 1) * (-1.0 if obj.get("flipX") else 1.0)
    sy = float(obj.get("scaleY", 1) or 1) * (-1.0 if obj.get("flipY") else 1.0)
    a = math.radians(float(obj.get("angle") or 0))

    m = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), cx, cy)
    m = _mat_mul(m, (sx, 0.0, 0.0, sy, 0.0, 0.0))
    skew_x = float(obj.get("skewX") or 0)
    if skew_x:
        m = _mat_mul(m, (1.0, 0.0, math.tan(math.radians(skew_x)), 1.0, 0.0, 0.0))
    skew_y = float(obj.get("skewY") or 0)
    if skew_y:
        m = _mat_mul(m, (1.0, math.tan(math.radians(skew_y)), 0.0, 1.0, 0.0, 0.0))
    return m


def _fabric_aabb(obj: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """(x0, y0, x1, y1): axis-aligned box, in its parent's coordinates, of
    an object's stroke-inclusive outline (what Fabric tests against when it
    decides which objects an eraser stroke touches)."""
    w, h = _fabric_size(obj)
    sw = float(obj.get("strokeWidth") or 0)
    if obj.get("strokeUniform"):
        # The stroke isn't scaled, so in the object's own (unscaled) plane
        # it is sw / scale wide.
        sx = abs(float(obj.get("scaleX", 1) or 1))
        sy = abs(float(obj.get("scaleY", 1) or 1))
        hx, hy = (w + sw / sx) / 2.0, (h + sw / sy) / 2.0
    else:
        hx, hy = (w + sw) / 2.0, (h + sw) / 2.0
    m = _fabric_matrix(obj, (w, h))
    corners = [_mat_apply(m, x, y) for x in (-hx, hx) for y in (-hy, hy)]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _boxes_overlap(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _points_bbox(points: Sequence[Point], pad: float = 0.0) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


# ---------------------------------------------------------------------------
# Fabric.js path data
# ---------------------------------------------------------------------------

# A parsed path is a list of subpaths; each subpath starts with ("M", p)
# followed by ("L", p), ("Q", ctl, p) or ("C", ctl1, ctl2, p) segments.
Segment = Tuple[Any, ...]

_PATH_TOKEN_RE = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_PATH_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4, "C": 6, "Z": 0}


def _path_commands(path: Any) -> List[List[Any]]:
    """Fabric path data as [command, *numbers] lists. Fabric stores the
    parsed form (lists); an SVG path string is tokenized into the same."""
    if isinstance(path, str):
        commands: List[List[Any]] = []
        for token in _PATH_TOKEN_RE.findall(path):
            if token.isalpha():
                commands.append([token])
            elif commands:
                commands[-1].append(float(token))
        return commands
    return [list(seg) for seg in (path or []) if isinstance(seg, (list, tuple)) and seg]


def _parse_fabric_path(path: Any) -> List[List[Segment]]:
    """Fabric path data -> subpaths of absolute segments. Fabric itself
    stores absolute M/L/Q/C/Z; relative commands and H/V are read too."""
    subpaths: List[List[Segment]] = []
    current: Optional[List[Segment]] = None
    cur: Optional[Point] = None
    start: Optional[Point] = None

    for seg in _path_commands(path):
        cmd = str(seg[0])
        upper = cmd.upper()
        arity = _PATH_ARITY.get(upper)
        if arity is None:
            print(f"warning: unsupported path command {cmd!r} in a Kitsu eraser, skipping it",
                  file=sys.stderr)
            continue
        if upper == "Z":
            if current is not None and cur is not None and start is not None and cur != start:
                current.append(("L", start))
            cur, current = start, None
            continue
        try:
            nums = [float(v) for v in seg[1:]]
        except (TypeError, ValueError):
            continue

        for i in range(0, len(nums) - arity + 1, arity):
            group = nums[i:i + arity]
            kind = "L" if (upper == "M" and i > 0) else upper
            ox, oy = cur if (cmd.islower() and cur is not None) else (0.0, 0.0)

            def pt(x: float, y: float) -> Point:
                return x + ox, y + oy

            if kind == "M":
                p = pt(group[0], group[1])
                current = [("M", p)]
                subpaths.append(current)
                cur = start = p
                continue
            if current is None:
                cur = start = cur if cur is not None else (0.0, 0.0)
                current = [("M", cur)]
                subpaths.append(current)
            if kind == "L":
                p = pt(group[0], group[1])
                current.append(("L", p))
            elif kind == "H":
                p = (group[0] + ox, cur[1])
                current.append(("L", p))
            elif kind == "V":
                p = (cur[0], group[0] + oy)
                current.append(("L", p))
            elif kind == "Q":
                p = pt(group[2], group[3])
                current.append(("Q", pt(group[0], group[1]), p))
            else:  # "C"
                p = pt(group[4], group[5])
                current.append(("C", pt(group[0], group[1]), pt(group[2], group[3]), p))
            cur = p
    return subpaths


def _cubic_extrema(p0: float, p1: float, p2: float, p3: float) -> List[float]:
    """Interior extreme values of a 1-D cubic Bezier."""
    d0, d1, d2 = p1 - p0, p2 - p1, p3 - p2
    a, b, c = d0 - 2.0 * d1 + d2, 2.0 * (d1 - d0), d0
    if abs(a) < 1e-12:
        roots = [-c / b] if b else []
    else:
        disc = b * b - 4.0 * a * c
        if disc < 0:
            roots = []
        else:
            sq = math.sqrt(disc)
            roots = [(-b + sq) / (2.0 * a), (-b - sq) / (2.0 * a)]
    return [
        (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3
        for t in roots if 0.0 < t < 1.0
    ]


def _path_bounds(subpaths: Sequence[Sequence[Segment]]) -> Tuple[float, float, float, float]:
    """(minX, minY, width, height) of the curves, as Fabric computes a
    path's size and its pathOffset (the centre of these bounds)."""
    xs: List[float] = []
    ys: List[float] = []
    for subpath in subpaths:
        cur = None
        for seg in subpath:
            p = seg[-1]
            if cur is not None and seg[0] == "Q":
                xs.extend(_quad_extrema(cur[0], seg[1][0], p[0]))
                ys.extend(_quad_extrema(cur[1], seg[1][1], p[1]))
            elif cur is not None and seg[0] == "C":
                xs.extend(_cubic_extrema(cur[0], seg[1][0], seg[2][0], p[0]))
                ys.extend(_cubic_extrema(cur[1], seg[1][1], seg[2][1], p[1]))
            xs.append(p[0])
            ys.append(p[1])
            cur = p
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _bezier_point(ctl: Sequence[Point], t: float) -> Point:
    pts = list(ctl)
    while len(pts) > 1:
        pts = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
               for a, b in zip(pts, pts[1:])]
    return pts[0]


def _flatten_subpath(subpath: Sequence[Segment]) -> List[Point]:
    """A subpath as a polyline, in the path's own coordinates. The sampling
    depends only on the path data, so copies of the same eraser stroke on
    different objects flatten to the same points."""
    cur = subpath[0][1]
    points = [cur]
    for seg in subpath[1:]:
        p = seg[-1]
        if seg[0] != "L":
            ctl = [cur, *seg[1:]]
            length = sum(math.dist(a, b) for a, b in zip(ctl, ctl[1:]))
            steps = max(1, min(FLATTEN_MAX_STEPS, math.ceil(length / FLATTEN_STEP_PX)))
            points.extend(_bezier_point(ctl, i / steps) for i in range(1, steps))
        points.append(p)
        cur = p
    return points


def _brush_path_commands(points: Sequence[Point]) -> List[List[Any]]:
    """A polyline as the path data Fabric's pencil/eraser brush makes from
    its points: quadratic curves through the midpoints of the segments,
    each controlled by the raw point between them, ending on the last."""
    pts = [(_clean(x, 4), _clean(y, 4)) for x, y in points]
    if len(pts) == 1:
        return [["M", *pts[0]], ["L", *pts[0]]]
    commands: List[List[Any]] = [["M", *pts[0]]]
    for p1, p2 in zip(pts, pts[1:]):
        if p1 != p2:
            commands.append(["Q", p1[0], p1[1],
                             _clean((p1[0] + p2[0]) / 2.0, 4), _clean((p1[1] + p2[1]) / 2.0, 4)])
    commands.append(["L", *pts[-1]])
    return commands


# ---------------------------------------------------------------------------
# Erasers
# ---------------------------------------------------------------------------
#
# An eraser stroke is handled as {"points": [(x, y), ...], "width": w}, in
# canvas px on the Kitsu side and RV units on the RV side.

def _is_erasable(obj: Dict[str, Any]) -> bool:
    """Kitsu text is drawn with erasable=False; erasers never cut it."""
    return obj.get("erasable", True) is not False


def _same_eraser_stroke(a: Dict[str, Any], b: Dict[str, Any], tol: float) -> bool:
    pa, pb = a["points"], b["points"]
    if len(pa) != len(pb) or not pa or abs(a["width"] - b["width"]) > tol:
        return False
    return all(abs(xa - xb) <= tol and abs(ya - yb) <= tol
               for (xa, ya), (xb, yb) in zip(pa, pb))


def _fabric_path_strokes(path_obj: Dict[str, Any], parent: Matrix) -> List[Dict[str, Any]]:
    """An eraser path as canvas-px strokes, one per subpath. `parent` maps
    the plane the path sits in (its eraser group's) to the canvas."""
    subpaths = _parse_fabric_path(path_obj.get("path"))
    if not subpaths:
        return []
    min_x, min_y, w, h = _path_bounds(subpaths)
    # Fabric draws a path's data offset by its pathOffset (the centre of
    # its bounds), from the path object's own centre.
    total = _mat_mul(
        _mat_mul(parent, _fabric_matrix(path_obj, (w, h))),
        _mat_translate(-(min_x + w / 2.0), -(min_y + h / 2.0)),
    )
    width_px = float(path_obj.get("strokeWidth") or 0) * _mat_scale_factor(total)
    strokes = []
    for subpath in subpaths:
        points = [_mat_apply(total, x, y) for x, y in _flatten_subpath(subpath)]
        if points:
            strokes.append({"points": points, "width": width_px})
    return strokes


def _fabric_eraser_entries(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The eraser paths stored on a Kitsu object, in the order they were
    made: [{"path": path object, "direct": a direct child of the eraser
    group, "strokes": its strokes in canvas px}, ...]."""
    eraser = obj.get("eraser")
    if not isinstance(eraser, dict):
        return []
    entries: List[Dict[str, Any]] = []

    def walk(container: Dict[str, Any], parent: Matrix, direct: bool) -> None:
        matrix = _mat_mul(parent, _fabric_matrix(container))
        for child in container.get("objects") or []:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "path":
                operation = child.get("globalCompositeOperation") or KITSU_ERASE_OPERATION
                if operation != KITSU_ERASE_OPERATION:
                    # Fabric's "inverted" eraser restores what was erased;
                    # RV has nothing like it.
                    print(f"warning: skipping eraser path on {obj.get('id')!r} drawn with "
                          f"{operation!r}; OpenRV erasers can only erase", file=sys.stderr)
                    continue
                strokes = _fabric_path_strokes(child, matrix)
                if strokes:
                    entries.append({"path": child, "direct": direct, "strokes": strokes})
            elif isinstance(child.get("objects"), list):
                walk(child, matrix, False)
            else:
                print(f"warning: skipping unsupported {child.get('type')!r} in the eraser "
                      f"of {obj.get('id')!r}", file=sys.stderr)

    walk(eraser, _fabric_matrix(obj), True)
    return entries


def _new_eraser_group(obj: Dict[str, Any]) -> Dict[str, Any]:
    """An empty eraser group for `obj`, as Kitsu makes them: centred on the
    object, the size of its stroke-inclusive box."""
    w, h = _fabric_size(obj)
    sw = float(obj.get("strokeWidth") or 0)
    return {
        "top": 0,
        "fill": "rgb(0,0,0)",
        "left": 0,
        "type": KITSU_ERASER_TYPE,
        "angle": 0,
        "flipX": False,
        "flipY": False,
        "skewX": 0,
        "skewY": 0,
        "width": _clean(w + sw, 4),
        "height": _clean(h + sw, 4),
        "scaleX": 1,
        "scaleY": 1,
        "shadow": None,
        "stroke": None,
        "objects": [],
        "opacity": 1,
        "originX": "center",
        "originY": "center",
        "version": FABRIC_VERSION,
        "visible": True,
        "erasable": True,
        "fillRule": "nonzero",
        "paintFirst": "fill",
        "interactive": False,
        "strokeWidth": 0,
        "layoutManager": {"type": "layoutManager", "strategy": "fixed"},
        "strokeLineCap": "butt",
        "strokeUniform": False,
        "strokeLineJoin": "miter",
        "subTargetCheck": False,
        "backgroundColor": "",
        "strokeDashArray": None,
        "strokeDashOffset": 0,
        "strokeMiterLimit": 4,
        "globalCompositeOperation": "source-over",
    }


def _build_eraser_path(stroke: Dict[str, Any], parent: Matrix) -> Dict[str, Any]:
    """A canvas-px eraser stroke -> a Fabric eraser path for a group whose
    plane `parent` maps to the canvas. Like Kitsu's, the path data stays in
    canvas coordinates and the path's own transform cancels the object's."""
    commands = _brush_path_commands(stroke["points"])
    min_x, min_y, w, h = _path_bounds(_parse_fabric_path(commands))
    offset = (min_x + w / 2.0, min_y + h / 2.0)

    # The path's matrix M must satisfy parent * M * T(-offset) = identity.
    angle, sx, sy, skew_x, cx, cy = _qr_decompose(
        _mat_mul(_mat_inv(parent), _mat_translate(*offset)))
    sw = _clean(stroke["width"])
    box_w, box_h = (w + sw) * abs(sx), (h + sw) * abs(sy)
    a = math.radians(angle)
    vx, vy = box_w / 2.0, box_h / 2.0
    left = cx - (vx * math.cos(a) - vy * math.sin(a))
    top = cy - (vx * math.sin(a) + vy * math.cos(a))

    return {
        "top": _clean(top, 4),
        "fill": None,
        "left": _clean(left, 4),
        "path": commands,
        "type": "path",
        "angle": _clean(angle, 6),
        "flipX": False,
        "flipY": False,
        "skewX": _clean(skew_x, 6),
        "skewY": 0,
        "width": _clean(w, 4),
        "height": _clean(h, 4),
        "scaleX": _clean(sx, 6),
        "scaleY": _clean(sy, 6),
        "shadow": None,
        "stroke": KITSU_ERASER_STROKE,
        "opacity": 1,
        "originX": "left",
        "originY": "top",
        "version": FABRIC_VERSION,
        "visible": True,
        "erasable": True,
        "fillRule": "nonzero",
        "paintFirst": "fill",
        "strokeWidth": sw,
        "strokeLineCap": "round",
        "strokeUniform": False,
        "strokeLineJoin": "round",
        "backgroundColor": "",
        "strokeDashArray": None,
        "strokeDashOffset": 0,
        "strokeMiterLimit": 10,
        "globalCompositeOperation": KITSU_ERASE_OPERATION,
    }


def _strokes_match_entries(
    strokes: Sequence[Dict[str, Any]], entries: Sequence[Dict[str, Any]], tol: float,
) -> bool:
    """True if `strokes` are exactly the strokes of `entries`, in any order
    (erasers cutting the same object can be applied in either order)."""
    pool = [s for entry in entries for s in entry["strokes"]]
    if len(pool) != len(strokes):
        return False
    used = [False] * len(pool)
    for stroke in strokes:
        for i, ref in enumerate(pool):
            if not used[i] and _same_eraser_stroke(stroke, ref, tol):
                used[i] = True
                break
        else:
            return False
    return True


def _resolve_eraser(
    obj: Dict[str, Any], ref: Optional[Dict[str, Any]], strokes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """The Kitsu object to send for a converted shape: `obj` (or `ref`, the
    existing Kitsu object it is an unchanged copy of) with an eraser made of
    `strokes`, the RV eraser strokes (canvas px) that cut it.

    An unchanged eraser is kept as it is on Kitsu. A changed one is rebuilt,
    reusing the paths of strokes that were already there."""
    tol = MATCH_TOLERANCE_PX
    ref_entries: List[Dict[str, Any]] = []
    group: Optional[Dict[str, Any]] = None
    if ref is not None:
        base = copy.deepcopy(ref)
        ref_entries = _fabric_eraser_entries(ref)
        if _strokes_match_entries(strokes, ref_entries, tol):
            return base
        if isinstance(ref.get("eraser"), dict):
            group = copy.deepcopy(ref["eraser"])
    else:
        base = obj

    if not strokes:
        base.pop("eraser", None)
        return base

    group = group or _new_eraser_group(base)
    parent = _mat_mul(_fabric_matrix(base), _fabric_matrix(group))
    reusable = [entry for entry in ref_entries if entry["direct"]]
    used = [False] * len(reusable)
    paths: List[Dict[str, Any]] = []
    i = 0
    while i < len(strokes):
        for k, entry in enumerate(reusable):
            n = len(entry["strokes"])
            if not used[k] and n and i + n <= len(strokes) and all(
                _same_eraser_stroke(s, r, tol)
                for s, r in zip(strokes[i:i + n], entry["strokes"])
            ):
                used[k] = True
                paths.append(copy.deepcopy(entry["path"]))
                i += n
                break
        else:
            paths.append(_build_eraser_path(strokes[i], parent))
            i += 1
    group["objects"] = paths
    base["eraser"] = group
    return base


def _is_rv_eraser(shape: Dict[str, Any]) -> bool:
    """True for an RV pen drawn in erase mode."""
    if shape.get("type") != "pen":
        return False
    mode = _rv_scalar(shape.get("properties", {}).get("mode"), RV_PEN_MODE_PAINT)
    return mode == RV_PEN_MODE_ERASE


def _rv_eraser_stroke(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Optional[Dict[str, Any]]:
    """An RV eraser pen as a canvas-px stroke, or None if it has no points.
    Fabric eraser paths have one width, so the widest point's is used."""
    props = shape["properties"]
    points = [
        (_clean(x), _clean(y))
        for x, y in (
            rv_normalized_to_pixel(nx, ny, width, height, canvas_width, canvas_height)
            for nx, ny in (props.get("points") or [])
        )
    ]
    if not points:
        return None
    width_px = max(
        rv_width_to_px(w, canvas_height)
        for w in _per_point_widths(props.get("width"), len(points))
    )
    return {"points": points, "width": width_px, "bbox": _points_bbox(points, width_px / 2.0)}


def _eraser_pen(eraser: Dict[str, Any], frame_num: int) -> Dict[str, Any]:
    """An RV eraser pen shape for a deduplicated Kitsu eraser stroke."""
    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": str(uuid.uuid5(_ERASER_UUID_NAMESPACE, eraser["seed"])),
            "points": list(eraser["points"]),
            "width": [eraser["width"]] * len(eraser["points"]),
            "color": list(_RV_ERASER_COLOR),
            "brush": "circle",
            "mode": RV_PEN_MODE_ERASE,
        },
    }


def _order_with_erasers(
    items: Sequence[Dict[str, Any]], erasers: Sequence[Dict[str, Any]], frame_num: int,
) -> List[Dict[str, Any]]:
    """Interleave a frame's converted shapes with its eraser pens.

    An RV eraser cuts everything drawn before it, so it has to come after
    every object it erased in Kitsu and before every object it overlaps
    but didn't erase (drawn after it, or not erasable, like text). Kitsu
    also moves an object to the end of its frame when it's updated, e.g.
    by erasing it, so the stored order alone can't be relied on.

    Within those rules the Kitsu order is kept: each eraser sits right
    after the last object it erased."""
    if not erasers:
        return [item["shape"] for item in items]

    nodes: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    succ: Dict[Tuple[int, int, int], set] = {}
    indeg: Dict[Tuple[int, int, int], int] = {}

    def add_edge(src: Tuple[int, int, int], dst: Tuple[int, int, int]) -> None:
        if dst not in succ.setdefault(src, set()):
            succ[src].add(dst)
            indeg[dst] = indeg.get(dst, 0) + 1

    for item in items:
        nodes[(item["index"], 0, 0)] = item["shape"]
    for seq, eraser in enumerate(erasers):
        key = (max(eraser["attached"]), 1, seq)
        nodes[key] = _eraser_pen(eraser, frame_num)
        for item in items:
            obj_key = (item["index"], 0, 0)
            if item["index"] in eraser["attached"]:
                add_edge(obj_key, key)
            elif item["bbox"] is not None and _boxes_overlap(item["bbox"], eraser["bbox"]):
                add_edge(key, obj_key)

    heap = [key for key in nodes if not indeg.get(key)]
    heapq.heapify(heap)
    remaining = set(nodes)
    order: List[Dict[str, Any]] = []
    while remaining:
        if not heap:
            # Contradictory erasers (each must cut the other's object but
            # spare its own): keep the Kitsu order for the rest.
            key = min(remaining)
            print(f"warning: erasers on frame {frame_num} can't all be ordered so "
                  f"they cut only what they cut in Kitsu", file=sys.stderr)
        else:
            key = heapq.heappop(heap)
            if key not in remaining:
                continue
        remaining.discard(key)
        order.append(nodes[key])
        for nxt in succ.get(key, ()):
            indeg[nxt] -= 1
            if indeg[nxt] == 0 and nxt in remaining:
                heapq.heappush(heap, nxt)
    return order


def _norm_box(
    box: Sequence[float], width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Tuple[float, float, float, float]:
    """A canvas-px box in RV normalized coordinates (still min/max)."""
    x0, y0 = pixel_to_rv_normalized(box[0], box[1], width, height, canvas_width, canvas_height)
    x1, y1 = pixel_to_rv_normalized(box[2], box[3], width, height, canvas_width, canvas_height)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


# ---------------------------------------------------------------------------
# OpenRV -> Kitsu
# ---------------------------------------------------------------------------

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
        "version": FABRIC_VERSION,
        "visible": True,
        "erasable": True,
        "fillRule": "nonzero",
        "createdBy": author,
        "paintFirst": "fill",
        "canvasWidth": _clean(canvas_width),
        "strokeWidth": stroke_width,
        "canvasHeight": _clean(canvas_height),
        "strokeLineCap": "round",
        "strokeUniform": False,
        "strokeLineJoin": "round",
        "backgroundColor": "",
        "strokeDashArray": None,
        "strokeDashOffset": 0,
        "strokeMiterLimit": 10,
        "globalCompositeOperation": "source-over",
    }


def _quad_extrema(a: float, c: float, b: float) -> List[float]:
    """Interior extreme values of a 1-D quadratic Bezier a -> (ctl c) -> b."""
    denom = a - 2.0 * c + b
    if denom == 0:
        return []
    t = (a - c) / denom
    if 0.0 < t < 1.0:
        return [(1 - t) ** 2 * a + 2 * (1 - t) * t * c + t * t * b]
    return []


def _psstroke_bounds(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    """(minX, minY, width, height) exactly as fabricjs-psbrush's
    PSStroke._parseDimensions computes them: the stroke is drawn as
    quadratic curves between point midpoints, so the bounds are those of
    the curves (the last raw point is never reached), not of the points.
    """
    n = len(points)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0
    if n == 1 or (n == 2 and points[0] != points[1]):
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

    xs: List[float] = []
    ys: List[float] = []
    mid = points[0]
    for i in range(1, n):
        p1, p2 = points[i - 1], points[i]
        new_mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
        xs.append(mid[0])
        ys.append(mid[1])
        xs.extend(_quad_extrema(mid[0], p1[0], new_mid[0]))
        ys.extend(_quad_extrema(mid[1], p1[1], new_mid[1]))
        xs.append(new_mid[0])
        ys.append(new_mid[1])
        mid = new_mid
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _per_point_widths(raw: Any, count: int) -> List[float]:
    """RV pen "width" as one value per point. RV stores one entry per point
    (pressure-modulated), but older data may hold a single value or a bare
    float."""
    if isinstance(raw, (int, float)):
        widths = [float(raw)]
    else:
        widths = [float(w) for w in (raw or [])]
    if not widths:
        widths = [0.01]
    if len(widths) < count:
        widths += [widths[-1]] * (count - len(widths))
    return widths[:count]


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
    points_px = [(_clean(x), _clean(y)) for x, y in points_px]

    # psbrush renders each segment at pressure * strokeWidth. RV keeps that
    # product per point, so split it back into a base width (the widest
    # point) and a 0..1 pressure per point. The rendered width is identical.
    widths_px = [
        rv_width_to_px(w, canvas_height)
        for w in _per_point_widths(props.get("width"), len(points_px))
    ]
    stroke_width_px = _clean(max(widths_px) if widths_px else 1.0)
    pressures = [
        round(w / stroke_width_px, 6) if stroke_width_px else 1
        for w in widths_px
    ]

    min_x, min_y, bbox_w, bbox_h = _psstroke_bounds(points_px)
    # psbrush positions the object by its stroke-inclusive box, so left/top
    # sit half a stroke width outside the curve bounds.
    left = min_x - stroke_width_px / 2.0
    top = min_y - stroke_width_px / 2.0

    start_time = clock_ms[0]
    duration = max(1, len(points_px) * MS_PER_STROKE_POINT)
    end_time = start_time + duration
    clock_ms[0] = end_time

    obj = _fabric_base(
        "PSStroke", _clean(left), _clean(top), _clean(bbox_w), _clean(bbox_h),
        rv_color_to_hex(props.get("color", [])), stroke_width_px,
        _clean(rv_alpha(props.get("color", [])), 6),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["strokePoints"] = [
        {"x": px, "y": py, "type": "PSPoint", "pressure": _clean(p, 6)}
        for (px, py), p in zip(points_px, pressures)
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


def _rv_box_px(
    props: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Tuple[float, float, float, float, Any]:
    """(x0, y0, x1, y1, stroke width) in canvas px of an RV ellipse or rect,
    which RV stores as the axis-aligned min/max corners of the box its
    outline is centred on. x0/y0 is the canvas top-left corner."""
    p0 = rv_normalized_to_pixel(*props["min"][0], width, height, canvas_width, canvas_height)
    p1 = rv_normalized_to_pixel(*props["max"][0], width, height, canvas_width, canvas_height)
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))
    stroke_width_px = _clean(rv_width_to_px(props.get("borderWidth", 0.01), canvas_height))
    return x0, y0, x1, y1, stroke_width_px


def _rv_fill(props: Dict[str, Any]) -> str:
    """Fabric "fill" for an RV innerColor ("transparent" when it has none)."""
    inner = props.get("innerColor")
    return rv_color_to_hex(inner) if inner and rv_alpha(inner) > 0 else "transparent"


def _ellipse_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    """RV "ellipse" (axis-aligned, given by its min/max corners) -> the
    Fabric "circle" Kitsu draws ellipses with. A non-round ellipse becomes
    a circle of the larger radius squashed with scaleX/scaleY, with
    strokeUniform on so the outline stays the same thickness all round."""
    props = shape["properties"]
    x0, y0, x1, y1, stroke_width_px = _rv_box_px(
        props, width, height, canvas_width, canvas_height)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0

    radius = max(rx, ry)
    scale_x = rx / radius if radius else 1.0
    scale_y = ry / radius if radius else 1.0
    uniform_stroke = abs(scale_x - scale_y) > 1e-9
    diameter = 2.0 * radius
    if uniform_stroke:
        box_w = diameter * scale_x + stroke_width_px
        box_h = diameter * scale_y + stroke_width_px
    else:
        box_w = box_h = diameter + stroke_width_px

    border = props.get("borderColor", [])
    obj = _fabric_base(
        "circle", _clean(cx - box_w / 2.0), _clean(cy - box_h / 2.0),
        _clean(diameter), _clean(diameter),
        rv_color_to_hex(border), stroke_width_px, _clean(rv_alpha(border), 6),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj.update({
        "fill": _rv_fill(props),
        "radius": _clean(radius),
        "scaleX": _clean(scale_x, 6),
        "scaleY": _clean(scale_y, 6),
        "startAngle": 0,
        "endAngle": 360,
        "counterClockwise": False,
        "strokeUniform": uniform_stroke,
        "strokeLineCap": "butt",
        "strokeLineJoin": "miter",
        "strokeMiterLimit": 4,
        "createdAt": _iso_ms(clock_ms[0]),
    })
    clock_ms[0] += 1
    return obj


def _rect_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    """RV "rect" (axis-aligned, given by its min/max corners) -> a Fabric
    "rect" as Kitsu draws it: unscaled, square-cornered, positioned by its
    stroke-inclusive box, so left/top sit half a stroke outside the
    outline's centre line."""
    props = shape["properties"]
    x0, y0, x1, y1, stroke_width_px = _rv_box_px(
        props, width, height, canvas_width, canvas_height)
    half_sw = stroke_width_px / 2.0

    border = props.get("borderColor", [])
    obj = _fabric_base(
        "rect", _clean(x0 - half_sw), _clean(y0 - half_sw),
        _clean(x1 - x0), _clean(y1 - y0),
        rv_color_to_hex(border), stroke_width_px, _clean(rv_alpha(border), 6),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj.update({
        "fill": _rv_fill(props),
        "rx": 0,
        "ry": 0,
        "strokeLineCap": "butt",
        "strokeLineJoin": "miter",
        "strokeMiterLimit": 4,
        "createdAt": _iso_ms(clock_ms[0]),
    })
    clock_ms[0] += 1
    return obj


def _rv_arrow_width(props: Dict[str, Any]) -> float:
    """An RV arrow's line width in RV units. RV writes it twice, as
    "thickness" and "borderWidth"; thickness wins when both are there."""
    for key in ("thickness", "borderWidth"):
        value = props.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is not None:
            return float(value)
    return 0.01


def _arrow_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    """RV "arrow" (tail startPos -> head endPos) -> a Kitsu "arrow": a
    Fabric Line from x1/y1 (tail) to x2/y2 (head), positioned by its
    stroke-inclusive box, so left/top sit half a stroke outside the ends."""
    props = shape["properties"]
    x1, y1 = rv_normalized_to_pixel(*props["startPos"][0], width, height,
                                    canvas_width, canvas_height)
    x2, y2 = rv_normalized_to_pixel(*props["endPos"][0], width, height,
                                    canvas_width, canvas_height)
    stroke_width_px = _clean(rv_width_to_px(_rv_arrow_width(props), canvas_height))
    half_sw = stroke_width_px / 2.0

    border = props.get("borderColor", [])
    obj = _fabric_base(
        "arrow", _clean(min(x1, x2) - half_sw), _clean(min(y1, y2) - half_sw),
        _clean(abs(x2 - x1)), _clean(abs(y2 - y1)),
        rv_color_to_hex(border), stroke_width_px, _clean(rv_alpha(border), 6),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj.update({
        "x1": _clean(x1),
        "y1": _clean(y1),
        "x2": _clean(x2),
        "y2": _clean(y2),
        "fill": "transparent",
        "arrowHeadSize": KITSU_ARROW_HEAD_SIZE,
        "arrowHeadWidth": KITSU_ARROW_HEAD_WIDTH,
        "strokeLineCap": "butt",
        "strokeLineJoin": "miter",
        "strokeMiterLimit": 4,
        "createdAt": _iso_ms(clock_ms[0]),
    })
    clock_ms[0] += 1
    return obj


def _text_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Optional[Dict[str, Any]]:
    """RV "text" (anchored at the left end of its first baseline, sized as
    a fraction of the image height) -> a Kitsu "i-text", positioned by the
    top-left of its box as Fabric lays text out. Returns None for a text
    with nothing in it (e.g. one placed in RV but never typed into)."""
    props = shape["properties"]
    text = _clean_text(props.get("text"))
    if not text.strip():
        print(f"warning: skipping empty OpenRV text {shape.get('name')!r}", file=sys.stderr)
        return None

    ax, ay = rv_normalized_to_pixel(*_rv_point(props["position"]), width, height,
                                    canvas_width, canvas_height)
    font_px = _clean(rv_width_to_px(_rv_text_font_size(props), canvas_height))
    lines = text.split("\n")
    # Fabric's default strokeWidth, which pads the box of a stroke-less text.
    stroke_width = 1
    half_sw = stroke_width / 2.0

    color = props.get("color", [])
    box_w = max(len(line) for line in lines) * _ARIAL_AVG_ADVANCE_EM * font_px
    box_h = _fabric_text_height(font_px, KITSU_TEXT_LINE_HEIGHT, len(lines))
    obj = _fabric_base(
        "i-text",
        _clean(ax - half_sw, 4),
        _clean(ay - half_sw - _fabric_baseline_offset(font_px), 4),
        _clean(box_w, 4), _clean(box_h, 4),
        None, stroke_width, _clean(rv_alpha(color), 6),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj.update({
        "text": text,
        "fill": rv_color_to_hex(color),
        "styles": [],
        "fontSize": font_px,
        "fontFamily": KITSU_TEXT_FONT_FAMILY,
        "fontWeight": "bold" if str(props.get("fontWeight", "")).lower() == "bold" else "normal",
        "fontStyle": "italic" if str(props.get("fontStyle", "")).lower() == "italic" else "normal",
        "underline": str(props.get("textDecoration", "")).lower() == "underline",
        "overline": False,
        "linethrough": False,
        "textAlign": "left",
        "lineHeight": KITSU_TEXT_LINE_HEIGHT,
        "charSpacing": 0,
        "direction": "ltr",
        "pathSide": "left",
        "pathAlign": "baseline",
        "pathStartOffset": 0,
        "textBackgroundColor": "",
        "textDecorationThickness": 66.667,
        "backgroundColor": KITSU_TEXT_BACKGROUND,
        "erasable": False,
        "strokeLineCap": "butt",
        "strokeLineJoin": "miter",
        "strokeMiterLimit": 4,
        "createdAt": _iso_ms(clock_ms[0]),
    })
    clock_ms[0] += 1
    return obj


def _infer_canvas_size(
    kitsu_records: Optional[Sequence[Dict[str, Any]]],
    fallback_width: float,
    fallback_height: float,
) -> Tuple[float, float]:
    """Best-effort recovery of the Fabric.js canvas size Kitsu is actually
    using for a preview's annotations, read off any existing object rather
    than assumed to match the raw video resolution (they can differ).
    Falls back to (fallback_width, fallback_height) only if no existing
    object carries the field, e.g. a preview with no annotations yet."""
    for record in kitsu_records or []:
        for obj in record.get("drawing", {}).get("objects", []):
            cw, ch = obj.get("canvasWidth"), obj.get("canvasHeight")
            if cw and ch:
                return float(cw), float(ch)
    return float(fallback_width), float(fallback_height)


def _rendered_stroke(obj: Dict[str, Any]) -> List[Tuple[float, float, float]]:
    """(x, y, rendered width) per point of a PSStroke."""
    sw = float(obj.get("strokeWidth") or 0)
    return [
        (float(p["x"]), float(p["y"]), float(p.get("pressure", 1)) * sw)
        for p in obj.get("strokePoints", [])
    ]


def _same_stroke(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("type") != b.get("type") or a.get("stroke") != b.get("stroke"):
        return False
    ra, rb = _rendered_stroke(a), _rendered_stroke(b)
    if len(ra) != len(rb) or not ra:
        return False
    tol = MATCH_TOLERANCE_PX
    return all(
        abs(xa - xb) <= tol and abs(ya - yb) <= tol and abs(wa - wb) <= tol
        for (xa, ya, wa), (xb, yb, wb) in zip(ra, rb)
    )


def _color_key(value: Any) -> Any:
    """Comparable form of a Fabric color; None for "no color"."""
    if not _has_color(value):
        return None
    if isinstance(value, str):
        return value.strip().lower().lstrip("#")
    return tuple(int(round(c)) for c in value[:4])


def _same_box(a: Dict[str, Any], b: Dict[str, Any], types: Sequence[str]) -> bool:
    """Same ellipse/rect: same outline and fill color, and the same box
    once Fabric's scale/stroke/origin conventions are resolved."""
    if b.get("type") not in types:
        return False
    if _color_key(a.get("stroke")) != _color_key(b.get("stroke")):
        return False
    if _color_key(a.get("fill")) != _color_key(b.get("fill")):
        return False
    tol = MATCH_TOLERANCE_PX
    return all(
        abs(va - vb) <= tol
        for va, vb in zip(_fabric_box_geometry(a), _fabric_box_geometry(b))
    )


def _same_ellipse(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return _same_box(a, b, KITSU_ELLIPSE_TYPES)


def _same_rect(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return _same_box(a, b, KITSU_RECT_TYPES)


def _same_arrow(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Same arrow: same color, and the same tail, head and line width once
    Fabric's box/scale/rotation are resolved."""
    if b.get("type") != "arrow":
        return False
    if _color_key(a.get("stroke")) != _color_key(b.get("stroke")):
        return False
    (ta, ha, wa), (tb, hb, wb) = _fabric_line_endpoints(a), _fabric_line_endpoints(b)
    tol = MATCH_TOLERANCE_PX
    return abs(wa - wb) <= tol and all(
        abs(va - vb) <= tol for va, vb in zip((*ta, *ha), (*tb, *hb))
    )


def _same_text(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Same text: same content and color, and the same baseline anchor and
    rendered font size once Fabric's box/scale/rotation are resolved."""
    if b.get("type") not in KITSU_TEXT_TYPES:
        return False
    if _clean_text(a.get("text")) != _clean_text(b.get("text")):
        return False
    if _color_key(a.get("fill")) != _color_key(b.get("fill")):
        return False
    tol = MATCH_TOLERANCE_PX
    return all(
        abs(va - vb) <= tol
        for va, vb in zip(_fabric_text_anchor(a), _fabric_text_anchor(b))
    )


_MATCHERS = {
    "PSStroke": _same_stroke,
    "circle": _same_ellipse,
    "ellipse": _same_ellipse,
    "rect": _same_rect,
    "arrow": _same_arrow,
    **{text_type: _same_text for text_type in KITSU_TEXT_TYPES},
}


def _reference_index(
    kitsu_records: Optional[Sequence[Dict[str, Any]]],
) -> Dict[Optional[int], List[Dict[str, Any]]]:
    """Existing Kitsu objects that can be matched back, grouped by frame."""
    index: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for record in kitsu_records or []:
        frame = record.get("frame")
        frame = int(frame) if frame is not None else None
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("type") in _MATCHERS:
                index.setdefault(frame, []).append(obj)
    return index


def _match_reference(
    obj: Dict[str, Any], frame_num: int,
    index: Dict[Optional[int], List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Find (and consume) the existing Kitsu object this converted one is an
    unchanged round trip of. RV can't carry Kitsu's author, timestamps, or
    Kitsu-specific fields (the strokeWidth/pressure split, a circle's
    radius vs. scale), so without this every sync would rewrite them."""
    same = _MATCHERS.get(obj.get("type"))
    if same is None:
        return None
    for key in (frame_num, None):
        candidates = index.get(key, [])
        for i, ref in enumerate(candidates):
            if same(obj, ref):
                return candidates.pop(i)
    return None


def _iso_ms(epoch_ms: int) -> str:
    dt = _dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_SHAPE_CONVERTERS = {
    "pen": _pen_to_fabric,
    "line": _line_to_fabric,
    "ellipse": _ellipse_to_fabric,
    "rect": _rect_to_fabric,
    "arrow": _arrow_to_fabric,
    "text": _text_to_fabric,
}


def convertToKitsu(
    openrv_shapes: List[Dict[str, Any]],
    width: int,
    height: int,
    fps: float = DEFAULT_FPS,
    author: Optional[str] = None,
    canvas_width: Optional[float] = None,
    canvas_height: Optional[float] = None,
    frame_offset: int = 0,
    frame_base: int = 1,
    skip_soft_deleted: bool = True,
    reference_records: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """OpenRV paint shapes -> Kitsu annotation records.

    `openrv_shapes` is a flat list of {"frame", "type", "properties", ...}
    dicts, as gathered from RVPaint nodes. Supported types: "pen" (->
    PSStroke), "line", "ellipse" (-> Kitsu's "circle"), "rect", "arrow"
    and "text" (-> "i-text"; empty texts are skipped).

    `fps` is the production's frame rate; each record's "time" is derived
    from its frame number with it, in seconds (see `frame_to_time`).

    Canvas size: an explicit `canvas_width`/`canvas_height` wins. Otherwise
    it is read off any object in `reference_records` (the preview's existing
    Kitsu annotations), falling back to the media `width`/`height`.

    Pass the preview's existing annotations as `reference_records` when
    syncing back: any stroke, ellipse, rect, arrow or text that comes back
    from RV unchanged is returned as the original Kitsu object (same id, author, timestamps
    and Kitsu-only fields) instead of a re-created copy.

    Erase-mode pens (mode 1) aren't objects in Kitsu: each becomes an
    eraser path on every earlier erasable object of its frame that it
    touches. An unchanged eraser is kept as Kitsu stored it.
    """
    fps = _check_fps(fps)
    author = author or str(uuid.uuid4())
    inferred_w, inferred_h = _infer_canvas_size(reference_records, width, height)
    canvas_width = canvas_width or inferred_w
    canvas_height = canvas_height or inferred_h

    by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for shape in openrv_shapes:
        if skip_soft_deleted and shape.get("properties", {}).get("softDeleted"):
            continue
        frame_num = shape["frame"] + frame_offset
        by_frame.setdefault(frame_num, []).append(shape)

    clock_ms = [int(_time.time() * 1000)]
    ref_index = _reference_index(reference_records)
    records: List[Dict[str, Any]] = []

    for frame_num in sorted(by_frame):
        # Each converted object with the RV eraser strokes that cut it.
        entries: List[Dict[str, Any]] = []
        for shape in by_frame[frame_num]:
            if _is_rv_eraser(shape):
                stroke = _rv_eraser_stroke(shape, width, height, canvas_width, canvas_height)
                if stroke is None:
                    continue
                # Like Fabric's eraser brush: the stroke goes on every
                # erasable object drawn before it that it touches.
                for entry in entries:
                    obj = entry["obj"]
                    if _is_erasable(obj) and _boxes_overlap(_fabric_aabb(obj), stroke["bbox"]):
                        entry["erasers"].append(stroke)
                continue

            shape_type = shape.get("type")
            converter = _SHAPE_CONVERTERS.get(shape_type)
            if converter is None:
                print(
                    f"warning: skipping unsupported OpenRV shape type "
                    f"'{shape_type}' (name={shape.get('name')!r})",
                    file=sys.stderr,
                )
                continue
            obj = converter(
                shape, width, height, canvas_width, canvas_height,
                author, clock_ms,
            )
            if obj is not None:
                entries.append({"obj": obj, "erasers": []})

        objects: List[Dict[str, Any]] = []
        for entry in entries:
            obj = entry["obj"]
            ref = _match_reference(obj, frame_num, ref_index)
            if ref is None and "createdAt" not in obj:
                obj["createdAt"] = _iso_ms(obj["endTime"])
            objects.append(_resolve_eraser(obj, ref, entry["erasers"]))

        if not objects:
            continue

        records.append({
            "time": frame_to_time(frame_num, fps, frame_base),
            "frame": frame_num,
            "drawing": {"objects": objects},
        })

    return records


# ---------------------------------------------------------------------------
# Kitsu -> OpenRV
# ---------------------------------------------------------------------------

def _pen_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    stroke_points = obj.get("strokePoints", [])
    points_norm = [
        pixel_to_rv_normalized(p["x"], p["y"], width, height, canvas_width, canvas_height)
        for p in stroke_points
    ]

    # psbrush draws each segment at pressure * strokeWidth; RV pens take a
    # width per point, so bake the pressure into it (the same thing RV's own
    # annotate mode does with tablet pressure). Kitsu's mouse fallback
    # pressure is 0.5 when a point has none.
    stroke_width_px = float(obj.get("strokeWidth", 0.01 * canvas_height))
    widths_norm = [
        px_width_to_rv(stroke_width_px * float(p.get("pressure", 0.5)), canvas_height)
        for p in stroke_points
    ]

    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "points": points_norm,
            "width": widths_norm,
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


def _box_from_fabric(
    kind: str, obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    """A Fabric box-shaped object -> an RV "ellipse" or "rect" (`kind`),
    which share one property set. RV's are axis-aligned, so any rotation
    is dropped (about the centre)."""
    cx, cy, rx, ry, stroke_width_px = _fabric_box_geometry(obj)
    if float(obj.get("angle") or 0) % 360:
        print(
            f"warning: {kind} {obj.get('id')!r} is rotated {obj.get('angle')} deg; "
            f"OpenRV {kind}s can't rotate, dropping the rotation",
            file=sys.stderr,
        )

    # Canvas y runs down and RV y runs up, so the canvas bottom-left corner
    # is RV's min and the top-right is RV's max.
    min_pt = pixel_to_rv_normalized(cx - rx, cy + ry, width, height, canvas_width, canvas_height)
    max_pt = pixel_to_rv_normalized(cx + rx, cy - ry, width, height, canvas_width, canvas_height)

    opacity = obj.get("opacity", 1.0)
    fill = obj.get("fill")
    inner_color = color_to_rv_color(fill, opacity) if _has_color(fill) else list(_RV_NO_COLOR)

    return {
        "type": kind,
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "min": [min_pt],
            "max": [max_pt],
            "borderWidth": px_width_to_rv(stroke_width_px, canvas_height),
            "borderColor": color_to_rv_color(obj.get("stroke"), opacity),
            "innerColor": inner_color,
            # The rest of the property set RV's own ellipses and rects
            # carry: shown on its frame only, in both eyes, not deleted.
            "startFrame": frame_num,
            "duration": 1,
            "eye": 2,
            "softDeleted": 0,
        },
    }


def _ellipse_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    """Kitsu "circle" (or Fabric "ellipse") -> RV "ellipse"."""
    return _box_from_fabric("ellipse", obj, width, height,
                            canvas_width, canvas_height, frame_num)


def _rect_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    """Kitsu "rect" -> RV "rect". RV rects have square corners, so any
    corner rounding (rx/ry) is dropped."""
    if float(obj.get("rx") or 0) or float(obj.get("ry") or 0):
        print(
            f"warning: rect {obj.get('id')!r} has rounded corners "
            f"(rx={obj.get('rx')}, ry={obj.get('ry')}); OpenRV rects are "
            f"square-cornered, dropping the rounding",
            file=sys.stderr,
        )
    return _box_from_fabric("rect", obj, width, height,
                            canvas_width, canvas_height, frame_num)


def _arrow_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    """Kitsu "arrow" -> RV "arrow", tail at startPos and head at endPos.
    RV sizes the head from the line thickness, so Kitsu's
    arrowHeadSize/arrowHeadWidth have nowhere to go and are dropped."""
    (tx, ty), (hx, hy), stroke_width_px = _fabric_line_endpoints(obj)
    start = pixel_to_rv_normalized(tx, ty, width, height, canvas_width, canvas_height)
    end = pixel_to_rv_normalized(hx, hy, width, height, canvas_width, canvas_height)

    width_rv = px_width_to_rv(stroke_width_px, canvas_height)
    # RV's own arrows carry the line color in both borderColor and innerColor.
    color = color_to_rv_color(obj.get("stroke"), obj.get("opacity", 1.0))

    return {
        "type": "arrow",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "startPos": [start],
            "endPos": [end],
            "borderWidth": width_rv,
            "thickness": width_rv,
            "borderColor": color,
            "innerColor": list(color),
            "startFrame": frame_num,
            "duration": 1,
            "eye": 2,
            "softDeleted": 0,
        },
    }


def _text_from_fabric(
    obj: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float, frame_num: int,
) -> Dict[str, Any]:
    """Kitsu "i-text" -> RV "text", with the property set RV's annotate
    mode writes. RV anchors text at the left end of its first baseline and
    sizes it as a fraction of the image height. RV draws text unrotated,
    left-aligned and without a background, in its own default font."""
    ax, ay, font_px = _fabric_text_anchor(obj)
    position = pixel_to_rv_normalized(ax, ay, width, height, canvas_width, canvas_height)
    font_size = px_width_to_rv(font_px, canvas_height)
    text = _clean_text(obj.get("text"))

    if float(obj.get("angle") or 0) % 360:
        print(
            f"warning: text {obj.get('id')!r} is rotated {obj.get('angle')} deg; "
            f"OpenRV draws text unrotated, dropping the rotation",
            file=sys.stderr,
        )
    align = str(obj.get("textAlign") or "left").lower()
    if "\n" in text and align not in ("left", "justify", "justify-left"):
        print(
            f"warning: text {obj.get('id')!r} is {align}-aligned; OpenRV draws "
            f"multi-line text left-aligned",
            file=sys.stderr,
        )

    weight = obj.get("fontWeight")
    try:
        bold = float(weight) >= 600
    except (TypeError, ValueError):
        bold = str(weight or "").lower() in ("bold", "bolder")
    italic = str(obj.get("fontStyle") or "").lower() in ("italic", "oblique")

    return {
        "type": "text",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "position": list(position),
            # Fabric text is colored by its fill; its stroke is an outline.
            "color": color_to_rv_color(obj.get("fill"), obj.get("opacity", 1.0)),
            "text": text,
            "fontSize": font_size,
            "size": font_size / RV_LEGACY_TEXT_SIZE_FACTOR,
            "scale": 1.0,
            "rotation": 0.0,
            "spacing": 0.8,
            "font": "",
            "origin": "",
            "debug": 0,
            "mode": 0,
            # "" is RV's default annotation font.
            "fontFamily": "",
            "fontWeight": "bold" if bold else "normal",
            "fontStyle": "italic" if italic else "normal",
            "textDecoration": "underline" if obj.get("underline") else "none",
            "textAlign": "left",
            "startFrame": frame_num,
            "duration": 1,
            "softDeleted": 0,
        },
    }


_TYPE_CONVERTERS = {
    "PSStroke": _pen_from_fabric,
    "line": _line_from_fabric,
    "circle": _ellipse_from_fabric,
    "ellipse": _ellipse_from_fabric,
    "rect": _rect_from_fabric,
    "arrow": _arrow_from_fabric,
    **{text_type: _text_from_fabric for text_type in KITSU_TEXT_TYPES},
}


def convertToORV(
    kitsu_records: Sequence[Dict[str, Any]],
    width: int,
    height: int,
    canvas_width: Optional[float] = None,
    canvas_height: Optional[float] = None,
    frame_offset: int = 0,
    default_frame: int = 1,
    fps: Optional[float] = None,
    frame_base: int = 1,
) -> List[Dict[str, Any]]:
    """Kitsu annotation records -> flat list of OpenRV paint shapes
    ({"type", "frame", "properties"}). Supported Kitsu types: "PSStroke"
    (-> pen), "line", "circle"/"ellipse" (-> ellipse), "rect", "arrow" and
    "i-text"/"text"/"textbox" (-> text).

    Each object's own canvasWidth/canvasHeight is used when present;
    otherwise `canvas_width`/`canvas_height`, then the media size.

    A record's "frame" is used when present. Without one, its "time" is
    converted to a frame at the production `fps` (the inverse of
    `convertToKitsu`, counting from `frame_base`). Records with neither,
    or when no `fps` is given, land on `default_frame` (still previews).

    Kitsu erasers (the "eraser" group on each erased object) become RV
    pens in erase mode (properties["mode"] == 1), one per eraser stroke,
    placed after the objects it erased and before the ones it didn't.
    """
    default_canvas_width = canvas_width or float(width)
    default_canvas_height = canvas_height or float(height)
    if fps is not None:
        fps = _check_fps(fps)

    shapes: List[Dict[str, Any]] = []

    for record in kitsu_records or []:
        raw_frame = record.get("frame")
        raw_time = record.get("time")
        if raw_frame is not None:
            frame_num = int(raw_frame)
        elif raw_time is not None and fps is not None:
            frame_num = time_to_frame(raw_time, fps, frame_base)
        else:
            frame_num = default_frame
        frame_num -= frame_offset
        objects = record.get("drawing", {}).get("objects", [])
        shapes.extend(_record_to_orv(
            objects, width, height, default_canvas_width, default_canvas_height, frame_num,
        ))

    return shapes


def _record_to_orv(
    objects: Sequence[Dict[str, Any]], width: int, height: int,
    default_canvas_width: float, default_canvas_height: float, frame_num: int,
) -> List[Dict[str, Any]]:
    """One Kitsu record's objects -> RV shapes, with an eraser pen for each
    distinct eraser stroke (Fabric stores a copy on every object it cut)."""
    items: List[Dict[str, Any]] = []
    erasers: List[Dict[str, Any]] = []

    for index, obj in enumerate(objects):
        obj_type = obj.get("type")
        obj_canvas_width = float(obj.get("canvasWidth") or default_canvas_width)
        obj_canvas_height = float(obj.get("canvasHeight") or default_canvas_height)

        converter = _TYPE_CONVERTERS.get(obj_type)
        shape = None
        if converter is None:
            print(
                f"warning: skipping unsupported Kitsu object type "
                f"'{obj_type}' (id={obj.get('id')!r})",
                file=sys.stderr,
            )
        else:
            shape = converter(
                obj, width, height, obj_canvas_width, obj_canvas_height, frame_num,
            )
        if shape is not None:
            items.append({
                "index": index,
                "shape": shape,
                "bbox": _norm_box(_fabric_aabb(obj), width, height,
                                  obj_canvas_width, obj_canvas_height),
            })

        tol = MATCH_TOLERANCE_PX / obj_canvas_height
        for e_i, entry in enumerate(_fabric_eraser_entries(obj)):
            for s_i, stroke in enumerate(entry["strokes"]):
                points = [
                    pixel_to_rv_normalized(x, y, width, height,
                                           obj_canvas_width, obj_canvas_height)
                    for x, y in stroke["points"]
                ]
                norm = {"points": points,
                        "width": px_width_to_rv(stroke["width"], obj_canvas_height)}
                same = next((e for e in erasers if _same_eraser_stroke(e, norm, tol)), None)
                if same is not None:
                    same["attached"].add(index)
                    continue
                erasers.append({
                    **norm,
                    "attached": {index},
                    "bbox": _points_bbox(points, norm["width"] / 2.0),
                    "seed": f"{obj.get('id')}:{e_i}:{s_i}",
                })

    return _order_with_erasers(items, erasers, frame_num)


def extract_authors(kitsu_records: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Map each object's Fabric "id" -> its Kitsu "createdBy" person id,
    since OpenRV has no field to carry that through. Useful if the caller
    wants to preserve authorship out-of-band alongside `convertToORV`'s
    output."""
    authors: Dict[str, str] = {}
    for record in kitsu_records or []:
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("id") and obj.get("createdBy"):
                authors[obj["id"]] = obj["createdBy"]
    return authors


def _same_time(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return a == b


def split_annotation_changes(
    records: Sequence[Dict[str, Any]],
    reference_records: Optional[Sequence[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(additions, updates) for gazu.files.update_preview_annotations,
    from `convertToKitsu` records and the annotations Kitsu already has.

    Kitsu never overwrites an object through an addition (one whose id is
    already on that frame is dropped), so objects that exist but changed,
    e.g. by being erased, have to go in updates, under the time Kitsu
    stores them at. Unchanged objects aren't sent."""
    known: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
    for record in reference_records or []:
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("id"):
                known[obj["id"]] = (record.get("time"), obj)

    additions: List[Dict[str, Any]] = []
    updates: Dict[Any, Dict[str, Any]] = {}
    for record in records:
        added = []
        for obj in record.get("drawing", {}).get("objects", []):
            ref = known.get(obj.get("id"))
            if ref is None or not _same_time(ref[0], record["time"]):
                added.append(obj)
            elif obj != ref[1]:
                update = updates.setdefault(ref[0], {
                    "time": ref[0],
                    "frame": record.get("frame"),
                    "drawing": {"objects": []},
                })
                update["drawing"]["objects"].append(obj)
        if added:
            additions.append({
                "time": record["time"],
                "frame": record.get("frame"),
                "drawing": {"objects": added},
            })
    return additions, list(updates.values())


# ---------------------------------------------------------------------------
# RVPaint GTO serialization
# ---------------------------------------------------------------------------

def _fnum(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return f"{n:.9f}".rstrip("0").rstrip(".") or "0"


def _flat(arr: Sequence[float]) -> str:
    return "[ " + " ".join(_fnum(n) for n in arr) + " ]"


def _nested(pairs: Sequence[Point]) -> str:
    return "[ " + " ".join(f"[ {_fnum(x)} {_fnum(y)} ]" for x, y in pairs) + " ]"


def _color01(color: Any, default: Sequence[float] = (1.0, 1.0, 1.0, 1.0)) -> List[float]:
    """Any RV color form (flat 0..255 from `convertToORV`, or RV's own 0..1,
    flat or as [[r, g, b, a]]) -> the flat 0..1 [r, g, b, a] GTO wants."""
    rgba = _rv_rgba(color)
    return [round(c, 6) for c in (rgba if rgba is not None else default)]


def _pen_block(pen: Dict[str, Any], pen_id: int, frame: int) -> Tuple[str, str]:
    name = f'"pen:{pen_id}:{frame}:Kitsu"'
    color = _color01(pen["color"])
    width = _per_point_widths(pen["width"], len(pen["points"]))

    lines = [f"    {name}", "    {"]
    lines.append(f"        float[4] color = {_flat(color)}")
    lines.append(f"        float width = {_flat(width)}")
    lines.append(f'        string brush = "{pen.get("brush", "circle")}"')
    lines.append(f"        float[2] points = {_nested(pen['points'])}")
    lines.append(f"        int debug = {int(pen.get('debug', 0))}")
    lines.append(f"        int join = {int(pen.get('join', 3))}")
    lines.append(f"        int cap = {int(pen.get('cap', 1))}")
    lines.append(f"        int splat = {int(pen.get('splat', 0))}")
    # 1 = eraser: clears what's drawn before it on the frame.
    lines.append(f"        int mode = {int(_rv_scalar(pen.get('mode'), RV_PEN_MODE_PAINT))}")
    if pen.get("uuid"):
        lines.append(f'        string uuid = "{pen["uuid"]}"')
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


def _box_block(kind: str, ell: Dict[str, Any], ell_id: int, frame: int) -> Tuple[str, str]:
    """An RVPaint ellipse or rect component (`kind`), with the same
    property set RV writes for those drawn in its own annotate mode."""
    name = f'"{kind}:{ell_id}:{frame}:Kitsu"'
    inner = _color01(ell.get("innerColor"), default=(1.0, 1.0, 1.0, 0.0))
    border = _color01(ell.get("borderColor"))

    lines = [f"    {name}", "    {"]
    lines.append(f"        int startFrame = {int(ell.get('startFrame', frame))}")
    lines.append(f"        int duration = {int(ell.get('duration', 1))}")
    lines.append(f"        int eye = {int(ell.get('eye', 2))}")
    lines.append(f"        float[2] min = {_nested(ell['min'])}")
    lines.append(f"        float[2] max = {_nested(ell['max'])}")
    lines.append(f"        float[4] innerColor = {_flat(inner)}")
    lines.append(f"        float[4] borderColor = {_flat(border)}")
    lines.append(f"        float borderWidth = {_fnum(float(ell.get('borderWidth', 0.01)))}")
    if ell.get("uuid"):
        lines.append(f'        string uuid = "{ell["uuid"]}"')
    lines.append(f"        int softDeleted = {int(ell.get('softDeleted', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


def _ellipse_block(ell: Dict[str, Any], ell_id: int, frame: int) -> Tuple[str, str]:
    return _box_block("ellipse", ell, ell_id, frame)


def _rect_block(rect: Dict[str, Any], rect_id: int, frame: int) -> Tuple[str, str]:
    return _box_block("rect", rect, rect_id, frame)


def _arrow_block(arrow: Dict[str, Any], arrow_id: int, frame: int) -> Tuple[str, str]:
    """An RVPaint arrow component, with the property set RV writes for
    arrows drawn in its own annotate mode."""
    name = f'"arrow:{arrow_id}:{frame}:Kitsu"'
    border = _color01(arrow.get("borderColor"))
    inner = _color01(arrow.get("innerColor")) if arrow.get("innerColor") else border
    width = _rv_arrow_width(arrow)

    lines = [f"    {name}", "    {"]
    lines.append(f"        int startFrame = {int(arrow.get('startFrame', frame))}")
    lines.append(f"        int duration = {int(arrow.get('duration', 1))}")
    lines.append(f"        int eye = {int(arrow.get('eye', 2))}")
    lines.append(f"        float[2] startPos = {_nested(arrow['startPos'])}")
    lines.append(f"        float[2] endPos = {_nested(arrow['endPos'])}")
    lines.append(f"        float[4] borderColor = {_flat(border)}")
    lines.append(f"        float borderWidth = {_fnum(float(arrow.get('borderWidth', width)))}")
    lines.append(f"        float[4] innerColor = {_flat(inner)}")
    lines.append(f"        float thickness = {_fnum(width)}")
    if arrow.get("uuid"):
        lines.append(f'        string uuid = "{arrow["uuid"]}"')
    lines.append(f"        int softDeleted = {int(arrow.get('softDeleted', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


def _gto_string(value: Any) -> str:
    escaped = (str(value).replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\t", "\\t"))
    return f'"{escaped}"'


def _text_block(txt: Dict[str, Any], text_id: int, frame: int) -> Tuple[str, str]:
    """An RVPaint text component, with the property set RV writes for text
    placed in its own annotate mode (legacy size/scale plus the QFont-era
    fontSize/fontFamily/...)."""
    name = f'"text:{text_id}:{frame}:Kitsu"'
    font_size = _rv_text_font_size(txt)
    scale = _rv_scalar(txt.get("scale"), 1.0) or 1.0
    size = _rv_scalar(txt.get("size"))
    if size is None:
        size = font_size / scale / RV_LEGACY_TEXT_SIZE_FACTOR

    lines = [f"    {name}", "    {"]
    lines.append(f"        float[2] position = {_nested([_rv_point(txt['position'])])}")
    lines.append(f"        float[4] color = {_flat(_color01(txt.get('color')))}")
    lines.append(f"        float spacing = {_fnum(_rv_scalar(txt.get('spacing'), 0.8))}")
    lines.append(f"        float size = {_fnum(size)}")
    lines.append(f"        float scale = {_fnum(scale)}")
    lines.append(f"        float rotation = {_fnum(_rv_scalar(txt.get('rotation'), 0.0))}")
    lines.append(f"        string font = {_gto_string(txt.get('font', ''))}")
    lines.append(f"        string text = {_gto_string(_clean_text(txt.get('text')))}")
    lines.append(f"        string origin = {_gto_string(txt.get('origin', ''))}")
    lines.append(f"        int debug = {int(txt.get('debug', 0))}")
    lines.append(f"        int startFrame = {int(txt.get('startFrame', frame))}")
    lines.append(f"        int duration = {int(txt.get('duration', 1))}")
    lines.append(f"        int mode = {int(txt.get('mode', 0))}")
    lines.append(f"        string fontFamily = {_gto_string(txt.get('fontFamily', ''))}")
    lines.append(f"        float fontSize = {_fnum(font_size)}")
    lines.append(f"        string fontWeight = {_gto_string(txt.get('fontWeight', 'normal'))}")
    lines.append(f"        string fontStyle = {_gto_string(txt.get('fontStyle', 'normal'))}")
    lines.append(f"        string textDecoration = {_gto_string(txt.get('textDecoration', 'none'))}")
    lines.append(f"        string textAlign = {_gto_string(txt.get('textAlign', 'left'))}")
    if txt.get("uuid"):
        lines.append(f"        string uuid = {_gto_string(txt['uuid'])}")
    lines.append(f"        int softDeleted = {int(txt.get('softDeleted', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


_GTO_BLOCKS = {
    "pen": _pen_block,
    "ellipse": _ellipse_block,
    "rect": _rect_block,
    "arrow": _arrow_block,
    "text": _text_block,
}


def _paint_gto(paint_node_name: str, items: Sequence[Tuple[int, str, Dict[str, Any]]]) -> Optional[str]:
    """RVPaint GTO text for (frame, kind, properties) items, drawn in the
    given order on each frame."""
    blocks = []
    frame_order: Dict[int, List[str]] = {}
    next_id = 0

    for frame, kind, data in items:
        block, cname = _GTO_BLOCKS[kind](data, next_id, frame)
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


def build_paint_gto(paint_node_name: str, openrv_annotations: List[Dict[str, Any]]) -> Optional[str]:
    """
    openrv_annotations: list of {
        "frame": int,
        "pens":     [ {color, width, brush, points, join, cap, splat, debug}, ... ],
        "ellipses": [ {min, max, borderWidth, borderColor, innerColor,
                       startFrame, duration, eye, uuid}, ... ],
        "rects":    [ same keys as "ellipses" ],
        "arrows":   [ {startPos, endPos, borderWidth, thickness, borderColor,
                       innerColor, startFrame, duration, eye, uuid}, ... ],
        "texts":    [ {position, color, text, fontSize, fontWeight, fontStyle,
                       textDecoration, spacing, size, scale, rotation,
                       startFrame, duration, uuid, debug}, ... ],
    }
    Pen, ellipse, rect, arrow and text entries are the "properties" dicts
    `convertToORV` emits. A text without "fontSize" is sized from its
    legacy size * scale.
    Colors may be 0..255 or 0..1; they are written as 0..1.

    Returns the RVPaint GTO fragment as text, or None if there's nothing
    to write on any frame.
    """
    items: List[Tuple[int, str, Dict[str, Any]]] = []
    for frame_data in openrv_annotations:
        frame = int(frame_data["frame"])
        items += [(frame, "pen", p) for p in frame_data.get("pens", [])]
        items += [(frame, "ellipse", e) for e in frame_data.get("ellipses", [])]
        items += [(frame, "rect", r) for r in frame_data.get("rects", [])]
        items += [(frame, "arrow", a) for a in frame_data.get("arrows", [])]
        items += [(frame, "text", t) for t in frame_data.get("texts", [])]
    return _paint_gto(paint_node_name, items)


def shapes_to_paint_gto(paint_node_name: str, shapes: Sequence[Dict[str, Any]]) -> Optional[str]:
    """`convertToORV` output -> RVPaint GTO text, keeping each frame's
    drawing order. Shape types with no GTO writer (e.g. "line") are skipped
    with a warning."""
    items: List[Tuple[int, str, Dict[str, Any]]] = []
    for shape in shapes:
        kind = shape.get("type")
        if kind not in _GTO_BLOCKS:
            print(f"warning: no RVPaint writer for shape type {kind!r}, skipping", file=sys.stderr)
            continue
        items.append((int(shape["frame"]), kind, shape["properties"]))
    return _paint_gto(paint_node_name, items)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    """Convert a Kitsu preview-annotation JSON dump into OpenRV paint
    shapes. Works without OpenRV, PySide6, or gazu installed."""
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
    parser.add_argument("--fps", type=float, default=None, help="Production frame rate; used to turn a record's 'time' into a frame when it has no 'frame' field")
    parser.add_argument("--frame-base", type=int, default=1, help="Frame number that time 0 corresponds to (with --fps)")
    parser.add_argument("-o", "--output", default=None, help="Where to write the OpenRV shapes JSON (default: stdout)")
    args = parser.parse_args()

    with open(args.kitsu_json) as f:
        records = json.load(f)

    shapes = convertToORV(
        records,
        width=args.width,
        height=args.height,
        canvas_width=args.canvas_width,
        canvas_height=args.canvas_height,
        frame_offset=args.frame_offset,
        default_frame=args.default_frame,
        fps=args.fps,
        frame_base=args.frame_base,
    )

    output = json.dumps(shapes, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        print(output)


if __name__ == "__main__":
    _main()