#!/usr/bin/env bash
# 修補 Genymotion ABI 腳本：啟用 arm64 houdini，並寫入 build.prop 後重開機
set -euo pipefail

NAME="${GENYMOTION_VM_NAME:-Alioth Nexus5 A11}"
export PATH="/root/genymotion/genymotion:/root/genymotion/genymotion/tools:/usr/bin:/bin:$PATH"
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA
export DISPLAY=:0

SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
if [[ -z "$SERIAL" ]]; then
  echo "ERROR: no running Genymotion device" >&2
  exit 1
fi
echo "serial=$SERIAL name=$NAME"

adb connect "$SERIAL"
adb -s "$SERIAL" root || true
sleep 2
adb -s "$SERIAL" remount || adb -s "$SERIAL" shell "mount -o rw,remount /" || true
adb -s "$SERIAL" shell "touch /system/vendor/bin/.write_test && rm /system/vendor/bin/.write_test && echo TOUCH_OK"

cat >/tmp/init.genymotion.abi.sh <<'EOF'
#!/system/bin/sh
source utils.genymotion.sh
armabi() {
    if is_genymotion_arm_product; then
        setprop ro.product.cpu.abi2 armeabi-v7a
        setprop ro.product.cpu.abilist arm64-v8a,armeabi-v7a,armeabi
        setprop ro.product.cpu.abilist32 armeabi-v7a,armeabi
        setprop ro.product.cpu.abilist64 arm64-v8a 2>/dev/null
        setprop ro.dalvik.vm.native.bridge 0
        return
    fi
    _abilist=x86
    if is_genymotion_64bit; then
        /vendor/bin/32-bit-checker 2>/dev/null
        _abilist=x86_64,x86
    fi
    if [ -f /system/lib/libhoudini.so ] || [ -f /system/lib64/libhoudini.so ]; then
        setprop ro.product.cpu.abi2 armeabi-v7a
        setprop ro.product.cpu.abilist ${_abilist},arm64-v8a,armeabi-v7a,armeabi
        setprop ro.product.cpu.abilist32 x86,armeabi-v7a,armeabi
        setprop ro.product.cpu.abilist64 x86_64,arm64-v8a 2>/dev/null
        setprop ro.dalvik.vm.isa.arm x86
        setprop ro.dalvik.vm.isa.arm64 x86_64
        setprop ro.dalvik.vm.native.bridge libhoudini.so
        setprop ro.enable.native.bridge.exec 1
        setprop ro.enable.native.bridge.exec64 1
    else
        setprop ro.dalvik.vm.native.bridge 0
        setprop ro.product.cpu.abilist $_abilist
        setprop ro.product.cpu.abilist32 x86
        setprop ro.product.cpu.abilist64 x86_64 2>/dev/null
    fi
    if [ -f /system/bin/houdini ]; then
        mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null
        [ -f /system/etc/binfmt_misc/arm_exe ] && cp /system/etc/binfmt_misc/arm_exe /proc/sys/fs/binfmt_misc/register
        [ -f /system/etc/binfmt_misc/arm_dyn ] && cp /system/etc/binfmt_misc/arm_dyn /proc/sys/fs/binfmt_misc/register
        [ -f /system/etc/binfmt_misc/arm64_exe ] && cp /system/etc/binfmt_misc/arm64_exe /proc/sys/fs/binfmt_misc/register
        [ -f /system/etc/binfmt_misc/arm64_dyn ] && cp /system/etc/binfmt_misc/arm64_dyn /proc/sys/fs/binfmt_misc/register
    fi
}
if [ "setabi" == "$1" ]; then
    armabi
fi
EOF

cat >/tmp/houdini-props.append <<'EOF'
ro.product.cpu.abilist=x86_64,x86,arm64-v8a,armeabi-v7a,armeabi
ro.product.cpu.abilist32=x86,armeabi-v7a,armeabi
ro.product.cpu.abilist64=x86_64,arm64-v8a
ro.dalvik.vm.native.bridge=libhoudini.so
ro.enable.native.bridge.exec=1
ro.enable.native.bridge.exec64=1
ro.dalvik.vm.isa.arm=x86
ro.dalvik.vm.isa.arm64=x86_64
EOF

adb -s "$SERIAL" push /tmp/init.genymotion.abi.sh /system/vendor/bin/init.genymotion.abi.sh
adb -s "$SERIAL" shell "chmod 755 /system/vendor/bin/init.genymotion.abi.sh"
adb -s "$SERIAL" shell "wc -c /system/vendor/bin/init.genymotion.abi.sh; grep abilist64 /system/vendor/bin/init.genymotion.abi.sh"

adb -s "$SERIAL" push /tmp/houdini-props.append /data/local/tmp/houdini-props.append
adb -s "$SERIAL" shell "sed -i -e 's/^ro.dalvik.vm.native.bridge=/#nb /' -e 's/^ro.product.cpu.abilist=/#al /' -e 's/^ro.product.cpu.abilist32=/#al32 /' -e 's/^ro.product.cpu.abilist64=/#al64 /' /system/build.prop"
adb -s "$SERIAL" shell "grep -q 'arm64-v8a' /system/build.prop || cat /data/local/tmp/houdini-props.append >> /system/build.prop"
adb -s "$SERIAL" shell "tail -12 /system/build.prop"
adb -s "$SERIAL" shell sync

echo "Stopping and restarting VM..."
gmtool admin stop "$NAME"
sleep 5
setsid /root/genymotion/genymotion/player --vm-name "$NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player-a11.log 2>&1 &
sleep 8

for i in $(seq 1 48); do
  SERIAL="$(gmtool admin list | awk -F'|' '/On/{gsub(/ /,"",$2); print $2; exit}')"
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  b="$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
  echo "round=$i serial=$SERIAL boot=$b"
  if [[ "$b" == "1" ]]; then
    echo -n "abilist="; adb -s "$SERIAL" shell getprop ro.product.cpu.abilist
    echo -n "abilist64="; adb -s "$SERIAL" shell getprop ro.product.cpu.abilist64
    echo -n "bridge="; adb -s "$SERIAL" shell getprop ro.dalvik.vm.native.bridge
    echo -n "isa64="; adb -s "$SERIAL" shell getprop ro.dalvik.vm.isa.arm64
    adb -s "$SERIAL" shell "wc -c /system/vendor/bin/init.genymotion.abi.sh; grep abilist64 /system/vendor/bin/init.genymotion.abi.sh || true"
    echo ARM64_ABI_OK
    exit 0
  fi
  sleep 5
done
echo ARM64_ABI_BOOT_TIMEOUT >&2
exit 1
