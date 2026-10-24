# 在 WSL2 啟動跨服沙盒：Android Emulator (KVM) + Mobile Host :8765
# 用法：powershell -File scripts/start-cross-server-wsl-sandbox.ps1
#
# 架構：
#   主站 Docker alioth-api  --REMOTE_SYNC-->  host.docker.internal:8765
#                                              └─ WSL Mobile Host → adb → WSL Emulator

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Distro = if ($env:ALIOTH_WSL_DISTRO) { $env:ALIOTH_WSL_DISTRO } else { "Ubuntu-24.04" }
$HostPort = if ($env:ALIOTH_MOBILE_HOST_PORT) { $env:ALIOTH_MOBILE_HOST_PORT } else { "8765" }

Write-Host "=== WSL cross-server sandbox ===" -ForegroundColor Cyan
Write-Host "distro=$Distro host_port=$HostPort"

$probe = wsl -d $Distro -- bash -lc "echo OK; test -e /dev/kvm && echo KVM=yes || echo KVM=no; hostname -I"
if ($LASTEXITCODE -ne 0) {
    throw "WSL 無法啟動（常見：VirtualMachinePlatform 未生效，需重開機）。輸出：$probe"
}
Write-Host $probe

foreach ($conn in (Get-NetTCPConnection -LocalPort ([int]$HostPort) -State Listen -ErrorAction SilentlyContinue)) {
    if ($conn.OwningProcess) {
        Write-Host "Stopping old listener on :$HostPort (pid=$($conn.OwningProcess))" -ForegroundColor Yellow
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

wsl -d $Distro -- bash /mnt/f/aliothG/scripts/wsl-start-cross-server-sandbox.sh
if ($LASTEXITCODE -ne 0) {
    throw "WSL sandbox 啟動失敗。日誌：wsl -d $Distro -- tail -80 /tmp/alioth-mobile-host-wsl.log"
}

# Windows 側再確認（localhost forwarding）
try {
    $h = (Invoke-WebRequest "http://127.0.0.1:$HostPort/health" -UseBasicParsing -TimeoutSec 8).Content
    Write-Host "[ok] Windows->WSL health: $h" -ForegroundColor Green
} catch {
    Write-Host "[warn] Windows localhost:$HostPort 尚未可達：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "next: powershell -File scripts/start-main-docker-cross-server-wsl.ps1" -ForegroundColor Yellow
Write-Host "then: powershell -File scripts/verify-cross-server-wsl.ps1" -ForegroundColor Yellow
