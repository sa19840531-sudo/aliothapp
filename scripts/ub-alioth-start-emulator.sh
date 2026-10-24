#!/bin/bash
set -euo pipefail
export ANDROID_SDK_ROOT=/home/eevee/android-sdk
export PATH=$ANDROID_SDK_ROOT/emulator:$ANDROID_SDK_ROOT/platform-tools:$PATH
export DISPLAY="${DISPLAY:-:0}"
GPU=${ALIOTH_EMU_GPU:-swiftshader_indirect}
AVD=${ALIOTH_EMU_AVD:-Alioth_API30_x86_64}

pkill -f 'qemu-system-x86_64' 2>/dev/null || true
pkill -f '/android-sdk/emulator/emulator' 2>/dev/null || true
sleep 2

EXTRA=(-accel on -gpu "$GPU" -no-audio -no-metrics -no-snapshot-load)
if [[ "${ALIOTH_EMU_NO_WINDOW:-0}" == "1" ]]; then
  EXTRA+=(-no-window)
fi

xhost +SI:localuser:eevee >/dev/null 2>&1 || true

nohup emulator -avd "$AVD" "${EXTRA[@]}" >/tmp/alioth-emulator.log 2>&1 &
echo pid=$! log=/tmp/alioth-emulator.log display=$DISPLAY no_window=${ALIOTH_EMU_NO_WINDOW:-0}
for i in $(seq 1 90); do
  if adb devices 2>/dev/null | grep -qE 'emulator-[0-9]+[[:space:]]+device'; then
    boot=$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)
    echo round=$i boot=$boot
    [[ "$boot" == "1" ]] && { echo READY; exit 0; }
  else
    echo round=$i waiting...
  fi
  sleep 5
done
echo BOOT_TIMEOUT
tail -40 /tmp/alioth-emulator.log || true
exit 1
