# System-1 game agents — how slow models become fast actions

**Date:** 2026-10-23
**Agent:** `s1-research`
**Status:** source-verified brief. No code was written and no experiments were run for this document.
**Question:** how do published game and robotics agents turn slow knowledge (LLM/VLM, seconds) into real actions (10–50+ Hz), and which of those patterns should vr-bot copy, adapt, or reject?

**Claim tags.** Every factual statement is one of:

- **CITED** — a source URL is given in the row or the Sources section.
- **MEASURED** — observed on this machine, with the local file named.
- **ASSUMPTION** — reasoning or extrapolation, not tested.

Table rows in §2 are **CITED** via their Source column. The tags in prose apply to individual claims.

---

## 0. Bottom line

- **No system reviewed here runs an LLM synchronously inside a 50 Hz motor loop.** Systems either put the slow model above a fast loop that owns timing (Voyager, Odyssey, Cradle, SayCan, Helix, GR00T N1, Hi Robot), or make the slow model itself the control loop at 1–5 Hz (RT-2). The fast loop is one of: a scripted game/robot API (Voyager, Odyssey, Cradle), a learned skill or reactive policy (SayCan, VPT, STEVE-1, SIMA, ACT, π0), or a latent-conditioned controller (Helix, GR00T N1). **[CITED]** (sources below)
- **The closest precedent for vr-bot's shape is not a humanoid VLA.** It is an asynchronous, validated command suggester over a deterministic 50 Hz loop: DistilGPT-2 on a Jetson proposes `(v, ω)` every ~186 ms, an FSM accepts ~80% of suggestions, and the loop never blocks. **[CITED]** [doi:10.3390/make8020049](https://doi.org/10.3390/make8020049)
- **Dual-system systems pass a narrow interface, not text at 200 Hz.** Helix passes a single latent vector at 7–9 Hz into a 200 Hz controller; Hi Robot passes an atomic language command once per second into π0. **[CITED]** [Figure Helix](https://www.figure.ai/news/helix), [Hi Robot](https://arxiv.org/abs/2502.19417)
- **For Superhot, `hold` is the enabling skill.** The game advances time only when the player moves. **[CITED]** [superhotgame.com/superhot-vr](https://www.superhotgame.com/superhot-vr), [Steam app 617830](https://store.steampowered.com/app/617830/). Our body's zero command is not a hold: the G1 policy drifts **0.475 m in 5 s** at zero command. **[MEASURED]** `stubborn-gamer/SEED.md` §3. The brain's 5–20 s latency is safe only if `hold` actually holds; otherwise the world advances while the brain thinks. **[ASSUMPTION]**
- **Build the skill loop, not a policy.** VPT/STEVE-1/SIMA/RT-2/π0 all need large demonstration corpora or teleoperation. We have neither. Our E3 commander is already a skill library; the work is to give it an async command envelope, an active hold, pixel-only target tracking, and achieved-state termination.

---

## 1. vr-bot's loops, for comparison

| Loop | Rate | Period | What it owns | Evidence |
| --- | ---: | ---: | --- | --- |
| Body | **50 Hz** packets, 500 Hz PD | 20 ms / 2 ms | joint targets, `mj_step`, achieved poses to Monado | **[MEASURED]** `research/e3-spike-results.md` |
| Skill (proposed) | 10–30 Hz | 33–100 ms | skill execution, target tracking, termination | **[ASSUMPTION]** design target |
| Brain | 0.05–0.2 Hz | 5–20 s | skill selection, target class, cancel | **[MEASURED]** `stubborn-gamer/SEED.md` §3: MiniCPM5-2B 20.00 s p50, MiMo-9B 5.07 s p50, LFM 0.21 s but 73% `navigate_to` |

The body is honest: only **achieved** `mjData` poses go into the OpenXR packet; desired IK targets never do. **[MEASURED]** `spike/e3/body_bridge.py`, `research/e3-spike-results.md` §6.

The body is also limited, and those limits shape which published patterns transfer:

- Reach IK residual **4.3 cm**, punch **13.5 cm**. **[MEASURED]** `research/e3-spike-results.md` §1.
- Joint targets rate-limited at **2 rad/s**; arm IK at 50 Hz; PD at 500 Hz. **[MEASURED]** `research/e3-spike-results.md` §7.
- **No neck.** HMD orientation is torso orientation; looking left/right rotates the whole chest. **[MEASURED]** `research/e3-spike-results.md` §2.
- Policy is **not in the loop**. E3 stands under the Menagerie position actuators. **[MEASURED]** `research/e3-spike-results.md` §2.
- The 0.475 m/5 s drift belongs to the **learned locomotion policy**, not to E3's scripted stand. E3's scripted stand stayed within **3.08 cm in x and 0.59 cm in y over 20 s**. **[MEASURED]** `research/e3-spike-results.md` §1 and `stubborn-gamer/SEED.md` §3. These are different regimes and must not be conflated.

---

## 2. The systems

### 2.1 Slow planner + skill/code API

These systems keep the slow model above a scripted interface. They are the closest architectural match to vr-bot today.

Where a paper does not publish a control frequency, the cell gives the cadence it does publish (seconds per call, 0.5 s screenshots, 8-action chunks) rather than inventing Hz.

| System | One-line | Slow loop | Fast loop | Slow output → fast input | Fast loop output | Observation | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Voyager** (2023) | GPT-4 writes JavaScript that calls Mineflayer primitives; successful code becomes a reusable skill library | GPT-4 per task, seconds; iterative prompting, execution errors, self-verification | Mineflayer API calls per game tick; no learned fast policy | JavaScript skill code → API calls | movement, mining, crafting, combat primitives | **privileged** Mineflayer state (inventory, position, blocks), not pixels | [arXiv:2305.16291](https://arxiv.org/abs/2305.16291) |
| **Odyssey** (2024/25) | LLM planner–actor–critic over an open-world skill library: 40 primitive + 183 compositional skills | fine-tuned LLaMA-3; planner/actor/critic calls per subgoal, seconds | scripted skill execution via Minecraft API; no learned fast policy | skill name + arguments → scripted skill | scripted Minecraft actions | **privileged** API state, not pixels | [arXiv:2407.15325](https://arxiv.org/abs/2407.15325) |
| **Cradle** (2024) | GPT-4V/4o sees screenshots and writes executable keyboard/mouse code; six modules incl. self-reflection and skill curation | one VLM call per action, seconds; screenshot every 0.5 s; game can be paused while waiting | executor runs code for a specified duration; no learned policy | Python skill code → OS keyboard/mouse | key presses, holds, releases, pointer moves | **pixels only** (no API, no game state) | [arXiv:2403.03186](https://arxiv.org/abs/2403.03186) |
| **SayCan** (2022) | LLM scores skill descriptions; learned affordance value functions ground them; executes one learned skill at a time | LLM query per skill, seconds; plan–execute–replan | learned visuomotor skill policies; one skill runs for seconds | skill text/ID → learned skill policy | visuomotor actions for one atomic skill | camera + proprio; no privileged game state | [arXiv:2204.01691](https://arxiv.org/abs/2204.01691) |
| **Edge LLM navigation** (2026) | DistilGPT-2 asynchronously suggests `(v, ω)` to a deterministic 50 Hz FSM, with schema, deadline, and kinematic validation | **~5 Hz** (186 ms) on a Jetson AGX Orin; asynchronous | **50 Hz** FSM; **<5 ms** fallback | JSON `{linear_x, angular_z}` → validated sample-and-hold | `cmd_vel` to TurtleBot3 | camera + FSM state; no simulator state | [doi:10.3390/make8020049](https://doi.org/10.3390/make8020049) |

**What matters here.** Voyager and Odyssey are not pixel agents; they read game state through an API. Cradle is the pixel-only version and pays for it with seconds per action. SayCan is the canonical "LLM selects among learned skills" design; it never asks the LLM to produce motor commands. The edge-navigation paper is the only source found that validates the exact dual-rate pattern we need: slow model async, fast loop deterministic, validation layer, TTL, fallback.

### 2.2 Reactive / instruction-conditioned policies

These systems remove the slow model at runtime. A single policy maps pixels (and sometimes proprio) to actions at the game or robot rate.

| System | One-line | Slow loop | Fast loop | Slow output → fast input | Fast loop output | Observation | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **VPT** (2022) | Inverse-dynamics labels 70k hours of Minecraft video; behavior cloning yields a native keyboard/mouse policy | none at runtime; IDM labeling is offline | **20 Hz** mouse/keyboard | none | keyboard/mouse | **pixels only** | [arXiv:2206.11795](https://arxiv.org/abs/2206.11795), [OpenAI VPT](https://openai.com/index/vpt/) |
| **STEVE-1** (2023) | unCLIP-style text prior maps an instruction to a MineCLIP latent; a VPT policy follows the latent | text prior once per instruction, not a control loop | **20 Hz** mouse/keyboard | instruction → MineCLIP latent | keyboard/mouse | **pixels only** | [arXiv:2306.00937](https://arxiv.org/abs/2306.00937) |
| **SIMA** (2024) | Image + instruction → keyboard/mouse, 8-action chunks, trained on human gameplay across commercial games | none at runtime; single policy | one policy emitting **8-action chunks** at the environment frame rate; no separate slow loop | instruction embedding → policy | keyboard/mouse | **pixels only**; no source code or bespoke API | [arXiv:2404.10179](https://arxiv.org/abs/2404.10179) |
| **RT-2** (2023) | A 55B VLM is co-fine-tuned to emit robot actions as text tokens; the 5B variant is ~5 Hz | **1–3 Hz** (55B), **~5 Hz** (5B); the slow pass *is* the control loop | none separate; each forward pass produces the next action | action tokens → robot | discretized end-effector/base actions | **pixels + instruction**; no privileged state | [arXiv:2307.15818](https://arxiv.org/abs/2307.15818) |
| **ACT** (2023) | CVAE transformer predicts a 90-action chunk from images + joint positions; 50 demos per task | none at runtime | **50 Hz**, chunk size **90** (~1.8 s) | none | joint positions | **pixels + joints** | [arXiv:2304.13705](https://arxiv.org/abs/2304.13705) |

**What matters here.** VPT/STEVE-1/SIMA are the existence proof that a fast, pixel-only game policy is possible, but each required a large corpus (70k hours for VPT; a multi-game human gameplay dataset for SIMA). RT-2 shows that a big VLM can be the policy, but at 1–5 Hz; that is still too slow for a 50 Hz body without a downstream controller. ACT shows the useful trick: predict a **chunk**, not a single action, and let a fast position controller execute it.

### 2.3 Dual-system / hierarchical

These systems explicitly split slow reasoning from fast control and define the interface between them.

| System | One-line | Slow loop | Fast loop | Slow output → fast input | Fast loop output | Observation | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **π0** (2024) | PaliGemma VLM + flow-matching action expert; predicts 50-action chunks at up to 50 Hz across embodiments | VLM + flow chunk inference; one chunk per call | **up to 50 Hz** | 50-action chunk → position controller | joint/end-effector actions | **pixels + proprio** | [arXiv:2410.24164](https://arxiv.org/abs/2410.24164) |
| **Hi Robot** (2025) | High-level VLM turns an open-ended prompt into an atomic language command; π0 executes it | high-level VLM rerun **every 1 s** or on new feedback | π0 at **50 Hz** chunks | atomic language command | action chunk | **pixels + proprio** | [arXiv:2502.19417](https://arxiv.org/abs/2502.19417) |
| **Helix** (2025) | Figure's "S2/S1" upper-body VLA: 7B VLM at 7–9 Hz outputs one latent; 80M visuomotor policy at 200 Hz outputs 35-DoF targets | **S2: 7–9 Hz** | **S1: 200 Hz** | single continuous latent vector | wrist poses, finger flexion/abduction, torso/head orientation | **pixels + wrist/finger state** | [figure.ai/news/helix](https://www.figure.ai/news/helix) (company report, not peer-reviewed) |
| **GR00T N1** (2025) | NVIDIA humanoid VLA: Eagle-2 VLM S2 at 10 Hz + DiT flow-matching S1 at 120 Hz | **S2: 10 Hz** on an L40 | **S1: 120 Hz**; 16-action chunk in 63.9 ms | VLM tokens | action chunk | **pixels + proprio** | [arXiv:2503.14734](https://arxiv.org/abs/2503.14734) |
| **DreamerV3** (2023) | Learns a world model and trains an actor-critic in imagination; solves Minecraft from pixels with fixed hyperparameters | world model imagination during training; no LLM at runtime | one action per environment step | none | discrete/continuous action | **pixels** (+ reward during training) | [arXiv:2301.04104](https://arxiv.org/abs/2301.04104) |

**What matters here.** The fast loop is not a language model in any of these. Helix and GR00T pass a **latent**; Hi Robot passes a **short atomic command**. The interface is deliberately low-dimensional and high-rate. DreamerV3 is the counterexample: a learned world model can act fast from pixels, but it needs a reward signal and a training regime; Superhot's reward is inside the game and cannot be read honestly.

---

## 3. Patterns: copy, adapt, reject

| Pattern | Best exemplars | Verdict for vr-bot | Why |
| --- | --- | --- | --- |
| **Skill library / code-as-policy** | Voyager, Odyssey, Cradle, SayCan | **Adapt** | Our commander already is a skill library, and a fixed one is safer than arbitrary generated code. Use LLM codegen offline to author skills, never in the 50 Hz path. SayCan's "LLM selects, learned skill executes" is the right division of labor; we can keep the LLM selection and replace the learned skill with scripted IK. |
| **Instruction-conditioned reactive policy** | VPT, STEVE-1, SIMA, RT-1, RT-2 | **Reject for v1; revisit after demos** | No Superhot demonstration corpus, no policy in the loop, 8 GB card, and no honest way to bootstrap a pixel policy without teleoperation. A learned policy is the only route to sub-100 ms reactions, but Superhot's time dilation means we do not need that yet. |
| **Action chunking** | ACT, π0, SIMA, GR00T N1 | **Copy** | Predicting/executing a timed chunk hides inference latency and produces smooth ballistic motion. Use chunks for punch/reach/dodge; do not average chunks across an impact (temporal ensembling can smear the strike). Rest at chunk boundaries if a learned policy ever replaces IK. |
| **Dual-system / latent interface** | Helix, GR00T N1, Hi Robot, π0.5 | **Copy the interface, reject the model** | Our explicit JSON skill command is the analogue of Helix's latent or Hi Robot's atomic command. The skill loop at 10–30 Hz is our S1; the 5–20 s VLM is our S2. Do not train an end-to-end VLA to get this separation. |
| **World-model actor** | DreamerV3 | **Reject** | Needs a reward, a learned dynamics model, and compute we do not have. Superhot's state is privileged; training a world model from pixels alone is a research project, not a spike. |
| **LLM in the motor loop** | Cradle, [GITM](https://arxiv.org/abs/2305.17144), edge LLM nav | **Reject synchronous; copy async validation** | A 5–20 s model cannot meet a 20 ms deadline. The transferable pattern is the edge-navigation one: async suggestions, schema/deadline/kinematic validation, TTL, sample-and-hold, <5 ms fallback. ~80% output validity means the validator is not optional. |
| **Options / semi-MDP temporal abstraction** | Sutton–Precup–Singh options | **Copy** | `hold`, `look`, `reach`, `punch` are options with initiation sets, policies, and termination conditions. The brain selects an option; the skill loop executes and terminates it. This is the correct formalism for Superhot's stop–go rhythm. |
| **Visual servoing / tracking** | KCF, CoTracker, Chaumette–Hutchinson | **Copy at 10–30 Hz** | A cheap tracker gives pixel-only target bearing between VLM calls, closing the loop without a fast VLM. KCF is hundreds of fps on CPU; CoTracker gives dense tracks but is heavier. Use tracking for reach/punch aim, not for 500 Hz control. |
| **Ballistic / DMP trajectory** | Ijspeert DMPs | **Copy** | Punch, reach, and dodge are timed trajectories with a clear start and end. Plan once, execute with PD + proprio feedback, never replan mid-swing. This matches the E3 phase model and the 2 rad/s limit. |
| **Unified command space** | HOVER | **Adapt** | HOVER's masks over root velocity, joint angles, and keypoint targets are a good schema precedent. Our body is simpler: one command type at a time, with a capability flag for unsupported skills (`move`, `dodge`) until the policy is validated. |

---

## 4. Concrete recommendations for our skill loop

### 4.1 Rates and ownership

- **Body stays at 50 Hz / 500 Hz PD.** Do not raise it to chase a learned policy. **[MEASURED]** E3.
- **Skill loop ticks at 50 Hz but re-decides at 10–30 Hz.** The 50 Hz tick keeps interpolation and packets smooth; the decision cadence can be slower. **[ASSUMPTION]** design.
- **Brain never blocks the skill loop.** It writes a command to a queue; the skill loop consumes the latest valid command and cancels the previous one. This is the edge-navigation pattern. **[CITED]** [doi:10.3390/make8020049](https://doi.org/10.3390/make8020049)
- **The brain does not stream motor targets.** It selects a skill and semantic parameters (`side`, `direction`, `target class`); the skill loop owns IK, rate limits, and reachability clamping.

### 4.2 Command envelope

Current E3 commander emits `stand`, `reach left/right`, `punch left/right`, `look ±yaw`, `stop`. Wrap those in a versioned envelope:

```json
{
  "id": "cmd-0007",
  "skill": "punch",
  "params": {"side": "right", "target": "chest_high"},
  "ttl_ms": 1500,
  "issued_at": 0.0,
  "source": "brain",
  "cancel_previous": true
}
```

Rules:

- **Map the E3 names:** `stand` → `hold`; `stop` → cancel then `hold`; `look_left/right` → `look` with `yaw=±0.6`; `reach_left/right` and `punch_left/right` → skill + `side`.
- **Known skill only.** Reject unknown skills and unknown params.
- **TTL mandatory.** A stale command is discarded, not executed late. TTL gates *acceptance*; once accepted, a ballistic skill runs to its own termination.
- **Latest wins, but only if valid.** Invalid → `hold`, not the previous motion.
- **Capability flag.** `move` and `dodge` are reserved but disabled until the body can execute them honestly. v1 supports `hold`, `look`, `reach`, `punch`, `trigger`, `stop`.
- **Validation is cheap and strict:** schema, side, yaw within ±0.6 rad, target within reachable workspace, implied joint speed ≤2 rad/s, duration ≤ max. Invalid commands are counted and fed back to the brain.
- **`trigger` is asserted during the punch phase**, as E3 already does; it is not a contact claim. The packet still carries achieved poses. **[MEASURED]** `spike/e3/body_bridge.py`.

### 4.3 `hold` is the enabling skill

- **E3's scripted stand is not a hold.** It stayed within 3.08 cm in x and 0.59 cm in y over 20 s with position actuators. **[MEASURED]** `research/e3-spike-results.md` §1. The learned G1 policy drifts **0.475 m in 5 s** at zero command. **[MEASURED]** `stubborn-gamer/SEED.md` §3.
- **Linear extrapolation:** at MiniCPM's 20 s p50, passive drift would be ~1.9 m; at MiMo's 5 s p50, ~0.475 m. **[ASSUMPTION]** linear extrapolation of the measured 0.095 m/s drift; real drift may saturate or diverge.
- **Superhot gates time on tracked motion.** The game advances only when the player moves. **[CITED]** [superhotgame.com/superhot-vr](https://www.superhotgame.com/superhot-vr), [Steam app 617830](https://store.steampowered.com/app/617830/). Because our HMD/controllers are the achieved body poses, base drift *is* player motion. **[ASSUMPTION]** direct consequence of E3's achieved-pose rule.
- **Therefore `hold` must be an active controller**, not a zero command: balance, base pose, drift estimate, and timeout. It is the skill that buys the brain its 5–20 s.
- **Stabilize the tracked end effectors, not just the base.** Superhot sees the HMD and controllers, so a hold that balances the base but lets the wrists sway still moves the world. Damp the arms during `hold` and target the achieved HMD/controller poses in game space, not just torso position. **[ASSUMPTION]** from the game's tracked-motion rule plus E3's achieved-pose mapping.
- **Validate `hold` with the ONNX policy in the loop**, not just the Menagerie actuators. The E3 stand result does not transfer to the policy regime.
- **Proposed termination (ASSUMPTION):** base drift < 5 cm over 2 s and base height within the stand range; otherwise re-issue or fail. Tune against measured drift.

### 4.4 Ballistic skills

- **Plan once at acceptance, execute to termination.** IK at 50 Hz, 2 rad/s rate limit, PD at 500 Hz, no mid-swing VLM. **[MEASURED]** E3.
- **Success is achieved-state, not desired-state.** Reach residual is 4.3 cm, punch residual 13.5 cm. **[MEASURED]** E3. Define reach success as the achieved wrist staying within a tolerance of the tracked target; define punch success as the strike phase completing with the achieved wrist reaching the strike volume. Do not claim contact.
- **Chunk lengths:** start from E3's measured phase durations (reach 3 s, punch 2 s, look 2 s) and shorten only after measuring tracking. The 2 rad/s limit and joint limits make a 0.3 s punch unlikely; do not assume it. **[ASSUMPTION]** for any shorter duration.
- **No temporal ensembling across impact.** ACT's overlap-and-average trick is for smooth manipulation, not for a strike. Execute the punch chunk as a unit; if a new command arrives mid-strike, queue it for after the strike.

### 4.5 Honesty and success

- **Only achieved `mjData` poses go to Monado.** This is already the E3 rule; keep it. **[MEASURED]** E3.
- **Never read game memory.** PokeGym exists because privileged-state leakage is a known benchmark flaw; its evaluator uses memory scanning, but the *agent* is restricted to raw RGB. **[CITED]** [arXiv:2604.08340](https://arxiv.org/abs/2604.08340). Our evaluator may read logs; our controller may not.
- **Log both desired and achieved.** The brain gets a compact achieved-state result (skill, duration, residual, drift, termination reason), not the desired target.
- **Per-skill success definitions** are required before the brain can sequence skills. Without them, the brain cannot tell "punch missed" from "punch done."

### 4.6 Vision

- **Send the brain a still frame plus a cheap digest:** target bearing/bbox, ego motion, achieved hand positions, and proprio. Do not send a video stream.
- **Track at 10–30 Hz with a cheap CV tracker** (KCF for speed, CoTracker when dense tracks are worth the cost). **[CITED]** [KCF](https://arxiv.org/abs/1404.7584), [CoTracker](https://arxiv.org/abs/2307.07635).
- **Close reach/punch on the tracked target with visual servoing**, not on a VLM re-query. **[CITED]** [Chaumette & Hutchinson](https://doi.org/10.1109/MRA.2006.250573). The VLM chooses *what*; the tracker and servo handle *where*.
- **No privileged game state in the digest.** If it cannot be seen in the screenshot or measured by the body, it does not go to the brain.

### 4.7 Superhot mapping

| Game need | Skill | Execution | Brain's job |
| --- | --- | --- | --- |
| Wait / think | `hold` | active balance; freeze tracked motion | decide when to move |
| Aim / turn | `look` | waist yaw ±0.6 rad; whole chest turns | choose direction; remember no neck |
| Hit | `punch` | ballistic; trigger asserted during punch | choose side/target class |
| Dodge | `dodge` | lateral burst / lean; **disabled until body can do it** | choose direction/time |
| Reposition | `move` | short burst; **disabled until policy validated** | choose waypoint |
| Shoot | `trigger` | asserted during punch phase | choose target |

The no-neck constraint is a game-design constraint, not just a body detail: `look` rotates the entire view, so the brain must not assume it can glance without changing the punch geometry. **[MEASURED]** E3 §2.

### 4.8 What not to build

- No learned policy in the loop yet.
- No VLM at 10–30 Hz.
- No arbitrary LLM-generated code in the body path.
- No game-memory reads.
- No mid-swing VLM replanning.
- No temporal ensembling across a punch.

---

## 5. Uncertainty

- **Exact control rates are not published for every system.** SIMA reports 8-action chunks, not a control Hz; the underlying rate is environment-dependent. Cradle reports a 0.5 s screenshot cadence, not a control frequency. SayCan reports one skill per LLM query, not a skill Hz. DreamerV3 runs at the environment step rate. Those cells say so rather than inventing a number.
- **Helix is a company report**, not a peer-reviewed paper. Its 7–9 Hz / 200 Hz split is the cleanest public description of the S1/S2 pattern, but treat the numbers as vendor-reported.
- **RT-2's 1–3 Hz / 5 Hz** are from the paper's cloud-TPU setup; a local 8 GB card cannot reproduce them.
- **"System 1 / System 2" is a cognitive analogy**, not a formal architecture. The engineering content is the rate split and the narrow interface.
- **The 1.9 m / 20 s drift is an extrapolation.** The measured figure is 0.475 m / 5 s for the learned policy; linear extrapolation is the simplest assumption and may be wrong.
- **Superhot's time-gating is cited from the game's marketing/Steam page**, not measured in our stack. We have not run Superhot VR with the E3 body. The claim that base drift advances time follows from our achieved-pose mapping, but it is an **ASSUMPTION** until measured.
- **Punch contact is unverified.** The 13.5 cm IK residual means the fist may not reach the game's expected strike volume. Triggering during the punch phase is honest about intent but not about contact; it may still miss.
- **No force feedback.** E3 sends poses out; the game does not push the body. A punch that looks right in sim may have no physical counterpart.
- **The brain latency percentiles are over parsed answers only**, so they have different denominators and exclude the slowest truncations. `stubborn-gamer/SEED.md` §3 records MiMo's p95 as 12.43 s over its 45 parsed states but 41.13 s over all 48; MiniCPM's p95 is 31.07 s over parsed states. The 20 s / 5 s p50 figures are the right planning numbers only if the model terminates; they are not safe upper bounds.
- **Compute:** the target RTX 4060 8 GB is planned to run Superhot + Monado + MuJoCo; it cannot also host Helix/GR00T-class models. **[ASSUMPTION]** until measured with all three running together. All VLM latency figures are llama.cpp measurements from `stubborn-gamer`, not vendor benchmarks.

---

## 6. Sources

**Game / skill agents**

- Voyager — [arXiv:2305.16291](https://arxiv.org/abs/2305.16291)
- Odyssey — [arXiv:2407.15325](https://arxiv.org/abs/2407.15325)
- Cradle — [arXiv:2403.03186](https://arxiv.org/abs/2403.03186)
- SayCan — [arXiv:2204.01691](https://arxiv.org/abs/2204.01691)
- VPT — [arXiv:2206.11795](https://arxiv.org/abs/2206.11795), [OpenAI VPT](https://openai.com/index/vpt/)
- STEVE-1 — [arXiv:2306.00937](https://arxiv.org/abs/2306.00937)
- SIMA — [arXiv:2404.10179](https://arxiv.org/abs/2404.10179)
- Edge LLM navigation — [doi:10.3390/make8020049](https://doi.org/10.3390/make8020049)

**Robot policies**

- RT-1 — [arXiv:2212.06817](https://arxiv.org/abs/2212.06817)
- RT-2 — [arXiv:2307.15818](https://arxiv.org/abs/2307.15818)
- ACT — [arXiv:2304.13705](https://arxiv.org/abs/2304.13705)
- π0 — [arXiv:2410.24164](https://arxiv.org/abs/2410.24164)
- π0.5 — [arXiv:2504.16054](https://arxiv.org/abs/2504.16054)
- Hi Robot — [arXiv:2502.19417](https://arxiv.org/abs/2502.19417)
- Helix — [figure.ai/news/helix](https://www.figure.ai/news/helix)
- GR00T N1 — [arXiv:2503.14734](https://arxiv.org/abs/2503.14734)
- HOVER — [arXiv:2410.21229](https://arxiv.org/abs/2410.21229)
- DreamerV3 — [arXiv:2301.04104](https://arxiv.org/abs/2301.04104)

**Patterns**

- Options / semi-MDP — [Sutton, Precup & Singh, 1999](https://doi.org/10.1016/S0004-3702(99)00052-1)
- KCF tracker — [arXiv:1404.7584](https://arxiv.org/abs/1404.7584)
- CoTracker — [arXiv:2307.07635](https://arxiv.org/abs/2307.07635)
- Visual servoing — [Chaumette & Hutchinson, 2006](https://doi.org/10.1109/MRA.2006.250573)
- DMPs — [Ijspeert et al., 2013](https://doi.org/10.1162/neco_a_00393)
- Privileged-state critique / pixel-only benchmark — [PokeGym, arXiv:2604.08340](https://arxiv.org/abs/2604.08340)
- Superhot VR — [superhotgame.com/superhot-vr](https://www.superhotgame.com/superhot-vr), [Steam app 617830](https://store.steampowered.com/app/617830/)

**Local measurements**

- `research/e3-spike-results.md` — 50 Hz packets, 500 Hz PD, achieved-pose equality, 4.3/13.5 cm IK residuals, no neck, scripted stand 3.08 cm/20 s.
- `spike/e3/body_bridge.py` — `PHASES`, trigger asserted during punch, achieved `mjData` only.
- `stubborn-gamer/SEED.md` §3 — 0.475 m/5 s drift, VLM latencies, 8 GB card.
- `RESEARCH.md` §5–§6, §11–§12 — honesty rule, game choice, Superhot rationale.

---

## 7. What to build next for the brain

- **Make `hold` the default skill and validate it with the ONNX policy in the loop.** Replace E3's scripted `stand` with active balance + drift monitoring + TTL; do not trust the 3.08 cm scripted-stand result under the learned policy.
- **Add an async command bus with a versioned JSON envelope.** Latest-wins queue, `id`/`ttl_ms`/`cancel`, strict schema and kinematic validation, invalid → `hold`; the brain must never block the 50 Hz body.
- **Add a pixel-only target digest + tracker.** KCF/CoTracker at 10–30 Hz produces target bearing and achieved-hand error; the brain gets one still frame + digest + proprio, and reach/punch close on achieved wrist vs tracked target, not on a VLM re-query.
- **Add per-skill outcome telemetry from `mjData`.** Log start/end, achieved pose, residual, drift, termination reason; feed the compact result back to the brain so it can sequence skills without privileged state.
- **Capture demonstrations for a future chunking policy, but do not train one yet.** Record scripted and teleoperated episodes in an ACT/π0-compatible format once `hold` and the skill loop are stable; a learned policy is a later step, not the next one.
