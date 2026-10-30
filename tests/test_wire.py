from __future__ import annotations

import math
import struct
import unittest

from superbot import wire


class LayoutTests(unittest.TestCase):
    def test_layout_matches_the_remote_driver(self) -> None:
        wire.assert_layout()
        self.assertEqual(wire.SIZE, 376)
        self.assertEqual(wire.C.sizeof(wire.HeadData), 128)
        self.assertEqual(wire.C.sizeof(wire.ControllerData), 120)

    def test_header_is_the_remote_driver_magic(self) -> None:
        self.assertEqual(struct.pack("<Q", wire.HEADER), b"mndrmt3\0")

    def test_layout_check_notices_a_shrunken_struct(self) -> None:
        original = wire.EXPECTED["r_remote_data"]
        wire.EXPECTED["r_remote_data"] = (376, 375)
        try:
            with self.assertRaises(ValueError):
                wire.assert_layout()
        finally:
            wire.EXPECTED["r_remote_data"] = original


class FrameMapTests(unittest.TestCase):
    def test_basis_vectors_follow_the_measured_map(self) -> None:
        self.assertEqual(wire.mj_to_xr_vec((1.0, 0.0, 0.0)), (0.0, 0.0, -1.0))
        self.assertEqual(wire.mj_to_xr_vec((0.0, 1.0, 0.0)), (-1.0, 0.0, 0.0))
        self.assertEqual(wire.mj_to_xr_vec((0.0, 0.0, 1.0)), (0.0, 1.0, 0.0))

    def test_wrist_rest_pose_lands_on_the_recorded_xr_point(self) -> None:
        # c0_client: MuJoCo L (-0.017, 0.222, 0.721) -> XR (-0.222, 0.721, 0.017)
        self.assertEqual(wire.mj_to_xr_vec((-0.017, 0.222, 0.721)), (-0.222, 0.721, 0.017))

    def test_quaternion_map_agrees_with_the_vector_map(self) -> None:
        # Rotating in MuJoCo then mapping must equal mapping then rotating with
        # the mapped quaternion; that is what makes the map a frame change and
        # not a lucky set of three vectors.
        half = math.radians(37.0) / 2.0
        q_mj = (math.cos(half), 0.0, 0.0, math.sin(half))  # 37 deg about +Z
        q_xr = wire.mj_to_xr_quat(q_mj)
        for v in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.3, -0.7, 0.2)):
            rotated_then_mapped = wire.mj_to_xr_vec(wire.quat_rotate(q_mj, v))
            mapped_then_rotated = wire.quat_rotate(
                (q_xr[3], q_xr[0], q_xr[1], q_xr[2]), wire.mj_to_xr_vec(v)
            )
            for got, want in zip(mapped_then_rotated, rotated_then_mapped, strict=True):
                self.assertAlmostEqual(got, want, places=9)

    def test_identity_stays_identity(self) -> None:
        q = wire.mj_to_xr_quat((1.0, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(q[0], 0.0)
        self.assertAlmostEqual(q[1], 0.0)
        self.assertAlmostEqual(q[2], 0.0)
        self.assertAlmostEqual(q[3], 1.0)


class BodyStateConversionTests(unittest.TestCase):
    @staticmethod
    def state(root_p=(0.0, 0.0, 0.8), root_q=(1.0, 0.0, 0.0, 0.0),
              head_p=(0.0, 0.0, 0.4), head_q=(0.0, 0.0, 0.0, 1.0)) -> dict:
        return {
            "root_p": list(root_p),
            "root_q": list(root_q),
            "head": {"p": list(head_p), "q_xyzw": list(head_q)},
            "left_hand": {"p": [0.0, 0.2, -0.2], "q_xyzw": [0.0, 0.0, 0.0, 1.0]},
            "right_hand": {"p": [0.0, -0.2, -0.2], "q_xyzw": [0.0, 0.0, 0.0, 1.0]},
        }

    def test_root_translation_is_added(self) -> None:
        poses = wire.body_state_to_xr(self.state(root_p=(1.0, 2.0, 0.8), head_p=(0.0, 0.0, 0.4)))
        self.assertAlmostEqual(poses["head"][0][0], -2.0)  # xr_x = -mj_y
        self.assertAlmostEqual(poses["head"][0][1], 1.2)  # xr_y = mj_z (0.8 + 0.4)
        self.assertAlmostEqual(poses["head"][0][2], -1.0)  # xr_z = -mj_x

    def test_root_rotation_is_composed_before_mapping(self) -> None:
        # Root turned 180 deg about Z: a head 1 m along +X relative to the root is
        # 1 m along -X in the world, which maps to +Z. Skipping the root rotation
        # would leave it at -Z and look plausible while being wrong.
        root_q_180 = (0.0, 0.0, 0.0, 1.0)  # w, x, y, z
        poses = wire.body_state_to_xr(self.state(root_q=root_q_180, head_p=(1.0, 0.0, 0.0)))
        p = poses["head"][0]
        self.assertAlmostEqual(p[0], 0.0, places=6)
        self.assertAlmostEqual(p[1], 0.8, places=6)
        self.assertAlmostEqual(p[2], 1.0, places=6)

    def test_head_orientation_inherits_the_root_rotation(self) -> None:
        half = math.radians(90.0) / 2.0
        root_q_90 = (math.cos(half), 0.0, 0.0, math.sin(half))  # w, x, y, z
        poses = wire.body_state_to_xr(self.state(root_q=root_q_90))
        expected = wire.mj_to_xr_quat(root_q_90)
        for got, want in zip(poses["head"][1], expected, strict=True):
            self.assertAlmostEqual(got, want, places=9)

    def test_rest_poses_describe_a_standing_humanoid(self) -> None:
        head, left, right = wire.REST_POSES["head"], wire.REST_POSES["left"], wire.REST_POSES["right"]
        self.assertAlmostEqual(head[0][1], 1.221)  # E3 HMD height
        self.assertLess(left[0][0], 0.0)
        self.assertGreater(right[0][0], 0.0)
        for pose in (left, right):
            self.assertLess(pose[0][1], head[0][1])


class FillTests(unittest.TestCase):
    def test_fill_writes_poses_and_buttons(self) -> None:
        poses = wire.body_state_to_xr(BodyStateConversionTests.state())
        d = wire.fill(
            wire.blank_packet(), poses, trigger=(1.0, 1.0), trigger_click=(True, True)
        )
        self.assertEqual(d.header, wire.HEADER)
        self.assertAlmostEqual(d.head.center.position.x, poses["head"][0][0], places=5)
        self.assertAlmostEqual(d.left.pose.position.y, poses["left"][0][1], places=5)
        self.assertTrue(d.right.trigger_click)
        self.assertAlmostEqual(d.right.trigger_value.x, 1.0)
        self.assertTrue(d.left.active)
        self.assertFalse(d.head.per_view_data_valid)

    def test_fill_sends_buttons_per_hand(self) -> None:
        packet = wire.fill(
            wire.blank_packet(),
            wire.REST_POSES,
            grip=(0.0, 1.0),
            trigger=(1.0, 0.0),
            trigger_click=(True, False),
        )
        self.assertAlmostEqual(packet.left.squeeze_value.x, 0.0)
        self.assertAlmostEqual(packet.right.squeeze_value.x, 1.0)
        self.assertAlmostEqual(packet.left.trigger_value.x, 1.0)
        self.assertAlmostEqual(packet.right.trigger_value.x, 0.0)
        self.assertTrue(packet.left.trigger_click)
        self.assertFalse(packet.right.trigger_click)

    def test_curl_and_squeeze_are_separately_controllable(self) -> None:
        squeeze_only = wire.fill(
            wire.blank_packet(), wire.REST_POSES, grip=(1.0, 1.0), curl=(0.0, 0.0)
        )
        self.assertAlmostEqual(squeeze_only.left.squeeze_value.x, 1.0)
        self.assertAlmostEqual(squeeze_only.left.squeeze_force.x, 1.0)
        self.assertAlmostEqual(squeeze_only.left.hand_curl[0], 0.0)

    def test_curl_alone_moves_no_button(self) -> None:
        curl_only = wire.fill(wire.blank_packet(), wire.REST_POSES, curl=(1.0, 0.0))
        self.assertAlmostEqual(curl_only.left.hand_curl[4], 1.0)
        self.assertAlmostEqual(curl_only.left.squeeze_value.x, 0.0)

    def test_held_trigger_does_not_repeat_the_click(self) -> None:
        held = wire.fill(wire.blank_packet(), wire.REST_POSES, trigger=(1.0, 1.0))
        self.assertFalse(held.left.trigger_click, "a click is an edge, not a level")
        self.assertAlmostEqual(held.left.trigger_value.x, 1.0)
        self.assertTrue(held.left.trigger_touch)


if __name__ == "__main__":
    unittest.main()


class DeclaredFov(unittest.TestCase):
    """We emit the HMD data, so the field of view can be a decision, not a guess.

    `per_view_data_valid` is the driver's own switch for "use `views[].fov`". Upstream
    `drivers/remote/r_interface.h` documents it as "Is the per view data valid and
    should be used?", and `r_head_data.views[2]` carries `{fov, pose, _pad}`. CITED from
    https://monado.pages.freedesktop.org/monado/r__interface_8h_source.html
    (accessed 2026-10-30).

    What is NOT cited: the code that consumes it. That path could not be read from the
    installed store or the raw source, so honouring it is an ASSUMPTION until the
    two-FOV experiment measures it. These tests pin our side of the contract only.
    """

    def test_the_default_leaves_per_view_data_invalid(self) -> None:
        # Unchanged behaviour: a caller that says nothing about FOV keeps the
        # compositor's own default, exactly as before this field existed.
        d = wire.fill(wire.blank_packet(), wire.REST_POSES)
        self.assertFalse(d.head.per_view_data_valid)

    def test_supplying_a_fov_marks_the_per_view_data_valid(self) -> None:
        d = wire.fill(wire.blank_packet(), wire.REST_POSES, fov=(math.radians(55.0), math.radians(45.0)))
        self.assertTrue(d.head.per_view_data_valid)

    def test_a_symmetric_fov_writes_all_four_half_angles(self) -> None:
        half_h = math.radians(55.0)
        half_v = math.radians(45.0)
        d = wire.fill(wire.blank_packet(), wire.REST_POSES, fov=(half_h, half_v))
        for view in d.head.views:
            self.assertAlmostEqual(view.fov.angle_left, half_h, places=6)
            self.assertAlmostEqual(view.fov.angle_right, half_h, places=6)
            self.assertAlmostEqual(view.fov.angle_up, half_v, places=6)
            self.assertAlmostEqual(view.fov.angle_down, half_v, places=6)

    def test_a_negative_or_zero_fov_is_rejected(self) -> None:
        for bad in ((0.0, 0.5), (-0.5, 0.5), (0.5, 0.0), (float("nan"), 0.5), (0.5, float("inf"))):
            with self.assertRaises(ValueError):
                wire.fill(wire.blank_packet(), wire.REST_POSES, fov=bad)

    def test_declaring_a_fov_does_not_move_anything(self) -> None:
        # The experiment changes one variable. If declaring a FOV also perturbed the
        # head pose, a changed image could not be attributed to the field of view.
        plain = wire.fill(wire.blank_packet(), wire.REST_POSES)
        fovved = wire.fill(wire.blank_packet(), wire.REST_POSES, fov=(1.0, 0.8))
        self.assertEqual(plain.head.center.position.x, fovved.head.center.position.x)
        self.assertEqual(plain.head.center.position.y, fovved.head.center.position.y)
        self.assertEqual(plain.head.center.position.z, fovved.head.center.position.z)
        self.assertEqual(plain.head.center.orientation.w, fovved.head.center.orientation.w)
