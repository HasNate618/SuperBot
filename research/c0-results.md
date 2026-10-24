# C0 — Superhot VR accepts fabricated HMD poses

**Date:** 2026-10-24 (host clock) · **Status:** core premise **PASS**; 3 of 6 planned measurements not run.

This is the gate the whole artifact depends on. Everything before it (E1–E3, C3a–C3m) tested the body and the pipe with `hello_xr` or with no game at all. C0 asks whether a **commercial VR game** will render its view from poses a humanoid robot produced, with no headset and no controllers in the room.

**Answer: yes.** Superhot VR reached its tutorial, driven by the G1's measured eye height and wrist positions, with the view verifiably following scripted pose changes.

## 0. Scorecard

| # | Measurement | Result |
| --- | --- | --- |
| 1 | Game binds to our runtime | **PASS** — session reached `FOCUSED`, system `Monado: Remote HMD`, 58 runtime extensions |
| 2 | View follows our poses | **PASS** — controlled A/B: 4.74 / 4.0% static vs 41.97 / 59.3% sweeping; operator saw the camera sweep |
| 3 | Scripted trigger reaches the game | **not run** — needs a pistol in hand |
| 4 | grim captures real game pixels | **PASS** — mean RGB ≈ (175, 188, 198), 41% bright, 3175 distinct colours |
| 5 | Clock floor in practice | **not run** — needs hold-vs-walk inside gameplay |
| 6 | The Fall's pistol/exit at 0.70 m | **not run** — needs level 1 |

## 1. What was actually run

- **Runtime:** Monado `remote` driver (`monado-psvr2-psvr2-work-steamvr-2026-06-04`), launched foreground from `spike/c0/launch_monado.sh` with `P_OVERRIDE_ACTIVE_CONFIG=remote`, `XRT_COMPOSITOR_COMPUTE=false`, `VK_DRIVER_FILES=…/nvidia_icd.json`, `IPC_EXIT_ON_DISCONNECT=off`, `XRT_NO_STDIN=1`. TCP 4242 listening; log `/tmp/c0-monado.log`.
- **Pose source:** `spike/c0/c0_client.py`, 376-byte `mndrmt3` packets at 50 Hz. **Real G1 geometry from E3**, not E1's placeholders: HMD eye height **1.221 m**, wrists at **(∓0.222, 0.721, 0.017) m**.
- **Game:** `/home/nate/Games/SUPERHOT VR/SUPERHOT_VR.exe` (Unity **2022.3.62f3**, D3D11, RTX 4060), launched by the Steam client as a non-Steam shortcut, `AppId=4249504744`, Proton **`cachyos-11.0-20260703-slr`** through `SteamLinuxRuntime_4` (pressure-vessel).
- **Displays:** game fullscreen **2048×1280** on the laptop panel `eDP-1` (2560×1600 @ scale 1.25); Monado presentation window 1920×1080 at (127, 199); the operator's working monitor `DP-1` was never touched.
- **Evidence:** game log `…/compatdata/4249504744/pfx/drive_c/users/steamuser/AppData/LocalLow/SUPERHOT_Team/SUPERHOT_VR/Player.log`; captures `/tmp/c0-shot1.png` (before), `/tmp/c0-shot2.png` (after), `/tmp/yaw_0*.png`, `/tmp/holdA_*.png`; client logs `/tmp/c0-client-hold.log`, `/tmp/c0-client-yaw.log`.

## 2. Three faults, in the order they bit

The interesting part, because most of it is invisible from the game's error message ("waiting for VR device").

### 2.1 The active-runtime pointer is invisible inside the container

Host `xrCreateInstance` worked; the game's did not:

```text
xrEnumerateInstanceExtensionProperties: RuntimeManifestFile::FindManifestFiles
  - failed to determine active runtime file path for this environment
xrizer::clientcore: EnumeratingExtensionsFailed(ERROR_RUNTIME_UNAVAILABLE)
```

The host has `/etc/xdg/openxr/1/active_runtime.json` → Monado. Inside pressure-vessel, **`/etc/xdg` is the container's own** and has no such file. The loader's user-level fallback is `$XDG_CONFIG_HOME/openxr/1/active_runtime.json` (i.e. `~/.config/openxr/1/`), and home *is* bind-mounted through.

**A wrong guess along the way, recorded because it is a natural one:** creating `~/.local/share/openxr/1/active_runtime.json` does nothing — that path is not searched. Verified from the game's own mount namespace that `/nix`, `/run/current-system` and `/home/nate/.config/…` are all visible while `/etc/xdg/openxr/1/active_runtime.json` is absent.

**Fix:** `~/.config/openxr/1/active_runtime.json` = copy of the Monado manifest.

### 2.2 The loader caches its runtime list per process

Creating the file while the game was running changed nothing. The game kept retrying in a loop (`WaitForDisplaySubsystem` → `Loading plugin UnityOpenXR` → `XR_ERROR_RUNTIME_UNAVAILABLE`) roughly every 0.7 s for four minutes. The OpenXR loader enumerates runtime manifests **once, at process start**, and caches the result; `xrCreateInstance` cannot heal a stale scan.

**Consequence:** the fix requires a game restart. There is no in-process recovery, and this is worth knowing before blaming the runtime for a failure that has already been fixed on disk.

### 2.3 Under Wine the *Windows* loader is in the path, via a shim

The game's `Plugins/x86_64/openxr_loader.dll` is the Windows loader. It does not read `~/.config`; it reads the prefix registry:

```text
[Software\\Khronos\\OpenXR\\1]
"ActiveRuntime"="C:\\openxr\\wineopenxr64.json"
```

which resolves to `C:\windows\system32\wineopenxr.dll`, a symlink into the Proton build. That shim forwards Windows OpenXR calls to the **host Linux loader**, which then does read `~/.config/openxr/1/active_runtime.json`. Both halves were verified present before the successful launch. So the chain is:

```text
game (Windows loader) → prefix registry → wineopenxr.dll → host Linux loader → Monado
```

## 3. Measurement 1 — the game binds to our runtime

From the game's own log, on the first launch after the fix:

```text
System Properties: Name="Monado: Remote HMD", VendorId=42
XrEventDataSessionStateChanged: XR_SESSION_STATE_UNKNOWN  -> IDLE
XrEventDataSessionStateChanged: XR_SESSION_STATE_IDLE     -> READY
XrEventDataSessionStateChanged: XR_SESSION_STATE_READY    -> SYNCHRONIZED
XrEventDataSessionStateChanged: XR_SESSION_STATE_SYNCHRONIZED -> VISIBLE
XrEventDataSessionStateChanged: XR_SESSION_STATE_VISIBLE  -> FOCUSED
VrSystem: Monado: Remote HMD
Detected Device: OpenXR Display
```

It enumerated **58 runtime extensions** (including `XR_KHR_vulkan_enable2`, `XR_KHR_opengl_enable`, `XR_EXT_hand_tracking`, `XR_FB_display_refresh_rate`). Monado's side logged haptics being pushed to `Remote Left/Right Controller` and answering `set_output is not implemented` — harmless, the remote driver has no haptics.

The game then ran its boot flow (`piOSBootFlowControl:PrepareNextScene(DeepLinkLauncher)` → `MindControlWord` → `LevelTimeline`), i.e. the actual Superhot intro, and reached the tutorial.

## 4. Measurement 2 — the view follows our poses

A/B under identical workload, 3.4 s apart, same scene, same session. **Only the pose differs.**

| Pose | Mean absolute pixel change | Pixels changed by >25 |
| --- | --- | --- |
| Static (`--mode hold`) | 4.74 | 4.0% |
| Sweeping (`--mode yaw --yaw-deg 35`) | **41.97** | **59.3%** |

**9× the mean change, 15× the changed area**, from a pose change alone. Scene animation alone accounts for the 4.74 baseline. The operator independently watched the camera sweep and confirmed it.

## 5. Measurement 4 — there are real pixels, and they were black before

| Capture | Mean RGB | Near-black | Bright | Distinct colours |
| --- | --- | --- | --- | --- |
| Before the fix (waiting screen) | (0.2, 0.2, 0.2) | **99.89%** | 0.06% | 32 |
| After the fix (tutorial) | (174.7, 188.3, 198.0) | **0.00%** | 41.2% | 3175 |
| Monado window region, after | (176.8, 190.5, 200.0) | 0.00% | 43.1% | 2245 |

Worth recording: **before the fix the game was still rendering at ~236 KB/s of log** with `VrFog:OnPreRender()` every frame and `Camera.GetStereoProjectionMatrix is returning an invalid value`. A game that logs like it is drawing, and a capture that is 99.89% black, is a specific and misleading failure shape.

## 6. The "not responding" dialog is Hyprland's, not Monado's

Hyprland raised an **"Application Not Responding"** dialog for the Monado window. Owner confirmed as pid-level `.hyprland-dialog`, i.e. the compositor's own dialog.

Monado was not hung: **~1% CPU**, presenting frames, and its log is full of

```text
WARN [renderer_wait_for_present] Compositor probably missed frame by 1.60ms
```

It blocks in `renderer_wait_for_present`, so it does not service Wayland's liveness ping in time. This is a symptom of Monado *presenting*, not of a stall. Dismiss by killing the window titled "Application Not Responding"; it will recur.

## 7. What this proves, and what it does not

**Proves:** a commercial VR game, unmodified, under Proton, accepts a headset that does not exist; renders from its pose; shows real content in a capturable surface; and its view tracks pose changes with a 9× signal over scene animation.

**Does not prove:**

- That a **robot** is driving it. The poses in this run are **scripted constants** built from G1 geometry measured in E3 (1.221 m eye height, measured wrist offsets). The sim→packet link was proven separately in E3 (1001 packets / 20.02 s, max position error **0.0**), but C0 did not run MuJoCo in the loop.
- That input works. The trigger was never fired (measurement 3).
- That the clock floor behaves as the assets claim (measurement 5).
- That the body can reach The Fall's pistol or exit at 0.70 m (measurement 6) — the known-hard question, since the reliable reach envelope is ~0.20 m.
- Generality: one game, one Proton build, one HMD pose.

## 8. Machine-level consequence

This fix is not Superhot-specific. Any Proton/Wine VR title on this machine will now find Monado with no per-game launch options, because the pointer lives in `~/.config/openxr/1/active_runtime.json` where the loader looks, and wineopenxr forwards to it.

## 9. Reproduce

```bash
# 1. user-level runtime pointer (the fix)
mkdir -p ~/.config/openxr/1
cp /etc/xdg/openxr/1/active_runtime.json ~/.config/openxr/1/active_runtime.json

# 2. Monado with the corrected env (see spike/c0/launch_monado.sh)
systemctl --user stop monado.socket      # systemd's unit owns the IPC path we need
./spike/c0/launch_monado.sh &            # expects: TCP 4242 listening

# 3. poses (real G1 geometry)
python3 spike/c0/c0_client.py --mode hold  --duration 900 &
python3 spike/c0/c0_client.py --mode yaw --yaw-deg 35 --duration 120   # pose A/B

# 4. launch from the Steam client, NOT the CLI (see 10)
# 5. capture and measure
grim -o <output> /tmp/shot.png
```

## 10. Steam launch from the CLI does not work here

Recorded so nobody spends an hour on it again:

- `steam -applaunch 4249504744` → Steam resolves the shortcut ID to a **store** AppID (`4869096`) and fails `AppError_9` (licensing). Non-Steam shortcut IDs are not store apps.
- `steam steam://rungameid/4249504744` → `steamid.cpp: Unknown GameID type`.
- Running the Proton entry point by hand
  (`SteamLinuxRuntime_4/_v2-entry-point … proton waitforexitandrun`) → `bwrap: execvp true: No such file or directory`, because pressure-vessel expects `/usr/bin/true` from a conventional host, which only exists inside Steam's own fhsenv.
- Direct `proton …` → its Python cannot find `libvulkan.so.1` outside the runtime.

**The launch must come from the Steam client UI.** Everything after the launch can be automated.

## 11. State left behind

- `monado-service` running detached (`/tmp/c0-monado.log`), holding the IPC socket.
- `monado.socket` **stopped** — it must stay stopped while our service owns the path. A reboot restores it; to restore by hand, stop our service first, then `systemctl --user start monado.socket`.
- `~/.config/openxr/1/active_runtime.json` created (the fix; keep).
- `~/.local/share/openxr/1/active_runtime.json` created earlier — **inert**, not a searched path; safe to delete.
- Superhot still running in the tutorial on `eDP-1`; the pose client is `--mode hold`.
- No window rules were installed (Hyprland 0.56 rejects `windowrulev2` via `keyword`; it needs `eval`). The Monado window was placed with an explicit dispatch instead.
