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
    * Y is UP
    * the LONGER image axis spans [-1, 1]; the shorter axis spans
      [-s, s] where s = shorter / longer

  (An earlier version of this docstring claimed X spanned [-aspect,
  aspect] and Y spanned [-1, 1]. That does not match what
  `rv_normalized_to_pixel` actually implements -- for a landscape plate
  it is X that is clamped to [-1, 1] -- and the mismatch is worth
  knowing about, because it is the convention every stroke width, font
  size and arrow head size in this file is normalized against too.)

Kitsu/Fabric.js works in plain PIXEL space relative to the annotation
canvas:

    * origin (0, 0) is the TOP-LEFT corner
    * X grows right, Y grows DOWN
    * "canvasWidth" / "canvasHeight" define the pixel space that points
      and left/top/width/height are expressed in (this can differ
      slightly from the actual video resolution)

    m = max(width, height)
    OpenRV -> Kitsu:  px = (nx * m / width + 1) / 2 * canvas_width
                      py = (1 - ny * m / height) / 2 * canvas_height
    Kitsu -> OpenRV:  nx = (2 * px / canvas_width - 1) * width / m
                      ny = (1 - 2 * py / canvas_height) * height / m

Lengths that aren't points -- stroke width, border width, font size,
arrow head size -- go through `norm_size_to_px` / `px_size_to_norm`
instead, which are deliberately the single place that convention is
spelled out (see the note on their implementation).

--------------------------------------------------------------------------
Fabric.js transforms
--------------------------------------------------------------------------
Fabric objects do NOT store their on-screen geometry solely in
left/top/width/height:

* "scaleX"/"scaleY" record an interactive resize; Fabric leaves
  width/height (and rx/ry, radius, fontSize) at their pre-resize values.
  Any shape the artist dragged a handle on therefore has to be read as
  width * scaleX, not width.
* "originX"/"originY" decide what left/top actually mean. They are only
  the top-left corner when the origins are "left"/"top"; Kitsu's shape
  tools commonly create objects with centered origins, in which case
  reading left/top as a corner is off by half the shape.
* "angle" rotates the object. RVPaint's rect/ellipse components are
  axis-aligned min/max boxes with nowhere to put a rotation, so a
  rotated ellipse or rectangle is imported unrotated and a warning is
  printed rather than silently landing in the wrong place.
* Fabric's Line keeps x1/y1/x2/y2 in the object's LOCAL space and moves
  the object by changing left/top, so the raw x1..y2 values are not
  canvas coordinates once a line or arrow has been dragged. Endpoints
  are reconstructed from the normalized bbox, using x1..y2 only for the
  direction of the diagonal.

`_fabric_bbox` / `_fabric_segment` are the single place all of that is
handled; every "from_fabric" converter goes through them.

--------------------------------------------------------------------------
Round-tripping notes
--------------------------------------------------------------------------
* Pure Fabric.js/CSS boilerplate (flipX/Y, skewX/Y, version, shadow,
  erasable, fillRule, paintFirst, strokeLineCap/Join, strokeUniform,
  strokeDashArray/Offset, strokeMiterLimit, globalCompositeOperation,
  backgroundColor, ...) has no OpenRV equivalent in either direction and
  is simply discarded.
* "startTime"/"endTime" on a PSStroke are wall-clock telemetry of when
  the artist drew it -- cosmetic, not structural. Going OpenRV->Kitsu we
  synthesize monotonically increasing values; going Kitsu->OpenRV we
  drop them.
* "createdBy" (a Kitsu person id) has no OpenRV field. Going
  OpenRV->Kitsu every shape gets the same `author`; going Kitsu->OpenRV
  it's dropped from the shape but still available via `extract_authors`.
* Fabric's "id" round-trips as OpenRV's "uuid" property, so shape
  identity survives a full OpenRV -> Kitsu -> OpenRV trip.
* Rotation flips sign across the two spaces: Fabric's "angle" is degrees
  CLOCKWISE in a Y-down space, RV's "rotation" is degrees
  COUNTER-clockwise in a Y-up space. Text is the only shape carrying a
  rotation through, and it is negated in both directions.
* Colors are accepted in every form the real data actually uses --
  [r, g, b, a] arrays (0..255 ints, which is what real Kitsu annotation
  objects contain), "#rgb"/"#rgba"/"#rrggbb"/"#rrggbbaa" hex strings,
  and "rgb()"/"rgba()" CSS functions. RV's own color rows are FLOATS
  0..1. `_rv_color_floats` and `color_to_rv_color` are the two funnels
  everything goes through, and both are scale-tolerant.
* Only "PSStroke" (freehand pen) has a confirmed real Kitsu sample.
  "line", "ellipse", "circle", "rectangle", and "text" are mapped onto
  Fabric.js's native "line" / "ellipse" / "circle" / "rect" / "textbox"
  object types. Deployments that use "PS"-prefixed custom subclasses
  instead are handled too: `_TYPE_CONVERTERS` registers both spellings,
  and the lookup is case-insensitive.
* "arrow" has no Fabric.js native equivalent at all, so it's mapped
  onto a guessed "PSArrow" custom type (same x1/y1/x2/y2 fields as
  "line" plus a "headSize"), following the "PS"-prefixed subclassing
  convention the real PSStroke sample uses. This is the least-confirmed
  mapping in the file -- double check it against your Kitsu deployment
  before relying on it.
* RV's text origin is the lower-left of the first line, Fabric's is the
  top-left of the whole box. `TEXT_BASELINE_RATIO` is the one knob that
  reconciles them, and it is applied symmetrically so text round-trips
  to the same place it started.

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

# Default shape metrics, in RV normalized units (fractions of the frame --
# the same convention `norm_size_to_px` uses).
#
# These exist because the defaults used to be spelled out inline, and the
# three places that needed each one disagreed. An arrow's head size, for
# instance, defaulted to 0.03 coming in from Kitsu but 0.005 when applied
# to a live RVPaint node -- a 6x difference, which is exactly why imported
# arrows came back with oversized heads while exported ones looked fine.
# Every default now resolves here, on both sides of the conversion and in
# the live-apply code.
DEFAULT_PEN_WIDTH = 0.003
DEFAULT_BORDER_WIDTH = 0.005
DEFAULT_ARROW_THICKNESS = 0.01
DEFAULT_TEXT_SIZE = 0.05
DEFAULT_TEXT_SPACING = 0.8  # RVPaint's own default line-spacing factor

# RV anchors text at the lower-left of its FIRST line; Fabric's textbox
# anchors at the top-left of the WHOLE box. The offset between them is one
# line's ascent, expressed here as a multiple of the font size. Applied
# symmetrically in `_text_to_fabric` / `_text_from_fabric`, so text stays
# where it was put no matter how many times it round-trips. If your text
# still sits a line high or low, this is the number to adjust.
TEXT_BASELINE_RATIO = 1.0

# Where downloaded preview files are written before being loaded into RV.
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "kitsu_review_downloads")

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

# "#rgb", "#rgba", "#rrggbb", "#rrggbbaa" -- Kitsu and hand-edited data use
# all four, and the old 6-digit-only pattern quietly rejected the rest,
# which is how a perfectly good "#fff" or "#ff3860cc" ended up falling back
# to opaque white.
_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# "rgb(255, 56, 96)" / "rgba(255, 56, 96, 0.8)" / "rgb(100% 20% 40% / 50%)"
_RGB_FUNC_RE = re.compile(r"^rgba?\(([^)]*)\)$", re.IGNORECASE)

# Fabric.js/CSS spellings that mean "no color here" rather than an actual
# color value. Kitsu (and hand-edited/older annotation data) can hand back
# any of these for "stroke" or "fill" instead of a real color.
_NO_COLOR_VALUES = {"none", "null", "transparent", ""}


# ============================================================================
# 1. Coordinate, size & color conversion
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


def norm_size_to_px(value: float, canvas_width: float, canvas_height: float) -> float:
    """OpenRV -> Kitsu: a normalized LENGTH (stroke width, border width,
    font size, arrow head size) -> canvas pixels.

    Every such length in this file resolves through here rather than
    multiplying by `canvas_height` at each call site, so the convention is
    stated once and can be changed in one place.

    Note that this is the frame-height convention, which is NOT the same
    scale `rv_normalized_to_pixel` uses for points (that one normalizes
    against max(width, height)). For a landscape plate the two differ by
    the aspect ratio -- roughly 12% at 16:9. Keeping the historical
    behaviour here because it is what existing exported data was written
    with, and because it is at least self-consistent: the inverse below
    undoes it exactly, so widths round-trip. If your stroke widths and
    font sizes come out systematically too fat in Kitsu, change these two
    functions to use `max(canvas_width, canvas_height) / 2.0` and they
    will line up with the point mapping.
    """
    return value * canvas_height


def px_size_to_norm(value: float, canvas_width: float, canvas_height: float) -> float:
    """Kitsu -> OpenRV: inverse of `norm_size_to_px`."""
    return value / canvas_height if canvas_height else 0.0


# ---------------------------------------------------------------------------
# Tolerant readers for RV property values
# ---------------------------------------------------------------------------
# RV property values reach the converters in more than one shape depending
# on where they came from, and the old code assumed exactly one of them:
#
#   * `_gather_rv_annotations` groups known vector properties into rows, so
#     a position arrives as [[x, y]] and a color as [[r, g, b, a]] -- but it
#     also UNWRAPS any length-1 list into a bare scalar, so a single-point
#     pen's `width` arrives as 0.003 rather than [0.003].
#   * `convert_kitsu_annotations` emits the [(x, y)] / [[r, g, b, a]] row
#     form.
#   * Hand-written data, older exports and .gto-derived dicts use the flat
#     [x, y] form.
#
# Indexing straight into these (props["min"][0], widths[0]) raises
# TypeError on the scalar form and silently reads a single float as a
# coordinate on the flat form. These three helpers accept all of it.

def _first_pair(value: Any, default: Point = (0.0, 0.0)) -> Point:
    """Pull an (x, y) pair out of an RV property value in any of its shapes."""
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
    """Pull a single number out of an RV property value in any of its shapes."""
    if isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and value:
        return _first_scalar(value[0], default)
    return float(default)


def _pairs(value: Any) -> List[Point]:
    """Pull a list of (x, y) pairs out of a points-style property, accepting
    both the grouped [[x, y], ...] form and the flat [x0, y0, x1, y1, ...]
    form."""
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


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

def _rv_color_floats(
    color_rows: Any, default: Sequence[float] = (1.0, 1.0, 1.0, 1.0),
) -> Tuple[float, float, float, float]:
    """Normalize any RV-side color value into (r, g, b, a) floats in 0..1.

    Accepts the grouped row form ([[r, g, b, a]], what
    `_gather_rv_annotations` produces, channels 0..1 floats), the flat form
    ([r, g, b, a], what `color_to_rv_color` produces, channels 0..255
    ints), and missing/empty values.

    Scale is detected rather than assumed: any channel above 1.0 means the
    value is on the 0..255 scale. The one ambiguous case is a near-black
    0..255 color like [1, 1, 1, 1], which reads as float white -- worth
    knowing about, but it isn't reachable through the converters in this
    file, which always carry a 0..255 alpha of 255 for opaque colors.
    """
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
    """OpenRV -> Kitsu: an RV color row -> a Kitsu/Fabric hex string, e.g.
    "#ff3860"."""
    r, g, b, _a = _rv_color_floats(color_rows)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def rv_alpha(color_rows: Any) -> float:
    """OpenRV -> Kitsu: pull the alpha channel (0..1) out of an RV color row."""
    return _rv_color_floats(color_rows)[3]


def _css_channel(part: str) -> int:
    """One r/g/b component of a CSS rgb()/rgba() function -> 0..255."""
    part = part.strip()
    if part.endswith("%"):
        return int(round(max(0.0, min(100.0, float(part[:-1]))) * 255.0 / 100.0))
    return int(round(max(0.0, min(255.0, float(part)))))


def _css_alpha(part: str) -> int:
    """The alpha component of a CSS rgba() function -> 0..255."""
    part = part.strip()
    if part.endswith("%"):
        return int(round(max(0.0, min(100.0, float(part[:-1]))) * 255.0 / 100.0))
    return int(round(max(0.0, min(1.0, float(part))) * 255.0))


def _parse_css_color(value: Any) -> Optional[Tuple[int, int, int, Optional[int]]]:
    """Parse a Fabric/CSS color string into (r, g, b, a) with 0..255
    channels, or None if it isn't one. Alpha is None when the string
    didn't carry one, so the caller can fall back to Fabric's separate
    "opacity" field.

    Handles "#rgb", "#rgba", "#rrggbb", "#rrggbbaa", "rgb(...)" and
    "rgba(...)". The old code only recognized bare 6-digit hex, so every
    other spelling fell through to the "unrecognized color" branch and
    came back opaque white -- the single most common reason an imported
    ellipse fill or text color was wrong.
    """
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


def _is_hex_color(value: Any) -> bool:
    """Kept for backwards compatibility with anything importing this name."""
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value.strip()))


def _is_no_color(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NO_COLOR_VALUES)


def _color_channels(value: Any) -> Optional[Tuple[int, int, int, Optional[int]]]:
    """Kitsu-side color value -> (r, g, b, a) 0..255 channels, or None if it
    isn't a color. Alpha is None when the source didn't carry one."""
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
    """True if `value` is an actual color (rgba array, hex string or CSS
    function), as opposed to a "none"/"transparent"/None sentinel."""
    if _is_no_color(value):
        return False
    return _color_channels(value) is not None


def color_to_rv_color(value: Any, opacity: float = 1.0) -> List[List[int]]:
    """Kitsu -> OpenRV: a Kitsu/Fabric "stroke" or "fill" value -> RV's
    [[r, g, b, a]] color row, each channel an INTEGER 0..255. Inverse of
    `rv_color_to_hex` / `rv_alpha`.

    Accepts whatever form the real data hands back:
      * an rgba array, e.g. [255, 56, 96, 255] or [255, 56, 96] (3 or 4
        numbers, each already on a 0..255 scale) -- this is what real
        Kitsu annotation objects actually contain.
      * "#rgb" / "#rgba" / "#rrggbb" / "#rrggbbaa" hex strings.
      * "rgb(...)" / "rgba(...)" CSS functions.
      * Fabric.js/CSS "none"/"transparent"/None -> treated as opaque
        white (i.e. "no color set").

    `opacity` is Fabric's separate 0..1 "opacity" field. It is used as the
    alpha channel when `value` carries no alpha of its own, and MULTIPLIES
    the alpha when it does -- which is what Fabric itself does when
    compositing.

    Falls back to opaque white (with a stderr warning) instead of raising
    if `value` doesn't match any recognized shape.

    NOTE: this returns the nested row form [[r, g, b, a]], matching every
    other color value in this file and what `rv_color_to_hex` / `rv_alpha`
    expect. It previously returned a FLAT [r, g, b, a] while its own type
    annotation and every neighbouring fallback ([[0, 0, 0, 0]]) used the
    nested form, so an ellipse's borderColor and its innerColor came out
    in two different shapes from the same function.
    """
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


# kept as an alias -- some callers/older code may still import this name
hex_to_rv_color = color_to_rv_color


# ---------------------------------------------------------------------------
# Fabric.js geometry
# ---------------------------------------------------------------------------

def _fabric_scale(obj: Dict[str, Any]) -> Tuple[float, float]:
    """(scaleX, scaleY) for a Fabric object, defaulting to 1 and never 0."""
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
    """Top-left anchored, scale-applied (left, top, width, height) in canvas
    pixels for a Fabric object.

    `width_px` / `height_px` override the object's own width/height for
    shapes that carry their size elsewhere (an ellipse's rx/ry, a circle's
    radius); pass them PRE-scale, the scale is applied here.

    This exists because the old converters read left/top/width/height raw,
    which ignores the two transform fields that actually decide where a
    Fabric object sits -- see the "Fabric.js transforms" section of the
    module docstring.
    """
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
    """(start, end) in canvas pixels for a line-ish or arrow-ish object.

    Fabric's Line keeps x1/y1/x2/y2 in the object's own local space and
    records a move by changing left/top, so reading the raw x1..y2 pairs
    (what this file used to do) puts a dragged line or arrow back at the
    position it was first drawn. The endpoints are therefore rebuilt from
    the normalized bbox, with x1..y2 consulted only for which diagonal of
    that box the segment runs along.
    """
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
    if obj.get("flipX"):
        flip_x = not flip_x
    if obj.get("flipY"):
        flip_y = not flip_y

    start = (left + w if flip_x else left, top + h if flip_y else top)
    end = (left if flip_x else left + w, top if flip_y else top + h)
    return start, end


def _warn_dropped_rotation(obj: Dict[str, Any], kind: str) -> None:
    """RVPaint's rect/ellipse components are axis-aligned min/max boxes with
    no rotation field, so a rotated Fabric shape can't be represented. Say
    so rather than importing it silently misplaced."""
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
# Only "PSStroke" has a confirmed real sample; "line", "ellipse",
# "circle", and "rectangle" below are mapped onto Fabric.js's own native
# object types of the same/similar name ("rectangle" -> "rect"), "text"
# is mapped onto Fabric's native "textbox", and "arrow" (no Fabric
# native equivalent) is mapped onto a guessed "PSArrow" custom type.
#
# Everything written here is anchored at originX/originY "left"/"top" with
# scaleX/scaleY 1, so the bbox IS left/top/width/height. That is what makes
# `_fabric_bbox` on the import side an exact inverse -- it just also copes
# with the objects Kitsu itself produces, which are not always so tidy.
#
# "time" (per frame record) is meaningful -- Kitsu uses it to scrub/seek:
# (frame - frame_base) / fps * 1000. "startTime"/"endTime" (per PSStroke)
# are cosmetic wall-clock telemetry; since OpenRV doesn't record them, we
# synthesize monotonically increasing values rather than inventing fake
# "real" timestamps.

def _fabric_base(
    obj_type: str,
    left: float, top: float, width: float, height: float,
    stroke_hex: Optional[str], stroke_width: float, opacity: float,
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


def _inner_fill(props: Dict[str, Any]) -> Optional[str]:
    """Shared "innerColor -> Fabric fill" rule: a fully transparent inner
    color means the shape isn't filled, which Fabric spells as a null
    fill."""
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

    # `width` is per-point in RV, and `_gather_rv_annotations` unwraps a
    # single-point stroke's list into a bare float -- so this has to go
    # through _first_scalar rather than indexing.
    stroke_width_norm = _first_scalar(props.get("width"), DEFAULT_PEN_WIDTH)
    stroke_width_px = norm_size_to_px(stroke_width_norm, canvas_width, canvas_height)

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
    # NOTE: no confirmed Kitsu sample for ellipses either. Mapped onto
    # Fabric.js's native "ellipse" object type. Fabric derives an
    # ellipse's size from rx/ry rather than width/height, so BOTH are
    # written here -- writing only width/height (as this used to) leaves
    # rx/ry at Fabric's default and the ellipse renders at the wrong size
    # in Kitsu. Adjust here if your deployment uses a custom "PSEllipse".
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
    # NOTE: no confirmed Kitsu sample for circles either. Mapped onto
    # Fabric.js's native "circle" object type (radius + left/top bbox).
    # Unlike ellipse, a circle only carries a single "radius" in OpenRV
    # (center + radius, not an arbitrary bbox), so the x/y radii are
    # measured separately in pixel space and averaged -- they'll only
    # differ if canvas_width/canvas_height don't share the source
    # width/height's aspect ratio. Adjust here if your Kitsu deployment
    # uses a custom "PSCircle" type instead.
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
    # NOTE: no confirmed Kitsu sample for rectangles either. Mapped onto
    # Fabric.js's native "rect" object type -- identical bbox handling
    # to _ellipse_to_fabric, just without rx/ry. Adjust here if your
    # Kitsu deployment uses a custom "PSRectangle" type instead.
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
    # NOTE: no confirmed Kitsu sample for arrows, and Fabric.js has no
    # native arrow object at all -- this is mapped onto a guessed
    # "PSArrow" custom type, following the same "PS"-prefixed
    # subclassing convention the real PSStroke sample uses, with the
    # same x1/y1/x2/y2 fields the "line" mapping uses plus a "headSize"
    # for the arrowhead. If your Kitsu deployment represents arrows
    # differently (e.g. a Fabric "group" of a line + a triangle head),
    # this converter is intentionally isolated so you can swap it out.
    #
    # RVPaint's arrow component calls the head size "thickness"; the
    # Fabric side calls it "headSize". Both now default to
    # DEFAULT_ARROW_THICKNESS so a missing value means the same size in
    # either direction -- they used to differ by 6x.
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
        "PSArrow", left, top, bbox_w, bbox_h,
        rv_color_to_hex(props.get("borderColor", [])), round(stroke_width_px, 2),
        rv_alpha(props.get("borderColor", [])),
        author, canvas_width, canvas_height,
        props.get("uuid"),
    )
    obj["startTime"] = start_time
    obj["endTime"] = end_time
    obj["x1"], obj["y1"], obj["x2"], obj["y2"] = x1, y1, x2, y2
    obj["headSize"] = round(head_size_px, 2)
    obj["fill"] = _inner_fill(props)
    return obj


def _text_to_fabric(
    shape: Dict[str, Any], width: int, height: int,
    canvas_width: float, canvas_height: float,
    author: str, clock_ms: List[int],
) -> Dict[str, Any]:
    # NOTE: no confirmed Kitsu sample for text either. Mapped onto
    # Fabric.js's native "textbox" object type. Text color rides on
    # Fabric's "fill" field (not "stroke" -- glyphs are filled, not
    # outlined), and RV's normalized "size" (fraction of frame height,
    # matching the stroke-width convention used elsewhere in this file)
    # becomes Fabric's "fontSize" in canvas pixels. Adjust here if your
    # Kitsu deployment uses "i-text" or a custom "PSText" type instead.
    #
    # Two things this has to reconcile, both of which used to be passed
    # straight through and both of which moved the text visibly:
    #   * anchor -- RV positions text by the lower-left of its first line,
    #     Fabric by the top-left of the whole box, so the position is
    #     lifted by TEXT_BASELINE_RATIO font sizes here and dropped again
    #     on import.
    #   * rotation -- RV's is CCW in a Y-up space, Fabric's is CW in a
    #     Y-down space, so the sign flips.
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

    # Fabric doesn't ship a bbox up front for text the way strokes/shapes
    # do -- it's derived from font metrics client-side -- so this is a
    # rough estimate, good enough to seed "width"/"height" on export.
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
    # RV's "spacing" is passed straight through as Fabric's "lineHeight" --
    # the two fields aren't a confirmed exact match, but both are
    # multiplicative line-spacing factors, so this is the closest
    # reasonable mapping absent a real sample. Both sides now fall back to
    # RVPaint's own default (DEFAULT_TEXT_SPACING) when the field is
    # missing, rather than to two different numbers.
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
    canvas_width = float(canvas_width or width)
    canvas_height = float(canvas_height or height)

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
            # One malformed shape shouldn't cost the artist the whole
            # frame's worth of notes, so failures are reported and skipped
            # rather than raised.
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
# * Every converter reads geometry through `_fabric_bbox` / `_fabric_segment`
#   rather than off left/top/width/height directly, so scaleX/scaleY,
#   originX/originY and Fabric's local-space line endpoints are all
#   accounted for. Objects this file wrote are unaffected (they're always
#   left/top anchored at scale 1); objects Kitsu's own tools wrote often
#   are not.
# * The ellipse's "min"/"max" bookkeeping only ever stored an axis-aligned
#   bounding box in OpenRV, so reconstructing it from the Fabric bbox
#   round-trips exactly -- there's no information about which literal
#   corner was "min" vs "max" to lose in the first place. A ROTATED Fabric
#   ellipse or rect can't be represented at all, and warns.
# * OpenRV's real paint node (RVPaint) has no native "circle" primitive --
#   only "ellipse" (see `_apply_ellipse_live`). A Kitsu Fabric "circle" is
#   therefore converted to an OpenRV *ellipse* shape (with an equal-sided
#   bounding box) rather than a fictitious OpenRV "circle" type, so it
#   actually round-trips through RVPaint instead of silently failing to
#   render.

def _border_from_fabric(obj: Dict[str, Any], canvas_width: float, canvas_height: float) -> float:
    """Fabric "strokeWidth" (canvas px) -> RV "borderWidth"/"width"
    (normalized), with a single shared default."""
    default_px = norm_size_to_px(DEFAULT_BORDER_WIDTH, canvas_width, canvas_height)
    stroke_width_px = obj.get("strokeWidth")
    if stroke_width_px is None:
        stroke_width_px = default_px
    # A Fabric object scaled non-uniformly scales its stroke too unless
    # strokeUniform is set; averaging the two is the best single number RV
    # can hold.
    if not obj.get("strokeUniform"):
        scale_x, scale_y = _fabric_scale(obj)
        stroke_width_px = float(stroke_width_px) * (scale_x + scale_y) / 2.0
    return px_size_to_norm(float(stroke_width_px), canvas_width, canvas_height)


def _fill_from_fabric(obj: Dict[str, Any], default_transparent: bool = True) -> List[List[int]]:
    """Fabric "fill" -> RV "innerColor".

    An explicit null/"transparent" fill means an unfilled shape, which RV
    spells as an alpha-0 innerColor. A MISSING fill key is different: the
    object simply never recorded one, and the caller decides (see
    `_arrow_from_fabric`, where an unfilled head would be invisible).
    """
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
    # NOTE: mirrors _line_to_fabric -- assumes the native Fabric.js "line"
    # type. Endpoints come from `_fabric_segment` rather than the raw
    # x1..y2 fields; see its docstring for why.
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
    """Fabric ellipse -> (left, top, width, height) in canvas px.

    Fabric's Ellipse is defined by rx/ry; width/height are derived and are
    not always present (and are never the post-resize size on their own).
    Preferring rx/ry, then falling back, is what makes an ellipse the
    artist resized in Kitsu come back the size they left it.
    """
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
    # NOTE: mirrors _ellipse_to_fabric. Adjust here if your Kitsu
    # deployment uses a custom "PSEllipse" subclass with different fields.
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
    # NOTE: OpenRV's RVPaint has no native "circle" shape command -- only
    # "ellipse" (ShapeEllipse, min/max bounding box). So a Kitsu circle is
    # converted to an OpenRV *ellipse* shape. A circle scaled
    # non-uniformly in Kitsu is a visual ellipse, and comes through as one
    # here, which is another reason not to force a square box.
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
    # NOTE: mirrors _rectangle_to_fabric -- assumes the native Fabric.js
    # "rect" type. Adjust here if your Kitsu deployment uses a custom
    # "PSRectangle" subclass instead.
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
    # NOTE: mirrors _arrow_to_fabric -- assumes the guessed "PSArrow"
    # custom type (x1/y1/x2/y2 + headSize). Adjust the field lookups
    # here to match your Kitsu deployment's actual arrow representation.
    (x1, y1), (x2, y2) = _fabric_segment(obj)

    start = pixel_to_rv_normalized(x1, y1, width, height, canvas_width, canvas_height)
    end = pixel_to_rv_normalized(x2, y2, width, height, canvas_width, canvas_height)

    head_size_px = obj.get("headSize")
    if head_size_px is None:
        thickness_norm = DEFAULT_ARROW_THICKNESS
    else:
        thickness_norm = px_size_to_norm(float(head_size_px), canvas_width, canvas_height)

    # RVPaint fills the arrowhead with innerColor, so an arrow that never
    # recorded a fill needs one or the head renders invisible. An explicit
    # null/"transparent" fill is still honoured as unfilled, which keeps
    # RV -> Kitsu -> RV exact (this file always writes the key).
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
    # NOTE: mirrors _text_to_fabric -- assumes the native Fabric.js
    # "textbox" type. If your Kitsu deployment uses "i-text" or a
    # custom "PSText" instead, adjust the field lookups here (all three
    # spellings are registered in _TYPE_CONVERTERS and share this
    # converter, since they carry the same fields).
    _scale_x, scale_y = _fabric_scale(obj)

    font_size_px = obj.get("fontSize")
    if font_size_px is None:
        font_size_px = norm_size_to_px(DEFAULT_TEXT_SIZE, canvas_width, canvas_height)
    # Fabric records a resized textbox as a scale, leaving fontSize alone.
    font_size_px = float(font_size_px) * scale_y
    size_norm = px_size_to_norm(font_size_px, canvas_width, canvas_height)

    left, top, _bbox_w, _bbox_h = _fabric_bbox(obj)
    # Undo the anchor shift applied on export: RV wants the lower-left of
    # the first line, Fabric gave us the top of the box.
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
            # Fabric angle is CW in a Y-down space, RV rotation is CCW in a
            # Y-up space.
            "rotation": -angle,
            "spacing": obj.get("lineHeight", DEFAULT_TEXT_SPACING),
            "font": obj.get("fontFamily") or "",
            "color": color,
        },
    }


# Both the native Fabric.js type names and the "PS"-prefixed custom
# subclass names some Kitsu deployments use are registered, because which
# one you get depends on the deployment and an unrecognized type was
# previously dropped with nothing but a warning. Lookup is
# case-insensitive (see `_lookup_type_converter`).
_TYPE_CONVERTERS = {
    "PSStroke": _pen_from_fabric,
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
    """Resolve a Fabric object's "type" to a converter, tolerating case and
    stray whitespace. Fabric v6 lowercased its built-in type names, older
    serializations didn't, and custom subclasses are CamelCase."""
    if not isinstance(obj_type, str):
        return None
    key = obj_type.strip()
    return _TYPE_CONVERTERS.get(key) or _TYPE_CONVERTERS_LOWER.get(key.lower())


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
    default_canvas_width = float(canvas_width or width)
    default_canvas_height = float(canvas_height or height)

    shapes: List[Dict[str, Any]] = []

    for record in kitsu_records or []:
        try:
            frame_num = int(record["frame"]) - frame_offset
        except (KeyError, TypeError, ValueError):
            print(f"warning: skipping annotation record with no usable frame "
                  f"number: {record!r}", file=sys.stderr)
            continue
        objects = record.get("drawing", {}).get("objects", []) or []

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

            # per-object canvas size takes precedence if Kitsu recorded
            # one (it can differ slightly from the nominal video res).
            obj_canvas_width = float(obj.get("canvasWidth") or default_canvas_width)
            obj_canvas_height = float(obj.get("canvasHeight") or default_canvas_height)

            # One malformed object shouldn't lose the rest of the frame's
            # annotations.
            try:
                shapes.append(converter(
                    obj, width, height, obj_canvas_width, obj_canvas_height, frame_num,
                ))
            except Exception as exc:
                print(
                    f"warning: could not convert Kitsu object {obj.get('id')!r} "
                    f"of type '{obj_type}' on frame {frame_num}: {exc}",
                    file=sys.stderr,
                )

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


# ============================================================================
# 4. OpenRV RVPaint GTO serialization
# ============================================================================
# Serializes converted Kitsu annotations (the {"frame", "pens", "texts"}
# structure) into a standalone RVPaint GTO text fragment -- an alternative
# to poking RV's live property API when you just want the .gto text (e.g.
# to write directly into a session file).
#
# NOTE: this takes the grouped {"frame", "pens", "texts"} form, NOT the
# flat [{"type", "frame", "properties"}, ...] list
# `convert_kitsu_annotations` returns -- see
# `KitsuReviewPanel.apply_annotations_live` for the flat one.

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
    points = _pairs(pen["points"])
    # RVPaint's color rows are 0..1 floats; _rv_color_floats accepts either
    # scale so a pen dict built from converted Kitsu data (0..255) works too.
    color = _rv_color_floats(pen.get("color"))
    width = pen.get("width", DEFAULT_PEN_WIDTH)
    if isinstance(width, (int, float)):
        width = [width] * len(points)
    elif len(width) != len(points):
        width = [_first_scalar(width, DEFAULT_PEN_WIDTH)] * len(points)

    lines = [f"    {name}", "    {"]
    lines.append(f"        float[4] color = {_flat(color)}")
    lines.append(f"        float width = {_flat(width)}")
    lines.append(f'        string brush = "{pen.get("brush", "circle")}"')
    lines.append(f"        float[2] points = {_nested(points)}")
    lines.append(f"        int debug = {int(pen.get('debug', 0))}")
    lines.append(f"        int join = {int(pen.get('join', 3))}")
    lines.append(f"        int cap = {int(pen.get('cap', 1))}")
    lines.append(f"        int splat = {int(pen.get('splat', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')


def _text_block(txt: Dict[str, Any], text_id: int, frame: int) -> Tuple[str, str]:
    name = f'"text:{text_id}:{frame}:Kitsu"'
    escaped = str(txt["text"]).replace('"', '\\"').replace("\n", "\\n")

    lines = [f"    {name}", "    {"]
    lines.append(f"        float[2] position = {_flat(_first_pair(txt.get('position')))}")
    lines.append(f"        float[4] color = {_flat(_rv_color_floats(txt.get('color')))}")
    lines.append(f"        float spacing = {_fnum(txt.get('spacing', DEFAULT_TEXT_SPACING))}")
    lines.append(f"        float size = {_fnum(txt.get('size', DEFAULT_TEXT_SIZE))}")
    lines.append(f"        float scale = {_fnum(txt.get('scale', 1))}")
    lines.append(f"        float rotation = {_fnum(txt.get('rotation', 0))}")
    lines.append(f'        string font = "{txt.get("font") or ""}"')
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
#   - gazu.files.update_preview_annotations(...)           -- annotation export
#
# The RV-side node parsing in `_gather_rv_annotations` uses the real RV
# command API where possible, but the exact per-frame paint property paths
# can vary between RV versions/builds -- double check those against the RV
# build you are targeting before shipping.
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

    # Fallback source resolution when a preview file doesn't report one.
    # Annotations are normalized against the source aspect ratio, so
    # guessing wrong here skews every shape -- but crashing on a missing
    # field is worse, and this at least gets 16:9 material close.
    DEFAULT_SOURCE_SIZE = (1920, 1080)
    DEFAULT_FPS = 24.0

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

    # ------------------------------------------------------------------
    # Source geometry helpers
    # ------------------------------------------------------------------

    def _source_size(self, preview_file):
        """(width, height) of a preview file's media, with a fallback.

        Preview files don't always carry width/height (audio, stills
        pending transcode, older Kitsu versions), and indexing straight
        into them raised KeyError mid-download."""
        default_w, default_h = self.DEFAULT_SOURCE_SIZE
        try:
            width = int(preview_file.get("width") or default_w)
            height = int(preview_file.get("height") or default_h)
        except (TypeError, ValueError):
            return default_w, default_h
        if width <= 0 or height <= 0:
            return default_w, default_h
        return width, height

    def _source_fps(self, preview_file):
        try:
            fps = float(preview_file.get("fps") or self.DEFAULT_FPS)
        except (TypeError, ValueError):
            return self.DEFAULT_FPS
        return fps if fps > 0 else self.DEFAULT_FPS

    # ------------------------------------------------------------------
    # RVPaint live properties
    # ------------------------------------------------------------------

    def _set_prop(self, full_name, ptype, width, values):
        if not rvc.propertyExists(full_name):
            rvc.newProperty(full_name, ptype, width)
        if ptype == rvc.FloatType:
            rvc.setFloatProperty(full_name, values, True)
        elif ptype == rvc.IntType:
            rvc.setIntProperty(full_name, values, True)
        elif ptype == rvc.StringType:
            rvc.setStringProperty(full_name, values, True)

    def _next_paint_id(self, paint_node):
        """RVPaint hands out component ids from its `paint.nextId` counter.

        Starting from 0 (as this used to) reuses ids that the artist's own
        strokes already occupy, so an imported Kitsu shape would land on
        top of an existing component instead of alongside it -- which is
        exactly the "sometimes my annotations just aren't there" symptom.
        """
        prop = f"{paint_node}.paint.nextId"
        try:
            if rvc.propertyExists(prop):
                values = rvc.getIntProperty(prop)
                if values:
                    return int(values[0])
        except Exception as exc:
            print(f"[KitsuReview] Could not read {prop} (starting ids at 0): {exc}")
        return 0

    def _existing_frame_order(self, paint_node, frame):
        """The component names already listed for a frame, so imported
        shapes can be appended rather than replacing them."""
        prop = f"{paint_node}.frame:{frame}.order"
        try:
            if not rvc.propertyExists(prop):
                return []
            order = rvc.getStringProperty(prop) or []
        except Exception as exc:
            print(f"[KitsuReview] Could not read {prop}: {exc}")
            return []
        if isinstance(order, str):
            return [order]
        return list(order)

    def apply_annotations_live(self, paint_node, openrv_annotations):
        """openrv_annotations: flat list of shapes as returned by
        convert_kitsu_annotations(), i.e. [{"type", "frame", "properties"}, ...] --
        NOT the {"frame", "pens", "texts"} grouping build_paint_gto() uses.

        "pen", "text", "rect"/"rectangle", "ellipse", "line", and "arrow"
        shapes all have a confirmed RVPaint live-property group wired up
        below -- the "pen:"/"text:"/"rect:"/"line:"/"arrow:"/"ellipse:"
        component-name prefixes and their property sets are exactly what
        RVPaint's own GTO reader (PaintIPNode::propertyChanged /
        compile*Component) expects. There is intentionally no "circle"
        branch: OpenRV has no native circle primitive, only ellipse (see
        `_circle_from_fabric`, which already emits an "ellipse" shape for
        a Kitsu circle), so a bare "circle" type should never actually
        reach this method via convert_kitsu_annotations().

        Component ids continue from the node's own `paint.nextId`, and each
        frame's existing draw order is preserved and appended to, so
        importing Kitsu notes onto a frame the artist has already painted
        on no longer clobbers their work.
        """
        self._set_prop(f"{paint_node}.paint.show", rvc.IntType, 1, [1])

        next_id = self._next_paint_id(paint_node)
        frame_order: Dict[int, List[str]] = {}

        for shape in openrv_annotations:
            shape_type = shape.get("type")
            frame = int(shape["frame"])
            props = shape["properties"]

            if shape_type == "pen":
                cname = self._apply_pen_live(paint_node, props, next_id, frame)
            elif shape_type == "text":
                cname = self._apply_text_live(paint_node, props, next_id, frame)
            elif shape_type in ("rect", "rectangle"):
                cname = self._apply_rect_live(paint_node, props, next_id, frame)
            elif shape_type == "ellipse":
                cname = self._apply_ellipse_live(paint_node, props, next_id, frame)
            elif shape_type == "line":
                cname = self._apply_line_live(paint_node, props, next_id, frame)
            elif shape_type == "arrow":
                cname = self._apply_arrow_live(paint_node, props, next_id, frame)
            else:
                print(f"[KitsuReview] Live-apply: skipping unsupported shape "
                      f"type {shape_type!r} (no RVPaint live-property group "
                      "wired up yet)", file=sys.stderr)
                continue

            next_id += 1
            frame_order.setdefault(frame, []).append(cname)

        for frame, names in frame_order.items():
            existing = self._existing_frame_order(paint_node, frame)
            merged = existing + [n for n in names if n not in existing]
            self._set_prop(f"{paint_node}.frame:{frame}.order", rvc.StringType, 1, merged)

        # Hand the counter back so RV's own paint tools don't reuse the ids
        # we just consumed.
        self._set_prop(f"{paint_node}.paint.nextId", rvc.IntType, 1, [next_id])

        rvc.redraw()

    @staticmethod
    def _rv_color(value, default=(1.0, 1.0, 1.0, 1.0)):
        """Normalize a stored RV color into a flat list of 0..1 floats, the
        format RVPaint's color properties actually want.

        Delegates to `_rv_color_floats`, which detects the scale rather
        than assuming it. This used to divide unconditionally by 255, so a
        color that was already on the 0..1 float scale (anything read back
        off a live paint node) came out effectively black."""
        return list(_rv_color_floats(value, default))

    def _apply_pen_live(self, paint_node, props, pen_id, frame):
        points = _pairs(props.get("points"))

        color_float = self._rv_color(props.get("color"))

        width = props.get("width", [DEFAULT_PEN_WIDTH])
        if not isinstance(width, list):
            width = [width]
        if len(width) != len(points):
            width = [_first_scalar(width, DEFAULT_PEN_WIDTH)] * len(points)

        cname = f"pen:{pen_id}:{frame}:Kitsu"
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
        return cname

    def _apply_text_live(self, paint_node, props, text_id, frame):
        # Mirrors the "text:" GTO group build_paint_gto() writes
        # (_text_block), since RVPaint's native text support is
        # confirmed to use that same field set.
        color_float = self._rv_color(props.get("color"))
        position = _first_pair(props.get("position"))

        cname = f"text:{text_id}:{frame}:Kitsu"
        base = f"{paint_node}.{cname}"

        self._set_prop(f"{base}.position", rvc.FloatType, 2, list(position))
        self._set_prop(f"{base}.color", rvc.FloatType, 4, color_float)
        self._set_prop(f"{base}.spacing", rvc.FloatType, 1,
                       [_first_scalar(props.get("spacing"), DEFAULT_TEXT_SPACING)])
        self._set_prop(f"{base}.size", rvc.FloatType, 1,
                       [_first_scalar(props.get("size"), DEFAULT_TEXT_SIZE)])
        self._set_prop(f"{base}.scale", rvc.FloatType, 1,
                       [_first_scalar(props.get("scale"), 1.0)])
        self._set_prop(f"{base}.rotation", rvc.FloatType, 1,
                       [_first_scalar(props.get("rotation"), 0.0)])
        self._set_prop(f"{base}.font", rvc.StringType, 1, [props.get("font") or ""])
        self._set_prop(f"{base}.text", rvc.StringType, 1, [str(props.get("text", ""))])
        self._set_prop(f"{base}.origin", rvc.StringType, 1, [""])
        self._set_prop(f"{base}.debug", rvc.IntType, 1, [0])
        return cname

    def _apply_rect_live(self, paint_node, props, rect_id, frame):
        # RVPaint's "rect:" component (PaintIPNode::compileRectComponent)
        # reads min/max/innerColor/borderColor/borderWidth -- an
        # axis-aligned box, not a Fabric-style left/top/width/height.
        min_pt = _first_pair(props.get("min"))
        max_pt = _first_pair(props.get("max"), (0.1, 0.1))

        cname = f"rect:{rect_id}:{frame}:Kitsu"
        base = f"{paint_node}.{cname}"

        self._set_prop(f"{base}.min", rvc.FloatType, 2, list(min_pt))
        self._set_prop(f"{base}.max", rvc.FloatType, 2, list(max_pt))
        self._set_prop(f"{base}.innerColor", rvc.FloatType, 4,
                       self._rv_color(props.get("innerColor"), default=(0.0, 0.0, 0.0, 0.0)))
        self._set_prop(f"{base}.borderColor", rvc.FloatType, 4, self._rv_color(props.get("borderColor")))
        self._set_prop(f"{base}.borderWidth", rvc.FloatType, 1,
                       [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
        return cname

    def _apply_ellipse_live(self, paint_node, props, ellipse_id, frame):
        # Same field set as "rect:" (PaintIPNode::compileEllipseComponent) --
        # RVPaint has no separate "circle" primitive, so Kitsu circles are
        # already normalized to this shape by _circle_from_fabric.
        min_pt = _first_pair(props.get("min"))
        max_pt = _first_pair(props.get("max"), (0.1, 0.1))

        cname = f"ellipse:{ellipse_id}:{frame}:Kitsu"
        base = f"{paint_node}.{cname}"

        self._set_prop(f"{base}.min", rvc.FloatType, 2, list(min_pt))
        self._set_prop(f"{base}.max", rvc.FloatType, 2, list(max_pt))
        self._set_prop(f"{base}.innerColor", rvc.FloatType, 4,
                       self._rv_color(props.get("innerColor"), default=(0.0, 0.0, 0.0, 0.0)))
        self._set_prop(f"{base}.borderColor", rvc.FloatType, 4, self._rv_color(props.get("borderColor")))
        self._set_prop(f"{base}.borderWidth", rvc.FloatType, 1,
                       [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
        return cname

    def _apply_line_live(self, paint_node, props, line_id, frame):
        # RVPaint's "line:" component (PaintIPNode::compileLineComponent)
        # has no innerColor -- lines aren't filled.
        start = _first_pair(props.get("startPos"))
        end = _first_pair(props.get("endPos"), (0.1, 0.0))

        cname = f"line:{line_id}:{frame}:Kitsu"
        base = f"{paint_node}.{cname}"

        self._set_prop(f"{base}.startPos", rvc.FloatType, 2, list(start))
        self._set_prop(f"{base}.endPos", rvc.FloatType, 2, list(end))
        self._set_prop(f"{base}.borderColor", rvc.FloatType, 4, self._rv_color(props.get("borderColor")))
        self._set_prop(f"{base}.borderWidth", rvc.FloatType, 1,
                       [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
        return cname

    def _apply_arrow_live(self, paint_node, props, arrow_id, frame):
        # RVPaint's "arrow:" component (PaintIPNode::compileArrowComponent)
        # uses "thickness" for the shaft/head thickness (not the
        # Fabric-side "headSize" this file's own PSArrow guess uses) and
        # does have its own innerColor, unlike "line:".
        start = _first_pair(props.get("startPos"))
        end = _first_pair(props.get("endPos"), (0.1, 0.0))

        cname = f"arrow:{arrow_id}:{frame}:Kitsu"
        base = f"{paint_node}.{cname}"

        border_color = self._rv_color(props.get("borderColor"))

        self._set_prop(f"{base}.startPos", rvc.FloatType, 2, list(start))
        self._set_prop(f"{base}.endPos", rvc.FloatType, 2, list(end))
        # An arrowhead with no fill is invisible, so fall back to the
        # border color rather than to opaque white.
        self._set_prop(f"{base}.innerColor", rvc.FloatType, 4,
                       self._rv_color(props.get("innerColor"), default=border_color))
        self._set_prop(f"{base}.borderColor", rvc.FloatType, 4, border_color)
        self._set_prop(f"{base}.thickness", rvc.FloatType, 1,
                       [_first_scalar(props.get("thickness"), DEFAULT_ARROW_THICKNESS)])
        self._set_prop(f"{base}.borderWidth", rvc.FloatType, 1,
                       [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
        return cname

    def _on_download_clicked(self):
        """Download the selected revision's preview file from Kitsu and
        load it into the current RV session."""
        if not self.current_revision:
            return
        rev = self.current_revision
        preview_file = rev["preview_file"]

        # A preview with no annotations yet is normal, and its width/height
        # can be missing -- neither should raise part way through a
        # download.
        kitsu_annotations = preview_file.get("annotations") or []
        src_width, src_height = self._source_size(preview_file)
        # Kitsu's annotation canvas is not necessarily the video
        # resolution, and the objects' own canvasWidth/Height is the only
        # reliable record of it. Passing None here (as this used to) made
        # every incoming shape fall back to the video resolution, which
        # scaled and offset anything drawn on a differently-sized canvas.
        canvas_width, canvas_height = _infer_canvas_size(
            kitsu_annotations, src_width, src_height
        )

        openrv_annotations = convert_kitsu_annotations(
            kitsu_annotations,
            width=src_width,
            height=src_height,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            frame_offset=0,
        )

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

        applied = 0
        if source_node and openrv_annotations:
            group_name = rvc.nodeGroup(source_node)
            paint_node_name = f"{group_name}_paint"
            try:
                self.apply_annotations_live(paint_node_name, openrv_annotations)
                applied = len(openrv_annotations)
            except Exception as exc:
                print(f"[KitsuReview] Could not apply Kitsu annotations to "
                      f"{paint_node_name}: {exc}")
                QtWidgets.QMessageBox.warning(
                    self, "Kitsu",
                    "The media loaded, but the existing Kitsu annotations could "
                    f"not be applied to the paint node.\n\nError: {exc}"
                )

        self.export_btn.setEnabled(True)

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            f"Downloaded and loaded into RV:\n\n{rev['shot']} - {rev['task_type']} v{rev['revision']:03d}\n"
            f"(saved to: {file_path})\n"
            f"Existing Kitsu annotations applied: {applied}\n\n"
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
        "center": 2,       # circle
        "position": 2,     # text
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
                            # NOTE: this is why the converters read every
                            # property through _first_pair/_first_scalar/_pairs
                            # rather than indexing -- a single-point pen's
                            # `width` comes out of here as a bare float.
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
        # NOTE: comments are posted to Kitsu immediately (see
        # _on_add_comment_clicked), so this just reports what's already
        # there; the annotations are what actually get pushed here.
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

        src_width, src_height = self._source_size(preview_file)
        canvas_width, canvas_height = _infer_canvas_size(
            preview_file.get("annotations") or [], src_width, src_height
        )

        # "createdBy" should be whoever is reviewing right now, not
        # whoever uploaded the preview (which is what person_id is).
        author = (self.current_user or {}).get("id") or preview_file.get("person_id")

        records = convert_openrv_annotations(
            annotations,
            width=src_width,
            height=src_height,
            fps=self._source_fps(preview_file),
            author=author,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self._push_to_kitsu(preview_file, records, [], [])
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(
                self, "Kitsu", f"Failed to send annotations to Kitsu: {exc}"
            )
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        QtWidgets.QMessageBox.information(
            self, "Kitsu",
            "Export complete!\n\n"
            f"Shot: {rev['shot']}\n"
            f"Task: {rev['task_type']}\n"
            f"Revision: v{rev['revision']:03d}\n"
            f"Comments on Kitsu: {n_comments}\n"
            f"Annotations exported: {frames_note}\n"
            f"Canvas: {canvas_width:g} x {canvas_height:g}"
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

    canvas_width, canvas_height = args.canvas_width, args.canvas_height
    if canvas_width is None or canvas_height is None:
        # Same recovery the plugin does: the objects' own canvasWidth/Height
        # is the only reliable record of the canvas Kitsu drew on.
        inferred_w, inferred_h = _infer_canvas_size(records, args.width, args.height)
        canvas_width = canvas_width if canvas_width is not None else inferred_w
        canvas_height = canvas_height if canvas_height is not None else inferred_h

    shapes = convert_kitsu_annotations(
        records,
        width=args.width,
        height=args.height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
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