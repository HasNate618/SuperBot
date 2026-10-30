# Superhot VR as the body actually has to play it

**Date:** 2026-10-29
**Status:** operator briefing, not a measurement. The join exists; nobody has played.

This is the missing half of `research/wbc-game-proposal.md`. That document measured *level geometry* (The Fall's 0.70 m pistol, 1.41 m enemy, 0.005 clock floor). This one records how the game is *played*, from the operator, because the next body skill is determined by the hub and the teleport, not by Corridor's 12 m factory.

Claim tags: `[OPERATOR]` = told to us by the person who owns the game, unverified here. `[MEASURED]` = produced locally by a named script/run. `[ASSUMPTION]` = inference from those, untested.

## 0. What this changes

The previous "what's next" ranking put **walking under command** first, because The Fall's pistol sits 0.70 m away and the standing reach is ~0.20 m `[MEASURED: research/wbc-game-proposal.md]`. That ranking assumed the body would *go to things*. Superhot is the other way around:

- The hub is a grab-and-place puzzle, not a walk.
- A level is authored around the spawn. Moving more than about a metre makes the next teleport drop you in the wrong place.
- Time itself is a motion channel: sometimes you move only so the world will advance.

Walking 40 m of MuJoCo floor is still a real SONIC result. Walking around a Superhot level is the wrong demo. A half-metre *step toward a nearby pickup*, inside the spawn radius, is a different and smaller problem, and it is not the first one.

## 1. Hub — the first interaction, never documented

`[OPERATOR]` The hub is a room of floppy disks. Level select is:

1. Move a hand near a disk. **Nothing glows.** The hub disk has no pre-grab
   highlight; proximity is judged, then one press.
2. Press once. The hand is replaced by the disk - a thin floating rectangle just
   in front of the controller. That appearance *is* the confirmation.
3. Move the hand to the insertion point, which glows **green**: large and
   saturated, nothing like the subtle in-level highlight.
4. Let go. That activates a headset above the player.
5. Move a hand to the headset and press once, then move it over the head. The
   headset is proximity-and-press like the disk, also with no pre-grab glow.

`[OPERATOR 2026-10-30, correcting the line above]` The subtle glow described in
step 1 was wrong for the hub. It exists, but for *other* grabbables in a level
(§2). The correction matters twice: a hub script that waited for a glow would
wait forever, and the insertion slot is the one high-contrast signal the hub
actually offers.

Until that sequence works, The Fall does not exist. The join currently cannot do it: `pose_relay` calls `wire.fill(packet, poses)` with the defaults `trigger=0, squeeze=0` `[MEASURED: superbot/pose_relay.py]`, and `BodyState` has no closure field to forward even if it wanted to `[MEASURED: superbot/body.py]`. `AUTH_CLOSURE` is listed as supported on the executor and then ignored `[MEASURED: spike/wbc/sonic_executor.py]`.

The G1 model has no fingers. "Close the gripper" cannot mean the game sees a closed hand. It has to become a controller button. The manual client already treats those as two different buttons: LMB/T → `trigger`, RMB/G → `squeeze` / `squeeze_force` / `hand_curl` `[MEASURED: spike/c0/c0_manual.py]`. Which of those Superhot uses for grab is `[ASSUMPTION]` until someone A/B's them against a glowing floppy.

Do not use `c0_client.py`'s `trigger` mode as that A/B. It sets `trigger` and `squeeze` together `[MEASURED: spike/c0/c0_client.py script_trigger]`, so a pickup would not tell you which button did it.

## 2. In-level interaction

`[OPERATOR]`

- Glow-then-grab is the pickup rule for environment objects too: a subtle highlight
  appears when a controller is close enough. The hub disk and the headset are the
  exception - proximity and a press, no highlight (§1). Grabbed objects can be
  thrown, or aimed and shot.
- Two hands at once is allowed, not required.
- Guns have limited ammo. An empty gun still "fires" — it clicks.
- After some enemies die the player is teleported to a new location with new enemies. Infinite-wave mode still teleports; it is not a stay-in-one-room endurance test.
- Strategy is local: grab whatever is closest (throwable or gun), fight the closest threat or the one that can shoot you from here.

`[ASSUMPTION]` Punch is a hand ballistic we already have as a verb (`punch` in `BODY_VERBS`); grab is the missing button, not a new pose.

## 3. Stay in the spawn radius

`[OPERATOR]` The game expects you to stay roughly where it put you. A radius of about **1 m** is probably fine. Walking the level is not: when the game teleports you, it places you relative to the origin it thinks you occupy, so a body that has walked off spawn arrives offset from cover, items, and enemies.

This is compatible with the measured geometry. The Fall's pistol and exit are 0.70 m forward of the XR origin `[MEASURED: research/wbc-game-proposal.md]`. That is inside a 1 m radius and outside the 0.20 m standing reach. So:

| Motion | Role in Superhot | Status |
| --- | --- | --- |
| Look / lean / reach on the spot | the actual play | look is proven in-game; dodge is a 12 cm lean, not a step |
| Half-metre step toward a nearby pickup | only if the gun is not handed over and 0.20 m cannot touch it | `AUTH_ROOT_VEL` not honoured in a hold session |
| Walk the level | **wrong** — breaks the next teleport | do not build |

The 0.70 m pistol is therefore a *small-step* problem if it is a pickup, and a *grab* problem either way. It is not a locomotion problem.

## 4. Time is a motion channel

`[OPERATOR]` Superhot scales time with player motion. Sometimes the correct action is not a grab or a shot but a small movement whose only job is to let a melee enemy walk into punch range.

This is already sitting in the numbers we have:

- Settled hold is RMS 1.3×10⁻⁴ m/s `[MEASURED: research/c3m-results.md]`, which should pin the clock near its 0.005 floor `[MEASURED: research/wbc-game-proposal.md]`.
- Entering hold from a walk costs ~0.05 m/s RMS for the first seconds `[MEASURED: research/c3m-results.md]`.
- Beside the running game the sim advances at 0.89× real time `[MEASURED: research/r6-co-residency.md]`. Superhot's own clock absorbs most of that while the body is still; it matters when the body is moving.

`[ASSUMPTION]` A slow brain is cheap in this game as long as hold is actually still. Micro-motions (head, a 12 cm lean, a wrist twitch) are the time-advance actuator, not `AUTH_ROOT_VEL`.

## 5. What "played it" actually means, in order

Looking is not playing. The line is the hub, then a grab, then a level. Ranked by what is blocked on what:

1. **Squeeze (and separately trigger) through the join.** Without this, a perfect reach at a glowing floppy still does nothing. Keep the two buttons independent; the contract's "adapter maps closure to trigger/grip" is one scalar asked to mean two things.
2. **Grab, measured in the hub.** Reach toward a floppy, send squeeze only, screenshot for glow then pickup. If squeeze does nothing, try trigger only. If both do nothing, try `squeeze_force` and `hand_curl` the way the manual client does. This is C0-3's cousin, and it does not need a pistol.
3. **Place-and-release.** Floppy to insertion point, then `squeeze=0`. Then headset-over-head — a new reach: the G1 has no neck, head pose is derived, and "over the head" may sit outside the 0.20 m envelope. Measure before inventing a stretch.
4. **A bounded step, only if a pickup is out of reach.** Commanded 0.3–0.7 m, stay inside the 1 m spawn radius, no corridor walking.
5. **Trigger as fire**, once something that shoots is in a hand. Empty-click is a useful readout that does not require a hit.
6. **Time-advance micro-motion** and two-hand overlap, later. They are real, they are not the gate.

Until (1) and (2) exist, every other skill is a pose the game cannot use.
