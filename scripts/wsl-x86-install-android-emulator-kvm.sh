#!/usr/bin/env bash
# 在 x86_64 Linux（WSL/VMware）安裝 KVM + Android Emulator + API30 x86_64 AVD
set -euo pipefail

echo "=== host probe ==="
uname -a
echo "arch=$(uname -m)"
ls -l /dev/kvm || { echo "ERROR: no /dev/kvm"; exit 1; }
sudo kvm-ok || true

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get install -y \
  curl unzip openjdk-17-jdk-headless \
  qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils \
  cpu-checker \
  libpulse0 libnss3 libxcomposite1 libxdamage1 libxrandr2 libgl1 libegl1 \
  mesa-utils adb \
  || true

# 目前使用者加入 kvm（WSL 預設常是 root；桌面使用者亦處理）
if id -u "${SUDO_USER:-}" >/dev/null 2>&1; then
  sudo usermod -aG kvm,libvirt "${SUDO_USER}"
fi
sudo usermod -aG kvm,libvirt "$(whoami)" || true

SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/android-sdk}"
AVD_NAME="${ALIOTH_AVD_NAME:-Alioth_API30_x86_64}"
API=30
IMG="system-images;android-${API};google_apis;x86_64"
CMDLINE_VER="11076708"
ZIP_URL="https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_VER}_latest.zip"

mkdir -p "$SDK_ROOT/cmdline-tools"
if [[ ! -x "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]]; then
  TMP="$(mktemp -d)"
  curl -fsSL "$ZIP_URL" -o "$TMP/cmdline-tools.zip"
  unzip -q "$TMP/cmdline-tools.zip" -d "$TMP"
  rm -rf "$SDK_ROOT/cmdline-tools/latest"
  mkdir -p "$SDK_ROOT/cmdline-tools/latest"
  mv "$TMP/cmdline-tools"/* "$SDK_ROOT/cmdline-tools/latest/"
  rm -rf "$TMP"
fi

export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
export ANDROID_SDK_ROOT="$SDK_ROOT"
export ANDROID_HOME="$SDK_ROOT"
export PATH="$SDK_ROOT/cmdline-tools/latest/bin:$SDK_ROOT/platform-tools:$SDK_ROOT/emulator:$PATH"

yes | sdkmanager --licenses >/dev/null || true
sdkmanager --install \
  "platform-tools" \
  "emulator" \
  "platforms;android-${API}" \
  "$IMG"

echo no | avdmanager create avd -n "$AVD_NAME" -k "$IMG" -d "pixel" --force
AVD_DIR="${ANDROID_AVD_HOME:-$HOME/.android/avd}/${AVD_NAME}.avd"
if [[ -f "$AVD_DIR/config.ini" ]]; then
  grep -q '^hw.gpu.mode=' "$AVD_DIR/config.ini" || echo 'hw.gpu.mode=swiftshader_indirect' >>"$AVD_DIR/config.ini"
  sed -i 's/^hw.gpu.mode=.*/hw.gpu.mode=swiftshader_indirect/' "$AVD_DIR/config.ini" || true
fi

mkdir -p "$HOME/bin"
cat >"$HOME/bin/alioth-start-emulator.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export ANDROID_SDK_ROOT="$SDK_ROOT"
export PATH="\$ANDROID_SDK_ROOT/emulator:\$ANDROID_SDK_ROOT/platform-tools:\$PATH"
GPU="\${ALIOTH_EMU_GPU:-swiftshader_indirect}"
nohup emulator -avd $AVD_NAME -accel on -gpu "\$GPU" -no-audio -no-window \\
  >/tmp/alioth-emulator.log 2>&1 &
echo "pid=\$! log=/tmp/alioth-emulator.log"
for i in \$(seq 1 90); do
  if adb devices | grep -qE 'emulator-[0-9]+\\s+device'; then
    boot=\$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\\r')
    abi=\$(adb shell getprop ro.product.cpu.abi 2>/dev/null | tr -d '\\r')
    echo "round=\$i boot=\$boot abi=\$abi"
    [[ "\$boot" == "1" ]] && { echo READY; exit 0; }
  else
    echo "round=\$i waiting..."
  fi
  sleep 5
done
echo BOOT_TIMEOUT
tail -40 /tmp/alioth-emulator.log || true
exit 1
EOF
chmod +x "$HOME/bin/alioth-start-emulator.sh"

echo "INSTALL_OK sdk=$SDK_ROOT avd=$AVD_NAME"
emulator -version | head -3 || true
echo "GPU probe:"; glxinfo -B 2>/dev/null | head -15 || echo "no glxinfo (ok for headless swiftshader)"
