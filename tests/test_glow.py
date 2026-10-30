"""The glow score: a local brightness delta, not a detector and not a model.

These tests pin the three choices that decide whether glow is usable at all:
the window is clipped rather than raised at the frame edge, darkening scores
nothing, and the score is a *tail* statistic so a single stray pixel cannot
stand in for an outline.
"""

from __future__ import annotations

import unittest

from superbot import glow


def flat(width: int, height: int, value: int = 10) -> list[list[list[int]]]:
    return [[[value, value, value] for _ in range(width)] for _ in range(height)]


class PatchBounds(unittest.TestCase):
    def test_patch_bounds_clip_at_the_frame_edge(self) -> None:
        x0, y0, x1, y1 = glow.patch_bounds(0.99, 0.99, 100, 80)
        self.assertLessEqual(x1, 100)
        self.assertLessEqual(y1, 80)
        self.assertLess(x0, x1)
        self.assertLess(y0, y1)

    def test_patch_bounds_clip_at_the_origin(self) -> None:
        x0, y0, _, _ = glow.patch_bounds(0.0, 0.0, 100, 80)
        self.assertEqual((x0, y0), (0, 0))

    def test_an_interior_point_is_centred(self) -> None:
        x0, y0, x1, y1 = glow.patch_bounds(0.5, 0.5, 100, 100)
        self.assertAlmostEqual((x0 + x1) / 2.0, 50, delta=1)
        self.assertAlmostEqual((y0 + y1) / 2.0, 50, delta=1)

    def test_a_tiny_frame_still_gets_the_pixel_floor(self) -> None:
        self.assertEqual(glow.patch_side(10, 10), glow.MIN_PATCH_PX)

    def test_a_degenerate_frame_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            glow.patch_side(0, 10)


class Score(unittest.TestCase):
    def test_a_bright_outline_moves_the_tail(self) -> None:
        # A 3 px border is ~28% of the patch: comfortably past the 95th percentile.
        before = flat(40, 40)
        after = [row[:] for row in before]
        for y in range(40):
            for x in range(40):
                if x < 3 or y < 3 or x >= 37 or y >= 37:
                    after[y][x] = [255, 255, 255]
        self.assertGreater(glow.score_patch(before, after, (0, 0, 40, 40)), 200)

    def test_an_unchanged_frame_scores_zero(self) -> None:
        frame = flat(40, 40)
        self.assertEqual(glow.score_patch(frame, frame, (0, 0, 40, 40)), 0.0)

    def test_darkening_scores_nothing(self) -> None:
        # We want a highlight, not any change: a shadow is not a grab prompt.
        before = flat(40, 40, value=200)
        after = flat(40, 40, value=20)
        self.assertEqual(glow.score_patch(before, after, (0, 0, 40, 40)), 0.0)

    def test_one_stray_pixel_is_not_an_outline(self) -> None:
        # 40x40 = 1600 px; one lit pixel is the 99.94th percentile, not the 95th.
        # A max() would call this glow and grab at nothing.
        before = flat(40, 40)
        after = [row[:] for row in before]
        after[5][5] = [255, 255, 255]
        self.assertEqual(glow.score_patch(before, after, (0, 0, 40, 40)), 0.0)

    def test_the_score_uses_the_brightest_channel(self) -> None:
        # Superhot's highlight is not guaranteed to be neutral in colour.
        before = flat(40, 40)
        after = [row[:] for row in before]
        for i in range(160):
            after[i // 8][i % 8] = [10, 10, 200]
        self.assertGreater(glow.score_patch(before, after, (0, 0, 40, 40)), 100)

    def test_a_highlight_that_covers_too_little_scores_low(self) -> None:
        # The percentile is over every pixel in the window, so a highlight has to
        # cover a real fraction of it. A thin smear is not a grab prompt.
        before = flat(40, 40)
        after = [row[:] for row in before]
        for x in range(10):
            after[0][x] = [255, 255, 255]
        self.assertEqual(glow.score_patch(before, after, (0, 0, 40, 40)), 0.0)

    def test_the_percentile_is_a_calibration_knob(self) -> None:
        # S4 tunes this against a measured on/off pair without editing the module.
        # 32 of 1600 px is 2%: below the 95th percentile, above the 99th.
        before = flat(40, 40)
        after = [row[:] for row in before]
        for i in range(32):
            after[i // 8][i % 8] = [255, 255, 255]
        self.assertEqual(glow.score_patch(before, after, (0, 0, 40, 40)), 0.0)
        self.assertEqual(
            # 245 = 255 - the 10 baseline, not 255: the score is a delta.
            glow.score_patch(before, after, (0, 0, 40, 40), percentile=99), 245.0
        )

    def test_the_window_limits_what_counts(self) -> None:
        # A bright region outside the window is not this target's glow.
        before = flat(40, 40)
        after = [row[:] for row in before]
        for y in range(30, 40):
            for x in range(30, 40):
                after[y][x] = [255, 255, 255]
        self.assertEqual(glow.score_patch(before, after, (0, 0, 10, 10)), 0.0)


if __name__ == "__main__":
    unittest.main()
