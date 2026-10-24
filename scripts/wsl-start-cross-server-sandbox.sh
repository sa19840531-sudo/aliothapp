#!/usr/bin/env bash
# WSL 沙盒：等 Emulator boot → 啟動 Mobile Host :8765
set -euo pipefail

ROOT="${ALIOTH_WSL_ROOT:-/mnt/f/aliothG}"
SDK="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
HOST_PORT="${ALIOTH_MOBILE_DEMO_HOST_PORT:-8765}"
export PATH="$SDK/platform-tools:$SDK/emulator:/usr/bin:/bin:$PATH"
export ANDROID_HOME="$SDK"
export ANDROID_SDK_ROOT="$SDK"

if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm missing" >&2
  exit 1
fi

# Emulator
if ! pgrep -f 'qemu-system-x86_64' >/dev/null 2>&1; then
  echo "starting emulator..."
  bash "$ROOT/scripts/wsl-start-android-emulator.sh"
fi

adb start-server >/dev/null 2>&1 || true
READY_SERIAL=""
for i in $(seq 1 60); do
  adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
  while read -r serial state _; do
    [[ "$state" != "device" ]] && continue
    [[ "$serial" != emulator-* && "$serial" != 127.0.0.1:* ]] && continue
    boot="$(adb -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
    if [[ "$boot" == "1" ]]; then
      READY_SERIAL="$serial"
      break 2
    fi
  done < <(adb devices | awk 'NR>1{print}')
  echo "wait_$i"
  sleep 5
done
if [[ -z "$READY_SERIAL" ]]; then
  echo "ERROR: emulator adb boot timeout" >&2
  tail -40 /tmp/alioth-wsl-emulator.log 2>/dev/null || true
  exit 1
fi
echo "adb ready: $READY_SERIAL"

cd "$ROOT"
export ALIOTH_DEVICE_BACKEND=redroid
export ALIOTH_REDROID_ADB="${ALIOTH_REDROID_ADB:-127.0.0.1:5555}"
export ALIOTH_MOBILE_DEMO_FORCE_LOCAL=1
export ALIOTH_MOBILE_DEMO_HOST_BIND=0.0.0.0
export ALIOTH_MOBILE_DEMO_HOST_PORT="$HOST_PORT"
export ALIOTH_MOBILE_DEMO_DATA_DIR="$ROOT/.alioth-mobile-demo"
export ALIOTH_WORKSPACE_HOST="$ROOT"
export ALIOTH_MOBILE_DEMO_STAGE=dev

pkill -f 'mobile_demo_host_runner' 2>/dev/null || true
sleep 1
PY="${ALIOTH_WSL_PYTHON:-/opt/alioth-wsl-venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi
nohup "$PY" -m services.api.app.mobile_demo_host_runner >/tmp/alioth-mobile-host-wsl.log 2>&1 &
echo "host_pid=$! python=$PY"
sleep 3
curl -sf "http://127.0.0.1:${HOST_PORT}/health"
echo
echo "WSL sandbox OK on :${HOST_PORT}"
