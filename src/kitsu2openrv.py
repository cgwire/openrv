#!/usr/bin/env python3
"""
Load a Kitsu (Fabric.js) annotation JSON file and display the equivalent
OpenRV paint-annotation JSON, using `convert_kitsu_annotations` straight
from the original kitsu_review module.

Usage:
    python src/kitsu2openrv.py kitsu_annotations.json [--width W] [--height H]

The input file must contain a JSON array of Kitsu per-frame annotation
records, e.g.:

    [
      {
        "time": 0,
        "frame": 12,
        "drawing": {
          "objects": [
            {
              "id": "stroke-1",
              "type": "PSStroke",
              "stroke": "#ff0000",
              "strokeWidth": 3.24,
              "canvasWidth": 1920,
              "canvasHeight": 1080,
              "strokePoints": [
                {"x": 960.0, "y": 540.0, "type": "PSPoint", "pressure": 1},
                {"x": 1008.0, "y": 520.8, "type": "PSPoint", "pressure": 1}
              ]
            }
          ]
        }
      },
      ...
    ]
"""

from __future__ import annotations

import argparse
import json

from kitsu import convert_kitsu_annotations


def display_openrv_annotations(
    input_path: str,
    width: int = 1920,
    height: int = 1080,
    canvas_width: float | None = None,
    canvas_height: float | None = None,
) -> None:
    """Read a Kitsu annotation JSON file, convert it to OpenRV's flat
    paint-shape format, and pretty-print the result to stdout."""
    with open(input_path, "r", encoding="utf-8") as f:
        kitsu_records = json.load(f)

    if not isinstance(kitsu_records, list):
        raise ValueError(
            f"expected {input_path} to contain a JSON array of Kitsu annotation "
            f"records, got {type(kitsu_records).__name__}"
        )

    openrv_shapes = convert_kitsu_annotations(
        kitsu_records,
        width=width,
        height=height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )

    print(json.dumps(openrv_shapes, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to a Kitsu annotation JSON file")
    parser.add_argument("--width", type=int, default=1920, help="Source width in pixels")
    parser.add_argument("--height", type=int, default=1080, help="Source height in pixels")
    parser.add_argument("--canvas-width", type=float, default=None, dest="canvas_width",
                         help="Fabric.js canvas width the points are expressed in (defaults to --width)")
    parser.add_argument("--canvas-height", type=float, default=None, dest="canvas_height",
                         help="Fabric.js canvas height the points are expressed in (defaults to --height)")
    args = parser.parse_args()

    display_openrv_annotations(
        args.input,
        width=args.width,
        height=args.height,
        canvas_width=args.canvas_width,
        canvas_height=args.canvas_height,
    )


if __name__ == "__main__":
    main()