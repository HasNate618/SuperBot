# C3f / C3g / C3h Legs Spike Results — arms while walking, integral hold, gain scheduling

**Spike:** C3f / C3c-retest / C3g / C3h (sim only, headless, CPU)
**Agent:** `c3fgh-legs`
**Date:** 2026-10-23
**Model:** Playground `scene_mjx_feetonly_flat_terrain.xml` (the correct model from C3e)
**Policy:** official `mujoco_playground` ONNX, SHA-256 `db2eb258494c1297c43d2b9ffa94cdbde97654c2a44cbab0b40fd4b990752a5b`, 903,543 bytes
**Script:** `spike/c3/measure_c3fgh.py`
**Data:** `spike/c3/c3fgh_measurements.json`; per-run logs `spike/c3/logs/c3f_*`, `c3c_G*`, `c3g_*`, `c3h_*` (pruned from the repository; regenerate by re-running)

All pose/speed numbers are **[MEASURED]** from `mjData` after `mj_step`; commands are never echoed back as achievements. The <2 cm hold target is the task's stretch goal, not a measured quantity. Nothing was retrained, neither XML was altered, and the design doc / other research files were not edited.

## 0. Verdicts

| Step | Verdict | One line |
|---|---|---|
| **C3f** | **FAIL** | On the correct model the body **cannot hold and move its arms while walking**. Hard wrist IK falls at **4.84 s** (neutral) and **3.39 s** (reach). A 15 % soft posture blend survives 30 s but **does not reach the target** (0.119 m residual; wrist local-x never advances the commanded +0.10 m). Only the no-arm baseline walks 30 s. |
| **C3c re-test** | **ALLOWED-FAIL** | Waist arc reproduces (+34.6°), but the base makes a **−185° U-turn**; arms+look still falls (2.95 s). The old “82 % speed loss” does **not** reproduce. |
| **C3g** | **ALLOWED-FAIL (partial)** | Combined vx+vy PI holds net creep to **0.274 / 0.326 / 0.361 m at 20 / 60 / 120 s** (14–18× the 2 cm target), bounded at 6.8° tilt, but still steps at 0.12 m/s RMS and loses the `kv_vx`/`ky` margins. |
| **C3h** | **FAIL** | The specified schedule falls **4.24 s** into the first hold (combined config: **3.11 s**). A tuned 0.75–1.25 s settle (and 2.0 s) survives vx-only, but still drifts ~0.5 m per hold; the combined config's window is only 0.75 s/2.0 s. |

## 1. Setup and what changed

Same recipe as C3a/C3e: `SIM_DT 0.002`, `CTRL_DT 0.02`, 10 substeps, `ACTION_SCALE 0.5`, `GAIT_FREQ 1.5`, `default_angles = knees_bent.qpos[7:]`, obs `[1,103]`, action `[1,29]`, adapter `command = [vx*1.5, vy*0.8, wz*2*pi]`. The policy always runs; C3d's two-mode body is not used.

`BodySim` layers optional arm and waist authority on top of the policy's 29-joint action:
- **policy** — no override (F0).
- **ik_hold (hard)** — both wrists held at the reset-time neutral pose, expressed in the moving `torso_link` frame, by damped-least-squares IK at 50 Hz, 2 rad/s joint rate limit (F1). This is the same DLS used by E3/C3a.
- **ik_hold + reach** — same, but the left wrist target adds +0.10 m along the torso +x axis with a cosine ease 0→1 over 1–3 s, then holds (F2). The achieved local-x confirms the direction: 0.094 → 0.193 m.
- **soft** — the same IK target is computed, but only **15 %** of the IK joint target is blended into the policy's arm ctrl (`ctrl = 0.85*policy + 0.15*q_ik`). This is a low-stiffness posture nudge, not an override (F3).

The 30 s baseline itself is a new measurement: C3e only ran 10 s.

### 1.1 The 30 s baseline is not perfectly straight [MEASURED]

| Window | dx | dy | path | heading travel |
|---:|---:|---:|---:|---:|
| 5 s | 3.58 m | −0.14 m | 3.58 m | −1.59° |
| 10 s | 7.43 m | −0.56 m | 7.45 m | **+0.23°** |
| 20 s | 15.00 m | −2.04 m | 15.14 m | **−7.96°** |
| 30 s | 22.25 m | −4.72 m | 22.75 m | **−17.28°** |

The +0.23° / 10 s result reproduces, but the policy slowly curves after that: −17.3° over 30 s. “Straight” is a 10 s statement, not a 30 s one. No outer loop was used in F0.

## 2. C3f — arms while walking (30 s at vx = 0.5) [MEASURED]

| Variant | Arms | Fall | Heading change | Forward speed | Path / dx | Max tilt | Torso pitch std | Max IK/task residual |
|---|---|---|---:|---:|---:|---:|---:|---:|
| **F0** | policy owns all 29 | **no** | **−17.28°** | **0.742 m/s** | 22.75 / 22.25 m | 4.92° | 0.72° | — |
| **F1** | hard DLS IK, neutral | **FALL 4.84 s** | −46.34° (to fall) | 0.451 m/s | 2.47 / 2.18 m | **56.77°** | 13.38° | 0.042 m |
| **F2** | hard DLS IK + left reach +0.10 m | **FALL 3.39 s** | −12.68° (to fall) | 0.435 m/s | 1.66 / 1.48 m | **51.87°** | 10.32° | 0.034 m |
| **F3** | soft 15 % blend, same reach | **no** | **+19.77°** | **0.751 m/s** | 22.60 / 22.53 m | 5.06° | 0.63° | **0.119 m** (target not reached) |

F1 and F2 both fall by collapsing forward/rolling (tilt 52–57°, base z 0.45 m) after a few seconds, not at the 30 s mark. The fall is **faster on the Playground model than the 25.6 s Menagerie result**; the old number does not survive the model fix.

F3 survives, but it is **not arm authority**: the left wrist's torso-local x stays 0.072–0.135 m and never advances by the commanded +0.10 m. The 0.119 m residual is the unachieved reach target. The 15 % blend lets the policy dominate the arms almost completely. A softer task that actually tracks the wrist was not found in this spike.

### 2.1 F1 at zero command, 20 s [MEASURED]

| Run | Result | Base drift at 5 s | Head drift at 5 s | Heading at fall |
|---|---|---:|---:|---:|
| F1-zero, hard neutral IK | **FALL at 5.27 s** | **1.72 m** | 1.79 m | −84.8° |

The old “3× creep” claim (5.29 m / 20 s on Menagerie) is replaced by: **on the correct model, hard arm IK at zero command falls at 5.27 s**, after 1.72 m of base drift in the first 5 s. There is no valid 20 s creep number for this configuration.

**C3f verdict: FAIL.** The body can walk (F0) but cannot hold or move its arms while walking. Hard IK is fatal within 3–5 s; the only surviving arm variant does not move the arms. The correct-model C3a/C3c arm results must be treated as newly negative, not inherited.

## 3. C3c re-test on the correct model (10 s at vx = 0.5) [MEASURED]

The old C3c option B (`waist_yaw` overridden to +30° while walking) was measured on Menagerie. Re-run on the Playground model, arms policy (G1) and arms hard IK (G2):

| Config | Arms | Fall | Achieved head-rel-base | Waist actual (end / max) | Base yaw travel | Forward speed | Path speed | Max tilt |
|---|---|---|---:|---:|---:|---:|---:|---:|
| **G1** | policy | **no** | **+34.6° end** (max 38.2°) | +35.6° / 38.7° | **−185.0°** | **−0.177 m/s** | 0.370 m/s | 15.3° |
| **G2** | hard DLS IK | **FALL 2.95 s** | +29.2° (to fall) | +30.4° / 33.5° | −59.3° (to fall) | 0.440 m/s | 0.628 m/s | 53.7° |

“Base yaw travel” is the net change in the base quaternion yaw (`base_yaw_travel_deg` in the JSON); “heading travel” elsewhere is the head yaw, which differs when the waist is overridden.

### 3.1 What reproduces and what does not

- **The waist arc reproduces.** The head-relative-to-base yaw reaches **+34.6°** (old Menagerie: +34.3°), with the actual waist at +35.6° (old: +34.9°). The actuator overshoots the +30° target under load, identically to the old run.
- **The arms-plus-look fall reproduces.** G2 falls at **2.95 s** (old: 2.5 s). That part was not a Menagerie artifact.
- **The “82 % speed loss” does not reproduce.** On the Playground model G1 still travels **3.70 m in 10 s (0.370 m/s path, 49 % of the 0.758 m/s no-look baseline)**, not 0.093 m/s. But the base yaw travels **−185°**: the robot makes a U-turn while walking. The forward component in the initial frame is negative (−0.177 m/s) because it ends up facing backwards. The old 0.093 m/s was a Menagerie-specific stall; the correct-model failure mode is a **base-yaw divergence**, not a stall.

**C3c re-test verdict: ALLOWED-FAIL.** The +34° waist arc is real, but it is **not an independent look**: overriding the waist costs the policy its heading regulation, and the base turns ~185° in 10 s. Adding arm IK still falls (2.95 s). The design conclusion “no independent look while walking” survives the model fix; the supporting number is the U-turn, not 82 % speed loss.



## 4. C3g — PI station-keeping at zero command [MEASURED]

The loop is an **outer controller around the walking policy**, not a new mode. The policy still owns all 29 joints; the loop only replaces the external `(vx, vy, wz)` command while the robot is supposed to hold:

```
vx_cmd = clamp(kv_vx*(0 - vx_meas) + I_vx, -1.0, +1.0)      # m/s
vy_cmd = clamp(kv_vy*(0 - vy_meas) + I_vy, -1.0, +1.0)      # m/s
wz_cmd = clamp(ky*(yaw_ref - yaw) + I_yaw, -0.6, +0.6)      # rad/s
I_vx += ki_vx*(0 - vx_meas)*dt, etc.                        # dt = 0.02 s
```

Output clamp ±1.0 m/s / ±0.6 rad/s; integrator clamp ±0.20 m/s / ±0.40 rad/s; conditional anti-windup plus a hard clamp; integrators reset on every engage. The P gains are the previously stable ones from C3e-2 (`ky=4`, `kv=1`). `yaw_ref` is captured at engage, so the loop holds the heading the robot had, not world zero.

### 4.1 The full vy loop falls; a small vy loop works

| Probe (20 s, zero command) | Fall | Net creep | Forward | Lateral | Heading | Max tilt | Last-5 s speed RMS |
|---|---:|---:|---:|---:|---:|---:|---:|
| P-only, `kv_vy=0` (control) | no | 1.341 m | +1.341 m | 0.018 m | +0.1° | 5.4° | 0.133 m/s |
| yaw integral only, `ki_yaw=0.1` | no | 1.379 m | +1.376 m | 0.086 m | −0.6° | 6.5° | 0.143 m/s |
| **P-only vy loop, `kv_vy=1.0`** | **FALL 5.74 s** | 1.705 m | +1.691 m | 0.216 m | +77.7° | **60.3°** | 0.501 m/s |
| P-only vy loop, `kv_vy=0.2` | no | 1.356 m | +1.345 m | 0.168 m | −0.2° | 5.9° | 0.135 m/s |

- **A full vy P loop is fatal, but the channel is not unusable.** `kv_vy=1.0` falls at 5.74 s; P-only `kv_vy=0.2` survives but is no better than P-only (1.356 m vs 1.341 m). The stable region is a **small vy gain plus a small vy integral** (next section), not the full gain.
- **The yaw integral does not help.** With the P yaw loop already holding heading travel to ~0.1° (end heading 2.5°), adding `ki_yaw=0.1` slightly increases net creep (1.379 m) and does not reduce heading error.
- The P-only control reproduces C3e-2's 1.344 m / 20 s to 3 mm (1.341 m here), so the loop implementation matches the earlier spike.

### 4.2 vx-only sweep (20 s, `ky=4`, `kv_vx=1`, `kv_vy=0`) [MEASURED]

| `ki_vx` | Fall | Net creep | Max drift | Forward | Lateral | Heading travel | Max tilt | Last-5 s speed RMS |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 0.00 | no | 1.341 m | 1.341 m | +1.341 m | 0.018 m | +0.1° | 5.4° | 0.133 m/s |
| 0.05 | no | 1.087 m | 1.087 m | +1.081 m | 0.106 m | +0.8° | 6.8° | 0.133 m/s |
| 0.10 | no | 0.998 m | 0.998 m | +0.959 m | 0.276 m | −0.0° | 7.2° | 0.147 m/s |
| **0.20** | no | **1.653 m** | 1.752 m | +1.546 m | 0.585 m | **−36.2°** | **27.6°** | **0.400 m/s** |
| 0.30 | no | 0.518 m | 0.519 m | +0.468 m | 0.222 m | +1.4° | 7.5° | 0.134 m/s |
| 0.50 | no | 0.397 m | 0.400 m | +0.300 m | 0.260 m | +1.3° | 7.5° | 0.133 m/s |
| **1.00** | no | **0.379 m** | 0.390 m | +0.195 m | 0.325 m | +0.6° | 7.6° | 0.152 m/s |

The integral **does** cut forward creep: 1.341 m → 0.195 m at `ki_vx=1.0` (85 %). But the response is **non-monotonic** (`ki=0.20` is worse than P-only, with a −36° heading excursion) and the residual moves into the **lateral** channel (0.325 m at 20 s). For the vx-only loop, the literal 20 s minimum is `ki=1.0`; `ki=0.5` is 5 % worse at 20 s but is the better engineering point.

### 4.3 Small vy sub-sweep and the combined config (20 s) [MEASURED]

At `ki_vx=0.5`, a small vy loop is stable and removes most of the lateral residual:

| `kv_vy` | `ki_vy` | Fall | Net creep | Max drift | Forward | Lateral | Heading | Max tilt | Last-5 s speed RMS |
|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 0.10 | 0.10 | no | 0.354 m | 0.354 m | +0.290 m | 0.204 m | +0.9° | 7.3° | 0.138 m/s |
| **0.15** | 0.10 | no | **0.266 m** | 0.275 m | +0.233 m | 0.130 m | −0.2° | 6.3° | 0.120 m/s |
| 0.20 | 0.05 | no | 0.283 m | 0.289 m | +0.234 m | 0.159 m | −0.1° | 6.3° | 0.122 m/s |
| 0.20 | 0.10 | no | 0.282 m | 0.291 m | +0.238 m | 0.153 m | −0.1° | 6.5° | 0.122 m/s |
| **0.20** | **0.15** | no | **0.274 m** | 0.282 m | +0.235 m | 0.142 m | −0.1° | 6.5° | 0.121 m/s |
| 0.25 | 0.10 | no | 0.286 m | 0.291 m | +0.237 m | 0.161 m | −0.1° | 6.4° | 0.121 m/s |
| 0.30 | 0.10 | no | 0.291 m | 0.291 m | +0.232 m | 0.176 m | −0.0° | 5.9° | 0.116 m/s |

The **combined config** carried forward is `ki_vx=0.5, kv_vy=0.2, ki_vy=0.15` (bold row): net **0.274 m**, forward 0.235 m, lateral 0.142 m at 20 s, with 6.5° tilt and 0.121 m/s last-5 s RMS. It is not the literal minimum (`kv_vy=0.15` is 0.266 m), but it is the best point that keeps the long-run stability and margins below.

### 4.4 60 s and 120 s runs [MEASURED]

| Config | Duration | Fall | Net creep | Max drift | Forward | Lateral | Max tilt | Last-5 s speed RMS |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| combined | 20 s | no | 0.274 m | 0.282 m | +0.235 m | 0.142 m | 6.5° | 0.121 m/s |
| combined | 60 s | no | **0.326 m** | 0.343 m | +0.215 m | 0.245 m | 6.8° | 0.120 m/s |
| combined | 120 s | no | **0.361 m** | 0.370 m | +0.190 m | 0.307 m | 6.8° | 0.123 m/s |
| vx-only `ki=0.5` | 60 s | no | 1.180 m | 1.187 m | +0.327 m | 1.134 m | 7.7° | 0.140 m/s |
| vx-only `ki=0.5` | 120 s | no | 2.869 m | 2.869 m | +1.980 m | 2.076 m | 21.6° | 0.444 m/s |
| P-only | 60 s | **FALL 58.32 s** | 5.691 m | 5.691 m | +5.669 m | 0.502 m | **50.1°** | 0.616 m/s |

The combined loop is **bounded**: 0.274 → 0.326 → 0.361 m at 20/60/120 s, with tilt staying at 6.8°. The vx-only loop is not: it grows to 2.869 m and 21.6° tilt at 120 s. So the small vy loop (P + integral) is what keeps the hold bounded long-run. But the last-5 s speed RMS is still **0.12 m/s**: the robot is stepping in place, not standing still, and the net excursion is still 18× the 2 cm target at 120 s.

### 4.5 One-step margins [MEASURED]

| Config | double `ki` | double `kv_vx` | double `kv_vy` | double `ky` |
|---|---|---|---|---|
| vx-only `ki=0.5` | survives 20 s (0.379 m) | **FALL 7.23 s** | — | **FALL 2.35 s** |
| vx-only `ki=1.0` | **FALL 11.08 s** | **FALL 11.56 s** | — | **FALL 2.36 s** |
| **combined** | survives 20 s (0.387 m, 13.8° tilt) | **FALL 5.73 s** | survives 20 s (0.312 m) | **FALL 2.39 s** |

The combined config retains the **`ki`** and **`kv_vy`** margins but loses **`kv_vx`** and **`ky`**. The vx-only `ki=1.0` has no margin at all. This is a tuned point, not a robust controller, but it is substantially more robust than the vx-only loop.

**C3g verdict: ALLOWED-FAIL (partial).** The PI loop is real and large: it cuts forward creep 85 % at 20 s, and the combined vx+vy loop holds net creep to **0.274 m / 0.326 m / 0.361 m at 20 / 60 / 120 s** (14–18× the 2 cm target), bounded, with 6.8° tilt. It does **not** meet the <2 cm hold target, the steady-state speed RMS is still 0.12 m/s (stepping in place), and it loses the `kv_vx` and `ky` margins. **Do not ship this as a station-keeping controller.**

## 5. C3h — gain-scheduled loop instead of a mode switch [MEASURED]

The schedule is the same walk→hold→walk program, with the policy running throughout and the outer loop engaged **only** during the hold segment. There is no policy swap and no park pose; the gains are identical in walk and hold. The switch is driven by the external command (`|vx_cmd|<0.05`, `|vy_cmd|<0.05`, `|wz_cmd|<0.05`), and the integrators reset on engage. The C3d mode-switch baseline fell **0.64–1.49 s** after park started (walk→park, every variant); the question is whether this schedule avoids that fall.

### 5.1 The specified schedule (no settle) [MEASURED]

| Run | Arms | Fall | Hold entry speed | Forward speed (whole run) | Heading travel | Max tilt | Max IK residual |
|---|---|---|---:|---:|---:|---:|---:|
| **H1** walk10 → hold20 → walk10 | policy | **FALL 4.24 s into hold** | 0.78 m/s | 0.633 m/s | +86.8° | **59.9°** | — |
| **H2** ×3 | policy | **FALL 4.24 s into hold** | 0.78 m/s | 0.633 m/s | +86.8° | **59.9°** | — |
| **H3** H1 + arms | policy | **FALL 4.24 s into hold** | 0.78 m/s | 0.633 m/s | +86.8° | **59.9°** | — |
| H1/H2 | soft 15 % | **FALL 8.46 s into hold** | 0.78 m/s | 0.541 m/s | +31.7° | 57.3° | 0.116 m |
| H3 (reach) | soft 15 % | no | 0.77 m/s | 0.277 m/s | **+160.6°** | 21.7° | 0.177 m |
| H3 (reach) | hard IK | **FALL 3.39 s (walk)** | — | 0.435 m/s | −12.7° | 51.9° | 0.034 m |

H3 with policy arms is identical to H1 (the policy already owns all 29 joints); the independent-arm H3 evidence is the soft and hard-IK lines.

The **best steady-state C3g config** (combined vx+vy) is **worse at the transition**: H1 with those gains falls **3.11 s** into the hold (forward 0.662 m/s, heading +65.9° during the hold / +74.3° whole run, tilt 59.4°). Better holding does not mean a better walk→hold handoff.

The hold does **not** stop the robot. At the boundary the speed is 0.78 m/s; 0.5 s later it is still 0.52 m/s; the net forward speed over the whole run is 0.63 m/s, and the heading diverges **+66.4° during the 4.24 s hold** (+86.8° over the whole run) before the fall at 59.9° tilt. This is the same walk→static failure as C3d, only **delayed from ~1 s to 4.2 s**. The command step at the boundary is 1.28 m/s (from +0.5 to −0.78), so the "no discontinuity" claim is false.

The soft-arm H3 run is the one exception, and it is not a success: it survives 40 s but turns **+160.6°**, ends at 21.7° tilt, and its IK residual is 0.177 m (the +0.10 m reach was not achieved). Hard IK falls during the **walk** segment at 3.39 s, reproducing C3f F2.

### 5.2 A tuned zero-command settle before engaging [MEASURED, TUNED]

A short **zero-command walk** segment before the hold lets the policy slow down without swapping controllers. This is not part of the specified H1; it is an exploratory variant, and the whole window is reported:

| Settle before hold | Fall | End time | Net forward speed (whole run) | Heading travel | Max tilt |
|---:|---|---:|---:|---:|---:|
| 0.0 s | **FALL** | 14.24 s (4.24 s into hold) | 0.633 m/s | +86.8° | 59.9° |
| 0.5 s | **FALL** | 12.78 s (2.28 s into hold) | 0.709 m/s | −10.8° | 60.4° |
| **0.75 s** | no | 40.75 s | 0.377 m/s | +1.5° | 7.4° |
| **1.0 s** | no | 41.00 s | 0.379 m/s | −8.3° | 7.6° |
| **1.25 s** | no | 41.25 s | 0.379 m/s | −6.1° | 14.3° |
| 1.5 s | **FALL** | 17.94 s (6.44 s into hold) | 0.509 m/s | −64.7° | 48.1° |
| 2.0 s | no | 42.00 s | 0.380 m/s | +9.8° | 15.2° |

The safe window is **0.75–1.25 s**, with 2.0 s also surviving but at 15° tilt; 0.5 s and 1.5 s fall. That is a narrow, non-monotonic tuning artifact, not a design. With the 1.0 s settle, H2 ×3 survives 123 s (max tilt 12.7° sampled, 14.5° at 500 Hz; heading −22.6° total), and the three hold segments move **0.42–0.51 m net / 0.40–0.53 m head displacement** each. The hold is still not stationary.

The **combined** C3g config narrows the window further: 0.75 s survives (hold net 0.277 m, 6.8° tilt) and 2.0 s survives (hold net 0.284 m, 7.1°), but 1.0 s, 1.25 s, and 1.5 s all fall (8.03 s, 4.59 s, and 8.78 s into the hold). The vx-only config is therefore the one used for the settle sweep: the combined config is better at holding but worse at the transition. There is no config that is best at both.

Adding arms to the tuned schedule does **not** help: soft arms fall 5.36 s into the hold (+101.0° during the hold, +126.9° whole run, 55.3° tilt); hard IK falls during the walk at 3.39 s. There is no arm variant that both survives and reaches.

**C3h verdict: FAIL.** The specified gain-scheduled loop falls **4.24 s** into the first hold — later than C3d's 0.64–1.49 s, but the same failure. The better-holding combined config falls even earlier (**3.11 s**) and its settle window is narrower (0.75 s and 2.0 s only). A 0.75–1.25 s zero-command settle makes the vx-only H1/H2 survive, but the window is narrow and non-monotonic, the hold still drifts ~0.5 m per 20 s and moves at 0.14 m/s RMS, and arms make it worse. The schedule **delays** the walk→static failure; it does not remove it.

## 6. What the design should now say

1. **No independent arm control while walking.** C3f: hard wrist IK falls in 3–5 s; the 15 % soft blend survives but does not move the wrist (+0.10 m commanded, 0.119 m residual, local-x never advances). The arms must stay policy-owned. Any "hold the controller / reach for the pistol" arm pose during locomotion is not supported by this body.
2. **No independent look while walking.** C3c re-test: the +34.6° waist arc is real, but overriding the waist costs heading regulation — the base makes a **−185° U-turn in 10 s**. Adding arm IK falls in 2.95 s. The old "82 % speed loss" number does not reproduce; the failure is base-yaw divergence.
3. **No outer-loop freeze.** C3g: a combined vx+vy PI holds net creep to **0.274 / 0.326 / 0.361 m at 20 / 60 / 120 s** (14–18× the 2 cm target), bounded at 6.8° tilt, but still steps at 0.12 m/s RMS and loses the `kv_vx` and `ky` margins. C3h: engaging it from a 0.78 m/s walk falls **3.11 s** later (vx-only: 4.24 s). The <2 cm hold target is not reachable with this policy and an outer loop.
4. **Walk→static is the hard transition.** C3d (mode switch) and C3h (scheduled loop) both fail; the surviving C3h variants need a tuned zero-command settle (0.75–1.25 s for vx-only, only 0.75 s/2.0 s for the better-holding combined config) and still drift ~0.3–0.5 m per hold. If the experience needs "freeze in place," it must be a **policy-level behaviour or a balance controller**, not a command override or a frozen pose.
5. **The design doc's §7 / §18.3–18.4 claims should be narrowed.** Specifically: "the body can hold and move its arms while walking" and "an outer loop can hold station at zero command" are not supported. The supported statement is: the policy walks at 0.74 m/s (10 s heading +0.23°), but it curves over 30 s (−17.3°); any hard independent arm/waist authority on top of it is fatal, and the soft arm blend is ineffective, while the best station-keeping loop is still 14–18× short of the 2 cm target.

### 6.1 Honesty notes

- **Tuned vs untuned.** The C3h settle sweep is explicitly tuned: 0.75–1.25 s and 2.0 s survive; 0.5 s and 1.5 s fall. The headline H1/H2/H3 result is the untuned schedule, and it fails.
- **Net creep vs max drift.** The 60 s PI runs can end near the start after a large excursion; the table reports both. For the vx-only runs, the 60 s `ki=0.5` net is 1.180 m and max drift 1.187 m; the 20 s `ki=0.2` net is 1.653 m and max 1.752 m. Neither is a 2 cm hold. The combined config is bounded (0.326 m net / 0.343 m max at 60 s) but still 16× the target.
- **Command ≠ achieved.** The C3g loop commands ±1.0 m/s but the policy does not track it; the measured velocity is from `local_linvel_pelvis` after `mj_step`. The C3h hold entry speed is measured, not the command.
- **The vy loop was tested at both scales, not omitted.** A full vy P loop (`kv_vy=1.0`) falls at 5.74 s; a small vy loop (`kv_vy=0.2`, `ki_vy=0.15`) is stable and cuts lateral drift. The main vx-only sweep is kept for comparability; the combined config is the best hold.
- **Max tilt is the 50 Hz sampled maximum.** The 500 Hz per-segment maximum can be 0.3–2.0° higher (e.g., F0 4.9° sampled vs 5.2° at 500 Hz; H2 ×3 12.7° vs 14.5°; the 1.25 s settle 14.3° vs 16.3°). Fall tilts are recorded at the triggering step, so the fall rows are exact.
- **No retraining, no XML changes, no design-doc edits.** The policy is the official ONNX; only the outer loop, arm IK, and schedule were added. All falls are reported as results, not hidden.
- **The soft-arm H3 survival is chaotic, not a capability.** It survives 40 s but spins +160.6° and does not reach; a different arm target changes the fall time from 4.24 s to 8.46 s. Do not cite it as "arms while walking works."

### 6.2 Artifacts

- `spike/c3/measure_c3fgh.py` — `BodySim`, `PIStation`, C3f/C3c/C3g/C3h runners.
- `spike/c3/c3fgh_measurements.json` — all summaries, switch edges, margins, settle sweep.
- `spike/c3/logs/c3f_*`, `c3c_G*`, `c3g_*`, `c3h_*` — per-run CSV/JSON.
