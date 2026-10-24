"""Google Android Emulator（SDK）控制封裝：啟動 AVD、ADB 安裝、準備桌面。

與雷電後端並列；由 ALIOTH_DEVICE_BACKEND=google_emulator 啟用。
依賴本機 ANDROID_HOME / platform-tools / emulator，建議 Linux + KVM。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from services.api.app.infrastructure.device.adb_ops import apply_force_portrait_lock
from services.api.app.infrastructure.device.ldplayer import (
    AppVersionInfo,
    LdPlayerError,
    adb_output_has_device_error,
    evaluate_apk_install_plan,
    normalize_adb_serial,
    parse_pm_list_packages_output,
    parse_pm_path_apk_output,
    parse_version_from_dumpsys_package,
    try_extract_apk_version_from_aapt,
    try_extract_application_label_from_aapt,
    try_extract_package_name_from_aapt,
)

# dumpsys 取证空输出的重试次数：云机 ADB 瞬断常连续发生，单次重试不够。
_FOREGROUND_EVIDENCE_RETRIES = 3

# 系统权限框包名：首启弹权限时前景会是它们，属 App 已启动而非启动失败。
_SYSTEM_PERMISSION_PACKAGES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.samsung.android.permissioncontroller",
)


def parse_foreground_evidence_lines(
    *,
    window_dump: str = "",
    activity_dump: str = "",
) -> list[str]:
    """從 dumpsys 抽出前景證據行。

    部分裝置（含雷電／偽裝機）的 ``dumpsys window`` 會殘留多條 ``mFocusedApp``，
    只取第一行會誤判；應優先看 Activity 的 Resumed／topResumed。
    """
    evidence: list[str] = []
    for line in (activity_dump or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if (
            "topResumedActivity" in text
            or "mResumedActivity" in text
            or text.startswith("ResumedActivity:")
            or " ResumedActivity:" in text
        ):
            evidence.append(text)
    for line in (window_dump or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if "mCurrentFocus=" in text:
            # null 不具判定價值
            if re.search(r"mCurrentFocus=\s*null\b", text):
                continue
            evidence.append(text)
        elif "mFocusedApp=" in text:
            evidence.append(text)
    return evidence


def package_in_foreground_evidence(package_name: str, evidence_lines: list[str]) -> bool:
    """套件名是否出現在任一前景證據行。"""
    pkg = (package_name or "").strip()
    if not pkg:
        return False
    return any(pkg in line for line in evidence_lines)


class GoogleEmulatorError(RuntimeError):
    """Google Emulator / adb 操作失敗。"""


@dataclass(frozen=True)
class GoogleEmulatorPaths:
    android_home: Path
    adb: Path
    emulator: Path | None


def resolve_android_home() -> Path:
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = (os.getenv(key) or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    # 常見預設
    for candidate in (
        Path.home() / "Android" / "Sdk",
        Path("/opt/android-sdk"),
        Path("/usr/lib/android-sdk"),
    ):
        if candidate.exists():
            return candidate.resolve()
    raise GoogleEmulatorError(
        "找不到 Android SDK。請設定 ANDROID_HOME 或 ANDROID_SDK_ROOT，"
        "並安裝 platform-tools 與 emulator。"
    )


def resolve_adb_executable(*, android_home: Path | None = None) -> Path:
    override = (os.getenv("ALIOTH_ADB_EXECUTABLE") or "").strip()
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return path.resolve()
        raise GoogleEmulatorError(f"ALIOTH_ADB_EXECUTABLE 不存在：{override}")

    home = android_home or resolve_android_home()
    for name in ("adb.exe", "adb"):
        candidate = home / "platform-tools" / name
        if candidate.exists():
            return candidate.resolve()

    which = shutil.which("adb")
    if which:
        return Path(which).resolve()
    raise GoogleEmulatorError(
        f"找不到 adb。請確認 {home / 'platform-tools'} 或 PATH 中有 adb。"
    )


def resolve_emulator_executable(*, android_home: Path | None = None) -> Path | None:
    home = android_home or resolve_android_home()
    for name in ("emulator.exe", "emulator"):
        candidate = home / "emulator" / name
        if candidate.exists():
            return candidate.resolve()
    which = shutil.which("emulator")
    if which:
        return Path(which).resolve()
    return None


def resolve_google_emulator_paths() -> GoogleEmulatorPaths:
    home = resolve_android_home()
    return GoogleEmulatorPaths(
        android_home=home,
        adb=resolve_adb_executable(android_home=home),
        emulator=resolve_emulator_executable(android_home=home),
    )


def resolve_avd_name() -> str:
    return (os.getenv("ALIOTH_AVD_NAME") or "Alioth_API30").strip() or "Alioth_API30"


class GoogleEmulatorController:
    """官方 SDK Emulator + adb 控制。"""

    def __init__(self) -> None:
        self._paths = resolve_google_emulator_paths()
        self._avd = resolve_avd_name()
        self._serial: str | None = None
        self._emulator_proc: subprocess.Popen[str] | None = None

    @property
    def android_home(self) -> Path:
        return self._paths.android_home

    @property
    def adb_path(self) -> Path:
        return self._paths.adb

    @property
    def avd_name(self) -> str:
        return self._avd

    def _run_adb(
        self,
        args: list[str],
        *,
        serial: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [str(self._paths.adb)]
        use_serial = serial if serial is not None else self._serial
        if use_serial:
            cmd.extend(["-s", use_serial])
        cmd.extend(args)
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GoogleEmulatorError(f"adb 逾時：{' '.join(cmd)}") from exc
        except Exception as exc:
            raise GoogleEmulatorError(f"adb 執行失敗：{' '.join(cmd)}") from exc

    def list_adb_devices(self, *, timeout_seconds: float = 30.0) -> list[tuple[str, str]]:
        cmd = [str(self._paths.adb), "devices"]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except Exception as exc:
            raise GoogleEmulatorError(f"adb devices 失敗：{exc}") from exc

        devices: list[tuple[str, str]] = []
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and not parts[0].startswith("*") and parts[0] != "List":
                serial = normalize_adb_serial(parts[0])
                if serial:
                    devices.append((serial, parts[1]))
        return devices

    def _device_prop(self, serial: str, prop: str, *, timeout_seconds: float = 8.0) -> str:
        """讀取 getprop；失敗回空字串。"""
        try:
            r = self._run_adb(
                ["shell", "getprop", prop],
                serial=serial,
                timeout_seconds=timeout_seconds,
            )
            if r.returncode != 0:
                return ""
            return (r.stdout or "").strip()
        except Exception:
            return ""

    def _looks_like_google_avd(self, serial: str) -> bool:
        """判斷是否為官方 Android Emulator AVD（排除雷電等偽裝成 emulator-* 的裝置）。"""
        model = self._device_prop(serial, "ro.product.model").lower()
        manufacturer = self._device_prop(serial, "ro.product.manufacturer").lower()
        name = self._device_prop(serial, "ro.product.name").lower()
        fingerprint = self._device_prop(serial, "ro.build.fingerprint").lower()
        qemu = self._device_prop(serial, "ro.kernel.qemu").lower()
        hardware = self._device_prop(serial, "ro.hardware").lower()
        blob = " ".join([model, manufacturer, name, fingerprint, hardware])
        # 雷電／多開常見偽裝
        spoof_markers = (
            "asus",
            "rog",
            "ldmnq",
            "ldplayer",
            "changzhi",
            "nox",
            "bluestacks",
            "microvirt",
            "xyaz",
        )
        if any(m in blob for m in spoof_markers):
            return False
        if qemu in ("1", "true"):
            return True
        google_markers = (
            "sdk_gphone",
            "google_sdk",
            "generic_x86",
            "generic_x86_64",
            "aosp_",
            "emulator",
            "ranchu",
            "goldfish",
        )
        return any(m in blob for m in google_markers)

    def _pick_online_serial(self) -> str | None:
        preferred = (os.getenv("ALIOTH_ADB_SERIAL") or "").strip()
        online = [s for s, state in self.list_adb_devices() if state == "device"]
        if preferred:
            preferred_norm = normalize_adb_serial(preferred) or preferred
            if preferred_norm in online:
                return preferred_norm
        emulator_serials = [s for s in online if s.startswith("emulator-")]
        # 優先真實 Google AVD，避免雷電佔用 emulator-5554 被誤認
        googleish = [s for s in emulator_serials if self._looks_like_google_avd(s)]
        if googleish:
            return googleish[0]
        # 若只有雷電等偽裝機在線：不視為 ready，讓 ensure_ready 去啟動 AVD
        if emulator_serials and not googleish:
            return None
        return online[0] if online else None

    def _auto_start_enabled(self) -> bool:
        return os.getenv("ALIOTH_EMULATOR_AUTO_START", "1").lower() not in (
            "0",
            "false",
            "no",
        )

    def _start_emulator_process(self) -> list[str]:
        logs: list[str] = []
        if self._paths.emulator is None:
            raise GoogleEmulatorError(
                "找不到 emulator 可執行檔。請用 sdkmanager 安裝 emulator，"
                "或確認 ANDROID_HOME/emulator。"
            )
        extra = (os.getenv("ALIOTH_EMULATOR_EXTRA_ARGS") or "").strip()
        if not extra:
            # 伺服器無頭預設。無 KVM 時可再加 -accel off（極慢／可能無法 boot）
            extra = "-no-window -no-audio -gpu swiftshader_indirect -no-metrics"
        cmd = [str(self._paths.emulator), "-avd", self._avd, *extra.split()]
        logs.append(f"start emulator: {' '.join(cmd)}")
        try:
            self._emulator_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except Exception as exc:
            raise GoogleEmulatorError(f"啟動 emulator 失敗：{exc}") from exc
        logs.append(f"emulator pid={self._emulator_proc.pid}")
        return logs

    def _emulator_serials_ordered(self, devices: list[tuple[str, str]]) -> list[str]:
        """emulator-* 序號：已 online 的優先，避免卡在 stale offline。"""
        online: list[str] = []
        others: list[str] = []
        for serial, state in devices:
            if not serial.startswith("emulator-"):
                continue
            if state == "device":
                online.append(serial)
            else:
                others.append(serial)
        return online + others

    def ensure_ready(self, *, timeout_seconds: float | None = None) -> tuple[str, list[str]]:
        """確保有可用 emulator device；必要時自動啟動 AVD。"""
        logs: list[str] = []
        logs.append(f"android_home={self._paths.android_home}")
        logs.append(f"adb={self._paths.adb}")
        logs.append(f"avd={self._avd}")
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("ALIOTH_GOOGLE_EMU_ADB_WAIT_SECONDS", "300")
        )
        serial = self._pick_online_serial()
        if serial:
            logs.append(f"adb already ready: {serial}")
            self._serial = serial
            return serial, logs

        # 有 emulator-* 但皆非 Google AVD（常見：雷電佔用 5554）→ 仍啟動目標 AVD
        spoofed = [
            s
            for s, state in self.list_adb_devices()
            if state == "device" and s.startswith("emulator-")
        ]
        if spoofed:
            logs.append(
                "skip non-Google emulator serial(s)="
                + ",".join(spoofed)
                + "；將啟動 AVD="
                + self._avd
            )

        # 已有 emulator 條目（含 offline）時先等，勿再起第二個 AVD 造成 5556 offline
        existing = self._emulator_serials_ordered(self.list_adb_devices())
        started_new = False
        if not existing and self._auto_start_enabled():
            logs.extend(self._start_emulator_process())
            started_new = True
        elif not existing:
            raise GoogleEmulatorError(
                "adb 無 device，且 ALIOTH_EMULATOR_AUTO_START=0。"
                f"請先手動啟動 AVD：emulator -avd {self._avd}"
            )
        else:
            logs.append(f"wait existing emulator(s): {existing}")

        deadline = time.time() + max(30.0, timeout)
        last_hint = "waiting for emulator"
        while time.time() < deadline:
            online = self._pick_online_serial()
            if online:
                # 已 online 再確認 boot_completed
                boot = self._run_adb(
                    ["shell", "getprop", "sys.boot_completed"],
                    serial=online,
                    timeout_seconds=20.0,
                )
                boot_out = (boot.stdout or "").strip()
                combined = "\n".join(p for p in (boot.stdout, boot.stderr) if p)
                if adb_output_has_device_error(combined):
                    last_hint = combined.strip() or "adb shell error"
                elif boot_out == "1":
                    self._serial = online
                    logs.append(f"adb ready: {online}")
                    return online, logs
                else:
                    last_hint = f"{online} boot_completed={boot_out or '(empty)'}"
            else:
                devices = self.list_adb_devices()
                emu = self._emulator_serials_ordered(devices)
                if not emu and not started_new and self._auto_start_enabled():
                    logs.extend(self._start_emulator_process())
                    started_new = True
                    last_hint = "started emulator after empty devices"
                elif emu:
                    # 對每個候選短等；offline 逾時不中斷整輪
                    for candidate in emu:
                        remain = deadline - time.time()
                        if remain <= 1.0:
                            break
                        try:
                            self._run_adb(
                                ["wait-for-device"],
                                serial=candidate,
                                timeout_seconds=min(15.0, remain),
                            )
                        except GoogleEmulatorError as exc:
                            last_hint = str(exc)
                            continue
                        last_hint = f"wait-for-device ok: {candidate}"
                        break
                else:
                    last_hint = f"devices={devices or 'empty'}"
            time.sleep(3.0)

        raise GoogleEmulatorError(f"等待 Google Emulator ADB 就緒逾時（{timeout}s）：{last_hint}")

    def _require_serial(self) -> str:
        if self._serial:
            return self._serial
        serial = self._pick_online_serial()
        if not serial:
            raise GoogleEmulatorError("ADB 無可用 device，請先 ensure_ready")
        self._serial = serial
        return serial

    def is_package_installed(self, package_name: str, *, timeout_seconds: float = 30.0) -> bool:
        pkg = (package_name or "").strip()
        if not pkg:
            return False
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "pm", "list", "packages", pkg],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        combined = "\n".join(p for p in (r.stdout, r.stderr) if p)
        if adb_output_has_device_error(combined):
            raise GoogleEmulatorError(f"ADB 未就緒，無法檢查包名：{combined.strip()}")
        out = (r.stdout or "").strip()
        for line in out.splitlines():
            line = line.strip()
            if line == f"package:{pkg}" or line.endswith(f":{pkg}"):
                return True
        return f"package:{pkg}" in out.replace(" ", "")

    def list_user_installed_packages(self, *, timeout_seconds: float = 30.0) -> list[str]:
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "pm", "list", "packages", "-3"],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        combined = "\n".join(p for p in (r.stdout, r.stderr) if p)
        if adb_output_has_device_error(combined):
            raise GoogleEmulatorError(f"ADB 未就緒，無法列出包名：{combined.strip()}")
        return parse_pm_list_packages_output(r.stdout or "")

    def _resolve_installed_apk_path(self, package_name: str, *, timeout_seconds: float = 30.0) -> str | None:
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "pm", "path", package_name],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        combined = "\n".join(p for p in (r.stdout, r.stderr) if p)
        if adb_output_has_device_error(combined):
            raise GoogleEmulatorError(f"ADB 未就緒，無法讀取包路徑：{combined.strip()}")
        return parse_pm_path_apk_output(r.stdout or "")

    def get_package_application_label(self, package_name: str, *, timeout_seconds: float = 90.0) -> str | None:
        import tempfile

        from services.api.app.infrastructure.device.installed_apk_metadata import (
            download_installed_apk_metadata,
        )
        from services.api.app.infrastructure.device.ldplayer import (
            _run_aapt_dump_badging,
            parse_application_label_from_aapt_badging,
        )

        apk_path = self._resolve_installed_apk_path(package_name, timeout_seconds=timeout_seconds)
        if not apk_path:
            return None
        serial = self._require_serial()
        with tempfile.TemporaryDirectory(prefix="alioth-apk-") as td:
            local = Path(td) / "base.apk"
            if download_installed_apk_metadata(
                remote_apk_path=apk_path,
                local_apk_path=local,
                run_adb=lambda args: self._run_adb(
                    args, serial=serial, timeout_seconds=timeout_seconds
                ),
            ):
                badging = _run_aapt_dump_badging(
                    local, ldplayer_home=None, allow_label_only_result=True
                )
                label = parse_application_label_from_aapt_badging(badging) if badging else None
                if label:
                    return label
                # 特殊资源引用无法由元数据独立解析时，继续原有整包读取。
                local.unlink(missing_ok=True)
            self._run_adb(
                ["pull", apk_path, str(local)],
                serial=serial,
                timeout_seconds=timeout_seconds,
            )
            if not local.is_file():
                return None
            badging = _run_aapt_dump_badging(local, ldplayer_home=None)
            if badging:
                return parse_application_label_from_aapt_badging(badging)
        return None

    def get_installed_app_version(self, package_name: str, *, timeout_seconds: float = 30.0) -> AppVersionInfo:
        pkg = (package_name or "").strip()
        if not pkg:
            return AppVersionInfo()
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "dumpsys", "package", pkg],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        return parse_version_from_dumpsys_package(
            "\n".join(p for p in (r.stdout, r.stderr) if p)
        )

    def ensure_apk_installed(self, apk_path: Path, *, timeout_seconds: float = 180.0) -> tuple[str, str | None, bool]:
        if not apk_path.exists():
            raise GoogleEmulatorError(f"APK 不存在：{apk_path}")
        self._require_serial()

        pkg = try_extract_package_name_from_aapt(apk_path, ldplayer_home=None)
        apk_version = try_extract_apk_version_from_aapt(apk_path, ldplayer_home=None)
        apk_label = try_extract_application_label_from_aapt(apk_path, ldplayer_home=None)
        logs: list[str] = []
        before_pkgs: set[str] = set()
        before_package_snapshot_ready = False
        before_update_times: dict[str, str] = {}
        if not pkg:
            logs.append(
                "無法解析 package name，改直接安裝 APK"
                "（安裝後以 pm list / lastUpdateTime / 檔案大小補齊）"
            )
            try:
                before_pkgs = set(
                    self.list_user_installed_packages(timeout_seconds=timeout_seconds)
                )
                before_package_snapshot_ready = True
                before_update_times = self._snapshot_package_update_times(
                    before_pkgs, timeout_seconds=timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 - 差分僅為兜底，失敗不阻擋安裝
                logs.append(f"安裝前 pm list 失敗（略過差分）：{exc}")

        try:
            should_install, plan_logs = evaluate_apk_install_plan(
                apk_pkg=pkg,
                apk_version=apk_version,
                apk_label=apk_label,
                is_package_installed=lambda name: self.is_package_installed(
                    name, timeout_seconds=timeout_seconds
                ),
                get_installed_version=lambda name: self.get_installed_app_version(
                    name, timeout_seconds=timeout_seconds
                ),
                list_user_packages=lambda: self.list_user_installed_packages(
                    timeout_seconds=timeout_seconds
                ),
                get_package_label=lambda name: self.get_package_application_label(
                    name, timeout_seconds=timeout_seconds
                ),
            )
        except LdPlayerError as exc:
            raise GoogleEmulatorError(str(exc)) from exc
        logs.extend(plan_logs)
        if not should_install:
            return "\n".join(logs), pkg, False

        if pkg and not self.is_package_installed(pkg, timeout_seconds=timeout_seconds):
            try:
                before_pkgs = set(
                    self.list_user_installed_packages(timeout_seconds=timeout_seconds)
                )
                before_package_snapshot_ready = True
            except Exception as exc:  # noqa: BLE001 - 安装后仍可按 APK 大小恢复
                logs.append(f"安装前 pm list 失败（改用安装后 APK 大小恢复）：{exc}")

        if pkg:
            logs.append(f"will install apk: {pkg}")
        install_log = self.install_apk(apk_path, timeout_seconds=timeout_seconds)
        if install_log:
            logs.append(install_log)

        parsed_package = pkg
        package_requires_recovery = not parsed_package
        if parsed_package:
            try:
                package_requires_recovery = not self.is_package_installed(
                    parsed_package,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - 继续走设备事实恢复
                package_requires_recovery = True
                logs.append(f"安装后校验 package={parsed_package} 失败：{exc}")

        if package_requires_recovery:
            if parsed_package:
                logs.append(f"安装后 package={parsed_package} 不存在，尝试恢复真实包名")
            recovered, how = self._recover_package_name_after_install(
                apk_path=apk_path,
                before_pkgs=(before_pkgs if before_package_snapshot_ready else None),
                before_update_times=before_update_times,
                timeout_seconds=timeout_seconds,
            )
            if recovered:
                pkg = recovered
                logs.append(f"安裝後補齊 package={pkg}（{how}）")
            else:
                logs.append("安裝後仍無法補齊 package name")
        return "\n".join(logs), pkg, True

    def _package_last_update_time(
        self, package_name: str, *, timeout_seconds: float
    ) -> str | None:
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "dumpsys", "package", package_name],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        text = "\n".join(p for p in (r.stdout, r.stderr) if p)
        m = re.search(r"lastUpdateTime=([^\r\n]+)", text)
        if not m:
            return None
        return m.group(1).strip() or None

    def _snapshot_package_update_times(
        self, packages: set[str], *, timeout_seconds: float
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in packages:
            ts = self._package_last_update_time(name, timeout_seconds=timeout_seconds)
            if ts:
                out[name] = ts
        return out

    def _package_apk_size_bytes(
        self, package_name: str, *, timeout_seconds: float
    ) -> int | None:
        apk_path = self._resolve_installed_apk_path(
            package_name, timeout_seconds=timeout_seconds
        )
        if not apk_path:
            return None
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "stat", "-c", "%s", apk_path],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        text = (r.stdout or "").strip().splitlines()
        if not text:
            return None
        try:
            return int(text[0].strip())
        except ValueError:
            return None

    def _recover_package_name_after_install(
        self,
        *,
        apk_path: Path,
        before_pkgs: set[str] | None,
        before_update_times: dict[str, str],
        timeout_seconds: float,
    ) -> tuple[str | None, str]:
        """aapt 不可用時：差分 / lastUpdateTime / 檔案大小推測 package name。"""
        try:
            after_pkgs = set(
                self.list_user_installed_packages(timeout_seconds=timeout_seconds)
            )
        except Exception:  # noqa: BLE001
            return None, ""

        if before_pkgs is not None:
            added = sorted(after_pkgs - before_pkgs)
            if len(added) == 1:
                return added[0], "pm list 新增差分"
            if len(added) > 1:
                return added[0], f"pm list 多個新增取首個({len(added)})"

        # 重裝（-r）時包名不變：比對 lastUpdateTime 是否前進
        updated: list[str] = []
        for name in sorted(after_pkgs):
            if name not in before_update_times:
                continue
            after_ts = self._package_last_update_time(
                name, timeout_seconds=timeout_seconds
            )
            before_ts = before_update_times.get(name)
            if after_ts and after_ts != before_ts:
                updated.append(name)
        if len(updated) == 1:
            return updated[0], "lastUpdateTime 變更"
        if len(updated) > 1:
            return updated[0], f"lastUpdateTime 多個變更取首個({len(updated)})"

        # 最後以本機 APK 大小對已安裝 base.apk 對齊
        try:
            local_size = int(apk_path.stat().st_size)
        except OSError:
            return None, ""
        size_hits: list[str] = []
        for name in sorted(after_pkgs):
            remote_size = self._package_apk_size_bytes(
                name, timeout_seconds=timeout_seconds
            )
            if remote_size == local_size:
                size_hits.append(name)
        if len(size_hits) == 1:
            return size_hits[0], "APK 檔案大小對齊"
        if len(size_hits) > 1:
            return size_hits[0], f"APK 大小多個命中取首個({len(size_hits)})"
        return None, ""

    def install_apk(self, apk_path: Path, *, timeout_seconds: float = 180.0) -> str:
        if not apk_path.exists():
            raise GoogleEmulatorError(f"APK 不存在：{apk_path}")
        serial = self._require_serial()
        apk_str = str(apk_path.resolve())
        logs: list[str] = [f"adb -s {serial} install -r {apk_str}"]
        r = self._run_adb(
            ["install", "-r", apk_str],
            serial=serial,
            timeout_seconds=timeout_seconds,
        )
        combined = "\n".join(p for p in (r.stdout, r.stderr) if p).strip()
        if combined:
            logs.append(combined)
        if adb_output_has_device_error(combined):
            raise GoogleEmulatorError("ADB 未就緒，無法安裝 APK：\n" + "\n".join(logs))
        if r.returncode != 0 or "Success" not in combined:
            raise GoogleEmulatorError("adb install 失敗：\n" + "\n".join(logs))
        return "\n".join(logs)

    def resolve_launcher_component(self, package_name: str) -> str | None:
        """解析 MAIN/LAUNCHER Activity 元件（package/activity）。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return None
        serial = self._require_serial()
        r = self._run_adb(
            ["shell", "cmd", "package", "resolve-activity", "--brief", pkg],
            serial=serial,
            timeout_seconds=20.0,
        )
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        for line in reversed(lines):
            # 例如 me.mfanv.npymci/com.jycg.icg.MainActivity
            if "/" in line and not line.startswith("priority=") and " " not in line:
                return line
        return None

    def current_focus_line(self) -> str:
        """讀取目前焦點描述（優先 ResumedActivity，避免多條 mFocusedApp 誤判）。"""
        evidence = self.foreground_evidence_lines()
        return evidence[0] if evidence else ""

    def foreground_evidence_lines(self) -> list[str]:
        """彙總 Activity／Window dumpsys 的前景證據行。

        dumpsys 自身失敗（雲機 ADB 瞬斷常見 error: closed）時 stdout 為空，
        會被上游讀成「App 不在前景」。雲機的瞬斷常是連續的（重連成功後下一條
        指令又斷），單次重試不夠，因此空輸出時退避重試數次並在中途重連 ADB；
        真的沒有前景時 dumpsys 仍會回內容，不會被這層掩蓋。
        """
        serial = self._require_serial()

        def _dump() -> tuple[str, str]:
            activity = self._run_adb(
                ["shell", "dumpsys", "activity", "activities"],
                serial=serial,
                timeout_seconds=20.0,
            )
            window = self._run_adb(
                ["shell", "dumpsys", "window"],
                serial=serial,
                timeout_seconds=20.0,
            )
            return (activity.stdout or ""), (window.stdout or "")

        activity_dump, window_dump = _dump()
        for attempt in range(_FOREGROUND_EVIDENCE_RETRIES):
            if activity_dump.strip() or window_dump.strip():
                break
            time.sleep(1.0 + attempt)
            if attempt == 1:
                # 连续两次空输出多半是 ADB 通道半开，重连后再取一次
                try:
                    self._reconnect_for_foreground_evidence()
                except Exception:  # noqa: BLE001 - 重连失败仍按取证失败处理
                    pass
            activity_dump, window_dump = _dump()
        # 「dumpsys 本身没输出」与「有输出但解析不出前景行」是两回事，排障要分得开：
        # 前者是通道断了，后者是前景真的不是我们要的 App。记在实例上供上游取用。
        self._last_foreground_dump_empty = not (
            activity_dump.strip() or window_dump.strip()
        )
        return parse_foreground_evidence_lines(
            window_dump=window_dump,
            activity_dump=activity_dump,
        )

    def app_process_running(self, package_name: str) -> bool | None:
        """App 进程是否存活；探针本身失败回传 None（区别于「确定没跑」）。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return None
        serial = self._require_serial()
        try:
            result = self._run_adb(
                ["shell", "pidof", pkg], serial=serial, timeout_seconds=15.0
            )
        except Exception:  # noqa: BLE001 - 探针失败不改变既有判定
            return None
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip().casefold()
        if not stdout and ("error" in stderr or "closed" in stderr):
            return None
        return bool(stdout and stdout.split()[0].isdigit())

    def _reconnect_for_foreground_evidence(self) -> None:
        """取证通道半开时重连；基类无重连能力则跳过。"""
        reconnect = getattr(self, "_reopen_adb_session", None)
        if callable(reconnect):
            reconnect(self._require_serial(), [])

    def prepare_app_for_script(
        self,
        package_name: str,
        *,
        settle_seconds: float = 5.0,
        require_foreground: bool = True,
    ) -> str:
        """以 am start 啟動 App（不依賴雷電桌面圖示；比 monkey 更穩定）。

        require_foreground=False 時僅記錄警告（供 Agent 路徑在 native crash 時仍進入 Driver）。
        """
        pkg = (package_name or "").strip()
        if not pkg:
            raise GoogleEmulatorError("package_name 不可為空")
        serial = self._require_serial()
        logs: list[str] = []

        def _shell(args: list[str], timeout: float) -> None:
            self._run_adb(["shell", *args], serial=serial, timeout_seconds=timeout)

        def _shell_output(args: list[str], timeout: float) -> str:
            result = self._run_adb(
                ["shell", *args], serial=serial, timeout_seconds=timeout
            )
            return str(result.stdout or "")

        # 启动前锁定直屏，避免横屏机型／残留 Override 把 App 拉横。
        logs.extend(apply_force_portrait_lock(shell=_shell, shell_output=_shell_output))

        # 對齊 untitled.py 廣告模板常見解析度（1080x1920），降低跨裝置匹配失敗
        size = (os.getenv("ALIOTH_EMULATOR_WM_SIZE") or "1080x1920").strip()
        if size.lower() not in ("0", "off", "keep", "-"):
            self._run_adb(["shell", "wm", "size", size], serial=serial, timeout_seconds=15.0)
            logs.append(f"wm size {size}")
            density = (os.getenv("ALIOTH_EMULATOR_WM_DENSITY") or "320").strip()
            if density.lower() not in ("0", "off", "keep", "-"):
                self._run_adb(["shell", "wm", "density", density], serial=serial, timeout_seconds=15.0)
                logs.append(f"wm density {density}")
            # wm size 可能再次触发旋转协商，补锁一次。
            logs.extend(apply_force_portrait_lock(shell=_shell, shell_output=_shell_output))

        self._run_adb(["shell", "am", "force-stop", pkg], serial=serial, timeout_seconds=30.0)
        logs.append(f"force-stop {pkg}")
        time.sleep(1.0)

        component = self.resolve_launcher_component(pkg)
        if component:
            logs.append(f"launcher component={component}")
            launch = self._run_adb(
                ["shell", "am", "start", "-n", component],
                serial=serial,
                timeout_seconds=30.0,
            )
        else:
            # 後備：monkey（部分環境 resolve-activity 失敗時）
            logs.append("WARN: resolve-activity 失敗，改用 monkey")
            launch = self._run_adb(
                [
                    "shell",
                    "monkey",
                    "-p",
                    pkg,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
                serial=serial,
                timeout_seconds=30.0,
            )
        launch_out = "\n".join(p for p in (launch.stdout, launch.stderr) if p).strip()
        if launch_out:
            logs.append(launch_out[:500])
        logs.append(f"am start {pkg}" if component else f"monkey launch {pkg}")

        wait_s = max(3.0, float(settle_seconds))
        logs.append(f"wait settle {wait_s}s after app launch")
        time.sleep(wait_s)
        # App 冷启常会再请求横屏；settle 后再锁一次以压回直屏。
        logs.extend(apply_force_portrait_lock(shell=_shell, shell_output=_shell_output))

        # 確認仍在前景；否則後續 Airtest 只會截到桌面／其他偽裝 App。
        # 冷啟動的 App 可能還在初始化，dumpsys 也會短暫回空，單次判定會誤判成
        # 「啟動後立刻退出」；因此在既有 settle 之後再輪詢一段時間。
        evidence = self.foreground_evidence_lines()
        if not package_in_foreground_evidence(pkg, evidence):
            extra_deadline = time.monotonic() + max(0.0, float(settle_seconds) * 3.0)
            while time.monotonic() < extra_deadline:
                time.sleep(2.0)
                evidence = self.foreground_evidence_lines()
                if package_in_foreground_evidence(pkg, evidence):
                    logs.append("app entered foreground after extra wait")
                    break
        for line in evidence[:6]:
            logs.append(f"focus evidence: {line[:240]}")
        if not package_in_foreground_evidence(pkg, evidence):
            # 首启常见：系统权限框盖在 App 之上。这是 App 已经起来的证据，
            # 弹窗本身交由探索器按既有清障流程处理，不能当成启动失败。
            blob = "\n".join(evidence)
            if any(name in blob for name in _SYSTEM_PERMISSION_PACKAGES):
                logs.append("系統權限框位於前景，視為 App 已啟動並交由探索處理")
                return "\n".join(logs)
            # 取不到前景证据时先确认进程死没死：进程还活着说明 App 已经起来，
            # 只是这台机读不到焦点（云机 ADB 瞬断、dumpsys 被裁剪都会这样）。
            # 把「我没看见」当成「它不存在」正是此前整任务白跑的原因。
            dump_empty = bool(getattr(self, "_last_foreground_dump_empty", False))
            if not evidence and dump_empty and self.app_process_running(pkg) is True:
                logs.append(
                    "焦點取證不可用（dumpsys 無輸出）但 App 進程存活，視為已啟動"
                )
                return "\n".join(logs)
            summary = (
                evidence[0]
                if evidence
                else ("(dumpsys 無輸出)" if dump_empty else "(dumpsys 有輸出但無前景行)")
            )
            msg = (
                f"App 啟動後未留在前景（package={pkg}）。"
                f"目前焦點：{summary}。"
                "常見原因：模擬器 ABI/GPU 不相容導致進程立刻退出，"
                "或啟動 Activity 解析失敗；"
                "亦可能是 dumpsys window 殘留舊 mFocusedApp（已改查 ResumedActivity）。"
            )
            if require_foreground:
                raise GoogleEmulatorError(msg)
            logs.append(f"WARN: {msg}（require_foreground=0，繼續）")
        return "\n".join(logs)

    def ensure_sdk_adb_server(self) -> list[str]:
        """確保 ADB server 來自 SDK（避免 Airtest 綁到雷電 adb.exe 進程）。"""
        logs: list[str] = []
        adb = str(self._paths.adb)
        logs.append(f"ensure sdk adb server: {adb}")
        try:
            subprocess.run(
                [adb, "kill-server"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            subprocess.run(
                [adb, "start-server"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            # 重新掛上目前 serial
            if self._serial:
                subprocess.run(
                    [adb, "devices"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            logs.append("sdk adb server restarted")
        except Exception as exc:
            logs.append(f"sdk adb server warn: {exc}")
        return logs

    def airtest_device_uri(self) -> str:
        serial = self._require_serial()
        override = (os.getenv("ALIOTH_MOBILE_DEMO_AIRTEST_DEVICE") or "").strip()
        if override:
            return override
        return f"Android:///{serial}"
