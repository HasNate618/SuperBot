# C3d / C3e Legs Spike Results — model mismatch, heading control, park ↔ walk

**Spike:** C3d / C3e (sim only, headless, CPU)
**Agent:** `c3de-legs`
**Date:** 2026-10-23
**Policies/models:**
- Policy: official `mujoco_playground` ONNX, SHA-256 `db2eb258494c1297c43d2b9ffa94cdbde97654c2a44cbab0b40fd4b990752a5b`, 903,543 bytes (same checkpoint as C3a/b/c).
- Model A: Playground training XMLs, `spike/c3/playground_g1/xmls/scene_mjx_feetonly_flat_terrain.xml` (+ `g1_mjx_feetonly.xml`, `sensor.xml`, assets).
- Model B: Menagerie `unitree_g1/scene_mjx.xml` (the model used by C3a/b/c).
- Park pose/gains: Menagerie `unitree_g1/scene.xml` keyframe `stand` (straight legs).

**Script:** `spike/c3/measure_c3de.py`

Commands actually run:
```
spike/e3/.venv/bin/python spike/c3/measure_c3de.py --only c3e1
spike/e3/.venv/bin/python spike/c3/measure_c3de.py --only c3e2 --model playground
spike/e3/.venv/bin/python spike/c3/measure_c3de.py --only c3e2 --model menagerie
spike/e3/.venv/bin/python spike/c3/measure_c3de.py --only c3d --ramp 0.4
```

All pose/speed numbers are **[MEASURED]** from `mjData` after `mj_step`. Every `[ASSUMPTION]` is tagged inline and in §6. Raw evidence: `spike/c3/logs/c3e1_*`, `c3e2_*`, `c3d_*` (CSV+JSON) and `spike/c3/c3de_measurements.json`. **The per-run logs were pruned from the repository; `c3de_measurements.json` is committed.**

## 0. Verdicts

| Step | Verdict | One line |
|---|---|---|
| C3e-1 | **PASS** | The curve is the **model**, not the policy: same ONNX walks **+0.23° / 10 s** (7.45 m, 0.743 m/s) on the Playground XML and **+174.5° / 10 s** on Menagerie. A straight-command walk exists; we already had the wrong XML. |
| C3e-2 | **ALLOWED-FAIL** | A P-only heading outer loop cancels the curve on the *old* model (steady 7.3° RMS at `ky=2, kv=1`) but costs **0.51 m lateral RMS**, most higher gains **fall**, and the 20 s zero-velocity run **falls**. On the Playground model the loop is unnecessary and adds lateral scatter. Heading is controllable; position/freeze is not. |
| C3d | **FAIL** | **park → walk** survives; **walk → park falls in every variant** (0.64–1.49 s after park starts, tilt 60°). Ramps 0.05–2.0 s, a 2–3 s zero-command brake, and a slow approach all fail. Even the `knees_bent` static pose falls. Park cannot be a frozen open-loop pose entered from walking. |

## 1. Setup

The C3a/b/c recipe is unchanged: `SIM_DT 0.002`, `CTRL_DT 0.02`, 10 substeps, `ACTION_SCALE 0.5`, `GAIT_FREQ 1.5`, `default_angles = knees_bent.qpos[7:]`, obs `[1,103]`, action `[1,29]`, adapter `command = [vx*1.5, vy*0.8, wz*2*pi]`. The new harness loads each XML into its own `MjModel` with the same session and the same `PolicySim`.

The Menagerie and Playground models have identical kinematics: `nq 36`, `nv 35`, `nu 29`, **mass 33.341 kg**, identical joint/actuator order (verified by name), identical armature, identical timestep, identical friction and solver type. That makes the A/B a clean model swap.

## 2. C3e-1 — same policy, two models [MEASURED]

### 2.1 Straight command `(0.5, 0, 0)`, 10 s

| Model | path | path speed | forward x | heading travel | heading end | max tilt | fall |
|---|---:|---:|---:|---:|---:|---:|---|
| **Playground** `scene_mjx_feetonly_flat_terrain.xml` | 7.447 m | 0.745 m/s | **+7.426 m** (0.743 m/s) | **+0.23°** | +1.15° | 4.8° | no |
| Menagerie `scene_mjx.xml` | 5.184 m | 0.518 m/s | −0.346 m | **+174.5°** | +175.3° | 9.9° | no |

The Playground run is a straight line to within a quarter of a degree over 10 s. The Menagerie run is a half-circle: the *path* is 5.18 m but the initial-heading forward component is **negative**, because the robot turned around.

### 2.2 Zero command, 20 s

| Model | base drift | head drift (xy) | heading travel | heading end | max tilt | fall |
|---|---:|---:|---:|---:|---:|---|
| Playground | **2.408 m** | 2.397 m | **+91.5°** | +93.8° | 5.5° | no |
| Menagerie | 1.736 m | 1.727 m | +66.3° | +68.6° | 8.5° | no |

Zero-command drift is **not** caused by the model swap — it is a policy property and is actually slightly worse on the Playground model (2.41 m vs 1.74 m / 20 s). That part of C3b stands.

### 2.3 What actually differs between the XMLs

| Item | Playground | Menagerie `scene_mjx.xml` |
|---|---|---|
| Foot/leg colliders | 6 simple geoms: `left/right_thigh`, `left/right_shin`, `left/right_foot` | 25 collision geoms incl. `*_foot_box_collision` + `*_foot1/2/3_collision`, pelvis/torso/head/arm boxes |
| Actuator bias damping term | **0** (`[0,-75,0]` hips/knees/waist) | **−2** (`[0,-75,-2]`) |
| Ankle-roll actuator gain | **2** | **20** |
| Wrist actuator gain | **2** | **20** |
| Solver iterations / line-search | 3 / 5 | 5 / 8 |
| `dof_damping` on arms/waist | nonzero (0.2–2.0) | **0** |
| Mass, armature, timestep, friction, cone, gravity | identical | identical |

**C3e-1 verdict: PASS.** The heading curve is reproduced by swapping only the model. The policy and the ONNX are fine. The practical answer to C3a's “does not walk straight” is: **load the Playground feet-only flat-terrain XML**, where it walks straight with no outer loop.

[ASSUMPTION] The dominant contributors are the different foot collision set and the 10× ankle-roll gain difference (2 vs 20), with the `−2` actuator damping term and solver iterations secondary. This spike did not isolate the individual terms.

## 3. C3e-2 — can an outer loop cancel the curve? [MEASURED]

P-only controller, no integrator (no windup): `wz = clamp(kp_yaw * heading_err, ±0.6)`, `vx = clamp(target_vx + kp_vx * (target_vx − vx_measured), [−1.0, 1.2])`, 10 s at `vx = 0.5`, target heading 0.

### 3.1 Sweep on Menagerie (the model that curves)

Best non-fall: **`ky=2.0, kv=1.0`** — steady heading RMS **7.28°**, max 14.85°, lateral RMS **0.511 m**, forward **0.623 m/s**, max tilt 11.6°, no fall. Open loop was +174.5° travel, so the loop does bend the robot back toward zero.

But the sweep is fragile:

| | `ky=0.5` | `ky=1` | `ky=2` | `ky=4` | `ky=8` |
|---|---|---|---|---|---|
| `kv=0.5` | ok | ok | **FALL** | **FALL** | **FALL** |
| `kv=1` | ok (14.1°) | ok (9.9°) | **ok (7.3°)** | **FALL** | **FALL** |
| `kv=2` | ok (13.0°) | ok (9.1°) | **FALL** | **FALL** | **FALL** |
| `kv=4` | ok (9.5°) | ok (8.5°) | **FALL** | **FALL** | **FALL** |

Only the low-yaw-gain corner is stable; **11 of 20** combinations fall. Doubling the winning gains also falls.

- 20 s zero command with `ky=2, kv=1`: **FALL** (tilt 100°, base drift 2.09 m, heading travel +208°). The loop is not usable as a freeze.

### 3.2 Sweep on Playground (the model we should adopt)

Best: **`ky=4.0, kv=1.0`** — steady heading RMS **4.23°**, max 10.30°, lateral RMS **0.438 m**, forward **0.610 m/s**, max tilt 4.83°, no fall.

- This is *worse* than the open-loop Playground walk, which already held **+0.23°** of heading travel over 10 s. The loop is treating a non-problem and adding **0.44 m** of lateral wander while dropping forward speed 0.743 → 0.610 m/s.
- 20 s zero command with `ky=4, kv=1`: no fall, heading held (travel +0.096°, end +2.49°), but base drift **1.344 m** and head drift **1.307 m** (0.067 m/s). Still ~67× the 2 cm freeze budget.
- Stability margin: doubling `kv` (to 2.0) survives at 4.47° RMS; doubling `ky` (to 8.0) **falls** (tilt 170.8°); doubling both **falls**. The useful region is narrow on the yaw gain.

**C3e-2 verdict: ALLOWED-FAIL.** The outer loop can cancel the Menagerie heading curve at moderate gain, but it does not give a clean walk (0.5 m lateral scatter, many falls), and it cannot hold position at zero velocity. On the Playground model it is unnecessary. Prefer the model fix; do not ship the outer loop as a straightness fix without a lateral/longitudinal study.

## 4. C3d — park ↔ walk mode machine [MEASURED]

### 4.1 Implementation

`ModeSim` runs the Playground model. Two modes:
- **walk:** policy owns all 29 joints, command `vx = 0.5`.
- **park:** open-loop static `stand` ctrl from the Menagerie `scene.xml` keyframe, with the Menagerie actuator gains copied by name onto the Playground model (required — the Playground gains tip the straight-leg pose to 160°). Arms are held by the existing DLS IK at the park neutral; the reach test adds **+0.10 m** forward on the left wrist with a cosine ramp over 1–3 s.

Transitions blend actuator **ctrl** and actuator **gains** over `ramp` seconds. Initial park is entered from `stand_q`; initial walk from `knees_bent`. Fall = base `z < 0.6·z0` or sampled tilt > 60°.

### 4.2 Results

| Test | Schedule | ramp | Result | park entry outcome |
|---|---|---:|---|---|
| T1 | park5 → (walk2 → park5)×5 | 0.4 | **FALL** | first park OK; park after 2 s walk falls at **0.97 s** into park |
| T2 | walk10 → park10 | 0.4 | **FALL** | falls at **0.80 s** into park |
| T3 | T1 + left reach | 0.4 | **FALL** | reach in the *initial* park OK; second park falls at 0.88 s (reach never reached) |
| T4 | walk10 → stop2 → park10 | 0.4 | **FALL** | stop still moved **1.72 m**; park falls at 1.49 s |
| T5 | walk10 → stop3 → park10 | 0.4 | **FALL** | stop still moved **2.15 m**; park falls at 1.33 s |
| T6 | walk10 → park10 | 1.0 | **FALL** | 0.72 s |
| T7 | walk10 → park10 | 2.0 | **FALL** | 0.72 s |
| T8 | walk10 → park10 | 0.1 | **FALL** | 0.87 s |
| T9 | walk10 → park10 | 0.05 | **FALL** | 0.91 s |
| T10 | walk5 @ 0.05 → park10 | 0.4 | **FALL** | still moved 0.98 m in 5 s; park falls at 1.19 s |
| T11 | walk10 → knees10 (static `knees_bent`, walk gains) | 0.4 | **FALL** | falls at **0.64 s** |

What *does* work:
- **Initial park from `stand_q`:** z 0.794 m, tilt **0.1°**, settled in **0.04 s** (T1). T3's initial park held 0.2° while the left wrist reached **0.10 m** with **0.02 m** max IK residual.
- **park → walk:** T1/T3 walk segment after a settled park ran 2 s, moved 1.17–1.39 m, ended at 5–6° tilt, no fall.
- **walk:** 10 s on the Playground model, 7.37 m, 3.4° end tilt, no fall.

Every walk → static handoff fell. The fall signature is consistent: at park start the robot is still moving ~0.3–0.8 m/s; `base_z` briefly rises as the legs extend to the straight stance, then tilt grows monotonically (pitch first, then roll) and it goes down at 60°.

### 4.3 Root cause

This is **not** a ramp-duration bug. Ramps from 0.05 s to 2.0 s all fail within 0.6–0.9 s. It is the combination of:

1. **No balance feedback in park.** The static `stand` ctrl is a fixed joint target; once the base has any residual velocity or a staggered foot placement, nothing corrects the CoM. The pose is only stable from its exact initial condition (hence the clean initial park).
2. **The walk policy has no true stop.** A zero command still steps: after a 10 s walk it moved **1.72 m in 2 s** and **2.15 m in 3 s**, i.e. it does not brake to rest. A “stop” phase therefore cannot prepare the handoff.
3. **The transition freezes the last walk action.** During the blend the policy is no longer queried, so the only balance authority is the frozen action + static target. A longer ramp makes this open-loop interval longer, not safer.

The `knees_bent` control (T11) falls too, so this is not specific to the straight-leg pose.

**C3d verdict: FAIL.** Park → walk is safe; walk → park is not, with this policy and an open-loop park pose. Do not claim a working mode switch.

## 5. What this means for the design

- **Adopt the Playground feet-only flat-terrain XML as the simulation/collision model.** It removes the curve (0.23° / 10 s) with no controller changes. This supersedes the C3a conclusion that the policy “does not walk straight”: on the right model it does. The Menagerie model remains useful for visualization/URDF parity, but not for policy evaluation.
- **Do not ship a heading outer loop as the straightness fix.** On the Playground model it is unnecessary; on Menagerie it buys heading at the cost of ~0.5 m lateral scatter and many falls, and it fails at zero velocity. If the team keeps Menagerie for some reason, `ky≈2, kv≈1` is the only stable corner found, and it still is not a clean walk.
- **The 2 cm / 20 s freeze remains unachievable.** On Playground, zero command drifts 2.41 m / 20 s open-loop and 1.34 m / 20 s with the best outer loop. C3b's FAIL stands; the model fix does not fix creep.
- **Park cannot be an open-loop frozen pose after walking.** Options, none demonstrated here:
  1. keep the policy active in park at zero velocity (accept ~0.07–0.10 m/s creep and the C3b drift);
  2. add a balance-feedback stand controller (CoM/ankle strategy) for park;
  3. find a true stop behaviour for the walk policy before handing off.
- **Never transition walk → park while moving in the app.** Park → walk is safe. If the experience needs “freeze in place”, it must be a policy-level behaviour or a balance controller, not `ctrl = stand`.
- **Park gains must be retuned on the chosen model.** This spike used Menagerie park gains on the Playground model because the Playground gains cannot hold the straight-leg pose. That cross-model gain mix is a stopgap, not a recommendation.

## 6. Honesty notes

- **C3d reach is only partially validated.** The +0.10 m left-wrist reach was exercised in T3's *initial* park (0.02 m IK residual, stable at 0.2° tilt). In T3's second park the robot fell at 0.88 s, before the reach ramp starts at 1.0 s — so “reach after a walk→park transition” was **not** tested. The reported `reach_max` for that segment is 0.0 m.
- **The model-mismatch cause is not decomposed.** The A/B is controlled (same policy, same harness, two XMLs), but the spike did not switch individual terms (colliders vs ankle-roll gain vs damping vs solver). The attribution in §2.3 is an [ASSUMPTION].
- **No retraining, no tune-as-shipped.** The walk policy was used exactly as downloaded; only the XML and the explicit outer loop/park controller were changed. Park gains were taken verbatim from `scene.xml`.
- **“park → walk works” is a 2 s segment**, not a completed multi-cycle run; the 5× sequence never finished because every reverse transition fell.
- **Cross-model gain mix.** Park uses Menagerie PD gains on the Playground model. It holds the stand pose (0.1° tilt) but is not a validated production gain set.
- Metrics sampled every 0.1 s; fall = `qpos[2] < 0.6*z0` or sampled tilt > 60°, matching the C3a recipe. `yaw_rate` uses the finite difference of unwrapped head yaw (no `np.gradient` duplicate-t artifacts).
- No files outside `spike/c3/` and this results file were modified. No NixOS, Monado, Steam, Proton, or game process was touched. The prior C3a/b/c results file was not edited.

## 7. Artifacts

- `spike/c3/measure_c3de.py` — C3e-1 model A/B, C3e-2 outer-loop sweep/margins, `ModeSim` and C3d schedules.
- `spike/c3/c3de_measurements.json` — aggregate: `c3e1`, `c3e2` (Playground), `c3e2_menagerie`, `c3d`.
- `spike/c3/logs/c3e1_pg_fwd10.*`, `c3e1_pg_zero20.*`, `c3e1_men_fwd10.*`, `c3e1_men_zero20.*`.
- `spike/c3/logs/c3e2_ky*_kv*.*`, `c3e2_zero20_winner.*`, `c3e2_double_*`.
- `spike/c3/logs/c3d_T1..T11_*.csv/json` (JSON includes per-segment summary and the raw 0.1 s samples).
- `spike/c3/playground_g1/` — the Playground `scene_mjx_feetonly_flat_terrain.xml` + `g1_mjx_feetonly.xml` + `sensor.xml` + assets used for Model A.
- `spike/c3/policy/g1_policy.onnx` — official checkpoint, SHA-256 above.
