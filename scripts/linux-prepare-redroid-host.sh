#!/usr/bin/env bash
# 在 Linux 宿主準備 ReDroid 所需 binderfs 與 KVM（需 root 或 sudo）
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[skip] 非 Linux，略過 ReDroid 宿主準備"
  exit 0
fi

if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm 不存在。ReDroid 需硬體虛擬化（KVM）。"
  echo "       Windows Docker Desktop 通常無法跑 ReDroid，請改用 Linux 伺服器或本機雷電。"
  exit 1
fi

run_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "ERROR: 需要 root 權限以載入 binder 模組"
    exit 1
  fi
}

run_root modprobe binder_linux devices="binder,hwbinder,vndbinder" 2>/dev/null || true
run_root mkdir -p /dev/binderfs
if ! mountpoint -q /dev/binderfs 2>/dev/null; then
  run_root mount -t binder binder /dev/binderfs
fi

echo "OK: KVM=$(test -r /dev/kvm && echo yes || echo no), binderfs=$(mountpoint -q /dev/binderfs && echo mounted || echo missing)"
ls /dev/binderfs 2>/dev/null | head -5 || true
