"""The glow score: a local brightness delta, not a detector and not a model.

These tests pin the three choices that decide whether glow is usable at all:
the window is clipped rather than raised at the frame edge, darkening scores
nothing, and the score is a *tail* statistic so a single stray pixel cannot
stand in for an outline.
"""

from __future__ import annotations

import unittest

from clank import glow


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


class Appearance(unittest.TestCase):
    """An object arriving in the hand is a *fraction* of changed pixels.

    This is the hub pickup's confirmation: the floppy has no glow, so the only
    read-back is that the flat rectangle replaces the hand. A maximum would call a
    single specular pixel a pickup; a fraction asks for area.
    """

    def test_an_object_arriving_covers_part_of_the_patch(self) -> None:
        before = flat(40, 40)
        after = [row[:] for row in before]
        for y in range(10, 20):
            for x in range(10, 20):
                after[y][x] = [200, 200, 200]
        self.assertAlmostEqual(
            glow.lit_fraction(before, after, (0, 0, 40, 40)), 100 / 1600, places=3
        )

    def test_an_unchanged_patch_is_a_fraction_of_zero(self) -> None:
        frame = flat(40, 40)
        self.assertEqual(glow.lit_fraction(frame, frame, (0, 0, 40, 40)), 0.0)

    def test_one_stray_pixel_is_a_negligible_fraction(self) -> None:
        before = flat(40, 40)
        after = [row[:] for row in before]
        after[5][5] = [255, 255, 255]
        self.assertLess(glow.lit_fraction(before, after, (0, 0, 40, 40)), 0.01)

    def test_a_faint_change_is_below_the_delta_floor(self) -> None:
        before = flat(40, 40)
        after = flat(40, 40, value=15)
        self.assertEqual(glow.lit_fraction(before, after, (0, 0, 40, 40)), 0.0)


def green_frame(width: int = 40, height: int = 40, box=None) -> list[list[list[int]]]:
    frame = flat(width, height)
    if box is not None:
        x0, y0, x1, y1 = box
        for y in range(y0, y1):
            for x in range(x0, x1):
                frame[y][x] = [20, 200, 40]
    return frame


class GreenSlot(unittest.TestCase):
    """The insertion slot is the one high-contrast signal in the hub."""

    def test_a_green_region_is_found_at_its_centre(self) -> None:
        frame = green_frame(box=(10, 10, 30, 30))
        centre = glow.green_centroid(frame)
        assert centre is not None
        self.assertAlmostEqual(centre[0], 0.5, delta=0.02)
        self.assertAlmostEqual(centre[1], 0.5, delta=0.02)

    def test_an_offcentre_region_reports_its_own_centre(self) -> None:
        frame = green_frame(box=(0, 0, 10, 10))
        centre = glow.green_centroid(frame)
        assert centre is not None
        self.assertLess(centre[0], 0.2)
        self.assertLess(centre[1], 0.2)

    def test_a_frame_without_green_reports_nothing(self) -> None:
        self.assertIsNone(glow.green_centroid(flat(40, 40)))

    def test_a_red_frame_is_not_a_slot(self) -> None:
        frame = [[[200, 20, 20] for _ in range(40)] for _ in range(40)]
        self.assertIsNone(glow.green_centroid(frame))

    def test_a_dark_green_is_below_the_brightness_floor(self) -> None:
        frame = [[[2, 12, 3] for _ in range(40)] for _ in range(40)]
        self.assertIsNone(glow.green_centroid(frame))

    def test_a_barely_green_pixel_is_not_green(self) -> None:
        # Grey noise with a 5-level green lead is not the slot.
        frame = [[[100, 105, 100] for _ in range(40)] for _ in range(40)]
        self.assertIsNone(glow.green_centroid(frame))

    def test_white_is_not_green(self) -> None:
        frame = [[[255, 255, 255] for _ in range(40)] for _ in range(40)]
        self.assertIsNone(glow.green_centroid(frame))

    def test_a_tiny_green_speck_is_not_a_slot(self) -> None:
        frame = green_frame(box=(5, 5, 6, 6))
        self.assertIsNone(glow.green_centroid(frame))

    def test_the_fraction_is_reported_independently(self) -> None:
        frame = green_frame(box=(10, 10, 30, 30))
        self.assertAlmostEqual(glow.green_fraction(frame), 400 / 1600, places=3)


if __name__ == "__main__":
    unittest.main()
