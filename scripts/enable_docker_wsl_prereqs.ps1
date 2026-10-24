# 啟用 Docker Desktop（WSL2）所需 Windows 功能
# 必須以系統管理員執行，完成後需重開機

$ErrorActionPreference = "Stop"

function Require-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "需要系統管理員權限，正在提權..." -ForegroundColor Yellow
        Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        exit 0
    }
}

Require-Admin

Write-Host "=== 啟用 Virtual Machine Platform + WSL ===" -ForegroundColor Cyan

dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart

Write-Host "=== 更新 WSL ===" -ForegroundColor Cyan
wsl --update
wsl --set-default-version 2

Write-Host ""
Write-Host "完成。請重開機後執行：" -ForegroundColor Green
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/restart_docker_desktop.ps1"
Write-Host ""
Read-Host "按 Enter 結束（建議現在重開機）"
