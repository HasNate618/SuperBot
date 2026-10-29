"""Camera framing, verified by projection instead of by rendering.

These run with no MuJoCo and no GPU. They answer the questions that matter for a
third-person view: is the subject in frame, how much of the frame does it fill,
does the camera lag or snap, and does a turn stay visible.
"""

import math
import unittest

from superbot.camera import PRESETS, FollowCamera, project, yaw_from_quat

# A standing G1: feet near the root, head about 1.5 m above it.
ROOT = (0.0, 0.0, 0.79)
FEET = (0.0, 0.0, 0.85)
HEAD = (0.0, 0.0, 2.30)
FOVY = 45.0
ASPECT = 4 / 3


class FollowCameraTestCase(unittest.TestCase):
    def test_unknown_preset_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FollowCamera(preset="drone")

    def test_presets_give_different_angles(self) -> None:
        angles = {name: FollowCamera(preset=name).azimuth(0.0) for name in PRESETS}
        self.assertEqual(len(set(angles.values())), len(PRESETS))

    def test_converges_to_the_root_when_still(self) -> None:
        cam = FollowCamera()
        for _ in range(120):
            cam.update(ROOT, 0.0, 0.02)
        for got, want in zip(cam.lookat, (0.0, 0.0, ROOT[2] + cam.target_height), strict=True):
            self.assertAlmostEqual(got, want, places=3)

    def test_lag_is_bounded_while_walking(self) -> None:
        """0.66 m/s with a 0.25 s time constant should trail by ~0.17 m, not run away."""
        cam = FollowCamera()
        speed, dt = 0.663, 0.02
        for i in range(600):
            cam.update((speed * dt * i, 0.0, ROOT[2]), 0.0, dt)
        travelled = speed * dt * 599
        lag = travelled - cam.lookat[0]
        self.assertGreater(lag, 0.0, "camera should trail the body, not lead it")
        self.assertLess(lag, 0.30, f"lag {lag:.3f} m is too large to keep the body framed")

    def test_azimuth_is_world_locked_by_default(self) -> None:
        cam = FollowCamera()
        straight = cam.azimuth(0.0)
        turned = cam.azimuth(math.radians(90.0))
        self.assertEqual(straight, turned, "a turning body must not turn the camera")

    def test_yaw_follow_can_ride_the_body(self) -> None:
        cam = FollowCamera(yaw_follow=1.0)
        straight = cam.azimuth(0.0)
        turned = cam.azimuth(math.radians(90.0))
        self.assertAlmostEqual(turned - straight, 90.0, places=6)

    def test_zero_dt_is_safe(self) -> None:
        cam = FollowCamera()
        before = cam.lookat
        cam.update((5.0, 5.0, 0.79), 0.0, 0.0)
        self.assertEqual(cam.lookat, before)

    def test_huge_dt_cannot_teleport_the_camera(self) -> None:
        cam = FollowCamera()
        cam.update((100.0, 0.0, 0.79), 0.0, 60.0)  # a stall, not a jump
        self.assertLess(abs(cam.lookat[0]), 100.0)


class FramingTestCase(unittest.TestCase):
    """The subject must fill a usable part of the frame and stay inside it."""

    def _frame(self, root=ROOT, preset="three_quarter"):
        cam = FollowCamera(preset=preset)
        for _ in range(200):
            cam.update(root, 0.0, 0.02)
        pos = cam.position(0.0)
        return cam, pos

    def test_body_is_inside_the_frame(self) -> None:
        for preset in PRESETS:
            cam, pos = self._frame(preset=preset)
            for label, point in (("feet", FEET), ("head", HEAD)):
                x, y, depth = project(point, cam.lookat, pos, FOVY, ASPECT)
                self.assertGreater(depth, 0.0)
                self.assertLess(abs(x), 1.0, f"{preset}/{label} x={x:.2f} outside frame")
                self.assertLess(abs(y), 1.0, f"{preset}/{label} y={y:.2f} outside frame")

    def test_body_fills_a_usable_share_of_the_frame(self) -> None:
        cam, pos = self._frame()
        _, y_feet, _ = project(FEET, cam.lookat, pos, FOVY, ASPECT)
        _, y_head, _ = project(HEAD, cam.lookat, pos, FOVY, ASPECT)
        height_fraction = abs(y_head - y_feet) / 2.0
        self.assertGreater(height_fraction, 0.35, "the body is too small to judge motion")
        self.assertLess(height_fraction, 0.95, "the body is cropped at the edges")

    def test_framing_survives_a_walk(self) -> None:
        """Over a 3 m walk the head must stay in frame at every step."""
        cam = FollowCamera()
        for i in range(200):
            root = (0.663 * 0.02 * i, 0.0, ROOT[2])
            cam.update(root, 0.0, 0.02)
            pos = cam.position(0.0)
            head = (root[0], root[1], HEAD[2])
            x, y, depth = project(head, cam.lookat, pos, FOVY, ASPECT)
            self.assertGreater(depth, 0.0)
            self.assertLess(abs(x), 1.0, f"head left the frame at step {i}")
            self.assertLess(abs(y), 1.0, f"head left the frame at step {i}")

    def test_point_behind_the_camera_is_reported(self) -> None:
        cam, pos = self._frame()
        behind = (pos[0] + 10.0 * (pos[0] - cam.lookat[0]),
                  pos[1] + 10.0 * (pos[1] - cam.lookat[1]),
                  pos[2] + 10.0 * (pos[2] - cam.lookat[2]))
        _, _, depth = project(behind, cam.lookat, pos, FOVY, ASPECT)
        self.assertLess(depth, 0.0)


class YawTestCase(unittest.TestCase):
    def test_yaw_of_identity_is_zero(self) -> None:
        self.assertAlmostEqual(yaw_from_quat((1.0, 0.0, 0.0, 0.0)), 0.0, places=9)

    def test_ninety_degrees_about_z(self) -> None:
        half = math.radians(90.0) / 2.0
        self.assertAlmostEqual(
            yaw_from_quat((math.cos(half), 0.0, 0.0, math.sin(half))),
            math.radians(90.0), places=6,
        )


if __name__ == "__main__":
    unittest.main()
