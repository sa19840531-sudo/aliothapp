# Integrated: Docker sandbox + cross-server remote sync (Windows + WSL)
#
# Architecture:
#   Main  : Windows Docker alioth-api  -> REMOTE_SYNC -> host.docker.internal:8765
#   Sandbox: WSL Docker Engine
#            - alioth-mobile-host (--network host)
#            - Google Emulator (WSL KVM; ReDroid needs binderfs, WSL lacks it)
#
# Usage:
#   powershell -File scripts/start-docker-cross-server-integrated.ps1
#   powershell -File scripts/verify-cross-server-wsl.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Distro = if ($env:ALIOTH_WSL_DISTRO) { $env:ALIOTH_WSL_DISTRO } else { "Ubuntu-24.04" }

Write-Host "=== 1) WSL Docker sandbox (Mobile Host + Emulator) ===" -ForegroundColor Cyan
wsl -d $Distro -- bash /mnt/f/aliothG/scripts/wsl-start-docker-sandbox-integrated.sh
if ($LASTEXITCODE -ne 0) { throw "WSL Docker sandbox failed" }

Write-Host "=== 2) Main Windows Docker -> cross-server WSL ===" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\start-main-docker-cross-server-wsl.ps1")
if ($LASTEXITCODE -ne 0) { throw "Main cross-server switch failed" }

Write-Host "=== 3) Verify ===" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\verify-cross-server-wsl.ps1")

Write-Host ""
Write-Host "Demo: http://127.0.0.1:8000/api/v1/mobile-demo/demo" -ForegroundColor Yellow
Write-Host "Note: WSL has no binderfs; device side uses Google Emulator(KVM). Mobile Host is Docker." -ForegroundColor DarkYellow
