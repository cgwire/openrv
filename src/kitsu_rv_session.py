"""
Read and write the open RV session: sources, frames and paint nodes.
"""

import re
from typing import Dict, List

from kitsu_review_log import log

try:
    import rv.commands as rvc
except ImportError:
    rvc = None

_FRAME_ORDER_RE = re.compile(r"\bframe:(\d+)\b.*\.order$")


class RvSession:
    """
    Read and write the open RV session: sources, frame ranges and the
    paint-node properties where RV stores annotations.
    """

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

    @staticmethod
    def current_frame():
        try:
            return rvc.frame()
        except Exception:
            return None

    @staticmethod
    def add_source(file_path):
        try:
            return rvc.addSourceVerbose([file_path])
        except Exception as exc:
            log(f"Skipped adding source to RV session: {exc}")
            return None

    @staticmethod
    def set_fps(fps, source_node=None):
        """
        Make RV play at `fps`. If a source is given, also pin its media to that
        rate (the `group.fps` override of the RVFileSource). Then RV counts
        frames as Kitsu does. The setting also stays when RV derives the
        playback rate again after the view changes.
        """
        if source_node:
            prop = f"{source_node}.group.fps"
            try:
                if rvc.propertyExists(prop):
                    rvc.setFloatProperty(prop, [float(fps)], True)
            except Exception as exc:
                log(f"Could not set {prop} to {fps}: {exc}")
        try:
            rvc.setFPS(float(fps))
        except Exception as exc:
            log(f"Could not set RV playback FPS to {fps}: {exc}")
            return False
        return True

    @staticmethod
    def paint_node_for(source_node):
        return f"{rvc.nodeGroup(source_node)}_paint"

    @staticmethod
    def source_start_frame(source_node, default=1):
        """
        Return the first frame of a source in the current session. A still
        image uses exactly one frame. The frameless annotations of Kitsu must
        go on that frame.
        """
        try:
            info = rvc.nodeRangeInfo(source_node) or {}
        except Exception as exc:
            log(f"Could not query frame range for {source_node}: {exc}")
            return default
        start = info.get("start") if isinstance(info, dict) else None
        try:
            return int(start)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def paint_nodes_for_source(source_group):
        """
        Return every RVPaint node that draws on one source, upstream first.

        First comes the paint node in the group of the source. Then come the
        per-input paint nodes that RV puts in each sequence, retime or switch
        group that the source feeds ("<group>_p_<sourceGroup>").

        The annotate mode of RV draws on the first paint node that it finds
        from the view downward. So in a sequence view, the strokes made in RV
        go in "defaultSequence_p_..." and not in the paint node of the source.
        Those per-input nodes are before the retime, so their frames are source
        frames too.

        RV renders the strokes of all paint nodes into one list, upstream node
        first. So an eraser in a downstream node erases strokes from the node
        of the source. Read the nodes in this order to keep this true.
        """
        try:
            all_paint = list(rvc.nodesOfType("RVPaint") or [])
        except Exception as exc:
            log(f"Could not query paint nodes: {exc}")
            return []

        own, downstream = [], []
        suffix = f"_p_{source_group}"
        for node in all_paint:
            try:
                group = rvc.nodeGroup(node)
            except Exception:
                group = None
            if group == source_group:
                own.append(node)
            elif node.endswith(suffix):
                downstream.append(node)
        return own + sorted(downstream)

    @staticmethod
    def _pipeline_order(paint_nodes):
        """
        Return the paint nodes with those in source groups first. They are
        upstream of any view-level paint node, so RV draws their strokes first
        (see `paint_nodes_for_source`).
        """

        def is_view_level(node):
            try:
                return rvc.nodeType(rvc.nodeGroup(node)) != "RVSourceGroup"
            except Exception:
                return True

        return sorted(paint_nodes, key=is_view_level)

    def current_source_paint_nodes(self):
        """
        Return (sources, paint_nodes) for the sources under the playhead.

        In a stack or layout view, several sources can be visible at once. So
        return all of them, with every paint node that draws on each (see
        `paint_nodes_for_source`).
        """
        try:
            sources = list(rvc.sourcesAtFrame(rvc.frame()) or [])
        except Exception as exc:
            log(f"Could not query sources at current frame: {exc}")
            return [], []

        paint_nodes: List[str] = []
        for source in sources:
            try:
                group = rvc.nodeGroup(source)
            except Exception as exc:
                log(f"Could not inspect group of {source}: {exc}")
                continue
            for node in self.paint_nodes_for_source(group):
                if node not in paint_nodes:
                    paint_nodes.append(node)
        return sources, paint_nodes

    @staticmethod
    def _set_prop(full_name, ptype, width, values):
        if not rvc.propertyExists(full_name):
            rvc.newProperty(full_name, ptype, width)
        if ptype == rvc.FloatType:
            rvc.setFloatProperty(full_name, values, True)
        elif ptype == rvc.IntType:
            rvc.setIntProperty(full_name, values, True)
        elif ptype == rvc.StringType:
            rvc.setStringProperty(full_name, values, True)

    @staticmethod
    def _color_float(color, default=(255, 255, 255, 255)):
        """
        Convert a color from `convertToORV` (flat [r, g, b, a], 0..255) to the
        0..1 floats that RV paint properties hold.
        """
        color = color or list(default)
        if isinstance(color[0], (list, tuple)):
            color = color[0]
        return [c / 255.0 for c in color]

    def _set_props(self, base, specs):
        """
        Write the (attribute, type, width, values) specs in order.
        """
        for attr, ptype, width, values in specs:
            self._set_prop(f"{base}.{attr}", ptype, width, values)

    @staticmethod
    def _timing_specs(frame, props):
        return [
            (
                "startFrame",
                rvc.IntType,
                1,
                [int(props.get("startFrame", frame))],
            ),
            ("duration", rvc.IntType, 1, [int(props.get("duration", 1))]),
            ("eye", rvc.IntType, 1, [int(props.get("eye", 2))]),
        ]

    @staticmethod
    def _uuid_specs(props):
        if not props.get("uuid"):
            return []
        return [("uuid", rvc.StringType, 1, [str(props["uuid"])])]

    @staticmethod
    def _soft_deleted_spec(props):
        return (
            "softDeleted",
            rvc.IntType,
            1,
            [int(props.get("softDeleted", 0))],
        )

    def _write_pen(self, base, frame, props):
        points = props["points"]

        width = props.get("width", [0.003])
        if not isinstance(width, list):
            width = [width]
        if len(width) != len(points):
            width = [width[0]] * len(points)

        self._set_props(
            base,
            [
                (
                    "color",
                    rvc.FloatType,
                    4,
                    self._color_float(props.get("color")),
                ),
                ("width", rvc.FloatType, 1, width),
                ("brush", rvc.StringType, 1, ["circle"]),
                ("points", rvc.FloatType, 2, [c for xy in points for c in xy]),
                ("debug", rvc.IntType, 1, [0]),
                ("join", rvc.IntType, 1, [3]),
                ("cap", rvc.IntType, 1, [1]),
                ("splat", rvc.IntType, 1, [0]),
                # 0 paints. 1 erases what the earlier strokes drew (Kitsu
                # erasers).
                ("mode", rvc.IntType, 1, [int(props.get("mode") or 0)]),
                *self._uuid_specs(props),
            ],
        )

    def _write_box(self, base, frame, props):
        """
        Write the properties that RV writes for its own ellipse or rect.

        RV writes them when you draw with its annotate tools. To see them,
        look at a gathered RV shape in the Dev tab.

        The two shapes share one property set. They differ only in the prefix
        "ellipse:" or "rect:" of the component name.
        """
        inner = self._color_float(
            props.get("innerColor"), default=(255, 255, 255, 0)
        )
        border = self._color_float(props.get("borderColor"))
        border_width = float(props.get("borderWidth", 0.003))
        self._set_props(
            base,
            [
                *self._timing_specs(frame, props),
                ("min", rvc.FloatType, 2, [float(v) for v in props["min"][0]]),
                ("max", rvc.FloatType, 2, [float(v) for v in props["max"][0]]),
                ("innerColor", rvc.FloatType, 4, inner),
                ("borderColor", rvc.FloatType, 4, border),
                ("borderWidth", rvc.FloatType, 1, [border_width]),
                *self._uuid_specs(props),
                self._soft_deleted_spec(props),
            ],
        )

    def _write_arrow(self, base, frame, props):
        """
        Write the same properties that RV writes for an arrow drawn with its
        own annotate tool. The tail is at `startPos` and the head is at
        `endPos`. The line width is both `thickness` and `borderWidth`. The
        color is in `borderColor` and `innerColor`.
        """
        border = self._color_float(props.get("borderColor"))
        inner = (
            self._color_float(props["innerColor"])
            if props.get("innerColor")
            else border
        )
        thickness = float(
            props.get("thickness", props.get("borderWidth", 0.003))
        )
        border_width = float(props.get("borderWidth", thickness))
        start = [float(v) for v in props["startPos"][0]]
        end = [float(v) for v in props["endPos"][0]]
        self._set_props(
            base,
            [
                *self._timing_specs(frame, props),
                ("startPos", rvc.FloatType, 2, start),
                ("endPos", rvc.FloatType, 2, end),
                ("borderColor", rvc.FloatType, 4, border),
                ("borderWidth", rvc.FloatType, 1, [border_width]),
                ("innerColor", rvc.FloatType, 4, inner),
                ("thickness", rvc.FloatType, 1, [thickness]),
                *self._uuid_specs(props),
                self._soft_deleted_spec(props),
            ],
        )

    def _write_text(self, base, frame, props):
        """
        Write the same properties that RV writes for text placed with its own
        annotate tool. `position` is the left end of the first baseline.
        `fontSize` is the text height as a fraction of the image height. `size`
        is the legacy equivalent that older RV builds read (`fontSize` / 12.5
        at scale 1).
        """
        position = props["position"]
        if isinstance(position[0], (list, tuple)):
            position = position[0]
        scale = float(props.get("scale", 1.0)) or 1.0
        font_size = float(props.get("fontSize", 0.05))
        size = float(props.get("size", font_size / scale / 12.5))

        def text_spec(attr, default):
            return (attr, rvc.StringType, 1, [str(props.get(attr, default))])

        def number_spec(attr, ptype, cast, default):
            return (attr, ptype, 1, [cast(props.get(attr, default))])

        self._set_props(
            base,
            [
                (
                    "position",
                    rvc.FloatType,
                    2,
                    [float(v) for v in position[:2]],
                ),
                (
                    "color",
                    rvc.FloatType,
                    4,
                    self._color_float(props.get("color")),
                ),
                number_spec("spacing", rvc.FloatType, float, 0.8),
                ("size", rvc.FloatType, 1, [size]),
                ("scale", rvc.FloatType, 1, [scale]),
                number_spec("rotation", rvc.FloatType, float, 0.0),
                text_spec("font", ""),
                text_spec("text", ""),
                text_spec("origin", ""),
                number_spec("debug", rvc.IntType, int, 0),
                number_spec("startFrame", rvc.IntType, int, frame),
                number_spec("duration", rvc.IntType, int, 1),
                number_spec("mode", rvc.IntType, int, 0),
                text_spec("fontFamily", ""),
                ("fontSize", rvc.FloatType, 1, [font_size]),
                text_spec("fontWeight", "normal"),
                text_spec("fontStyle", "normal"),
                text_spec("textDecoration", "none"),
                text_spec("textAlign", "left"),
                *self._uuid_specs(props),
                self._soft_deleted_spec(props),
            ],
        )

    _SHAPE_WRITERS = {
        "pen": "_write_pen",
        "ellipse": "_write_box",
        "rect": "_write_box",
        "arrow": "_write_arrow",
        "text": "_write_text",
    }

    def apply_annotations_live(self, paint_node, openrv_annotations):
        """
        openrv_annotations: flat list of shapes as returned by
        `convertToORV()`, that is [{"type", "frame", "properties"}, ...].
        """
        self._set_prop(f"{paint_node}.paint.show", rvc.IntType, 1, [1])

        next_id = 0
        frame_order: Dict[int, List[str]] = {}

        for shape in openrv_annotations:
            kind = shape.get("type")
            writer = self._SHAPE_WRITERS.get(kind)
            if writer is None:
                log(
                    f"Live-apply: skipping unsupported shape "
                    f"type {kind!r}",
                )
                continue

            frame = int(shape["frame"])
            cname = f"{kind}:{next_id}:{frame}:Kitsu"
            getattr(self, writer)(
                f"{paint_node}.{cname}", frame, shape["properties"]
            )

            frame_order.setdefault(frame, []).append(cname)
            next_id += 1

        for frame, names in frame_order.items():
            self._set_prop(
                f"{paint_node}.frame:{frame}.order", rvc.StringType, 1, names
            )

        rvc.redraw()

    @staticmethod
    def _property_value(prop):
        try:
            info = rvc.propertyInfo(prop)
        except Exception:
            return None

        ptype = (
            info.get("type")
            if isinstance(info, dict)
            else getattr(info, "type", None)
        )

        if ptype == rvc.FloatType:
            return rvc.getFloatProperty(prop)
        if ptype == rvc.IntType:
            return rvc.getIntProperty(prop)
        if ptype == rvc.StringType:
            return rvc.getStringProperty(prop)
        return None

    @classmethod
    def _shaped_value(cls, attr, value):
        """
        Convert a raw flat property value to the form that the shape dicts
        hold. Group it (points, colors, ...), or unwrap it if it is a single
        value.
        """
        if not isinstance(value, list):
            return value
        group_size = cls._GROUPED_KEYS.get(attr)
        if group_size and value and len(value) % group_size == 0:
            return [
                list(value[i : i + group_size])
                for i in range(0, len(value), group_size)
            ]
        return value[0] if len(value) == 1 else value

    def _shape_properties(self, node, item_name, all_props):
        prefix = f"{node}.{item_name}."
        return {
            p[len(prefix) :]: self._shaped_value(
                p[len(prefix) :], self._property_value(p)
            )
            for p in all_props
            if p.startswith(prefix)
        }

    @staticmethod
    def _frame_order(prop):
        try:
            order = rvc.getStringProperty(prop)
        except Exception:
            return []
        return [order] if isinstance(order, str) else order

    def gather_annotations(self, paint_nodes=None):
        """
        Read annotation shapes from RV paint nodes.

        paint_nodes: list of RVPaint node names to read, upstream first (as
        `paint_nodes_for_source` returns them). If it is None, read every paint
        node in the session.

        Shapes come out per frame in drawing order: the nodes in the given
        order, and each node in its own frame order. The order matters for
        erasers, which cut only what is drawn before them.
        """
        annotations = []

        if paint_nodes is None:
            try:
                paint_nodes = self._pipeline_order(
                    rvc.nodesOfType("RVPaint") or []
                )
            except Exception as exc:
                log(f"Could not query paint nodes: {exc}")
                return annotations

        for node in paint_nodes:
            try:
                all_props = rvc.properties(node)
            except Exception as exc:
                log(f"Skipped paint node {node}: {exc}")
                continue

            for prop in all_props:
                match = _FRAME_ORDER_RE.search(prop)
                if not match:
                    continue
                for item_name in self._frame_order(prop) or []:
                    annotations.append(
                        {
                            "frame": int(match.group(1)),
                            "node": node,
                            "name": item_name,
                            "type": item_name.split(":")[0],
                            "properties": self._shape_properties(
                                node, item_name, all_props
                            ),
                        }
                    )

        annotations.sort(key=lambda a: a["frame"])

        return annotations
