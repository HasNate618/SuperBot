"""Where the hand is, found by moving it.

The adapter's only detector that needs no model: capture the same view twice with
one thing changed, and the pixels that differ are the thing. This module is the
arithmetic; `spike/wbc/frames.py` supplies the pixels through Pillow, which
`clank/` may not import.

Why a separate module from `glow`: `glow` answers "is something in range" from
colour. This answers "what moved, and where", from a difference. They share a
threshold idiom and nothing else.
"""

from __future__ import annotations

import unittest

from clank import framediff


def blank(w: int = 8, h: int = 6, value: int = 0):
    return [[(value, value, value) for _ in range(w)] for _ in range(h)]


class Delta(unittest.TestCase):
    def test_identical_frames_have_no_change_and_no_centroid(self) -> None:
        d = framediff.delta(blank(value=90), blank(value=90))
        self.assertEqual(d.mean, 0.0)
        self.assertEqual(d.changed, 0)
        self.assertEqual(d.fraction, 0.0)
        self.assertIsNone(d.centroid)

    def test_one_changed_pixel_is_counted_and_located(self) -> None:
        before = blank()
        after = blank()
        after[1][2] = (255, 255, 255)
        d = framediff.delta(before, after)
        self.assertEqual(d.changed, 1)
        self.assertAlmostEqual(d.fraction, 1.0 / 48.0)
        assert d.centroid is not None
        # Pixel 2 of 8 -> centre 2.5/8; row 1 of 6 -> 1.5/6.
        self.assertAlmostEqual(d.centroid[0], 2.5 / 8.0)
        self.assertAlmostEqual(d.centroid[1], 1.5 / 6.0)

    def test_a_change_below_the_threshold_is_ignored_as_a_pixel_but_counted_in_the_mean(self) -> None:
        before = blank(value=100)
        after = blank(value=100)
        after[0][0] = (120, 100, 100)  # 20 levels: visible to the mean, not "changed"
        d = framediff.delta(before, after, min_delta=40)
        self.assertEqual(d.changed, 0)
        self.assertGreater(d.mean, 0.0)

    def test_the_threshold_is_inclusive_of_the_delta(self) -> None:
        before = blank(value=100)
        after = blank(value=100)
        after[0][0] = (140, 100, 100)  # exactly min_delta
        self.assertEqual(framediff.delta(before, after, min_delta=40).changed, 1)

    def test_the_mean_is_over_every_channel_of_every_pixel(self) -> None:
        before = blank()
        after = blank()
        after[0][0] = (30, 60, 90)
        d = framediff.delta(before, after)
        self.assertAlmostEqual(d.mean, (30 + 60 + 90) / (48 * 3))

    def test_two_separate_blobs_report_the_average_position(self) -> None:
        before = blank()
        after = blank()
        after[0][0] = (255, 255, 255)
        after[0][7] = (255, 255, 255)
        d = framediff.delta(before, after)
        assert d.centroid is not None
        self.assertAlmostEqual(d.centroid[0], 0.5)  # symmetric in x
        self.assertAlmostEqual(d.centroid[1], 0.5 / 6.0)

    def test_mismatched_shapes_are_rejected(self) -> None:
        # A silently resized frame would shift every coordinate and turn a fit into
        # a plausible-looking lie.
        with self.assertRaises(ValueError):
            framediff.delta(blank(8, 6), blank(8, 5))

    def test_bbox_covers_the_changed_pixels(self) -> None:
        before = blank()
        after = blank()
        after[1][1] = (255, 255, 255)
        after[4][5] = (255, 255, 255)
        d = framediff.delta(before, after)
        self.assertEqual(d.bbox, (1, 1, 5, 4))

    def test_a_frame_missing_a_pixel_is_rejected_rather_than_padded(self) -> None:
        before = blank()
        after = blank()
        after[0] = after[0][:-1]
        with self.assertRaises(ValueError):
            framediff.delta(before, after)


if __name__ == "__main__":
    unittest.main()
