#!/usr/bin/env bash
# 為 Genymotion Android 11 刷入 libhoudini 並寫入 ABI / native.bridge
set -euo pipefail

NAME="${GENYMOTION_VM_NAME:-Alioth Nexus5 A11}"
ZIP="${1:-/mnt/f/alioth/.alioth-mobile-demo/tools/genymotion-a11-libhoudini-system.zip}"
GMTOOL="/root/genymotion/genymotion/gmtool"
export PATH="/root/genymotion/genymotion:/root/genymotion/genymotion/tools:/usr/bin:/bin:$PATH"
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0

if [[ ! -f "$ZIP" ]]; then
  echo "ERROR: houdini zip missing: $ZIP" >&2
  exit 1
fi

SERIAL="$($GMTOOL admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
echo "flashing serial=$SERIAL name=$NAME"
$GMTOOL device flash -n "$NAME" "$ZIP"

adb connect "$SERIAL" || true
adb -s "$SERIAL" root || true
sleep 2
adb -s "$SERIAL" shell "mount -o rw,remount /" || true

cat >/tmp/houdini-build.prop.append <<'EOF'
ro.product.cpu.abilist=x86_64,x86,arm64-v8a,armeabi-v7a,armeabi
ro.product.cpu.abilist32=x86,armeabi-v7a,armeabi
ro.product.cpu.abilist64=x86_64,arm64-v8a
ro.vendor.product.cpu.abilist=x86_64,x86,arm64-v8a,armeabi-v7a,armeabi
ro.vendor.product.cpu.abilist32=x86,armeabi-v7a,armeabi
ro.vendor.product.cpu.abilist64=x86_64,arm64-v8a
ro.odm.product.cpu.abilist=x86_64,x86,arm64-v8a,armeabi-v7a,armeabi
ro.odm.product.cpu.abilist32=x86,armeabi-v7a,armeabi
ro.odm.product.cpu.abilist64=x86_64,arm64-v8a
ro.dalvik.vm.native.bridge=libhoudini.so
ro.enable.native.bridge.exec=1
ro.enable.native.bridge.exec64=1
ro.dalvik.vm.isa.arm=x86
ro.dalvik.vm.isa.arm64=x86_64
ro.zygote=zygote64_32
EOF

adb -s "$SERIAL" push /tmp/houdini-build.prop.append /data/local/tmp/houdini-build.prop.append
adb -s "$SERIAL" shell "grep -q 'ro.dalvik.vm.native.bridge=libhoudini.so' /system/build.prop || cat /data/local/tmp/houdini-build.prop.append >> /system/build.prop"
adb -s "$SERIAL" shell "grep -q 'ro.dalvik.vm.native.bridge=libhoudini.so' /system/vendor/build.prop || cat /data/local/tmp/houdini-build.prop.append >> /system/vendor/build.prop"
adb -s "$SERIAL" shell "ls -la /system/lib64/libhoudini.so /system/lib/libhoudini.so"

# Genymotion 原廠 abi-setup 只開 32-bit ARM；需再補 arm64
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/wsl-patch-genymotion-arm64-abi.sh" ]]; then
  GENYMOTION_VM_NAME="$NAME" bash "$SCRIPT_DIR/wsl-patch-genymotion-arm64-abi.sh"
  exit $?
fi

echo "Stopping and restarting VM..."
$GMTOOL admin stop "$NAME"
sleep 4
setsid /root/genymotion/genymotion/player --vm-name "$NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player.log 2>&1 &
sleep 10

for i in $(seq 1 48); do
  SERIAL="$($GMTOOL admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  b="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  echo "round=$i serial=$SERIAL boot=$b"
  if [[ "$b" == "1" ]]; then
    echo -n "abilist="; adb -s "$SERIAL" shell getprop ro.product.cpu.abilist
    echo -n "bridge="; adb -s "$SERIAL" shell getprop ro.dalvik.vm.native.bridge
    echo "HOUDINI_OK serial=$SERIAL"
    exit 0
  fi
  sleep 5
done
echo HOUDINI_BOOT_TIMEOUT >&2
exit 1
