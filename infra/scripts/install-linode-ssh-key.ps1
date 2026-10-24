# 同步 Linode 跳板 SSH 私钥：仓库 infra/ssh <-> %USERPROFILE%\.ssh
# 用法：
#   powershell -ExecutionPolicy Bypass -File infra/scripts/install-linode-ssh-key.ps1
#
# 私钥不入库；本脚本只在本机拷贝。公钥见 infra/ssh/alioth-linode-tunnel.pub

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$RepoPriv = Join-Path $RepoRoot "infra\ssh\alioth-linode-tunnel"
$RepoPub = Join-Path $RepoRoot "infra\ssh\alioth-linode-tunnel.pub"
$UserPriv = Join-Path $env:USERPROFILE ".ssh\id_ed25519_alioth_linode"
$UserPub = Join-Path $env:USERPROFILE ".ssh\id_ed25519_alioth_linode.pub"

function Protect-PrivateKeyFile {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path $Path)) { return }
  # OpenSSH on Windows rejects overly open ACLs
  icacls $Path /inheritance:r | Out-Null
  icacls $Path /grant:r (${env:USERNAME} + ":(R)") | Out-Null
}

New-Item -ItemType Directory -Force -Path (Split-Path $UserPriv -Parent) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $RepoPriv -Parent) | Out-Null

$repoHas = Test-Path $RepoPriv
$userHas = Test-Path $UserPriv

if ($repoHas -and -not $userHas) {
  Copy-Item -Force $RepoPriv $UserPriv
  if (Test-Path $RepoPub) { Copy-Item -Force $RepoPub $UserPub }
  Protect-PrivateKeyFile -Path $UserPriv
  Write-Host "Installed repo private key -> $UserPriv"
}
elseif ($userHas -and -not $repoHas) {
  Copy-Item -Force $UserPriv $RepoPriv
  if (Test-Path $UserPub) { Copy-Item -Force $UserPub $RepoPub }
  Protect-PrivateKeyFile -Path $RepoPriv
  Write-Host "Copied user private key -> $RepoPriv (gitignored)"
}
elseif ($repoHas -and $userHas) {
  Protect-PrivateKeyFile -Path $RepoPriv
  Protect-PrivateKeyFile -Path $UserPriv
  if (Test-Path $RepoPub) { Copy-Item -Force $RepoPub $UserPub }
  Write-Host "Both locations already have private key; ACL refreshed."
  Write-Host "  repo: $RepoPriv"
  Write-Host "  user: $UserPriv"
}
else {
  Write-Error "No private key found at $RepoPriv or $UserPriv"
}

if (Test-Path $RepoPub) {
  Write-Host "Public key:"
  Get-Content $RepoPub -Raw
  ssh-keygen -lf $RepoPub
}

Write-Host "Test: ssh -i `"$UserPriv`" -o IdentitiesOnly=yes root@172.104.172.119"
