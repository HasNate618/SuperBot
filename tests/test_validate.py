# Re-indexed: AUTH_TRIGGER and the trigger range check live in
# superbot/validate.py and superbot/body.py.
import math
import unittest

from superbot.body import (
    AUTH_CLOSURE,
    AUTH_GAIT,
    AUTH_L_HAND,
    AUTH_POSTURE,
    AUTH_R_HAND,
    AUTH_TRIGGER,
    AUTH_WAIST,
    BodyCommand,
    Pose,
)
from superbot.validate import WAIST_LIMIT_RAD, validate_command

NOW = 1_000_000_000


def cmd(**kwargs) -> BodyCommand:
    """A command that arrives now, so only the property under test can reject it."""
    kwargs.setdefault("t_mono_ns", NOW)
    return BodyCommand(**kwargs)


def accepted(result) -> BodyCommand:
    """Assert acceptance and hand back a non-optional command."""
    if result.command is None:
        raise AssertionError(f"expected acceptance, got: {result.reason}")
    return result.command


def unit_pose(p=(0.2, 0.1, 0.9)) -> Pose:
    return Pose(p, (0.0, 0.0, 0.0, 1.0))


class ValidateTestCase(unittest.TestCase):
    def test_accepts_minimal_hold(self) -> None:
        result = validate_command(cmd(), NOW)
        self.assertEqual(accepted(result).authority, 0)

    def test_reject_bad_schema(self) -> None:
        result = validate_command(cmd(schema="superbot.body/0"), NOW)
        self.assertFalse(result.ok)
        self.assertIn("schema", result.reason)

    def test_reject_nonfinite(self) -> None:
        result = validate_command(cmd(yaw_rate=math.inf), NOW)
        self.assertFalse(result.ok)
        self.assertIn("non-finite", result.reason)

    def test_reject_nonfinite_hand_target(self) -> None:
        bad = Pose((0.2, float("nan"), 0.9), (0.0, 0.0, 0.0, 1.0))
        result = validate_command(
            cmd(authority=AUTH_R_HAND, right_hand=bad), NOW
        )
        self.assertFalse(result.ok)
        self.assertIn("non-finite", result.reason)

    def test_reject_expired_ttl(self) -> None:
        stale = cmd(t_mono_ns=0, ttl_ms=200)
        result = validate_command(stale, now_ns=10 * 1_000_000_000)
        self.assertFalse(result.ok)
        self.assertIn("ttl expired", result.reason)

    def test_accepts_command_arriving_inside_ttl(self) -> None:
        fresh = cmd(t_mono_ns=NOW - 100_000_000, ttl_ms=200)
        self.assertTrue(validate_command(fresh, NOW).ok)

    def test_reject_workspace_box(self) -> None:
        far = unit_pose((1.5, 0.0, 0.9))
        result = validate_command(
            cmd(authority=AUTH_L_HAND, left_hand=far), NOW
        )
        self.assertFalse(result.ok)
        self.assertIn("outside", result.reason)

    def test_reject_unknown_auth_bits(self) -> None:
        result = validate_command(cmd(authority=1 << 20), NOW)
        self.assertFalse(result.ok)
        self.assertIn("unknown authority bits", result.reason)

    def test_known_but_unsupported_bits_are_accepted(self) -> None:
        """GAIT and POSTURE are legal requests that v1 flags UNSUPPORTED."""
        result = validate_command(cmd(authority=AUTH_GAIT | AUTH_POSTURE), NOW)
        self.assertTrue(result.ok, result.reason)

    def test_reject_waist_outside_limit(self) -> None:
        result = validate_command(
            cmd(authority=AUTH_WAIST, waist=(0.0, 0.0, WAIST_LIMIT_RAD + 0.1)),
            NOW,
        )
        self.assertFalse(result.ok)
        self.assertIn("waist", result.reason)

    def test_reject_closure_outside_unit_range(self) -> None:
        result = validate_command(
            cmd(authority=AUTH_CLOSURE, closure=(0.0, 1.5)), NOW
        )
        self.assertFalse(result.ok)
        self.assertIn("closure", result.reason)

    def test_reject_non_unit_quaternion(self) -> None:
        tilted = Pose((0.2, 0.0, 0.9), (0.0, 0.0, 0.0, 2.0))
        result = validate_command(
            cmd(authority=AUTH_R_HAND, right_hand=tilted), NOW
        )
        self.assertFalse(result.ok)
        self.assertIn("quaternion", result.reason)

    def test_reject_height_outside_range(self) -> None:
        result = validate_command(cmd(height=2.5), NOW)
        self.assertFalse(result.ok)
        self.assertIn("height", result.reason)

    def test_accepts_measured_hmd_height(self) -> None:
        """1.221 m is the E3 achieved head height; it must validate."""
        self.assertTrue(validate_command(cmd(height=1.221), NOW).ok)

    def test_reject_too_fast_joint_speed(self) -> None:
        result = validate_command(cmd(max_joint_speed=9.0), NOW)
        self.assertFalse(result.ok)
        self.assertIn("max_joint_speed", result.reason)

    def test_rejection_never_raises(self) -> None:
        for bad in (
            cmd(schema="x"),
            cmd(yaw_rate=math.nan),
            cmd(authority=1 << 30),
            cmd(waist=(9.0, 0.0, 0.0), authority=AUTH_WAIST),
        ):
            result = validate_command(bad, NOW)
            self.assertFalse(result.ok)
            self.assertIsInstance(result.reason, str)

    def test_trigger_outside_unit_range_is_rejected(self) -> None:
        result = validate_command(cmd(authority=AUTH_TRIGGER, trigger=(1.5, 0.0)), NOW)
        self.assertFalse(result.ok)
        self.assertIn("trigger", result.reason)

    def test_closure_and_trigger_validate_only_under_their_own_bit(self) -> None:
        grip_only = cmd(authority=AUTH_CLOSURE, closure=(1.0, 0.0), trigger=(9.0, 9.0))
        self.assertTrue(validate_command(grip_only, NOW).ok, "an unset bit must not be validated")


if __name__ == "__main__":
    unittest.main()


class FrameTests(unittest.TestCase):
    """The frame a command's poses are expressed in.

    `frame="world"` used to be accepted and then silently ignored by the executor,
    which is worse than rejecting it: a writer could believe it had asked for world
    coordinates and get body coordinates. `frame="head"` replaces it, and means the
    hand offsets are carried by however far the torso has twisted since the clip - so
    the hand stays where the camera is looking.
    """

    def test_head_frame_is_accepted_for_a_hand(self) -> None:
        result = validate_command(
            cmd(authority=AUTH_R_HAND, frame="head", right_hand=unit_pose()), NOW
        )
        accepted(result)

    def test_world_frame_is_rejected_rather_than_ignored(self) -> None:
        result = validate_command(
            cmd(authority=AUTH_R_HAND, frame="world", right_hand=unit_pose()), NOW
        )
        self.assertIsNone(result.command)
        self.assertIn("unknown frame", result.reason or "")

    def test_a_head_pose_cannot_be_in_the_head_frame(self) -> None:
        # The head frame is defined by the head's own yaw, so asking for the head in
        # it is circular. Rejecting keeps one meaning per frame.
        result = validate_command(
            cmd(authority=1 << 2, frame="head", head=unit_pose()), NOW
        )
        self.assertIsNone(result.command)
        self.assertIn("head", (result.reason or "").lower())
