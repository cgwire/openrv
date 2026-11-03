#!/usr/bin/env python3
"""
Convert annotations between Kitsu and OpenRV.

This module is pure Python. It has no OpenRV, Qt or gazu dependency.
So you can unit test it and run it alone (see the CLI at the bottom).

Public API:
    convertToKitsu(openrv_shapes, width, height, ...) -> Kitsu records
    convertToORV(kitsu_records, width, height, ...)   -> OpenRV shapes
    split_annotation_changes(records, reference)      -> (additions, updates)

Erasers work in different ways on the two sides. In RV, an eraser is a pen with
mode 1. It clears everything drawn before it on its frame.

In Kitsu (Fabric.js), the eraser stroke is stored on each object that it
erased. It is a path in the "eraser" group of that object. Later objects are
not affected.

The converters translate between the two (see "Erasers").
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import heapq
import json
import math
import re
import sys
import time as _time
import uuid
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "convertToKitsu",
    "convertToORV",
    "frame_to_time",
    "time_to_frame",
    "split_annotation_changes",
    "DEFAULT_FPS",
]

Point = Tuple[float, float]

MS_PER_STROKE_POINT = 8

# Fabric.js version that Kitsu writes now in annotation objects.
FABRIC_VERSION = "7.4.0"

# Distance limit (canvas px). A converted stroke that is this close to a Kitsu
# stroke is the same, unchanged stroke.
MATCH_TOLERANCE_PX = 0.05

# Kitsu draws ellipses as Fabric.js "circle" objects. When the ellipse is not
# round, Kitsu stretches the circle with scaleX/scaleY. The code also reads the
# "ellipse" class of Fabric.
KITSU_ELLIPSE_TYPES = ("circle", "ellipse")

# Kitsu rectangles are Fabric.js "rect" objects. The rx/ry fields of Fabric on
# a rect are corner radii. RV rects do not have corner radii.
KITSU_RECT_TYPES = ("rect",)

# Kitsu arrows are a Fabric.js Line subclass ("arrow"). x1/y1 is the tail and
# x2/y2 is the head. The object also has a head size and a head width in canvas
# px. In RV, the head size follows the line thickness, so RV arrows have no
# separate head size. Arrows from RV get the head that Kitsu draws by default.
KITSU_ARROW_HEAD_SIZE = 15
KITSU_ARROW_HEAD_WIDTH = 12

# Kitsu text is a Fabric.js "i-text". The code also reads "text" and "textbox".
# The color is the "fill" and the fontSize is in canvas px. Kitsu always draws
# text in Arial on a translucent white background.
KITSU_TEXT_TYPES = ("i-text", "text", "textbox")
KITSU_TEXT_FONT_FAMILY = "arial"
KITSU_TEXT_LINE_HEIGHT = 1.16
KITSU_TEXT_BACKGROUND = "rgba(255,255,255, 0.8)"

# Fabric.js text metrics (FabricText._fontSizeMult / _fontSizeFraction): a line
# is fontSize * 1.13 tall. Its baseline is 22.2% of that height above the
# bottom of the line.
FABRIC_FONT_SIZE_MULT = 1.13
FABRIC_FONT_SIZE_FRACTION = 0.222

# Rough Arial advance per character, in em. The code uses it only for the box
# width stored on text from RV. Fabric measures the text again when Kitsu loads
# it. The width does not change where the text is drawn.
_ARIAL_AVG_ADVANCE_EM = 0.55

# RV annotate mode also writes a legacy "size" for RV builds that are older
# than QFont. These builds draw text with a height of size * scale * 12.5.
# Current RV reads "fontSize".
RV_LEGACY_TEXT_SIZE_FACTOR = 12.5

# While the user types a text, RV annotate mode keeps an editing cursor (0x01)
# at the end of it. The cursor and other control characters are not content.
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

_NO_COLOR_VALUES = {"none", "null", "transparent", ""}

# Fully transparent RV color, in the flat 0..255 form that `convertToORV`
# makes.
_RV_NO_COLOR = [255, 255, 255, 0]

# RV pen "mode": 0 paints. 1 erases what was drawn before it on the frame.
RV_PEN_MODE_PAINT = 0
RV_PEN_MODE_ERASE = 1

# The eraser pens of RV are white. The color is not drawn.
_RV_ERASER_COLOR = [255, 255, 255, 255]

# Kitsu erasers: a Fabric "eraser" group on the erased object. It holds one
# "path" for each eraser stroke. The paths use "destination-out", so they cut
# the object. The eraser brush of Kitsu paints them black.
KITSU_ERASER_TYPE = "eraser"
KITSU_ERASE_OPERATION = "destination-out"
KITSU_ERASER_STROKE = "#000"

# Fabric paths are curves, but RV pens are polylines. The code samples the
# curves about every FLATTEN_STEP_PX canvas px. The brush curves of Kitsu are
# only a few px long, so most curves become one or two points.
FLATTEN_STEP_PX = 2.0
FLATTEN_MAX_STEPS = 16

# Namespace for the ids of the RV eraser pens made from Kitsu erasers. It gives
# the same id to the same Kitsu eraser each time the code loads it.
_ERASER_UUID_NAMESPACE = uuid.UUID("5d7e1c2a-3b4f-4e8a-9c61-2f0d8a7b4e19")


# ---------------------------------------------------------------------------
# Coordinate and color helpers
# ---------------------------------------------------------------------------

# RV paint space is the normalized image space of OpenRV. The image is centred
# on the origin and is 1 unit tall. So y goes from -0.5 (bottom) to +0.5 (top)
# and x goes from -aspect/2 to +aspect/2. Pen widths use the same units. So 1
# canvas pixel of stroke width is 1 / canvas_height RV units.


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


class _PaintSpace(NamedTuple):
    """
    Give the sizes that a conversion between RV and Kitsu coordinates needs.

    `width` and `height` are the media size. `canvas_width` and
    `canvas_height` are the size of the Fabric.js canvas. The two sizes can
    be different.
    """

    width: int
    height: int
    canvas_width: float
    canvas_height: float


def _aspect(width: int, height: int) -> float:
    return float(width) / float(height) if width and height else 1.0


def rv_normalized_to_pixel(
    nx: float,
    ny: float,
    space: _PaintSpace,
) -> Point:
    """
    OpenRV -> Kitsu: convert a normalized point of RV paint space to Fabric.js
    canvas pixels (origin top-left, y down).
    """
    px = (nx / _aspect(space.width, space.height) + 0.5) * space.canvas_width
    py = (0.5 - ny) * space.canvas_height
    return px, py


def pixel_to_rv_normalized(
    px: float,
    py: float,
    space: _PaintSpace,
) -> Point:
    """
    Kitsu -> OpenRV: convert Fabric.js canvas pixels to a normalized point of
    RV paint space. This is the inverse of `rv_normalized_to_pixel`.
    """
    nx = (px / space.canvas_width - 0.5) * _aspect(space.width, space.height)
    ny = 0.5 - py / space.canvas_height
    return nx, ny


def px_width_to_rv(width_px: float, canvas_height: float) -> float:
    """
    Kitsu -> OpenRV: convert a stroke width from canvas pixels to RV units.
    """
    return float(width_px) / float(canvas_height)


def rv_width_to_px(width_rv: float, canvas_height: float) -> float:
    """
    OpenRV -> Kitsu: convert a stroke width from RV units to canvas pixels.
    """
    return float(width_rv) * float(canvas_height)


def _clean(value: float, digits: int = 3) -> Any:
    """
    Round away the float32 noise that RV adds (489.98999954 -> 489.99). Write
    whole numbers as ints, as Kitsu stores them.
    """
    r = round(float(value), digits)
    return int(r) if r == int(r) else r


def _rv_rgba(color: Any) -> Optional[Tuple[float, float, float, float]]:
    """
    Return an RV color as 0..1 floats. Accept the [[r, g, b, a]] rows of RV
    (0..1) and the flat [r, g, b, a] that `convertToORV` makes (0..255).
    """
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
    """
    OpenRV -> Kitsu: RV stores a color as a list of [r, g, b, a] floats in
    0..1. The "stroke" and "fill" fields of Kitsu are plain hex strings, for
    example "#ff3860".
    """
    rgba = _rv_rgba(color_rows)
    if rgba is None:
        return "#ffffff"
    r, g, b, _a = rgba
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def rv_alpha(color_rows: Any) -> float:
    """
    OpenRV -> Kitsu: get the alpha channel from an RV color row.
    """
    rgba = _rv_rgba(color_rows)
    return 1.0 if rgba is None else rgba[3]


def _is_hex_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value.strip()))


def _is_no_color(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().lower() in _NO_COLOR_VALUES
    )


def _has_color(value: Any) -> bool:
    """
    Return True if `value` is an actual color (rgba array or hex string).
    Return False for a "none", "transparent" or None marker.
    """
    if _is_no_color(value):
        return False
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return True
    return _is_hex_color(value)


def color_to_rv_color(value: Any, opacity: float = 1.0) -> List[int]:
    """
    Kitsu -> OpenRV: convert a "stroke" or "fill" value of Kitsu/Fabric to an
    RV [r, g, b, a] color. Each channel is an INTEGER 0..255. This is the
    inverse of `rv_color_to_hex` and `rv_alpha`.

    Accepted forms (the forms that the real data contains):
      * An rgba array, for example [255, 56, 96, 255] or [255, 56, 96].
        It has 3 or 4 numbers, each already on a 0..255 scale. Real Kitsu
        annotation objects contain this form.
      * A "#rrggbb" hex string. The code keeps it for backwards
        compatibility.
      * Fabric.js/CSS "none", "transparent" or None. The code treats it as
        opaque white (that is, "no color set").

    `opacity` is the separate 0..1 "opacity" field of Fabric. The code scales
    it to 0..255 and uses it as the alpha channel ONLY when `value` does not
    already have its own 4th (alpha) element.

    If `value` has no recognized shape, the function does not raise an error.
    It returns opaque white and writes a warning to stderr.
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

    _warn(f"unrecognized color value {value!r}, falling back to white")
    return [255, 255, 255, fallback_alpha]


def _per_point_widths(raw: Any, count: int) -> List[float]:
    """
    Return the RV pen "width" as one value for each point. RV stores one entry
    for each point (modulated by pressure). Older data can hold a single value
    or a bare float.
    """
    if isinstance(raw, (int, float)):
        widths = [float(raw)]
    else:
        widths = [float(w) for w in (raw or [])]
    if not widths:
        widths = [0.01]
    if len(widths) < count:
        widths += [widths[-1]] * (count - len(widths))
    return widths[:count]


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
    """
    Return the length of one frame in seconds, as the player of Kitsu computes
    it: 1 / fps rounded to 4 decimals (30 fps -> 0.0333, 24 fps -> 0.0417).
    """
    return round(1.0 / _check_fps(fps), 4)


def frame_to_time(frame_num: int, fps: float, frame_base: int = 1) -> float:
    """
    Return the "time" of a Kitsu record for a frame number.

    The unit is SECONDS from the first frame, at the production frame rate.
    The function uses the rounded frame duration of Kitsu. Thus the values
    are the same as the values that Kitsu writes (frame 34 at 30 fps ->
    1.0989).
    """
    return round((frame_num - frame_base) * kitsu_frame_duration(fps), 4)


def time_to_frame(time_s: float, fps: float, frame_base: int = 1) -> int:
    """
    Inverse of `frame_to_time`: convert a Kitsu "time" in seconds to a frame.
    """
    return frame_base + round(float(time_s) / kitsu_frame_duration(fps))


def _iso_ms(epoch_ms: int) -> str:
    dt = _dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Fabric.js box geometry (ellipses and rects)
# ---------------------------------------------------------------------------

_ORIGIN_FACTOR = {
    "left": 0.0,
    "top": 0.0,
    "center": 0.5,
    "right": 1.0,
    "bottom": 1.0,
}


def _origin_factor(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return _ORIGIN_FACTOR.get(str(value), 0.0)


def _size_fallback(obj: Dict[str, Any], key: str) -> float:
    """
    Return the full width and height of an object that has no "width" or
    "height" field. Only an ellipse can get its size from its radii. The rx/ry
    of a rect are corner radii, not its size.
    """
    if obj.get("type") not in KITSU_ELLIPSE_TYPES:
        return 0.0
    return 2.0 * float(obj.get(key, obj.get("radius", 0)) or 0)


def _fabric_scale(obj: Dict[str, Any]) -> Point:
    return (
        abs(float(obj.get("scaleX", 1) or 1)),
        abs(float(obj.get("scaleY", 1) or 1)),
    )


def _fabric_box_dims(obj: Dict[str, Any], w: float, h: float) -> Point:
    """
    Return the width and height of the scaled box of an object, stroke
    included. Fabric uses this box to position the object.
    """
    sx, sy = _fabric_scale(obj)
    sw = float(obj.get("strokeWidth") or 0)
    if obj.get("strokeUniform"):
        return w * sx + sw, h * sy + sw
    return (w + sw) * sx, (h + sw) * sy


def _fabric_stroke_width(obj: Dict[str, Any]) -> float:
    """
    Return the rendered stroke width. Without strokeUniform, the outline scales
    with the object, and RV has only one width.
    """
    sw = float(obj.get("strokeWidth") or 0)
    if obj.get("strokeUniform"):
        return sw
    sx, sy = _fabric_scale(obj)
    return sw * (sx + sy) / 2.0


def _fabric_box_geometry(
    obj: Dict[str, Any],
) -> Tuple[float, float, float, float, float]:
    """
    Return (cx, cy, half width, half height, rendered stroke width) of a Fabric
    "circle", "ellipse" or "rect". The values are in canvas px. They are
    measured along the centre line of the stroke (for an ellipse, its radii).

    Fabric positions an object by its stroke-inclusive box. The box is (width +
    strokeWidth) * scale, or width * scale + strokeWidth with strokeUniform.
    left/top is the origin point of the box (originX/originY). The box is
    rotated by `angle` about this point.
    """
    sx, sy = _fabric_scale(obj)

    w = obj.get("width")
    h = obj.get("height")
    if w is None:
        w = _size_fallback(obj, "rx")
    if h is None:
        h = _size_fallback(obj, "ry")
    w, h = float(w), float(h)

    cx, cy = _fabric_center(obj, *_fabric_box_dims(obj, w, h))
    return cx, cy, w * sx / 2.0, h * sy / 2.0, _fabric_stroke_width(obj)


def _fabric_center(obj: Dict[str, Any], box_w: float, box_h: float) -> Point:
    """
    Return the centre, in canvas px, of a Fabric object. Its scaled,
    stroke-inclusive box is box_w x box_h. left/top is the origin point of the
    box (originX/originY). The box is rotated by `angle` about this point.
    """
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
    """
    Return the annotation text without the typing cursor of RV or other control
    characters. Line breaks and tabs stay in the text.
    """
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _TEXT_CONTROL_RE.sub("", text)


def _fabric_text_height(
    font_px: float, line_height: float, n_lines: int
) -> float:
    """
    Return the height of a uniformly styled text, as FabricText.calcTextHeight
    does. Each line except the last has the spacing lineHeight.
    """
    line = font_px * FABRIC_FONT_SIZE_MULT
    return line * (line_height * (max(1, n_lines) - 1) + 1)


def _fabric_baseline_offset(font_px: float) -> float:
    """
    Return the distance from the top of the first line of a Fabric text to its
    baseline.
    """
    return font_px * FABRIC_FONT_SIZE_MULT * (1.0 - FABRIC_FONT_SIZE_FRACTION)


def _fabric_text_anchor(obj: Dict[str, Any]) -> Tuple[float, float, float]:
    """
    Return (x, y, rendered font size), in canvas px, of a Fabric text. x/y is
    the left end of the baseline of the first line. RV anchors text there.

    Fabric positions text like any object: by its box with strokeWidth added
    around it. It does this even when the text has no stroke. Fabric draws the
    baseline of the first line at a fixed fraction of the line height below the
    top of the box without this strokeWidth.
    """
    sx, sy = _fabric_scale(obj)
    font_px = float(obj.get("fontSize") or 16)
    lines = _clean_text(obj.get("text")).split("\n")

    w = obj.get("width")
    h = obj.get("height")
    if w is None:
        w = max(len(line) for line in lines) * _ARIAL_AVG_ADVANCE_EM * font_px
    if h is None:
        h = _fabric_text_height(
            font_px,
            float(obj.get("lineHeight") or KITSU_TEXT_LINE_HEIGHT),
            len(lines),
        )
    w, h = float(w), float(h)

    cx, cy = _fabric_center(obj, *_fabric_box_dims(obj, w, h))

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
    """
    Return an RV 2D point. The input is flat ([x, y], as RV returns text
    positions) or nested ([[x, y]], as for the positions of other shapes).
    """
    if value and isinstance(value[0], (list, tuple)):
        value = value[0]
    return float(value[0]), float(value[1])


def _rv_scalar(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return default if value is None else float(value)


def _rv_text_font_size(props: Dict[str, Any]) -> float:
    """
    Return the height of an RV text in RV units. It is the "fontSize". For
    sessions written before RV had this field, it is the legacy size * scale *
    12.5.
    """
    font_size = _rv_scalar(props.get("fontSize"))
    if font_size is not None and font_size > 0:
        return font_size
    size = _rv_scalar(props.get("size"), 0.01)
    scale = _rv_scalar(props.get("scale"), 1.0)
    return size * scale * RV_LEGACY_TEXT_SIZE_FACTOR


def _fabric_line_endpoints(obj: Dict[str, Any]) -> Tuple[Point, Point, float]:
    """
    Return ((tail x, y), (head x, y), rendered stroke width), in canvas px.

    The object is a Fabric object based on Line, such as the "arrow" of Kitsu.

    Fabric keeps the true position of a line in its box (left/top/width/height,
    scale, angle, flips). The x1..y2 fields only show which corner is which
    end. Fabric does not update them when the object is moved, scaled or
    rotated. So the function rebuilds the ends from the box. It uses x1..y2
    directly only when the object has no box fields.
    """
    x1, y1 = float(obj.get("x1", 0)), float(obj.get("y1", 0))
    x2, y2 = float(obj.get("x2", 0)), float(obj.get("y2", 0))
    if obj.get("left") is None or obj.get("top") is None:
        return (x1, y1), (x2, y2), float(obj.get("strokeWidth") or 0)

    sx = float(obj.get("scaleX", 1) or 1)
    sy = float(obj.get("scaleY", 1) or 1)
    w = float(obj["width"]) if obj.get("width") is not None else abs(x2 - x1)
    h = float(obj["height"]) if obj.get("height") is not None else abs(y2 - y1)

    box_w, box_h = _fabric_box_dims(obj, w, h)

    a = math.radians(float(obj.get("angle") or 0))
    cos_a, sin_a = math.cos(a), math.sin(a)

    def rotate(dx: float, dy: float) -> Point:
        return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a

    ox, oy = rotate(
        (0.5 - _origin_factor(obj.get("originX", "left"))) * box_w,
        (0.5 - _origin_factor(obj.get("originY", "top"))) * box_h,
    )
    cx, cy = float(obj["left"]) + ox, float(obj["top"]) + oy

    # Offset of the tail from the centre in object space. The head is opposite.
    dx = (-1.0 if x1 <= x2 else 1.0) * w / 2.0 * sx
    dy = (-1.0 if y1 <= y2 else 1.0) * h / 2.0 * sy
    if obj.get("flipX"):
        dx = -dx
    if obj.get("flipY"):
        dy = -dy
    tx, ty = rotate(dx, dy)
    return (cx + tx, cy + ty), (cx - tx, cy - ty), _fabric_stroke_width(obj)


# ---------------------------------------------------------------------------
# Fabric.js transforms
# ---------------------------------------------------------------------------

# Affine matrices use the Fabric form [a, b, c, d, e, f]:
#   x' = a*x + c*y + e,   y' = b*x + d*y + f
Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m: Matrix, n: Matrix) -> Matrix:
    """
    Return m * n: apply n first, then m (multiplyTransformMatrices of Fabric).
    """
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def _mat_inv(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if not det:
        return _IDENTITY
    return (
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    )


def _mat_apply(m: Matrix, x: float, y: float) -> Point:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _mat_translate(x: float, y: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, float(x), float(y))


def _mat_scale_factor(m: Matrix) -> float:
    """
    Return the average factor by which `m` scales lengths (a stroke width gets
    this factor).
    """
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def _qr_decompose(
    m: Matrix,
) -> Tuple[float, float, float, float, float, float]:
    """
    Return (angle, scaleX, scaleY, skewX, translateX, translateY) of `m`, with
    angles in degrees. This is how qrDecompose of Fabric splits a matrix into
    the transform options of an object.
    """
    a, b, c, d, e, f = m
    denom = a * a + b * b
    if not denom:
        return 0.0, 1.0, 1.0, 0.0, e, f
    scale_x = math.sqrt(denom)
    scale_y = (a * d - c * b) / scale_x
    skew_x = math.atan2(a * c + b * d, denom)
    return (
        math.degrees(math.atan2(b, a)),
        scale_x,
        scale_y,
        math.degrees(skew_x),
        e,
        f,
    )


def _fabric_size(obj: Dict[str, Any]) -> Point:
    """
    Return the unscaled width and height of a Fabric object, without its
    stroke.
    """
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


def _fabric_matrix(
    obj: Dict[str, Any], size: Optional[Point] = None
) -> Matrix:
    """
    Return the calcOwnMatrix of Fabric.

    This matrix maps the plane of an object into the plane of its parent. The
    origin of the object plane is at its centre. The parent is the canvas, or
    the group that holds the object.

    `size` overrides the width and height of the object (the path data gives
    the size of paths). The box size does not include skew. This matters only
    for skewed objects, and Kitsu does not make them.
    """
    w, h = size if size is not None else _fabric_size(obj)
    cx, cy = _fabric_center(obj, *_fabric_box_dims(obj, w, h))
    sx = float(obj.get("scaleX", 1) or 1) * (-1.0 if obj.get("flipX") else 1.0)
    sy = float(obj.get("scaleY", 1) or 1) * (-1.0 if obj.get("flipY") else 1.0)
    a = math.radians(float(obj.get("angle") or 0))

    m = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), cx, cy)
    m = _mat_mul(m, (sx, 0.0, 0.0, sy, 0.0, 0.0))
    skew_x = float(obj.get("skewX") or 0)
    if skew_x:
        m = _mat_mul(
            m, (1.0, 0.0, math.tan(math.radians(skew_x)), 1.0, 0.0, 0.0)
        )
    skew_y = float(obj.get("skewY") or 0)
    if skew_y:
        m = _mat_mul(
            m, (1.0, math.tan(math.radians(skew_y)), 0.0, 1.0, 0.0, 0.0)
        )
    return m


def _fabric_aabb(obj: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """
    Return (x0, y0, x1, y1): the axis-aligned box of the stroke-inclusive
    outline of an object, in the coordinates of its parent. Fabric tests
    against this box when it decides which objects an eraser stroke touches.
    """
    w, h = _fabric_size(obj)
    sw = float(obj.get("strokeWidth") or 0)
    if obj.get("strokeUniform"):
        # The stroke is not scaled. So in the own (unscaled) plane of the
        # object, it is sw / scale wide.
        sx, sy = _fabric_scale(obj)
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


def _points_bbox(
    points: Sequence[Point], pad: float = 0.0
) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


# ---------------------------------------------------------------------------
# Fabric.js path data
# ---------------------------------------------------------------------------

# A parsed path is a list of subpaths. Each subpath starts with ("M", p). It
# continues with ("L", p), ("Q", ctl, p) or ("C", ctl1, ctl2, p) segments.
Segment = Tuple[Any, ...]

_PATH_TOKEN_RE = re.compile(
    r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
)
_PATH_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4, "C": 6, "Z": 0}


def _path_commands(path: Any) -> List[List[Any]]:
    """
    Return Fabric path data as [command, *numbers] lists. Fabric stores the
    parsed form (lists). The function tokenizes an SVG path string into the
    same form.
    """
    if isinstance(path, str):
        commands: List[List[Any]] = []
        for token in _PATH_TOKEN_RE.findall(path):
            if token.isalpha():
                commands.append([token])
            elif commands:
                commands[-1].append(float(token))
        return commands
    return [
        list(seg)
        for seg in (path or [])
        if isinstance(seg, (list, tuple)) and seg
    ]


def _parse_fabric_path(path: Any) -> List[List[Segment]]:
    """
    Convert Fabric path data to subpaths of absolute segments. Fabric stores
    absolute M/L/Q/C/Z itself. The function also reads relative commands and
    H/V.
    """
    subpaths: List[List[Segment]] = []
    current: Optional[List[Segment]] = None
    cur: Optional[Point] = None
    start: Optional[Point] = None

    for seg in _path_commands(path):
        cmd = str(seg[0])
        upper = cmd.upper()
        arity = _PATH_ARITY.get(upper)
        if arity is None:
            _warn(
                f"unsupported path command {cmd!r} in a Kitsu eraser, "
                f"skipping it"
            )
            continue
        if upper == "Z":
            if (
                current is not None
                and cur is not None
                and start is not None
                and cur != start
            ):
                current.append(("L", start))
            cur, current = start, None
            continue
        try:
            nums = [float(v) for v in seg[1:]]
        except (TypeError, ValueError):
            continue

        for i in range(0, len(nums) - arity + 1, arity):
            group = nums[i : i + arity]
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
                current.append(
                    ("C", pt(group[0], group[1]), pt(group[2], group[3]), p)
                )
            cur = p
    return subpaths


def _quad_extrema(a: float, c: float, b: float) -> List[float]:
    """
    Return the interior extreme values of a 1-D quadratic Bezier a -> (ctl c)
    -> b.
    """
    denom = a - 2.0 * c + b
    if denom == 0:
        return []
    t = (a - c) / denom
    if 0.0 < t < 1.0:
        return [(1 - t) ** 2 * a + 2 * (1 - t) * t * c + t * t * b]
    return []


def _cubic_extrema(p0: float, p1: float, p2: float, p3: float) -> List[float]:
    """
    Return the interior extreme values of a 1-D cubic Bezier.
    """
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
        (1 - t) ** 3 * p0
        + 3 * (1 - t) ** 2 * t * p1
        + 3 * (1 - t) * t * t * p2
        + t**3 * p3
        for t in roots
        if 0.0 < t < 1.0
    ]


def _path_bounds(
    subpaths: Sequence[Sequence[Segment]],
) -> Tuple[float, float, float, float]:
    """
    Return (minX, minY, width, height) of the curves, as Fabric computes the
    size of a path and its pathOffset (the centre of these bounds).
    """
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
        pts = [
            ((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
            for a, b in zip(pts, pts[1:])
        ]
    return pts[0]


def _flatten_subpath(subpath: Sequence[Segment]) -> List[Point]:
    """
    Return a subpath as a polyline, in the coordinates of the path. The
    sampling depends only on the path data. So copies of the same eraser stroke
    on different objects flatten to the same points.
    """
    cur = subpath[0][1]
    points = [cur]
    for seg in subpath[1:]:
        p = seg[-1]
        if seg[0] != "L":
            ctl = [cur, *seg[1:]]
            length = sum(math.dist(a, b) for a, b in zip(ctl, ctl[1:]))
            steps = max(
                1, min(FLATTEN_MAX_STEPS, math.ceil(length / FLATTEN_STEP_PX))
            )
            points.extend(
                _bezier_point(ctl, i / steps) for i in range(1, steps)
            )
        points.append(p)
        cur = p
    return points


def _brush_path_commands(points: Sequence[Point]) -> List[List[Any]]:
    """
    Return a polyline as the path data that the pencil/eraser brush of Fabric
    makes from its points: quadratic curves through the midpoints of the
    segments. The raw point between two midpoints controls each curve. The path
    ends on the last point.
    """
    pts = [(_clean(x, 4), _clean(y, 4)) for x, y in points]
    if len(pts) == 1:
        return [["M", *pts[0]], ["L", *pts[0]]]
    commands: List[List[Any]] = [["M", *pts[0]]]
    for p1, p2 in zip(pts, pts[1:]):
        if p1 != p2:
            commands.append(
                [
                    "Q",
                    p1[0],
                    p1[1],
                    _clean((p1[0] + p2[0]) / 2.0, 4),
                    _clean((p1[1] + p2[1]) / 2.0, 4),
                ]
            )
    commands.append(["L", *pts[-1]])
    return commands


# ---------------------------------------------------------------------------
# Erasers
# ---------------------------------------------------------------------------
#
# An eraser stroke is a dict {"points": [(x, y), ...], "width": w}. Units are
# canvas px on the Kitsu side and RV units on the RV side.


def _is_erasable(obj: Dict[str, Any]) -> bool:
    """
    Kitsu draws text with erasable=False. Erasers never cut it.
    """
    return obj.get("erasable", True) is not False


def _same_eraser_stroke(
    a: Dict[str, Any], b: Dict[str, Any], tol: float
) -> bool:
    pa, pb = a["points"], b["points"]
    if len(pa) != len(pb) or not pa or abs(a["width"] - b["width"]) > tol:
        return False
    return all(
        abs(xa - xb) <= tol and abs(ya - yb) <= tol
        for (xa, ya), (xb, yb) in zip(pa, pb)
    )


def _fabric_path_strokes(
    path_obj: Dict[str, Any], parent: Matrix
) -> List[Dict[str, Any]]:
    """
    Return an eraser path as canvas-px strokes, one for each subpath. `parent`
    maps the plane of the path (the plane of its eraser group) to the canvas.
    """
    subpaths = _parse_fabric_path(path_obj.get("path"))
    if not subpaths:
        return []
    min_x, min_y, w, h = _path_bounds(subpaths)
    # Fabric draws the path data with an offset of pathOffset (the centre of
    # its bounds), from the own centre of the path object.
    total = _mat_mul(
        _mat_mul(parent, _fabric_matrix(path_obj, (w, h))),
        _mat_translate(-(min_x + w / 2.0), -(min_y + h / 2.0)),
    )
    width_px = float(path_obj.get("strokeWidth") or 0) * _mat_scale_factor(
        total
    )
    strokes = []
    for subpath in subpaths:
        points = [
            _mat_apply(total, x, y) for x, y in _flatten_subpath(subpath)
        ]
        if points:
            strokes.append({"points": points, "width": width_px})
    return strokes


def _fabric_eraser_entries(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return the eraser paths stored on a Kitsu object, in the order that Kitsu
    made them: [{"path": path object, "direct": a direct child of the eraser
    group, "strokes": its strokes in canvas px}, ...].
    """
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
                operation = (
                    child.get("globalCompositeOperation")
                    or KITSU_ERASE_OPERATION
                )
                if operation != KITSU_ERASE_OPERATION:
                    # The "inverted" eraser of Fabric restores what it erased.
                    # RV has nothing like it.
                    _warn(
                        f"skipping eraser path on {obj.get('id')!r} drawn "
                        f"with "
                        f"{operation!r}; OpenRV erasers can only erase"
                    )
                    continue
                strokes = _fabric_path_strokes(child, matrix)
                if strokes:
                    entries.append(
                        {"path": child, "direct": direct, "strokes": strokes}
                    )
            elif isinstance(child.get("objects"), list):
                walk(child, matrix, False)
            else:
                _warn(
                    f"skipping unsupported {child.get('type')!r} in the "
                    f"eraser "
                    f"of {obj.get('id')!r}"
                )

    walk(eraser, _fabric_matrix(obj), True)
    return entries


def _new_eraser_group(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return an empty eraser group for `obj`, as Kitsu makes them: it is centred
    on the object and has the size of the stroke-inclusive box of the object.
    """
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


def _build_eraser_path(
    stroke: Dict[str, Any], parent: Matrix
) -> Dict[str, Any]:
    """
    Convert a canvas-px eraser stroke to a Fabric eraser path for a group. The
    plane `parent` of the group maps to the canvas. As in Kitsu, the path data
    stays in canvas coordinates. The transform of the path cancels the
    transform of the object.
    """
    commands = _brush_path_commands(stroke["points"])
    min_x, min_y, w, h = _path_bounds(_parse_fabric_path(commands))
    offset = (min_x + w / 2.0, min_y + h / 2.0)

    # The matrix M of the path must satisfy parent * M * T(-offset) = identity.
    angle, sx, sy, skew_x, cx, cy = _qr_decompose(
        _mat_mul(_mat_inv(parent), _mat_translate(*offset))
    )
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
    strokes: Sequence[Dict[str, Any]],
    entries: Sequence[Dict[str, Any]],
    tol: float,
) -> bool:
    """
    Return True if `strokes` are exactly the strokes of `entries`, in any order
    (erasers that cut the same object can be applied in either order).
    """
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
    obj: Dict[str, Any],
    ref: Optional[Dict[str, Any]],
    strokes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Return the Kitsu object to send for a converted shape. It is `obj` (or
    `ref`, the Kitsu object of which `obj` is an unchanged copy) with an eraser
    made of `strokes`. `strokes` are the RV eraser strokes (canvas px) that cut
    it.

    An unchanged eraser stays as it is on Kitsu. The function rebuilds a
    changed eraser. It reuses the paths of the strokes that were already there.
    """
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
            if (
                not used[k]
                and n
                and i + n <= len(strokes)
                and all(
                    _same_eraser_stroke(s, r, tol)
                    for s, r in zip(strokes[i : i + n], entry["strokes"])
                )
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
    """
    Return True for an RV pen drawn in erase mode.
    """
    if shape.get("type") != "pen":
        return False
    mode = _rv_scalar(
        shape.get("properties", {}).get("mode"), RV_PEN_MODE_PAINT
    )
    return mode == RV_PEN_MODE_ERASE


def _rv_eraser_stroke(
    shape: Dict[str, Any],
    space: _PaintSpace,
) -> Optional[Dict[str, Any]]:
    """
    Return an RV eraser pen as a canvas-px stroke, or None if it has no points.
    Fabric eraser paths have one width, so the function uses the width of the
    widest point.
    """
    props = shape["properties"]
    points = [
        (_clean(x), _clean(y))
        for x, y in (
            rv_normalized_to_pixel(nx, ny, space)
            for nx, ny in (props.get("points") or [])
        )
    ]
    if not points:
        return None
    width_px = max(
        rv_width_to_px(w, space.canvas_height)
        for w in _per_point_widths(props.get("width"), len(points))
    )
    return {
        "points": points,
        "width": width_px,
        "bbox": _points_bbox(points, width_px / 2.0),
    }


def _eraser_pen(eraser: Dict[str, Any], frame_num: int) -> Dict[str, Any]:
    """
    Return an RV eraser pen shape for a deduplicated Kitsu eraser stroke.
    """
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
    items: Sequence[Dict[str, Any]],
    erasers: Sequence[Dict[str, Any]],
    frame_num: int,
) -> List[Dict[str, Any]]:
    """
    Interleave the converted shapes of a frame with its eraser pens.

    An RV eraser cuts everything drawn before it. So it must come after every
    object that it erased in Kitsu. It must come before every object that it
    overlaps but did not erase (drawn after it, or not erasable, like text).
    Kitsu also moves an object to the end of its frame when it updates the
    object, for example by erasing it. So the function cannot rely on the
    stored order alone.

    Within these rules, the function keeps the Kitsu order: each eraser sits
    right after the last object that it erased.
    """
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
            elif item["bbox"] is not None and _boxes_overlap(
                item["bbox"], eraser["bbox"]
            ):
                add_edge(key, obj_key)

    heap = [key for key in nodes if not indeg.get(key)]
    heapq.heapify(heap)
    remaining = set(nodes)
    order: List[Dict[str, Any]] = []
    while remaining:
        if not heap:
            # Contradictory erasers (each must cut the object of the other but
            # spare its own object): keep the Kitsu order for the rest.
            key = min(remaining)
            _warn(
                f"erasers on frame {frame_num} can't all be ordered so "
                f"they cut only what they cut in Kitsu"
            )
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
    box: Sequence[float],
    space: _PaintSpace,
) -> Tuple[float, float, float, float]:
    """
    Return a canvas-px box in RV normalized coordinates (still min/max).
    """
    x0, y0 = pixel_to_rv_normalized(box[0], box[1], space)
    x1, y1 = pixel_to_rv_normalized(box[2], box[3], space)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


# ---------------------------------------------------------------------------
# OpenRV -> Kitsu
# ---------------------------------------------------------------------------


def _fabric_base(
    obj_type: str,
    left: float,
    top: float,
    width: float,
    height: float,
    stroke_hex: str,
    stroke_width: float,
    opacity: float,
    author: str,
    canvas_width: float,
    canvas_height: float,
    source_uuid: Optional[str],
) -> Dict[str, Any]:
    """
    Return the Fabric.js boilerplate that all converted objects share, for any
    shape type.
    """
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


def _psstroke_bounds(
    points: Sequence[Point],
) -> Tuple[float, float, float, float]:
    """
    Return (minX, minY, width, height) exactly as PSStroke._parseDimensions of
    fabricjs-psbrush computes them. The stroke is drawn as quadratic curves
    between point midpoints. So the bounds are the bounds of the curves, not of
    the points (the curves never reach the last raw point).
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


def _pen_to_fabric(
    shape: Dict[str, Any],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    points_px = [
        rv_normalized_to_pixel(nx, ny, space) for nx, ny in props["points"]
    ]
    points_px = [(_clean(x), _clean(y)) for x, y in points_px]

    # psbrush renders each segment at pressure * strokeWidth. RV keeps that
    # product for each point. So split it back into a base width (the widest
    # point) and a 0..1 pressure for each point. The rendered width is the
    # same.
    widths_px = [
        rv_width_to_px(w, space.canvas_height)
        for w in _per_point_widths(props.get("width"), len(points_px))
    ]
    stroke_width_px = _clean(max(widths_px) if widths_px else 1.0)
    pressures = [
        round(w / stroke_width_px, 6) if stroke_width_px else 1
        for w in widths_px
    ]

    min_x, min_y, bbox_w, bbox_h = _psstroke_bounds(points_px)
    # psbrush positions the object by its stroke-inclusive box. So left/top are
    # half a stroke width outside the curve bounds.
    left = min_x - stroke_width_px / 2.0
    top = min_y - stroke_width_px / 2.0

    start_time = clock_ms[0]
    duration = max(1, len(points_px) * MS_PER_STROKE_POINT)
    end_time = start_time + duration
    clock_ms[0] = end_time

    obj = _fabric_base(
        "PSStroke",
        _clean(left),
        _clean(top),
        _clean(bbox_w),
        _clean(bbox_h),
        rv_color_to_hex(props.get("color", [])),
        stroke_width_px,
        _clean(rv_alpha(props.get("color", [])), 6),
        author,
        space.canvas_width,
        space.canvas_height,
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
    shape: Dict[str, Any],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> Dict[str, Any]:
    props = shape["properties"]
    (sx, sy), (ex, ey) = props["startPos"][0], props["endPos"][0]
    x1, y1 = rv_normalized_to_pixel(sx, sy, space)
    x2, y2 = rv_normalized_to_pixel(ex, ey, space)

    left, top = min(x1, x2), min(y1, y2)
    bbox_w, bbox_h = abs(x2 - x1), abs(y2 - y1)
    stroke_width_px = props.get("borderWidth", 0.01) * space.canvas_height

    start_time = clock_ms[0]
    end_time = start_time + MS_PER_STROKE_POINT * 2
    clock_ms[0] = end_time

    obj = _fabric_base(
        "line",
        left,
        top,
        bbox_w,
        bbox_h,
        rv_color_to_hex(props.get("borderColor", [])),
        round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author,
        space.canvas_width,
        space.canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["x1"], obj["y1"], obj["x2"], obj["y2"] = x1, y1, x2, y2
    return obj


def _kitsu_shape_style(clock_ms: List[int]) -> Dict[str, Any]:
    """
    Return the fields that Kitsu writes on the shapes it draws (not on brush
    strokes). Each call uses one tick of `clock_ms`, so the timestamps stay
    different.
    """
    style = {
        "strokeLineCap": "butt",
        "strokeLineJoin": "miter",
        "strokeMiterLimit": 4,
        "createdAt": _iso_ms(clock_ms[0]),
    }
    clock_ms[0] += 1
    return style


def _rv_box_px(
    props: Dict[str, Any],
    space: _PaintSpace,
) -> Tuple[float, float, float, float, Any]:
    """
    Return (x0, y0, x1, y1, stroke width) in canvas px of an RV ellipse or
    rect. RV stores it as the axis-aligned min/max corners of the box on which
    its outline is centred. x0/y0 is the top-left corner of the canvas.
    """
    p0 = rv_normalized_to_pixel(*props["min"][0], space)
    p1 = rv_normalized_to_pixel(*props["max"][0], space)
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))
    stroke_width_px = _clean(
        rv_width_to_px(props.get("borderWidth", 0.01), space.canvas_height)
    )
    return x0, y0, x1, y1, stroke_width_px


def _rv_fill(props: Dict[str, Any]) -> str:
    """
    Return the Fabric "fill" for an RV innerColor ("transparent" when it has
    none).
    """
    inner = props.get("innerColor")
    return (
        rv_color_to_hex(inner)
        if inner and rv_alpha(inner) > 0
        else "transparent"
    )


def _ellipse_to_fabric(
    shape: Dict[str, Any],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> Dict[str, Any]:
    """
    Convert an RV "ellipse" (axis-aligned, given by its min/max corners) to the
    Fabric "circle" that Kitsu uses to draw ellipses. A non-round ellipse
    becomes a circle of the larger radius, squashed with scaleX/scaleY.
    strokeUniform is on, so the outline keeps the same thickness all round.
    """
    props = shape["properties"]
    x0, y0, x1, y1, stroke_width_px = _rv_box_px(props, space)
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
        "circle",
        _clean(cx - box_w / 2.0),
        _clean(cy - box_h / 2.0),
        _clean(diameter),
        _clean(diameter),
        rv_color_to_hex(border),
        stroke_width_px,
        _clean(rv_alpha(border), 6),
        author,
        space.canvas_width,
        space.canvas_height,
        props.get("uuid"),
    )
    obj.update(
        {
            "fill": _rv_fill(props),
            "radius": _clean(radius),
            "scaleX": _clean(scale_x, 6),
            "scaleY": _clean(scale_y, 6),
            "startAngle": 0,
            "endAngle": 360,
            "counterClockwise": False,
            "strokeUniform": uniform_stroke,
            **_kitsu_shape_style(clock_ms),
        }
    )
    return obj


def _rect_to_fabric(
    shape: Dict[str, Any],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> Dict[str, Any]:
    """
    Convert an RV "rect" (axis-aligned, given by its min/max corners) to a
    Fabric "rect" as Kitsu draws it: unscaled, square-cornered, and positioned
    by its stroke-inclusive box. So left/top are half a stroke outside the
    centre line of the outline.
    """
    props = shape["properties"]
    x0, y0, x1, y1, stroke_width_px = _rv_box_px(props, space)
    half_sw = stroke_width_px / 2.0

    border = props.get("borderColor", [])
    obj = _fabric_base(
        "rect",
        _clean(x0 - half_sw),
        _clean(y0 - half_sw),
        _clean(x1 - x0),
        _clean(y1 - y0),
        rv_color_to_hex(border),
        stroke_width_px,
        _clean(rv_alpha(border), 6),
        author,
        space.canvas_width,
        space.canvas_height,
        props.get("uuid"),
    )
    obj.update(
        {
            "fill": _rv_fill(props),
            "rx": 0,
            "ry": 0,
            **_kitsu_shape_style(clock_ms),
        }
    )
    return obj


def _rv_arrow_width(props: Dict[str, Any]) -> float:
    """
    Return the line width of an RV arrow in RV units. RV writes it twice, as
    "thickness" and "borderWidth". When both are present, thickness is used.
    """
    for key in ("thickness", "borderWidth"):
        value = props.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is not None:
            return float(value)
    return 0.01


def _arrow_to_fabric(
    shape: Dict[str, Any],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> Dict[str, Any]:
    """
    Convert an RV "arrow" (tail startPos -> head endPos) to a Kitsu "arrow": a
    Fabric Line from x1/y1 (tail) to x2/y2 (head). It is positioned by its
    stroke-inclusive box. So left/top are half a stroke outside the ends.
    """
    props = shape["properties"]
    x1, y1 = rv_normalized_to_pixel(*props["startPos"][0], space)
    x2, y2 = rv_normalized_to_pixel(*props["endPos"][0], space)
    stroke_width_px = _clean(
        rv_width_to_px(_rv_arrow_width(props), space.canvas_height)
    )
    half_sw = stroke_width_px / 2.0

    border = props.get("borderColor", [])
    obj = _fabric_base(
        "arrow",
        _clean(min(x1, x2) - half_sw),
        _clean(min(y1, y2) - half_sw),
        _clean(abs(x2 - x1)),
        _clean(abs(y2 - y1)),
        rv_color_to_hex(border),
        stroke_width_px,
        _clean(rv_alpha(border), 6),
        author,
        space.canvas_width,
        space.canvas_height,
        props.get("uuid"),
    )
    obj.update(
        {
            "x1": _clean(x1),
            "y1": _clean(y1),
            "x2": _clean(x2),
            "y2": _clean(y2),
            "fill": "transparent",
            "arrowHeadSize": KITSU_ARROW_HEAD_SIZE,
            "arrowHeadWidth": KITSU_ARROW_HEAD_WIDTH,
            **_kitsu_shape_style(clock_ms),
        }
    )
    return obj


def _text_to_fabric(
    shape: Dict[str, Any],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> Optional[Dict[str, Any]]:
    """
    Convert an RV "text" to a Kitsu "i-text". RV anchors the text at the left
    end of its first baseline and sizes it as a fraction of the image height.
    The "i-text" is positioned by the top-left of its box, as Fabric lays out
    text. Return None for a text with nothing in it (for example, a text placed
    in RV but never typed into).
    """
    props = shape["properties"]
    text = _clean_text(props.get("text"))
    if not text.strip():
        _warn(f"skipping empty OpenRV text {shape.get('name')!r}")
        return None

    ax, ay = rv_normalized_to_pixel(
        *_rv_point(props["position"]),
        space,
    )
    font_px = _clean(
        rv_width_to_px(_rv_text_font_size(props), space.canvas_height)
    )
    lines = text.split("\n")
    # Default strokeWidth of Fabric. It pads the box of a text that has no
    # stroke.
    stroke_width = 1
    half_sw = stroke_width / 2.0

    color = props.get("color", [])
    box_w = max(len(line) for line in lines) * _ARIAL_AVG_ADVANCE_EM * font_px
    box_h = _fabric_text_height(font_px, KITSU_TEXT_LINE_HEIGHT, len(lines))
    obj = _fabric_base(
        "i-text",
        _clean(ax - half_sw, 4),
        _clean(ay - half_sw - _fabric_baseline_offset(font_px), 4),
        _clean(box_w, 4),
        _clean(box_h, 4),
        None,
        stroke_width,
        _clean(rv_alpha(color), 6),
        author,
        space.canvas_width,
        space.canvas_height,
        props.get("uuid"),
    )
    obj.update(
        {
            "text": text,
            "fill": rv_color_to_hex(color),
            "styles": [],
            "fontSize": font_px,
            "fontFamily": KITSU_TEXT_FONT_FAMILY,
            "fontWeight": (
                "bold"
                if str(props.get("fontWeight", "")).lower() == "bold"
                else "normal"
            ),
            "fontStyle": (
                "italic"
                if str(props.get("fontStyle", "")).lower() == "italic"
                else "normal"
            ),
            "underline": str(props.get("textDecoration", "")).lower()
            == "underline",
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
            **_kitsu_shape_style(clock_ms),
        }
    )
    return obj


def _infer_canvas_size(
    kitsu_records: Optional[Sequence[Dict[str, Any]]],
    fallback_width: float,
    fallback_height: float,
) -> Tuple[float, float]:
    """
    Recover, on a best-effort basis, the Fabric.js canvas size that Kitsu uses
    for the annotations of a preview. The function reads it from any object
    that already exists. It does not assume that the size is the same as the
    raw video resolution (they can differ). It returns (fallback_width,
    fallback_height) only if no object has the field, for example a preview
    with no annotations yet.
    """
    for record in kitsu_records or []:
        for obj in record.get("drawing", {}).get("objects", []):
            cw, ch = obj.get("canvasWidth"), obj.get("canvasHeight")
            if cw and ch:
                return float(cw), float(ch)
    return float(fallback_width), float(fallback_height)


# ---------------------------------------------------------------------------
# Match converted objects to Kitsu objects
# ---------------------------------------------------------------------------


def _rendered_stroke(obj: Dict[str, Any]) -> List[Tuple[float, float, float]]:
    """
    Return (x, y, rendered width) for each point of a PSStroke.
    """
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
    """
    Return a comparable form of a Fabric color. Return None for "no color".
    """
    if not _has_color(value):
        return None
    if isinstance(value, str):
        return value.strip().lower().lstrip("#")
    return tuple(int(round(c)) for c in value[:4])


def _same_box(
    a: Dict[str, Any], b: Dict[str, Any], types: Sequence[str]
) -> bool:
    """
    Return True for the same ellipse or rect: same outline and fill color, and
    same box after the scale, stroke and origin conventions of Fabric are
    resolved.
    """
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
    """
    Return True for the same arrow: same color, and same tail, head and line
    width after the box, scale and rotation of Fabric are resolved.
    """
    if b.get("type") != "arrow":
        return False
    if _color_key(a.get("stroke")) != _color_key(b.get("stroke")):
        return False
    ta, ha, wa = _fabric_line_endpoints(a)
    tb, hb, wb = _fabric_line_endpoints(b)
    tol = MATCH_TOLERANCE_PX
    return abs(wa - wb) <= tol and all(
        abs(va - vb) <= tol for va, vb in zip((*ta, *ha), (*tb, *hb))
    )


def _same_text(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """
    Return True for the same text: same content and color, and same baseline
    anchor and rendered font size after the box, scale and rotation of Fabric
    are resolved.
    """
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
    """
    Return the Kitsu objects that the code can match back, grouped by frame.
    """
    index: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for record in kitsu_records or []:
        frame = record.get("frame")
        frame = int(frame) if frame is not None else None
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("type") in _MATCHERS:
                index.setdefault(frame, []).append(obj)
    return index


def _match_reference(
    obj: Dict[str, Any],
    frame_num: int,
    index: Dict[Optional[int], List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """
    Find (and consume) the Kitsu object of which this converted object is an
    unchanged round trip. RV cannot carry the author, the timestamps or the
    Kitsu-only fields of Kitsu (the strokeWidth/pressure split, the radius of a
    circle against its scale). So without this match, each sync rewrites them.
    """
    same = _MATCHERS.get(obj.get("type"))
    if same is None:
        return None
    for key in (frame_num, None):
        candidates = index.get(key, [])
        for i, ref in enumerate(candidates):
            if same(obj, ref):
                return candidates.pop(i)
    return None


# ---------------------------------------------------------------------------
# OpenRV -> Kitsu: entry point
# ---------------------------------------------------------------------------

_SHAPE_CONVERTERS = {
    "pen": _pen_to_fabric,
    "line": _line_to_fabric,
    "ellipse": _ellipse_to_fabric,
    "rect": _rect_to_fabric,
    "arrow": _arrow_to_fabric,
    "text": _text_to_fabric,
}


def _convert_frame_shapes(
    shapes: Sequence[Dict[str, Any]],
    space: _PaintSpace,
    author: str,
    clock_ms: List[int],
) -> List[Dict[str, Any]]:
    """
    Convert the RV shapes of one frame to [{"obj": converted object, "erasers":
    the RV eraser strokes that cut it}, ...], in drawing order.
    """
    entries: List[Dict[str, Any]] = []
    for shape in shapes:
        if _is_rv_eraser(shape):
            stroke = _rv_eraser_stroke(shape, space)
            if stroke is None:
                continue
            # As the eraser brush of Fabric does: the stroke goes on every
            # erasable object that is drawn before it and that it touches.
            for entry in entries:
                obj = entry["obj"]
                if _is_erasable(obj) and _boxes_overlap(
                    _fabric_aabb(obj), stroke["bbox"]
                ):
                    entry["erasers"].append(stroke)
            continue

        shape_type = shape.get("type")
        converter = _SHAPE_CONVERTERS.get(shape_type)
        if converter is None:
            _warn(
                f"skipping unsupported OpenRV shape type "
                f"'{shape_type}' (name={shape.get('name')!r})"
            )
            continue
        obj = converter(shape, space, author, clock_ms)
        if obj is not None:
            entries.append({"obj": obj, "erasers": []})
    return entries


def _resolve_frame_objects(
    entries: Sequence[Dict[str, Any]],
    frame_num: int,
    ref_index: Dict[Optional[int], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Return the Kitsu objects to send for the converted entries of a frame.
    """
    objects: List[Dict[str, Any]] = []
    for entry in entries:
        obj = entry["obj"]
        ref = _match_reference(obj, frame_num, ref_index)
        if ref is None and "createdAt" not in obj:
            obj["createdAt"] = _iso_ms(obj["endTime"])
        objects.append(_resolve_eraser(obj, ref, entry["erasers"]))
    return objects


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
    """
    Convert OpenRV paint shapes to Kitsu annotation records.

    `openrv_shapes` is a flat list of {"frame", "type", "properties", ...}
    dicts, as gathered from RVPaint nodes. Supported types: "pen" (->
    PSStroke), "line", "ellipse" (-> "circle" of Kitsu), "rect", "arrow" and
    "text" (-> "i-text"). The function skips empty texts.

    `fps` is the frame rate of the production. The function uses it to compute
    the "time" of each record, in seconds, from its frame number (see
    `frame_to_time`).

    Canvas size: an explicit `canvas_width`/`canvas_height` wins. Otherwise the
    function reads the size from any object in `reference_records` (the
    annotations that the preview already has in Kitsu). If there is none, it
    uses the media `width`/`height`.

    Pass the annotations that the preview already has as `reference_records`
    when you sync back. A stroke, ellipse, rect, arrow or text can come back
    unchanged from RV. The function then returns the original Kitsu object
    (same id, author, timestamps and Kitsu-only fields). It does not return a
    re-created copy.

    Erase-mode pens (mode 1) are not objects in Kitsu. Each one becomes an
    eraser path on every earlier erasable object of its frame that it touches.
    An unchanged eraser stays as Kitsu stored it.
    """
    fps = _check_fps(fps)
    author = author or str(uuid.uuid4())
    inferred_w, inferred_h = _infer_canvas_size(
        reference_records, width, height
    )
    space = _PaintSpace(
        width, height, canvas_width or inferred_w, canvas_height or inferred_h
    )

    by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for shape in openrv_shapes:
        if skip_soft_deleted and shape.get("properties", {}).get(
            "softDeleted"
        ):
            continue
        frame_num = shape["frame"] + frame_offset
        by_frame.setdefault(frame_num, []).append(shape)

    clock_ms = [int(_time.time() * 1000)]
    ref_index = _reference_index(reference_records)
    records: List[Dict[str, Any]] = []

    for frame_num in sorted(by_frame):
        entries = _convert_frame_shapes(
            by_frame[frame_num], space, author, clock_ms
        )
        objects = _resolve_frame_objects(entries, frame_num, ref_index)
        if objects:
            records.append(
                {
                    "time": frame_to_time(frame_num, fps, frame_base),
                    "frame": frame_num,
                    "drawing": {"objects": objects},
                }
            )

    return records


# ---------------------------------------------------------------------------
# Kitsu -> OpenRV
# ---------------------------------------------------------------------------


def _pen_from_fabric(
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    stroke_points = obj.get("strokePoints", [])
    points_norm = [
        pixel_to_rv_normalized(p["x"], p["y"], space) for p in stroke_points
    ]

    # psbrush draws each segment at pressure * strokeWidth. RV pens take a
    # width for each point. So apply the pressure to the width (RV annotate
    # mode does the same with tablet pressure). The mouse fallback pressure of
    # Kitsu is 0.5 when a point has none.
    stroke_width_px = float(obj.get("strokeWidth", 0.01 * space.canvas_height))
    widths_norm = [
        px_width_to_rv(
            stroke_width_px * float(p.get("pressure", 0.5)),
            space.canvas_height,
        )
        for p in stroke_points
    ]

    return {
        "type": "pen",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "points": points_norm,
            "width": widths_norm,
            "color": color_to_rv_color(
                obj.get("stroke"), obj.get("opacity", 1.0)
            ),
        },
    }


def _line_from_fabric(
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    x1, y1 = obj.get("x1", obj["left"]), obj.get("y1", obj["top"])
    x2 = obj.get("x2", obj["left"] + obj["width"])
    y2 = obj.get("y2", obj["top"] + obj["height"])

    start = pixel_to_rv_normalized(x1, y1, space)
    end = pixel_to_rv_normalized(x2, y2, space)

    stroke_width_px = obj.get("strokeWidth", 0.01 * space.canvas_height)
    border_width_norm = stroke_width_px / space.canvas_height

    return {
        "type": "line",
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "startPos": [start],
            "endPos": [end],
            "borderWidth": border_width_norm,
            "borderColor": color_to_rv_color(
                obj.get("stroke"), obj.get("opacity", 1.0)
            ),
        },
    }


def _box_from_fabric(
    kind: str,
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    """
    Convert a Fabric box-shaped object to an RV "ellipse" or "rect" (`kind`).
    They share one property set. RV shapes are axis-aligned, so the function
    drops any rotation (about the centre).
    """
    cx, cy, rx, ry, stroke_width_px = _fabric_box_geometry(obj)
    if float(obj.get("angle") or 0) % 360:
        _warn(
            f"{kind} {obj.get('id')!r} is rotated {obj.get('angle')} deg; "
            f"OpenRV {kind}s can't rotate, dropping the rotation"
        )

    # Canvas y goes down and RV y goes up. So the bottom-left corner of the
    # canvas is the min of RV, and the top-right corner is the max of RV.
    min_pt = pixel_to_rv_normalized(cx - rx, cy + ry, space)
    max_pt = pixel_to_rv_normalized(cx + rx, cy - ry, space)

    opacity = obj.get("opacity", 1.0)
    fill = obj.get("fill")
    inner_color = (
        color_to_rv_color(fill, opacity)
        if _has_color(fill)
        else list(_RV_NO_COLOR)
    )

    return {
        "type": kind,
        "frame": frame_num,
        "properties": {
            "uuid": obj.get("id"),
            "min": [min_pt],
            "max": [max_pt],
            "borderWidth": px_width_to_rv(
                stroke_width_px, space.canvas_height
            ),
            "borderColor": color_to_rv_color(obj.get("stroke"), opacity),
            "innerColor": inner_color,
            # The other properties that RV ellipses and rects have: shown on
            # its frame only, in both eyes, not deleted.
            "startFrame": frame_num,
            "duration": 1,
            "eye": 2,
            "softDeleted": 0,
        },
    }


def _ellipse_from_fabric(
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    """
    Convert a Kitsu "circle" (or Fabric "ellipse") to an RV "ellipse".
    """
    return _box_from_fabric("ellipse", obj, space, frame_num)


def _rect_from_fabric(
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    """
    Convert a Kitsu "rect" to an RV "rect". RV rects have square corners, so
    the function drops any corner rounding (rx/ry).
    """
    if float(obj.get("rx") or 0) or float(obj.get("ry") or 0):
        _warn(
            f"rect {obj.get('id')!r} has rounded corners "
            f"(rx={obj.get('rx')}, ry={obj.get('ry')}); OpenRV rects are "
            f"square-cornered, dropping the rounding"
        )
    return _box_from_fabric("rect", obj, space, frame_num)


def _arrow_from_fabric(
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    """
    Convert a Kitsu "arrow" to an RV "arrow", with the tail at startPos and the
    head at endPos. RV sizes the head from the line thickness. So arrowHeadSize
    and arrowHeadWidth of Kitsu have nowhere to go, and the function drops
    them.
    """
    (tx, ty), (hx, hy), stroke_width_px = _fabric_line_endpoints(obj)
    start = pixel_to_rv_normalized(tx, ty, space)
    end = pixel_to_rv_normalized(hx, hy, space)

    width_rv = px_width_to_rv(stroke_width_px, space.canvas_height)
    # RV arrows carry the line color in both borderColor and innerColor.
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
    obj: Dict[str, Any],
    space: _PaintSpace,
    frame_num: int,
) -> Dict[str, Any]:
    """
    Convert a Kitsu "i-text" to an RV "text", with the property set that RV
    annotate mode writes. RV anchors text at the left end of its first baseline
    and sizes it as a fraction of the image height. RV draws text unrotated,
    left-aligned and without a background, in its default font.
    """
    ax, ay, font_px = _fabric_text_anchor(obj)
    position = pixel_to_rv_normalized(ax, ay, space)
    font_size = px_width_to_rv(font_px, space.canvas_height)
    text = _clean_text(obj.get("text"))

    if float(obj.get("angle") or 0) % 360:
        _warn(
            f"text {obj.get('id')!r} is rotated {obj.get('angle')} deg; "
            f"OpenRV draws text unrotated, dropping the rotation"
        )
    align = str(obj.get("textAlign") or "left").lower()
    if "\n" in text and align not in ("left", "justify", "justify-left"):
        _warn(
            f"text {obj.get('id')!r} is {align}-aligned; OpenRV draws "
            f"multi-line text left-aligned"
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
            # Fabric text gets its color from its fill. Its stroke is an
            # outline.
            "color": color_to_rv_color(
                obj.get("fill"), obj.get("opacity", 1.0)
            ),
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
            # "" is the default annotation font of RV.
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
    """
    Convert Kitsu annotation records to a flat list of OpenRV paint shapes
    ({"type", "frame", "properties"}). Supported Kitsu types: "PSStroke" (->
    pen), "line", "circle"/"ellipse" (-> ellipse), "rect", "arrow" and
    "i-text"/"text"/"textbox" (-> text).

    The function uses the own canvasWidth/canvasHeight of each object when
    present. Otherwise it uses `canvas_width`/`canvas_height`, then the media
    size.

    The function uses the "frame" of a record when present. Without it, the
    function converts the "time" to a frame at the production `fps` (the
    inverse of `convertToKitsu`, counting from `frame_base`). A record with
    neither goes on `default_frame`. A record with only a "time" goes there
    too when no `fps` is given (still previews).

    Kitsu erasers (the "eraser" group on each erased object) become RV pens in
    erase mode (properties["mode"] == 1), one for each eraser stroke. The
    function places them after the objects that they erased and before the
    others.
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
        shapes.extend(
            _record_to_orv(
                objects,
                width,
                height,
                default_canvas_width,
                default_canvas_height,
                frame_num,
            )
        )

    return shapes


def _collect_erasers(
    obj: Dict[str, Any],
    index: int,
    erasers: List[Dict[str, Any]],
    space: _PaintSpace,
) -> None:
    """
    Add the eraser strokes of `obj` to `erasers`, in RV units. Fabric stores a
    copy of a stroke on every object that it cut. So a stroke that is already
    in `erasers` only records that it also cut this object.
    """
    tol = MATCH_TOLERANCE_PX / space.canvas_height
    for e_i, entry in enumerate(_fabric_eraser_entries(obj)):
        for s_i, stroke in enumerate(entry["strokes"]):
            points = [
                pixel_to_rv_normalized(x, y, space)
                for x, y in stroke["points"]
            ]
            norm = {
                "points": points,
                "width": px_width_to_rv(stroke["width"], space.canvas_height),
            }
            same = next(
                (e for e in erasers if _same_eraser_stroke(e, norm, tol)),
                None,
            )
            if same is not None:
                same["attached"].add(index)
                continue
            erasers.append(
                {
                    **norm,
                    "attached": {index},
                    "bbox": _points_bbox(points, norm["width"] / 2.0),
                    "seed": f"{obj.get('id')}:{e_i}:{s_i}",
                }
            )


def _record_to_orv(
    objects: Sequence[Dict[str, Any]],
    width: int,
    height: int,
    default_canvas_width: float,
    default_canvas_height: float,
    frame_num: int,
) -> List[Dict[str, Any]]:
    """
    Convert the objects of one Kitsu record to RV shapes.

    Add an eraser pen for each distinct eraser stroke. Fabric stores a copy of
    the stroke on every object that it cut.
    """
    items: List[Dict[str, Any]] = []
    erasers: List[Dict[str, Any]] = []

    for index, obj in enumerate(objects):
        obj_type = obj.get("type")
        space = _PaintSpace(
            width,
            height,
            float(obj.get("canvasWidth") or default_canvas_width),
            float(obj.get("canvasHeight") or default_canvas_height),
        )

        converter = _TYPE_CONVERTERS.get(obj_type)
        shape = None
        if converter is None:
            _warn(
                f"skipping unsupported Kitsu object type "
                f"'{obj_type}' (id={obj.get('id')!r})"
            )
        else:
            shape = converter(
                obj,
                space,
                frame_num,
            )
        if shape is not None:
            items.append(
                {
                    "index": index,
                    "shape": shape,
                    "bbox": _norm_box(
                        _fabric_aabb(obj),
                        space,
                    ),
                }
            )

        _collect_erasers(
            obj,
            index,
            erasers,
            space,
        )

    return _order_with_erasers(items, erasers, frame_num)


# ---------------------------------------------------------------------------
# Sync: what to send to Kitsu
# ---------------------------------------------------------------------------


def _same_time(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return a == b


def split_annotation_changes(
    records: Sequence[Dict[str, Any]],
    reference_records: Optional[Sequence[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Return (additions, updates) for gazu.files.update_preview_annotations, from
    the `convertToKitsu` records and the annotations that Kitsu already has.

    Kitsu never overwrites an object through an addition (it drops an object
    whose id is already on that frame). So objects that exist but changed (for
    example, by an erase) must go in updates, under the time at which Kitsu
    stores them. The function does not send unchanged objects.
    """
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
                update = updates.setdefault(
                    ref[0],
                    {
                        "time": ref[0],
                        "frame": record.get("frame"),
                        "drawing": {"objects": []},
                    },
                )
                update["drawing"]["objects"].append(obj)
        if added:
            additions.append(
                {
                    "time": record["time"],
                    "frame": record.get("frame"),
                    "drawing": {"objects": added},
                }
            )
    return additions, list(updates.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> None:
    """
    Convert a Kitsu preview-annotation JSON dump into OpenRV paint shapes. This
    works without OpenRV, PySide6 or gazu installed.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Convert Kitsu preview-annotation JSON into OpenRV paint shapes."
        )
    )
    parser.add_argument(
        "kitsu_json",
        help=(
            "Path to a JSON file containing the Kitsu annotation records list"
        ),
    )
    parser.add_argument(
        "--width",
        type=int,
        required=True,
        help="Source video/image width in px",
    )
    parser.add_argument(
        "--height",
        type=int,
        required=True,
        help="Source video/image height in px",
    )
    parser.add_argument(
        "--canvas-width",
        type=float,
        default=None,
        help="Fabric.js canvas width, if different from --width",
    )
    parser.add_argument(
        "--canvas-height",
        type=float,
        default=None,
        help="Fabric.js canvas height, if different from --height",
    )
    parser.add_argument(
        "--frame-offset",
        type=int,
        default=0,
        help="Subtracted from each Kitsu frame number",
    )
    parser.add_argument(
        "--default-frame",
        type=int,
        default=1,
        help="Frame to use for records with no frame field (still previews)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help=(
            "Production frame rate; used to turn a record's 'time' into a "
            "frame when it has no 'frame' field"
        ),
    )
    parser.add_argument(
        "--frame-base",
        type=int,
        default=1,
        help="Frame number that time 0 corresponds to (with --fps)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Where to write the OpenRV shapes JSON (default: stdout)",
    )
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
