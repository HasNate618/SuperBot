# Re-indexed: AUTH_TRIGGER, commanded_buttons and the BodyState button
# fields live in superbot/body.py and are exercised below.
import unittest

from superbot.body import (
    AUTH_CLOSURE,
    AUTH_HEAD,
    AUTH_L_HAND,
    AUTH_ROOT_VEL,
    AUTH_TRIGGER,
    KNOWN_AUTH,
    SCHEMA,
    BodyCommand,
    BodyState,
    Pose,
    commanded_buttons,
    describe_auth,
    hold,
)

POSE = Pose((0.1, 0.2, 1.0), (0.0, 0.0, 0.0, 1.0))


def sample_state() -> BodyState:
    return BodyState(
        seq_ack=7,
        t_mono_ns=123456789,
        root_p=(0.0, 0.0, 0.793),
        root_q=(1.0, 0.0, 0.0, 0.0),
        v_xy=(0.01, -0.02),
        yaw_rate=0.003,
        q=tuple(0.1 * i for i in range(29)),
        qd=tuple(0.01 * i for i in range(29)),
        head=POSE,
        left_hand=POSE,
        right_hand=POSE,
        residual_head_m=0.004,
        residual_left_m=0.031,
        residual_right_m=0.028,
        flags=0,
        supported=AUTH_ROOT_VEL | AUTH_HEAD,
    )


class BodyTestCase(unittest.TestCase):
    def test_schema_name_is_superbot_body_1(self) -> None:
        self.assertEqual(SCHEMA, "superbot.body/1")
        self.assertEqual(BodyCommand().schema, "superbot.body/1")

    def test_command_roundtrip(self) -> None:
        cmd = BodyCommand(
            seq=4,
            t_mono_ns=99,
            authority=AUTH_HEAD | AUTH_L_HAND,
            v_xy=(0.2, -0.1),
            yaw_rate=0.4,
            height=1.221,
            head=POSE,
            left_hand=POSE,
            closure=(0.0, 1.0),
            waist=(0.1, 0.0, -0.1),
            duration_ms=1500,
            preempt=False,
            done_when="target",
        )
        self.assertEqual(BodyCommand.from_dict(cmd.to_dict()), cmd)

    def test_state_roundtrip(self) -> None:
        state = sample_state()
        self.assertEqual(BodyState.from_dict(state.to_dict()), state)

    def test_unset_fields_are_none_not_zero(self) -> None:
        cmd = BodyCommand.from_dict({"seq": 1})
        self.assertIsNone(cmd.height)
        self.assertIsNone(cmd.head)
        self.assertIsNone(cmd.left_hand)
        self.assertIsNone(cmd.right_hand)
        self.assertIsNone(cmd.closure)
        self.assertIsNone(cmd.waist)
        # And they survive the return trip as null, not 0.0.
        self.assertIsNone(cmd.to_dict()["height"])

    def test_hold_requests_no_authority(self) -> None:
        cmd = hold(seq=5, now_ns=10)
        self.assertEqual(cmd.authority, 0)
        self.assertEqual(describe_auth(cmd.authority), [])
        self.assertFalse(cmd.is_ballistic())
        self.assertFalse(cmd.is_blocking())

    def test_is_blocking_only_for_ballistic_without_preempt(self) -> None:
        self.assertFalse(BodyCommand(duration_ms=0).is_blocking())
        self.assertTrue(BodyCommand(duration_ms=800, preempt=False).is_blocking())
        self.assertFalse(BodyCommand(duration_ms=800, preempt=True).is_blocking())

    def test_expires_at_uses_ttl(self) -> None:
        cmd = BodyCommand(t_mono_ns=1_000_000, ttl_ms=200)
        self.assertEqual(cmd.expires_at_ns(), 1_000_000 + 200 * 1_000_000)

    def test_describe_auth_names_bits(self) -> None:
        self.assertEqual(
            describe_auth(AUTH_ROOT_VEL | AUTH_HEAD | AUTH_L_HAND),
            ["root_vel", "head", "left_hand"],
        )

    def test_trigger_is_its_own_authority_bit(self) -> None:
        self.assertEqual(AUTH_TRIGGER, 1 << 9)
        self.assertEqual(AUTH_TRIGGER & AUTH_CLOSURE, 0)
        self.assertEqual(AUTH_TRIGGER & ~KNOWN_AUTH, 0, "trigger must be inside KNOWN_AUTH")
        self.assertEqual(describe_auth(AUTH_TRIGGER), ["trigger"])

    def test_commanded_buttons_are_per_hand_and_authority_gated(self) -> None:
        grip_and_fire = BodyCommand(
            authority=AUTH_CLOSURE | AUTH_TRIGGER,
            closure=(0.0, 1.0),
            trigger=(1.0, 0.0),
        )
        self.assertEqual(commanded_buttons(grip_and_fire), ((0.0, 1.0), (1.0, 0.0)))

    def test_commanded_buttons_are_zero_without_their_bit(self) -> None:
        no_authority = BodyCommand(closure=(1.0, 1.0), trigger=(1.0, 1.0))
        self.assertEqual(commanded_buttons(no_authority), ((0.0, 0.0), (0.0, 0.0)))

        absent = BodyCommand(authority=AUTH_CLOSURE | AUTH_TRIGGER)
        self.assertEqual(commanded_buttons(absent), ((0.0, 0.0), (0.0, 0.0)))

    def test_command_round_trips_the_trigger(self) -> None:
        # to_dict wrote `trigger` and from_dict never read it, so any JSON writer
        # (the bus, a future brain) had its trigger silently discarded.
        cmd = BodyCommand(authority=AUTH_CLOSURE | AUTH_TRIGGER,
                          closure=(0.0, 1.0), trigger=(1.0, 0.0))
        back = BodyCommand.from_dict(cmd.to_dict())
        self.assertEqual(back.closure, (0.0, 1.0))
        self.assertEqual(back.trigger, (1.0, 0.0))
        self.assertEqual(commanded_buttons(back), ((0.0, 1.0), (1.0, 0.0)))

    def test_state_round_trips_buttons(self) -> None:
        state = sample_state()
        state = BodyState(**{**state.__dict__, "closure": (0.0, 1.0), "trigger": (1.0, 0.0)})
        back = BodyState.from_dict(state.to_dict())
        self.assertEqual(back.closure, (0.0, 1.0))
        self.assertEqual(back.trigger, (1.0, 0.0))


if __name__ == "__main__":
    unittest.main()
