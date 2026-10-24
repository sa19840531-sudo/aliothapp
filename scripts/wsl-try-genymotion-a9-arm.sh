#!/usr/bin/env bash
set -euo pipefail
export PATH="/root/genymotion/genymotion:/root/genymotion/genymotion/tools:/usr/bin:/bin:$PATH"
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA
export DISPLAY=:0

ZIP="${1:-/mnt/f/alioth/.alioth-mobile-demo/tools/Genymotion-ARM-Translation_for_8.0.zip}"
APK="${2:-/mnt/f/alioth/.alioth-mobile-demo/uploads/b90d7759bb2b4140aa3db6fe5d0c5291_91cg_1.3.5_260710_2.apk}"
NAME="Alioth Nexus5 A9"

gmtool admin osimages | grep -i "9.0" || true

if ! gmtool admin list | grep -q "$NAME"; then
  gmtool admin create "Google Nexus 5" "Android 9.0 (Pie)" "$NAME" --root-access on --nbcpu 4 --ram 3072 --quickboot off
fi

setsid /root/genymotion/genymotion/player --vm-name "$NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player-a9.log 2>&1 &
sleep 8
for i in $(seq 1 48); do
  SERIAL="$(gmtool admin list | awk -v n="$NAME" -F'|' '$0 ~ n && /On/{gsub(/ /,"",$2); print $2; exit}')"
  if [[ -z "$SERIAL" ]]; then
    SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
  fi
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  b="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  ver="$(adb -s "$SERIAL" shell getprop ro.build.version.release 2>/dev/null | tr -d '\r' || true)"
  echo "round=$i serial=$SERIAL boot=$b ver=$ver"
  if [[ "$b" == "1" ]]; then break; fi
  sleep 5
done

SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
echo "flashing $ZIP to $NAME ($SERIAL)"
gmtool device flash -n "$NAME" "$ZIP" || echo FLASH_FAIL
adb -s "$SERIAL" root || true
sleep 1
adb -s "$SERIAL" remount || adb -s "$SERIAL" shell "mount -o rw,remount /" || true
adb -s "$SERIAL" shell "getprop ro.product.cpu.abilist; getprop ro.dalvik.vm.native.bridge; ls /system/lib/libhoudini.so 2>&1 | head"

gmtool admin stop "$NAME"
sleep 4
setsid /root/genymotion/genymotion/player --vm-name "$NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player-a9.log 2>&1 &
sleep 8
for i in $(seq 1 48); do
  SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  b="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  echo "reboot_round=$i boot=$b"
  if [[ "$b" == "1" ]]; then break; fi
  sleep 5
done

SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
echo -n "abilist="; adb -s "$SERIAL" shell getprop ro.product.cpu.abilist
echo -n "bridge="; adb -s "$SERIAL" shell getprop ro.dalvik.vm.native.bridge
adb -s "$SERIAL" uninstall me.mfanv.npymci >/dev/null 2>&1 || true
adb -s "$SERIAL" install -r "$APK" || echo INSTALL_FAIL
adb -s "$SERIAL" logcat -c
adb -s "$SERIAL" shell am start -n me.mfanv.npymci/com.jycg.icg.MainActivity || true
sleep 12
adb -s "$SERIAL" shell "ps -A | grep mfanv || ps | grep mfanv || echo NO_PROCESS"
adb -s "$SERIAL" logcat -d -t 150 | grep -E "SIGILL|SIGSEGV|Fatal signal|houdini|Initialize library" | tail -25 || true
