# 重新開啟 Windows Hypervisor（還原 Docker Desktop / WSL2）
#
# 用法（管理員 PowerShell）：
#   powershell -File scripts/enable-windows-hypervisor.ps1
#   shutdown /r /t 60

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

bcdedit /set '{current}' hypervisorlaunchtype Auto
if ($LASTEXITCODE -ne 0) { throw "bcdedit failed" }

Write-Host "hypervisorlaunchtype=Auto" -ForegroundColor Green
Write-Host "Reboot required. After reboot: start Docker Desktop; VMware nested Android may be slow again."
