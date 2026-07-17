#!/usr/bin/env python3
"""
Usage:
python3 ./src/map_annotations.py openrv_annotations.json 5e0ecd69-1559-41a3-b4da-dc1c9d1e0b5c --width 1920 --height 1080
openrv_to_kitsu.py
==================

Convert OpenRV / RV native paint-annotation dumps (pen strokes, lines,
ellipses exported from RV's "sourceGroup" paint nodes) into Kitsu's real
preview-annotation format, and push them with
``gazu.files.update_preview_annotations``.

--------------------------------------------------------------------------
Target format (Kitsu)
--------------------------------------------------------------------------
Kitsu's annotation tool is built on Fabric.js. Annotations for a preview
file are stored as a list of *per-frame* records:

    [
      {
        "time": 0,          # this frame's offset into the timeline, in ms
        "frame": 1,
        "drawing": {
          "objects": [ <fabric-style object>, <fabric-style object>, ... ]
        }
      },
      ...
    ]

Each object inside "drawing.objects" is a serialized Fabric.js object.
For freehand pen strokes Kitsu uses a custom subclass called "PSStroke"
(Paint-Stroke), e.g.:

    {
      "id": "...", "type": "PSStroke",
      "left": <bbox left px>, "top": <bbox top px>,
      "width": <bbox width px>, "height": <bbox height px>,
      "stroke": "#ff3860", "strokeWidth": 20, "opacity": 1,
      "canvasWidth": 1697.77, "canvasHeight": 955,
      "strokePoints": [{"x":.., "y":.., "type":"PSPoint", "pressure":1}, ...],
      "createdBy": "<user id>",
      "startTime": <ms>, "endTime": <ms>,
      ... (a bunch of Fabric.js boilerplate: angle, flipX/Y, skewX/Y,
           scaleX/Y, originX/Y, version, visible, erasable, fillRule,
           paintFirst, strokeLineCap/Join, strokeUniform, strokeDashArray,
           strokeDashOffset, strokeMiterLimit, globalCompositeOperation)
    }

This script only has a confirmed sample for "PSStroke" (pen). "line" and
"ellipse" shapes are mapped onto **native Fabric.js object types**
("line" / "ellipse") using the same styling conventions, since Kitsu's
canvas is Fabric.js underneath -- but if your Kitsu deployment uses its
own custom subclasses for those too (e.g. "PSLine" / "PSEllipse" with
extra bookkeeping fields like PSStroke has), you may need to add those
fields in ``_line_to_fabric`` / ``_ellipse_to_fabric`` below. Those two
functions are intentionally isolated so you can adjust them without
touching anything else.

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
    * "canvasWidth" / "canvasHeight" define the pixel space the points,
      left/top/width/height are expressed in (this can differ slightly
      from your actual video resolution -- e.g. the sample uses
      1697.77 x 955 -- so it's exposed as its own --canvas-width /
      --canvas-height CLI options, defaulting to --width / --height).

Conversion (see ``rv_normalized_to_pixel``):

    aspect = width / height          (aspect of the SOURCE video/image)
    px     = (nx / aspect + 1) / 2 * canvas_width
    py     = (1 - ny) / 2 * canvas_height

--------------------------------------------------------------------------
Time fields
--------------------------------------------------------------------------
* "time" (per frame record) is the frame's offset into the playback
  timeline in milliseconds: ``(frame - frame_offset_base) / fps * 1000``.
  This is meaningful and used by Kitsu to scrub/seek.
* "startTime" / "endTime" (per PSStroke) look like wall-clock telemetry
  of when the artist began/finished drawing that particular stroke --
  cosmetic, not structurally required. Since OpenRV doesn't record this,
  this script synthesizes monotonically increasing millisecond values
  (current time + a per-point estimate) rather than inventing fake
  "real" timestamps.
"""

from __future__ import annotations

import sys
import time as _time
import uuid
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

MS_PER_STROKE_POINT = 8  # rough authoring-speed estimate for startTime/endTime


# --------------------------------------------------------------------------
# Coordinate / color conversion
# --------------------------------------------------------------------------

def rv_normalized_to_pixel(
    nx: float, ny: float, width: int, height: int,
    canvas_width: float, canvas_height: float,
) -> Point:
    """Convert an RV paint-space normalized point to Fabric.js canvas pixels.

    `width`/`height` = source video/image resolution (defines the aspect
    ratio RV normalized its coordinates against).
    `canvas_width`/`canvas_height` = the annotation canvas's own pixel
    space that the output point should be expressed in (usually the same
    as width/height, but Kitsu can store its own canvas size).
    """
    aspect = width / height
    px = (nx / aspect + 1.0) / 2.0 * canvas_width
    py = (1.0 - ny) / 2.0 * canvas_height
    return px, py


def rv_color_to_hex(color_rows: Sequence[Sequence[float]]) -> str:
    """RV stores color as a list of [r, g, b, a] floats in 0..1.
    Kitsu's "stroke" field is a plain hex string, e.g. "#ff3860"."""
    if not color_rows:
        return "#ffffff"
    r, g, b, _a = color_rows[0]
    return "#{:02x}{:02x}{:02x}".format(
        round(r * 255), round(g * 255), round(b * 255)
    )


def rv_alpha(color_rows: Sequence[Sequence[float]]) -> float:
    if not color_rows:
        return 1.0
    return color_rows[0][3]


# --------------------------------------------------------------------------
# Fabric.js boilerplate shared by every object
# --------------------------------------------------------------------------

def _fabric_base(
    obj_type: str,
    left: float, top: float, width: float, height: float,
    stroke_hex: str, stroke_width: float, opacity: float,
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


# --------------------------------------------------------------------------
# Per-shape converters -> Fabric.js objects
# --------------------------------------------------------------------------

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
    clock_ms[0] = end_time  # advance shared clock so strokes don't overlap

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
    obj["fill"] = (
        rv_color_to_hex(props.get("innerColor", [])) if inner_alpha > 0 else None
    )
    return obj


_SHAPE_CONVERTERS = {
    "pen": _pen_to_fabric,
    "line": _line_to_fabric,
    "ellipse": _ellipse_to_fabric,
}


# --------------------------------------------------------------------------
# Top-level conversion
# --------------------------------------------------------------------------

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
        frame_base: the OpenRV frame number that corresponds to
            Kitsu "time": 0 (i.e. the first frame of the shot/clip on
            Kitsu's timeline).
        skip_soft_deleted: if True (default), shapes with
            softDeleted=1 are dropped; pass False to keep them.
    """
    author = author or str(uuid.uuid4())
    canvas_width = canvas_width or float(width)
    canvas_height = canvas_height or float(height)

    # group OpenRV shapes by (converted) frame number, preserving order
    by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for shape in openrv_shapes:
        if skip_soft_deleted and shape.get("properties", {}).get("softDeleted"):
            continue
        frame_num = shape["frame"] + frame_offset
        by_frame.setdefault(frame_num, []).append(shape)

    print(json.dumps(by_frame, indent=2))
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