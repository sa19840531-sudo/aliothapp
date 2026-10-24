#!/usr/bin/env bash
# 以軟渲染重啟 Genymotion，並安裝/啟動目標 APK 驗證
set -euo pipefail
NAME="${GENYMOTION_VM_NAME:-Alioth Nexus5 A11}"
APK="${1:-/mnt/f/alioth/.alioth-mobile-demo/uploads/b90d7759bb2b4140aa3db6fe5d0c5291_91cg_1.3.5_260710_2.apk}"
export PATH="/root/genymotion/genymotion:/root/genymotion/genymotion/tools:/usr/bin:/bin:$PATH"
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0

MODE="${GL_MODE:-soft}" # soft|d3d12
if [[ "$MODE" == "d3d12" ]]; then
  export LIBGL_ALWAYS_SOFTWARE=0
  export GALLIUM_DRIVER=d3d12
  export MESA_LOADER_DRIVER_OVERRIDE=d3d12
  export MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA
else
  export LIBGL_ALWAYS_SOFTWARE=1
  unset GALLIUM_DRIVER MESA_LOADER_DRIVER_OVERRIDE MESA_D3D12_DEFAULT_ADAPTER_NAME || true
fi

echo "mode=$MODE name=$NAME"
gmtool admin stop "$NAME" || true
sleep 4
setsid /root/genymotion/genymotion/player --vm-name "$NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player-verify.log 2>&1 &
sleep 8

for i in $(seq 1 48); do
  SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  b="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  echo "round=$i serial=$SERIAL boot=$b"
  if [[ "$b" == "1" ]]; then
    break
  fi
  sleep 5
done

SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
echo -n "abilist="; adb -s "$SERIAL" shell getprop ro.product.cpu.abilist
echo -n "bridge="; adb -s "$SERIAL" shell getprop ro.dalvik.vm.native.bridge

adb -s "$SERIAL" uninstall me.mfanv.npymci >/dev/null 2>&1 || true
adb -s "$SERIAL" install -r "$APK"
adb -s "$SERIAL" logcat -c
adb -s "$SERIAL" shell am start -n me.mfanv.npymci/com.jycg.icg.MainActivity
sleep 12
echo "=== PROCESS ==="
adb -s "$SERIAL" shell "ps -A | grep mfanv || echo NO_PROCESS"
echo "=== LOG ==="
adb -s "$SERIAL" logcat -d -t 200 | grep -E "SIGILL|SIGSEGV|Fatal signal|houdini|Initialize library" | tail -30 || true
mkdir -p /mnt/f/alioth/.alioth-mobile-demo/runs/houdini-verify
adb -s "$SERIAL" exec-out screencap -p > "/mnt/f/alioth/.alioth-mobile-demo/runs/houdini-verify/after_launch_${MODE}.png"
echo "shot=after_launch_${MODE}.png"
