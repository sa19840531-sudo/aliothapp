# Verify main Docker <-> WSL sandbox (health / version / sync / snapshot)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
if (-not (Test-Path $docker)) {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { $docker = $cmd.Source }
}
$Sandbox = if ($env:SANDBOX_URL) { $env:SANDBOX_URL } else { "http://127.0.0.1:8765" }
$Main = if ($env:MAIN_API_URL) { $env:MAIN_API_URL } else { "http://127.0.0.1:8000" }

Write-Host "=== verify cross-server WSL ===" -ForegroundColor Cyan
Write-Host "sandbox=$Sandbox"
Write-Host "main=$Main"

$h = (Invoke-WebRequest "$Sandbox/health" -UseBasicParsing -TimeoutSec 10).Content
Write-Host "[ok] sandbox health: $h"

$v = (Invoke-WebRequest "$Main/api/v1/mobile-demo/version?format=json" -UseBasicParsing -TimeoutSec 10).Content
Write-Host "[ok] main version: $v"
$vj = $v | ConvertFrom-Json
if (-not $vj.remote_sync) { throw "remote_sync should be true" }
Write-Host "[ok] remote_sync=$($vj.remote_sync) host_executor=$($vj.host_executor)"

if ($docker -and (Test-Path $docker)) {
    $fromApi = & $docker exec alioth-api python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:8765/health', timeout=10).read().decode())"
    Write-Host "[ok] api-container->sandbox: $fromApi"
}

$apk = Get-ChildItem (Join-Path $Root ".alioth-mobile-demo\uploads\*.apk") -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $apk) {
    Write-Host "[skip] sync smoke (no uploads/*.apk)"
    Write-Host "=== done (connectivity ok) ===" -ForegroundColor Green
    exit 0
}

$runId = "wslverify-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
$dataDir = Join-Path $Root ".alioth-mobile-demo"
New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "runs\$runId") | Out-Null
$staged = "${runId}_smoke.apk"
Copy-Item $apk.FullName (Join-Path $dataDir "uploads\$staged") -Force
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$runObj = @{
    run_id = $runId
    status = "queued"
    message = "wsl cross-server sync smoke"
    apk_filename = $staged
    apk_path = "/workspace/.alioth-mobile-demo/uploads/$staged"
    created_at = $now
    package_name = "smoke.test"
    screenshots = @()
    matrix = $false
    mode = "single"
} | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText(
    (Join-Path $dataDir "runs\$runId\run.json"),
    $runObj,
    (New-Object System.Text.UTF8Encoding $false)
)

& $docker exec `
    -e ALIOTH_MOBILE_DEMO_DATA_DIR=/workspace/.alioth-mobile-demo `
    -e "ALIOTH_VERIFY_RUN_ID=$runId" `
    -e ALIOTH_VERIFY_SANDBOX=http://host.docker.internal:8765 `
    alioth-api python /workspace/scripts/wsl_cross_sync_smoke.py

Write-Host "[ok] sync/snapshot smoke run_id=$runId" -ForegroundColor Green
Write-Host "=== done ===" -ForegroundColor Green
