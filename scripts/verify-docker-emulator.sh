#!/usr/bin/env bash
# 驗證 Docker ReDroid + Mobile Host 鏈路
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDROID_PORT="${ALIOTH_REDROID_PORT:-5555}"
HOST_PORT="${ALIOTH_MOBILE_HOST_PORT:-8765}"
API_PORT="${ALIOTH_API_PORT:-8000}"
ADB_ENDPOINT="127.0.0.1:${REDROID_PORT}"

adb_bin=""
for candidate in adb "${ANDROID_HOME:-}/platform-tools/adb" /opt/android-sdk/platform-tools/adb; do
  if command -v "$candidate" >/dev/null 2>&1; then
    adb_bin="$(command -v "$candidate")"
    break
  fi
  if [[ -x "$candidate" ]]; then
    adb_bin="$candidate"
    break
  fi
done

echo "=== verify docker emulator（青云客 Docker 集成）==="

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! $DOCKER_BIN info >/dev/null 2>&1; then
  DOCKER_BIN="sudo docker"
fi

if [[ "$(uname -s)" == "Linux" ]]; then
  if [[ -e /dev/kvm ]]; then
    echo "[ok] /dev/kvm present"
  else
    echo "[fail] /dev/kvm missing（需 Linux 宿主硬體虛擬化）"
    exit 1
  fi
  if mountpoint -q /dev/binderfs 2>/dev/null; then
    echo "[ok] binderfs mounted"
  else
    echo "[warn] binderfs 未掛載；請先: bash scripts/linux-prepare-redroid-host.sh"
  fi
fi

if $DOCKER_BIN ps --format '{{.Names}}' | grep -qx alioth-redroid; then
  echo "[ok] alioth-redroid running"
else
  echo "[fail] alioth-redroid not running"
  exit 1
fi

if $DOCKER_BIN ps --format '{{.Names}}' | grep -qx alioth-mobile-host; then
  echo "[ok] alioth-mobile-host running"
else
  echo "[warn] alioth-mobile-host not running (Windows 可改用手動 Host Runner)"
fi

if [[ -n "$adb_bin" ]]; then
  "$adb_bin" connect "$ADB_ENDPOINT" >/dev/null 2>&1 || true
  deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    if "$adb_bin" devices | grep -E "${REDROID_PORT}[[:space:]]+device" >/dev/null 2>&1; then
      boot=$("$adb_bin" -s "$ADB_ENDPOINT" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)
      if [[ "$boot" == "1" ]]; then
        echo "[ok] adb $ADB_ENDPOINT boot_completed=1"
        break
      fi
      echo "[wait] boot_completed=${boot:-empty}"
    else
      echo "[wait] adb connecting $ADB_ENDPOINT"
    fi
    sleep 3
  done
  if (( SECONDS >= deadline )); then
    echo "[fail] adb boot timeout"
    exit 1
  fi
else
  echo "[skip] host adb not found; rely on alioth-mobile-host container"
fi

if curl -sf "http://127.0.0.1:${API_PORT}/api/v1/mobile-demo/version?format=json" >/dev/null; then
  ver=$(curl -s "http://127.0.0.1:${API_PORT}/api/v1/mobile-demo/version?format=json")
  echo "[ok] mobile-demo API: $ver"
else
  echo "[warn] mobile-demo API not reachable on :${API_PORT}"
fi

echo "=== done ==="
