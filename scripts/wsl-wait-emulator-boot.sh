#!/usr/bin/env bash
set -u
export PATH="/opt/android-sdk/platform-tools:/usr/bin:/bin:$PATH"
SERIAL="${1:-emulator-5554}"
for i in $(seq 1 48); do
  state="$(adb -s "$SERIAL" get-state 2>/dev/null || true)"
  boot="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  echo "round=$i state=$state boot=$boot"
  if [[ "$boot" == "1" ]]; then
    echo BOOT_OK
    adb -s "$SERIAL" shell getprop ro.product.cpu.abilist
    exit 0
  fi
  sleep 5
done
echo BOOT_TIMEOUT >&2
exit 1
