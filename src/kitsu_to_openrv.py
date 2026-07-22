#!/usr/bin/env python3
"""
kitsu_to_openrv.py
===================

Inverse of ``openrv_to_kitsu.py``: takes Kitsu's real preview-annotation
format (Fabric.js objects, as returned by e.g.
``gazu.files.get_preview_file_annotations`` /
``preview.get('annotations')``) and converts it back into OpenRV / RV
native paint-annotation shapes (the same "pen" / "line" / "ellipse"
shape dicts RV exports).

This is a best-effort inverse. A few notes on round-tripping:

* All the pure Fabric.js/CSS boilerplate on each object (angle, flipX/Y,
  skewX/Y, scaleX/Y, version, shadow, erasable, fillRule, paintFirst,
  strokeLineCap/Join, strokeUniform, strokeDashArray/Offset,
  strokeMiterLimit, globalCompositeOperation, backgroundColor, ...) has
  no OpenRV equivalent and is simply discarded.
* "startTime"/"endTime" on PSStroke are cosmetic wall-clock telemetry
  (see openrv_to_kitsu.py) and are dropped rather than reconstructed.
* "createdBy" (Kitsu person id) has no OpenRV field and is dropped,
  though it's returned separately by ``extract_authors`` below in case
  the caller wants to preserve it out-of-band.
* The ellipse's "min"/"max" bookkeeping only ever stored an axis-aligned
  bounding box in OpenRV, so reconstructing it from the Fabric bbox
  (left/top/width/height) round-trips exactly -- there's no information
  about which literal corner was "min" vs "max" to lose in the first
  place.
* "id" on the Fabric object is carried through as OpenRV's
  "uuid" property so that a round trip (RV -> Kitsu -> RV) preserves
  shape identity.
* Color rows ("color" / "borderColor" / "innerColor") are emitted as
  [r, g, b, a] with each channel an INTEGER 0..255 -- confirmed against
  real RV round-tripping. (openrv_to_kitsu.py's original comment assumed
  0..1 floats; that assumption turned out to be wrong.)

Coordinate systems are the same as in ``openrv_to_kitsu.py``, just
inverted:

    Kitsu/Fabric.js (pixel space):
        * origin (0, 0) top-left, X right, Y down
    OpenRV paint space (normalized):
        * origin (0, 0) center of image, Y up, spans [-1, 1]
        * X scaled by aspect = width / height, spans [-aspect, aspect]

    nx = aspect * (2 * px / canvas_width - 1)
    ny = 1 - 2 * py / canvas_height
"""

from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# Fabric.js/CSS spellings that mean "no color here" rather than an actual
# hex value. Kitsu (and hand-edited/older annotation data) can hand back
# any of these for "stroke" or "fill" instead of a real "#rrggbb" string.
_NO_COLOR_VALUES = {"none", "null", "transparent", ""}


def _is_hex_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value.strip()))


def _is_no_color(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NO_COLOR_VALUES)


def _has_color(value: Any) -> bool:
    """True if `value` is an actual color (rgba array or hex string),
    as opposed to a "none"/"transparent"/None sentinel."""
    if _is_no_color(value):
        return False
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return True
    return _is_hex_color(value)


# --------------------------------------------------------------------------
# Coordinate / color conversion (inverse of the forward helpers)
# --------------------------------------------------------------------------

def pixel_to_rv_normalized(
    px: float, py: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    """Inverse of ``rv_normalized_to_pixel``: Fabric.js canvas pixels ->
    RV paint-space normalized point.

    `width`/`height` = source video/image resolution (defines the aspect
    ratio RV normalizes its coordinates against).
    `canvas_width`/`canvas_height` = the annotation canvas's own pixel
    space that (px, py) are expressed in.
    """
    aspect = width / height
    nx = aspect * (2.0 * px / canvas_width - 1.0)
    ny = 1.0 - 2.0 * py / canvas_height
    return nx, ny


def color_to_rv_color(value: Any, opacity: float = 1.0) -> List[List[int]]:
    """Inverse of ``rv_color_to_hex``/``rv_alpha``: a Kitsu/Fabric "stroke"
    or "fill" value -> RV's [[r, g, b, a]] color row, each channel an
    INTEGER 0..255 (this is RV's actual on-disk format).

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

    print(
        f"warning: unrecognized color value {value!r}, falling back to white",
        file=sys.stderr,
    )
    return [255, 255, 255, fallback_alpha]


# kept as an alias -- some callers/older code may still import this name
hex_to_rv_color = color_to_rv_color


# --------------------------------------------------------------------------
# Per-shape converters -> OpenRV shapes
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Top-level conversion
# --------------------------------------------------------------------------

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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kitsu_json", help="Path to a JSON file containing the Kitsu annotation records list")
    parser.add_argument("--width", type=int, required=True, help="Source video/image width in px")
    parser.add_argument("--height", type=int, required=True, help="Source video/image height in px")
    parser.add_argument("--canvas-width", type=float, default=None)
    parser.add_argument("--canvas-height", type=float, default=None)
    parser.add_argument("--frame-offset", type=int, default=0)
    parser.add_argument("-o", "--output", default=None, help="Where to write the OpenRV shapes JSON (default: stdout)")
    args = parser.parse_args()

    import json as _json

    with open(args.kitsu_json) as f:
        records = _json.load(f)

    result = convert_kitsu_annotations(
        records,
        width=args.width,
        height=args.height,
        canvas_width=args.canvas_width,
        canvas_height=args.canvas_height,
        frame_offset=args.frame_offset,
    )

    out = _json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
    else:
        print(out)