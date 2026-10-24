# E2 Spike Results — NVIDIA compositor, pixel capture, pose→pixel latency

**Status: PASS (core), with two failed capture paths recorded.**

E2 proves the important thing: on the real NVIDIA GPU (not the Intel/null
compositor used in E1), an injected HMD pose visibly changes the rendered
stereo view, and that view can be captured at well above 10 Hz. The debug-GUI
mirror is dead on this setup; the desktop-window capture path works.

## Environment

- GPU: `NVIDIA GeForce RTX 4060 Laptop GPU`, driver `610.57.04`, 8188 MiB
- `/dev/nvidia0` present
- Session: Hyprland on Wayland; `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-1`
- Monado service: `/nix/store/89h2h18iz10aad69jpa2wvxmlhlg6k5i-monado-psvr2-psvr2-work-steamvr-2026-06-04/bin/monado-service`
- Runtime JSON: `/run/current-system/sw/share/openxr/1/openxr_monado.json`
- App: `/nix/store/4z8ibn91k9r6yismm2nrcnl19b1v9rp7-openxr-loader-1.1.54/bin/hello_xr -g Vulkan2`
- Displays: `eDP-1` 2560x1600 (scene capture), `DP-1` 3840x2160

### NVIDIA ICD path

The task's suggested path
`/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.x86_64.json` **does not exist**
on this machine. The real file is:

```
/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.json
  -> /nix/store/a6pwi640iza2yr0fkj0a3vdaq1dacfn8-nvidia-x11-610.57.04/share/vulkan/icd.d/nvidia_icd.json
```

The installed `monado.service` Environment still points at the missing
`.x86_64.json` path. The E2 transient unit overrode `VK_DRIVER_FILES` to the
real `nvidia_icd.json`; the installed unit was not modified.

`vulkaninfo` is not on PATH in this shell (it lives at
`/nix/store/raya468h30s65baj7h97aqbki7bwx4wk-vulkan-tools-1.4.350.0/bin/vulkaninfo`).
Using the real ICD confirms the GPU:

```
GPU0:
    deviceName = NVIDIA GeForce RTX 4060 Laptop GPU
    driverName = NVIDIA
    driverInfo = 610.57.04
```

Using the task's `.x86_64.json` path fails:

```
ERROR: [Loader Message] Code 0 : loader_get_json: Failed to open JSON file
  /run/opengl-driver/share/vulkan/icd.d/nvidia_icd.x86_64.json
ERROR: [Loader Message] Code 0 : vkCreateInstance: Found no drivers!
```

The Monado log independently confirms the selection: `Selected GPU: 0`,
`name: NVIDIA GeForce RTX 4060 Laptop GPU`,
`deviceType: VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU`.

Evidence: `spike/e2/evidence/vulkaninfo-nvidia.txt`,
`spike/e2/evidence/vulkaninfo-wrong-icd-path.txt`.

## Launch

Transient unit only; `~/.config/monado/config_v0.json` was never created and the
installed unit's `Environment=` was not changed.

```bash
systemd-run --user --unit=e2-monado --collect \
  -p Environment=IPC_EXIT_ON_DISCONNECT=off \
  -p Environment=XRT_NO_STDIN=1 \
  -p Environment=P_OVERRIDE_ACTIVE_CONFIG=remote \
  -p Environment=XRT_COMPOSITOR_COMPUTE=false \
  -p Environment=XRT_COMPOSITOR_LOG=debug \
  -p Environment=XRT_LOG=debug \
  -p Environment=REMOTE_LOG=debug \
  -p Environment=VK_DRIVER_FILES=/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.json \
  /run/wrappers/bin/monado-service
```

`XRT_COMPOSITOR_COMPUTE=false` is the key: it selects the **graphics/render
compositor**. Leaving the variable unset selects the compute compositor, which
crashes here.

## Capture-path results

### (a) Debug-GUI mirror — FAILED

Attempt: `XRT_DEBUG_GUI=1` with `XRT_COMPOSITOR_COMPUTE=true`.

The service died with SIGSEGV before the GUI could be used. The log shows the
Wayland presentation check failing first:

```
ERROR [comp_target_swapchain_create_images] Queue family (index: 2) does not support presentation ops for wayland surfaces.
ERROR [renderer_acquire_swapchain_image] comp_target_acquire: VK_ERROR_INITIALIZATION_FAILED
...
Stack trace ... comp_renderer_draw / compositor_layer_commit
... libnvidia-glcore.so.610.57.04 ...
... SDL_IOReady / Wayland_CursorThreadFunc (libSDL3.so.0) ...
e2-monado.service: Main process exited, code=dumped, status=11/SEGV
```

Evidence: `spike/e2/evidence/crash-debug-gui.txt`.

### (b) Desktop window capture — WORKED

Attempt: no `XRT_DEBUG_GUI`; `XRT_COMPOSITOR_COMPUTE=false`.

The service stayed active, listened on `0.0.0.0:4242`, selected the NVIDIA GPU,
created a `GRAPHICS` queue, created a 1920x1080 Wayland swapchain, and mapped a
`Monado` window (`class: openxr`). `hello_xr` rendered its stereo test scene into
that window, and `grim` captured it from the Hyprland desktop.

Key log lines:

```
Using builder remote: Remote simulation devices builder
Listen address 0.0.0.0 on port 4242
Selected GPU: 0
name: NVIDIA GeForce RTX 4060 Laptop GPU
Created device and GRAPHICS queue with QUEUE_GLOBAL_PRIORITY_REALTIME.
Target backend wayland initialized!
VkSwapchainCreateInfoKHR ... imageExtent: {1920, 1080}
```

Evidence: `spike/e2/evidence/working-run-compute.txt`,
`spike/e2/evidence/scene_plus25.png`, `spike/e2/evidence/scene_minus25.png`,
`spike/e2/evidence/yaw_sweep.mp4`.

Note: the log still prints
`WARN [comp_window_peek_create] Peek window cannot be enabled on compute compositor`
even in the working `GRAPHICS`-queue run. Treat that string as a misleading log
line in this fork; the device queue and successful swapchain show the graphics
compositor is what is running. The window is capturable regardless.

### (c) Swapchain readback — NOT NEEDED

The desktop-window path produced clean stereo pixels, so swapchain readback was
not exercised.

### Compute compositor (no `XRT_COMPOSITOR_COMPUTE`) — FAILED

For completeness: omitting `XRT_COMPOSITOR_COMPUTE` selected the compute
compositor (`Created device and COMPUTE queue`), hit the same Wayland
presentation queue-family error, and died with SIGSEGV.

Evidence: `spike/e2/evidence/crash-compute-unset-part1.txt`,
`spike/e2/evidence/crash-compute-unset-part2.txt`.

## Pose injection

`spike/e2/remote_pose_client_e2.py` is the E1 client plus per-packet CSV logging
and a square-wave yaw mode. It sends the same 376-byte `mndrmt3` packets at
120 Hz.

Measurement run:

```
--mode step --step-period 2.0 --yaw-deg 25 --hz 120 --duration 26
```

So the head yaw alternates between +25° and -25° every 2 seconds. Each packet's
send wall time and yaw are logged (`evidence/pose-measurement.csv`).

The stereo view visibly shifts between the two yaw extremes. The two full-screen
captures (`scene_plus25.png`, `scene_minus25.png`) show the cube cluster moving;
the tracked blue face's centroid alternates 610↔577 px and its blue area
7830↔9139 px, consistently with the yaw sign. A 60 fps clip of the sweep is in
`yaw_sweep.mp4` (238 frames / 3.95 s, 2560x1600).

## Capture rate and latency

Capture: `grim -g "0,400 640x400"` on `eDP-1` (800x500 physical output), looping
for 12.014 s.

```
443 frames / 12.014 s = 36.88 fps
```

That is comfortably above the required 10 Hz. (`wf-recorder` also recorded the
full output at 60 fps for the clip.)

Latency method: decode each grim PNG, track the blue face's centroid/area, and
for each pose step find the first frame after the step's send wall time whose
image shows the new yaw. The frame is bracketed by its capture start and end, so
the result is a range: `[t_start - t_step, t_end - t_step]`. The capture itself
costs about 27 ms per frame, so the true pose→pixel latency is near the lower
bound; the upper bound is the defensible "first captured frame" number.

| # | step wall time | yaw | first changed frame | latency (lower..upper) | mid |
|---|----------------|-----|---------------------|------------------------|-----|
| 1 | 1792733081.565508 | +25° | f0042 | 30.4..59.9 ms | 45.1 ms |
| 2 | 1792733083.569956 | -25° | f0121 | 26.5..58.7 ms | 42.6 ms |
| 3 | 1792733085.564065 | +25° | f0196 | 29.1..52.2 ms | 40.7 ms |
| 4 | 1792733087.565251 | -25° | f0265 | 13.4..50.0 ms | 31.7 ms |
| 5 | 1792733089.563356 | +25° | f0337 | 38.7..65.0 ms | 51.9 ms |
| 6 | 1792733091.569134 | -25° | f0409 | 40.7..68.6 ms | 54.6 ms |

Summary:

- Lower-bound average: **29.8 ms**
- Midpoint average: **44.4 ms**
- Upper-bound average: **59.1 ms** (worst 68.6 ms)
- Capture rate: **36.9 fps** (443 frames in 12.014 s)

So: injected pose → captured pixel change is roughly **30–59 ms** on this
NVIDIA/Hyprland setup, with the capture overhead included in the upper bound.
The true compositor/display latency is likely in the low-to-mid 30s of ms.

## Evidence files

All under `spike/e2/evidence/`:

- `working-run-compute.txt` — successful graphics-compositor run
- `crash-debug-gui.txt` — debug-GUI SIGSEGV stack
- `crash-compute-unset-part1.txt`, `crash-compute-unset-part2.txt` — compute-compositor crash
- `scene_plus25.png`, `scene_minus25.png` — yaw-extreme stereo captures
- `yaw_sweep.mp4` — 60 fps sweep clip
- `pose-measurement.csv`, `client-measurement.log` — pose log for the latency run
- `frames.csv`, `frame_metrics.csv`, `analysis.txt` — capture timestamps, per-frame metrics, full analysis
- `nvidia-smi.txt`, `icd-dir.txt`, `launch-command.txt`
- `vulkaninfo-nvidia.txt`, `vulkaninfo-wrong-icd-path.txt`
- `e2-monado-journal.txt` — full journal (all attempts)
- `hypr-clients.json` — window geometry during the run

## Restore state

- `e2-monado` stopped and removed (`status` reports the transient unit not found)
- `monado.socket` active; `monado.service` inactive
- `~/.config/monado/config_v0.json` absent
- Installed `monado.service` Environment= untouched

## Conclusion

E2 passes the core requirement. The compositor ran on the NVIDIA RTX 4060 with
the real `nvidia_icd.json`; injected yaw poses visibly changed the rendered
stereo view; the view was captured at 36.9 fps; and pose→pixel latency measured
at roughly 30–59 ms (mid ~44 ms) including capture overhead.

Two caveats for future work:

1. **Debug GUI is not usable here.** `XRT_DEBUG_GUI=1` with the compute
   compositor crashes at the Wayland presentation queue-family check.
2. **The compute compositor is not usable here.** It crashes the same way even
   without the GUI. `XRT_COMPOSITOR_COMPUTE=false` (graphics/render compositor)
   is the working configuration on this NVIDIA/Wayland machine.

The `Peek window cannot be enabled on compute compositor` warning appears even in
the working graphics run and should not be read as "the window is dead"; the
`Monado` window does display and capture the app's stereo output.
