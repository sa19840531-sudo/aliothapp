#!/usr/bin/env bash
# 驗證主站 ↔ 模擬器沙盒 跨伺服器連通性
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ONLY=0
if [[ "${1:-}" == "--local-only" ]]; then
  LOCAL_ONLY=1
fi

SANDBOX_HOST="${SANDBOX_HOST:-127.0.0.1}"
SANDBOX_PORT="${SANDBOX_PORT:-8765}"
MAIN_API_URL="${MAIN_API_URL:-http://127.0.0.1:8000}"
TOKEN="${ALIOTH_MOBILE_DEMO_HOST_TOKEN:-}"

AUTH=()
if [[ -n "$TOKEN" ]]; then
  AUTH=(-H "Authorization: Bearer ${TOKEN}")
fi

echo "=== verify cross-server emulator ==="
echo "sandbox: http://${SANDBOX_HOST}:${SANDBOX_PORT}"
echo "main api: ${MAIN_API_URL}"

health_url="http://${SANDBOX_HOST}:${SANDBOX_PORT}/health"
if ! curl -sf "${AUTH[@]}" "$health_url" >/tmp/alioth_sandbox_health.json; then
  echo "[fail] cannot reach sandbox health: $health_url"
  exit 1
fi
echo "[ok] sandbox health:"
cat /tmp/alioth_sandbox_health.json
echo ""

backend="$(python3 -c "import json; print(json.load(open('/tmp/alioth_sandbox_health.json')).get('device_backend',''))" 2>/dev/null || true)"
if [[ "$backend" != "redroid" ]]; then
  echo "[warn] sandbox device_backend=$backend (expected redroid)"
fi

if [[ "$LOCAL_ONLY" -eq 1 ]]; then
  echo "[skip] main api check (--local-only)"
  echo "=== done (sandbox only) ==="
  exit 0
fi

if curl -sf "${MAIN_API_URL}/api/v1/mobile-demo/version?format=json" >/tmp/alioth_main_ver.json; then
  echo "[ok] main api version:"
  cat /tmp/alioth_main_ver.json
  echo ""
  executor="$(python3 -c "import json; print(json.load(open('/tmp/alioth_main_ver.json')).get('host_executor',''))" 2>/dev/null || true)"
  if [[ -n "$executor" ]]; then
    echo "[info] main host_executor=$executor"
  fi
else
  echo "[warn] main api not reachable at ${MAIN_API_URL}"
fi

# 防火牆提示
if [[ "$SANDBOX_HOST" != "127.0.0.1" && "$SANDBOX_HOST" != "localhost" ]]; then
  echo ""
  echo "[checklist] 主站容器需能存取 http://${SANDBOX_HOST}:${SANDBOX_PORT}"
  echo "  - 開放沙盒 ${SANDBOX_PORT}/tcp（Mobile Host）"
  echo "  - 可選 ${ALIOTH_REDROID_PORT:-5555}/tcp（僅手動 adb 調試）"
  echo "  - 主站設 ALIOTH_MOBILE_DEMO_REMOTE_SYNC=1"
fi

echo "=== done ==="
