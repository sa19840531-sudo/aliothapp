#!/usr/bin/env bash
# 移除 WSL 內 Genymotion 安裝（使用者要求改用官方 Emulator+KVM）
set -u
pkill -f '/root/genymotion' 2>/dev/null || true
pkill -f '[g]enymotion' 2>/dev/null || true
sleep 1
rm -rf /root/genymotion /root/.Genymobile
rm -f /tmp/genymotion*.run /tmp/player*.log /tmp/genymotion-gui.log
if [[ -d /root/genymotion ]]; then
  echo "FAILED: /root/genymotion still exists" >&2
  exit 1
fi
echo GENYMOTION_REMOVED
