#!/usr/bin/env bash
# C0: start Monado's `remote` driver with the env that actually works here.
#
# The systemd --user unit is NOT usable on this box (measured, E2):
#   XRT_COMPOSITOR_COMPUTE=true          -> SIGSEGV in the Wayland
#                                           presentation queue-family check
#   VK_DRIVER_FILES=...nvidia_icd.x86_64.json -> that file does not exist;
#                                           the real one is nvidia_icd.json
# So launch the service directly with a corrected environment, in the
# foreground, and kill it with Ctrl-C when C0 is done.
set -euo pipefail

export XR_RUNTIME_JSON="${XR_RUNTIME_JSON:-/run/current-system/sw/share/openxr/1/openxr_monado.json}"
export P_OVERRIDE_ACTIVE_CONFIG=remote   # no config_v0.json needed (E1)
export IPC_EXIT_ON_DISCONNECT=off
export XRT_NO_STDIN=1
export XRT_COMPOSITOR_COMPUTE=false      # GRAPHICS queue; compute SIGSEGVs
export VK_DRIVER_FILES=/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.json

if ! [ -r "$VK_DRIVER_FILES" ]; then
  echo "fatal: VK_DRIVER_FILES not readable: $VK_DRIVER_FILES" >&2
  echo "       find it with: ls /run/opengl-driver/share/vulkan/icd.d/" >&2
  exit 1
fi

if systemctl --user is-active --quiet monado.service; then
  echo "refusing to start: monado.service is already active and would fight" >&2
  echo "for the IPC socket. Stop it first if you mean to:" >&2
  echo "  systemctl --user stop monado.service" >&2
  exit 1
fi

cat <<INFO
monado remote driver
  runtime json : $XR_RUNTIME_JSON
  vulkan icd   : $VK_DRIVER_FILES
  listening    : 127.0.0.1:4242  (TCP, one client, last pose freezes on disconnect)
  compositor   : XRT_COMPOSITOR_COMPUTE=false

Next, in another shell:
  step 1  (pose follows)  python3 spike/c0/c0_client.py --mode yaw    --duration 20
  step 2  (input)         python3 spike/c0/c0_client.py --mode trigger --duration 20
  ...and capture the game window with spike/c0/capture_game.sh
INFO

exec /run/wrappers/bin/monado-service
