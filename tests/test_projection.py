"""Where the wrist lands in the view we ourselves emit.

This is the whole reason a slow model can still be useful: we own the camera. The
HMD pose is a value we send, so a wrist from `mjData` and a pixel named by a writer
are comparable through arithmetic - no detector, no depth, no 3D reconstruction.

The root cancels. `BodyState.head` and `BodyState.right_hand` are both
root-relative, so the projection needs no root composition at all; a test below
pins that, because recomposing the root would silently reintroduce the
`(w,x,y,z)`-vs-`(x,y,z,w)` trap that already cost one debugging session.
"""

from __future__ import annotations

import math
import unittest

from superbot import projection
from superbot.body import Pose

IDENTITY = Pose((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
HALF_45 = projection.Fov.from_degrees(45.0, 45.0, 45.0, 45.0)


def yawed(degrees: float) -> Pose:
    """A head pose turned about +Z, the frame's up axis."""
    half = math.radians(degrees) / 2.0
    return Pose((0.0, 0.0, 0.0), (0.0, 0.0, math.sin(half), math.cos(half)))


class ProjectPoint(unittest.TestCase):
    def test_a_point_on_the_gaze_axis_is_the_centre(self) -> None:
        u, v = projection.project_point(IDENTITY, (1.0, 0.0, 0.0), HALF_45)  # type: ignore[misc]
        self.assertAlmostEqual(u, 0.5)
        self.assertAlmostEqual(v, 0.5)

    def test_a_point_at_the_left_edge_maps_to_u_zero(self) -> None:
        u, _ = projection.project_point(IDENTITY, (1.0, 1.0, 0.0), HALF_45)  # type: ignore[misc]
        self.assertAlmostEqual(u, 0.0)

    def test_a_point_at_the_top_edge_maps_to_v_zero(self) -> None:
        _, v = projection.project_point(IDENTITY, (1.0, 0.0, 1.0), HALF_45)  # type: ignore[misc]
        self.assertAlmostEqual(v, 0.0)

    def test_a_point_behind_the_camera_is_not_projected(self) -> None:
        # Returning a mirrored pixel here would drive the servo away from the target.
        self.assertIsNone(projection.project_point(IDENTITY, (-1.0, 0.0, 0.0), HALF_45))

    def test_head_yaw_moves_a_fixed_point_to_the_right(self) -> None:
        # Turning the head left (+Z) moves a fixed world point right in the image.
        u, _ = projection.project_point(yawed(20.0), (1.0, 0.0, 0.0), HALF_45)  # type: ignore[misc]
        self.assertGreater(u, 0.5)

    def test_the_root_cancels(self) -> None:
        # Head and wrist offset together: same answer as at the origin.
        head = Pose((0.0, 0.0, 1.5), (0.0, 0.0, 0.0, 1.0))
        u, v = projection.project_point(head, (1.0, 0.0, 1.5), HALF_45)  # type: ignore[misc]
        self.assertAlmostEqual(u, 0.5)
        self.assertAlmostEqual(v, 0.5)

    def test_an_asymmetric_fov_puts_the_centre_off_the_middle(self) -> None:
        wide_left = projection.Fov.from_degrees(60.0, 30.0, 40.0, 40.0)
        u, _ = projection.project_point(IDENTITY, (1.0, 0.0, 0.0), wide_left)  # type: ignore[misc]
        self.assertNotAlmostEqual(u, 0.5)
        self.assertGreater(u, 0.0)
        self.assertLess(u, 1.0)


class Fov(unittest.TestCase):
    def test_degrees_are_converted(self) -> None:
        self.assertAlmostEqual(HALF_45.angle_left, math.radians(45.0))

    def test_a_degenerate_fov_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            projection.Fov.from_degrees(0.0, 0.0, 30.0, 30.0)

    def test_a_non_finite_angle_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            projection.Fov.from_degrees(math.inf, 30.0, 30.0, 30.0)

    def test_negative_angles_are_treated_as_magnitudes(self) -> None:
        # OpenXR implementations disagree about the sign of angle_right; taking
        # magnitudes means a symmetric frustum projects the same either way.
        signed = projection.Fov.from_degrees(45.0, -45.0, 45.0, -45.0)
        u, _ = projection.project_point(IDENTITY, (1.0, 1.0, 0.0), signed)  # type: ignore[misc]
        self.assertAlmostEqual(u, 0.0)


class PixelError(unittest.TestCase):
    def test_error_is_euclidean_in_normalised_pixels(self) -> None:
        self.assertAlmostEqual(
            projection.pixel_error((0.4, 0.5), (0.7, 0.9)), 0.5
        )

    def test_a_perfect_match_is_zero(self) -> None:
        self.assertEqual(projection.pixel_error((0.4, 0.5), (0.4, 0.5)), 0.0)


if __name__ == "__main__":
    unittest.main()
