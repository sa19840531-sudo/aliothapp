#!/usr/bin/env bash
# 停掉其他 VM，以 D3D12 啟動 Alioth Nexus5 A11
set -euo pipefail
# 勿繼承 Windows PATH（含括號路徑會弄壞 bash）
export PATH="/root/genymotion/genymotion:/root/genymotion/genymotion/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0

NAME="${GENYMOTION_VM_NAME:-Alioth Nexus5 A11}"
gmtool admin stopall || true
sleep 3
pkill -f '/root/genymotion/genymotion/player' || true
pkill -f 'qemu-system-x86_64' || true
sleep 2

pgrep -f '/root/genymotion/genymotion/genymotion$' >/dev/null || \
  setsid /root/genymotion/genymotion/genymotion </dev/null >/tmp/genymotion-gui.log 2>&1 &
sleep 2

setsid /root/genymotion/genymotion/player --vm-name "$NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player-a11.log 2>&1 &
echo "player started for $NAME"
sleep 8

for i in $(seq 1 48); do
  SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  b="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  echo "round=$i serial=$SERIAL boot=$b"
  if [[ "$b" == "1" ]]; then
    echo -n "abilist="; adb -s "$SERIAL" shell getprop ro.product.cpu.abilist
    echo -n "bridge="; adb -s "$SERIAL" shell getprop ro.dalvik.vm.native.bridge
    echo "READY serial=$SERIAL"
    gmtool admin list
    exit 0
  fi
  sleep 5
done
tail -40 /tmp/genymotion-player-a11.log || true
exit 1
