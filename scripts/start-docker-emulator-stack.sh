#!/usr/bin/env bash
# 啟動 Alioth 平台 + Docker Android 模擬器（ReDroid + Mobile Host）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"

bash "$ROOT/scripts/linux-prepare-redroid-host.sh"

cd "$INFRA"
DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! $DOCKER_BIN info >/dev/null 2>&1; then
  DOCKER_BIN="sudo docker"
fi

# 运行时只接受已填写的 .env.local；示例文件仅用于人工复制和编辑
ENV_FILE="${ALIOTH_EMULATOR_ENV_FILE:-env/.env.local}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少运行时环境文件：$ENV_FILE，请从示例复制后填写 token" >&2
  exit 1
fi
HOST_TOKEN="$(grep -E '^ALIOTH_MOBILE_DEMO_HOST_TOKEN=' "$ENV_FILE" | tail -n 1 | cut -d '=' -f 2- || true)"
HOST_TOKEN_NORMALIZED="$(printf '%s' "$HOST_TOKEN" | tr -d "\"'" | xargs)"
if [[ -z "$HOST_TOKEN_NORMALIZED" || "$HOST_TOKEN_NORMALIZED" =~ ^(change-me|change-me-|your-|replace-|<) ]]; then
  echo "ALIOTH_MOBILE_DEMO_HOST_TOKEN 缺失或仍为占位值" >&2
  exit 1
fi

$DOCKER_BIN compose \
  --env-file "$ENV_FILE" \
  -f docker-compose.yml \
  -f docker-compose.emulator.yml \
  --profile emulator \
  up -d --build \
  alioth-postgres \
  alioth-redis \
  alioth-redroid \
  alioth-mobile-host \
  alioth-api \
  alioth-worker

echo ""
echo "等待 ReDroid ADB 就緒…"
DOCKER_BIN="$DOCKER_BIN" bash "$ROOT/scripts/verify-docker-emulator.sh"

echo ""
echo "提示：请确认 infra/env/.env.local 已设置："
echo "  ALIOTH_DEVICE_BACKEND=redroid"
echo "  ALIOTH_MOBILE_DEMO_HOST_EXECUTOR=http://alioth-mobile-host:8765"
echo "（或 Windows 本机 Host Runner 时用 http://host.docker.internal:8765）"
echo ""
echo "Backend: redroid (alioth-redroid:5555 → alioth-mobile-host:8765)"
echo "青云客对照: Emulator=alioth-redroid | Agent侧=api+worker+mobile-host | DB=postgres+redis"
echo "可选: --profile emulator-multi（第二台） / --profile appium（Appium 预留）"
