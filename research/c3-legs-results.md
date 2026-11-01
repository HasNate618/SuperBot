# C3 Legs Spike Results — Playground G1 policy on the Menagerie G1

**Spike:** C3a / C3b / C3c (sim only, headless, CPU)
**Agent:** `c3-legs`
**Date:** 2026-10-23
**Model:** `/home/nate/Projects/Clank/spike/e3/vendor/mujoco_menagerie/unitree_g1/scene_mjx.xml`
**Policy:** official `mujoco_playground` ONNX, not a third-party substitute
**Script:** `spike/c3/measure_c3.py` — run with `spike/e3/.venv/bin/python spike/c3/measure_c3.py --only all`

All pose and speed numbers below are **[MEASURED]** from `mjData` after `mj_step`. Nothing is a desired target or a command echoed back. **[ASSUMPTION]** tags are called out inline and in §6. Raw evidence: `spike/c3/logs/*.csv`, `spike/c3/logs/*.json`, aggregate `spike/c3/measurements.json`. **The per-run files were pruned from the repository; the aggregate is committed, and the logs regenerate by re-running the spike.**

## 0. Verdicts

| Step | Verdict | One line |
|---|---|---|
| C3a | **FAIL** | The *port reproduces the prior numbers exactly* (0.515 vs 0.516 m/s forward-x; 0.471 vs 0.475 m drift), but the required arm-override configuration falls at **25.6 s** and triples zero-command creep. Separately, the unmodified policy does not walk straight: +73.6° of heading change in 5 s. |
| C3b | **FAIL** | Zero-command drift is **1.736 m in 20 s** (0.481 m at 5 s, matching the known 0.475 m/5 s). The tracked head moves **1.727 m**. The < 2 cm freeze budget is missed by ~87×. |
| C3c | **ALLOWED-FAIL** | Both options degrade. B gives a real independent head arc (**+34.3°** waist-to-head) but collapses forward speed to **0.093 m/s** and falls at **2.5 s** when the required arm IK is added. A cannot look independently. Keep **A**; do not claim independent look. |

## 1. Policy source and environment

**Source [MEASURED]:**
- Repo: `https://github.com/google-deepmind/mujoco_playground`
- File: `mujoco_playground/experimental/sim2sim/onnx/g1_policy.onnx`
- Raw URL used: `https://raw.githubusercontent.com/google-deepmind/mujoco_playground/main/mujoco_playground/experimental/sim2sim/onnx/g1_policy.onnx`
- Last upstream commit touching that path: `046a38fa7e156664406c63f09afde25a996300de` (2025-01-15)
- Size: **903,543 bytes**
- SHA-256: `db2eb258494c1297c43d2b9ffa94cdbde97654c2a44cbab0b40fd4b990752a5b`
- Official loader copied for reference: `spike/c3/policy/play_g1_joystick.py`

The ONNX was loaded with `onnxruntime` and confirms:
- input `obs` shape `[1, 103]`
- output `continuous_actions` shape `[1, 29]`

**Environment [MEASURED]:**
- Python 3.12.13 in `spike/e3/.venv`
- `mujoco 3.17.0`, `numpy 2.2.6`
- `onnxruntime 1.31.0` installed with:
  `uv pip install --python /home/nate/Projects/Clank/spike/e3/.venv/bin/python onnxruntime`
- CPUExecutionProvider, no GPU/display, no Monado, no game

**Control recipe [MEASURED, replicated from `bench/g1_policy_test.py` and the official loader]:**
- `SIM_DT = 0.002`, `CTRL_DT = 0.02`, 10 substeps, `ACTION_SCALE = 0.5`, `GAIT_FREQ = 1.5`
- `default_angles = m.keyframe("knees_bent").qpos[7:]`
- `ctrl[:] = action * 0.5 + default_angles`
- obs = `linvel(3) gyro(3) gravity(3) command(3) qpos[7:]-default(29) qvel[6:](29) last_action(29) phase(4)`
- adapter command mapping: `command = [vx*1.5, vy*0.8, wz*2*pi]`
- sensors `local_linvel_pelvis`, `gyro_pelvis`; gravity from site `imu_in_pelvis`; head pose from the `head_link` **mesh geom** on `torso_link` (there is no `head_link` body in this model; E3 used the same geom).
- Waist joint ranges measured: yaw `[-2.618, 2.618] rad`, roll/pitch `[-0.52, 0.52] rad`.

## 2. C3a — reproduce the walk

### 2.1 Prior-number reproduction [MEASURED]

| Run | Command | Result | Prior |
|---|---|---|---|
| Zero 5 s | `(0,0,0)` | net drift **0.471 m**; dx +0.471, dy +0.012; max tilt 8.5°; no fall; head z last-3 s 1.1695 m | 0.475 m / 5 s |
| Forward 5 s | `(0.5,0,0)` | `dx +2.576 m`, `dy +2.454 m`; forward-x speed **0.515 m/s**; path speed 0.712 m/s; heading change **+73.6°**; max tilt 9.9°; no fall | 0.516 m/s, dx +2.58 / dy +2.46 |

The port is confirmed. But the same 5 s run also shows the policy is **turning while it translates**: the path length is 3.558 m, not 2.58 m, and the heading rotates +73.6°. Initial-heading lateral error is **2.454 m max / 1.162 m RMS**. The historical “0.516 m/s” is the x-displacement only. It is not straight-line walking.

30 s unmodified baseline [MEASURED]: no fall (max tilt 9.9°), but net displacement only 4.90 m, final heading `-159°`; it is circling, not tracking a heading. `base_z` 0.669–0.744 m.

### 2.2 Real C3a — policy legs + waist, arms on E3 DLS IK holding neutral [MEASURED]

The arm targets are the reset-time wrist positions expressed in the `torso_link` frame; E3's `mj_jacBody` + damped-least-squares solver runs at 50 Hz with a 2 rad/s joint rate limit. Max IK residual in the 0.1 s samples: **0.075 m** (30 s walk), 0.053 m (20 s zero).

| Window | Unmodified policy | Arms on DLS IK |
|---|---|---|
| 5 s forward | path 3.558 m, heading +73.6°, max tilt 9.9° | path 3.370 m, heading **+98.8°**, max tilt 11.2° |
| 10 s forward | path 5.184 m, heading +174.5° | path 6.260 m, heading +135.8° |
| 30 s forward | **no fall**, max tilt 9.9° | **FALL at 25.6 s**; tilt 96.1°, base z 0.120 m, head z 0.077 m |
| Zero 20 s | 1.736 m net drift, +66.3° heading | **5.292 m net drift** (0.265 m/s), **-113.0° heading** |

**Did the arms break the walk? Yes.** The walk was already curving, but the arms made heading divergence faster (98.8° vs 73.6° at 5 s) and the run fell at 25.6 s. The dominant early symptom here was **heading divergence and a fall**, not the clean “forward lean + endless stepping” signature; torso pitch std at 5 s was 2.68° in both cases. At zero command the arm override **tripled creep** (5.29 m vs 1.74 m in 20 s) and reversed the heading drift.

### 2.3 HMD height on `knees_bent` [MEASURED]

10 s zero-command policy run, last-3 s head `head_link` geom z:

- mean **1.1698 m**, range 1.1454–1.1805 m, initial 1.1777 m
- E3 straight-legged `stand` value was **1.221 m**
- change: **-0.051 m (-4.2%)**

Other measured head heights: arms-neutral zero 1.1476 m; unmodified forward walk 1.1490 m; C3b zero drift 1.1693 m. The body’s HMD is ~1.15–1.17 m under the policy, not 1.221 m.

**C3a verdict: FAIL.** Port/reproduction PASS; the required arms-in-the-loop configuration does not survive 30 s, and the policy does not walk a straight line even without arms.

## 3. C3b — zero-command drift, 20 s [MEASURED]

Command `(0,0,0)` for 20 s, policy owns all 29, sampled every 0.1 s. Net displacement from the first post-reset sample:

| t (s) | base dx (m) | base dy (m) | **base net drift (m)** | increment since 5 s ago (m) | head dx (m) | head dy (m) | **head net drift xy (m)** | head dz (m) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5 | +0.481 | -0.002 | **0.481** | — | +0.449 | -0.021 | **0.450** | -0.008 |
| 10 | +0.937 | +0.095 | **0.942** | 0.467 | +0.898 | +0.068 | **0.900** | -0.007 |
| 15 | +1.319 | +0.272 | **1.347** | 0.421 | +1.300 | +0.251 | **1.324** | -0.012 |
| 20 | +1.655 | +0.525 | **1.736** | 0.421 | +1.662 | +0.472 | **1.727** | +0.006 |

- Drift rate (net / 20 s): **0.087 m/s**
- Per-5-s figure: **0.481 m at 5 s**, vs the known **0.475 m / 5 s** — it persists.
- Head movement at 20 s: **1.727 m**, and head yaw drifted **+66.3°**.
- Max tilt 8.5°, no fall, base z 0.714–0.758 m.

**C3b verdict: FAIL.** Drift is ~0.48 m per 5 s, two orders of magnitude over the 2 cm / 20 s freeze budget. Superhot-style freeze cannot be built on this zero-velocity behavior. The head — the thing Superhot actually clocks — moves just as far as the pelvis.

## 4. C3c — waist ownership [MEASURED]

Two configurations:
- **A:** policy owns all 29 joints. No independent look; the only way to aim the head is a base-yaw command (`wz`) that turns the whole robot.
- **B:** adapter overrides `waist_yaw_joint` with a rate-limited target (2 rad/s), policy keeps legs and arms, waist roll/pitch left to the policy.

### 4.1 Walk quality, 10 s at `(0.5,0,0)`

| Config | path | forward / lateral (initial frame) | heading change | max tilt | torso pitch std | fall |
|---|---:|---:|---:|---:|---:|---|
| A, policy all 29 | 5.184 m (0.518 m/s) | -0.346 / +5.172 m | **+174.5°** | 9.9° | 2.61° | no |
| B, waist clamped at 0 | 4.990 m (0.502 m/s) | -0.645 / +4.978 m | **-166.7°** | 9.0° | 1.13° | no |

Both circle. Clamping waist yaw to zero slightly reduces torso pitch oscillation (2.61° → 1.13°) but does not fix the policy’s heading divergence. **[ASSUMPTION]** This is a property of the policy-on-this-model, not of the waist override alone; the spike did not isolate the cause.

### 4.2 +30° look while walking

| Config | achieved head yaw | head relative to base | waist actual | forward net | path speed | tilt / fall |
|---|---:|---:|---:|---:|---:|---|
| A (base-yaw command only) | **+42.1° travel**, 44.7° absolute | **-6.7°** (no independent look) | policy waist, -6.6° end, 22.6° max | +5.739 m | 0.821 m/s | 12.4°, no fall |
| B (waist adapter +30°) | **+40.9° travel**, 44.5° absolute | **+34.3°** | **+34.9°** end, +36.1° max | +0.924 m | **0.093 m/s** | 10.5°, no fall |
| B + arms on neutral DLS IK | n/a (fell at 2.5 s) | n/a | +25.8° at fall | +2.189 m | — | **FALL at 2.5 s**, tilt 100.3° |

- In A, the look “works” only by rotating the whole base: heading changed ~+51°, so the robot is not looking independently. The request overshot to +42° of head travel.
- In B, the waist-held arc is real: **+34.3° head relative to the base** while the base itself swings (it reached -178° mid-run and +10° at the end). The cost is translation: forward speed falls to **0.093 m/s, 18.5% of the 0.5 m/s command** (from 0.502 m/s in the B walk). Torso pitch std rises 1.13° → 1.57°, max tilt 9.0° → 10.5°, still no fall in 10 s.
- Adding the required arm IK on top of B is fatal: **fall at 2.5 s**. At 2 s the body is already at 34.7° tilt and z 0.651 m; by 3 s z is 0.121 m.

### 4.3 Recommendation: **A**

Keep the waist on the policy for v1. Numbers behind the call:

- B buys **+34.3°** of independent head arc.
- B loses **82% of forward speed** during the look (0.502 → 0.093 m/s) and **falls at 2.5 s** once the arm IK required by the design is enabled.
- A cannot decouple head from torso (relative yaw stayed **-6.7°** when asked for +30°); it only turns the whole body (+51° heading change, 8.21 m path in 10 s). But A stayed upright in every walking and walking-plus-look run.
- Neither option is a clean pass: A has no independent look, B is not compatible with the arm IK. Do not claim both work. If independent look is mandatory, B is the only measured mechanism and the arm controller must be redesigned first; that is outside this spike.

**C3c verdict: ALLOWED-FAIL** — trade recorded, A kept, independent look not delivered.

## 5. What this means for the design

- The C3a port is real and exactly reproduces the archived recipe, so the ONNX and environment are not the problem.
- The walking policy on Menagerie `scene_mjx.xml` has a large **heading drift even at zero command** and curves under a straight command. Treat “0.516 m/s tracked” as an x-component only.
- **Arms on DLS IK break it**: fall at 25.6 s while walking, 3× zero-command creep, reversed heading drift.
- **C3b is decisive**: 0.481 m / 5 s and 1.727 m of head movement in 20 s. The freeze that makes a 5–20 s brain legal does not survive this policy. Per design §14, the game choice should be reopened.
- HMD height for the policy pose is **~1.17 m**, not E3’s 1.221 m. Update any `hmd_z_offset` assumption by about **-5 cm**.

## 6. Honesty notes

- **Cause of the heading drift is not isolated.** [ASSUMPTION] The policy was trained against Playground’s feet-only flat-terrain MJX XML; running it on Menagerie `scene_mjx.xml` changes contacts/actuator details and may explain the yaw drift. This spike did not retrain, tune, or substitute the model, per instructions. Do not treat the cause as proven.
- “No fall” is not “walks straight.” The 30 s baseline stayed upright while circling through 360°.
- C3c comparisons inherit the policy’s heading drift, so “walk quality degradation” is measured against an already-curving baseline. The B-look speed collapse (0.502 → 0.093 m/s) and the B+arms fall are large enough to be unambiguous despite that.
- All masses/heights/positions are from the post-step `mjData`; the head point is the `head_link` mesh geom on `torso_link`, matching E3. Metrics are sampled every 0.1 s of sim time; fall is `z_end < 0.6*z0` or sampled tilt > 60°, matching the archived recipe.
- No files outside `spike/c3/` and this results file were modified. No NixOS, Monado, Steam, Proton, or game processes were touched.

## 7. Artifacts

- `spike/c3/measure_c3.py` — measurement harness (policy, arm DLS, waist override, CSV/JSON logging)
- `spike/c3/policy/g1_policy.onnx` — official checkpoint, SHA-256 above
- `spike/c3/policy/play_g1_joystick.py` — official loader, fetched for reference
- `spike/c3/logs/c3a_*.csv/json` — baseline, reproduction, arms, head height
- `spike/c3/logs/c3b_zero20.csv/json` — 20 s zero-command drift
- `spike/c3/logs/c3c_*.csv/json` — A/B walk and look runs
- `spike/c3/measurements.json` — aggregate metrics for all runs
