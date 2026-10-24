#!/usr/bin/env bash
# C0: unpack the Superhot VR build. Read-only on the archive.
#
# Facts, both measured:
#   - the file is named .rar but is a ZIP (compression=store)
#   - the build is Windows (Redist/DirectX, .exe) so it needs Proton
#   - it ships UnityOpenXR.dll + openxr_loader.dll => native OpenXR.
#     xrizer is NOT in this path; Monado's remote driver is the runtime
#     the game talks to directly.
set -euo pipefail

ARCHIVE="${1:-/home/nate/Downloads/Superhot-VR.rar}"
DEST="${2:-$HOME/Games/SUPERHOT_VR}"
EXE="$DEST/SUPERHOT VR/SUPERHOT_VR.exe"

[ -r "$ARCHIVE" ] || { echo "fatal: cannot read $ARCHIVE" >&2; exit 1; }

if [ -x "$EXE" ]; then
  echo "already extracted: $EXE"
  exit 0
fi

echo "extracting $ARCHIVE -> $DEST"
mkdir -p "$DEST"
unzip -q -o "$ARCHIVE" -d "$DEST"

[ -f "$EXE" ] || { echo "fatal: expected $EXE after extraction" >&2; exit 1; }
echo "ok: $EXE"
echo
echo "Add to Steam as a non-Steam game, then:"
echo "  Properties -> Compatibility -> Force the use of a specific tool -> GE-Proton10-34"
echo "  Launch options: XR_RUNTIME_JSON=$HOME/.local/share/openxr/1/active_runtime.json %command%"
echo "  (or leave it unset: Monado installs its manifest system-wide, see README)"
