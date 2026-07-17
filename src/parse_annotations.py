#!/usr/bin/env python3
"""
Extract frame annotations from an OpenRV (.rv) session file.

.rv files are text GTO files. Annotation data (pen strokes, text, etc.) lives
inside RVPaint objects, in components named like "frameNNNN" (an "order" list
of stroke/text names for that frame) plus one component per named stroke
(e.g. "pen:1001:1", "text:1050:1") holding the actual point/color/text data.

Usage:
    python3 src/parse_annotations.py openrv.rv > openrv_annotations.json
"""

import sys
import json
import re


TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'      # quoted string
    r'|[{}\[\]=(),]'          # punctuation
    r'|[^\s{}\[\]=(),"]+'     # bareword / number / identifier
)


def tokenize(text):
    return TOKEN_RE.findall(text)


class Parser:
    """Minimal recursive-descent parser for text GTO files."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def parse_file(self):
        # first token is the GTO header, e.g. GTOa (3)
        objects = {}
        self.next()  # header identifier
        if self.peek() == "(":
            self._skip_paren()
        while self.peek() is not None:
            name, obj = self.parse_object()
            objects[name] = obj
        return objects

    def _skip_paren(self):
        self.next()  # (
        while self.peek() != ")":
            self.next()
        self.next()  # )

    def parse_object(self):
        name = self._unquote(self.next())
        protocol = None
        if self.peek() == ":":
            self.next()
            protocol = self.next()
            if self.peek() == "(":
                self._skip_paren()
        components = {}
        self.next()  # {
        while self.peek() != "}":
            cname, comp = self.parse_component()
            components[cname] = comp
        self.next()  # }
        return name, {"protocol": protocol, "components": components}

    def parse_component(self):
        name = self._unquote(self.next())
        properties = {}
        self.next()  # {
        while self.peek() != "}":
            pname, value = self.parse_property()
            properties[pname] = value
        self.next()  # }
        return name, properties

    def parse_property(self):
        # forms seen: TYPE NAME = VALUE
        #             TYPE[DIMS] NAME = VALUE
        #             TYPE[DIMS][SIZE] NAME = VALUE
        #             TYPE NAME as INTERP = VALUE
        self.next()  # type token, e.g. "float" or "float[2]" or "string"
        # dims/size may be separate bracket tokens if tokenizer split them
        while self.peek() == "[":
            self.next()
            while self.peek() != "]":
                self.next()
            self.next()
        name = self._unquote(self.next())
        if self.peek() == "as":
            self.next()
            self.next()  # interpretation name
        self.next()  # =
        value = self.parse_value()
        return name, value

    def parse_value(self):
        if self.peek() == "[":
            self.next()
            values = []
            while self.peek() != "]":
                values.append(self.parse_value())
            self.next()
            return values
        tok = self.next()
        return self._unquote(tok)

    @staticmethod
    def _unquote(tok):
        if tok is None:
            return tok
        if tok.startswith('"') and tok.endswith('"'):
            return tok[1:-1].encode().decode("unicode_escape")
        for cast in (int, float):
            try:
                return cast(tok)
            except (ValueError, TypeError):
                continue
        return tok


def parse_gto(text):
    return Parser(tokenize(text)).parse_file()


FRAME_RE = re.compile(r"^frame:(\d+)$")


def extract_annotations(objects):
    """Pull per-frame annotation entries out of every RVPaint object."""
    annotations = []
    for obj_name, obj in objects.items():
        if obj.get("protocol") != "RVPaint":
            continue
        components = obj["components"]
        for comp_name, comp in components.items():
            m = FRAME_RE.match(comp_name)
            if not m:
                continue
            frame = int(m.group(1))
            order = comp.get("order", [])
            if isinstance(order, str):
                order = [order]
            for item_name in order:
                item = components.get(item_name, {})
                kind = item_name.split(":")[0] if ":" in item_name else item_name
                annotations.append({
                    "frame": frame,
                    "node": obj_name,
                    "name": item_name,
                    "type": kind,
                    "properties": item,
                })
    annotations.sort(key=lambda a: a["frame"])
    return annotations


def main():
    if len(sys.argv) != 2:
        print("Usage: python rv_annotations.py <path/to/session.rv>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    objects = parse_gto(text)
    annotations = extract_annotations(objects)
    print(json.dumps(annotations, indent=2))


if __name__ == "__main__":
    main()