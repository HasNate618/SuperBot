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
        d = wire.fill(wire.blank_packet(), poses, trigger=1.0)
        self.assertEqual(d.header, wire.HEADER)
        self.assertAlmostEqual(d.head.center.position.x, poses["head"][0][0], places=5)
        self.assertAlmostEqual(d.left.pose.position.y, poses["left"][0][1], places=5)
        self.assertTrue(d.right.trigger_click)
        self.assertAlmostEqual(d.right.trigger_value.x, 1.0)
        self.assertTrue(d.left.active)
        self.assertFalse(d.head.per_view_data_valid)


if __name__ == "__main__":
    unittest.main()
