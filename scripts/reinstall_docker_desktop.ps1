# Reinstall Docker Desktop + enable WSL2 prerequisites (Admin required)

$ErrorActionPreference = "Continue"
$Log = Join-Path $env:TEMP "alioth-reinstall-docker.log"

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Require-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Log "Elevating to Administrator..."
        $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
        exit 0
    }
}

Require-Admin
if (Test-Path $Log) { Remove-Item $Log -Force -ErrorAction SilentlyContinue }
Log "log=$Log"

Log "=== 1) Stop Docker processes ==="
foreach ($n in @("Docker Desktop", "com.docker.backend", "com.docker.service", "vpnkit", "docker")) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
        Log "stop pid=$($_.Id) name=$($_.Name)"
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
}
try { Stop-Service com.docker.service -Force -ErrorAction SilentlyContinue } catch {}
Start-Sleep 3
try { wsl --shutdown 2>$null } catch {}

Log "=== 2) Enable VirtualMachinePlatform + WSL ==="
$d1 = dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart 2>&1 | Out-String
Log $d1.Trim()
$d2 = dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart 2>&1 | Out-String
Log $d2.Trim()
try {
    $w1 = wsl --update 2>&1 | Out-String
    Log $w1.Trim()
    $w2 = wsl --set-default-version 2 2>&1 | Out-String
    Log $w2.Trim()
} catch {
    Log "wsl update warn: $_"
}

Log "=== 3) Uninstall Docker Desktop ==="
$u = winget uninstall --id Docker.DockerDesktop --exact --silent --accept-source-agreements 2>&1 | Out-String
Log $u.Trim()
Start-Sleep 5

$uninstCandidates = @(
    "C:\Program Files\Docker\Docker\Docker Desktop Installer.exe",
    "${env:ProgramFiles}\Docker\Docker\Docker Desktop Installer.exe"
)
$uninst = $uninstCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($uninst) {
    Log "quiet uninstall via installer: $uninst"
    Start-Process -FilePath $uninst -ArgumentList "uninstall","--quiet" -Wait -NoNewWindow
}

Log "=== 4) Install Docker Desktop (latest) ==="
$i = winget install --id Docker.DockerDesktop --exact --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-String
Log $i.Trim()

$exe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $exe)) {
    Log "ERROR: missing $exe after install"
    exit 1
}

Log "=== 5) Start Docker Desktop ==="
Start-Process -FilePath $exe
Start-Sleep 20

$deadline = (Get-Date).AddMinutes(5)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        docker info 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $info = docker info 2>&1 | Out-String
            if ($info -match "Server Version") {
                Log "ENGINE READY"
                $ready = $true
                break
            }
        }
    } catch {}
    Log "waiting engine..."
    Start-Sleep 10
}

if (-not $ready) {
    Log "Engine not ready yet. If VirtualMachinePlatform was just enabled, REBOOT Windows first."
    Log "After reboot: powershell -ExecutionPolicy Bypass -File F:\aliothG\scripts\restart_docker_desktop.ps1"
    exit 2
}

Log "DONE. docker version:"
$v = docker version 2>&1 | Out-String
Log $v.Trim()
exit 0
