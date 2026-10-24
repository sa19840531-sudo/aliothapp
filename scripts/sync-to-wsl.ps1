# 將專案同步到 WSL Ubuntu 家目錄
# 用法：.\scripts\sync-to-wsl.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$distros = @(wsl --list --quiet 2>$null)
$target = $distros | Where-Object { $_ -match "Ubuntu" } | Select-Object -First 1

if (-not $target) {
    Write-Host "找不到 Ubuntu 發行版。請從開始選單開啟「Ubuntu」完成初次設定後重試。" -ForegroundColor Yellow
    exit 1
}

$target = $target.Trim()
Write-Host "=== 同步到 WSL: $target ~/alioth ===" -ForegroundColor Cyan

wsl -d $target -e bash -c "mkdir -p ~/alioth"

Push-Location $Root
try {
    tar -cf - `
        --exclude=.venv `
        --exclude=__pycache__ `
        --exclude=node_modules `
        --exclude=.git `
        . | wsl -d $target -e bash -c "cd ~/alioth && tar -xf -"
    Write-Host "同步完成！" -ForegroundColor Green
    Write-Host ""
    Write-Host "在 Ubuntu 執行："
    Write-Host "  cd ~/alioth && bash scripts/setup-linux.sh"
} finally {
    Pop-Location
}
