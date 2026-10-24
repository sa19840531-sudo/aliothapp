#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source .venv/bin/activate
export PYTHONPATH="$ROOT"
if [[ -f "$ROOT/.env.emulator" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.env.emulator"
fi
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/android-sdk}"
export PATH="$ANDROID_SDK_ROOT/emulator:$ANDROID_SDK_ROOT/platform-tools:$PATH"
export DISPLAY="${DISPLAY:-:0}"
export ALIOTH_MOBILE_DEMO_DEVICE_BACKEND="${ALIOTH_MOBILE_DEMO_DEVICE_BACKEND:-google_emulator}"
# VMware 桌面預設有視窗；伺服器無頭請設 ALIOTH_EMULATOR_EXTRA_ARGS 含 -no-window
export ALIOTH_EMULATOR_EXTRA_ARGS="${ALIOTH_EMULATOR_EXTRA_ARGS:--no-audio -gpu swiftshader_indirect -no-metrics}"
HOST="${ALIOTH_API_HOST:-0.0.0.0}"
PORT="${ALIOTH_API_PORT:-8000}"
exec python -m uvicorn services.api.app.main:app --host "$HOST" --port "$PORT"
