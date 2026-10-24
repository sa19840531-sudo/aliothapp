# 主站 Docker（跨服模式：API 連遠端沙盒 192.168.33.128:8765）
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Root "infra")

$mainEnv = Join-Path $Root "infra\env\.env.cross-server.main"
$localEnv = Join-Path $Root "infra\env\.env.local"
if (-not (Test-Path $localEnv)) {
    throw "缺少 $localEnv，請先從 .env.local.example 複製"
}

# 將 cross-server 變數 merge 進 .env.local（不覆寫 DB/LLM 等）
& python (Join-Path $Root "scripts\merge_env_snippet.py") `
    --target $localEnv `
    --snippet $mainEnv `
    --marker "CROSS_SERVER_MOBILE_DEMO"

Write-Host "已 merge 跨服 Mobile Demo 設定到 .env.local" -ForegroundColor Green
Write-Host "主站 → 沙盒: http://192.168.33.128:8765" -ForegroundColor Cyan

docker compose --env-file env/.env.local -f docker-compose.yml up -d alioth-postgres alioth-redis alioth-api alioth-worker

Write-Host ""
Write-Host "Demo: http://127.0.0.1:8000/api/v1/mobile-demo/demo" -ForegroundColor Yellow
Write-Host "驗證: curl http://127.0.0.1:8000/api/v1/mobile-demo/version?format=json" -ForegroundColor Yellow
