# Local x86 host: boot Ubuntu 24.04 ARM64 via QEMU TCG (no ARM KVM).
# SSH: ssh -p 2222 alioth@127.0.0.1  password: alioth
param(
  [int]$Cpus = 4,
  [int]$MemoryMb = 6144,
  [int]$SshHostPort = 2222,
  [int]$AdbHostPort = 5555,
  [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$VmDir = Join-Path (Split-Path $PSScriptRoot -Parent) ".alioth-mobile-demo\arm64-vm"
$QemuDir = "C:\Program Files\qemu"
$Qemu = Join-Path $QemuDir "qemu-system-aarch64.exe"
$Disk = Join-Path $VmDir "disk.qcow2"
$Seed = Join-Path $VmDir "seed.img"
$CodeFd = Join-Path $VmDir "edk2-aarch64-code.fd"
$VarsFd = Join-Path $VmDir "vars.fd"
$Log = Join-Path $VmDir "qemu.log"
$ErrLog = Join-Path $VmDir "qemu.err"

if (-not (Test-Path $Qemu)) { throw "QEMU not found: $Qemu" }
if (-not (Test-Path $Disk)) { throw "Disk not found: $Disk" }
if (-not (Test-Path $Seed)) { throw "Seed not found: $Seed" }
if (-not (Test-Path $CodeFd)) {
  Copy-Item (Join-Path $QemuDir "share\edk2-aarch64-code.fd") $CodeFd -Force
}
if (-not (Test-Path $VarsFd)) {
  Copy-Item (Join-Path $QemuDir "share\edk2-arm-vars.fd") $VarsFd -Force
}

$existing = Get-Process -Name "qemu-system-aarch64" -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "qemu-system-aarch64 already running (PID=$($existing.Id -join ',')). SSH: ssh -p $SshHostPort alioth@127.0.0.1"
  exit 0
}

# Firmware/disk paths must avoid unquoted spaces (QEMU splits on space).
$qemuArgs = @(
  "-M", "virt,gic-version=3",
  "-cpu", "max",
  "-smp", "$Cpus",
  "-m", "$MemoryMb",
  "-drive", "if=pflash,format=raw,readonly=on,file=$CodeFd",
  "-drive", "if=pflash,format=raw,file=$VarsFd",
  "-drive", "if=virtio,format=qcow2,file=$Disk",
  "-drive", "if=virtio,format=raw,file=$Seed",
  "-netdev", "user,id=net0,hostfwd=tcp::${SshHostPort}-:22,hostfwd=tcp::${AdbHostPort}-:5555",
  "-device", "virtio-net-device,netdev=net0",
  "-nographic",
  "-serial", "file:$Log"
)

Write-Host "Starting local ARM64 VM (QEMU TCG; first boot/cloud-init may take 10-20 min)..."
Write-Host "  disk: $Disk"
Write-Host "  SSH : ssh -p $SshHostPort alioth@127.0.0.1  (password: alioth)"
Write-Host "  log : $Log"
Write-Host "  NOTE: no /dev/kvm here; not a production Android Emulator host."

if ($Foreground) {
  & $Qemu @qemuArgs
} else {
  $p = Start-Process -FilePath $Qemu -ArgumentList $qemuArgs -WindowStyle Hidden -PassThru -RedirectStandardError $ErrLog
  Write-Host "QEMU PID=$($p.Id)"
  Write-Host "Wait SSH: powershell -File scripts\wait-local-arm64-vm.ps1"
}