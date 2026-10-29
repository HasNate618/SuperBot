# E3 Spike Results — simulated G1 body drives Monado remote HMD/controllers

**Date:** 2026-10-23
**Agent:** OpenCode `e3-body`
**Verdict: PASS (honest body path), with recorded IK/neck/locomotion caveats.**

A Unitree G1 in MuJoCo was stepped at 500 Hz, its **achieved** `torso_link`/`head_link` pose was sent as the HMD and its **achieved** wrist poses as controllers over the E1/E2 `mndrmt3` TCP protocol. `hello_xr` rendered the resulting view; screenshots at different scripted body phases show the view changing. No desired IK target or scripted oscillator was ever sent to Monado.

## 1. What was measured

- **[MEASURED]** GPU: NVIDIA GeForce RTX 4060 Laptop GPU, driver 610.57.04, `/dev/nvidia0` present.
- **[MEASURED]** Monado selected the NVIDIA GPU, created a **GRAPHICS** queue (`XRT_COMPOSITOR_COMPUTE=false`), and presented a 1920x1080 Wayland window.
- **[MEASURED]** G1 model loaded from MuJoCo Menagerie `unitree_g1/scene.xml` (commit `cda5236f98383f6fbaa4820c6b18dba892cd087f`), 29 actuated joints, no neck.
- **[MEASURED]** The G1 stood under the model's position actuators (keyframe `stand`, PD at sim rate) for the full run. Base height stayed in `0.7905..0.7916 m`; max horizontal base excursion was `3.08 cm` in x and `0.59 cm` in y. It did not fall.
- **[MEASURED]** `body_bridge.py` sent **1001 packets in 20.02 s** (50 Hz) to `127.0.0.1:4242`.
- **[MEASURED]** Packet head/wrist **positions equal the MuJoCo `mjData` positions exactly** after the documented frame conversion (`max error 0.0 m`), and packet quaternions equal the converted `mjData` quaternions (`max error 0.0`).
- **[MEASURED]** Packet HMD yaw tracked torso yaw with `max |error| = 0.0108°`. Packet yaw spanned `-35.84°..+36.02°`, produced by waist-yaw motion plus small base reaction.
- **[MEASURED]** `hello_xr` rendered a stereo scene from the remote devices (`Head: 'Remote HMD'`, `Left/Right: 'Remote Left/Right Controller'`). The Monado window was `class: openxr`, 1920x1080 at `2624,341`; `grim` captured it at 2400x1350 (1.25 scale).
- **[MEASURED]** Pixel evidence: between `look_left` and `look_right`, the tracked blue-cube centroid in the left eye moved from `x=550.8` to `x=665.1` (**~114 px**). Reach and punch phase pairs changed 5.9–6.9% of pixels by >10 levels.
- **[MEASURED]** IK residuals: reach **4.3 cm**, punch **13.5 cm**. The arms are visibly moving but not accurately placed. This is an allowed "IK ugly" result, not a hard fail.

## 2. What was not used / caveats

- **Policy: NOT used.** No `g1_policy.onnx`, no locomotion, no walking. The robot stands with the Menagerie position actuators and the scripted commander moves arms/waist. The task explicitly allows standing + scripted arms as a pass.
- **No neck.** The G1 has no neck joint, so HMD orientation is the **torso orientation**. HMD position uses the `head_link` mesh geom's world position (part of `torso_link`), which is ~`1.221 m` in the stand pose. Head orientation cannot be decoupled from the torso; this is a real body limitation.
- **IK is raw DLS, not mink.** `mink 1.3.0` was installed successfully, but the bridge uses `mj_jacBody` + damped least squares restricted to the 7 arm DoFs per side. This guarantees legs/base are not IK targets and keeps the code small.
- **No force feedback.** The game/VLM does not push the body; E3 only sends body poses out.
- **Compute compositor / debug GUI** were not retested; E2 already recorded that both SIGSEGV on this NVIDIA/Wayland setup. E3 uses the E2 working graphics-compositor path.

## 3. Environment / how to reproduce

All throwaway code and assets are under `spike/e3/`; nothing was installed into the user profile.

### Python environment

```bash
cd /home/nate/Projects/SuperBot/spike/e3
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python 'mujoco==3.17.0' 'numpy<2.3' mink imageio
```

Installed/measured versions: `mujoco 3.17.0`, `numpy 2.2.6`, `scipy 1.18.1`, `mink 1.3.0`, `imageio 2.38.1` / Pillow.

### Model

```bash
cd spike/e3/vendor
git clone --depth 1 https://github.com/google-deepmind/mujoco_menagerie.git
# model: spike/e3/vendor/mujoco_menagerie/unitree_g1/scene.xml
```

The old `/tmp/menagerie/unitree_g1` stub was empty and was not used.

### Offscreen sim rendering

MuJoCo EGL needed the 64-bit libglvnd EGL library explicitly:

```bash
export LD_LIBRARY_PATH=/nix/store/v8x5c24y4zgxv5xmwhz5lz26ir816c31-libglvnd-1.7.0/lib:/run/opengl-driver/lib
export LD_PRELOAD=/nix/store/v8x5c24y4zgxv5xmwhz5lz26ir816c31-libglvnd-1.7.0/lib/libEGL.so.1
export PYOPENGL_PLATFORM=egl MUJOCO_GL=egl MUJOCO_EGL_DEVICE_ID=0
```

Without those, PyOpenGL fails with `'NoneType' object has no attribute 'eglQueryString'`.

### Monado

```bash
systemctl --user stop monado.service monado.socket
rm -f /run/user/1000/monado_comp_ipc
systemd-run --user --unit=e3-monado --collect \
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

Key log lines observed:

```
Listen address 0.0.0.0 on port 4242
Selected GPU: 0
name: NVIDIA GeForce RTX 4060 Laptop GPU
Created device and GRAPHICS queue with QUEUE_GLOBAL_PRIORITY_REALTIME.
Target backend wayland initialized!
imageExtent: {1920, 1080}
```

`XRT_COMPOSITOR_COMPUTE=false` is required. The `Peek window cannot be enabled on compute compositor` warning still prints in the working GRAPHICS run; it is misleading in this fork.

### hello_xr

```bash
nohup setsid bash -c 'sleep 300 | exec /nix/store/4z8ibn91k9r6yismm2nrcnl19b1v9rp7-openxr-loader-1.1.54/bin/hello_xr -g Vulkan2' \
  > evidence/hello_xr.log 2>&1 < /dev/null &
```

`XR_RUNTIME_JSON=/run/current-system/sw/share/openxr/1/openxr_monado.json` was set in the environment. The held-open pipe is required because `hello_xr` exits on stdin EOF.

### Bridge

```bash
cd spike/e3
.venv/bin/python body_bridge.py \
  --duration 20 --hz 50 \
  --log evidence/bridge.csv \
  --simshots-dir evidence
```

For a network-free sim test: `--no-net`.

### Capture

The Monado window was found with `hyprctl clients -j` and captured with:

```bash
grim -g "2624,341 1920x1080" evidence/hello_look_left.png
```

## 4. Frame conversion (MuJoCo -> OpenXR)

MuJoCo is Z-up (`x` forward, `y` left, `z` up), quaternions are `wxyz`. OpenXR is Y-up (`x` right, `y` up, `z` backward), quaternions are `xyzw`.

Position:

```
xr_x = -mj_y
xr_y =  mj_z
xr_z = -mj_x
```

Quaternion (active rotation alignment):

```
q_align_xyzw = (-0.5, +0.5, +0.5, +0.5)
q_mj_xyzw    = (q_mj.wxyz[1], [2], [3], [0])
q_xr_xyzw    = q_align * q_mj * conjugate(q_align)
```

Check: a MuJoCo `+0.6 rad` waist yaw about Z maps to an OpenXR `+0.6 rad` yaw about Y. The live CSV confirms the packet yaw matches the torso yaw to `0.0108°`.

Controller poses use the actual `left_wrist_yaw_link` / `right_wrist_yaw_link` body world poses; no hand offset was added.

## 5. Honesty proof (the important part)

The bridge runs:

1. `mujoco.mj_step` for 10 sim steps (500 Hz, `sim_dt=0.002`).
2. At 50 Hz, computes rate-limited arm joint targets from IK.
3. Reads `d.body(...).xpos/xquat` and `d.geom_xpos[head_gid]` **after the step**.
4. Converts those achieved poses to OpenXR and writes the 376-byte packet.

IK targets are stored only in `q_target`/`d.ctrl`; they are never placed in the packet. The CSV logs both sides:

```
mj_head_x/y/z          packet_head_x/y/z          head_pos_err_m
mj_left_x/y/z          packet_left_x/y/z          left_pos_err_m
mj_right_x/y/z         packet_right_x/y/z         right_pos_err_m
mj_torso_yaw_deg       packet_yaw_deg             yaw_err_deg
head_quat_err          left_quat_err              right_quat_err
left_ik_residual_m     right_ik_residual_m
```

Live 20 s run, 1001 rows (`evidence/bridge.csv`):

| phase | rows | torso yaw mean | packet yaw mean | max yaw err | head pos err | wrist pos err | quat err | IK residual |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| stand | 301 | -1.038° | -1.038° | 0.0001° | 0.0 m | 0.0 m | 0.0 | — |
| reach_left | 151 | -0.557° | -0.557° | 0.0005° | 0.0 m | 0.0 m | 0.0 | 0.043 m |
| reach_right | 150 | 0.002° | 0.002° | 0.0001° | 0.0 m | 0.0 m | 0.0 | 0.043 m |
| punch_left | 99 | -0.011° | -0.011° | 0.0003° | 0.0 m | 0.0 m | 0.0 | 0.135 m |
| punch_right | 100 | -0.112° | -0.112° | 0.0005° | 0.0 m | 0.0 m | 0.0 | 0.134 m |
| look_left | 100 | +31.383° | +31.381° | 0.0108° | 0.0 m | 0.0 m | 0.0 | — |
| look_right | 100 | -23.306° | -23.306° | 0.0008° | 0.0 m | 0.0 m | 0.0 | — |

The look-phase means include the rate-limited ramp; steady-state waist actual reached `+0.617 / -0.616 rad` (about ±35°), and packet yaw reached ±36°.

Achieved wrist positions (mean per phase, MuJoCo metres):

| phase | left wrist | right wrist |
|---|---|---|
| stand | `(-0.013, 0.220, 0.722)` | `(-0.020, -0.220, 0.722)` |
| reach_left | `(0.097, 0.371, 0.980)` | `(-0.017, -0.222, 0.724)` |
| reach_right | `(0.001, 0.237, 0.744)` | `(0.093, -0.374, 0.981)` |
| punch_left | `(0.231, 0.160, 0.919)` | `(0.014, -0.247, 0.752)` |
| punch_right | `(0.043, 0.213, 0.747)` | `(0.233, -0.161, 0.920)` |
| look_left | `(-0.127, 0.178, 0.723)` | `(0.148, -0.175, 0.747)` |
| look_right | `(0.070, 0.195, 0.722)` | `(-0.099, -0.184, 0.722)` |

These are achieved body poses, not desired targets.

## 6. Pixel evidence

Sim (offscreen EGL):

- `spike/e3/evidence/sim_stand.png`
- `sim_reach_left.png`, `sim_reach_right.png`
- `sim_punch_left.png`, `sim_punch_right.png`
- `sim_look_left.png`, `sim_look_right.png`

hello_xr window (`class: openxr`, 2400x1350 captured pixels):

- `spike/e3/evidence/hello_stand.png`
- `hello_reach_left.png`, `hello_reach_right.png`
- `hello_punch_left.png`, `hello_punch_right.png`
- `hello_look_left.png`, `hello_look_right.png`

Measured pixel changes:

- `hello_look_left.png` vs `hello_look_right.png`: blue-cube centroid `x=550.8 -> 665.1` in the left eye (~114 px); `1.8%` of pixels changed by >10 levels (sparse scene on a flat background).
- `hello_reach_left.png` vs `hello_reach_right.png`: `5.9%` of pixels changed by >10 levels.
- `hello_punch_left.png` vs `hello_punch_right.png`: `6.9%` of pixels changed by >10 levels.

The look-left/look-right screenshots visibly show the cube cluster yawed in opposite directions. The HMD view moved because the simulated waist moved, not because of a scripted camera.

## 7. Scripted commander

Timed sequence used:

```
stand 2 s
reach_left 3 s
reach_right 3 s
punch_left 2 s
punch_right 2 s
look_left 2 s       (waist yaw +0.6 rad)
look_right 2 s      (waist yaw -0.6 rad)
stand 2 s
```

- Arm IK at 50 Hz; joint targets rate-limited at 2.0 rad/s; PD actuators run at 500 Hz.
- Waist yaw target rate-limited at 2.0 rad/s.
- IK only writes the 7 arm DoFs of the requested side. Legs and floating base are never IK targets.
- The commander is a fixed script, not a VLM. No Superhot, no game logic, no VLM.

## 8. Errors / things that went wrong

- **Punch IK residual is large (13.5 cm).** The forward targets are near the arm's reachable envelope and the 2 rad/s rate limit plus joint limits prevent full extension. The arms still visibly punch; recorded as allowed IK ugliness.
- **No neck.** HMD orientation is the torso; looking left/right rotates the whole chest. Head position uses the `head_link` geom, but head orientation cannot be stabilized independently.
- **No locomotion policy.** Standing only. The task allowed this if the ONNX checkpoint was hard; no checkpoint was fetched. The base drifted by at most a few centimetres while the arms moved.
- **EGL setup is fiddly.** Needed the 64-bit libglvnd `libEGL.so.1` via `LD_PRELOAD`/`LD_LIBRARY_PATH`; the `/run/opengl-driver/lib` directory has `libEGL_mesa`/`libEGL_nvidia` but no plain `libEGL.so.1`.
- **hello_xr stdin.** It exits immediately on stdin EOF; use `sleep N | hello_xr` or an equivalent held-open pipe.
- **Transient unit cleanup.** At cleanup `e3-monado.service` was already not loaded; `monado.socket` was restarted and `monado.service` is inactive.
- **Misleading Monado warning.** `Peek window cannot be enabled on compute compositor` appears even in the working GRAPHICS-queue run; the GRAPHICS queue and successful 1920x1080 swapchain are the authoritative signals.
- **Operational gotcha:** `pkill -f hello_xr` can match the shell command itself and kill the shell; use `pkill -x hello_xr`.

## 9. Restore state

- `e3-monado`: not loaded/inactive.
- `monado.socket`: active; `monado.service`: inactive.
- `~/.config/monado/config_v0.json`: absent.
- Installed `/etc/systemd/user/monado.service` Environment was not modified; it still contains the original `XRT_COMPOSITOR_COMPUTE=true` and `VK_DRIVER_FILES=...nvidia_icd.x86_64.json`.
- `/run/user/1000/monado_comp_ipc` removed before restore.
- Throwaway code/assets intentionally left under `spike/e3/`; evidence is in `spike/e3/evidence/`.

## 10. What the VLM commander should plug into next

The next layer should send **intent**, not poses. Recommended interface:

```
{"action":"stand"}
{"action":"reach","side":"left"}
{"action":"punch","side":"right"}
{"action":"look","yaw_deg":+35}
{"action":"stop"}
```

The bridge should:

1. Translate the action into joint targets / IK targets **in the sim**.
2. Step MuJoCo at 500 Hz with PD at sim rate and IK at 20–50 Hz.
3. Read achieved torso/head/wrist poses **after** `mj_step`.
4. Send those achieved poses to Monado.

Rules for the next layer:

- Keep the 2 rad/s joint/waist rate limit; do not bolt unbounded arm IK onto a walking policy.
- If locomotion is added, legs stay on the policy; arms/waist get rate-limited residual control.
- Thumbstick later should be **achieved base velocity**, not desired velocity.
- Use slow, scripted/VLM-paced commands; this body is not a Beat Saber controller.
- Do not send desired IK targets or game-side poses to Monado; that would recreate the E1/E2 puppet failure and violate the E3 hard-fail rule.

## 11. Evidence index

All under `spike/e3/`:

- `body_bridge.py` — sim -> OpenXR bridge, DLS IK, scripted commander, CSV logger.
- `probe_sim.py`, `render_probe.py` — model/stand/render checks.
- `evidence/bridge.csv` — live 1001-packet honesty log.
- `evidence/bridge.log` — live phase timeline and packet count.
- `evidence/offline_bridge2.csv`, `offline_bridge2.log` — network-free validation run.
- `evidence/hello_*.png` — hello_xr pixels at stand/reach/punch/look phases.
- `evidence/sim_*.png` — MuJoCo body at the same phases.
- `evidence/hello_xr.log` — hello_xr device/session/swapchain log.
- `evidence/e3-monado-journal-live.txt` — full Monado journal (GPU, GRAPHICS queue, session).
- `evidence/sim_stand.png` — first standing sim screenshot.
- `vendor/mujoco_menagerie/unitree_g1/` — model + assets (Menagerie commit `cda5236...`).

## 12. Conclusion

E3 passes the core requirement: **the simulated G1 body, not a scripted pose generator, drove Monado.** The HMD pose came from the post-step `torso_link`/`head_link` state; the controller poses came from the post-step wrist bodies; `hello_xr` visibly yawed when the simulated waist yawed. The honesty CSV shows zero position/quaternion error between `mjData` and the packet, with yaw tracking to `0.0108°`.

The body is not a good humanoid yet: no neck, no locomotion policy, punch IK residual 13.5 cm, and reach residual 4.3 cm. Those are recorded, not hidden. The next useful step is a command layer that turns VLM intent into these sim-side actions while preserving the "packets only from achieved `mjData`" rule.
