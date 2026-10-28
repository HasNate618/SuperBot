# vr-bot

A simulated Unitree G1 humanoid plays **Superhot VR** with no headset and no controllers.

The game renders from a fabricated HMD pose. Captured frames are the robot's only camera. The robot's *achieved* simulated torso and wrist poses become the emulated headset and controller poses — nothing is faked, nothing is teleoperated, and the game's own state is never read back as control.

> ## ⚠️ Work in progress — research is far ahead of the demo
>
> **No level has been cleared.** The body controller walks, holds and dodges in simulation; the game accepts fabricated poses and renders from them. Those two halves have **not yet been joined**: the pose client currently sends scripted constants derived from measured G1 geometry, not a live MuJoCo simulation.
>
> Everything below is labelled with what was measured, where, and what is still missing. See [Status](#status) for the honest scoreboard.

<!-- ![vrbot dashboard](docs/dashboard.png) -->

---

## What works

- **A hidden C0 session you can start from a web page.** One click brings up a nested Weston compositor, Monado with a NULL compositor and its `remote` driver, Steam, Superhot and the pose client — isolated from your desktop, in their own process groups, with cleanup verified afterwards.
- **The game accepts a headset that does not exist.** Superhot VR reaches `XR_SESSION_STATE_FOCUSED` on `Monado: Remote HMD` and enumerates 58 runtime extensions. Measured, `research/c0-results.md`.
- **The view is driven by our poses.** A ±35° scripted sweep changes **59.3 %** of pixels versus **4.0 %** with a static pose — a **9×** signal over scene animation.
- **Real game pixels are captured**, live-previewed in the dashboard at 8 fps and written to disk.
- **172 unit tests** covering config validation, process-group handling, the orchestrator's concurrency guards, the dashboard HTTP API and preview lifecycle.

## What's missing

- **The body does not drive the game yet.** This is the headline gap.
- **In-game measurements still owed:** does a scripted trigger actually fire; what Superhot's clock floor does in play; whether The Fall's pistol and exit at 0.70 m are reachable.
- **`BodyCommand` / `BodyState` bus** — the native interface exists only on paper so far.
- **Off-axis aiming.** The body aims well straight ahead (3.5 cm / 7.2°) and badly at 30° (8.6 cm / 28.3°). It must turn to face a target rather than aim sideways.
- **Live co-residency** — the controller has never run alongside the game, Monado and MuJoCo on the same 8 GB card.
- **The brain.** A model bake-off on labelled frames is planned but not run.

---

## How it works

Three loops, one socket. Only the innermost is real-time.

| Loop | Rate | Owner | What it does |
| --- | --- | --- | --- |
| Body | 50 Hz packets, 500 Hz PD | MuJoCo → `c0_client.py` | Emits achieved torso/wrist poses as 376-byte `mndrmt3` packets |
| Skills | 10–30 Hz | (planned) | Turns a goal into a bounded skill invocation |
| Brain | 5–20 s | (planned) | Names a skill and a side — never joint angles, never poses |

Superhot only advances its clock while the player moves, and its floor is `0.005` — 0.5 % speed, not a pause. That is what makes a 5–20 s brain even legal, and why `hold` is a load-bearing skill rather than "send zero".

## Requirements

Tested on **NixOS** with Hyprland/Wayland. Expected to be adaptable elsewhere, but nothing else is claimed.

- **Steam** plus **your own copy of Superhot VR** (not included) added as a non-Steam game, running under Proton.
- **Monado** — specifically a build carrying the [`remote` driver](https://gitlab.freedesktop.org/monado/monado) (a PSVR2 fork was used here).
- **weston**, **weston-screenshooter**, **weston-debug** — for the nested compositor and capture. `run.sh` shells into `nix shell nixpkgs#weston` if they are missing.
- **ImageMagick** (`magick`) — preview compositing.
- **Python 3.12+** — standard library only, no third-party runtime dependencies.
- **No headset and no controllers.**

## Setup

```bash
git clone <this repo> && cd vr-bot
cp config.example.toml config.toml   # then edit it
./run.sh                             # serves http://127.0.0.1:8765
```

In `config.toml`, the values you almost certainly must change:

| Key | What to put there |
| --- | --- |
| `paths.steam_bin` | your Steam launcher |
| `paths.game_exe` | path to `SUPERHOT_VR.exe` |
| `paths.openxr_active_runtime` | your Monado runtime pointer (below) |
| `paths.session_root` | where session logs go |
| `launch.shortcut_id`, `launch.proton_app_id` | your Steam shortcut's AppId |

**The OpenXR runtime pointer is required, and vrbot only reads it.** Under Wine the game resolves its runtime through the Proton prefix registry → `wineopenxr.dll` → the host OpenXR loader, which looks for `~/.config/openxr/1/active_runtime.json`. Note that `/etc/xdg` is invisible inside the Proton container and `~/.local/share/openxr/1/` is *not* a searched path. Create it from your Monado install:

```bash
mkdir -p ~/.config/openxr/1
cp /etc/xdg/openxr/1/active_runtime.json ~/.config/openxr/1/active_runtime.json
```

If a game is already running, restart it — the loader caches the runtime list per process.

## Controls

The dashboard is a single page with a state badge, session id, uptime and log viewer.

| Control | Effect |
| --- | --- |
| **Start** | Runs the full sequence: Monado → Weston → pose client → Steam → game. `409` if a session is already running. |
| **Stop** | Tears the session down; idempotent. |
| **mode** | Switches the pose client: `hold`, `yaw`, `trigger`, `dodge`, `walk`, `clock`, `manual`. |
| **kill** | Force-stops the game or Steam specifically. |
| **preview** | Live `/preview.mjpg`, plus `/api/frame.jpg` for the current frame. |

`./run.sh --preview-smoke` checks the Weston capture path without launching Steam.

## Safety notes

- **This launches Steam and Superhot on your machine**, in a nested Weston session with a NULL-compositor Monado, so it does not draw on your desktop. Your mouse and keyboard focus stay yours.
- **The kill buttons are force kills.** They terminate *your* Steam and the game process. Anything you had unsaved in other Steam games does not survive a `kill steam`.
- **One session at a time** by design; concurrent starts are guarded and rejected.
- **It does not modify your OpenXR configuration.** It validates that the runtime pointer exists and is readable, and fails preflight with a message if it does not.
- **No game files are included.** Superhot VR is commercial software you must own.
- Cleanup is best-effort and verified rather than assumed: after a stop, any surviving process is logged and surfaced in `/api/state`.

## Repo map

| Path | What it is |
| --- | --- |
| `vrbot/` | **The app.** Config, process-group management, session orchestrator, preview capture, dashboard HTTP API and UI. |
| `tests/` | 172 unittest cases, standard library only: `python3 -m unittest discover -s tests` |
| `spike/` | **Evidence.** `c0/` pose client and launchers, `e1/`–`e3/` OpenXR/capture/latency spikes, `c3/` and `wbc/` controller harnesses. |
| `research/` | **Findings.** Per-spike results with measured numbers, plus controller and game-side surveys. |
| `docs/` | **Design.** The design doc, its builder-check, the state-of-findings summary, and the dashboard spec. |
| `RESEARCH.md` | The original feasibility study (partly superseded; see its banner). |

## Status

Measured on the development machine. `[M]` = measured, file cited.

| Capability | Result | Source |
| --- | --- | --- |
| Game binds to a fabricated HMD | **Session `FOCUSED`**, 58 extensions | `research/c0-results.md` `[M]` |
| View follows fabricated poses | **9× pixel-change signal** over static | `research/c0-results.md` `[M]` |
| Walk (headless simulation) | **39.79 m in 60 s**, 0.663 m/s, no fall | `research/sonic-gate-results.md` `[M]` |
| Hold still (headless) | **RMS 1.3×10⁻⁴ m/s** — 0.02 % of walking speed | `research/c3m-results.md` `[M]` |
| Walk → hold → walk | **5/5 cycles**, no fall, resumes within 0.2 m | `research/c3m-results.md` `[M]` |
| Dodge | **0.15–0.18 m in < 0.5 s** | `research/wbc-policy-proposal.md` `[M]` |
| Aim, straight ahead | **3.51 cm / 7.22°** | `research/sonic-gate-results.md` `[M]` |
| Aim, 30° off-axis | **8.63 cm / 28.33°** — the weak axis | `research/sonic-gate-results.md` `[M]` |
| Trigger reaches the game | **not measured** | — |
| Body drives the game | **not built** | — |
| Level cleared | **not built** | — |

## Scope

Deliberately narrow: **NixOS, Superhot VR, a local Steam install, a local Monado runtime, one session at a time.** This is not a general VR-game bridge, not a headset emulator for arbitrary titles, and not a robot-learning research platform. Where the scope is narrower than the goal, the README says so rather than rounding up.

## License

MIT — see [`LICENSE`](LICENSE). Superhot VR is property of its owners and is not distributed here.
