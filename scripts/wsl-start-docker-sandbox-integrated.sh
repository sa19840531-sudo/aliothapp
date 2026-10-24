#!/usr/bin/env bash
# WSL：Docker 統一承載 Mobile Host + KVM Emulator（跨服沙盒）
# ReDroid 需要 binderfs；WSL 核心沒有，故 Emulator 用官方 AVD（同機 KVM）。
set -euo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:$PATH"
ROOT=/mnt/f/aliothG
INFRA="$ROOT/infra"
ADB=/opt/android-sdk/platform-tools/adb
PY=/opt/alioth-wsl-venv/bin/python

echo "=== preflight ==="
test -e /dev/kvm || { echo "NO_KVM"; exit 1; }
docker info >/dev/null || { echo "NO_DOCKER"; exit 1; }

# 停舊 ReDroid / 舊 host（避免埠衝突）
docker rm -f alioth-redroid 2>/dev/null || true
docker rm -f alioth-mobile-host 2>/dev/null || true
pkill -f mobile_demo_host_runner 2>/dev/null || true
fuser -k 8765/tcp 2>/dev/null || true
sleep 1

# --- Emulator ---
if ! pgrep -f 'qemu-system-x86_64' >/dev/null 2>&1; then
  echo "starting Google Emulator (KVM)..."
  bash "$ROOT/scripts/wsl-start-android-emulator.sh"
else
  echo "qemu already running"
fi

echo "waiting adb boot..."
$ADB start-server >/dev/null 2>&1 || true
READY=""
for i in $(seq 1 60); do
  $ADB connect 127.0.0.1:5555 >/dev/null 2>&1 || true
  while read -r serial state _; do
    [[ "${state:-}" != "device" ]] && continue
    [[ "$serial" != emulator-* && "$serial" != 127.0.0.1:* ]] && continue
    boot="$($ADB -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    if [[ "$boot" == "1" ]]; then
      READY="$serial"
      break 2
    fi
  done < <($ADB devices | awk 'NR>1{print}')
  echo "wait_$i"
  sleep 5
done
[[ -n "$READY" ]] || { echo "EMULATOR_BOOT_TIMEOUT"; exit 1; }
echo "adb ready: $READY"

# --- Mobile Host in Docker (host network → 直連本機 adb) ---
cd "$INFRA"
echo "building/starting alioth-mobile-host (host network)..."
docker build -t alioth-mobile-host:local -f docker/mobile-host/Dockerfile ..

docker run -d --name alioth-mobile-host --restart unless-stopped \
  --network host \
  -e ALIOTH_DEVICE_BACKEND=redroid \
  -e ALIOTH_REDROID_ADB=127.0.0.1:5555 \
  -e ALIOTH_MOBILE_DEMO_FORCE_LOCAL=1 \
  -e ALIOTH_MOBILE_DEMO_HOST_BIND=0.0.0.0 \
  -e ALIOTH_MOBILE_DEMO_HOST_PORT=8765 \
  -e ALIOTH_MOBILE_DEMO_DATA_DIR=/workspace/.alioth-mobile-demo \
  -e ALIOTH_WORKSPACE_HOST=/workspace \
  -e ALIOTH_MOBILE_DEMO_STAGE=dev \
  -e ANDROID_HOME=/opt/android-sdk \
  -e ANDROID_SDK_ROOT=/opt/android-sdk \
  -v "$ROOT:/workspace" \
  alioth-mobile-host:local

ok=0
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8765/health >/tmp/mh.json; then
    echo "HEALTH_OK"
    cat /tmp/mh.json; echo
    ok=1
    break
  fi
  echo "wait_host_$i"
  sleep 2
done
[[ "$ok" == "1" ]] || { docker logs --tail 50 alioth-mobile-host; exit 1; }

# 確認 host 容器內 adb 可見模擬器
docker exec alioth-mobile-host /opt/android-sdk/platform-tools/adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
docker exec alioth-mobile-host /opt/android-sdk/platform-tools/adb devices -l || true

echo "WSL_DOCKER_SANDBOX_OK"
echo "  Mobile Host: http://127.0.0.1:8765/health (Docker)"
echo "  Emulator ADB: 127.0.0.1:5555 (WSL KVM)"
echo "  Note: ReDroid 需 binderfs；本環境以 Google Emulator 暫代裝置面"
