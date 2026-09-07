"""Stateless helpers for reading and writing RVPaint annotation properties.

This module is the only place that knows the shape of RVPaint's property
namespace (``<paintNode>.<kind>:<id>:<frame>:<tag>.<attr>``). It holds no
state and imports neither Qt nor gazu, so the UI layer can call it as a set
of plain functions.

Coordinate/colour maths lives in ``utils``; this module only moves values in
and out of RV properties.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from utils import (
    DEFAULT_ARROW_THICKNESS,
    DEFAULT_BORDER_WIDTH,
    DEFAULT_PEN_WIDTH,
    DEFAULT_TEXT_SIZE,
    DEFAULT_TEXT_SPACING,
    _first_pair,
    _first_scalar,
    _pairs,
    _rv_color_floats,
)

try:
    import rv
    import rv.commands as rvc

    RV_AVAILABLE = True
except ImportError:  # imported standalone, e.g. from a test runner
    rv = None
    rvc = None
    RV_AVAILABLE = False


#: Tag appended to component names so our shapes are recognisable in a session.
ANNOTATION_TAG = "Kitsu"

FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")

#: Attributes stored flat in RV but meaningful as tuples of this width.
GROUPED_KEYS = {
    "points": 2,
    "color": 4,
    "innerColor": 4,
    "borderColor": 4,
    "startPos": 2,
    "endPos": 2,
    "min": 2,
    "max": 2,
    "center": 2,
    "position": 2,
}


def _warn(message: str) -> None:
    print(f"[KitsuReview] {message}", file=sys.stderr)


def _color(value: Any, default=(1.0, 1.0, 1.0, 1.0)) -> List[float]:
    return list(_rv_color_floats(value, default))


# ---------------------------------------------------------------------------
# Property primitives
# ---------------------------------------------------------------------------


def set_property(full_name: str, ptype, width: int, values: Sequence) -> None:
    """Create the property if needed, then write ``values`` into it."""
    if not rvc.propertyExists(full_name):
        rvc.newProperty(full_name, ptype, width)
    if ptype == rvc.FloatType:
        rvc.setFloatProperty(full_name, list(values), True)
    elif ptype == rvc.IntType:
        rvc.setIntProperty(full_name, list(values), True)
    elif ptype == rvc.StringType:
        rvc.setStringProperty(full_name, list(values), True)


def read_property(full_name: str) -> Optional[List]:
    """Read a property without needing to know its type up front."""
    try:
        info = rvc.propertyInfo(full_name)
    except Exception:
        return None

    ptype = info.get("type") if isinstance(info, dict) else getattr(info, "type", None)

    if ptype == rvc.FloatType:
        return rvc.getFloatProperty(full_name)
    if ptype == rvc.IntType:
        return rvc.getIntProperty(full_name)
    if ptype == rvc.StringType:
        return rvc.getStringProperty(full_name)
    return None


def _writer(base: str) -> Callable[[str, Any, int, Sequence], None]:
    """Return a ``set(attr, type, width, values)`` bound to one component."""

    def write(attr: str, ptype, width: int, values: Sequence) -> None:
        set_property(f"{base}.{attr}", ptype, width, values)

    return write


def next_paint_id(paint_node: str) -> int:
    prop = f"{paint_node}.paint.nextId"
    try:
        if rvc.propertyExists(prop):
            values = rvc.getIntProperty(prop)
            if values:
                return int(values[0])
    except Exception as exc:
        _warn(f"Could not read {prop} (starting ids at 0): {exc}")
    return 0


def frame_order(paint_node: str, frame: int) -> List[str]:
    """Ordered component names already painted on ``frame``."""
    prop = f"{paint_node}.frame:{frame}.order"
    try:
        if not rvc.propertyExists(prop):
            return []
        order = rvc.getStringProperty(prop) or []
    except Exception as exc:
        _warn(f"Could not read {prop}: {exc}")
        return []
    return [order] if isinstance(order, str) else list(order)


def paint_node_for_source(source_node: str) -> str:
    return f"{rvc.nodeGroup(source_node)}_paint"


def add_source(file_path: str) -> Optional[str]:
    """Load media into the current session, or None if RV declined."""
    try:
        return rvc.addSourceVerbose([file_path])
    except Exception as exc:
        _warn(f"Skipped adding source to RV session: {exc}")
        return None


def component_name(kind: str, shape_id: int, frame: int) -> str:
    return f"{kind}:{shape_id}:{frame}:{ANNOTATION_TAG}"


# ---------------------------------------------------------------------------
# Writing shapes (Kitsu -> RVPaint)
# ---------------------------------------------------------------------------


def apply_pen(paint_node: str, props: Dict[str, Any], shape_id: int, frame: int) -> str:
    points = _pairs(props.get("points"))

    width = props.get("width", [DEFAULT_PEN_WIDTH])
    if not isinstance(width, list):
        width = [width]
    if len(width) != len(points):
        width = [_first_scalar(width, DEFAULT_PEN_WIDTH)] * len(points)

    cname = component_name("pen", shape_id, frame)
    write = _writer(f"{paint_node}.{cname}")

    write("color", rvc.FloatType, 4, _color(props.get("color")))
    write("width", rvc.FloatType, 1, width)
    write("brush", rvc.StringType, 1, ["circle"])
    write("points", rvc.FloatType, 2, [c for xy in points for c in xy])
    write("debug", rvc.IntType, 1, [0])
    write("join", rvc.IntType, 1, [3])
    write("cap", rvc.IntType, 1, [1])
    write("splat", rvc.IntType, 1, [0])
    return cname


def apply_text(paint_node: str, props: Dict[str, Any], shape_id: int, frame: int) -> str:
    cname = component_name("text", shape_id, frame)
    write = _writer(f"{paint_node}.{cname}")

    write("position", rvc.FloatType, 2, list(_first_pair(props.get("position"))))
    write("color", rvc.FloatType, 4, _color(props.get("color")))
    write("spacing", rvc.FloatType, 1,
          [_first_scalar(props.get("spacing"), DEFAULT_TEXT_SPACING)])
    write("size", rvc.FloatType, 1,
          [_first_scalar(props.get("size"), DEFAULT_TEXT_SIZE)])
    write("scale", rvc.FloatType, 1, [_first_scalar(props.get("scale"), 1.0)])
    write("rotation", rvc.FloatType, 1, [_first_scalar(props.get("rotation"), 0.0)])
    write("font", rvc.StringType, 1, [props.get("font") or ""])
    write("text", rvc.StringType, 1, [str(props.get("text", ""))])
    write("origin", rvc.StringType, 1, [""])
    write("debug", rvc.IntType, 1, [0])
    return cname


def _apply_box(
    kind: str, paint_node: str, props: Dict[str, Any], shape_id: int, frame: int
) -> str:
    """Shared body for the two min/max box shapes (rect and ellipse)."""
    cname = component_name(kind, shape_id, frame)
    write = _writer(f"{paint_node}.{cname}")

    write("min", rvc.FloatType, 2, list(_first_pair(props.get("min"))))
    write("max", rvc.FloatType, 2, list(_first_pair(props.get("max"), (0.1, 0.1))))
    write("innerColor", rvc.FloatType, 4,
          _color(props.get("innerColor"), default=(0.0, 0.0, 0.0, 0.0)))
    write("borderColor", rvc.FloatType, 4, _color(props.get("borderColor")))
    write("borderWidth", rvc.FloatType, 1,
          [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
    return cname


def apply_rect(paint_node: str, props: Dict[str, Any], shape_id: int, frame: int) -> str:
    return _apply_box("rect", paint_node, props, shape_id, frame)


def apply_ellipse(paint_node: str, props: Dict[str, Any], shape_id: int, frame: int) -> str:
    return _apply_box("ellipse", paint_node, props, shape_id, frame)


def apply_line(paint_node: str, props: Dict[str, Any], shape_id: int, frame: int) -> str:
    cname = component_name("line", shape_id, frame)
    write = _writer(f"{paint_node}.{cname}")

    write("startPos", rvc.FloatType, 2, list(_first_pair(props.get("startPos"))))
    write("endPos", rvc.FloatType, 2, list(_first_pair(props.get("endPos"), (0.1, 0.0))))
    write("borderColor", rvc.FloatType, 4, _color(props.get("borderColor")))
    write("borderWidth", rvc.FloatType, 1,
          [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
    return cname


def apply_arrow(paint_node: str, props: Dict[str, Any], shape_id: int, frame: int) -> str:
    cname = component_name("arrow", shape_id, frame)
    write = _writer(f"{paint_node}.{cname}")

    border_color = _color(props.get("borderColor"))

    write("startPos", rvc.FloatType, 2, list(_first_pair(props.get("startPos"))))
    write("endPos", rvc.FloatType, 2, list(_first_pair(props.get("endPos"), (0.1, 0.0))))
    write("innerColor", rvc.FloatType, 4,
          _color(props.get("innerColor"), default=border_color))
    write("borderColor", rvc.FloatType, 4, border_color)
    write("thickness", rvc.FloatType, 1,
          [_first_scalar(props.get("thickness"), DEFAULT_ARROW_THICKNESS)])
    write("borderWidth", rvc.FloatType, 1,
          [_first_scalar(props.get("borderWidth"), DEFAULT_BORDER_WIDTH)])
    return cname


#: Shape type -> writer. Add a live-property group here to support a new type.
SHAPE_WRITERS: Dict[str, Callable[[str, Dict[str, Any], int, int], str]] = {
    "pen": apply_pen,
    "text": apply_text,
    "rect": apply_rect,
    "rectangle": apply_rect,
    "ellipse": apply_ellipse,
    "line": apply_line,
    "arrow": apply_arrow,
}


def apply_annotations(
    paint_node: str, annotations: Iterable[Dict[str, Any]]
) -> int:
    """Paint ``annotations`` onto ``paint_node``, returning how many landed.

    Existing components on a frame are preserved: new names are appended to
    that frame's draw order rather than replacing it.
    """
    set_property(f"{paint_node}.paint.show", rvc.IntType, 1, [1])

    shape_id = next_paint_id(paint_node)
    new_order: Dict[int, List[str]] = {}
    applied = 0

    for shape in annotations:
        shape_type = shape.get("type")
        writer = SHAPE_WRITERS.get(shape_type)
        if writer is None:
            _warn(
                f"Live-apply: skipping unsupported shape type {shape_type!r} "
                "(no RVPaint live-property group wired up yet)"
            )
            continue

        frame = int(shape["frame"])
        cname = writer(paint_node, shape["properties"], shape_id, frame)

        shape_id += 1
        applied += 1
        new_order.setdefault(frame, []).append(cname)

    for frame, names in new_order.items():
        existing = frame_order(paint_node, frame)
        merged = existing + [n for n in names if n not in existing]
        set_property(f"{paint_node}.frame:{frame}.order", rvc.StringType, 1, merged)

    set_property(f"{paint_node}.paint.nextId", rvc.IntType, 1, [shape_id])

    rvc.redraw()
    return applied


# ---------------------------------------------------------------------------
# Reading shapes (RVPaint -> plain dicts)
# ---------------------------------------------------------------------------


def _regroup(attr: str, value: Any) -> Any:
    """Turn a flat property list back into tuples where the attr expects them."""
    group_size = GROUPED_KEYS.get(attr)
    if (
        group_size
        and isinstance(value, list)
        and value
        and len(value) % group_size == 0
    ):
        return [list(value[i:i + group_size]) for i in range(0, len(value), group_size)]
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def _component_properties(
    node: str, item_name: str, all_props: Sequence[str]
) -> Dict[str, Any]:
    prefix = f"{node}.{item_name}."
    properties: Dict[str, Any] = {}
    for prop in all_props:
        if not prop.startswith(prefix):
            continue
        attr = prop[len(prefix):]
        properties[attr] = _regroup(attr, read_property(prop))
    return properties


def gather_node_annotations(node: str) -> List[Dict[str, Any]]:
    """Every painted component on one RVPaint node, as plain dicts."""
    try:
        all_props = rvc.properties(node)
    except Exception as exc:
        _warn(f"Skipped paint node {node}: {exc}")
        return []

    annotations = []
    for prop in all_props:
        match = FRAME_ORDER_RE.search(prop)
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
            annotations.append({
                "frame": frame,
                "node": node,
                "name": item_name,
                "type": item_name.split(":")[0] if ":" in item_name else item_name,
                "properties": _component_properties(node, item_name, all_props),
            })
    return annotations


def gather_annotations() -> List[Dict[str, Any]]:
    """Every painted component in the session, sorted by frame."""
    try:
        paint_nodes = rvc.nodesOfType("RVPaint")
    except Exception as exc:
        _warn(f"Could not query paint nodes: {exc}")
        return []

    annotations: List[Dict[str, Any]] = []
    for node in paint_nodes:
        annotations.extend(gather_node_annotations(node))

    annotations.sort(key=lambda a: a["frame"])
    return annotations