# SONIC gate (C3i) and arm-override (C3j) — results

> **Status 2026-10-24 — read §2.3 of this document through `research/c3m-results.md`.**
> This document's conclusion that *"hold quality is a property of which reference motion is frozen"*
> was **half right and half harness bug**. Right: a standing clip holds and a walking one does not.
> Wrong: the 0.23–2.07 m drift was caused by freezing only the *anchor index* while the encoder's
> **0.9 s lookahead kept walking** — the same pose with a flat horizon holds to 0.0085 m/s (36×
> better), and a standing clip with anchor-only freezing is the **best** configuration measured
> (1.9×10⁻⁴). The guardrail "don't quote 3.6 cm without the clip" should become **"don't quote a hold
> without stating what the lookahead was doing."** C3j's arm-override verdict is unaffected and stands.
**Date:** 2026-10-23
**Model:** GEAR-SONIC default (`nvidia/GEAR-SONIC`, encoder 1762→64, decoder 994→29), on SONIC's own MuJoCo model
**Scripts:** `spike/wbc/gate_common.py`, `spike/wbc/gate_c3i.py`, `spike/wbc/arm_override_c3j.py`
**Data:** `spike/wbc/logs/c3i_summary.json`, `spike/wbc/logs/c3j_summary.json`, per-run JSONLs `spike/wbc/logs/c3i_*`, `c3j_*`

**Provenance.** The agent that ran this died on a 30-minute wall-clock cap while preparing its write-up (it had tried `git diff` in a repository that has no git). All runs had already finished and both summary JSONs were written. This document was assembled afterwards from those artifacts. Every number below is read out of `c3i_summary.json` / `c3j_summary.json`, which were produced from `mjData` after `mj_step`. Nothing here was re-run. **C3k (model unification) was never attempted.**

**Claim tags.** `[MEASURED]` = in those two summary files. `[CITED]` = a prior local document. `[ASSUMPTION]` = reasoning, untested.

---

## 0. Verdicts

| Step | Verdict | One line |
| --- | --- | --- |
| **C3i-1** five-cycle soak | **PASS on survival, FAIL on the hold bar** | 5/5 cycles, 100 s, no fall — but the 10 s holds drift **0.23, 0.50, 0.28, 1.82, 2.07 m**, cumulative **4.89 m**. Not a freeze. |
| **C3i-2** 60 s walk | **PASS** | **39.79 m** path in 60 s at **0.663 m/s**, yaw drift **−2.1°**, tilt max 9.5°, min z 0.719 m, no fall. This is a solid walk. |
| **C3i-3** aim while walking | **PASS at the 10 cm gate, with an orientation caveat** | Fixed aim point 0° off-axis: **3.51 cm / 7.22°** position/orientation, max 7.19 cm. At **30° off-axis: 8.63 cm / 28.3°** — passes the gate, too coarse to be called aiming. |
| **C3j** QP arm override | **FAIL — and the hypothesis is falsified** | Hard override makes the arms **worse**: hold 1.14 → **14.40 cm**, walking aim 3.51 → **12.84 cm**. SONIC resists having its arms replaced, exactly as C3f's policy did. Only **orientation-only** override helps (7.22° → **5.08°**). |
| **C3k** model unification | **NOT RUN** | The two controllers still live on different models. Every hybrid estimate remains approximate. |

**The single most important result is not in the table.** The 3.6 cm hold that `research/wbc-policy-proposal.md` reported — and that §7 of the design doc quotes — was measured by freezing a **squat** motion at frame 0. This soak froze a **walking** motion mid-stride, and the hold degraded to **0.23–2.07 m**. SONIC's hold quality is a property of *which reference motion is frozen*, not a fixed capability. See §2.3.

---

## 1. C3i-1 — five-cycle soak

Program: walk 5 s → hold 10 s → walk 5 s, ×5, on `walk_forward_amateur_001__A001`, freeze frames `[250, 1250, 2250, 3250, 4250]`.

| Cycle | Hold net (m) | Hold max exc (m) | Hold tilt max | Hold min z | Resume dist / ref | Re-lock (q-err) | Tilt max | Survived |
| ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |
| 0 | **2.070** | 2.070 | 5.65° | 0.746 | 2.939 / 3.132 | 4.14 s | 9.48° | yes |
| 1 | **0.227** | 0.227 | 9.17° | 0.733 | 2.973 / 3.132 | 4.44 s | 10.35° | yes |
| 2 | **0.497** | 0.509 | 5.60° | 0.751 | 3.021 / 3.105 | 4.70 s | 5.60° | yes |
| 3 | **1.815** | 1.815 | 4.31° | 0.737 | 3.059 / 3.123 | 4.66 s | 7.76° | yes |
| 4 | **0.278** | 0.278 | 13.50° | 0.739 | 2.834 / 3.154 | 4.94 s | **16.68°** | yes |

`c3i_summary.json` `soak`: `n_ticks` 5000/5000, `survived` true, `fall_t` null, `cum_hold_net` **4.887 m**, `tilt_max_deg_all` **16.68°**, `min_z_all` 0.719 m.

**Reading it honestly.** The gate asked for no fall, and there is no fall — 100 s of alternating walk and hold, five times, staying upright, worst tilt 16.7°, never below z = 0.719 m. That is real and it is the first time anything in this project has done it.

But the holds are not holds. Two of five drifted ~2 m in 10 s; the others drifted 0.23–0.50 m. Only cycle 1 (0.227 m) is anywhere near a number you could call "standing still", and it is still 45× the QP's 0.22 mm and 7× the design's own 3.2 cm target. **Do not describe this as SONIC holding.** It is SONIC surviving a hold while continuing to walk.

Two secondary observations that matter more than they look:

- **Re-locking takes ~4.1–4.9 s.** After every hold the policy needs about four seconds to return to the walking reference's path error. That is a long blind window if the game is live.
- **Resume distance is consistently 2.83–3.06 m against a 3.10–3.15 m reference.** So the walk *resumes correctly* — the failure is entirely in the hold segment, not in the handoff. That is good news for C3h's old problem: the transition works, the standing does not.

---

## 2. C3i-2 — 60 s walk

| Metric | Value |
| --- | --- |
| Ticks | 3000 / 3000 (59.98 s) |
| Path length | **39.79 m** |
| Net displacement | 37.82 m |
| Mean speed (path / net) | **0.663** / 0.631 m/s |
| Reference path length | 38.46 m |
| Path error mean / max / end | 0.230 / **0.554** / 0.529 m |
| Base yaw drift | **−2.10°** |
| Yaw per 10 s window | −2.50, +0.81, −2.79, +3.35, −3.16° |
| Path bearing peak-to-peak | 13.92° |
| Max tilt | **9.48°** |
| Min z | 0.719 m |
| Survived | **yes**, no fall |

This is the strongest result the project has produced for locomotion. Sixty seconds, forty metres, no fall, 2° of heading drift, 9.5° of tilt, tracking the reference path to 23 cm mean over 40 m. The per-10-s yaw oscillation (±3°) says the heading wanders slightly but does not accumulate — which is exactly what a closed-loop walk should look like.

**PASS.** The walking half of the goal is real.

---

## 3. C3i-3 — aim while walking

A fixed world aim point for head + right wrist, commanded during walking. `wrist_rel` `[0.33, −0.06, 0.10]` m from the torso.

| Case | Survived | Dist | Tilt max | Pos mean | Pos p95 | Pos max | Orn mean | Aim mean | Head aim mean |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **walk_aim_0deg** | yes | 6.48 m | 15.09° | **3.51 cm** | 5.63 cm | 7.19 cm | **7.22°** | 6.53° | 5.82° |
| **walk_aim_30left** | yes | 6.36 m | 15.84° | **8.63 cm** | 12.58 cm | 13.58 cm | **28.33°** | 28.17° | 11.89° |

Gate bar was ≤10 cm position. Both pass it. But the honest reading is narrower than "pass":

- **Straight ahead, the arms track to 3.5 cm and 7.2°.** At the game's measured 1.41 m enemy distance, 7.2° is ≈17 cm of lateral pointing error, and 28° would be ≈67 cm. The aim *position* is fine; the aim *direction* is the weak axis.
- **30° off-axis it degrades to 8.6 cm and 28°.** So the body aims well in the direction it is walking and poorly to the side. For Superhot, where enemies circle and appear at angles, this is the real limitation.
- Note the correspondence with §1: **the hold cases are worse than the walking cases.** `hold_aim_0deg` gives 1.14 cm / 7.33°, `hold_aim_30left` gives **4.87 cm / 13.66°**. Standing still does not improve the arms; it degrades them. The policy is better at tracking an aim point while moving, which is backwards from intuition and consistent with it being a motion-tracking controller.

---

## 4. C3j — can the QP replace SONIC's arms?

Hypothesis under test: SONIC's arms are already driven by external VR targets in its training distribution, so overriding its 14 arm joints with a sharper IK solve toward the same target would be tolerated, where C3f's override of a balance-using locomotion policy was not.

Variants: `policy` (baseline), `hard` (full override), `rate:N` (override with an N rad/s joint rate limit), `blend:N` (fraction of the IK target mixed in), `orient` (override wrist orientation only, leave arm positions to the policy), `int:N` (override plus integral correction).

### 4.1 Holding (10 s, aim target)

| Variant | Survived | Pos err | Orn err | Aim err | Head aim | IK ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **policy (baseline)** | yes | **1.14 cm** | 7.33° | 7.23° | 1.68° | — |
| hard | yes | **14.40 cm** | 24.00° | 22.64° | 3.95° | 1.38 |
| **orient** | yes | 2.79 cm | **4.15°** | 4.12° | 1.80° | 1.43 |
| blend:0.5 | yes | 4.09 cm | 4.82° | 4.44° | 1.47° | 1.39 |
| int:0.15 | yes | 7.72 cm | 14.42° | 13.80° | 8.96° | 1.36 |

### 4.2 Walking, motion target (no aim point)

| Variant | Survived | Net disp | Speed | Tilt max | Pos err | Orn err |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **policy** | yes | 6.357 m | 0.637 m/s | 18.21° | **2.63 cm** | **5.74°** |
| hard | yes | 6.269 m | 0.628 m/s | 16.11° | **9.15 cm** | 11.07° |
| int:0.5 | **FALL** | 5.446 m | 0.789 m/s | **64.69°** | 47.55 cm | 80.56° |
| int:1.0 | **FALL** | 2.850 m | 0.699 m/s | **74.81°** | 43.49 cm | 78.27° |

### 4.3 Walking, aim target

| Variant | Survived | Dist | Tilt max | Pos err | Orn err | Aim err | Head aim |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **policy (baseline)** | yes | 6.477 m | 15.09° | **3.51 cm** | 7.22° | 6.53° | 5.82° |
| hard | yes | 6.518 m | 14.97° | **12.84 cm** | 22.40° | 19.55° | 4.73° |
| rate:2.0 | yes | 6.566 m | 13.91° | 12.43 cm | 22.01° | 19.42° | 6.49° |
| rate:6.0 | yes | 6.519 m | 14.62° | 13.08 cm | 22.80° | 19.97° | 4.80° |
| blend:0.5 | yes | 6.465 m | 14.90° | 4.88 cm | 8.92° | 7.89° | 5.88° |
| blend:0.75 | yes | 6.457 m | 14.87° | 7.49 cm | 13.77° | 11.88° | 6.22° |
| **orient** | yes | 6.320 m | 15.29° | **3.27 cm** | **5.08°** | 4.42° | 5.53° |
| int:0.15 | yes | 5.938 m | 14.87° | 5.97 cm | 12.90° | 9.84° | 8.50° |
| int:0.5 | **FALL** | 4.379 m | 69.62° | 33.00 cm | 75.03° | 60.19° | 30.00° |
| int:1.0 | **FALL** | 3.550 m | 74.01° | 25.33 cm | 93.28° | 77.39° | 79.33° |

Head-yaw coupling survives the override (`yaw30_hard`: 6.256 m, tilt 16.03° vs baseline 6.336 m / 18.45°), so a hard override does not break the waist coupling or the walk. It just makes the arms worse.

### 4.4 Verdict

**The hypothesis is falsified.** A hard arm override under SONIC is not tolerated — it *degrades* tracking by roughly 4× in position (1.14 → 14.40 cm holding, 3.51 → 12.84 cm walking) and ~3× in orientation. Rate-limiting does not help. Blending does not help beyond 50%, where it merely returns to baseline. **Integral correction is actively dangerous**: `int:0.5` and `int:1.0` both fall with 65–75° tilt, in every target case, so the arm loop can tip the robot.

This is a milder version of C3f, and the *same* mechanism: the policy produces its own arm motion and a joint-space override fights it rather than commanding it. The absence of falls in the non-integral variants is reassuring for balance and irrelevant for precision — the arms simply do not go where the IK says, and `arm_track_err_final` ≈ 0.23 says the IK never converged toward its own target.

**The one variant that helps is `orient`**, and it helps in the direction that matters for shooting: orientation 7.22° → **5.08°** and aim 6.53° → **4.42°** while walking, with position unchanged (3.51 → 3.27 cm). When holding it trades position for orientation (1.14 → 2.79 cm position, 7.33 → 4.15° orientation). A wrist-orientation-only override is a small, safe improvement to pointing; it is not the leap from 7° to 2.3°.

**So: no, the QP does not transfer onto SONIC.** The body aims with SONIC's own arms at ~3.5 cm / 7° straight ahead and ~8.6 cm / 28° off-axis, nudged to ~5° pointing by an orientation-only override.

---

## 5. The finding that changes the plan

**SONIC's hold quality depends on which reference motion you freeze.**

`research/wbc-policy-proposal.md` measured the hold by freezing **`squat_001__A359` frame 0** and got **3.6 cm net over 20 s**. That number is in the design doc as SONIC's hold capability. This soak froze a **walking** clip mid-stride and got **0.23–2.07 m over 10 s**.

Both measurements are correct. They are measuring different things, and the design doc picked the flattering one. Freezing a walk mid-stride leaves the policy in a walking gait phase with nowhere to put the step; freezing a standing clip leaves it standing.

The practical consequence is not that SONIC cannot hold — it is that **`hold` must be implemented as a reference-motion switch to a standing clip, not as a freeze of the current clip.** That is a different integration decision from the one §7 currently implies, and it is cheap to test: the squat clip already exists, and the walk→squat→walk transition is exactly what the soak's resume segment already does successfully (resume distance within 0.3 m of reference, ~4.5 s re-lock).

This deserves to be the next experiment, ahead of any more arm work.

---

## 6. What is still unmeasured

- **C3k — the two controllers are on different models.** SONIC runs its own MuJoCo model; the QP's 0.22 mm / 2.34° numbers are on Menagerie `scene.xml`. Nothing here compares them like-for-like.
- **Hold on a standing reference, and walk→stand→walk as a reference switch.** Untested. §5 says it is the key experiment.
- **Live co-residency.** All numbers are headless CPU. The 50 Hz loop beside Superhot + Monado is unverified.
- **Orientation while aiming off-axis during walking with the `orient` override.** Only the 0° aim point was run with `orient`.
- **60 s with an aim point.** The 60 s walk carries no aim target, and the aim cases are 10 s.
- **The re-lock window's effect on the game.** ~4.5 s of degraded tracking after every hold; whether that is safe in Superhot is a game-side question.
- **Push recovery, uneven ground, external forces.** Still none.
- **C0.** Still unrun. All of the above is worthless if the game rejects the synthetic HMD.

---

## 7. What the design doc should now say

1. **Walking is real: 39.79 m / 60 s, 0.663 m/s, −2.1° yaw drift, no fall.** State this as the project's verified locomotion result. It supersedes every C3 lineage number.
2. **Do not quote "SONIC holds 3.6 cm" without the clip.** It holds 3.6 cm when a squat motion is frozen and 0.23–2.07 m when a walk is frozen. `hold` is a reference-motion switch, and the next experiment is exactly that.
3. **Drop the QP-on-SONIC hybrid.** The arm override is measurably worse in every hard/blended/rate-limited variant and tips the robot with integral action. Keep at most an orientation-only override as a pointing refinement (7.2° → 5.1°). The two-controller architecture in §7 is not achievable by joint override.
4. **Aiming is straight-ahead-only.** 3.5 cm / 7.2° ahead, 8.6 cm / 28° at 30° off-axis. A game that puts enemies to the side exposes the body. Either the body turns to face the target (turning is walking, which works) or the claim narrows.
5. **The five-cycle soak passes on survival, not on stillness.** Say "survives 5 walk→hold→walk cycles; the holds drift and the walk resumes correctly". Do not say "SONIC holds and transitions."

---

## 8. Reproduce

```bash
cd /home/nate/Projects/vr-bot
# the gate (writes c3i_summary.json + per-run jsonl)
spike/e3/.venv/bin/python spike/wbc/gate_c3i.py
# the arm override matrix (writes c3j_summary.json)
spike/e3/.venv/bin/python spike/wbc/arm_override_c3j.py
```

Existing logs are not to be deleted: `spike/wbc/logs/c3i_*.jsonl`, `spike/wbc/logs/c3j_*.jsonl`, and the two summary JSONs are the evidence for every table above.
