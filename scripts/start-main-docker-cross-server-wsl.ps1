# 主站 Docker：跨服模式指向 WSL Mobile Host（host.docker.internal:8765）
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Root "infra")

$docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
if (-not (Test-Path $docker)) {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { $docker = $cmd.Source } else { throw "找不到 docker.exe" }
}

$mainEnv = Join-Path $Root "infra\env\.env.cross-server.wsl"
$localEnv = Join-Path $Root "infra\env\.env.local"
if (-not (Test-Path $localEnv)) { throw "缺少 $localEnv" }

& python (Join-Path $Root "scripts\merge_env_snippet.py") `
    --target $localEnv `
    --snippet $mainEnv `
    --marker "CROSS_SERVER_MOBILE_DEMO"

Write-Host "已 merge WSL 跨服設定到 .env.local" -ForegroundColor Green
Write-Host "主站 → WSL 沙盒: http://host.docker.internal:8765 (REMOTE_SYNC=1)" -ForegroundColor Cyan

& $docker compose --env-file env/.env.local -f docker-compose.yml up -d --force-recreate alioth-postgres alioth-redis alioth-api alioth-worker

Write-Host ""
Write-Host "Demo: http://127.0.0.1:8000/api/v1/mobile-demo/demo" -ForegroundColor Yellow
Write-Host "驗證: powershell -File scripts/verify-cross-server-wsl.ps1" -ForegroundColor Yellow
