#!/usr/bin/env bash
# 在 ARM64 Linux 上安裝官方 Android SDK + arm64-v8a AVD（不做轉譯）
# Usage: sudo bash scripts/arm64-linux-install-android-emulator.sh
#
# 注意：Google sdkmanager 在 linux-aarch64 宿主上沒有 "emulator" 套件，
# 需另行下載 AOSP CI 的 sdk-repo-linux_aarch64-emulator-*.zip。
set -euo pipefail

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "ERROR: 本腳本必須在 aarch64 宿主執行（目前: $(uname -m)）" >&2
  echo "本機 x86/WSL 不能當 ARM64 伺服器；請在 ARM64 雲主機或實體 ARM 伺服器上執行。" >&2
  exit 1
fi

if [[ ! -e /dev/kvm ]]; then
  if [[ "${ALIOTH_ALLOW_NO_KVM:-}" == "1" ]]; then
    echo "WARN: /dev/kvm 不存在；ALIOTH_ALLOW_NO_KVM=1，僅允許本機嵌套演練（正式伺服器必須有 KVM）" >&2
  else
    echo "ERROR: /dev/kvm 不存在，無法 KVM 加速 Emulator" >&2
    echo "提示: 本機 QEMU 嵌套 VM 可設 ALIOTH_ALLOW_NO_KVM=1 僅做安裝演練；正式環境必須原生 ARM64 + KVM。" >&2
    exit 1
  fi
fi

SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
AVD_NAME="${ALIOTH_AVD_NAME:-Alioth_ARM64_API30}"
API=30
IMG="system-images;android-${API};google_apis;arm64-v8a"
CMDLINE_VER="11076708"
ZIP_URL="https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_VER}_latest.zip"
# AOSP CI 仍可下載的 linux_aarch64 Emulator（約 1.6GB；sdkmanager 無此包）
EMU_BUILD="${ALIOTH_EMU_BUILD:-8632828}"
EMU_ZIP_URL="${ALIOTH_EMU_ZIP_URL:-https://ci.android.com/builds/submitted/${EMU_BUILD}/emulator-linux_aarch64/latest/sdk-repo-linux_aarch64-emulator-${EMU_BUILD}.zip}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl unzip openjdk-17-jdk-headless libpulse0 libnss3 libxcomposite1 libxdamage1 adb || \
  apt-get install -y curl unzip openjdk-17-jdk-headless libpulse0 adb

mkdir -p "$SDK_ROOT/cmdline-tools"
if [[ ! -x "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]]; then
  TMP="$(mktemp -d)"
  curl -fsSL "$ZIP_URL" -o "$TMP/cmdline-tools.zip"
  unzip -q "$TMP/cmdline-tools.zip" -d "$TMP"
  rm -rf "$SDK_ROOT/cmdline-tools/latest"
  mkdir -p "$SDK_ROOT/cmdline-tools/latest"
  if [[ -d "$TMP/cmdline-tools" ]]; then
    mv "$TMP/cmdline-tools"/* "$SDK_ROOT/cmdline-tools/latest/"
  else
    mv "$TMP"/* "$SDK_ROOT/cmdline-tools/latest/" 2>/dev/null || true
  fi
  rm -rf "$TMP"
fi

export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-arm64}"
export ANDROID_SDK_ROOT="$SDK_ROOT"
export ANDROID_HOME="$SDK_ROOT"
export PATH="$SDK_ROOT/cmdline-tools/latest/bin:$SDK_ROOT/platform-tools:$SDK_ROOT/emulator:$PATH"

yes | sdkmanager --licenses >/dev/null || true

# 先手動安裝 linux_aarch64 Emulator（否則 sdkmanager 裝映像會因缺少 emulator 依賴失敗）
if [[ ! -x "$SDK_ROOT/emulator/emulator" ]]; then
  echo "Downloading linux_aarch64 emulator build=$EMU_BUILD ..."
  TMP="$(mktemp -d)"
  # 支援 file:// 本機已上傳的 zip，或直接 https；CI 頁面需再解析 artifactUrl
  if [[ "$EMU_ZIP_URL" == file://* ]]; then
    SRC="${EMU_ZIP_URL#file://}"
    cp -f "$SRC" "$TMP/emulator.zip"
  elif [[ "$EMU_ZIP_URL" == /* ]]; then
    cp -f "$EMU_ZIP_URL" "$TMP/emulator.zip"
  else
    # CI 入口回 HTML，需抽出 GCS 簽名 URL
    PAGE="$(curl -fsSL "$EMU_ZIP_URL" || true)"
    SIGNED="$(printf '%s' "$PAGE" | sed -n 's/.*"artifactUrl":"\([^"]*\)".*/\1/p' | head -1 | sed 's/\\u0026/\&/g')"
    if [[ -n "$SIGNED" ]]; then
      curl -fL --progress-bar -o "$TMP/emulator.zip" "$SIGNED"
    else
      curl -fL --progress-bar -o "$TMP/emulator.zip" "$EMU_ZIP_URL"
    fi
  fi
  # 拒絕把 HTML 當 zip
  if ! unzip -t "$TMP/emulator.zip" >/dev/null 2>&1; then
    echo "ERROR: emulator zip 無效（可能下到 HTML）。SIZE=$(wc -c <"$TMP/emulator.zip")" >&2
    head -c 200 "$TMP/emulator.zip" >&2 || true
    exit 9
  fi
  rm -rf "$SDK_ROOT/emulator"
  unzip -q "$TMP/emulator.zip" -d "$SDK_ROOT"
  if [[ ! -d "$SDK_ROOT/emulator" ]] && [[ -d "$TMP/emulator" ]]; then
    mv "$TMP/emulator" "$SDK_ROOT/emulator"
  fi
  rm -rf "$TMP"
fi
if [[ ! -f "$SDK_ROOT/emulator/package.xml" ]]; then
  REV="$(grep -E '^Pkg.Revision=' "$SDK_ROOT/emulator/source.properties" 2>/dev/null | cut -d= -f2 || echo 31.3.9)"
  MAJOR="$(echo "$REV" | cut -d. -f1)"
  MINOR="$(echo "$REV" | cut -d. -f2)"
  MICRO="$(echo "$REV" | cut -d. -f3 | cut -d- -f1)"
  cat >"$SDK_ROOT/emulator/package.xml" <<EOF
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02"
                xmlns:ns3="http://schemas.android.com/repository/android/common/01"
                xmlns:ns4="http://schemas.android.com/repository/android/generic/01"
                xmlns:ns5="http://schemas.android.com/sdk/android/repo/repository2/01"
                xmlns:ns6="http://schemas.android.com/sdk/android/repo/repository2/02"
                xmlns:ns7="http://schemas.android.com/sdk/android/repo/sys-img2/01"
                xmlns:ns8="http://schemas.android.com/sdk/android/repo/sys-img2/02"
                xmlns:ns9="http://schemas.android.com/repository/android/license/01">
  <localPackage path="emulator" obsolete="false">
    <type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ns4:genericDetailsType"/>
    <revision><major>${MAJOR}</major><minor>${MINOR}</minor><micro>${MICRO}</micro></revision>
    <display-name>Android Emulator</display-name>
  </localPackage>
</ns2:repository>
EOF
fi
chmod +x "$SDK_ROOT/emulator/emulator" || true
file "$SDK_ROOT/emulator/emulator" || true
"$SDK_ROOT/emulator/emulator" -version | head -5 || true

# 再裝映像與平台（此時本地已有 emulator package）
sdkmanager --install \
  "platforms;android-${API}" \
  "$IMG"

# platform-tools：優先用發行版 adb（原生 aarch64）；sdkmanager 常給 x86_64
mkdir -p "$SDK_ROOT/platform-tools"
if command -v adb >/dev/null 2>&1; then
  ln -sfn "$(command -v adb)" "$SDK_ROOT/platform-tools/adb"
else
  sdkmanager --install "platform-tools" || true
fi

echo no | avdmanager create avd -n "$AVD_NAME" -k "$IMG" -d "pixel" --force

AVD_DIR="${ANDROID_AVD_HOME:-$HOME/.android/avd}/${AVD_NAME}.avd"
if [[ -f "$AVD_DIR/config.ini" ]]; then
  sed -i 's/^hw.gpu.mode=.*/hw.gpu.mode=swiftshader_indirect/' "$AVD_DIR/config.ini" || true
  grep -q '^hw.gpu.mode=' "$AVD_DIR/config.ini" || echo 'hw.gpu.mode=swiftshader_indirect' >>"$AVD_DIR/config.ini"
  sed -i 's/^fastboot.forceColdBoot=.*/fastboot.forceColdBoot=yes/' "$AVD_DIR/config.ini" || true
fi

cat >/etc/profile.d/alioth-android-sdk.sh <<EOF
export ANDROID_SDK_ROOT=$SDK_ROOT
export ANDROID_HOME=$SDK_ROOT
export PATH=\$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:\$ANDROID_SDK_ROOT/platform-tools:\$ANDROID_SDK_ROOT/emulator:\$PATH
EOF

echo "INSTALL_OK sdk=$SDK_ROOT avd=$AVD_NAME emu_build=$EMU_BUILD"
echo "Next: bash scripts/arm64-linux-start-android-emulator.sh"
echo "Verify: uname -m; ls /dev/kvm; emulator -list-avds"
