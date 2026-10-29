# SuperBot

**A Unitree G1 humanoid simulated in MuJoCo, a released NVIDIA whole-body controller, and a commercial VR game that renders from head and hand poses we send through Monado. No headset, no controllers, no game files touched.**

SuperBot is a real-time controls and systems-integration project that uses a commercial VR game as an adversarial task interface. The game is the framing device. The work is a 29-DoF humanoid at 50 Hz behind an undocumented socket, where every claim is measured and every failure is written down.

> **The scoreboard, up front.** A commercial VR game accepts a headset that does not exist and renders from poses we send — measured at 9× the pixel signal of the scene's own animation. A simulated G1 walks 39.79 m in 60 s without falling, holds to an RMS of 1.3×10⁻⁴ m/s, and aims to 3.51 cm dead ahead — all measured headless.
>
> **Those two halves have never been connected.** The pose client sends *scripted constants* derived from measured G1 geometry, not a live MuJoCo simulation. The body has never been joined to the game. The robot has never played the game. No level has been cleared. Everything below is labelled with what is proven, what is not, and where the number came from.

<!-- ![superbot dashboard](docs/dashboard.png) -->

---

## What this demonstrates

- **Real-time humanoid control.** 29 DoF (12 legs, 3 waist, 14 arms; no fingers, no neck), MuJoCo 3.17 MJCF physics, a 50 Hz policy with 500 Hz PD, and a measured **17.66 ms** of work per 20 ms control tick (encoder 7.54, decoder 5.42, physics 4.71).
- **Integrating a released learned controller.** NVIDIA GR00T-WholeBodyControl / GEAR-SONIC, the released ONNX weights, encoder 1762→64 and decoder 994→29 through ONNX Runtime — plus a real performance bug found by measurement: onnxruntime opened one intra-op thread per core and consumed 1876 % CPU across 47 threads, load average 21, for two networks that small.
- **Systems triage across a hostile five-layer stack.** Wine/Proton → OpenXR loader → Monado → headless compositor → game. The working path was found by diagnosing each layer separately: a runtime pointer invisible inside the container, a loader that caches its manifest list per process, and a Wine shim that forwards Windows OpenXR calls to the host loader.
- **A designed command contract, not an ad-hoc pipe.** A masked task-space `BodyCommand` / `BodyState` protocol over a Unix socket: authority bitmask, TTL on acceptance, monotonic sequencing, latest-wins sample-and-hold, non-preemptible ballistics, and strict validation that never raises into the control loop. Standard-library Python only.
- **Process supervision and a reproducible environment.** One command brings up Monado, a headless Weston compositor, Steam, the game and the pose client as supervised process groups, in a session you can see in a browser and kill cleanly. NixOS flake devShell; 238 unit tests, no network, no GPU.
- **Measuring instead of assuming.** Latency budgets, an A/B pixel proof that the view really follows our poses, and documented negative results — including two of this project's own hypotheses being falsified, and one of its own harness bugs being caught and corrected.

---

## Architecture

Two pipes. The game pipe runs today. The body pipe is measured today. The edge between them is the missing piece, and it is drawn as missing.

```text
GAME PIPE — live today
  weston --backend=headless --renderer=gl     private display, no desktop window
    ├─ Steam client UI → Proton/Wine → SUPERHOT_VR.exe
    │    UnityOpenXR.dll → wineopenxr.dll → host OpenXR loader
    │    → Monado [remote driver] :4242, with XRT_COMPOSITOR_NULL=1
    └─ game mirror window → weston-screenshooter → preview + PNG frames

  ✗  NOT CONNECTED. Nothing forwards BodyState into the game.

BODY PIPE — measured today
  BodyCommand (JSON lines, Unix socket) → CommandBus → SonicExecutor
    → MuJoCo G1, 29 DoF, 50 Hz policy / 500 Hz PD
    → achieved torso and wrist poses → 376-byte mndrmt3 @ 50 Hz → Monado :4242

  Today the pose client sends scripted constants instead of those poses.
```

The pose client (`spike/c0/c0_client.py`) speaks Monado's `remote` driver wire format directly: a 376-byte `r_remote_data` packet, header `mndrmt3`, at 50 Hz over TCP 4242. Its constants are the real body, not placeholders: eye height 1.221 m and wrists at (∓0.222, 0.721, 0.017) m, both measured from the G1 model in the E3 spike.

**The command contract.** The brain is supposed to name a skill and a side; it never writes joints or poses. Everything below the adapter speaks one masked task-space command:

| Field | Meaning |
| --- | --- |
| `authority` | 9-bit mask. Unset bits are *ignored, not zeroed*, so a mode is a mask. `hold` is the empty mask: nothing requested, the executor's standing behaviour applies. |
| `seq` | Monotonic. A stale sequence number never rewinds the latest command. |
| `ttl_ms` | 200 ms default. Expiry gates **acceptance**, not execution: an expired command is dropped and the body holds. |
| `duration_ms`, `preempt` | A non-preemptible ballistic runs to completion; newer commands queue behind it instead of blending into it mid-skill. |
| `head`, `left_hand`, `right_hand` | Root-relative pose, metres and `xyzw` quaternion. |
| `closure` | 0 open → 1 closed; the game adapter maps it to trigger/grip. |
| `BodyState` | Achieved `mjData` poses, per-effector residuals, and flags (`SATURATED`, `TTL_EXPIRED`, `FALL_RISK`, `UNSUPPORTED`). Never what was asked for. |

Validation is deliberately strict and never raises into the 50 Hz loop: schema, unknown authority bits, non-finite numbers, quaternion norm, workspace box, joint and hand speed limits, TTL on arrival. A rejection means execute `hold` and increment an invalid counter that reaches the next brain observation.

---

## Status: what is proven, what is not

This is the table a technical interviewer should check first. Nothing in the "not proven" column is implied by the "proven" column.

| Capability | State | Evidence |
| --- | --- | --- |
| Game binds to a fabricated HMD | **proven** — session reached `XR_SESSION_STATE_FOCUSED`, 58 runtime extensions | `research/c0-results.md` |
| Game's view follows our poses | **proven** — 9× mean pixel change, 15× changed area, pose-only A/B | `research/c0-results.md` |
| Real game pixels are capturable | **proven** — tutorial frame with real content: 41 % bright, 3175 distinct colours | `research/c0-results.md` |
| 29-DoF body walks 60 s without falling | **proven** — headless CPU | `research/sonic-gate-results.md` |
| Body holds still | **proven when settled**; entering `hold` from a walk costs a settle transient | `research/c3m-results.md` |
| Body aims straight ahead | **proven** — 3.51 cm / 7.22° | `research/sonic-gate-results.md` |
| Body aims off-axis | **fails** — 8.63 cm / 28.33° at 30°, so the body must turn to face a target | `research/sonic-gate-results.md` |
| Sim pose → wire packet is exact | **proven** — 1001 packets / 20.02 s, max position and quaternion error 0.0 | `research/e3-spike-results.md` |
| `BodyCommand` bus (TTL, sequencing, validation) | **implemented and tested** | `superbot/body.py`, `superbot/bus.py`, `superbot/validate.py`, `tests/` |
| Executor honours `BodyCommand` | **implemented in the spike**; runs the third-person body view | `spike/wbc/sonic_executor.py` |
| **Body drives the game** | **not built** — the join is missing | — |
| **Trigger reaches the game** | **not measured** — needs a pistol in hand | `research/c0-results.md` |
| Clock floor behaviour in play | **not measured** — the 0.005 value is read from serialized assets | `research/wbc-game-proposal.md` |
| The Fall's 0.70 m pistol and exit | **not reachable standing** — 27.3 cm wrist residual on the pistol | `spike/wbc_game/logs/reach_far_test.txt` |
| Live co-residency on the 8 GB card | **unverified** — SONIC has never run beside the game, Monado and MuJoCo | `docs/2026-10-23-vr-bot-state-of-findings.md` (R6) |
| A brain / VLM in the loop | **not built** — the scripted commander is the only `BodyCommand` writer | `superbot/scripted.py` |
| **Level cleared** | **no** | — |

---

## Measured results

All values below are read from this repository. `research/` holds the per-spike result documents; every row names its source.

| Metric | Value | Conditions | Source |
| --- | ---: | --- | --- |
| Walk, 60 s | **39.79 m** path / 37.82 m net; 0.663 / 0.631 m/s; yaw drift −2.10°; tilt 9.48°; no fall | GEAR-SONIC default, headless CPU | `research/sonic-gate-results.md` |
| Hold, settled | **RMS 1.3×10⁻⁴ m/s** (0.02 % of walking speed) | 20 s hold, standing clip, anchor-index freeze | `research/c3m-results.md` |
| Hold entered from a walk | RMS 0.050 m/s mean (worst 0.067), 5/5 cycles, no fall, walk resumes within ~0.2 m | settle transient dominates | `research/c3m-results.md` |
| Aim, dead ahead | **3.51 cm** position / **7.22°** orientation | walking, fixed aim point | `research/sonic-gate-results.md` |
| Aim, 30° off-axis | **8.63 cm** / **28.33°** — the weak axis | same | `research/sonic-gate-results.md` |
| Dodge (simulation) | **0.15–0.18 m** head step in < 0.5 s | SONIC via VR override; the game's requirement is unmeasured | `research/wbc-policy-proposal.md` |
| Head yaw → torso yaw | **~0.83 gain**, saturating at **27–31°** | SONIC's native coupling; aim is not turn | `research/wbc-policy-proposal.md` |
| Reach envelope | **~0.20 m**; a 0.70 m target leaves a **27.3 cm** wrist residual | classical WBC probe, standing | `research/wbc-game-proposal.md` |
| Control cost, 1 thread | **17.66 ms** per 20 ms tick: encoder 7.54, decoder 5.42, physics 4.71 — 32 % of one core | onnxruntime pinned to 1 thread (the default) | `research/wbc-policy-proposal.md` |
| Control cost, all cores | **6.8 ms** per tick at **1876 %** CPU, 47 threads, load average 21 | onnxruntime defaults — this is the cost the pinning removes | `research/wbc-policy-proposal.md` |
| Render + encode | **0.86 ms** per 560×420 frame + 1.65 ms JPEG on the RTX 4060 | EGL headless, 10 fps tile | `research/wbc-policy-proposal.md` |
| Sim → wire fidelity | **1001 packets / 20.02 s** at 50 Hz, max position error **0.0** | E3 body bridge, live | `research/e3-spike-results.md` |
| Game session | **`FOCUSED`**, 58 runtime extensions, system `Monado: Remote HMD` | Superhot VR, Proton, RTX 4060 | `research/c0-results.md` |
| View follows pose | static **4.74** mean Δ / **4.0 %** pixels vs sweeping **41.97** / **59.3 %** | same session, same scene, 3.4 s apart | `research/c0-results.md` |
| Capture and latency | **36.9 fps**; pose → pixel **30–59 ms** (mid 44 ms, incl. ~27 ms capture overhead) | `grim` on the laptop panel, `hello_xr` | `research/e2-spike-results.md` |
| Repo test suite | **238 tests**, standard library only | no network, no GPU, no game | `python3 -m unittest discover -s tests -t .` |

---

## Key engineering decisions, and the negative results that forced them

**1. Use the released whole-body controller, not a from-scratch one.**
The from-scratch classical QP is *excellent* and cannot walk: 0.22 mm of base excursion over a 20 s hold, ±35° look to 0.63°, single-arm reach residuals of 2.2–12.9 mm. But every single-support foot lift tipped the robot within 1.3–1.6 s, and 0.10 m of lateral weight shift is the measured limit (0.12 m tips). That is the walking bottleneck appearing *before* the first step. So the QP is a precision executor for standing skills, and walking came from the released policy instead — which is why this repository contains two harnesses and recommends only one.

**2. Do not override the policy's arms with inverse kinematics.**
Hypothesis: SONIC's arms are already driven by VR targets in its training distribution, so an IK override toward the same target should be tolerated. **Falsified.** A hard override makes the arms roughly 4× worse (3.51 → 12.84 cm while walking; 1.14 → 14.40 cm while holding), rate-limiting does not help, and integral correction tips the robot at 65–75° tilt. The only surviving refinement is wrist-orientation-only: 7.22° → 5.08°. The controller is a motion tracker; it produces its own arms, and a joint-space override fights it.

**3. `hold` is a reference-motion switch — not "send zero", and not a freeze of whatever clip is loaded.**
This one caught our own harness. Freezing only the *anchor index* of a walking clip left the encoder's 0.9 s lookahead still walking, so the "hold" travelled 3.93 m in 20 s at 0.21 m/s without falling — exactly what a walking policy told to keep walking should do. Freezing the whole horizon is 36× better (0.2005 → 0.0085 m/s), and switching the reference to a standing clip is better again (1.3×10⁻⁴ m/s). The lesson is not "SONIC cannot hold"; it is that a hold is only meaningful when you state what the lookahead was doing.

**4. Inject at the OpenXR runtime, not with a custom OpenVR driver.**
Superhot ships `UnityOpenXR.dll` and binds Monado's `remote` driver directly, so neither a custom driver nor SteamVR nor a translation layer was needed. The price was a three-fault hunt, all in `research/c0-results.md`: `/etc/xdg` is the *container's* inside pressure-vessel so the active-runtime pointer must live in `~/.config/openxr/1/`; `~/.local/share/openxr/1/` is not a searched path at all; and the loader enumerates manifests once per process, so fixing the pointer requires a game restart. The launch must also come from the Steam client UI — CLI `-applaunch` resolves a non-Steam shortcut to a store AppID and fails `AppError_9`, `rungameid` reports `Unknown GameID type`, and a hand-run Proton entry point dies in `bwrap`.

**5. Keep the commercial game.**
Sanctioned pose APIs (VRChat's OSC input, Resonite) are explicit non-goals in the design, because they hand you the interface and delete the systems problem. Equally out of bounds: reading game memory, injecting into the title, or driving it through an unofficial API. The game is unmodified commercial software you must own, and the interface is the deliverable.

**6. Hide the game in a compositor we control.**
`gamescope --backend headless` with PipeWire capture produced black frames; a Hyprland headless output produced black frames in `grim`. A nested `weston --backend headless --renderer gl --xwayland` session does work: Steam and the game run inside it, never touch the host desktop, and `weston-screenshooter` captures the game's mirror view.

**7. Pin ONNX Runtime to one thread.**
With default settings onnxruntime opened one intra-op thread per core and the body view held about 19 of 20 logical CPUs: 1876 % CPU, 47 threads, load average 21. The networks are 1762→64 and 994→29; a thread pool costs more in spin-wait than it saves in FLOPs. One intra-op and one inter-op thread, sequential execution: 21 threads, 32 % of one core. That costs latency — 6.8 ms/tick becomes 17.66 ms — and buys back 19 cores, which is the right trade for a body that has to share the machine with the game. It also leaves only 2.3 ms of slack in the control tick, which is worth knowing before anything else is added there.

**8. Trust the measured envelope, and let it shape the demo.**
The reliable reach is ~0.20 m, so a standing body cannot touch The Fall's 0.70 m pistol pedestal — measured, 27.3 cm short. The consequence is a design rule, not a rounding error: the body must *walk closer* to what it wants, and must *turn to face* a target rather than aim sideways, because at 30° off-axis the arm error grows from 3.51 cm to 8.63 cm and from 7.22° to 28.33°.

---

## How to run

**Prerequisites.**
- **NixOS** with Steam. Tested on a Hyprland/Wayland host; nothing else is claimed.
- **Your own copy of Superhot VR** (not included, never will be), added to Steam as a non-Steam game and running under Proton.
- **Monado** with the `remote` driver built in.
- **weston**, **weston-screenshooter**, **weston-debug** — supplied by `flake.nix`; `run.sh` re-enters `nix develop` when they are not on `PATH`.
- **Python 3.12+**, standard library only. No headset, no controllers.

```bash
git clone <this repo> && cd SuperBot
cp config.example.toml config.toml        # edit paths, Steam shortcut id, Proton app id
./run.sh                                  # serves http://127.0.0.1:8765
# run.sh reuses a dashboard already on that port; run.sh --replace stops it first.
```

The OpenXR runtime pointer is required and SuperBot only ever reads it. Under Wine the game resolves its runtime through the Proton prefix registry → `wineopenxr.dll` → the host OpenXR loader, which looks for `~/.config/openxr/1/active_runtime.json`:

```bash
mkdir -p ~/.config/openxr/1
cp /etc/xdg/openxr/1/active_runtime.json ~/.config/openxr/1/active_runtime.json
```

If a game is already running, restart it — the loader caches its runtime list per process.

**Development.**

```bash
nix develop                                   # python3 + weston + magick, pinned by flake.lock
python3 -m unittest discover -s tests -t .    # 238 tests, no network, no GPU
nix flake check                               # same suite, sandboxed
./run.sh --preview-smoke                      # Weston capture path, without launching Steam
```

**The controller, headless.** The simulator spikes use their own venv because they need MuJoCo, NumPy and ONNX Runtime while `superbot/` stays standard-library only:

```bash
cd spike/e3
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python 'mujoco==3.17.0' 'numpy<2.3' mink imageio
uv pip install --python .venv/bin/python onnxruntime
cd ../..

# the three SONIC gates (write the summary JSONs the research docs cite)
# hold_c3m.py has one known crash in an extra 5-cycle variant; the two configs
# that completed are the cited evidence, in spike/wbc/logs/c3m_summary.json
spike/e3/.venv/bin/python spike/wbc/gate_c3i.py
spike/e3/.venv/bin/python spike/wbc/arm_override_c3j.py
spike/e3/.venv/bin/python spike/wbc/hold_c3m.py

# one gate re-run by hand
spike/e3/.venv/bin/python spike/wbc/sonic_runner.py \
  --motion squat_001__A359 --mode 1 --seconds 20 --freeze 0 --log /tmp/hold.jsonl --verbose

# third-person MuJoCo view, driving BodyCommand through the SONIC executor
spike/e3/.venv/bin/python spike/wbc/body_view.py --out /tmp/body --reel --seconds 20
```

Offscreen rendering needs no display: `spike/wbc/gl_boot.py` re-execs with the NixOS libglvnd path, because NixOS ships `libEGL_nvidia.so` but no plain `libEGL.so`, and `LD_LIBRARY_PATH` is only read at process start.

Weights are `nvidia/GEAR-SONIC` (encoder 1762→64, decoder 994→29) under the **NVIDIA Open Model License**, not Apache — code written around them here is MIT. The controller code wraps them unmodified.

---

## Controls

The dashboard is a single local page with a state badge, session id, uptime, log tails and the preview.

| Control | Effect |
| --- | --- |
| **Start** | Runs the sequence: Monado → headless Weston → pose client → Steam → game. Returns `409` if a session is already running. |
| **Stop** | Tears the session down; idempotent, and cleanup is verified rather than assumed — surviving processes are logged and surfaced in `/api/state`. |
| **mode** | Switches the pose client: `hold`, `yaw`, `trigger`, `dodge`, `walk`, `clock`, `manual`. |
| **kill** | Force-stops the game or Steam specifically. These are force kills and they hit *your* Steam. |
| **preview** | Live `/preview.png`, plus `/api/frame.png` for the current frame. Frames are the raw Weston output: vertically flipped and including the 32 px status panel. API consumers must flip and crop, as the browser does. |
| **body** | Optional third-person MuJoCo tile (`/api/body.jpg`), run as a sidecar that cannot delay or break a session. It needs the spike venv and weights, and is a *viewer* — it is not the pose source. |

In `manual` mode, click the preview to pointer-lock the browser and drive the hidden game directly: mouse look, WASD move, arrows move the active hand, wheel reaches, LMB/T fires, RMB/G grips, Tab switches hands. Esc releases the pointer immediately; the last pose keeps streaming. Browser batches go to `POST /api/manual/input` and are forwarded to the manual pose client over a per-session Unix datagram socket.

---

## Repo map

| Path | What it is |
| --- | --- |
| `superbot/` | **The app.** Config validation, process-group supervision, session orchestrator, preview capture, dashboard HTTP API and UI, the `BodyCommand`/`BodyState` bus and its validator, manual input bridge. Standard library only. |
| `tests/` | 238 unittest cases: config, process groups, orchestrator concurrency guards, dashboard API, preview lifecycle, command bus, validation. |
| `spike/c0/` | **Pose client and launchers.** The `remote`-driver client (`hold`, `yaw`, `trigger`, `dodge`, `walk`, `clock`), the manual client, the runtime launcher. |
| `spike/e1/`–`spike/e3/` | **Injection and evidence.** OpenXR pose injection, NVIDIA compositor capture and latency measurement, and the MuJoCo body → OpenXR bridge with its honesty CSV. |
| `spike/c3/`, `spike/wbc/`, `spike/wbc_classical/`, `spike/wbc_game/` | **Controller harnesses and logs.** SONIC gates and arm-override matrix, hold/look investigation, classical QP, the 0.70 m reach probe. |
| `research/` | **Findings.** Per-spike results with measured numbers, plus controller and game-side surveys — the source for nearly every figure in this README. |
| `docs/` | **Design.** The design document, its builder-check, the state of findings, and the dashboard spec. |
| `RESEARCH.md` | The original feasibility study (partly superseded; see its banner). |
| `flake.nix`, `run.sh` | Pinned devShell and the one-command entry point. |

---

## Scope and limitations

Deliberately narrow: **NixOS, Superhot VR, a local Steam install, a local Monado runtime, one session at a time, no headset, no controllers.** This is not a general VR-game bridge, not a headset emulator for arbitrary titles, and not a robot-learning research platform. GPU-side VLM inference next to Superhot, Monado and MuJoCo on the same 8 GB card is out of scope.

The sharpest limitations, stated as facts rather than caveats:

- **The robot has never played the game.** The two pipes are individually measured and never joined.
- **Off-axis aiming fails.** At 30° the body points 28.3° off, so it must turn rather than aim sideways.
- **Reach is ~0.20 m.** A standing body cannot touch a 0.70 m pedestal; the pistol sits 27.3 cm out of reach.
- **Live co-residency is unmeasured.** Every controller number is headless CPU; whether 50 Hz survives beside the game is an open question.
- **`hold` has a settle transient.** The 1.3×10⁻⁴ m/s figure is the settled hold; entering it from a walk costs about 0.05 m/s RMS for the first seconds.

The design's own anti-goals are worth repeating because they are the reason the numbers can be trusted: no desired IK targets or VLM-emitted poses in the pose path, no reading game memory, no claiming a punch hit when the residual is 13.5 cm, and no framing this as "robot plays Superhot" instead of whole-body control on an OpenXR task.

---

## License

MIT — see [`LICENSE`](LICENSE). Superhot VR is proprietary software owned by its developers and is not distributed here; the GEAR-SONIC weights are licensed by NVIDIA under the NVIDIA Open Model License.
