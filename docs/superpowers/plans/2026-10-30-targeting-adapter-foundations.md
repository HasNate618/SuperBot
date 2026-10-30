# Targeting Adapter Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the three stdlib-computable pieces of the targeting adapter — the intent schema, the wrist→camera projection, and the glow patch score — plus parameterised verbs, so the only thing left before a hub pickup is the live-session capture and the script.

**Architecture:** A writer names `skill + side + optional pixel` as `superbot.intent/1`. Three new stdlib modules do the arithmetic that does not need the game: `intent.py` (schema, validation, verb→intent), `projection.py` (`BodyState` wrist → normalised `(u, v)` through a given FOV), `glow.py` (95th-percentile local brightness delta). `spike/wbc/body_view.py` gains parameterised verbs by calling the stdlib parser, so the mapping is testable without MuJoCo.

**Tech Stack:** Python 3.12 standard library only for `superbot/`. The adapter's capture loop (VNC/RFB) and any image decoding stay in `spike/wbc/` in a later plan.

**Spec:** `docs/superpowers/specs/2026-10-30-targeting-adapter.md`. Parent design: `docs/2026-10-23-vr-bot-design.md` §§6, 8, 9. Play rules: `research/superhot-play.md`.

## Global Constraints

- `superbot/` stays **standard-library only**. No NumPy, no PIL.
- `BodyCommand` / `BodyState` (`superbot.body/1`) **must not** gain a pixel field. Pixels are the intent schema.
- The adapter **never** closes a hand. Glow true means "in range"; `grab` is its own intent.
- Projection is tested with a **fixture FOV**. The remote driver's default FOV is not written into the packet today (`wire.fill` sets `per_view_data_valid = False`), so live FOV is measured later and is never guessed in a test's favour.
- Glow is a **baseline** delta, never `frame[t] − frame[t−1]`.
- Existing behaviour that must not regress: grip latch, 3-tick trigger, `SATURATED` at 0.22 m, and every existing verb name.

## Review Focus

1. **Pixel normalisation.** A pixel is `[u, v]` in `[0, 1]` of the **HMD view**, origin top-left. A caller passing VNC pixels must be rejected, not silently clamped to an edge.
2. **Behind-camera wrist.** `projection.project_point` must return `None`, not a mirrored pixel, or the servo drives away from the target.
3. **`side` on a skill that has no side.** `grab`/`reach`/`fire` without a side must be rejected; `hold`/`look`/`twitch` with a side is harmless but must not be read as a hand.
4. **Glow on a stale frame** is not glow. The score function must not care, but the adapter contract says stale means `unknown`.
5. **Patch clipping.** A pixel at `u = 0.99` must not raise `IndexError` when the patch window runs off the frame.

---

### Task 1: Intent schema

**Files:**
- Create: `superbot/intent.py`
- Test: `tests/test_intent.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `SCHEMA = "superbot.intent/1"`; `SKILLS`, `SIDES`, `SIDE_SKILLS`; `Intent` (frozen dataclass with `schema, seq, t_mono_ns, ttl_ms, source, skill, side, pixel, yaw_rad`); `Intent.from_dict(raw) -> Intent`; `.to_dict() -> dict`; `validate_intent(intent) -> str` (empty string means valid); `verb_to_intent(name: str, raw: Mapping[str, Any]) -> Intent | None`.

- [x] **Step 1: Write the failing tests**

```python
def test_intent_round_trips():
    i = intent.Intent(skill="reach", side="right", pixel=(0.4, 0.5))
    assert intent.Intent.from_dict(i.to_dict()) == i

def test_pixel_outside_the_unit_square_is_rejected():
    assert intent.validate_intent(intent.Intent(skill="reach", side="right", pixel=(1.4, 0.5)))

def test_a_hand_skill_needs_a_side():
    assert intent.validate_intent(intent.Intent(skill="grab", side="none"))

def test_hold_needs_no_side():
    assert intent.validate_intent(intent.Intent(skill="hold")) == ""

def test_verb_carries_its_parameters():
    i = intent.verb_to_intent("look", {"yaw_deg": 20})
    assert i.skill == "look" and abs(i.yaw_rad - math.radians(20)) < 1e-9

def test_unknown_verb_is_none():
    assert intent.verb_to_intent("teleport", {}) is None
```

- [x] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_intent -v` — expects `ModuleNotFoundError`.

- [x] **Step 3: Implement `superbot/intent.py`**

`SKILLS = ("hold", "look", "reach", "grab", "release", "fire", "strike", "dodge", "twitch")`; `SIDE_SKILLS = ("reach", "grab", "release", "fire", "strike", "dodge")`. `verb_to_intent` maps the existing verb names plus parameterised forms: `look` reads `yaw_deg`; `reach`/`aim` read `side`, `forward_m`, `up_m`, `lateral_m`. Unknown name → `None`.

- [x] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_intent -v` — all pass.

- [x] **Step 5: Commit**

```bash
git add superbot/intent.py tests/test_intent.py
git commit -m "feat(intent): skill + side + pixel on a socket the brain can share"
```

---

### Task 2: Wrist projection

**Files:**
- Create: `superbot/projection.py`
- Test: `tests/test_projection.py`

**Interfaces:**
- Consumes: `superbot.body.Pose`.
- Produces: `Fov(angle_left, angle_right, angle_up, angle_down)` with `.from_degrees(left, right, up, down)`; `project_point(head: Pose, point_p, fov: Fov) -> tuple[float, float] | None`; `pixel_error(target: tuple[float, float], actual: tuple[float, float]) -> float`.

- [x] **Step 1: Write the failing tests**

```python
IDENTITY = Pose((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
HALF_45 = projection.Fov.from_degrees(45, 45, 45, 45)

def test_a_point_on_the_gaze_axis_is_the_centre():
    u, v = projection.project_point(IDENTITY, (1.0, 0.0, 0.0), HALF_45)
    assert abs(u - 0.5) < 1e-9 and abs(v - 0.5) < 1e-9

def test_a_point_at_the_left_edge_maps_to_u_zero():
    u, _ = projection.project_point(IDENTITY, (1.0, 1.0, 0.0), HALF_45)
    assert abs(u) < 1e-9

def test_a_point_behind_the_camera_is_not_projected():
    assert projection.project_point(IDENTITY, (-1.0, 0.0, 0.0), HALF_45) is None

def test_head_yaw_moves_the_projection_sideways():
    left = Pose((0.0, 0.0, 0.0), (0.0, 0.0, math.sin(math.radians(20)/2), math.cos(math.radians(20)/2)))
    u, _ = projection.project_point(left, (1.0, 0.0, 0.0), HALF_45)
    assert u > 0.5
```

- [x] **Step 2: Run to verify it fails.** `python3 -m unittest tests.test_projection -v`

- [x] **Step 3: Implement**

Head-frame axes: forward `+X`, up `+Z`, left `+Y` (MuJoCo Z-up with +X forward). Rotate `(point − head.p)` by the conjugate of `head.q_xyzw`, then

```text
u = (|angle_left| − atan2(y, x)) / (|angle_left| + |angle_right|)
v = (|angle_up|   − atan2(z, x)) / (|angle_up|   + |angle_down|)
```

`None` when the rotated `x <= 1e-6`. Document the axis choice as an ASSUMPTION to be confirmed by the live FOV/crop measurement.

- [x] **Step 4: Run tests.** All pass.

- [x] **Step 5: Commit**

```bash
git add superbot/projection.py tests/test_projection.py
git commit -m "feat(projection): where the wrist lands in the view we emit"
```

---

### Task 3: Glow score

**Files:**
- Create: `superbot/glow.py`
- Test: `tests/test_glow.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PATCH_FRACTION = 0.05`, `MIN_PATCH_PX = 16`, `GLOW_PERCENTILE = 95`; `patch_bounds(u, v, width, height) -> tuple[int, int, int, int]`; `score_patch(before, after, bounds) -> float`.

- [x] **Step 1: Write the failing tests**

```python
def test_patch_bounds_clip_at_the_frame_edge():
    x0, y0, x1, y1 = glow.patch_bounds(0.99, 0.99, 100, 80)
    assert x1 <= 100 and y1 <= 80 and x0 < x1 and y0 < y1

def test_a_bright_outline_moves_the_tail():
    before = [[[10, 10, 10] for _ in range(40)] for _ in range(40)]
    after = [row[:] for row in before]
    for i in range(4):
        after[i][0] = [255, 255, 255]
    assert glow.score_patch(before, after, (0, 0, 40, 40)) > 200

def test_an_unchanged_frame_scores_zero():
    frame = [[[10, 10, 10] for _ in range(40)] for _ in range(40)]
    assert glow.score_patch(frame, frame, (0, 0, 40, 40)) == 0.0
```

- [x] **Step 3: Implement**

Score = the 95th percentile of `max(R, G, B)` over `clip(after − before, 0)`, restricted to `bounds`. Pure Python: flatten the patch values, sort, index at `int(0.95 * (n − 1))`. `patch_bounds` centres a `max(MIN_PATCH_PX, round(fraction * min(width, height)))` window on `(u * width, v * height)` and clips.

- [x] **Step 5: Commit**

```bash
git add superbot/glow.py tests/test_glow.py
git commit -m "feat(glow): a local brightness delta is the grab-range oracle"
```

---

### Task 4: Parameterised verbs

**Files:**
- Modify: `spike/wbc/body_view.py` (`verb_command`, `_handle`)
- Test: `tests/test_intent.py` (parser half)

**Interfaces:**
- Consumes: `intent.verb_to_intent`, `intent.validate_intent`.
- Produces: `verb_command(executor, name, params=None)` accepting `{"verb": "look", "yaw_deg": 20}` and `{"verb": "reach", "side": "right", "forward_m": 0.18}`.

- [x] **Step 1: Parse tests in `tests/test_intent.py`** for both forms; run; fail.
- [x] **Step 2: Implement.** `_handle` passes the whole raw dict; `verb_command` asks `verb_to_intent`, then builds the pose with `executor.look_target(intent.yaw_rad)` or `executor.hand_target(...)`.
- [x] **Step 3: Verify without MuJoCo** by driving `verb_command` in a `/tmp` harness with the `spike/e3` venv (the Task-3 pattern from the previous plan), asserting `authority` and the pose delta.
- [x] **Step 4: Full suite.** `python3 -m unittest discover -s tests -t . -q`
- [x] **Step 5: Commit**

```bash
git add spike/wbc/body_view.py tests/test_intent.py
git commit -m "feat(verbs): look and reach take a yaw and an offset"
```

---

## Not in this plan

Gated on the live session and the operator; do not write them here.

- **S3 of the spec** — measure the live FOV and the HMD→VNC crop map. Needs the game running and one recorded correspondence.
- **S4** — the glow on/off still pair. If those two stills do not separate, glow is abandoned and S5 does not exist.
- **S5** — the adapter capture loop (RFB or `DISPLAY=:1`) and the 10–30 Hz skill loop.
- **S6** — the hub script and the disk lift.
- Everything E5/d1. The brain is a writer on `superbot.intent/1`, after S6 produces frames that are not all `hold`.
