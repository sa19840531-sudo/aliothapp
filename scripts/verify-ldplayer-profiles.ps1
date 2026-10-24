# 對照 ALIOTH_LDPLAYER_PROFILES（或 FILE）與本機 ldconsole list2 的 index。
# 用法（專案根目錄）：
#   powershell -File scripts/verify-ldplayer-profiles.ps1
# 可選：
#   $env:ALIOTH_LDPLAYER_PROFILES_FILE = "F:\alioth\infra\env\ldplayer-profiles.example.json"

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[verify-ldplayer-profiles] repo=$root"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  throw "找不到 python，請先裝好 Python 並加入 PATH"
}

& python (Join-Path $PSScriptRoot "_verify_ldplayer_profiles.py")
exit $LASTEXITCODE
