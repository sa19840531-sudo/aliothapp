#!/usr/bin/env bash
# 以 Mesa D3D12 → NVIDIA 啟動 Genymotion Desktop（WSLg）
set -euo pipefail

BIN="/root/genymotion/genymotion"
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

# WSL GPU（已驗證對 OpenGL 有效）
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME="${MESA_D3D12_DEFAULT_ADAPTER_NAME:-NVIDIA}"
export __GLX_VENDOR_LIBRARY_NAME=mesa

if [[ ! -x "$BIN/genymotion" ]]; then
  echo "ERROR: Genymotion not installed at $BIN" >&2
  exit 1
fi

echo "GPU probe:"
glxinfo -B 2>/dev/null | grep -E 'Device:|renderer string:|Accelerated:' || true

cd "$BIN"
exec ./genymotion "$@"
