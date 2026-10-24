# Wait until local QEMU ARM64 VM SSH is ready (Windows host -> 127.0.0.1 via paramiko)
param(
  [int]$SshHostPort = 2222,
  [int]$TimeoutSec = 1800,
  [string]$Password = "alioth"
)

$ErrorActionPreference = "Continue"
$probe = Join-Path (Split-Path $PSScriptRoot -Parent) ".alioth-mobile-demo\arm64-vm\probe_ssh.py"
$deadline = (Get-Date).AddSeconds($TimeoutSec)
Write-Host "Waiting for ssh://alioth@127.0.0.1:$SshHostPort (timeout ${TimeoutSec}s)..."

while ((Get-Date) -lt $deadline) {
  if (-not (Get-Process -Name "qemu-system-aarch64" -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: qemu-system-aarch64 not running; start scripts\start-local-arm64-vm.ps1 first"
    exit 1
  }
  $out = python $probe 2>&1 | Out-String
  if ($out -match "SSH_OK") {
    Write-Host $out
    exit 0
  }
  Write-Host ("{0:HH:mm:ss} not ready: {1}" -f (Get-Date), ($out.Trim() -replace "`r?`n", " | "))
  Start-Sleep -Seconds 20
}

Write-Host "TIMEOUT"
exit 1