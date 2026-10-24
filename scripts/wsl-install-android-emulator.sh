#!/usr/bin/env bash
# 在 Ubuntu WSL 安裝 Android SDK Emulator（KVM），供本機 Linux Demo 使用
set -euo pipefail

SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
AVD_NAME="${ALIOTH_AVD_NAME:-Alioth_WSL_API30}"
API=30
IMAGE="system-images;android-${API};google_apis;x86_64"

if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm missing; abort." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq openjdk-17-jdk-headless wget unzip libpulse0 libnss3 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libgtk-3-0 >/dev/null

mkdir -p "$SDK_ROOT/cmdline-tools"
cd /tmp
if [[ ! -x "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]]; then
  echo "Downloading cmdline-tools..."
  wget -q -O cmdline-tools.zip "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
  rm -rf /tmp/cmdline-tools-unpack
  unzip -q cmdline-tools.zip -d /tmp/cmdline-tools-unpack
  rm -rf "$SDK_ROOT/cmdline-tools/latest"
  mkdir -p "$SDK_ROOT/cmdline-tools/latest"
  mv /tmp/cmdline-tools-unpack/cmdline-tools/* "$SDK_ROOT/cmdline-tools/latest/"
fi

export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(which javac)")")")"
export ANDROID_HOME="$SDK_ROOT"
export ANDROID_SDK_ROOT="$SDK_ROOT"
export PATH="$SDK_ROOT/cmdline-tools/latest/bin:$SDK_ROOT/platform-tools:$SDK_ROOT/emulator:$PATH"

yes | sdkmanager --sdk_root="$SDK_ROOT" --licenses >/tmp/sdk-licenses.log || true
sdkmanager --sdk_root="$SDK_ROOT" \
  "platform-tools" \
  "emulator" \
  "platforms;android-${API}" \
  "$IMAGE"

echo "no" | avdmanager create avd -n "$AVD_NAME" -k "$IMAGE" -d pixel_4 --force

# 寫入加速相關設定
AVD_DIR="${HOME}/.android/avd/${AVD_NAME}.avd"
if [[ -f "$AVD_DIR/config.ini" ]]; then
  grep -q '^hw.gpu.enabled=' "$AVD_DIR/config.ini" || echo 'hw.gpu.enabled=yes' >> "$AVD_DIR/config.ini"
  sed -i 's/^hw.gpu.mode=.*/hw.gpu.mode=auto/' "$AVD_DIR/config.ini" 2>/dev/null || echo 'hw.gpu.mode=auto' >> "$AVD_DIR/config.ini"
fi

echo "OK SDK=$SDK_ROOT AVD=$AVD_NAME"
emulator -accel-check || true
avdmanager list avd
