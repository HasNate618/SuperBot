# Humanoid command spaces — a general interface for a slow brain and a fast body

**Question.** How do published humanoid systems let a slow high-level writer (human, VLM, planner) command a whole-body controller without inventing a game-specific verb list? What should Clank's native command struct be, and how do Superhot's verbs become adapters over it?

**Evidence tags.** `CITED` = a source link; `MEASURED` = a local spike/measurement; `ASSUMPTION` = a design hypothesis. Synthesis sentences that aggregate cited table rows are not individually re-tagged.

**Answer.** Use a **masked, root-relative task-space command**, not a verb enum. The native struct is a fixed description of the robot's task state: root planar velocity + yaw rate + height, head pose, two hand poses, hand closure, bounded waist, optional gait/posture. An **authority bitmask** says which fields are live; a **TTL** says when the command expires; a **termination** says when a ballistic skill is done. Human VR, a VLM, and a planner all write the same struct. Game verbs are adapters that set the mask and generate a short target trajectory. The executor (IK/QP/RL) owns feasibility and returns achieved state. **v1 does not need a new learned System-1 model**; it needs this contract plus a fast executor.

---

## 1. Two interfaces, not one

The confusion in the Superhot design is that “punch” and “look” are treated as control modes. They are not. Separate the layers:

```
brain (VLM/planner, 0.05–0.2 Hz)
   └─ skill adapter (10–30 Hz)          # game- or task-specific
        └─ BodyCommand (task space)     # general, this document
             └─ executor (50 Hz)        # IK / QP / learned policy
                  └─ JointTarget (29 floats, 50 Hz)
                       └─ PD (500 Hz) → MuJoCo → achieved pose → OpenXR
```

- **`BodyCommand`** is the *general* interface. It says where the head/hands should be, how the base should move, and which parts of that request are authoritative.
- **`JointTarget`** is the *body-specific* interface. It is 29 joint angles for the G1, produced by the executor.
- **Game verbs never cross into the executor.** “Punch” is an adapter that writes a `BodyCommand` with the right hand authoritative and a 2 s termination. “Look” writes a head pose with a bounded waist-yaw share. The executor does not know what a punch is.

This is the same separation used by HOVER (a masked reference command over bodies/joints/root, with many modes in one policy) [CITED](https://arxiv.org/abs/2410.21229), OmniH2O (three tracked points, head and hands) [CITED](https://arxiv.org/html/2406.08858), and SEIKO (Cartesian commands → whole-body QP → position commands) [CITED](https://arxiv.org/html/2312.16465). The game-specific part is a thin adapter; the body sees a general task.

---

## 2. Systems and their command spaces

Dims are the published or code-derived command/observation sizes. “Hz” is the rate at which the command or policy is evaluated; where a paper does not publish it, the cell says so rather than inventing a number.

| System (robot) | Command space (fields / dims) | Hz | Writer | Executor | Pixels vs proprio | Source |
|---|---|---|---|---|---|---|
| **OmniH2O** (H1) | 3 tracked points: head + L/R hand; per point `(p̃−p, ṽ−v, p̃)` in root frame; 25-step proprio history. Exact flat length not tabulated. | **50** policy | Human VR (Vision Pro); also **VLM precedent**: GPT-4V emits 6 hand coordinates | Learned whole-body RL policy (teacher–student, DAgger) | Proprio history; no pixels in the controller | [paper](https://arxiv.org/html/2406.08858) |
| **HOVER** (H1 config) | Masked reference over **20 bodies (+3 extension parents), 19 joints, 7 root refs**; mask = body + joint + 7 root bits → **49 bits**; reference = `23×3 + 19 + 7 = 95` floats (code-derived; the command may use the same 23-body set). Root refs: `vx, vy, vz, roll, pitch, yaw_delta, height`. | **50** policy (`decimation 4 × dt 0.005`) | Human VR / motion planner; mode selects subset | Distilled masked RL policy; >15 modes incl. upper-body tracking + lower-body velocity | Proprio; reference motion, no pixels | [paper](https://arxiv.org/abs/2410.21229), [mask.py](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/core/neural_wbc/core/mask.py), [H1 cfg](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/inference_env/inference_env/neural_wbc_env_cfg_h1.py) |
| **HOMIE** (G1, GR-1) | `C_t = [v_x, ω_yaw, h]` (3) + `q_upper` (7-DoF per arm, 14) + glove finger angles (up to 15 DoF) | **50** loco; arms updated **10 Hz**, interpolated 50× | Human exoskeleton + pedals + gloves | `π_loco` RL policy for legs/height; upper joints set directly | Proprio; 30 Hz camera for feedback only | [paper](https://arxiv.org/html/2502.13013) |
| **ExBody2** (G1) | Local 3D keypoints (upper + lower body) + target root velocity/pose; action = **23-D joint targets** | **50** policy; **500** low-level | Human motion / monocular RGB / planner | RL student policy (teacher–student, DAgger) → PD | Proprio; optional monocular RGB for demo | [paper](https://arxiv.org/html/2412.13196) |
| **TWIST2** (G1-class) | `p_cmd = [v_x, v_y, z_root, roll, pitch, ω_yaw, q_1…q_N]`; **6 + N_joints** | PICO stream **100**; low-level rate not stated; DP chunks **64 ≈ 2 s** | Human PICO; high-level Diffusion Policy from RGB | One-stage RL motion tracker → desired joint positions → PD | Low level proprio; high-level DP uses RGB | [paper](https://arxiv.org/html/2511.02832) |
| **HugWBC** (H1) | `c_t = [v_t, b̃_t]`: **3-D task velocity + 9-D behavior block** — gait frequency, foot swing height, body height, body pitch, waist yaw, phase/clock terms. Upper body is a *separate intervention channel*, not in `c_t`. | **50** | Human / planner | One RL policy for whole body; external upper-body action override | Proprio; no pixels | [paper](https://arxiv.org/html/2502.03206) |
| **AnyBody** (G1) | **Arbitrary subset** of body keypoints; each body token = 15 frames × 3 = **45** + visibility; root-anchored. No fixed interface. | Not stated; **15 frames / 0.5 s** window (≈30 Hz equivalent; log-spaced) | Human VR / trajectory planner | Learned latent encoder–decoder; transformer keypoint encoder with masked self-attention | Proprio + keypoints; no pixels | [paper](https://arxiv.org/html/2606.29209) |
| **SONIC** (humanoid) | **Universal token `z`** from encoders for robot motion, human motion, or hybrid motion (upper-body keypoints + lower-body robot motion); token dim not stated | **50** policy; **500** command stream; **100** operator | Human motion / gamepad / planner | Control decoder → motor commands; motion decoder auxiliary | Proprio; no pixels | [paper](https://arxiv.org/html/2511.07820) |
| **LangWBC** (G1) | Natural language → CLIP embedding → CVAE latent `z`; no explicit vector | Not stated; CLIP reused across control steps | Human / LLM planner | Single end-to-end language-conditioned policy | Proprio; no pixels | [paper](https://arxiv.org/html/2504.21738) |
| **Open-TeleVision** (H1, GR-1) | `SE(3)` poses of head, both wrists, hand keypoints; 7 vectors for dexterous hand retargeting | **60** loop | Human VR (Apple Vision Pro) | Closed-loop IK (Pinocchio) + dex-retargeting → joint targets; no learned policy | Proprio for IK; stereo video feedback | [paper](https://arxiv.org/html/2407.01512) |
| **HumanPlus** (custom 33-DoF) | 19-D body target joint angles + target root velocities/roll/pitch/yaw velocity; hand joints (12) bypass to PD | **50** policy; **1000** PD; body retarget **25 fps** | Human RGB camera (WHAM); skill policy (25 Hz) | Decoder-only transformer policy → 19 joint setpoints → PD | Pixels for shadowing; proprio for control | [paper](https://arxiv.org/html/2406.10454) |
| **SEIKO** (Talos) | Cartesian poses/velocities for hands/feet + contact-force references + joint torque limits; retargeting QP and controller QP | Real-time (two QPs); exact rate not stated | Human teleoperation / planner | Two QPs: whole-body retargeting + flexibility-aware controller; position-controlled robot | Proprio/force sensing; no pixels | [paper](https://arxiv.org/html/2312.16465) |

### What the table shows

1. **There is no standard vector.** Dimensions range from HOMIE's 3+14 to HOVER's 95-D reference, to AnyBody's variable keypoint set. The commonality is not a fixed size; it is a **fixed schema with a mask**.
2. **The mask is the anti-verb device.** HOVER's `calculate_mask_length` is literally `num_bodies + num_joints + 7 root refs`; the command is `num_bodies×3 + num_joints + 7` [CITED](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/core/neural_wbc/core/mask.py). A skill chooses which elements are authoritative; it does not choose a new controller. This is exactly the generalization Clank needs.
3. **Two rate regimes.** Learned policies sit at **50 Hz** with PD at **500–1000 Hz** (HOVER, HOMIE, ExBody2, TWIST2, HugWBC, HumanPlus, OmniH2O; see rows). Teleop/IK loops sit at **60–100 Hz** (Open-TeleVision, TWIST2's PICO stream). Nothing credible runs a VLM in the motor loop.
4. **Root-relative is the norm.** OmniH2O, ExBody2, TWIST2, and HOVER all avoid absolute global pose; they use root-relative targets or root velocity/height. This is directly relevant to Clank, which has no reliable global odometry.
5. **Task-space vs joint-space is an executor choice, not an interface choice.** HOMIE and HumanPlus pass joint targets because they have an exoskeleton or a learned tracker; Open-TeleVision and SEIKO pass Cartesian targets and solve IK/QP. Both can sit under the same task-space contract.

---

## 3. Pattern table — copy, adapt, or reject

| Pattern | Representative systems | Verdict for Clank | Why |
|---|---|---|---|
| **3-point pose tracking** (head + hands) | OmniH2O, HOVER default, Open-TeleVision (head + wrists + hands), AnyBody | **Copy, adapt the head** | It is the minimal interface proven to work: OmniH2O's 3-point ablation is comparable to 22-point tracking [CITED](https://arxiv.org/html/2406.08858). But the G1 has **no neck**, so the “head pose” request must be executed as bounded waist yaw plus base yaw; roll/pitch will have a visible residual [MEASURED](../RESEARCH.md). AnyBody shows the longer-term generalization: arbitrary keypoint subsets, chosen at deploy time [CITED](https://arxiv.org/html/2606.29209). |
| **Velocity + upper-body pose** | HOMIE, ExBody2, HugWBC | **Copy** | This is the natural split for the G1: legs consume `[v_x, v_y, ω_yaw]` (+ height), arms/waist consume a pose target. HOMIE's `[v_x, ω_yaw, h] + q_upper` is the cleanest published version [CITED](https://arxiv.org/html/2502.13013); ExBody2 decouples velocity from local keypoints [CITED](https://arxiv.org/html/2412.13196); HugWBC adds behavior commands but keeps the upper body as an intervention channel [CITED](https://arxiv.org/html/2502.03206). |
| **Masked reference / authority** | HOVER | **Copy — this is the core schema idea** | A mask over bodies, joints, and root references expresses “hold”, “look”, “track hands”, “velocity only” without a verb enum. HOVER distills one policy over >15 modes [CITED](https://arxiv.org/abs/2410.21229). Clank should copy the *mask semantics*, not the 95-D H1 reference. |
| **Latent VLA / universal token** | SONIC, LangWBC, Helix, GR00T N1 | **Reject for the v1 interface; adapt only as an optional executor** | Opaque, needs a large motion corpus or demonstration data, and is not debuggable when the body is wrong. SONIC's universal token is impressive — one encoder–decoder accepts robot, human, and hybrid motion [CITED](https://arxiv.org/html/2511.07820) — but it is a *learned* command space. Clank's constraint is no VLA retrain and no Superhot corpus. Keep the token idea behind the same `BodyCommand` if a learned executor is added later. |
| **Language-skill ID** | LangWBC, SayCan, Hi Robot | **Reject as the body interface; copy above it** | Language is too slow (5–20 s brain) and ambiguous to be a 50 Hz command. LangWBC maps text directly to a CVAE latent and actions [CITED](https://arxiv.org/html/2504.21738); that is a learned skill policy, not a body contract. Use language to *select* a skill adapter, which then writes `BodyCommand`. |
| **QP task list** | SEIKO, pink/mink, classical WBC | **Copy for the arm/waist executor** | A prioritized task list (balance/contact > waist > hands > posture) with joint limits and velocity constraints handles the G1's redundancy and keeps the body honest. SEIKO solves whole-body retargeting and control as two QPs on a position-controlled humanoid [CITED](https://arxiv.org/html/2312.16465). Reject the multi-contact force-control part for v1: Clank has no force sensing and the game applies no forces. |
| **Action chunking** | ACT, π0, GR00T N1, TWIST2's Diffusion Policy | **Adapt for ballistic skills** | Predict/execute a timed chunk to hide writer latency and make smooth ballistic motion. TWIST2 predicts 64 chunks ≈ 2 s [CITED](https://arxiv.org/html/2511.02832). Clank should carry a duration/waypoints in the command; do not temporally ensemble across an impact. |
| **Joint setpoints as the contract** | HOMIE, HumanPlus, TWIST2 | **Adapt through the executor, do not expose as the contract** | Joint targets are simple and fast, but body-specific and require upstream retargeting. HOMIE can do it because it has an isomorphic exoskeleton [CITED](https://arxiv.org/html/2502.13013); Clank does not. Keep joint targets below the executor boundary. |

---

## 4. Proposed Clank interface

### 4.1 `BodyCommand` (task space, writer-agnostic)

```python
from dataclasses import dataclass
from typing import Literal

# Authority bits: the only way to select behaviour. No verb enum.
AUTH_ROOT_VEL    = 1 << 0   # v_xy, yaw_rate
AUTH_ROOT_HEIGHT = 1 << 1   # height
AUTH_HEAD        = 1 << 2   # head pose
AUTH_L_HAND      = 1 << 3
AUTH_R_HAND      = 1 << 4
AUTH_CLOSURE     = 1 << 5   # hand closure
AUTH_WAIST       = 1 << 6   # yaw, pitch, roll (bounded)
AUTH_GAIT        = 1 << 7   # frequency, foot swing height
AUTH_POSTURE     = 1 << 8   # optional body-specific joint offsets

@dataclass(frozen=True)
class Pose:
    p: tuple[float, float, float]          # metres, root-relative
    q: tuple[float, float, float, float]   # xyzw, root-relative

@dataclass(frozen=True)
class BodyCommand:
    schema: str = "clank.body/1"
    seq: int = 0
    t_mono_ns: int = 0
    ttl_ms: int = 200
    source: Literal["human", "vlm", "planner", "script"] = "script"
    authority: int = 0
    frame: Literal["root", "world"] = "root"

    # Base task
    v_xy: tuple[float, float] = (0.0, 0.0)   # m/s
    yaw_rate: float = 0.0                    # rad/s
    height: float | None = None              # m; None = executor default

    # Upper-body task
    head: Pose | None = None
    left_hand: Pose | None = None
    right_hand: Pose | None = None
    closure: tuple[float, float] = (0.0, 0.0)  # 0 open .. 1 closed
    waist: tuple[float, float, float] | None = None  # yaw, pitch, roll (bounded)

    # Optional behaviour / posture
    gait: tuple[float, float] | None = None  # frequency, foot swing height
    posture: tuple[float, ...] | None = None # body-specific, use sparingly

    # Execution semantics
    priority: int = 0
    preempt: bool = True                     # False = queue behind ballistic skill
    duration_ms: int = 0                     # 0 = until preempted
    done_when: Literal["duration", "target", "external"] = "duration"
    tolerance_m: float = 0.02
    max_joint_speed: float = 2.0             # rad/s
    max_hand_speed: float = 1.0              # m/s
```

The fixed fields are a task description, not a game description. A “punch” and a “reach” differ only in the hand target, the authority mask, the duration, and the termination — not in the type of command.

### 4.2 `BodyState` (feedback; achieved, never desired)

```python
@dataclass(frozen=True)
class BodyState:
    seq_ack: int
    t_mono_ns: int
    root_p: tuple[float, float, float]
    root_q: tuple[float, float, float, float]
    v_xy: tuple[float, float]
    yaw_rate: float
    q: tuple[float, ...]           # 29 joint positions
    qd: tuple[float, ...]          # 29 joint velocities
    head: Pose                    # achieved
    left_hand: Pose               # achieved
    right_hand: Pose              # achieved
    residual_head_m: float
    residual_left_m: float
    residual_right_m: float
    flags: int                    # SATURATED, TTL_EXPIRED, FALL_RISK, UNSUPPORTED
    supported: int                # authority bits this executor can honour now
```

**Honest-body rule:** `BodyState` reports what MuJoCo actually achieved after `mj_step`, not the desired IK target. This is the same rule E3 enforced: only achieved `torso_link`/`head_link` and wrist poses went to Monado [MEASURED](../research/e3-spike-results.md).

### 4.3 Semantics

- **Authority is the mode.** `authority` is a bitmask, not an enum. Unset fields are ignored, not zeroed. This is HOVER's mask idea reduced to the fields the G1 actually has [CITED](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/core/neural_wbc/core/mask.py).
- **Root-relative by default.** `head`/`left_hand`/`right_hand` are relative to the current root frame. No global odometry is required. `frame="world"` is an escape hatch for a planner that has a map; the executor converts it using `BodyState.root_p`.
- **TTL gates acceptance only.** A stale command is discarded on arrival. Once accepted, a ballistic skill runs to its own `done_when` termination. This is the edge-navigation pattern [CITED](system1-game-agents.md): latest-wins for continuous commands, queue for ballistic ones.
- **Duration, not a separate verb.** `duration_ms=0` means “until preempted” (hold, move, look). `duration_ms>0` means a timed chunk (punch, dodge, reach). A skill adapter decides which.
- **One writer at a time, with priority.** `priority` and `preempt` let a safety/cancel command override a skill. A non-preemptible ballistic skill queues the next command; it does not blend across impact.
- **Validation is cheap and strict.** Schema version, authority bits, finite numbers, joint/velocity limits, workspace box, and a balance envelope. Invalid → `hold`, and the invalid count goes back to the brain.
- **Executor owns feasibility.** The command is a request. The executor clamps, solves IK/QP, rate-limits, and reports `SATURATED`/`FALL_RISK`. The brain never assumes the request was achieved.

### 4.4 Why this is not a verb list

The same struct expresses non-game tasks without modification:

- **“Reach that point”** — `AUTH_R_HAND`, right-hand pose at the point, `duration_ms≈2000`, `done_when="target"`.
- **“Hold this pose”** — authority over the live pose fields, zero root velocity, `duration_ms=0`, active balance.
- **“Look at that person”** — `AUTH_HEAD + AUTH_WAIST`, head orientation, bounded waist yaw share.
- **“Walk to (x, y)”** — `AUTH_ROOT_VEL`, velocity from a planner's closed-loop controller using `BodyState.root_p`.
- **“Push that button”** — `AUTH_R_HAND + AUTH_CLOSURE`, hand pose + closure, short duration.
- **“Squat and hand me the box”** — `AUTH_ROOT_HEIGHT + AUTH_R_HAND`, height + hand pose.

None of these needs a new executor path. That is the test of a general interface: a new task is a new adapter, not a new controller.

---

## 5. Superhot as an adapter over `BodyCommand`

The game-facing layer is a **compiler from verbs to `BodyCommand`**. The body/executor never sees `hold`, `look`, `punch`, `dodge`, `move`, or `trigger`. Superhot is the **first adapter, not the schema**; the same adapter pattern applies to any VR title or non-game task.

| Superhot verb | Adapter emits | Authority | Termination | Status / notes |
|---|---|---|---|---|
| **hold** | `v_xy=(0,0)`, `yaw_rate=0`, `height=default`, head and hands held at the current achieved pose, waist held | `ROOT_VEL + ROOT_HEIGHT + HEAD + L_HAND + R_HAND + WAIST` | `duration_ms=0`; cancel on new command | **ASSUMPTION.** This must be an *active* hold (balance + pose hold), not a zero command. The learned G1 policy drifts **0.475 m in 5 s** at zero command [MEASURED](../../stubborn-gamer/SEED.md); E3's scripted stand held within 3.08 cm in x / 0.59 cm in y over 20 s with position actuators [MEASURED](../research/e3-spike-results.md). Superhot advances time only when the player moves [CITED](https://www.superhotgame.com/superhot-vr). |
| **look(yaw, pitch)** | Head pose with yaw split: bounded waist yaw (≈±35–40°) + remainder as `yaw_rate`; small pitch; hands hold | `HEAD + WAIST + ROOT_VEL` | duration 1–2 s | **CITED** for the 40° stepping threshold on HRP-2 [CITED](https://vigir.missouri.edu/~gdesouza/Research/Conference_CDs/IEEE_IROS_2009/papers/0305.pdf); **ASSUMPTION** for the G1 split and pitch limits. The G1 has no neck [MEASURED](../RESEARCH.md). |
| **punch(side, target)** | Hand pose at target + orientation; waist/chest lead; opposite hand holds; zero root velocity | `R_HAND or L_HAND + WAIST + HEAD` | duration ≈2 s, `done_when="duration"`; no preempt | **ASSUMPTION.** E3's punch phase was 2 s with 13.5 cm IK residual [MEASURED](../research/e3-spike-results.md); treat the residual as a known limitation, not a success. |
| **dodge(direction)** | Root velocity impulse (lateral/back), height dip, waist lean, head counter-look | `ROOT_VEL + ROOT_HEIGHT + WAIST + HEAD` | duration 0.5–1 s | **ASSUMPTION**, and **capability-gated**: a frozen locomotion policy may not support this; report `UNSUPPORTED` rather than pretending. |
| **move(v_xy, face)** | `v_xy` + head/waist gaze; hands hold | `ROOT_VEL + HEAD` | `duration_ms=0`; cancel on new command | **CITED** that VR locomotion is usually a 2D velocity command [CITED](../RESEARCH.md); **ASSUMPTION** for the exact mapping. Requires the Playground locomotion policy in the loop; until then, `UNSUPPORTED`. |
| **trigger** | Right-hand closure = 1, right hand pose held/aimed | `CLOSURE + R_HAND` | until release | **ASSUMPTION.** `trigger` is a game action, not a body skill. In the native struct it is hand closure + aim; the adapter maps that to the game's trigger input. The body never sees “trigger”. |
| **grab / release** | `closure` 1 / 0 on the relevant hand | `CLOSURE` | until changed | **ASSUMPTION.** Requires a hand model (`scene_with_hands.xml`) or a gripper abstraction. |
| **stand** | `v_xy=(0,0)`, height, optional posture | `ROOT_VEL + ROOT_HEIGHT + POSTURE` | `duration_ms=0` | **ASSUMPTION.** Same active-hold caveat as `hold`. |

**The important mapping decision:** `hold` is not “send zero”; it is “authority over the pose fields + active balance + zero velocity”. `look` is not a head joint command; it is a head pose request that the executor decomposes into waist yaw and base yaw. `punch` is not a special controller; it is a hand pose + duration + no-preempt. Every verb collapses to the same five concepts: **authority, target, duration, termination, priority**.

---

## 6. Does v1 need a learned System-1 model?

**No.** v1 needs a fast **executor**, not a new learned **model**.

- The 50 Hz control loop is System 1 in the control-theoretic sense. It can be classical: arm IK/QP, bounded waist, PD, and the existing legs policy. SEIKO (two QPs), Open-TeleVision (closed-loop IK), and HumanPlus's low-level PD path all consume task-space or joint targets without an end-to-end VLA [CITED](https://arxiv.org/html/2312.16465), [CITED](https://arxiv.org/html/2407.01512), [CITED](https://arxiv.org/html/2406.10454).
- The learned whole-body systems in the table (OmniH2O, HOVER, HOMIE, ExBody2, TWIST2) learn for **tracking robustness**, not because the command space requires learning. Their command spaces are still task-space or joint-space structures that a classical executor could, in principle, consume.
- Clank's constraints point the same way: no VLA retrain, no Superhot demonstration corpus, an 8 GB card, and an honest MuJoCo/PD path. A new learned System-1 would require motion data or RL and would not change the interface.
- The one learned component worth integrating is the **existing Playground locomotion policy** for the legs. **[ASSUMPTION]** Until it is in the loop, the executor should stand and report `velocity` as `UNSUPPORTED`.
- A learned System-1 becomes worthwhile only when the frozen policy + IK cannot meet the balance/tracking targets — for example, a dynamic dodge or a punch that requires whole-body counterweight. Even then, it should be swapped in **behind the same `BodyCommand`/`BodyState` interface**, not replace it.

The warning that motivates this: Unitree's own `rt/arm_sdk` bolts arm control onto a balance controller that is unaware of the arms, and integrators report forward lean, torso oscillation, and an infinite stepping loop when the arms are held out [CITED](https://roboticsknowledgebase.com/wiki/common-platforms/unitree-g1/), [CITED](https://github.com/unitreerobotics/unitree_sdk2_python/issues/173). The interface does not fix that; the executor does. Design the interface so the executor can be replaced without touching the game adapters.

---

## 7. What to build next (not Superhot-specific)

1. **Define and version the `BodyCommand`/`BodyState` schema.** Include the authority mask, TTL, priority/preempt, root-relative poses, and a strict validator. Make `BodyState` report achieved pose, residuals, saturation, and `supported` authority bits.
2. **Build the executor abstraction and a v1 implementation.** `Executor.apply(cmd) -> BodyState`: legs policy or stand, arm IK (E3 DLS first, then mink/pink) or QP, bounded waist-yaw split, PD at 500 Hz, rate limits. Log every residual and saturation.
3. **Build the skill-adapter layer as mask + target + duration + termination.** Start with `hold`, `look`, `reach`, `move`; add `strike` only after the first four are measured. No game verb names below this layer.
4. **Build a replay/teleop harness that drives `BodyCommand` without a game.** Feed recorded VR poses, scripted targets, and a slow VLM writer; measure tracking error, end-to-end latency, preemption, TTL expiry, and fall risk.
5. **Add capability and health feedback to the brain loop.** Per-authority support flags, balance envelope, residual, and a `hold` fallback. Latest-wins queue; the brain never blocks the 50 Hz loop.

---

## 8. Uncertainty and source status

- **HOVER dimensions are H1-specific.** The 49-bit mask and 95-float reference come from the H1 inference config (20 bodies + 3 extension parents, 19 joints, 7 root refs). The *formula* is general; the G1 numbers must be recomputed from its own body/joint lists [CITED](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/inference_env/inference_env/neural_wbc_env_cfg_h1.py).
- **Rates not published:** AnyBody, LangWBC, SEIKO, and TWIST2's low-level controller. The table says “not stated” rather than inventing a number. TWIST2's 100 Hz is the PICO motion stream, not the policy rate; its 64-action chunk is ≈2 s at the Diffusion Policy execution rate [CITED](https://arxiv.org/html/2511.02832).
- **Exact command lengths not tabulated:** OmniH2O's flat goal length, ExBody2's keypoint count, SONIC's token dimension, and HugWBC's exact `b̃_t` composition beyond “9-D behavior block” are not stated in the fetched text. The fields are verified; the exact flat widths are not.
- **HOVER's “>15 modes”** is taken from the paper/prior research; the exact mode list was not re-enumerated in this session.
- **G1 waist limits are not verified.** The ±35–40° yaw share is an HRP-2-derived heuristic, not a G1 specification. Measure `waist_yaw/pitch/roll` ranges in the loaded MJCF before hard-coding.
- **Superhot mappings are assumptions.** The game mechanic is cited, but the exact body mapping (punch duration, dodge feasibility, trigger-to-closure) is a design hypothesis to test, not a published result.
- **No HRL / “Hybrid Robotic Transformer” citation.** No verified paper by that name was found; do not cite it.
- **HOVER source access:** the raw `mask.py` fetch via crawl4ai returned 502, so the file was read through jsDelivr/curl; the GitHub blob URLs above were verified with HTTP 200.

---

## 9. Sources

- OmniH2O — [arXiv:2406.08858](https://arxiv.org/html/2406.08858)
- HOVER — [arXiv:2410.21229](https://arxiv.org/abs/2410.21229); [mask.py](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/core/neural_wbc/core/mask.py); [H1 config](https://github.com/NVlabs/HOVER/blob/main/neural_wbc/inference_env/inference_env/neural_wbc_env_cfg_h1.py)
- HOMIE — [arXiv:2502.13013](https://arxiv.org/html/2502.13013)
- ExBody2 — [arXiv:2412.13196](https://arxiv.org/html/2412.13196)
- TWIST2 — [arXiv:2511.02832](https://arxiv.org/html/2511.02832)
- HugWBC — [arXiv:2502.03206](https://arxiv.org/html/2502.03206)
- AnyBody — [arXiv:2606.29209](https://arxiv.org/html/2606.29209)
- SONIC — [arXiv:2511.07820](https://arxiv.org/html/2511.07820)
- LangWBC — [arXiv:2504.21738](https://arxiv.org/html/2504.21738)
- Open-TeleVision — [arXiv:2407.01512](https://arxiv.org/html/2407.01512)
- HumanPlus — [arXiv:2406.10454](https://arxiv.org/html/2406.10454)
- SEIKO — [arXiv:2312.16465](https://arxiv.org/html/2312.16465)
- Unitree G1 arm-control failure — [Robotics Knowledgebase](https://roboticsknowledgebase.com/wiki/common-platforms/unitree-g1/), [unitree_sdk2_python #173](https://github.com/unitreerobotics/unitree_sdk2_python/issues/173)
- HRP-2 head-yaw stepping threshold — [Steering a Humanoid Robot by Its Head](https://vigir.missouri.edu/~gdesouza/Research/Conference_CDs/IEEE_IROS_2009/papers/0305.pdf)
- Superhot VR time mechanic — [superhotgame.com](https://www.superhotgame.com/superhot-vr)
- Local measurements — [E3 spike results](../research/e3-spike-results.md), [RESEARCH.md §5](../RESEARCH.md)
