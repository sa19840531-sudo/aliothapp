# Verify free cloud phone ADB (+ optional APK white-screen check)
# Usage:
#   $env:ALIOTH_CLOUD_PHONE_ADB = "x.x.x.x:5555"
#   powershell -File scripts/verify-cloud-phone-adb.ps1
# Optional:
#   $env:ALIOTH_CLOUD_PHONE_APK = "F:\path\to\app.apk"
#   $env:ALIOTH_CLOUD_PHONE_PACKAGE = "me.mfanv.npymci"
#   $env:ALIOTH_CLOUD_PHONE_ACTIVITY = "me.mfanv.npymci/com.jycg.icg.MainActivity"

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Endpoint = if ($env:ALIOTH_CLOUD_PHONE_ADB) { $env:ALIOTH_CLOUD_PHONE_ADB.Trim() } else { "" }
if (-not $Endpoint) {
    Write-Host "ERROR: set ALIOTH_CLOUD_PHONE_ADB=host:port first" -ForegroundColor Red
    Write-Host "See docs/design/2026-07-26-cloud-phone-ai-poc.md"
    exit 2
}

function Resolve-Adb {
    $cands = @()
    if ($env:ANDROID_HOME) { $cands += (Join-Path $env:ANDROID_HOME "platform-tools\adb.exe") }
    if ($env:ANDROID_SDK_ROOT) { $cands += (Join-Path $env:ANDROID_SDK_ROOT "platform-tools\adb.exe") }
    $cands += @(
        "F:\Android\Sdk\platform-tools\adb.exe",
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
        "C:\Android\Sdk\platform-tools\adb.exe"
    )
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    throw "adb not found; install Android platform-tools or set ANDROID_HOME"
}

$adb = Resolve-Adb
Write-Host "=== verify cloud phone ADB ===" -ForegroundColor Cyan
Write-Host "adb=$adb"
Write-Host "endpoint=$Endpoint"

& $adb start-server | Out-Null
& $adb connect $Endpoint | Write-Host

$ready = $false
for ($i = 1; $i -le 12; $i++) {
    $devs = & $adb devices
    $line = ($devs | Where-Object { $_ -match [regex]::Escape($Endpoint) -and $_ -match "\tdevice$" } | Select-Object -First 1)
    if (-not $line) {
        $line = ($devs | Where-Object { $_ -match ":\d+\tdevice$" } | Select-Object -First 1)
    }
    if ($line) {
        $serial = ($line -split "\s+")[0]
        $boot = (& $adb -s $serial shell getprop sys.boot_completed 2>$null | Out-String).Trim()
        if ($boot -eq "1") { $ready = $true; $Endpoint = $serial; break }
        Write-Host "wait_boot_$i boot=$boot"
    } else {
        Write-Host "wait_device_$i"
        $connOut = (& $adb connect $Endpoint 2>&1 | Out-String)
        if ($connOut -match "failed|10060|10061|refused|無法|unable") {
            Write-Host $connOut.Trim()
        }
    }
    Start-Sleep 2
}
if (-not $ready) {
    Write-Host "ERROR: ADB not ready for $Endpoint (check free cloud phone ADB is online)" -ForegroundColor Red
    exit 1
}
Write-Host "[ok] adb device ready: $Endpoint" -ForegroundColor Green

$outDir = Join-Path $Root ".alioth-mobile-demo\cloud-phone-verify"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$homePng = Join-Path $outDir "home.png"
$homeBytes = & $adb -s $Endpoint exec-out screencap -p
[IO.File]::WriteAllBytes($homePng, $homeBytes)
Write-Host "[ok] home screencap -> $homePng ($((Get-Item $homePng).Length) bytes)"

$apk = $env:ALIOTH_CLOUD_PHONE_APK
if (-not $apk) {
    $hit = Get-ChildItem (Join-Path $Root ".alioth-mobile-demo\uploads\*.apk") -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $apk = $hit.FullName }
}
if (-not $apk -or -not (Test-Path $apk)) {
    Write-Host "[skip] APK white-screen check (no ALIOTH_CLOUD_PHONE_APK / uploads/*.apk)"
    Write-Host "=== done (connectivity ok) ===" -ForegroundColor Green
    exit 0
}

Write-Host "installing $apk ..."
& $adb -s $Endpoint install -r $apk | Write-Host
$pkg = if ($env:ALIOTH_CLOUD_PHONE_PACKAGE) { $env:ALIOTH_CLOUD_PHONE_PACKAGE } else { "me.mfanv.npymci" }
$act = if ($env:ALIOTH_CLOUD_PHONE_ACTIVITY) { $env:ALIOTH_CLOUD_PHONE_ACTIVITY } else { "$pkg/com.jycg.icg.MainActivity" }
& $adb -s $Endpoint shell am force-stop $pkg 2>$null | Out-Null
& $adb -s $Endpoint shell am start -n $act | Write-Host
Start-Sleep 8
$appPng = Join-Path $outDir "app.png"
$appBytes = & $adb -s $Endpoint exec-out screencap -p
[IO.File]::WriteAllBytes($appPng, $appBytes)

$py = @"
from pathlib import Path
try:
    from PIL import Image
except Exception as e:
    print('SKIP_PIL', e)
    raise SystemExit(0)
p = Path(r'$($appPng.Replace('\','\\'))')
im = Image.open(p).convert('RGB')
w, h = im.size
px = list(im.getdata())
n = len(px)
white = sum(1 for r,g,b in px if r > 245 and g > 245 and b > 245)
pct = 100.0 * white / n
print(f'white_pct={pct:.1f} dim={w}x{h} bytes={p.stat().st_size} center={im.getpixel((w//2,h//2))}')
if pct >= 80.0:
    raise SystemExit('WHITE_SCREEN_FAIL')
print('WHITE_SCREEN_OK')
"@
$tmpPy = Join-Path $outDir "_white_check.py"
[IO.File]::WriteAllText($tmpPy, $py, (New-Object Text.UTF8Encoding $false))
python $tmpPy
if ($LASTEXITCODE -ne 0) { throw "App screencap looks white (pct>=80). Cloud phone GPU/app may still be broken." }
Write-Host "=== done ===" -ForegroundColor Green
