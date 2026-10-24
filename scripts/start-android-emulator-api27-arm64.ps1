# 用不做轉譯方式啟動 API27 arm64（需舊版 Emulator；本機 x86 已實測易 hang）
# Usage: powershell -File scripts/start-android-emulator-api27-arm64.ps1
# 依賴：F:\Android\Sdk\emulator-31.3.13 （build 9189900）

$ErrorActionPreference = "Stop"
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "F:\Android\Sdk" }
$emuDir = Join-Path $sdk "emulator-31.3.13"
$emu = Join-Path $emuDir "emulator.exe"
$adb = Join-Path $sdk "platform-tools\adb.exe"
$avd = if ($env:ALIOTH_AVD_NAME) { $env:ALIOTH_AVD_NAME } else { "Alioth_API27_arm64" }

if (-not (Test-Path $emu)) {
  throw "Old emulator not found: $emu. Download emulator-windows_x64-9189900.zip and extract to $emuDir"
}

Write-Host "WARNING: On x86 hosts this path often hangs (QEMU2 hanging thread). Prefer ARM64 servers." -ForegroundColor Yellow
$env:ANDROID_SDK_ROOT = $sdk
$env:ANDROID_HOME = $sdk
Start-Process -FilePath $emu -ArgumentList @(
  "-avd", $avd,
  "-accel", "off",
  "-gpu", "swiftshader_indirect",
  "-no-audio",
  "-no-snapshot",
  "-no-snapshot-load",
  "-cores", "2"
) -WindowStyle Normal

Write-Host "Started $avd with emulator-31.3.13. Wait for adb device (may never reach boot_completed on x86)."
