#!/usr/bin/env bash
# 僅啟動模擬器沙盒（ReDroid + Mobile Host）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"

bash "$ROOT/scripts/linux-prepare-redroid-host.sh"

cd "$INFRA"
ENV_FILE="${ALIOTH_EMULATOR_ENV_FILE:-env/.env.local}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少运行时环境文件：$ENV_FILE，请从 sandbox 示例复制后填写 token" >&2
  exit 1
fi
ENV_FILE="$(realpath "$ENV_FILE")"
export ALIOTH_SANDBOX_SERVICE_ENV_FILE="$ENV_FILE"
HOST_TOKEN="$(grep -E '^ALIOTH_MOBILE_DEMO_HOST_TOKEN=' "$ENV_FILE" | tail -n 1 | cut -d '=' -f 2- || true)"
HOST_TOKEN_NORMALIZED="$(printf '%s' "$HOST_TOKEN" | tr -d "\"'" | xargs)"
if [[ -z "$HOST_TOKEN_NORMALIZED" || "$HOST_TOKEN_NORMALIZED" =~ ^(change-me|change-me-|your-|replace-|<) ]]; then
  echo "ALIOTH_MOBILE_DEMO_HOST_TOKEN 缺失或仍为占位值" >&2
  exit 1
fi
docker compose \
  --env-file "$ENV_FILE" \
  -f docker-compose.emulator-sandbox.yml \
  --profile emulator \
  up -d --build

echo ""
echo "沙盒已啟動。對外埠："
echo "  ReDroid ADB : ${ALIOTH_REDROID_PORT:-5555}"
echo "  Mobile Host : ${ALIOTH_MOBILE_HOST_PORT:-8765}"
echo ""
echo "主站請設："
echo "  ALIOTH_MOBILE_DEMO_HOST_EXECUTOR=http://<sandbox-ip>:8765"
echo "  ALIOTH_MOBILE_DEMO_REMOTE_SYNC=1"
echo ""
bash "$ROOT/scripts/verify-cross-server-emulator.sh" --local-only
