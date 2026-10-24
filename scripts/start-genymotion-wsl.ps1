# 啟動 WSL2 Ubuntu 內的 Genymotion Desktop（WSLg + NVIDIA D3D12）
# Usage: powershell -File scripts/start-genymotion-wsl.ps1

$ErrorActionPreference = "Stop"
$Distro = if ($env:ALIOTH_GENYMOTION_WSL_DISTRO) { $env:ALIOTH_GENYMOTION_WSL_DISTRO } else { "Ubuntu-24.04" }

Write-Host "Launching Genymotion in WSL ($Distro) with Mesa d3d12/NVIDIA..." -ForegroundColor Cyan
Write-Host "請登入帳號（若尚未登入），並建立/啟動虛擬裝置（Android 11 + ARM 轉譯需另刷 houdini）。" -ForegroundColor Yellow

wsl -d $Distro -e bash /mnt/f/alioth/scripts/wsl-start-genymotion.sh
