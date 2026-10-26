#!/usr/bin/env python3
"""Kitsu <-> OpenRV annotation conversion.

Pure Python with no OpenRV, Qt or gazu dependency, so it can be unit
tested and run standalone (see the CLI at the bottom).

Public API:
    convertToKitsu(openrv_shapes, width, height, ...) -> Kitsu annotation records
    convertToORV(kitsu_records, width, height, ...)   -> OpenRV paint shapes
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import re
import sys
import time as _time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "convertToKitsu", "convertToORV", "extract_authors", "build_paint_gto",
    "frame_to_time_ms", "time_ms_to_frame", "DEFAULT_FPS",
]

Point = Tuple[float, float]

MS_PER_STROKE_POINT = 8

# Fabric.js version Kitsu currently writes into annotation objects.
FABRIC_VERSION = "7.4.0"

# How close (canvas px) a converted stroke must be to an existing Kitsu
# stroke to be treated as the same, unchanged stroke.
MATCH_TOLERANCE_PX = 0.05

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

_NO_COLOR_VALUES = {"none", "null", "transparent", ""}


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


def frame_to_time_ms(frame_num: int, fps: float, frame_base: int = 1) -> int:
    """Kitsu record "time" (milliseconds from the first frame) for a frame
    number, at the production's frame rate."""
    return round((frame_num - frame_base) / _check_fps(fps) * 1000)


def time_ms_to_frame(time_ms: float, fps: float, frame_base: int = 1) -> int:
    """Inverse of `frame_to_time_ms`."""
    return frame_base + round(float(time_ms) / 1000.0 * _check_fps(fps))


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


def _reference_index(
    kitsu_records: Optional[Sequence[Dict[str, Any]]],
) -> Dict[Optional[int], List[Dict[str, Any]]]:
    """Existing Kitsu PSStroke objects grouped by frame."""
    index: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for record in kitsu_records or []:
        frame = record.get("frame")
        frame = int(frame) if frame is not None else None
        for obj in record.get("drawing", {}).get("objects", []):
            if obj.get("type") == "PSStroke":
                index.setdefault(frame, []).append(obj)
    return index


def _match_reference(
    obj: Dict[str, Any], frame_num: int,
    index: Dict[Optional[int], List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Find (and consume) the existing Kitsu object this converted one is an
    unchanged round trip of. RV can't carry Kitsu's id, author, timestamps,
    or the split between strokeWidth and pressure, so without this every
    sync would re-create the stroke under a new id."""
    if obj.get("type") != "PSStroke":
        return None
    for key in (frame_num, None):
        candidates = index.get(key, [])
        for i, ref in enumerate(candidates):
            if _same_stroke(obj, ref):
                return candidates.pop(i)
    return None


def _iso_ms(epoch_ms: int) -> str:
    dt = _dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_SHAPE_CONVERTERS = {
    "pen": _pen_to_fabric,
    "line": _line_to_fabric,
    "ellipse": _ellipse_to_fabric,
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
    dicts, as gathered from RVPaint nodes.

    `fps` is the production's frame rate; each record's "time" is derived
    from its frame number with it (see `frame_to_time_ms`).

    Canvas size: an explicit `canvas_width`/`canvas_height` wins. Otherwise
    it is read off any object in `reference_records` (the preview's existing
    Kitsu annotations), falling back to the media `width`/`height`.

    Pass the preview's existing annotations as `reference_records` when
    syncing back: any stroke that comes back from RV unchanged is returned
    as the original Kitsu object (same id, author, timestamps, strokeWidth
    and pressures) instead of a re-created copy.
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
            obj = converter(
                shape, width, height, canvas_width, canvas_height,
                author, clock_ms,
            )
            ref = _match_reference(obj, frame_num, ref_index)
            if ref is not None:
                obj = copy.deepcopy(ref)
            else:
                obj.setdefault("createdAt", _iso_ms(obj["endTime"]))
            objects.append(obj)

        if not objects:
            continue

        records.append({
            "time": frame_to_time_ms(frame_num, fps, frame_base),
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
    ({"type", "frame", "properties"}).

    Each object's own canvasWidth/canvasHeight is used when present;
    otherwise `canvas_width`/`canvas_height`, then the media size.

    A record's "frame" is used when present. Without one, its "time" is
    converted to a frame at the production `fps` (the inverse of
    `convertToKitsu`, counting from `frame_base`). Records with neither,
    or when no `fps` is given, land on `default_frame` (still previews).
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
            frame_num = time_ms_to_frame(raw_time, fps, frame_base)
        else:
            frame_num = default_frame
        frame_num -= frame_offset
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


def _pen_block(pen: Dict[str, Any], pen_id: int, frame: int) -> Tuple[str, str]:
    name = f'"pen:{pen_id}:{frame}:Kitsu"'
    color = pen["color"]
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