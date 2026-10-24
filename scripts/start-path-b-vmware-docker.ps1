# Path B：關 Hyper-V 後，在 VMware Ubuntu 內跑 Docker Engine + ReDroid + 主站
#
# 前置（管理員，且尚未重開機）：
#   powershell -File scripts/disable-windows-hypervisor.ps1
#   shutdown /r /t 0
#
# 重開機後（一般權限即可）：
#   powershell -File scripts/start-path-b-vmware-docker.ps1
#
# 環境變數（可選）：
#   ALIOTH_VMX_PATH   VMware .vmx 完整路徑
#   ALIOTH_VM_HOST    預設 192.168.33.128
#   ALIOTH_VM_USER    預設 eevee
#   ALIOTH_VM_PASSWORD

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Get-Vmrun {
    $cands = @(
        "C:\Program Files\VMware\VMware Workstation\vmrun.exe",
        "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    throw "vmrun.exe not found"
}

function Get-VmxPath {
    if ($env:ALIOTH_VMX_PATH -and (Test-Path $env:ALIOTH_VMX_PATH)) {
        return $env:ALIOTH_VMX_PATH
    }
    $hit = Get-ChildItem "F:\UB\*.vmx" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    throw "Set ALIOTH_VMX_PATH to your Ubuntu .vmx"
}

# --- 0) Hypervisor check ---
$cs = Get-CimInstance Win32_ComputerSystem
if ($cs.HypervisorPresent) {
    Write-Host "WARN: HypervisorPresent=True. Path B needs Hyper-V OFF." -ForegroundColor Yellow
    Write-Host "  Run as Admin: powershell -File scripts/disable-windows-hypervisor.ps1"
    Write-Host "  Then reboot, then re-run this script."
    throw "Windows Hypervisor still present"
}
Write-Host "[ok] HypervisorPresent=False (VMware can use native VT-x)" -ForegroundColor Green

$vmrun = Get-Vmrun
$vmx = Get-VmxPath
$hostIp = if ($env:ALIOTH_VM_HOST) { $env:ALIOTH_VM_HOST } else { "192.168.33.128" }
Write-Host "vmx=$vmx"
Write-Host "guest=$hostIp"

# --- 1) Recommend / patch CPU if powered off ---
$running = & $vmrun list
$isUp = ($running -join "`n") -like "*$([IO.Path]::GetFileName($vmx))*" -or ($running -join "`n") -like "*Ubuntu*"
if (-not $isUp -and (Test-Path $vmx)) {
    $raw = Get-Content -LiteralPath $vmx -Raw -Encoding UTF8
    if ($raw -match 'numvcpus\s*=\s*"(\d+)"') {
        $n = [int]$Matches[1]
        if ($n -lt 6) {
            Write-Host "Patching numvcpus $n -> 6 (ReDroid needs CPU headroom)" -ForegroundColor Cyan
            $raw2 = $raw -replace 'numvcpus\s*=\s*"\d+"', 'numvcpus = "6"'
            if ($raw2 -notmatch 'cpuid.coresPerSocket') {
                $raw2 = $raw2.TrimEnd() + "`ncpuid.coresPerSocket = `"2`"`n"
            }
            if ($raw2 -notmatch 'vhv\.enable') {
                $raw2 = $raw2.TrimEnd() + "`nvhv.enable = `"TRUE`"`n"
            } else {
                $raw2 = $raw2 -replace 'vhv\.enable\s*=\s*"[^"]*"', 'vhv.enable = "TRUE"'
            }
            Set-Content -LiteralPath $vmx -Value $raw2 -Encoding UTF8 -NoNewline
        }
    }
}

# --- 2) Start VM ---
Write-Host "=== start VMware guest ===" -ForegroundColor Cyan
& $vmrun start $vmx nogui
if ($LASTEXITCODE -ne 0) {
    Write-Host "vmrun start returned $LASTEXITCODE (may already be running)" -ForegroundColor Yellow
}

# --- 3) Wait SSH ---
Write-Host "=== wait SSH $hostIp:22 ===" -ForegroundColor Cyan
$ready = $false
for ($i = 1; $i -le 90; $i++) {
    try {
        $tcp = New-Object Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect($hostIp, 22, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(2000, $false)
        if ($ok -and $tcp.Connected) { $tcp.Close(); $ready = $true; break }
        $tcp.Close()
    } catch {}
    Write-Host "wait_ssh_$i"
    Start-Sleep 3
}
if (-not $ready) { throw "SSH not reachable at ${hostIp}:22 — check VMware network / guest IP" }

# --- 4) Deploy code ---
Write-Host "=== deploy repo to guest ~/alioth ===" -ForegroundColor Cyan
$env:ALIOTH_VM_HOST = $hostIp
python (Join-Path $Root "scripts\deploy-alioth-to-vmware.py")
if ($LASTEXITCODE -ne 0) { throw "deploy failed" }

# --- 5) Bootstrap + start Docker stack inside guest ---
Write-Host "=== guest: prepare binderfs + start docker emulator stack ===" -ForegroundColor Cyan
$boot = @'
set -euo pipefail
cd ~/alioth
bash scripts/linux-bootstrap-path-b.sh
'@
$bootPath = Join-Path $Root ".tmp_path_b_boot.sh"
[IO.File]::WriteAllText($bootPath, ($boot -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))

python -c @"
import os, sys, paramiko
host=os.environ.get('ALIOTH_VM_HOST','192.168.33.128')
user=os.environ.get('ALIOTH_VM_USER','eevee')
pw=os.environ.get('ALIOTH_VM_PASSWORD','1215627')
c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=pw, timeout=30, allow_agent=False, look_for_keys=False)
sftp=c.open_sftp()
sftp.put(r'$($bootPath.Replace('\','\\'))', '/tmp/path_b_boot.sh')
sftp.close()
stdin,stdout,stderr=c.exec_command('bash /tmp/path_b_boot.sh', get_pty=True, timeout=1800)
for line in iter(stdout.readline, ''):
    sys.stdout.write(line); sys.stdout.flush()
rc=stdout.channel.recv_exit_status()
err=stderr.read().decode('utf-8','replace')
if err: sys.stderr.write(err)
c.close(); sys.exit(rc)
"@
if ($LASTEXITCODE -ne 0) { throw "guest bootstrap failed" }

Write-Host ""
Write-Host "=== Path B ready ===" -ForegroundColor Green
Write-Host "Demo:  http://${hostIp}:8000/api/v1/mobile-demo/demo"
Write-Host "Health: http://${hostIp}:8765/health"
Write-Host "ADB:    adb connect ${hostIp}:5555"
Write-Host ""
Write-Host "Note: Windows Docker Desktop will NOT work while Hypervisor is Off."
Write-Host "      Use the VM URLs above from the browser."
