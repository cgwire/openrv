"""Pure-python checks of the OpenRV <-> Kitsu annotation conversion.

Runs outside OpenRV (the module tolerates the missing rv/gazu imports):

    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import kitsu  # noqa: E402


def _pen(frame, points=((-0.5, 0.25), (0.5, -0.25)), color=(1.0, 0.0, 0.0, 1.0)):
    return {
        "frame": frame,
        "node": "sourceGroup000000_paint",
        "name": f"pen:0:{frame}:rv",
        "type": "pen",
        "properties": {
            "points": [list(p) for p in points],
            "color": [list(color)],
            "width": [0.01, 0.01],
            "brush": "circle",
        },
    }


class TimeUnits(unittest.TestCase):
    def test_time_is_seconds_and_frame_is_zero_based(self):
        # RV source starts at frame 1001 (frame_base); a stroke on 1025 is
        # video frame 24 -> exactly 1.0 s at 24 fps.
        records = kitsu.convert_openrv_annotations(
            [_pen(1025)], 1920, 1080, fps=24.0, frame_base=1001
        )
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["time"], 1.0)
        self.assertEqual(records[0]["frame"], 24)

    def test_first_frame_is_time_zero(self):
        records = kitsu.convert_openrv_annotations(
            [_pen(1)], 1920, 1080, fps=25.0, frame_base=1
        )
        self.assertEqual(records[0]["time"], 0)
        self.assertEqual(records[0]["frame"], 0)

    def test_matches_kitsu_within_half_a_frame(self):
        fps = 23.976
        records = kitsu.convert_openrv_annotations(
            [_pen(1 + 100)], 1920, 1080, fps=fps, frame_base=1
        )
        player_time = 100 / fps  # what the Kitsu player reports on that frame
        self.assertLess(abs(records[0]["time"] - player_time), 0.5 / fps)


class Colours(unittest.TestCase):
    def test_rv_float_row_to_hex(self):
        self.assertEqual(kitsu.rv_color_to_hex([[1.0, 0.0, 0.5, 1.0]]), "#ff0080")

    def test_kitsu_hex_round_trips_through_live_apply_scale(self):
        row = kitsu.color_to_rv_color("#ff3860", 1.0)
        # apply_annotations_live divides by 255 before setFloatProperty
        self.assertEqual([round(c / 255.0, 3) for c in row][:3], [1.0, 0.22, 0.376])


class RoundTrip(unittest.TestCase):
    def test_export_then_import_lands_on_the_same_rv_frame(self):
        base = 1001
        records = kitsu.convert_openrv_annotations(
            [_pen(base + 10)], 1920, 1080, fps=24.0, frame_base=base
        )
        # mirrors _load_revision: RV source frames start at base
        shapes = kitsu.convert_kitsu_annotations(
            records, 1920, 1080, frame_offset=-base, default_frame=0
        )
        self.assertEqual([s["frame"] for s in shapes], [base + 10])


if __name__ == "__main__":
    unittest.main()
