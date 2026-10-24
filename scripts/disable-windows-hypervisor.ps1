# Path B 完整關閉 Windows Hypervisor（管理員執行後必須重開機）
#
# 僅關「Hyper-V 功能」不夠：只要 hypervisorlaunchtype=Auto 或 VBS/核心隔離開著，
# VMware 訪客就看不到 /dev/kvm。
#
# 用法：右鍵「以系統管理員身分執行」PowerShell
#   powershell -File F:\aliothG\scripts\disable-windows-hypervisor.ps1

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

Write-Host "=== 1) bcdedit hypervisorlaunchtype Off ===" -ForegroundColor Cyan
bcdedit /enum '{current}' | Select-String -Pattern "identifier|description|hypervisorlaunchtype"
bcdedit /set '{current}' hypervisorlaunchtype Off
if ($LASTEXITCODE -ne 0) { throw "bcdedit set failed" }

Write-Host "=== 2) optional features Off (best-effort) ===" -ForegroundColor Cyan
$features = @(
    "Microsoft-Hyper-V-All",
    "Microsoft-Hyper-V",
    "Microsoft-Hyper-V-Hypervisor",
    "Microsoft-Hyper-V-Tools-All",
    "Microsoft-Hyper-V-Services",
    "HypervisorPlatform",
    "VirtualMachinePlatform",
    "Windows-Defender-ApplicationGuard"
)
foreach ($f in $features) {
    try {
        $st = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue
        if ($null -eq $st) { continue }
        Write-Host ("  {0} = {1}" -f $f, $st.State)
        if ($st.State -eq "Enabled") {
            Disable-WindowsOptionalFeature -Online -FeatureName $f -NoRestart -ErrorAction SilentlyContinue | Out-Null
            Write-Host "    -> disabled (pending reboot)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host ("  skip {0}: {1}" -f $f, $_.Exception.Message)
    }
}

Write-Host "=== 3) Device Guard / VBS registry (best-effort) ===" -ForegroundColor Cyan
$paths = @(
    "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard",
    "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity"
)
foreach ($p in $paths) {
    if (-not (Test-Path $p)) { continue }
    try {
        New-ItemProperty -Path $p -Name "EnableVirtualizationBasedSecurity" -PropertyType DWord -Value 0 -Force -ErrorAction SilentlyContinue | Out-Null
        New-ItemProperty -Path $p -Name "RequirePlatformSecurityFeatures" -PropertyType DWord -Value 0 -Force -ErrorAction SilentlyContinue | Out-Null
        New-ItemProperty -Path $p -Name "Enabled" -PropertyType DWord -Value 0 -Force -ErrorAction SilentlyContinue | Out-Null
        Write-Host "  patched $p"
    } catch {
        Write-Host "  skip $p"
    }
}

Write-Host ""
Write-Host "=== after bcdedit ===" -ForegroundColor Green
bcdedit /enum '{current}' | Select-String -Pattern "hypervisorlaunchtype"

Write-Host ""
Write-Host "MUST REBOOT now." -ForegroundColor Yellow
Write-Host "After reboot verify:"
Write-Host "  (Get-CimInstance Win32_ComputerSystem).HypervisorPresent   # expect False"
Write-Host "  wsl -l -v   # expect fail or no Running"
Write-Host "Then:"
Write-Host "  powershell -File F:\aliothG\scripts\start-path-b-vmware-docker.ps1"
Write-Host ""
Write-Host "Also check Windows Security -> Device security -> Core isolation -> Memory integrity = Off"
Write-Host "Schedule reboot?  shutdown /r /t 60"
