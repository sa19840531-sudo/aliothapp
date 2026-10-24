#!/usr/bin/env bash
set -u
export PATH="/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/usr/bin:/bin:$PATH"
adb kill-server 2>/dev/null || true
adb start-server
sleep 1
echo "=== devices ==="
adb devices -l
echo "=== ports ==="
ss -ltn 2>/dev/null | grep -E '55[0-9]{2}|8554' || netstat -ltn 2>/dev/null | grep -E '55[0-9]{2}|8554' || true
echo "=== qemu ==="
pgrep -a qemu | head -5 || true
echo "=== log tail ==="
tail -30 /tmp/alioth-wsl-emulator.log || true
echo "=== boot ==="
for s in emulator-5554 emulator-5556 127.0.0.1:5555; do
  echo -n "$s: "
  adb -s "$s" shell getprop sys.boot_completed 2>/dev/null || echo n/a
done
