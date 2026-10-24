#!/usr/bin/env bash
# 在 Ubuntu WSL 安裝 Genymotion Desktop 3.10.0（需 /dev/kvm）
set -euo pipefail

INSTALL_DIR="${GENYMOTION_INSTALL_DIR:-/root/genymotion}"
RUN_FILE="/tmp/genymotion-3.10.0-linux_x64.run"
URL="https://dl.genymotion.com/releases/genymotion-3.10.0/genymotion-3.10.0-linux_x64.run"
EXPECTED_SHA="cb9924009a51b7d10e6009a0a2cd3e5fa5c53e0776cf65c4b42abbeaf015b7c7"

if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm missing; abort Genymotion install." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq libpulse-mainloop-glib0 libpulse0 bzip2 libxcb-cursor0 \
  libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 mesa-utils >/dev/null || true

if [[ ! -f "$RUN_FILE" ]]; then
  echo "Downloading Genymotion 3.10.0..."
  curl -L --retry 3 -o "$RUN_FILE" "$URL"
fi

echo "SHA256:"
sha256sum "$RUN_FILE"
ACTUAL="$(sha256sum "$RUN_FILE" | awk '{print $1}')"
if [[ "$ACTUAL" != "$EXPECTED_SHA" ]]; then
  echo "ERROR: SHA256 mismatch (got $ACTUAL)" >&2
  exit 1
fi

chmod +x "$RUN_FILE"
mkdir -p "$INSTALL_DIR"
echo "Installing to $INSTALL_DIR ..."
if ! "$RUN_FILE" -d "$INSTALL_DIR" --yes; then
  "$RUN_FILE" -d "$INSTALL_DIR" <<< "y"
fi

BIN_DIR="$INSTALL_DIR/genymotion"
test -x "$BIN_DIR/genymotion" && echo "OK: genymotion binary present"
test -x "$BIN_DIR/gmtool" && echo "OK: gmtool binary present"
kvm-ok || true
ls -l /dev/kvm /dev/dxg || true
echo "Install done: $BIN_DIR"
