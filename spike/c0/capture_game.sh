#!/usr/bin/env bash
# C0: capture the Superhot window (not the desktop) and record its geometry.
set -euo pipefail

OUT="${1:-spike/c0/logs/shots}"
mkdir -p "$OUT"

GEOM=$(hyprctl clients -j | python3 -c '
import json, sys
want = ("superhot", "superhot_vr.exe", "superhotvr")
try:
    clients = json.load(sys.stdin)
except Exception as exc:
    sys.exit(f"cannot parse hyprctl output: {exc}")
for c in clients:
    blob = f"{c.get(\"class\",\"\")} {c.get(\"title\",\"\")} {c.get(\"initialClass\",\"\")}".lower()
    if any(w in blob for w in want):
        x, y = c.get("at", [0, 0])
        w, h = c.get("size", [0, 0])
        if w and h:
            print(f"{x},{y} {w}x{h}")
            sys.exit(0)
sys.exit(2)
') || true

if [ -z "${GEOM:-}" ]; then
  echo "no Superhot window found. open windows:" >&2
  hyprctl clients -j | python3 -c '
import json, sys
for c in json.load(sys.stdin):
    print(f"  class={c.get(\"class\")!r} title={c.get(\"title\")!r} at={c.get(\"at\")} size={c.get(\"size\")}")' >&2
  exit 2
fi

TS=$(date +%Y%m%d_%H%M%S_%N)
IMG="$OUT/game_$TS.png"
grim -g "$GEOM" "$IMG"
cat > "$OUT/game_$TS.txt" <<META
geometry: $GEOM
captured: $(date -Is)
image:    $IMG
META
echo "$IMG"
