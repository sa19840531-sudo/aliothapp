#!/usr/bin/env bash
# Alioth WSL/Linux 部署腳本
# 用法：bash scripts/setup-linux.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Alioth Linux 部署 ==="

# 1. 系統依賴
echo "[1/5] 安裝系統依賴..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip curl git ca-certificates

# 2. Python 虛擬環境
echo "[2/5] 建立 Python 虛擬環境..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"

# 3. 環境變數
echo "[3/5] 檢查環境變數..."
[ -f infra/env/.env.local ] || cp infra/env/.env.local.example infra/env/.env.local
[ -f .env ] || cp .env.example .env

# 4. Docker 部署（優先使用 Docker Desktop WSL 整合）
echo "[4/5] 啟動 Docker 容器..."
export ALIOTH_ENV_FILE="./env/.env.local"

if docker info &>/dev/null 2>&1; then
    docker compose -f infra/docker-compose.yml up --build -d
    echo "  等待容器就緒..."
    sleep 12
    docker exec alioth-api python -m alembic -c services/api/migrations/alembic.ini upgrade head
    echo "  資料庫遷移完成"
else
    echo "  [警告] Docker 未就緒"
    echo "  請在 Docker Desktop 設定中啟用本發行版的 WSL 整合，或執行: sudo service docker start"
    echo "  改為本機直跑 API..."
    nohup uvicorn services.api.app.main:app --host 0.0.0.0 --port 8000 > /tmp/alioth-api.log 2>&1 &
    sleep 3
fi

# 5. 健康檢查
echo "[5/5] 健康檢查..."
sleep 5
if curl -sf http://localhost:8000/api/v1/health > /dev/null 2>&1; then
    echo ""
    echo "=== 部署成功 ==="
    curl -s http://localhost:8000/api/v1/health | python3 -m json.tool
else
    echo "API 尚未就緒，請檢查日誌"
fi

echo ""
echo "=== 服務位址 ==="
echo "  API 文件:  http://localhost:8000/docs"
echo "  管理後台:  http://localhost:5173"
echo "  健康檢查:  http://localhost:8000/api/v1/health"
