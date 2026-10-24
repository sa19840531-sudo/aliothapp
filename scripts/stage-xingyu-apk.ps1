# 把 APK 放进星语 Official 构建缓存（对齐青云 app-build:<uuid>）
# Usage:
#   powershell -File scripts/stage-xingyu-apk.ps1 -ApkPath F:\path\app.apk -BuildId 62681f10-c761-4ade-8834-16879baae6f8
#   powershell -File scripts/stage-xingyu-apk.ps1 -ApkPath .\demo.apk -BuildRef "app-build:...."

param(
    [Parameter(Mandatory = $true)][string]$ApkPath,
    [string]$BuildId = "",
    [string]$BuildRef = "",
    [string]$CacheDir = "",
    [string]$Repo = "F:\aliothapp"
)

$ErrorActionPreference = "Stop"
Set-Location $Repo

if (-not (Test-Path -LiteralPath $ApkPath)) {
    throw "APK 不存在：$ApkPath"
}

if (-not $BuildId) {
    if ($BuildRef -match '^app-build:([0-9a-fA-F-]{36})$') {
        $BuildId = $Matches[1].ToLower()
    } else {
        throw "请提供 -BuildId <uuid> 或 -BuildRef app-build:<uuid>"
    }
}
$BuildId = $BuildId.ToLower()

if (-not $CacheDir) {
    if ($env:ALIOTH_XINGYU_BUILD_CACHE) {
        $CacheDir = $env:ALIOTH_XINGYU_BUILD_CACHE
    } else {
        $CacheDir = Join-Path $Repo ".alioth-xingyu-builds"
    }
}
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

$dest = Join-Path $CacheDir "$BuildId.apk"
Copy-Item -LiteralPath $ApkPath -Destination $dest -Force

$hash = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLower()
Write-Host "staged=$dest"
Write-Host "platform_build_ref=app-build:$BuildId"
Write-Host "sha256=$hash"
Write-Host "Set ALIOTH_XINGYU_BUILD_CACHE=$CacheDir before starting Official API"
