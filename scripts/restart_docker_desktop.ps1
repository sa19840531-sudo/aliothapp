<#
重啟 Windows Docker Desktop，並等待 docker daemon 就緒。
#>

$ErrorActionPreference = "Stop"

$exe1 = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$exe2 = "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
$exe = $exe1
if (!(Test-Path $exe)) { $exe = $exe2 }
if (!(Test-Path $exe)) { throw "Docker Desktop exe not found (checked: $exe1, $exe2)" }

# 停止常見後端進程
$procs = @("com.docker.backend", "Docker Desktop", "vpnkit", "docker")
foreach ($n in $procs) {
  Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object { $_ | Stop-Process -Force -ErrorAction SilentlyContinue }
}

Write-Host "Starting: $exe"
Start-Process -FilePath $exe

# 先重置 WSL2（Docker Desktop 在不少情況下依賴它）
try { wsl --shutdown | Out-Null } catch { }

$deadline = (Get-Date).AddMinutes(10)
while ((Get-Date) -lt $deadline) {
  try {
    docker info > $null 2>&1
    if ($LASTEXITCODE -eq 0) {
      Write-Host "docker ready"
      exit 0
    }
  } catch {
    # ignore
  }
  Write-Host "waiting docker..."
  Start-Sleep -Seconds 5
}

throw "docker daemon not ready in time"