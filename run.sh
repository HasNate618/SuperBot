#!/usr/bin/env bash
set -euo pipefail

caller_dir="$(pwd)"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

args=()
after_separator=0
while (($#)); do
  if ((after_separator)); then
    args+=("$1")
    shift
    continue
  fi
  case "$1" in
    --config)
      args+=("--config")
      shift
      if (($#)); then
        config_path="$1"
        if [[ -n "$config_path" && "$config_path" != /* ]]; then
          config_path="$caller_dir/$config_path"
        fi
        args+=("$config_path")
        shift
      fi
      ;;
    --config=*)
      config_path="${1#--config=}"
      if [[ -n "$config_path" && "$config_path" != /* ]]; then
        config_path="$caller_dir/$config_path"
      fi
      args+=("--config=$config_path")
      shift
      ;;
    --)
      args+=("$1")
      after_separator=1
      shift
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

cd "$SCRIPT_DIR"

if command -v weston >/dev/null 2>&1 \
  && command -v weston-screenshooter >/dev/null 2>&1 \
  && command -v weston-debug >/dev/null 2>&1 \
  && command -v novnc >/dev/null 2>&1 \
  && command -v websockify >/dev/null 2>&1; then
  exec python3 -m superbot "${args[@]}"
fi

exec nix develop "$SCRIPT_DIR" -c python3 -m superbot "${args[@]}"
