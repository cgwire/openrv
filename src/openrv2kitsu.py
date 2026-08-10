#!/usr/bin/env python3
"""
Load an OpenRV paint-annotation JSON file and display the equivalent Kitsu
(Fabric.js) annotation JSON, using `convert_openrv_annotations` straight
from the original kitsu_review module.

Usage:
    python src/openrv2kitsu.py openrv_annotations.json [--width W] [--height H] [--fps FPS]

The input file must contain a JSON array of OpenRV shape dicts, e.g.:

    [
      {
        "type": "pen",
        "frame": 12,
        "properties": {
          "points": [[0.0, 0.0], [0.05, 0.02]],
          "width": 0.003,
          "color": [[1, 0, 0, 1]]
        }
      },
      ...
    ]
"""

from __future__ import annotations

import argparse
import json

from kitsu import convert_openrv_annotations


def display_kitsu_annotations(
    input_path: str,
    width: int = 1920,
    height: int = 1080,
    fps: float = 24.0,
    author: str | None = None,
    canvas_width: float | None = None,
    canvas_height: float | None = None,
) -> None:
    """Read an OpenRV annotation JSON file, convert it to Kitsu's per-frame
    Fabric.js annotation format, and pretty-print the result to stdout."""
    with open(input_path, "r", encoding="utf-8") as f:
        openrv_shapes = json.load(f)

    if not isinstance(openrv_shapes, list):
        raise ValueError(
            f"expected {input_path} to contain a JSON array of OpenRV shapes, "
            f"got {type(openrv_shapes).__name__}"
        )

    kitsu_records = convert_openrv_annotations(
        openrv_shapes,
        width=width,
        height=height,
        fps=fps,
        author=author,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )

    print(json.dumps(kitsu_records, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to an OpenRV annotation JSON file")
    parser.add_argument("--width", type=int, default=1920, help="Source width in pixels")
    parser.add_argument("--height", type=int, default=1080, help="Source height in pixels")
    parser.add_argument("--fps", type=float, default=24.0, help="Playback fps")
    parser.add_argument("--author", default=None, help="Kitsu person id to record as createdBy")
    parser.add_argument("--canvas-width", type=float, default=None, dest="canvas_width")
    parser.add_argument("--canvas-height", type=float, default=None, dest="canvas_height")
    args = parser.parse_args()

    display_kitsu_annotations(
        args.input,
        width=args.width,
        height=args.height,
        fps=args.fps,
        author=args.author,
        canvas_width=args.canvas_width,
        canvas_height=args.canvas_height,
    )


if __name__ == "__main__":
    main()