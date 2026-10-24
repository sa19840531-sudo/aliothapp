#!/usr/bin/env bash
# 以 D3D12/NVIDIA 啟動指定 Genymotion 虛擬機 player
set -euo pipefail

VM_NAME="${1:-Alioth Nexus5 A11}"
BIN="/root/genymotion/genymotion"
export PATH="/root/genymotion/genymotion:/root/genymotion/genymotion/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export LIBGL_ALWAYS_SOFTWARE=0
export GALLIUM_DRIVER=d3d12
export MESA_LOADER_DRIVER_OVERRIDE=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME="${MESA_D3D12_DEFAULT_ADAPTER_NAME:-NVIDIA}"
export __GLX_VENDOR_LIBRARY_NAME=mesa

if [[ ! -x "$BIN/player" ]]; then
  echo "ERROR: player not found" >&2
  exit 1
fi

# 若該 VM 已 On 則跳過
if "$BIN/gmtool" admin list 2>/dev/null | grep -F "$VM_NAME" | grep -q 'On'; then
  echo "already running"
  "$BIN/gmtool" admin list
  exit 0
fi

setsid "$BIN/player" --vm-name "$VM_NAME" --referer launchpad \
  </dev/null >/tmp/genymotion-player.log 2>&1 &
echo "player pid=$! vm=$VM_NAME"
sleep 6
"$BIN/gmtool" admin list || true
tail -30 /tmp/genymotion-player.log || true
pgrep -a qemu-system | head || true
