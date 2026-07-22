# openrv_paint_gto.py
"""Serialize converted Kitsu annotations into an OpenRV RVPaint GTO fragment."""

def _fnum(n):
    if n == int(n):
        return str(int(n))
    return f"{n:.9f}".rstrip("0").rstrip(".") or "0"

def _flat(arr):
    return "[ " + " ".join(_fnum(n) for n in arr) + " ]"

def _nested(pairs):
    return "[ " + " ".join(f"[ {_fnum(x)} {_fnum(y)} ]" for x, y in pairs) + " ]"

def _pen_block(pen, pen_id, frame):
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

def _text_block(txt, text_id, frame):
    name = f'"text:{text_id}:{frame}:Kitsu"'
    escaped = txt["text"].replace('"', '\\"').replace("\n", "\\n")
    lines = [f"    {name}", "    {"]
    lines.append(f"        float[2] position = {_flat(txt['position'])}")
    lines.append(f"        float[4] color = {_flat(txt.get('color', (1, 1, 1, 1)))}")
    lines.append(f"        float spacing = {_fnum(txt.get('spacing', 0.8))}")
    lines.append(f"        float size = {_fnum(txt.get('size', 0.05))}")
    lines.append(f"        float scale = {_fnum(txt.get('scale', 1))}")
    lines.append(f"        float rotation = {_fnum(txt.get('rotation', 0))}")
    lines.append(f'        string font = ""')
    lines.append(f'        string text = "{escaped}"')
    lines.append(f'        string origin = ""')
    lines.append(f"        int debug = {int(txt.get('debug', 0))}")
    lines.append("    }")
    return "\n".join(lines), name.strip('"')

def build_paint_gto(paint_node_name, openrv_annotations):
    """
    openrv_annotations: list of {
        "frame": int,
        "pens":  [ {color, width, brush, points, join, cap, splat, debug}, ... ],
        "texts": [ {position, color, spacing, size, scale, rotation, text, debug}, ... ],
    }
    """
    blocks = []
    frame_order = {}
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
        return None  # nothing to write

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