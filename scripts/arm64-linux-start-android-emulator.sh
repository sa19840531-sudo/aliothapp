#!/usr/bin/env bash
# 在 ARM64 Linux 上啟動官方 Emulator（KVM，arm64 映像，不做轉譯）
# Usage: bash scripts/arm64-linux-start-android-emulator.sh
set -euo pipefail

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "ERROR: 必須在 aarch64 上執行（目前: $(uname -m)）" >&2
  exit 1
fi

SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
AVD_NAME="${ALIOTH_AVD_NAME:-Alioth_ARM64_API30}"
GPU="${ALIOTH_EMU_GPU:-swiftshader_indirect}"
export ANDROID_SDK_ROOT="$SDK_ROOT"
export ANDROID_HOME="$SDK_ROOT"
export PATH="$SDK_ROOT/cmdline-tools/latest/bin:$SDK_ROOT/platform-tools:$SDK_ROOT/emulator:$PATH"

EMU="$SDK_ROOT/emulator/emulator"
ADB="$SDK_ROOT/platform-tools/adb"
if [[ ! -x "$EMU" ]]; then
  echo "ERROR: emulator not found: $EMU（先跑 arm64-linux-install-android-emulator.sh）" >&2
  exit 1
fi

# 若已有裝置在跑則直接回報
"$ADB" start-server >/dev/null 2>&1 || true
if "$ADB" devices 2>/dev/null | grep -qE 'emulator-[0-9]+\s+device'; then
  SERIAL="$("$ADB" devices | awk '/emulator-.*device/{print $1; exit}')"
  echo "already running serial=$SERIAL"
  "$ADB" -s "$SERIAL" shell getprop ro.product.cpu.abi
  "$ADB" -s "$SERIAL" shell getprop sys.boot_completed
  exit 0
fi

ACCEL=on
if [[ ! -e /dev/kvm ]]; then
  if [[ "${ALIOTH_ALLOW_NO_KVM:-}" == "1" ]]; then
    ACCEL=off
    echo "WARN: /dev/kvm 不存在，改用 -accel off（僅本機嵌套演練）" >&2
  else
    echo "ERROR: /dev/kvm 不存在；正式環境必須 KVM。嵌套演練可設 ALIOTH_ALLOW_NO_KVM=1" >&2
    exit 1
  fi
fi

echo "Starting AVD=$AVD_NAME accel=$ACCEL gpu=$GPU"
nohup "$EMU" -avd "$AVD_NAME" -accel "$ACCEL" -gpu "$GPU" -no-audio \
  >/tmp/alioth-arm64-emulator.log 2>&1 &
echo "emulator pid=$! log=/tmp/alioth-arm64-emulator.log"

# 無 KVM 時 boot 極慢，允許拉長等待
WAIT_ROUNDS="${ALIOTH_EMU_WAIT_ROUNDS:-90}"
for i in $(seq 1 "$WAIT_ROUNDS"); do
  SERIAL="$("$ADB" devices 2>/dev/null | awk '/emulator-.*device/{print $1; exit}')"
  if [[ -n "${SERIAL:-}" ]]; then
    BOOT="$("$ADB" -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
    ABI="$("$ADB" -s "$SERIAL" shell getprop ro.product.cpu.abi 2>/dev/null | tr -d '\r')"
    BRIDGE="$("$ADB" -s "$SERIAL" shell getprop ro.dalvik.vm.native.bridge 2>/dev/null | tr -d '\r')"
    echo "round=$i serial=$SERIAL boot=$BOOT abi=$ABI bridge=${BRIDGE:-0}"
    if [[ "$BOOT" == "1" ]]; then
      if [[ "$ABI" != "arm64-v8a" ]]; then
        echo "WARN: 期望 abi=arm64-v8a，實際=$ABI" >&2
      fi
      echo "READY serial=$SERIAL"
      echo "本機請建立隧道: ssh -N -L 5555:127.0.0.1:5555 user@<此主機>"
      echo "然後 ALIOTH_REDROID_ADB=127.0.0.1:5555"
      exit 0
    fi
  else
    echo "round=$i waiting device..."
  fi
  sleep 5
done

echo "BOOT_TIMEOUT" >&2
tail -50 /tmp/alioth-arm64-emulator.log >&2 || true
exit 1
