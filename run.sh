#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if command -v weston >/dev/null 2>&1 \
  && command -v weston-screenshooter >/dev/null 2>&1 \
  && command -v weston-debug >/dev/null 2>&1; then
  exec python3 -m vrbot "$@"
fi

exec nix shell nixpkgs#weston -c python3 -m vrbot "$@"
