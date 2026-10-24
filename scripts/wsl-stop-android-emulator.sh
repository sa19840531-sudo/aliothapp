#!/usr/bin/env bash
# 停止 WSL 內 Android Emulator
set -u
pkill -9 -f 'qemu-system-x86_64' 2>/dev/null || true
pkill -9 -f '/opt/android-sdk/emulator' 2>/dev/null || true
sleep 2
if pgrep -f 'qemu-system-x86_64' >/dev/null 2>&1; then
  echo "FAILED: still running" >&2
  pgrep -a qemu-system || true
  exit 1
fi
echo EMULATOR_STOPPED
