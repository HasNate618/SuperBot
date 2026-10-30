"""`superbot.intent/1`: what a writer names, and nothing a robot cannot obey.

The point of this schema is the boundary. A writer (script, operator click, or a
later model) says *which skill on which side, and optionally which pixel*. It does
not say joint angles, an OpenXR pose, or a world-space point for the hand. The
adapter owns turning that into `BodyCommand`s, and the body never sees a pixel.
"""

from __future__ import annotations

import math
import unittest

from superbot import intent


class IntentRoundTrip(unittest.TestCase):
    def test_intent_round_trips(self) -> None:
        i = intent.Intent(skill="reach", side="right", pixel=(0.4, 0.5))
        self.assertEqual(intent.Intent.from_dict(i.to_dict()), i)

    def test_an_offset_round_trips(self) -> None:
        i = intent.Intent(skill="reach", side="left", offset=(0.18, 0.12, -0.04))
        self.assertEqual(intent.Intent.from_dict(i.to_dict()), i)

    def test_a_pixel_serialises_as_a_list(self) -> None:
        d = intent.Intent(skill="reach", side="right", pixel=(0.4, 0.5)).to_dict()
        self.assertEqual(d["pixel"], [0.4, 0.5])
        self.assertEqual(d["schema"], intent.SCHEMA)


class Validation(unittest.TestCase):
    def test_pixel_outside_the_unit_square_is_rejected(self) -> None:
        self.assertTrue(
            intent.validate_intent(
                intent.Intent(skill="reach", side="right", pixel=(1.4, 0.5))
            )
        )

    def test_a_negative_pixel_is_rejected(self) -> None:
        self.assertTrue(
            intent.validate_intent(
                intent.Intent(skill="reach", side="right", pixel=(-0.1, 0.5))
            )
        )

    def test_a_hand_skill_needs_a_side(self) -> None:
        self.assertTrue(intent.validate_intent(intent.Intent(skill="grab", side="none")))

    def test_hold_needs_no_side(self) -> None:
        self.assertEqual(intent.validate_intent(intent.Intent(skill="hold")), "")

    def test_an_unknown_skill_is_rejected(self) -> None:
        self.assertTrue(intent.validate_intent(intent.Intent(skill="teleport")))

    def test_an_unknown_side_is_rejected(self) -> None:
        self.assertTrue(
            intent.validate_intent(intent.Intent(skill="grab", side="both"))
        )

    def test_a_non_finite_yaw_is_rejected(self) -> None:
        self.assertTrue(
            intent.validate_intent(
                intent.Intent(skill="look", yaw_rad=math.inf)
            )
        )

    def test_an_absurd_offset_is_rejected(self) -> None:
        # A metre-scale offset is outside the arm, not merely ambitious.
        self.assertTrue(
            intent.validate_intent(
                intent.Intent(skill="reach", side="right", offset=(5.0, 0.0, 0.0))
            )
        )

    def test_a_wrong_schema_is_rejected(self) -> None:
        self.assertTrue(
            intent.validate_intent(intent.Intent(skill="hold", schema="superbot.body/1"))
        )


class VerbParsing(unittest.TestCase):
    def test_look_carries_its_yaw(self) -> None:
        i = intent.verb_to_intent("look", {"yaw_deg": 20})
        assert i is not None
        self.assertEqual(i.skill, "look")
        self.assertAlmostEqual(i.yaw_rad or 0.0, math.radians(20))

    def test_reach_carries_its_offset_and_side(self) -> None:
        i = intent.verb_to_intent(
            "reach", {"side": "left", "forward_m": 0.18, "up_m": 0.1, "lateral_m": -0.02}
        )
        assert i is not None
        self.assertEqual((i.skill, i.side), ("reach", "left"))
        self.assertEqual(i.offset, (0.18, 0.1, -0.02))

    def test_reach_defaults_to_the_measured_envelope(self) -> None:
        i = intent.verb_to_intent("reach", {})
        assert i is not None
        offset = i.offset
        assert offset is not None
        self.assertLessEqual(offset[0], 0.22)

    def test_reach_carries_an_optional_pixel(self) -> None:
        i = intent.verb_to_intent("reach", {"side": "right", "pixel": [0.3, 0.7]})
        assert i is not None
        self.assertEqual(i.pixel, (0.3, 0.7))

    def test_the_existing_names_still_map(self) -> None:
        for name in ("hold", "look_left", "look_right", "aim", "punch",
                     "dodge_left", "dodge_right"):
            self.assertIsNotNone(intent.verb_to_intent(name, {}), name)

    def test_unknown_verb_is_none(self) -> None:
        self.assertIsNone(intent.verb_to_intent("teleport", {}))

    def test_a_parsed_verb_validates(self) -> None:
        i = intent.verb_to_intent("aim", {})
        assert i is not None
        self.assertEqual(intent.validate_intent(i), "")


if __name__ == "__main__":
    unittest.main()
