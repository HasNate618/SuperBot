# vr-bot — state of findings, for solution research

**Date:** 2026-10-23
**Purpose:** one shared ground-truth file so no researcher re-derives E1–E3 or C3. Read this before anything else. It states the goal *with no caveats*, what is proven, what is broken and why, and the pass criteria any proposed solution must meet.

**Claim tags.** `MEASURED` = a local spike on this machine, file named. `CITED` = source URL. `ASSUMPTION` = reasoning, untested.

---

## 0. The goal, stated without softening

A simulated **Unitree G1** in MuJoCo plays **Superhot VR**, rendered by the real game, with no headset and no controllers. It must be capable of:

1. **Walking** — move between positions in the level under its own balance.
2. **Dodging** — avoid incoming fire while staying upright.
3. **Aiming** — independently position its head and hands at a target.
4. **Completing a stage** — clear at least one stage with its own body.

Everything the game sees comes from the simulation: the emulated HMD pose is the robot's *achieved* torso pose, the emulated controller poses are its *achieved* wrist poses, and the robot's only camera is a capture of the game window. No faked poses, no game telemetry as control, no floating hands.

The project exists as a hiring artifact for **real-time controls and systems integration on a humanoid** (Apptronik / Figure / Agility / NVIDIA-shaped internships). The claim that must be true is **balance-constrained whole-body control with an OpenXR runtime as the task interface**. The demo may look clumsy. It may not be dishonest.

---

## 1. What is already proven working (do not redo)

### 1.1 The OpenXR injection pipe — E1, E2 `MEASURED`
- Monado's `remote` driver exists in the installed build and accepts a client over **TCP 4242**. Protocol magic `mndrmt3\0`, **376-byte** `r_remote_data` packets. One client, hub binds `0.0.0.0`, **last pose freezes on disconnect**.
- `P_OVERRIDE_ACTIVE_CONFIG=remote` is sufficient; **no `config_v0.json` is needed**. `IPC_EXIT_ON_DISCONNECT=off`, `XRT_NO_STDIN=1`.
- `xrLocateViews` returned tracked poses at ±35° and 0.5 Hz in the spike. No SteamVR, no custom OpenVR driver.
- Compositor must run with `XRT_COMPOSITOR_COMPUTE=false` (GRAPHICS queue) on this RTX 4060 / Wayland box. The compute compositor **and** `XRT_DEBUG_GUI=1` both SIGSEGV in the Wayland presentation queue-family check. `VK_DRIVER_FILES=/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.json`.
- Runtime: `/nix/store/89h2h18iz10aad69jpa2wvxmlhlg6k5i-monado-psvr2-psvr2-work-steamvr-2026-06-04` (Beyley PSVR2 fork).

### 1.2 Frame capture — E2 `MEASURED`
- Working capture is **grim on the Monado window**: **36.9 fps**, window located via `hyprctl clients -j`, e.g. `grim -g "2624,341 1920x1080"`.
- **Pose → pixel latency 30–59 ms (mid 44 ms)**, of which ~27 ms is grim overhead.

### 1.3 The body→HMD bridge — E3 `MEASURED`
- G1 stepped at 500 Hz PD with a 50 Hz pose publisher: **1001 packets in 20.02 s**. Packet positions/quats matched `mjData` exactly after frame conversion (**max error 0.0**).
- HMD yaw tracked torso to **0.0108°**. `hello_xr` followed.
- Frame conversion: MuJoCo Z-up `wxyz` → OpenXR Y-up `xyzw`: `xr_x = -mj_y`, `xr_y = mj_z`, `xr_z = -mj_x`.
- **Stationary body** (stiff stand PD at the `stand` keyframe, arms on DLS IK, ±35° waist look): base excursion **≤ 3.08 cm x / 0.59 cm y over 20 s**. This is the only configuration that has ever held still.
- IK residuals on that body: **reach 4.3 cm, punch 13.5 cm**. Punch residual is a known limitation, not a success. **Wrist orientation error has never been measured.**
- HMD height: **1.221 m** in `stand`, **1.170 m** in the policy pose `knees_bent`.

### 1.4 The robot
- **29 actuated joints**: 12 leg, 3 waist, 14 arm. **No neck** — `head_link` is a rigid mesh inside `torso_link`. No camera. `left_shoulder_pitch` moves the EE 0.151 m.
- Waist range: yaw **±2.618 rad**, roll/pitch **±0.52 rad**. The waist is the only "neck" available.
- Menagerie vendored at `spike/e3/vendor/mujoco_menagerie/unitree_g1/`.

---

## 2. What is proven broken, and the root cause

All of §2 was measured against the official MuJoCo Playground G1 walking policy (ONNX, sha256 `db2eb258494c1297c43d2b9ffa94cdbde97654c2a44cbab0b40fd4b990752a5b`, 903,543 B) on the **correct** collision model, `spike/c3/playground_g1/xmls/scene_mjx_feetonly_flat_terrain.xml`.

**Root cause, stated once: the policy is a 29-DoF joint-position *velocity tracker*. Its action space is the whole body, it owns every joint, and it was trained with no notion of an external upper-body authority. Every attempt to take joints back from it either destabilises the gait or is silently overridden by it.**

| Finding | Number | Root cause |
|---|---|---|
| Walks straight, briefly | **+0.23° / 10 s**, 7.45 m, 0.743 m/s, max tilt 4.8° | works |
| Does not stay straight | **−17.3° / 30 s** | no heading feedback in the policy |
| Arms: hard wrist IK while walking | **FALL 4.84 s** (neutral), **3.39 s** (reach) | policy uses arms for balance; overriding them removes its own stabiliser |
| Arms: hard wrist IK at **zero** command | **FALL 5.27 s** after 1.72 m base drift | not a speed problem — zero is already slower than any walk |
| Arms: soft 15% blend | survives 30 s but **wrist never moves** (0.119 m residual, local-x never advances) | policy dominates the blend; not shared authority |
| Look: waist override while walking | +34.6° arc achieved, but base does a **−185° U-turn in 10 s** | taking the waist costs the policy its own heading regulation |
| Look + arms while walking | **FALL 2.95 s** | combined destabilisation |
| Hold: best PI outer loop at zero command | **0.274 / 0.326 / 0.361 m at 20 / 60 / 120 s**, tilt 6.8°, **still stepping at 0.12 m/s RMS** | the policy has no stop; a loop can bound drift but not motion |
| Walk → hold, gain-scheduled | **FALL 3.11–4.24 s** into the hold | no stopping behaviour to hand over to |
| Walk → static stand pose (any ramp 0.05–2.0 s, incl. a 2 s and a 3 s brake, and a `knees_bent` pose) | **FALL 0.64–1.49 s, all variants** | static PD has no balance feedback and the policy never stops |

Superseded / corrected along the way, so nobody re-inherits them:
- The archived "0.516 m/s verified" was the x-component of an **arc**; the arc was our **Menagerie** XML, not the policy. Same ONNX on the Playground XML curls **+0.23°/10 s** vs **+174.5°/10 s**. Model choice fixed it.
- The old C3a/C3c arm and waist results were measured on the wrong XML and were re-run on the right one; the conclusions got **worse**, not better.

---

## 3. Why this is a structural gap, not a tuning gap

Published whole-body humanoid systems do not bolt upper-body authority onto a locomotion policy. They train **one policy whose command space already includes the upper body**: HOVER/OmniH2O track head + two hands directly; HOMIE keeps **12 lower-body actions** and treats the upper body as a smooth, time-varying **disturbance it was trained to tolerate**; ExBody separates expressive upper body from stable lower body. Unitree's own `rt/arm_sdk` has the same failure recorded in the wild (forward lean, endless stepping).

`research/humanoid-command-space.md` already argued the correct native interface is a **masked, root-relative task-space command** (root velocity + yaw rate + height, head pose, two hand poses, closure, authority bitmask, TTL, termination) — not a verb enum. That interface is the right contract. What is missing is an **executor that can honour it**.

The general `BodyCommand` struct was never the expensive part. The expensive assumption was that a velocity-tracking locomotion checkpoint is a whole-body controller.

---

## 4. Hard requirements any solution must satisfy

A proposal is only useful if it meets **all** of these. Numbers are the pass bar; report failures as failures.

| # | Requirement | Pass bar | Why |
|---|---|---|---|
| R1 | **Arms and head are independently commandable** | wrist reaches a commanded point with residual **≤ 3 cm**, while the body walks and while it holds; wrist **orientation** error measured (never measured yet) | aiming, shooting, punching, grabbing |
| R2 | **Holds still well enough to freeze time** | net base excursion **≤ ~5 cm** and continuous motion low enough that Superhot's clock stays near zero, over a **20 s** think | the VLM is slow; Superhot only advances when the player moves |
| R3 | **Walks** | ≥ 5 m straight-ish (heading error bounded), ≥ 0.3 m/s, no fall in 60 s, recovers from a small push | traversal; and the whole hiring claim |
| R4 | **Transitions in and out of hold do not fall** | 5 consecutive walk→hold→walk cycles, no fall, position recovered | every transition tried so far falls |
| R5 | **Dodges** | a lateral or lean displacement of the *head* of order **10–25 cm** in < 500 ms, returning upright, without falling | Superhot's dodge is small; a waist/leg lean may be enough — needs in-game measurement of what the game actually requires |
| R6 | **Runs on this machine, live** | 50 Hz control loop with Superhot + Monado + MuJoCo resident on one **8 GB RTX 4060 laptop** (Wayland, NixOS, Steam+Proton+gamescope) | a 200 Hz oracle that cannot run beside the game is not the demo |
| R7 | **Honest** | every pose is `mjData` after `mj_step`; no game telemetry as control, no faked poses, no floating hands, no root teleport | the artifact's entire value |
| R8 | **Completes a stage** | at least one early Superhot stage cleared end-to-end by the body + the brain, recorded | the literal goal |

Unknowns that block honest answering and should be **measured, not assumed**:
- What Superhot's clock actually does as a function of player motion (linear? thresholded? deadband?). If a small deadband exists, R2 may be far cheaper than it looks.
- What hitboxes and dodge distances the game actually requires (R5).
- ~~Whether Superhot under Proton accepts a synthetic HMD at all~~ — **C0 ran 2026-10-24 and passed** (`research/c0-results.md`): the game holds a focused session on `Monado: Remote HMD` and its view tracks our poses (9× pixel-change signal over static). Still unmeasured: trigger input, the clock floor in play, and The Fall's 0.70 m pistol/exit against a ~0.20 m reach envelope.

---

## 5. Candidate assets that exist and have not been evaluated here

Not endorsements — a starting list for the researchers to check, verify and price.

**Learned whole-body / loco-manipulation controllers**
- **HOMIE / OpenHomie** — 12 lower-body actions, upper body as a trained disturbance; released ONNX (`homie/model_walk`, `homie/model_stand`) at `huggingface.co/datasets/Kashu7100/eden_policies`; upstream `github.com/InternRobotics/OpenHomie`; MuJoCo + Mink recipe at `github.com/cyugai/G1_AMO_control`; an mjlab port `github.com/Nagi-ovo/mjlab-homierl` with a `stand` mode (1/6 of the command schedule) and an upper-body disturbance curriculum.
- **NVIDIA GEAR-SONIC / GR00T-WholeBodyControl** — released G1 ONNX encoder/decoder at `huggingface.co/nvidia/GEAR-SONIC`, 50 Hz, SMPL/G1/teleop inputs, **decoupled WBC (RL lower body + IK upper body)**; deployment is C++/TensorRT.
- **HOVER** (`github.com/NVlabs/HOVER`) — OmniH2O-mode specialist tracks head + hands; **train-it-yourself** in Isaac Lab, MuJoCo sim2sim provided.
- Others seen in the wild, unverified: Holosoma G1 29-DoF PPO, `humanoid_gpt`, HANDOFF, AnyBody, AMO, ExBody2, HugWBC, InternHumanoid.

**Classical control**
- Hierarchical QP whole-body control (cited at **0.42 ms on a 33-DoF humanoid**, IROS 2021) — CoM/contact/wrist/head priorities. Real robots do teleop this way.
- mink (already in `spike/e3/.venv`), pinocchio / crocoddyl / OCS2 as alternatives.
- Scripted gait + capture-point or LIPM balance; scripted ballistic dodge + PD recovery.

**Existing local assets**
- `spike/e1/remote_pose_client.py` — working remote-driver client.
- `spike/e2/` — capture + latency evidence.
- `spike/e3/body_bridge.py` — 500 Hz G1 stepper, 50 Hz pose publisher, DLS arm IK, waist look, scripted stand. **This is the fallback body and works.**
- `spike/c3/` — the Playground policy harness and every C3 measurement.
- Env: `spike/e3/.venv` (mujoco 3.17.0, mink 1.3.0, numpy 2.2.6, onnxruntime).

---

## 6. Rules for any researcher using this file

- Tag every claim `MEASURED` / `CITED` / `ASSUMPTION`. A number without a tag is not a finding.
- Prefer measuring to arguing. A one-hour MuJoCo experiment beats a paragraph of reasoning.
- A negative result is a deliverable. Never sweep until something survives and report only that; report the tuning window and its fragility.
- Do not assume a paper's number transfers. This project has already been bitten twice by inherited numbers and once by evaluating a policy on the wrong collision model before evaluating it on the right one.
- Do not edit the design doc, other research files, `spike/e1`, `spike/e2`, or `spike/e3`. Write new files.
- Do not touch NixOS, Monado systemd units, Steam, Proton, or Superhot.
- The goal is not negotiable down to "a turret." A proposal that cannot reach R1–R5 should say so plainly and price what it would take.
