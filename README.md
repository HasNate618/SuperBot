# SuperBot

A simulated Unitree G1 humanoid plays **Superhot VR** with no headset and no controllers.

The game renders from a fabricated HMD pose. The dashboard's VNC stream is operator-only, and the robot has no camera feed yet. The robot's *achieved* simulated torso and wrist poses become the emulated headset and controller poses — nothing is faked, nothing is teleoperated, and the game's own state is never read back as control.

> ## ⚠️ Work in progress — research is far ahead of the demo
>
> **No level has been cleared.** The body controller walks, holds and dodges in simulation; the game accepts fabricated poses and renders from them. Those two halves have **not yet been joined**: the pose client currently sends scripted constants derived from measured G1 geometry, not a live MuJoCo simulation.
>
> Everything below is labelled with what was measured, where, and what is still missing. See [Status](#status) for the honest scoreboard.

<!-- ![superbot dashboard](docs/dashboard.png) -->

---

## What works

- **A hidden C0 session you can start from a web page.** One click brings up a nested Weston compositor, Monado with a NULL compositor and its `remote` driver, Steam, Superhot and the pose client — isolated from your desktop, in their own process groups, with cleanup verified afterwards.
- **The game accepts a headset that does not exist.** Superhot VR reaches `XR_SESSION_STATE_FOCUSED` on `Monado: Remote HMD` and enumerates 58 runtime extensions. Measured, `research/c0-results.md`.
- **The view is driven by our poses.** A ±35° scripted sweep changes **59.3 %** of pixels versus **4.0 %** with a static pose — a **9×** signal over scene animation.
- **Real game pixels are streamed live to the dashboard.** A persistent Weston VNC session is viewed through noVNC at 1280x720, target 30 fps; game cadence targets 60 Hz with the companion Monado null-compositor patch (`/etc/nixos/modules/nixos/psvr2-vr.nix`), but the 60 Hz result is not measured in this repo.
- **211 unit tests** covering config validation, process-group handling, the orchestrator's concurrency guards, the dashboard HTTP API and VNC preview lifecycle.

## What's missing

- **The body does not drive the game yet.** This is the headline gap.
- **Robot camera feed.** The old PNG still endpoints were removed; the VNC stream is operator-only and nothing feeds robot perception yet.
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
- **weston** — the nested compositor and VNC backend; **weston-screenshooter** — only for the `--preview-smoke` headless check; **weston-debug** — used for Steam window detection. Supplied by this repo's `flake.nix`; `run.sh` re-enters `nix develop` when the direct-execution tools are not on `PATH`.
- **novnc** and **websockify** — the browser client assets and the WebSocket-to-VNC proxy. Supplied by the devshell.
- **The NixOS PAM service `weston-remote-access`** — authenticates the VNC viewer. This is host configuration, not something the repo installs.
- **ImageMagick** (`magick`) — optional; used only by `--preview-smoke` for a non-black frame check. Also in the devshell.
- **mpv** — needed only by `--preview-smoke`. Also in the devshell.
- **Python 3.12+** — the Python app is standard-library-only; `websockify` is an external runtime dependency.
- **No headset and no controllers.**

`--preview-smoke` is still a headless Weston screenshot-tool check, not the live path.

## Setup

```bash
git clone <this repo> && cd SuperBot
cp config.example.toml config.toml   # then edit it
./run.sh                             # serves http://127.0.0.1:8765
```

`run.sh` runs the app directly only when `weston`, `weston-screenshooter`, `weston-debug`, `novnc`, and `websockify` are all on `PATH`; otherwise it re-enters `nix develop`, so the Python and Weston versions come from `flake.lock` rather than whatever `nixpkgs` happens to be at launch time. `--preview-smoke` additionally needs `mpv` (and uses `magick` when present) and skips if `mpv` is unavailable.

## Configuration

In `config.toml`, the values you almost certainly must change:

| Key | What to put there |
| --- | --- |
| `paths.steam_bin` | your Steam launcher |
| `paths.game_exe` | path to `SUPERHOT_VR.exe` |
| `paths.openxr_active_runtime` | your Monado runtime pointer (below) |
| `paths.session_root` | where session logs go |
| `launch.shortcut_id`, `launch.proton_app_id` | your Steam shortcut's AppId |

**The OpenXR runtime pointer is required, and superbot only reads it.** Under Wine the game resolves its runtime through the Proton prefix registry → `wineopenxr.dll` → the host OpenXR loader, which looks for `~/.config/openxr/1/active_runtime.json`. Note that `/etc/xdg` is invisible inside the Proton container and `~/.local/share/openxr/1/` is *not* a searched path. Create it from your Monado install:

```bash
mkdir -p ~/.config/openxr/1
cp /etc/xdg/openxr/1/active_runtime.json ~/.config/openxr/1/active_runtime.json
```

If a game is already running, restart it — the loader caches the runtime list per process.

## Development

```bash
nix develop                                   # python3 + weston + novnc + websockify + mpv + magick, pinned
python3 -m unittest discover -s tests -t .    # 211 tests, no network, no GPU
nix flake check                               # same suite, sandboxed
```

## Controls

The dashboard is a single page with a state badge, session id, uptime and log viewer.

| Control | Effect |
| --- | --- |
| **Start** | Runs the full sequence: Monado → Weston (VNC) → websockify → pose client → Steam → game. `409` if a session is already running. |
| **Stop** | Tears the session down; idempotent. |
| **mode** | Switches the pose client: `hold`, `yaw`, `trigger`, `dodge`, `walk`, `clock`, `manual`. |
| **kill** | Force-stops the game or Steam specifically. |
| **preview** | VNC stream with PAM login; view-only. The stream is 1280x720 at a target 30 fps; the dashboard crops the top 32 px status panel, so the display area is 1280x688. Manual mode still uses pointer lock. |

In `manual` mode, click the preview to pointer-lock the browser and drive the hidden game: mouse look, WASD move, arrows move the active hand, wheel reaches, F throws a one-shot punch with the active hand, LMB/T fires, RMB/G grips, Tab switches hands. Esc releases the mouse; input stops immediately, while the last pose keeps streaming. The browser sends batches to `POST /api/manual/input`, which forwards them to the manual pose client over a per-session Unix datagram socket.

## Safety notes

- **This launches Steam and Superhot on your machine**, in a nested Weston session with a NULL-compositor Monado, so it does not draw on your desktop. Your mouse and keyboard focus stay yours until you click the preview in `manual` mode to capture the pointer; Esc returns them.
- **The preview stream is loopback-only.** Weston's VNC backend listens on `127.0.0.1:5901` and websockify on `127.0.0.1:6080`; noVNC assets are served by the dashboard, and credentials are not stored.
- **The kill buttons are force kills.** They terminate *your* Steam and the game process. Anything you had unsaved in other Steam games does not survive a `kill steam`.
- **One session at a time** by design; concurrent starts are guarded and rejected.
- **It does not modify your OpenXR configuration.** It validates that the runtime pointer exists and is readable, and fails preflight with a message if it does not.
- **No game files are included.** Superhot VR is commercial software you must own.
- Cleanup is best-effort and verified rather than assumed: after a stop, any surviving process is logged and surfaced in `/api/state`.

### VNC and PAM

The VNC viewer authenticates through this NixOS PAM service:

```nix
security.pam.services.weston-remote-access = {
  text = ''
    auth sufficient pam_unix.so likeauth try_first_pass
    auth required pam_deny.so
    account required pam_unix.so
  '';
};
```

VNC TLS is disabled: the stream has no confidentiality or server authentication, so keep it on loopback. The dashboard control API has no authentication either; keep `dashboard.host` on loopback and do not expose it.

## Repo map

| Path | What it is |
| --- | --- |
| `superbot/` | **The app.** Config, process-group management, session orchestrator, VNC preview transport, dashboard HTTP API and UI. |
| `tests/` | 211 unittest cases, standard library only: `python3 -m unittest discover -s tests -t .` |
| `spike/` | **Evidence.** `c0/` pose client and launchers, `e1/`–`e3/` OpenXR/capture/latency spikes, `c3/` and `wbc/` controller harnesses. |
| `research/` | **Findings.** Per-spike results with measured numbers, plus controller and game-side surveys. |
| `docs/` | **Design.** The design doc, its builder-check, the state-of-findings summary, and the dashboard spec (historical for the PNG preview; the current preview is VNC). |
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
