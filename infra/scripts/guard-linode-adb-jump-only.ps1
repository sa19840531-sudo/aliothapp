# Alioth：负责人本机 ADB 守护——只保留跳板三埠（禁止公网 2xxxx 双线抢连）。
# Usage:
#   powershell -ExecutionPolicy Bypass -File infra/scripts/guard-linode-adb-jump-only.ps1
# 与 start-linode-adb-tunnel.ps1 同时开着；本窗口保持开启。Ctrl+C 停止。
#
# 现象：同一 PC 同时 adb connect 多条公网 2xxxx 与跳板 3xxxx → 双线抢设备、5037 打架。
# 本脚本：周期扫描 adb devices；断开非跳板 TCP；可选补连三台跳板。

param(
  [string]$JumpHost = "172.104.172.119",
  # QA-A 30615、QA-B 30660、QA-C 30907。30033、30613 已到期。
  [string]$JumpPorts = "30615,30660,30907",
  # 全隧道模式下不保留任何公网直连
  [string]$AllowPublicEndpoints = "",
  # 已知应踢掉的公网直连（含 qa-b）
  [string]$KickPublicEndpoints = "51.89.164.208:20615,51.89.164.208:20613,51.195.137.27:20033,51.195.137.27:20660,198.244.190.180:20907,51.195.137.27:20233,198.244.190.180:20927,51.195.137.27:20224",
  [int]$IntervalSeconds = 8,
  [switch]$EnsureJumpConnect = $true,
  [switch]$EnsureAllowPublic = $false,
  # 空=自动挑选；可显式指定。勿与 ONIRENT GUI 各用各的 adb server 互抢 5037。
  [string]$AdbPath = "",
  # 若本机 ONIRENT 占用 5037，可改独立端口专管跳板（需 Alioth 侧也改同一端口才一致）
  [int]$AdbServerPort = 0
)

$ErrorActionPreference = "Continue"

function Resolve-AdbExe {
  param([string]$Preferred)
  if ($Preferred) {
    if (Test-Path $Preferred) { return (Resolve-Path $Preferred).Path }
    $cmdPreferred = Get-Command $Preferred -ErrorAction SilentlyContinue
    if ($cmdPreferred) { return $cmdPreferred.Source }
  }
  # 优先 Android SDK / platform-tools，避免默默用到 ONIRENT\adb.exe 与 GUI 抢线
  $candidates = @(
    "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
    "$env:ANDROID_HOME\platform-tools\adb.exe",
    "$env:ANDROID_SDK_ROOT\platform-tools\adb.exe",
    "C:\platform-tools\adb.exe",
    "C:\Android\platform-tools\adb.exe"
  )
  foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
  }
  $cmd = Get-Command adb -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return "adb"
}

function Invoke-Adb {
  param(
    [string]$Adb,
    [int]$ServerPort,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
  )
  if ($ServerPort -gt 0) {
    $env:ANDROID_ADB_SERVER_PORT = "$ServerPort"
  }
  & $Adb @Args 2>&1
}

function Get-AdbDeviceSerials {
  param([string]$Adb, [int]$ServerPort)
  $raw = (Invoke-Adb -Adb $Adb -ServerPort $ServerPort devices | Out-String)
  $serials = @()
  foreach ($line in ($raw -split "`r?`n")) {
    $t = $line.Trim()
    if (-not $t) { continue }
    if ($t -match '^List of devices') { continue }
    if ($t -match '^(\S+)\s+(device|offline|unauthorized)') {
      $serials += $Matches[1]
    }
  }
  return $serials
}

function Test-IsKeepEndpoint {
  param(
    [string]$Serial,
    [string]$JumpHost,
    [string[]]$AllowedJumpEndpoints,
    [System.Collections.Generic.HashSet[string]]$AllowPublic
  )
  if ($AllowPublic.Contains($Serial)) { return $true }
  if ($AllowedJumpEndpoints -contains $Serial) { return $true }
  if ($Serial -like ($JumpHost + ":*")) {
    $port = ($Serial.Split(":")[-1])
    foreach ($ep in $AllowedJumpEndpoints) {
      if ($ep.EndsWith(":" + $port)) { return $true }
    }
  }
  return $false
}

$adb = Resolve-AdbExe -Preferred $AdbPath
$jumpPortList = @()
foreach ($p in $JumpPorts.Split(",")) {
  $n = $p.Trim()
  if ($n) { $jumpPortList += $n }
}
$allowedJump = @()
foreach ($p in $jumpPortList) {
  $allowedJump += ("{0}:{1}" -f $JumpHost, $p)
}
$allowPublic = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($ep in $AllowPublicEndpoints.Split(",")) {
  $t = $ep.Trim()
  if ($t) { [void]$allowPublic.Add($t) }
}
$kickPublic = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($ep in $KickPublicEndpoints.Split(",")) {
  $t = $ep.Trim()
  if ($t -and -not $allowPublic.Contains($t)) { [void]$kickPublic.Add($t) }
}

Write-Host "ADB keep=jump + allow-public guard"
Write-Host ("  adb={0}" -f $adb)
if ($adb -match '(?i)ONIRENT') {
  Write-Host "  WARN: using ONIRENT adb.exe - close ONIRENT GUI, prefer Android platform-tools, run as Admin if needed."
}
Write-Host ("  keep-jump={0}" -f ($allowedJump -join ", "))
Write-Host ("  keep-public={0}" -f (($allowPublic | Sort-Object) -join ", "))
Write-Host ("  kick-known-public={0}" -f (($kickPublic | Sort-Object) -join ", "))
Write-Host ("  interval={0}s ensureJump={1} ensureAllowPublic={2} serverPort={3}" -f $IntervalSeconds, [bool]$EnsureJumpConnect, [bool]$EnsureAllowPublic, $AdbServerPort)
Write-Host "Keep this window open. Ctrl+C to stop."
Write-Host "NOTE: six jump ports only; kick ALL public 2xxxx on this host (incl. qa-b 20660)."

while ($true) {
  $stamp = Get-Date -Format "HH:mm:ss"
  try {
    $serials = @(Get-AdbDeviceSerials -Adb $adb -ServerPort $AdbServerPort)
  } catch {
    Write-Host ("[{0}] adb devices failed: {1}" -f $stamp, $_.Exception.Message)
    Start-Sleep -Seconds $IntervalSeconds
    continue
  }

  $kicked = @()
  foreach ($serial in $serials) {
    if (Test-IsKeepEndpoint -Serial $serial -JumpHost $JumpHost -AllowedJumpEndpoints $allowedJump -AllowPublic $allowPublic) {
      continue
    }
    # 跳过本地 USB（无冒号）以免误伤实体机调试；只踢 TCP 端点
    if ($serial -notmatch ':') {
      continue
    }
    Invoke-Adb -Adb $adb -ServerPort $AdbServerPort disconnect $serial | Out-Null
    $kicked += $serial
  }

  foreach ($pub in $kickPublic) {
    if ($serials -contains $pub) { continue }
    Invoke-Adb -Adb $adb -ServerPort $AdbServerPort disconnect $pub | Out-Null
  }

  if ($EnsureJumpConnect) {
    $after = @(Get-AdbDeviceSerials -Adb $adb -ServerPort $AdbServerPort)
    foreach ($ep in $allowedJump) {
      if ($after -contains $ep) { continue }
      $conn = (Invoke-Adb -Adb $adb -ServerPort $AdbServerPort connect $ep | Out-String).Trim()
      if ($conn) {
        Write-Host ("[{0}] ensure jump {1}: {2}" -f $stamp, $ep, $conn)
      }
    }
  }

  if ($EnsureAllowPublic) {
    $afterPub = @(Get-AdbDeviceSerials -Adb $adb -ServerPort $AdbServerPort)
    foreach ($ep in ($allowPublic | Sort-Object)) {
      if ($afterPub -contains $ep) { continue }
      $conn = (Invoke-Adb -Adb $adb -ServerPort $AdbServerPort connect $ep | Out-String).Trim()
      if ($conn) {
        Write-Host ("[{0}] ensure public {1}: {2}" -f $stamp, $ep, $conn)
      }
    }
  }

  if ($kicked.Count -gt 0) {
    Write-Host ("[{0}] kicked non-keep: {1}" -f $stamp, ($kicked -join ", "))
  } else {
    $keep = @(Get-AdbDeviceSerials -Adb $adb -ServerPort $AdbServerPort) | Where-Object {
      Test-IsKeepEndpoint -Serial $_ -JumpHost $JumpHost -AllowedJumpEndpoints $allowedJump -AllowPublic $allowPublic
    }
    Write-Host ("[{0}] ok keep count={1} [{2}]" -f $stamp, @($keep).Count, ($keep -join ", "))
  }

  Start-Sleep -Seconds $IntervalSeconds
}
