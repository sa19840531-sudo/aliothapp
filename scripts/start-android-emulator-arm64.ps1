# Start official Emulator arm64-v8a AVD (QEMU full ARM, no houdini/ndk)
# Usage: powershell -File scripts/start-android-emulator-arm64.ps1
# Note: very slow on x86 hosts; production should use ARM64 machines.

$ErrorActionPreference = "Stop"
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "F:\Android\Sdk" }
$avd = if ($env:ALIOTH_AVD_NAME) { $env:ALIOTH_AVD_NAME } else { "Alioth_API30_arm64" }
$emu = Join-Path $sdk "emulator\emulator.exe"
$adb = Join-Path $sdk "platform-tools\adb.exe"
if (-not (Test-Path $emu)) { throw "emulator not found: $emu" }

$gpu = if ($env:ALIOTH_EMU_GPU) { $env:ALIOTH_EMU_GPU } else { "swiftshader_indirect" }
$extra = if ($env:ALIOTH_EMULATOR_EXTRA_ARGS) { $env:ALIOTH_EMULATOR_EXTRA_ARGS } else { "-no-metrics -no-audio" }

Write-Host "Starting AVD=$avd (arm64 QEMU, accel=off, gpu=$gpu)..." -ForegroundColor Cyan
Write-Host "On x86 this is slow; first boot may take 5-15 minutes." -ForegroundColor Yellow

$argList = @("-avd", $avd, "-accel", "off", "-gpu", $gpu) + ($extra -split "\s+" | Where-Object { $_ })
Start-Process -FilePath $emu -ArgumentList $argList -WindowStyle Normal

& $adb start-server | Out-Null
$deadline = (Get-Date).AddMinutes(20)
do {
  Start-Sleep 10
  $devs = & $adb devices | Where-Object { $_ -match "emulator-\d+\s+device" }
  $serial = if ($devs) { (($devs | Select-Object -First 1) -split "\s+")[0] } else { $null }
  if ($serial) {
    $boot = (& $adb -s $serial shell getprop sys.boot_completed 2>$null | Out-String).Trim()
    $abi = (& $adb -s $serial shell getprop ro.product.cpu.abi 2>$null | Out-String).Trim()
    Write-Host "serial=$serial boot=$boot abi=$abi"
    if ($boot -eq "1") {
      Write-Host "READY serial=$serial abi=$abi" -ForegroundColor Green
      Write-Host "Set ALIOTH_REDROID_ADB=$serial ALIOTH_DEVICE_BACKEND=redroid"
      exit 0
    }
  } else {
    Write-Host "waiting for emulator device..."
  }
} while ((Get-Date) -lt $deadline)

Write-Host "BOOT_TIMEOUT" -ForegroundColor Red
exit 1
