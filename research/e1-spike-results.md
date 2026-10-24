# E1 spike results — Monado `remote` driver HMD pose injection

**Date:** 2026-10-23
**Agent:** OpenCode `e1-spike`
**Verdict: PASS (OpenXR view level).** A throwaway TCP client streamed a yaw-oscillating HMD pose into Monado's `remote` driver over TCP :4242, and an OpenXR client's `xrLocateViews` head pose followed it (±35° at 0.5 Hz, `XR_VIEW_STATE_ORIENTATION_VALID_BIT` set). No protocol/header/ABI rejection occurred.

**Caveat on the word "rendered":** this spike did **not** capture displayed pixels. The only usable GPU in this agent's environment was an Intel iGPU (not the RTX 4060), so Monado was run with `XRT_COMPOSITOR_NULL=1`; the compositor discards frames. Pose movement is measured at the OpenXR API, and a real rendering app (`hello_xr`) created a session and swapchains and ran frames against the same runtime. See §7 and §8.

---

## 1. Driver presence in the installed Monado

- **[MEASURED]** Runtime used:
  `/nix/store/89h2h18iz10aad69jpa2wvxmlhlg6k5i-monado-psvr2-psvr2-work-steamvr-2026-06-04`
- **[MEASURED]** Binary `.../bin/monado-service` is an ELF x86-64 PIE, 8,067,960 bytes, `sha256=48791d3f114fa221ece554967c455443cd1ae2c3b0701b100dcf04e76ae1be21`.
- **[MEASURED]** `.../lib/libmonado.so.25.1.0` is 215,448 bytes, `sha256=6403a048e527aa173b6a78508680e5676c3bfee01d45ab8af39f839b149c7cdd`.
- **[MEASURED]** Runtime JSON: `/run/current-system/sw/share/openxr/1/openxr_monado.json` points at that store path.
- **[MEASURED]** `monado-service` contains the remote driver built in. `strings` found:
  - `/build/source/src/xrt/drivers/remote/r_hub.c`
  - `mndrmt3`
  - `Protocol version mismatch (expected 0x%08lx, got 0x%08lx`
  - `remote_open_system`
  - `r_hub_system_devices_get_roles`
  - `r_hub_system_devices_destroy`
  - `Created remote`
- **[MEASURED]** The same strings are **absent** from `libmonado.so.25.1.0`; the remote driver is linked into the `monado-service` executable, not the client library.
- **[MEASURED]** Runtime reports `Monado(XRT) by Collabora et al 'GIT-NOTFOUND'`. The NixOS derivation name/version is `psvr2-work-steamvr-2026-06-04`; the NixOS module pins Beyley rev `47f9d39e686d6bbd7dad914b4dbe21f784c41911` (module source read, **[MEASURED]** locally; rev itself **[CITED]** from `/home/nate/Projects/nixos-config-public/modules/nixos/psvr2-vr.nix`).
- **[MEASURED]** Installed unit: `/etc/systemd/user/monado.service` → `/nix/store/6d5n1j0bkkjb01ir28yz32a6m22d198z-unit-monado.service/monado.service`; socket unit `/etc/systemd/user/monado.socket`. Unit sets `XRT_COMPOSITOR_COMPUTE=true`, `VK_DRIVER_FILES=/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.x86_64.json`, `IPC_EXIT_ON_DISCONNECT=off`, etc.

**Conclusion [MEASURED]:** this build does include the `remote` driver.

## 2. Activation

- **[MEASURED]** `~/.config/monado/config_v0.json` did **not** exist before or after the spike. No config file was written. `~/.config/monado/` is empty.
- **[MEASURED]** `P_OVERRIDE_ACTIVE_CONFIG=remote` was sufficient. Server log:
  ```
  Config selected remote
  Using builder remote: Remote simulation devices builder
  0: Remote HMD
  1: Remote Left Controller
  2: Remote Right Controller
  INFO [setup_accept_fd] Listen address 0.0.0.0 on port 4242
  ```
- **[MEASURED]** Because no `remote` JSON node existed, the builder logged `ERROR [u_config_json_get_remote_settings] No remote node` and fell back to defaults `port=4242`, `view_count=2`.
- **[CITED]** Fork source `src/xrt/targets/common/target_builder_remote.c` confirms the fallback defaults are `4242` / `2` and that the JSON `remote` node requires `version`, `port`, **and `view_count`** (`u_config_json_get_remote_settings`, `src/xrt/auxiliary/util/u_config_json.c`). The commonly quoted config `{"active":"remote","remote":{"version":0,"port":4242}}` is missing `view_count` for this fork; it still works only because the whole node fails and defaults are applied.
- **[MEASURED]** The TCP listener binds `0.0.0.0:4242` (all interfaces), with no authentication.

## 3. Wire protocol / ABI

- **[CITED]** `r_interface.h` at rev `47f9d39e…` defines `R_HEADER_VALUE = *(uint64_t *)"mndrmt3\0"`, `struct r_head_data`, `struct r_remote_controller_data`, and `struct r_remote_data`.
- **[MEASURED]** Header bytes `6d 6e 64 72 6d 74 33 00` decode to `0x0033746d72646e6d` on this little-endian host.
- **[MEASURED]** `ctypes` layout asserted in the client:
  - `r_head_data` = 128 bytes
  - `r_remote_controller_data` = 120 bytes
  - `r_remote_data` = **376 bytes** (`8 + 128 + 120 + 120`)
- **[MEASURED]** Pose field order is `struct xrt_pose { xrt_quat orientation; xrt_vec3 position; }`, confirmed in `xrt_defines.h` (`[CITED]` source) and validated empirically because the streamed quaternion produced the expected view yaw.
- **[CITED]** `r_hub.c` `run_thread()` accepts one connection, immediately writes two `r_remote_data` packets (`reset` and `latest`), then blocks reading 376-byte packets and assigns each to `r->latest`.
- **[MEASURED]** Client's handshake read both server packets; both carried header `0x0033746d72646e6d`.
- **[CITED] Important nuance:** the hub-side `read_one()` does **not** validate the incoming packet's `header` field; it copies the packet straight into `latest`. The `Protocol version mismatch` check lives in `r_remote_connection_read_one()`, i.e. the client-side helper used when *reading from a hub*. Therefore the magic header is a client-side version guard, not a server-side authentication/validation gate.
- **[MEASURED]** After the streaming client disconnected, the server logged `read_one: Disconnected!`, returned to `Listening on port '4242'.`, and the head pose stayed frozen at the last received packet (see §6).

## 4. Server run used for the test

Restored state at start: `monado.socket` active, `monado.service` inactive, no `config_v0.json`.

Command (transient unit so the user's unit/config was not modified):
```bash
systemd-run --user --unit=e1-monado --collect \
  -p Environment=IPC_EXIT_ON_DISCONNECT=off \
  -p Environment=XRT_NO_STDIN=1 \
  -p Environment=P_OVERRIDE_ACTIVE_CONFIG=remote \
  -p Environment=XRT_COMPOSITOR_NULL=1 \
  -p Environment=XRT_LOG=debug \
  -p Environment=REMOTE_LOG=debug \
  -p Environment=VK_DRIVER_FILES=/run/opengl-driver/share/vulkan/icd.d/intel_icd.x86_64.json \
  /run/wrappers/bin/monado-service
```

- **[MEASURED]** `XRT_NO_STDIN=1` was required for direct/transient launches. Without it, `monado-service` failed with `ERROR [init_epoll] epoll_ctl(stdin) failed '-1'` / `XRT_ERROR_IPC_MAINLOOP_FAILED_TO_INIT`, because the process got `/dev/null` on fd 0 and Monado tries to epoll stdin. **[ASSUMPTION]** the installed unit starts without this because the NixOS security wrapper changes stdin handling; this was not fully explained.
- **[MEASURED]** With the unit's own `VK_DRIVER_FILES` (NVIDIA `.x86_64.json`), the null compositor failed: `vk_check_required_extensions ... Missing required instance extensions` → `XRT_ERROR_VULKAN`. Overriding to the Intel ICD fixed Vulkan init.
- **[MEASURED]** Server log evidence (full log in `spike/e1/monado-e1-journal.log`):
  ```
  Using builder remote: Remote simulation devices builder
  head: Remote HMD (Remote HMD), view count: 2
  Listen address 0.0.0.0 on port 4242
  Listening on port '4242'.
  Connection received! 21
  Disconnected!
  ```
- **[MEASURED]** No `Protocol version mismatch` was ever logged.

## 5. Throwaway client

- **[MEASURED]** `/home/nate/Projects/vr-bot/spike/e1/remote_pose_client.py` (Python 3.14).
- **[MEASURED]** It connects to `127.0.0.1:4242`, reads the two `r_remote_data` handshake packets, then sends 376-byte packets at 50 Hz with `TCP_NODELAY`.
- **[MEASURED]** It streams `head.center` quaternion for yaw `35° · sin(2π · 0.5 Hz · t)`, position `y=1.6`, and static active controllers at `x=∓0.2, y=1.3, z=-0.5`.
- **[MEASURED]** Client log excerpt:
  ```
  wire size: 376 bytes, header=0x0033746d72646e6d
  handshake reset header=0x0033746d72646e6d latest header=0x0033746d72646e6d
  t=  1.99s packet=  100 yaw=   0.48 deg quat=(+0.0000,+0.0042,+0.0000,+1.0000)
  t=  3.00s packet=  150 yaw= -35.00 deg quat=(+0.0000,-0.3007,+0.0000,+0.9537)
  t=  5.01s packet=  250 yaw=  35.00 deg quat=(+0.0000,+0.3007,+0.0000,+0.9537)
  sent 299 packets; closing
  ```

## 6. OpenXR app evidence — the view moves

- **[MEASURED]** `/home/nate/Projects/vr-bot/spike/e1/xr_view_probe.c` creates an `XR_MND_headless` session (no graphics binding), enables `XR_KHR_convert_timespec_time`, and calls `xrLocateViews(LOCAL, PRIMARY_STEREO)` at ~30 Hz.
- **[MEASURED]** Build:
  ```bash
  gcc -O2 -I include -o xr_view_probe xr_view_probe.c \
    -L/nix/store/4z8ibn91k9r6yismm2nrcnl19b1v9rp7-openxr-loader-1.1.54/lib \
    -lopenxr_loader -Wl,-rpath,/nix/store/4z8ibn91k9r6yismm2nrcnl19b1v9rp7-openxr-loader-1.1.54/lib -lm
  ```
- **[MEASURED]** Run with `XR_RUNTIME_JSON=/run/current-system/sw/share/openxr/1/openxr_monado.json`.
- **[MEASURED]** Selected devices: `Head: 'Remote HMD'`, `Left: 'Remote Left Controller'`, `Right: 'Remote Right Controller'`; `views=2`, `valid=1` on every sample.

Timeline (`probe-with-stream.log`; pose stream active from probe `t≈2.0s` to `t≈10.0s`):

| probe t (s) | view yaw (deg) | phase |
|---:|---:|---|
| 0.03 | +0.43 | idle, before stream |
| 2.12 | +8.77 | stream ramping up |
| 2.35 | +28.46 | |
| 2.59 | +34.66 | peak |
| 3.51 | -34.87 | trough |
| 4.61 | +34.35 | peak |
| 5.51 | -34.58 | trough |
| 6.67 | +32.57 | peak |
| 7.59 | -34.62 | trough |
| 8.52 | +34.90 | peak |
| 9.45 | -33.40 | trough |
| 10.14 | -1.41 | idle, frozen after last packet |
| 12.00 | -1.41 | idle |

- **[MEASURED]** Peak-to-peak spacing is ≈2.0 s → **0.5 Hz**, amplitude ≈35°, matching the client's `--yaw-hz 0.5 --yaw-deg 35`.
- **[MEASURED]** The pose is static before the stream and static again after disconnect (at the last packet value), which confirms the motion is caused by the TCP stream, not app-side animation.
- **[MEASURED]** `hello_xr` (real Vulkan rendering client) ran concurrently with the stream: it created a session, reported `System Properties: Name=Monado: Remote HMD`, created color/depth swapchains at 320×240 for both views, and the server logged `Started native session, 1 active app session(s)` plus continuing frame scheduling (`wait_for_scheduled_free`, `Frame late by 50.00ms!`). No pixels were displayed because of the null compositor. `hello_xr` exits immediately if stdin is EOF (`Press any key to shutdown...`); it must be kept alive with a pipe.
- **[ASSUMPTION]** A normal rendering app that consumes `xrLocateViews` will see the same movement that `xr_view_probe` measured; `hello_xr` was not instrumented to print its view matrices.

## 7. Measured environment discrepancy (important for E2)

- **[MEASURED]** This agent's environment did **not** have a usable NVIDIA GPU:
  - `lspci` shows only `Intel Corporation Raptor Lake-P [Iris Xe Graphics]`.
  - `/dev/nvidia0` is absent.
  - `nvidia-smi` fails with “couldn't communicate with the NVIDIA driver”.
  - `/dev/nvidiactl` exists and `nvidia_icd.json` is present, but the driver is not loaded.
- **[MEASURED]** Therefore all successful runs used `VK_DRIVER_FILES=…/intel_icd.x86_64.json` and `XRT_COMPOSITOR_NULL=1`; the installed unit's `XRT_COMPOSITOR_COMPUTE=true` + NVIDIA path was **not** exercised.
- **[ASSUMPTION]** The user's real session may have the RTX 4060 available even though this agent cannot see it; E2 must re-verify on the real NVIDIA/compute path.

## 8. Errors encountered (none fatal to the E1 question)

- **[MEASURED]** `init_epoll(stdin) failed '-1'` → `XRT_ERROR_IPC_MAINLOOP_FAILED_TO_INIT` when launching transiently without `XRT_NO_STDIN=1`.
- **[MEASURED]** `XRT_ERROR_VULKAN` with the unit's NVIDIA ICD path (no NVIDIA device in this shell).
- **[MEASURED]** First version of the C probe used `xrWaitFrame` + `xrBeginFrame` and hit `XR_ERROR_CALL_ORDER_INVALID: xrBeginFrame without xrWaitFrame` in the headless path. Workaround: no frame loop; use `xrConvertTimespecTimeToTimeKHR` + `xrLocateViews`. This is a client-side issue, not a remote-driver rejection.
- **[MEASURED]** Non-fatal server log on client teardown: `ERROR [ipc_server_objects_destroy_xspc] Client tried to destroy non-existent space!`.
- **[MEASURED]** `hello_xr` exits at once when stdin is EOF; use a held-open pipe.

## 9. What E2 should do next

1. **[MEASURED-BASIS]** Re-run §4–§6 on the real NVIDIA path (`XRT_COMPOSITOR_COMPUTE=true`, NVIDIA ICD) and confirm the remote driver still accepts poses while the PSVR2 HMD driver is present. E1 only proves the remote driver works when it is the selected builder.
2. **[MEASURED-BASIS]** Capture actual displayed pixels: either (a) run the main windowed/XCB compositor with `XRT_DEBUG_GUI=1` / mirror-to-debug-GUI and screenshot the window, or (b) instrument a small Vulkan OpenXR app to read back its swapchain and diff frames while yaw changes. This is the only missing piece for a literal “rendered view moves” claim.
3. **[CITED]** Test the JSON `remote` node with explicit `view_count`, e.g. `{"active":"remote","remote":{"version":0,"port":4242,"view_count":2}}`, and verify a custom port. Note this fork requires `view_count`.
4. **[MEASURED-BASIS]** Characterize safety behavior when the TCP producer dies: E1 measured that Monado freezes at the last pose indefinitely. E2 should decide whether a watchdog / neutral-pose timeout is needed before driving a real headset, and whether to use `XR_REFERENCE_SPACE_TYPE_STAGE` or `LOCAL`.
5. **[MEASURED-BASIS]** Quantify pose→view latency and jitter at 90 Hz, and test reconnect behavior (the hub accepts one client at a time, then re-listens after disconnect).
6. **[MEASURED-BASIS]** Test controller fields (buttons, triggers, hand curl). E1 sent static active controllers only; no controller input was validated.
7. **[CITED]** Security note for any future bridge: the hub binds `0.0.0.0:4242`, has no auth, and does not validate incoming headers. Keep it on loopback or a trusted network namespace.

## 10. Artifacts

- Client: `/home/nate/Projects/vr-bot/spike/e1/remote_pose_client.py`
- OpenXR probe source/binary: `/home/nate/Projects/vr-bot/spike/e1/xr_view_probe.c`, `.../xr_view_probe`
- OpenXR headers used: Khronos OpenXR-SDK `release-1.1.54` (not committed; re-fetch from the SDK)
- Server journal: `/home/nate/Projects/vr-bot/spike/e1/monado-e1-journal.log`
- Binary facts/hashes: `/home/nate/Projects/vr-bot/spike/e1/evidence-binary.txt`
- Key logs:
  - `probe-with-stream.log` — view yaw follows stream
  - `probe-baseline.log` — idle pose
  - `client-stream.log` — sent poses
  - `helloxr-frames.log` — real rendering app against remote runtime
  - `client-helloxr*.log`, `probe-helloxr.log`

## 11. Repro commands (short form)

```bash
cd /home/nate/Projects/vr-bot/spike/e1

# 1. server (transient; does not touch ~/.config/monado or the installed unit)
systemctl --user stop monado.service monado.socket
rm -f /run/user/1000/monado_comp_ipc
systemd-run --user --unit=e1-monado --collect \
  -p Environment=IPC_EXIT_ON_DISCONNECT=off \
  -p Environment=XRT_NO_STDIN=1 \
  -p Environment=P_OVERRIDE_ACTIVE_CONFIG=remote \
  -p Environment=XRT_COMPOSITOR_NULL=1 \
  -p Environment=XRT_LOG=debug -p Environment=REMOTE_LOG=debug \
  -p Environment=VK_DRIVER_FILES=/run/opengl-driver/share/vulkan/icd.d/intel_icd.x86_64.json \
  /run/wrappers/bin/monado-service

# 2. OpenXR probe in background, then stream a moving pose
export XR_RUNTIME_JSON=/run/current-system/sw/share/openxr/1/openxr_monado.json
( ./xr_view_probe 12 > probe-with-stream.log 2>&1 ) &
sleep 2
python3 remote_pose_client.py --duration 8 --hz 50 --yaw-deg 35 --yaw-hz 0.5 \
  > client-stream.log 2>&1
wait
grep '^t=' probe-with-stream.log

# 3. restore the state found at start (socket active, service inactive, no config)
systemctl --user stop e1-monado
systemctl --user unset-environment P_OVERRIDE_ACTIVE_CONFIG XRT_COMPOSITOR_NULL XRT_LOG REMOTE_LOG
systemctl --user start monado.socket
```
