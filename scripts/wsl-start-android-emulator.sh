#!/usr/bin/env bash
# 啟動 WSL 內官方 Android Emulator（KVM + 盡量用 NVIDIA GPU）
# WSL2 圖形：Mesa Gallium d3d12 → Direct3D12 → 本機 NVIDIA
set -euo pipefail
SDK="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
AVD="${ALIOTH_AVD_NAME:-Alioth_WSL_API30}"
export ANDROID_HOME="$SDK"
export ANDROID_SDK_ROOT="$SDK"
export PATH="$SDK/emulator:$SDK/platform-tools:$PATH"

# --- WSL GPU（OpenGL via D3D12 / NVIDIA）---
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME="${MESA_D3D12_DEFAULT_ADAPTER_NAME:-NVIDIA}"
export __GLX_VENDOR_LIBRARY_NAME=mesa
# 避免誤用 llvmpipe ICD 當唯一裝置時仍可走 GL
export VK_ICD_FILENAMES="${VK_ICD_FILENAMES:-}"

if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm missing" >&2
  exit 1
fi
if [[ ! -e /dev/dxg ]]; then
  echo "WARNING: /dev/dxg missing — WSL GPU 可能不可用" >&2
fi

# 預設 host GPU；失敗時可 ALIOTH_EMU_GPU=swiftshader_indirect
GPU_MODE="${ALIOTH_EMU_GPU:-host}"

if pgrep -f "qemu-system-x86_64" >/dev/null 2>&1; then
  echo "qemu already running; skip start (kill first to apply GPU change)"
  pgrep -a qemu-system | head || true
  exit 0
fi

WINDOW_ARGS=("-no-window")
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
if [[ "${ALIOTH_EMU_WINDOW:-0}" == "1" ]]; then
  WINDOW_ARGS=()
  export DISPLAY="${DISPLAY:-:0}"
  export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
  export QT_QPA_PLATFORM=xcb
fi

echo "GPU probe (d3d12):"
GALLIUM_DRIVER=d3d12 MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA glxinfo -B 2>/dev/null | grep -E 'Device:|renderer string:|Accelerated:' || true

setsid emulator -avd "$AVD" -gpu "$GPU_MODE" -accel on -no-metrics -no-snapshot-load \
  "${WINDOW_ARGS[@]}" \
  </dev/null >/tmp/alioth-wsl-emulator.log 2>&1 &
echo "started pid=$! gpu=$GPU_MODE window=${ALIOTH_EMU_WINDOW:-0} gallium=d3d12"
sleep 8
tail -80 /tmp/alioth-wsl-emulator.log || true
pgrep -a qemu-system | head || true
if ! pgrep -f "qemu-system-x86_64" >/dev/null 2>&1; then
  echo "ERROR: emulator failed; see /tmp/alioth-wsl-emulator.log" >&2
  exit 1
fi
# 確認不是純 SwiftShader
if grep -q "SwiftShader" /tmp/alioth-wsl-emulator.log && ! grep -qiE "NVIDIA|D3D12|GeForce|host" /tmp/alioth-wsl-emulator.log; then
  echo "WARNING: log still looks like software GPU" >&2
fi
grep -E "Graphics Adapter|gpu|NVIDIA|SwiftShader|D3D12|llvmpipe|emuglConfig" /tmp/alioth-wsl-emulator.log | head -20 || true
echo EMULATOR_UP
