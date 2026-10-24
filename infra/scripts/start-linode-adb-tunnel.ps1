# Alioth：负责人本机把 ONIRENT 公网 ADB 反向转到跳板，供「代码直连跳板埠」。
# Usage:
#   powershell -ExecutionPolicy Bypass -File infra/scripts/start-linode-adb-tunnel.ps1
# Keep the window open. Ctrl+C to stop. Auto-reconnects on drop.
#
# 跳板对外（需 GatewayPorts clientspecified）；租约内三台：
#   qa-a  51.195.137.27:20233    -> 172.104.172.119:30615
#   qa-b  198.244.190.180:20927  -> 172.104.172.119:30660
#   qa-c  51.195.137.27:20224    -> 172.104.172.119:30907
# qa-d 30033、qa-e 30613 租约到期，不再转发。
#
# Alioth：在租约的云手机连跳板 IP:3xxxx；本机禁止再 adb connect 公网 2xxxx。
# SSH -R 使用 0.0.0.0 绑定；勿在跳板上 adb kill-server 抢 ONRENT。

param(
  [string]$LinodeHost = "172.104.172.119",
  [string]$User = "root",
  [string]$IdentityFile = "",
  # 远端绑定；对外可连用 0.0.0.0
  [string]$BindHost = "0.0.0.0",
  # 在租约的三台（手机端点=跳板监听埠）
  [string]$TunnelMap = "51.195.137.27:20233=30615,198.244.190.180:20927=30660,51.195.137.27:20224=30907",
  [int]$ReconnectSeconds = 5,
  # 默认另开窗口跑守护：只保留跳板三埠
  [bool]$StartJumpGuard = $true
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$RepoIdentity = Join-Path $RepoRoot "infra\ssh\alioth-linode-tunnel"
$UserIdentity = Join-Path $env:USERPROFILE ".ssh\id_ed25519_alioth_linode"

if (-not $IdentityFile) {
  if (Test-Path $RepoIdentity) {
    $IdentityFile = $RepoIdentity
  } elseif (Test-Path $UserIdentity) {
    $IdentityFile = $UserIdentity
  } else {
    $IdentityFile = $RepoIdentity
  }
}

if (-not (Test-Path $IdentityFile)) {
  Write-Error @"
Missing private key: $IdentityFile
Run: powershell -ExecutionPolicy Bypass -File infra/scripts/install-linode-ssh-key.ps1
"@
}

function ConvertTo-SshReverseForward {
  param([Parameter(Mandatory = $true)][string]$Pair)
  $parts = $Pair.Split("=", 2)
  if ($parts.Count -ne 2) {
    throw "Invalid TunnelMap pair: $Pair"
  }
  $public = $parts[0].Trim()
  $listen = $parts[1].Trim()
  $pubParts = $public.Split(":")
  if ($pubParts.Count -ne 2) {
    throw "Invalid public endpoint: $public"
  }
  $pubHost = $pubParts[0]
  $pubPort = $pubParts[1]
  if ($listen -match '^\d+$') {
    $locHost = $BindHost
    $locPort = $listen
  } else {
    $locParts = $listen.Split(":")
    if ($locParts.Count -ne 2) {
      throw "Invalid listen endpoint: $listen"
    }
    $locHost = $locParts[0]
    $locPort = $locParts[1]
    if ($locHost -in @("127.0.0.1", "localhost")) {
      $locHost = $BindHost
    }
  }
  return "{0}:{1}:{2}:{3}" -f $locHost, $locPort, $pubHost, $pubPort
}

$forwards = @()
foreach ($chunk in $TunnelMap.Split(",")) {
  $item = $chunk.Trim()
  if (-not $item) { continue }
  $forwards += (ConvertTo-SshReverseForward -Pair $item)
}

if ($forwards.Count -lt 1) {
  Write-Error "TunnelMap produced no forwards"
}

$target = "{0}@{1}" -f $User, $LinodeHost
Write-Host ("ADB reverse tunnel -> jump public ports ({0})" -f $forwards.Count)
foreach ($chunk in $TunnelMap.Split(",")) {
  $item = $chunk.Trim()
  if (-not $item) { continue }
  $bits = $item.Split("=", 2)
  $listen = $bits[1].Trim()
  if ($listen -match '^\d+$') {
    $client = "{0}:{1}" -f $LinodeHost, $listen
  } elseif ($listen.StartsWith("0.0.0.0:") -or $listen.StartsWith("127.0.0.1:")) {
    $client = "{0}:{1}" -f $LinodeHost, ($listen.Split(":")[-1])
  } else {
    $client = $listen
  }
  Write-Host ("  {0} -> Alioth 连 {1}  (ssh -R {2})" -f $bits[0].Trim(), $client, (ConvertTo-SshReverseForward -Pair $item))
}
Write-Host ("  target {0} (keep this window open; Alioth 不跑在跳板上)" -f $target)

if ($StartJumpGuard) {
  $guardScript = Join-Path $PSScriptRoot "guard-linode-adb-jump-only.ps1"
  if (Test-Path $guardScript) {
    Write-Host ("Starting jump-only ADB guard: {0}" -f $guardScript)
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
      "-NoProfile",
      "-ExecutionPolicy", "Bypass",
      "-File", $guardScript,
      "-JumpHost", $LinodeHost
    ) | Out-Null
  } else {
    Write-Warning ("Jump guard script missing: {0}" -f $guardScript)
  }
}

while ($true) {
  Write-Host ("[{0}] connecting..." -f (Get-Date -Format "HH:mm:ss"))
  $sshArgs = @(
    "-i", $IdentityFile,
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-N"
  )
  foreach ($fwd in $forwards) {
    $sshArgs += @("-R", $fwd)
  }
  $sshArgs += $target
  & ssh.exe @sshArgs
  $code = $LASTEXITCODE
  Write-Host ("[{0}] ssh exited code={1}; retry in {2}s" -f (Get-Date -Format "HH:mm:ss"), $code, $ReconnectSeconds)
  Start-Sleep -Seconds $ReconnectSeconds
}
