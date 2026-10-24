"""雷電模擬器（LDPlayer）控制封裝：啟動、安裝 APK、（可選）啟動 App。

說明：
- 這是 mobile-demo 的基礎設施層，與 Web 的 `infrastructure/browser` 並列，不替換任何既有 Web 執行層。
- 只做最小封裝，避免把 Agent / API 與 ldconsole 參數耦合在一起。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppVersionInfo:
    """APK 或模擬器內已安裝 App 的版本資訊。"""

    version_code: int | None = None
    version_name: str | None = None

    def label(self) -> str:
        parts: list[str] = []
        if self.version_name:
            parts.append(f"name={self.version_name}")
        if self.version_code is not None:
            parts.append(f"code={self.version_code}")
        return ", ".join(parts) if parts else "unknown"


@dataclass(frozen=True)
class LdPlayerPaths:
    home: Path
    ldconsole: Path


class LdPlayerError(RuntimeError):
    pass


# 允許 emulator-5554、IPv4:port、以及 Docker 服務名（如 alioth-redroid:5555）
_ADB_SERIAL_PATTERN = re.compile(
    r"^(emulator-\d+|\d+\.\d+\.\d+\.\d+:\d+|[A-Za-z0-9][A-Za-z0-9._-]*:\d+)$"
)
ADB_DEBUG_OFF = 0
ADB_DEBUG_LOCAL = 1


def normalize_adb_serial(candidate: str) -> str | None:
    """過濾 adb 錯誤訊息，只保留可用 serial。"""
    serial = (candidate or "").strip()
    if not serial:
        return None
    low = serial.lower()
    if low in ("unknown",):
        return None
    if "error" in low or "not found" in low:
        return None
    if _ADB_SERIAL_PATTERN.match(serial):
        return serial
    if serial.startswith("emulator-") and serial[9:].isdigit():
        return serial
    return None


def adb_output_has_device_error(text: str) -> bool:
    low = (text or "").lower()
    return "not found" in low or "no devices/emulators" in low or "unknown host service" in low


_LDPLAYER_HOME_DEV = Path(r"F:\LDPlayer\LDPlayer9")
_LDPLAYER_HOME_PROD = Path(r"C:\LDPlayer\LDPlayer9")


def ldplayer_home_for_stage(stage: str) -> Path:
    """依階段回傳雷電目錄；可被 ALIOTH_LDPLAYER_HOME_DEV / _PROD 覆寫。"""
    if stage == "prod":
        override = (os.getenv("ALIOTH_LDPLAYER_HOME_PROD") or "").strip()
        return Path(override) if override else _LDPLAYER_HOME_PROD
    override = (os.getenv("ALIOTH_LDPLAYER_HOME_DEV") or "").strip()
    return Path(override) if override else _LDPLAYER_HOME_DEV


def resolve_mobile_demo_stage() -> str:
    """解析 Mobile Demo 階段：dev（開發）或 prod（正式測試）。"""
    explicit = (os.getenv("ALIOTH_MOBILE_DEMO_STAGE") or "").strip().lower()
    if explicit in ("dev", "development", "local", "開發"):
        return "dev"
    if explicit in ("prod", "production", "formal", "正式"):
        return "prod"
    env = (os.getenv("ALIOTH_ENVIRONMENT") or "local").strip().lower()
    if env in ("production", "prod", "formal"):
        return "prod"
    return "dev"


def resolve_ldplayer_home() -> Path:
    """解析雷電安裝目錄：ALIOTH_LDPLAYER_HOME 優先，否則依 STAGE 選 DEV/PROD 路徑。"""
    override = (os.getenv("ALIOTH_LDPLAYER_HOME") or "").strip()
    if override:
        return Path(override)
    return ldplayer_home_for_stage(resolve_mobile_demo_stage())


@dataclass(frozen=True)
class LdDeviceProfile:
    """預建雷電實例的矩陣 profile（執行時只切 index，不改解析度）。"""

    id: str
    index: int
    label: str
    width: int | None = None
    height: int | None = None
    dpi: int | None = None


@dataclass(frozen=True)
class LdInstanceInfo:
    """ldconsole list2 解析後的單筆實例。"""

    index: int
    name: str
    raw: str = ""


_PROFILE_ID_RE = re.compile(r"^[\w.\-]+$")


def _sanitize_profile_id(raw: str, *, fallback: str) -> str:
    candidate = (raw or "").strip() or fallback
    if not _PROFILE_ID_RE.match(candidate):
        raise LdPlayerError(f"非法 profile id：{raw!r}（僅允許字母數字 _ . -）")
    return candidate


def _profile_from_dict(item: dict, *, fallback_id: str) -> LdDeviceProfile:
    try:
        index = int(item.get("index", 0))
    except (TypeError, ValueError) as exc:
        raise LdPlayerError(f"profile index 無效：{item.get('index')!r}") from exc
    if index < 0:
        raise LdPlayerError(f"profile index 不可為負數：{index}")
    pid = _sanitize_profile_id(str(item.get("id") or fallback_id), fallback=fallback_id)
    label = str(item.get("label") or pid).strip() or pid
    width = item.get("width")
    height = item.get("height")
    dpi = item.get("dpi")

    def _optional_positive(name: str, value: object) -> int | None:
        if value is None:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise LdPlayerError(f"profile {name} 無效：{value!r}") from exc
        if number <= 0:
            raise LdPlayerError(f"profile {name} 須為正整數：{number}")
        return number

    return LdDeviceProfile(
        id=pid,
        index=index,
        label=label,
        width=_optional_positive("width", width),
        height=_optional_positive("height", height),
        dpi=_optional_positive("dpi", dpi),
    )


def _default_ldplayer_profiles() -> list[LdDeviceProfile]:
    index = int(os.getenv("ALIOTH_LDPLAYER_INDEX", "0"))
    return [
        LdDeviceProfile(
            id="default",
            index=index,
            label=f"雷電 index={index}",
        )
    ]


def _load_profiles_json(raw: str, *, source: str) -> list[LdDeviceProfile]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LdPlayerError(f"解析 {source} 失敗：{exc}") from exc
    if not isinstance(data, list) or not data:
        raise LdPlayerError(f"{source} 須為非空 JSON 陣列")
    profiles: list[LdDeviceProfile] = []
    seen_ids: set[str] = set()
    seen_indexes: set[int] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise LdPlayerError(f"{source}[{i}] 須為物件")
        profile = _profile_from_dict(item, fallback_id=f"device_{i}")
        if profile.id in seen_ids:
            raise LdPlayerError(f"{source} 重複 profile id：{profile.id}")
        if profile.index in seen_indexes:
            raise LdPlayerError(f"{source} 重複 profile index：{profile.index}")
        seen_ids.add(profile.id)
        seen_indexes.add(profile.index)
        profiles.append(profile)
    return profiles


def _resolve_profiles_file_path(configured: str) -> Path:
    """把 ALIOTH_LDPLAYER_PROFILES_FILE 解析為當前進程可讀路徑。

    .env.local 常寫 Windows 宿主路徑（如 F:/aliothG/infra/env/...），
    Docker API 在 Linux 容器內無法直接讀取，需回退到 /workspace 掛載。
    """
    path = Path(configured)
    if path.is_file():
        return path

    name = path.name
    norm = configured.replace("\\", "/")
    candidates: list[Path] = []

    workspace = Path("/workspace")
    if workspace.is_dir():
        candidates.append(workspace / "infra" / "env" / name)
        for marker in ("infra/env/", "/infra/env/"):
            if marker in norm:
                rel = norm.split(marker, 1)[1]
                candidates.append(workspace / "infra" / "env" / rel)

    candidates.append(Path.cwd() / "infra" / "env" / name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise LdPlayerError(f"找不到 ALIOTH_LDPLAYER_PROFILES_FILE：{path}")


def resolve_ldplayer_profiles() -> list[LdDeviceProfile]:
    """解析多機型矩陣 profiles。

    優先順序：
    1. ALIOTH_LDPLAYER_PROFILES（JSON 陣列字串）
    2. ALIOTH_LDPLAYER_PROFILES_FILE（檔案路徑）
    3. 倉庫預設檔 infra/env/ldplayer-profiles.local.json（若存在）
    4. 回退單一 profile：ALIOTH_LDPLAYER_INDEX（預設 0）
    """
    inline = (os.getenv("ALIOTH_LDPLAYER_PROFILES") or "").strip()
    if inline:
        return _load_profiles_json(inline, source="ALIOTH_LDPLAYER_PROFILES")

    file_path = (os.getenv("ALIOTH_LDPLAYER_PROFILES_FILE") or "").strip()
    if file_path:
        path = _resolve_profiles_file_path(file_path)
        return _load_profiles_json(path.read_text(encoding="utf-8"), source=str(path))

    # 本機常見：寫了 local profiles 但進程沒載入 infra/env/.env.local
    # 僅檢查 cwd（通常為倉庫根），避免測試／異路徑誤讀
    candidate = Path.cwd() / "infra" / "env" / "ldplayer-profiles.local.json"
    try:
        if candidate.is_file():
            return _load_profiles_json(
                candidate.read_text(encoding="utf-8"),
                source=str(candidate),
            )
    except OSError:
        pass

    return _default_ldplayer_profiles()


def parse_ldconsole_list2(output: str) -> list[LdInstanceInfo]:
    """解析 ldconsole list2 輸出（CSV：index,name,...）。"""
    instances: list[LdInstanceInfo] = []
    for line in (output or "").splitlines():
        raw = line.strip()
        if not raw or raw.lower().startswith("index"):
            continue
        parts = [p.strip() for p in raw.split(",")]
        if not parts or not parts[0].isdigit():
            continue
        index = int(parts[0])
        name = parts[1] if len(parts) > 1 else f"leidian{index}"
        instances.append(LdInstanceInfo(index=index, name=name, raw=raw))
    return instances


def _default_home() -> Path:
    return resolve_ldplayer_home()


def _find_console_exe(home: Path) -> Path | None:
    for name in ("ldconsole.exe", "dnconsole.exe", "LDConsole.exe"):
        candidate = home / name
        if candidate.exists():
            return candidate
    return None


def is_ldplayer_available(home: Path | None = None) -> bool:
    """本機是否可呼叫雷電 ldconsole。"""
    if os.name != "nt":
        return False
    h = home or _default_home()
    return _find_console_exe(h) is not None


def resolve_ldplayer_paths(home: Path | None = None) -> LdPlayerPaths:
    h = home or _default_home()
    ldconsole = _find_console_exe(h)
    if ldconsole is None:
        tried = ", ".join(str(h / n) for n in ("ldconsole.exe", "dnconsole.exe"))
        hint = ""
        if os.path.exists("/.dockerenv") or os.getenv("RUNNING_IN_DOCKER") == "1":
            hint = (
                " 目前 API 在 Docker 容器內，無法直接執行 Windows 版雷電。"
                "請在本機 Windows 執行：python -m services.api.app.mobile_demo_host_runner"
                "（預設 http://127.0.0.1:8765），並設定 ALIOTH_MOBILE_DEMO_HOST_EXECUTOR=http://host.docker.internal:8765"
            )
        raise LdPlayerError(f"找不到雷電控制台程式（ldconsole/dnconsole）：{tried}.{hint}")
    return LdPlayerPaths(home=h, ldconsole=ldconsole)


class LdPlayerController:
    def __init__(self, *, home: Path | None = None, index: int = 0) -> None:
        self._paths = resolve_ldplayer_paths(home)
        self._index = index

    @property
    def home(self) -> Path:
        return self._paths.home

    @property
    def index(self) -> int:
        return self._index

    def _run_ldconsole(self, args: list[str], *, timeout_seconds: float = 120.0) -> subprocess.CompletedProcess:
        cmd = [str(self._paths.ldconsole), *args]
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
            raise LdPlayerError(f"ldconsole 逾時：{' '.join(cmd)}") from exc
        except Exception as exc:
            raise LdPlayerError(f"ldconsole 執行失敗：{' '.join(cmd)}") from exc

    def launch(
        self,
        *,
        timeout_seconds: float = 60.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """啟動模擬器；若已在跑則略過，否則輪詢直到進入 running 或逾時。"""
        if self.is_emulator_running():
            if log:
                log(f"emulator index={self._index} already running; skip launch")
            return

        if log:
            log(f"ldconsole launch --index {self._index}")
        r = self._run_ldconsole(["launch", "--index", str(self._index)], timeout_seconds=timeout_seconds)
        if r.returncode != 0:
            raise LdPlayerError(f"啟動雷電失敗：{r.stdout}\n{r.stderr}".strip())

        poll_seconds = float(os.getenv("ALIOTH_LDPLAYER_LAUNCH_POLL_SECONDS", "120"))
        deadline = time.time() + max(10.0, poll_seconds)
        while time.time() < deadline:
            if self.is_emulator_running():
                if log:
                    log(f"emulator index={self._index} process running after launch")
                return
            time.sleep(2.0)
        raise LdPlayerError(
            f"launch 指令已送出，但模擬器 index={self._index} 在 {poll_seconds}s 內未進入 running；"
            "請手動開啟雷電多開器檢查 index 0 是否閃退"
        )

    def instance_config_path(self) -> Path:
        """單開模擬器實例設定檔（含 basicSettings.adbDebug）。"""
        return self._paths.home / "vms" / "config" / f"leidian{self._index}.config"

    def is_emulator_running(self, *, timeout_seconds: float = 30.0) -> bool:
        r = self._run_ldconsole(
            ["isrunning", "--index", str(self._index)],
            timeout_seconds=timeout_seconds,
        )
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part).strip().lower()
        return "running" in combined

    def list_instances(self, *, timeout_seconds: float = 30.0) -> list[LdInstanceInfo]:
        """列出雷電多開實例（ldconsole list2）。"""
        r = self._run_ldconsole(["list2"], timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        instances = parse_ldconsole_list2(combined)
        if not instances and r.returncode != 0:
            raise LdPlayerError(f"ldconsole list2 失敗：{combined.strip()}")
        return instances

    def assert_instance_exists(self, index: int | None = None) -> LdInstanceInfo:
        """確認指定 index 實例存在於 list2；否則拋錯。"""
        target = self._index if index is None else int(index)
        instances = self.list_instances()
        for info in instances:
            if info.index == target:
                return info
        known = ", ".join(f"{i.index}:{i.name}" for i in instances) or "(無)"
        raise LdPlayerError(f"雷電實例 index={target} 不存在；目前 list2：{known}")

    def _has_adb_device(self, *, include_offline: bool = False) -> bool:
        devices = self.list_adb_devices(timeout_seconds=15.0)
        if include_offline:
            return bool(devices)
        return any(state == "device" for _, state in devices)

    def read_adb_debug_setting(self) -> int | None:
        """讀取雷電「ADB 偵錯」開關：0=關閉、1=本地連線、2=遠端連線。"""
        path = self.instance_config_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = data.get("basicSettings.adbDebug")
        if isinstance(value, bool):
            return ADB_DEBUG_LOCAL if value else ADB_DEBUG_OFF
        if isinstance(value, int):
            return value
        return None

    def write_adb_debug_setting(self, value: int) -> None:
        path = self.instance_config_path()
        if not path.exists():
            raise LdPlayerError(f"找不到模擬器設定檔：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LdPlayerError(f"讀取模擬器設定檔失敗：{path}") from exc
        data["basicSettings.adbDebug"] = int(value)
        path.write_text(
            json.dumps(data, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def adb_switch_is_off(self) -> bool:
        value = self.read_adb_debug_setting()
        return value == ADB_DEBUG_OFF

    @staticmethod
    def _auto_enable_adb_enabled() -> bool:
        return os.getenv("ALIOTH_LDPLAYER_AUTO_ENABLE_ADB", "1").lower() not in (
            "0",
            "false",
            "no",
        )

    def ensure_adb_debug_enabled(self, *, reboot_if_running: bool = True) -> tuple[bool, list[str]]:
        """若雷電 ADB 開關關閉則自動改為本地連線，必要時重啟模擬器套用。"""
        logs: list[str] = []
        current = self.read_adb_debug_setting()
        logs.append(f"adb switch config basicSettings.adbDebug={current}")
        running = self.is_emulator_running()
        logs.append(f"emulator running={running}")

        if current == ADB_DEBUG_LOCAL and not (running and reboot_if_running and not self._has_adb_device()):
            logs.append("adb switch already open in config")
            return False, logs

        if current == ADB_DEBUG_LOCAL and running and reboot_if_running and not self._has_adb_device():
            logs.append("adb switch open in config but no adb device; reboot to restore")
        elif current is None:
            logs.append("adb switch config missing; set basicSettings.adbDebug=1")
        elif current == ADB_DEBUG_OFF:
            logs.append(f"adb switch is off (basicSettings.adbDebug={current}); enabling local adb")
        else:
            logs.append(f"adb switch value={current}; normalize to local adb (1)")

        if current != ADB_DEBUG_LOCAL:
            self.write_adb_debug_setting(ADB_DEBUG_LOCAL)
        if running:
            self._run_ldconsole(
                [
                    "setprop",
                    "--index",
                    str(self._index),
                    "--key",
                    "basicSettings.adbDebug",
                    "--value",
                    str(ADB_DEBUG_LOCAL),
                ],
                timeout_seconds=30.0,
            )
            logs.append("wrote basicSettings.adbDebug=1 and applied setprop")
        else:
            logs.append("wrote basicSettings.adbDebug=1; emulator is stopped, defer setprop")

        if reboot_if_running and running:
            logs.append("reboot emulator to apply adb switch")
            r = self._run_ldconsole(["reboot", "--index", str(self._index)], timeout_seconds=180.0)
            if r.returncode != 0:
                raise LdPlayerError(
                    f"重啟模擬器套用 ADB 開關失敗：{(r.stdout or '')}\n{(r.stderr or '')}".strip()
                )
            time.sleep(10.0)

        return True, logs

    def recover_adb_if_needed(self) -> tuple[bool, list[str]]:
        """ADB 無 device 時嘗試恢復：先開開關，必要時重啟模擬器。"""
        if self.adb_switch_is_off():
            return self.ensure_adb_debug_enabled(reboot_if_running=True)
        if not self.is_emulator_running():
            return False, ["emulator not running; skip adb recovery"]

        logs = ["adb switch config open but device missing; reboot emulator to restore adb"]
        self._run_ldconsole(
            [
                "setprop",
                "--index",
                str(self._index),
                "--key",
                "basicSettings.adbDebug",
                "--value",
                str(ADB_DEBUG_LOCAL),
            ],
            timeout_seconds=30.0,
        )
        r = self._run_ldconsole(["reboot", "--index", str(self._index)], timeout_seconds=180.0)
        if r.returncode != 0:
            raise LdPlayerError(
                f"重啟模擬器恢復 ADB 失敗：{(r.stdout or '')}\n{(r.stderr or '')}".strip()
            )
        time.sleep(8.0)
        return True, logs

    def adb(self, command: str, *, timeout_seconds: float = 120.0) -> subprocess.CompletedProcess:
        """透過雷電內建 adb 執行指令（例：install -r \"path.apk\"）。"""
        return self._run_ldconsole(
            ["adb", "--index", str(self._index), "--command", command],
            timeout_seconds=timeout_seconds,
        )

    def _require_adb_ready(self, *, context: str) -> str:
        """確認 ADB 已連上模擬器；否則拋錯，避免把 error 字串當 serial。"""
        serial = self.adb_serial()
        return serial

    def list_adb_devices(self, *, timeout_seconds: float = 30.0) -> list[tuple[str, str]]:
        """列出 adb devices 狀態（serial, state）。"""
        r = self.adb("devices", timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        if adb_output_has_device_error(combined) and "device" not in combined:
            return []
        devices: list[tuple[str, str]] = []
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and not parts[0].startswith("*"):
                serial = normalize_adb_serial(parts[0])
                if serial:
                    devices.append((serial, parts[1]))
        return devices

    def wait_for_adb_device(
        self,
        *,
        timeout_seconds: float = 90.0,
        log: Callable[[str], None] | None = None,
    ) -> str:
        """等待本 index 模擬器 ADB 上線且 boot_completed=1。"""
        deadline = time.time() + max(10.0, timeout_seconds)
        started_at = time.time()
        boot_grace = float(os.getenv("ALIOTH_LDPLAYER_ADB_BOOT_GRACE_SECONDS", "30"))
        last_hint = ""
        recoveries_done = 0
        max_recoveries = max(0, int(os.getenv("ALIOTH_LDPLAYER_ADB_RECOVER_MAX", "2")))
        was_running_once = False
        last_progress_log = 0.0
        while time.time() < deadline:
            running = self.is_emulator_running()
            if running:
                was_running_once = True
            elif was_running_once:
                raise LdPlayerError(
                    f"模擬器 index={self._index} 啟動後異常退出（進程已停止）；"
                    "請在雷電多開器手動啟動該實例並確認無閃退"
                )

            if log and time.time() - last_progress_log >= 15.0:
                log(
                    f"waiting adb index={self._index} running={running} "
                    f"hint={last_hint or 'booting'}"
                )
                last_progress_log = time.time()

            serial: str | None = None
            try:
                serial = self.adb_serial(timeout_seconds=15.0)
            except LdPlayerError as exc:
                last_hint = str(exc)
                serial = None

            if serial:
                boot = self.adb("shell getprop sys.boot_completed", timeout_seconds=15.0)
                boot_out = (boot.stdout or "").strip()
                boot_combined = "\n".join(part for part in (boot.stdout, boot.stderr) if part)
                if adb_output_has_device_error(boot_combined):
                    last_hint = boot_combined.strip()
                elif boot_out == "1":
                    return serial
                else:
                    last_hint = f"serial={serial} boot_completed={boot_out or '(empty)'}"
            else:
                past_grace = (time.time() - started_at) >= boot_grace
                if (
                    past_grace
                    and self._auto_enable_adb_enabled()
                    and recoveries_done < max_recoveries
                    and self.is_emulator_running()
                ):
                    _, rec_logs = self.recover_adb_if_needed()
                    recoveries_done += 1
                    last_hint = "; ".join(rec_logs) or "attempted adb switch recovery"
                    time.sleep(10.0)
                    continue
            time.sleep(2.0)
        raise LdPlayerError(
            f"等待 ADB 裝置就緒逾時（index={self._index}, {timeout_seconds}s）：{last_hint}"
        )

    def list_user_installed_packages(self, *, timeout_seconds: float = 30.0) -> list[str]:
        """列出模擬器內第三方（用戶）已安裝包名。"""
        r = self.adb("shell pm list packages -3", timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        if adb_output_has_device_error(combined):
            raise LdPlayerError(f"ADB 裝置未就緒，無法列出包名：{combined.strip()}")
        return parse_pm_list_packages_output(r.stdout or "")

    def get_package_application_label(self, package_name: str, *, timeout_seconds: float = 90.0) -> str | None:
        """讀取已安裝 App 的顯示名稱（application-label），用於辨識換包名同 App。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return None
        apk_path = self._resolve_installed_apk_path(pkg, timeout_seconds=timeout_seconds)
        if not apk_path:
            return None
        import tempfile

        with tempfile.TemporaryDirectory(prefix="alioth-apk-") as td:
            local = Path(td) / "base.apk"
            pull_cmd = f'pull "{apk_path}" "{local}"'
            self.adb(pull_cmd, timeout_seconds=timeout_seconds)
            if not local.is_file():
                return None
            badging = _run_aapt_dump_badging(local, ldplayer_home=self._paths.home)
            if badging:
                return parse_application_label_from_aapt_badging(badging)
        return None

    def _resolve_installed_apk_path(self, package_name: str, *, timeout_seconds: float = 30.0) -> str | None:
        """取得已安裝 App 的 base.apk 裝置路徑。"""
        r = self.adb(f"shell pm path {package_name}", timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        if adb_output_has_device_error(combined):
            raise LdPlayerError(f"ADB 裝置未就緒，無法讀取包路徑：{combined.strip()}")
        return parse_pm_path_apk_output(r.stdout or "")

    def is_package_installed(self, package_name: str, *, timeout_seconds: float = 30.0) -> bool:
        """檢查模擬器內是否已安裝指定包名。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return False
        r = self.adb(f"shell pm list packages {pkg}", timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        if adb_output_has_device_error(combined):
            raise LdPlayerError(f"ADB 裝置未就緒，無法檢查包名：{combined.strip()}")
        out = (r.stdout or "").strip()
        # pm list packages 會回傳 package:xxx；比對整段避免前綴誤判
        for line in out.splitlines():
            line = line.strip()
            if line == f"package:{pkg}" or line.endswith(f":{pkg}"):
                return True
        return pkg in out and f"package:{pkg}" in out.replace(" ", "")

    def ensure_apk_installed(self, apk_path: Path, *, timeout_seconds: float = 180.0) -> tuple[str, str | None, bool]:
        """依包名與版本決定是否安裝 APK。

        - 未安裝：安裝
        - 已安裝且上傳版本 <= 模擬器版本：跳過
        - 已安裝且上傳版本 > 模擬器版本：覆蓋安裝

        回傳 (log, package_name, installed_now)。
        """
        if not apk_path.exists():
            raise LdPlayerError(f"APK 不存在：{apk_path}")

        self._require_adb_ready(context="ensure_apk_installed")

        pkg = try_extract_package_name_from_aapt(apk_path, ldplayer_home=self._paths.home)
        apk_version = try_extract_apk_version_from_aapt(apk_path, ldplayer_home=self._paths.home)
        apk_label = try_extract_application_label_from_aapt(apk_path, ldplayer_home=self._paths.home)
        logs: list[str] = []
        if not pkg:
            logs.append("無法解析 package name，改直接安裝 APK")

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
        logs.extend(plan_logs)
        if not should_install:
            return "\n".join(logs), pkg, False

        if pkg:
            logs.append(f"will install apk: {pkg}")
        install_log = self.install_apk(apk_path, timeout_seconds=timeout_seconds)
        if install_log:
            logs.append(install_log)
        return "\n".join(logs), pkg, True

    def get_installed_app_version(self, package_name: str, *, timeout_seconds: float = 30.0) -> AppVersionInfo:
        """讀取模擬器內已安裝 App 的 versionCode / versionName。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return AppVersionInfo()
        r = self.adb(f"shell dumpsys package {pkg}", timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        return parse_version_from_dumpsys_package(combined)

    def install_apk(self, apk_path: Path, *, timeout_seconds: float = 180.0) -> str:
        """安裝 APK 到指定 index 的模擬器，回傳安裝摘要日誌。"""
        if not apk_path.exists():
            raise LdPlayerError(f"APK 不存在：{apk_path}")

        self._require_adb_ready(context="install_apk")

        apk_str = str(apk_path.resolve())
        logs: list[str] = []
        pkg = try_extract_package_name_from_aapt(apk_path, ldplayer_home=self._paths.home)

        # LDPlayer9 的 installapp 常回傳 0 但未實際安裝，優先使用 adb install。
        adb_cmd = f'install -r "{apk_str}"'
        logs.append(f"adb {adb_cmd}")
        r = self.adb(adb_cmd, timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part).strip()
        if combined:
            logs.append(combined)
        if adb_output_has_device_error(combined):
            raise LdPlayerError("ADB 裝置未就緒，無法安裝 APK：\n" + "\n".join(logs))
        if r.returncode == 0 and "Success" in combined:
            if pkg:
                verify = self.adb(f"shell pm list packages {pkg}", timeout_seconds=30)
                verify_out = (verify.stdout or "").strip()
                logs.append(f"verify package: {verify_out or '(empty)'}")
                if not self.is_package_installed(pkg):
                    raise LdPlayerError("adb install 回報成功，但模擬器內找不到包名：" + pkg)
            return "\n".join(logs)

        logs.append(f"adb install failed (exit={r.returncode}), fallback installapp")
        r2 = self._run_ldconsole(
            ["installapp", "--index", str(self._index), "--filename", apk_str],
            timeout_seconds=timeout_seconds,
        )
        combined2 = "\n".join(part for part in (r2.stdout, r2.stderr) if part).strip()
        if combined2:
            logs.append(combined2)
        if r2.returncode != 0:
            raise LdPlayerError(
                "安裝 APK 失敗（adb install 與 installapp 皆未成功）：\n" + "\n".join(logs)
            )
        if pkg and not self.is_package_installed(pkg):
            raise LdPlayerError(
                "installapp 回報成功，但模擬器內找不到包名：" + pkg + "\n" + "\n".join(logs)
            )
        return "\n".join(logs)

    def kill_app(self, package_name: str, *, timeout_seconds: float = 60.0) -> None:
        """強制停止指定包名 App（回到乾淨啟動態）。"""
        pkg = (package_name or "").strip()
        if not pkg:
            raise LdPlayerError("package_name 不可為空")
        r = self._run_ldconsole(
            ["killapp", "--index", str(self._index), "--packagename", pkg],
            timeout_seconds=timeout_seconds,
        )
        # 部分版本 killapp 失敗時補 adb force-stop
        if r.returncode != 0:
            self.adb(f"shell am force-stop {pkg}", timeout_seconds=30.0)
        else:
            # 再保險一次
            self.adb(f"shell am force-stop {pkg}", timeout_seconds=30.0)

    def press_home(self, *, timeout_seconds: float = 15.0) -> None:
        """送 KEYCODE_HOME，避免殘留在前一次廣告/Chrome 畫面。"""
        self.adb("shell input keyevent 3", timeout_seconds=timeout_seconds)

    def align_display_for_script(self) -> list[str]:
        """可選調整顯示；預設保留各機原生解析度，以便跨解析度截圖比對。

        - `ALIOTH_LDPLAYER_WM_SIZE`：預設 `keep`（不改）。若需強制對齊腳本模板可設 `1080x1920`。
        - `ALIOTH_LDPLAYER_WM_DENSITY`：僅在有設 WM_SIZE 時預設 `320`；可 `keep` 關閉。
        - `ALIOTH_FORCE_PORTRAIT`：默认开。横屏 App 在启动时压回直屏；设 `0` 才关闭。
        """
        logs: list[str] = []
        size = (os.getenv("ALIOTH_LDPLAYER_WM_SIZE") or "keep").strip()
        if size.lower() not in ("0", "off", "keep", "-", ""):
            self.adb(f"shell wm size {size}", timeout_seconds=15.0)
            logs.append(f"wm size {size}")
            density = (os.getenv("ALIOTH_LDPLAYER_WM_DENSITY") or "320").strip()
            if density.lower() not in ("0", "off", "keep", "-", ""):
                self.adb(f"shell wm density {density}", timeout_seconds=15.0)
                logs.append(f"wm density {density}")
        else:
            logs.append("wm size keep (native resolution for multi-res compare)")

        # 默认跟随全局直屏锁，不再只靠可选的弱 settings。
        from services.api.app.infrastructure.device.adb_ops import (
            apply_force_portrait_lock,
            force_portrait_lock_enabled,
        )

        legacy = (os.getenv("ALIOTH_LDPLAYER_PORTRAIT_LOCK") or "").strip().lower()
        if force_portrait_lock_enabled() or legacy in ("1", "true", "yes", "on"):

            def _shell(args: list[str], timeout: float) -> None:
                result = self.adb(
                    "shell " + " ".join(args), timeout_seconds=timeout
                )
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout or "adb failed")[:200])

            def _shell_output(args: list[str], timeout: float) -> str:
                result = self.adb(
                    "shell " + " ".join(args), timeout_seconds=timeout
                )
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout or "adb failed")[:200])
                return str(result.stdout or "")

            logs.extend(
                apply_force_portrait_lock(shell=_shell, shell_output=_shell_output)
            )
        return logs

    def prepare_app_for_script(
        self,
        package_name: str,
        *,
        settle_seconds: float = 5.0,
        clear_app_data: bool = False,
    ) -> str:
        """以 am start／monkey 啟動 App，並跳過桌面圖示點擊（與 google_emulator 路徑一致）。

        桌面圖示模板在換皮／多解析度下極易 TargetNotFound；改由 worker 直接 package 啟動，
        Airtest 物化時會改寫掉 untitled 入口 touch。
        clear_app_data：清資料以重現首次彈層（如神器甄选），預設關。
        """
        pkg = (package_name or "").strip()
        if not pkg:
            raise LdPlayerError("package_name 不可為空")
        logs: list[str] = []

        # 可選 wm size／直屏鎖（預設 keep，保留原生解析度做比對）
        logs.extend(self.align_display_for_script())

        self.kill_app(pkg)
        logs.append(f"force-stop {pkg}")
        time.sleep(1.0)

        want_clear = clear_app_data or (
            (os.getenv("ALIOTH_MOBILE_DEMO_CLEAR_APP_DATA") or "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if want_clear:
            clr = self.adb(f"shell pm clear {pkg}", timeout_seconds=60.0)
            logs.append(
                f"pm clear {pkg} exit={clr.returncode} "
                f"out={(clr.stdout or '').strip()[:120]}"
            )
            time.sleep(1.0)

        component = ""
        resolve = self.adb(
            f"shell cmd package resolve-activity --brief {pkg}",
            timeout_seconds=20.0,
        )
        for line in (resolve.stdout or "").splitlines():
            s = line.strip()
            if "/" in s and not s.startswith("No activity"):
                component = s
                break
        if component:
            logs.append(f"launcher component={component}")
            launch = self.adb(f"shell am start -n {component}", timeout_seconds=30.0)
            launch_mode = "am start"
        else:
            logs.append("WARN: resolve-activity 失敗，改用 monkey")
            launch = self.adb(
                f"shell monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
                timeout_seconds=30.0,
            )
            launch_mode = "monkey"
        launch_out = "\n".join(p for p in (launch.stdout, launch.stderr) if p).strip()
        if launch_out:
            logs.append(launch_out[:500])
        logs.append(f"{launch_mode} {pkg}")

        wait_s = max(2.0, float(settle_seconds))
        logs.append(f"wait settle {wait_s}s after app launch")
        time.sleep(wait_s)

        focus = self.adb("shell dumpsys activity activities", timeout_seconds=20.0)
        focus_out = ""
        for line in (focus.stdout or "").splitlines():
            if "mResumedActivity" in line or "topResumedActivity" in line or "ResumedActivity" in line:
                focus_out = line.strip()
                if pkg in focus_out:
                    break
        if not focus_out:
            focus2 = self.adb("shell dumpsys window displays", timeout_seconds=15.0)
            for line in (focus2.stdout or "").splitlines():
                if "mCurrentFocus" in line or "mFocusedApp" in line:
                    focus_out = line.strip()
                    break
        if focus_out:
            logs.append(f"current focus: {focus_out[:240]}")
        logs.append("app launched by package; untitled entry icon tap will be skipped")
        return "\n".join(logs)

    def screencap_to_file(self, dest: Path, *, timeout_seconds: float = 30.0) -> None:
        """用 adb screencap 拍一張目前畫面，供除錯入口頁是否正確。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote = "/sdcard/alioth_demo_pre.png"
        self.adb(f"shell screencap -p {remote}", timeout_seconds=timeout_seconds)
        # ldconsole pull: pull --index N --remote X --local Y
        r = self._run_ldconsole(
            ["pull", "--index", str(self._index), "--remote", remote, "--local", str(dest)],
            timeout_seconds=timeout_seconds,
        )
        if r.returncode != 0 or not dest.exists():
            # fallback: adb exec-out 不穩定；改用 adb pull 命令
            self.adb(f'pull {remote} "{dest}"', timeout_seconds=timeout_seconds)
        if not dest.exists():
            raise LdPlayerError(f"screencap 失敗：{dest}")

    def run_app(self, package_name: str, *, timeout_seconds: float = 60.0) -> None:
        """啟動指定包名（若能取得包名時使用）。"""
        pkg = (package_name or "").strip()
        if not pkg:
            raise LdPlayerError("package_name 不可為空")
        r = self._run_ldconsole(
            ["runapp", "--index", str(self._index), "--packagename", pkg],
            timeout_seconds=timeout_seconds,
        )
        if r.returncode != 0:
            raise LdPlayerError(f"啟動 App 失敗：{r.stdout}\n{r.stderr}".strip())

    def adb_serial(self, *, timeout_seconds: float = 30.0) -> str:
        """取得本 index 模擬器 ADB serial（供 Airtest --device）。

        只用 ldconsole `adb --index N get-serialno`；禁止回退 `adb devices` 第一台
        （多開時會把 Airtest 綁錯實例）。
        """
        r = self.adb("get-serialno", timeout_seconds=timeout_seconds)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part)
        if adb_output_has_device_error(combined):
            raise LdPlayerError(
                f"ADB 未就緒，無法取得 index={self._index} serial：{combined.strip()}"
            )
        for line in combined.splitlines():
            serial = normalize_adb_serial(line)
            if serial and serial.lower() != "unknown":
                return serial
        raise LdPlayerError(
            f"無法取得有效 ADB serial（index={self._index}）：{combined.strip()}"
        )

    def airtest_device_uri(self, serial: str | None = None) -> str:
        """組成 Airtest 裝置 URI。可傳入已鎖定的 serial，避免多機並行綁錯。"""
        # 僅在未指定 serial 時允許全域覆寫（單機除錯）；並行多機必須傳 serial
        configured = (os.getenv("ALIOTH_MOBILE_DEMO_AIRTEST_DEVICE") or "").strip()
        if configured and not serial:
            if configured.startswith("Android:///"):
                serial_cfg = configured[len("Android:///") :]
            else:
                serial_cfg = configured
            valid = normalize_adb_serial(serial_cfg)
            if not valid:
                raise LdPlayerError(f"ALIOTH_MOBILE_DEMO_AIRTEST_DEVICE 無效：{configured}")
            return f"Android:///{valid}"
        resolved = (serial or "").strip() or self.adb_serial()
        valid = normalize_adb_serial(resolved)
        if not valid:
            raise LdPlayerError(f"無效 ADB serial：{resolved}")
        return f"Android:///{valid}"


def try_extract_package_name_from_apk_manifest(apk_path: Path) -> str | None:
    """不依賴 aapt：從 APK 內二進位 AndroidManifest.xml 掃出 package name。"""
    import zipfile

    # 優先結構解析（UTF-8／UTF-16 字串池都可）；避免僅掃 UTF-16 漏包名。
    attributes = _read_axml_manifest_attributes(apk_path) or {}
    package = str(attributes.get("package") or "").strip()
    if package and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
        return package

    # 其次完整 AXML 解析（避免把 OEM／設備 ID 服務誤判為 App 包名）
    try:
        from pyaxmlparser import APK as _PyAxmlApk

        parsed = _PyAxmlApk(str(apk_path))
        package = str(getattr(parsed, "package", "") or "").strip()
        if package and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
            return package
    except Exception:
        pass

    try:
        with zipfile.ZipFile(apk_path) as zf:
            raw = zf.read("AndroidManifest.xml")
    except Exception:
        return None

    # binary XML 內常見 UTF-16LE 字串
    parts = re.findall(rb"(?:[\x20-\x7e]\x00){3,}", raw)
    candidates: list[str] = []
    deny_prefixes = (
        "android.",
        "androidx.",
        "com.google.android.",
        "com.android.",
        "kotlin.",
        "java.",
        "javax.",
        "org.apache.",
        "okhttp3.",
        "okio.",
        "retrofit2.",
        # OEM／設備標識與推送中介，常見於 Manifest 但不是 App package
        "com.qiku.",
        "com.heytap.",
        "com.huawei.",
        "com.asus.",
        "com.coolpad.",
        "com.mdid.",
        "com.zui.",
        "com.samsung.",
        "com.meizu.",
        "com.vivo.",
        "com.xiaomi.",
        "freemme.",
    )
    deny_keywords = (
        "permission",
        "intent",
        "activity",
        "service",
        "receiver",
        "provider",
        "schema",
        "http",
        "layout",
        "style",
        "theme",
        "drawable",
        "profileinstaller",
        "metadata",
        "startup",
        "extensions",
        "sidecar",
        "fileprovider",
        "action.",
        "openid",
        "deviceid",
        "msa",
        "oaid",
        "aaid",
        "supplementarydid",
        "datatransport",
        "flutter_inappwebview",
        "gms.common",
    )
    for part in parts:
        try:
            s = part.decode("utf-16le").strip("\x00").strip()
        except Exception:
            continue
        if not s or "." not in s:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", s):
            continue
        low = s.lower()
        if any(low.startswith(p) for p in deny_prefixes):
            continue
        if any(k in low for k in deny_keywords):
            continue
        # 排除純版本號之類
        if re.fullmatch(r"[\d.]+", s):
            continue
        if s not in candidates:
            candidates.append(s)

    if not candidates:
        return None
    # 偏好較「像 App 包名」的短名稱（段數 2~4）；同段數時略偏好較長、更少 example 前綴
    scored = sorted(
        candidates,
        key=lambda x: (
            0 if 2 <= x.count(".") <= 3 else 1,
            0 if not x.lower().startswith("com.example.") else 1,
            -len(x),
            x,
        ),
    )
    return scored[0]


def _resolve_aapt_candidates(*, ldplayer_home: Path | None = None) -> list[str]:
    candidates: list[str] = []
    env_aapt = os.getenv("ALIOTH_AAPT_PATH", "").strip()
    if env_aapt:
        candidates.append(env_aapt)
    if ldplayer_home:
        for rel in ("aapt.exe", "aapt/aapt.exe", "tool/aapt.exe"):
            candidates.append(str(ldplayer_home / rel))
    for root_env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.getenv(root_env, "").strip()
        if not root:
            continue
        bt = Path(root) / "build-tools"
        if bt.exists():
            for aapt in sorted(bt.glob("*/aapt.exe"), reverse=True):
                candidates.append(str(aapt))
            for aapt in sorted(bt.glob("*/aapt"), reverse=True):
                candidates.append(str(aapt))
    local_bt = Path(os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\build-tools"))
    if local_bt.exists():
        for aapt in sorted(local_bt.glob("*/aapt.exe"), reverse=True):
            candidates.append(str(aapt))
        for aapt in sorted(local_bt.glob("*/aapt"), reverse=True):
            candidates.append(str(aapt))
    # Linux 常見路徑
    home_bt = Path.home() / "Android" / "Sdk" / "build-tools"
    if home_bt.exists():
        for aapt in sorted(home_bt.glob("*/aapt"), reverse=True):
            candidates.append(str(aapt))
    candidates.append("aapt")
    return candidates


def _run_aapt_dump_badging(
    apk_path: Path,
    *,
    ldplayer_home: Path | None = None,
    allow_label_only_result: bool = False,
) -> str | None:
    """执行 badging；轻量应用名读取可保留图标解析报错前已解析的名称。"""
    for aapt in _resolve_aapt_candidates(ldplayer_home=ldplayer_home):
        if aapt != "aapt" and not Path(aapt).exists():
            continue
        try:
            r = subprocess.run(
                [aapt, "dump", "badging", str(apk_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                check=False,
            )
        except Exception:
            continue
        if r.returncode == 0 and (r.stdout or "").strip():
            return r.stdout
        if allow_label_only_result and parse_application_label_from_aapt_badging(r.stdout or ""):
            # 元数据包不含图标；名称已由 aapt 解析出来，不需为后续图标错误拉取整包。
            return r.stdout
    return None


def parse_pm_list_packages_output(output: str) -> list[str]:
    """解析 `pm list packages` 輸出為包名列表。"""
    packages: list[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkg = line.split(":", 1)[1].strip()
            if pkg:
                packages.append(pkg)
    return packages


def parse_pm_path_apk_output(output: str) -> str | None:
    """解析 `pm path` 輸出，取第一個 base.apk 路徑。"""
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            path = line.split(":", 1)[1].strip()
            if path.endswith(".apk"):
                return path
    return None


def parse_application_label_from_aapt_badging(badging: str) -> str | None:
    """從 aapt dump badging 取預設 application-label。"""
    text = badging or ""
    m = re.search(r"application-label:'([^']*)'", text)
    if not m:
        return None
    label = m.group(1).strip()
    return label or None


def labels_match(left: str | None, right: str | None) -> bool:
    """比對兩個 App 顯示名稱是否相同（忽略首尾空白）。"""
    a = (left or "").strip()
    b = (right or "").strip()
    return bool(a) and a == b


def parse_version_from_aapt_badging(badging: str) -> AppVersionInfo:
    """從 aapt dump badging 輸出解析版本。"""
    text = badging or ""
    version_code: int | None = None
    version_name: str | None = None
    m_code = re.search(r"versionCode='(\d+)'", text)
    if m_code:
        version_code = int(m_code.group(1))
    m_name = re.search(r"versionName='([^']*)'", text)
    if m_name:
        name = m_name.group(1).strip()
        version_name = name or None
    return AppVersionInfo(version_code=version_code, version_name=version_name)


def parse_version_from_dumpsys_package(output: str) -> AppVersionInfo:
    """從 dumpsys package 輸出解析 versionCode / versionName。"""
    text = output or ""
    version_code: int | None = None
    version_name: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m_code = re.search(r"versionCode=(\d+)", stripped)
        if m_code and version_code is None:
            version_code = int(m_code.group(1))
        m_name = re.search(r"versionName=([^\s]+)", stripped)
        if m_name and version_name is None:
            version_name = m_name.group(1).strip() or None
    return AppVersionInfo(version_code=version_code, version_name=version_name)


def compare_version_names(left: str, right: str) -> int:
    """比較 versionName；回傳 1/0/-1 表示 left 新於/等於/舊於 right。"""

    def _parts(name: str) -> list[int]:
        chunks: list[int] = []
        for seg in re.split(r"[.\-_]", name.strip()):
            m = re.match(r"(\d+)", seg)
            if m:
                chunks.append(int(m.group(1)))
        return chunks or [0]

    pa, pb = _parts(left), _parts(right)
    width = max(len(pa), len(pb))
    pa.extend([0] * (width - len(pa)))
    pb.extend([0] * (width - len(pb)))
    for a, b in zip(pa, pb, strict=True):
        if a > b:
            return 1
        if a < b:
            return -1
    return 0


def compare_app_versions(uploaded: AppVersionInfo, installed: AppVersionInfo) -> int | None:
    """比較上傳 APK 與已安裝版本。

    回傳 1=上傳較新、0=相同、-1=上傳較舊、None=無法比較。
    優先使用 versionCode，其次 versionName。
    """
    if uploaded.version_code is not None and installed.version_code is not None:
        if uploaded.version_code > installed.version_code:
            return 1
        if uploaded.version_code < installed.version_code:
            return -1
        return 0
    if uploaded.version_name and installed.version_name:
        return compare_version_names(uploaded.version_name, installed.version_name)
    return None


def should_install_apk_update(uploaded: AppVersionInfo, installed: AppVersionInfo) -> tuple[bool, str]:
    """判斷已安裝同包名時，是否需要覆蓋安裝。"""
    cmp = compare_app_versions(uploaded, installed)
    if cmp is None:
        return (
            False,
            "無法比較版本（缺少 versionCode/versionName），已安裝同包名則跳過安裝",
        )
    if cmp > 0:
        return True, f"上傳版本較新，需要更新安裝（apk: {uploaded.label()}；installed: {installed.label()}）"
    if cmp == 0:
        return False, f"上傳版本與已安裝相同，跳過安裝（{uploaded.label()}）"
    return False, f"上傳版本較舊，跳過安裝（apk: {uploaded.label()}；installed: {installed.label()}）"


def evaluate_apk_install_plan(
    *,
    apk_pkg: str | None,
    apk_version: AppVersionInfo,
    apk_label: str | None,
    is_package_installed: Callable[[str], bool],
    get_installed_version: Callable[[str], AppVersionInfo],
    list_user_packages: Callable[[], list[str]],
    get_package_label: Callable[[str], str | None],
) -> tuple[bool, list[str]]:
    """依包名、版本與 App 顯示名稱決定是否允許安裝。

    - 同包名或同 application-label 且上傳版本較舊：拋 LdPlayerError 拒絕降版
    - 同包名且版本相同或較舊（已處理）：跳過安裝
    - 同包名且上傳較新：允許安裝
    """
    logs: list[str] = []
    if apk_pkg:
        logs.append(f"apk package={apk_pkg}")
    logs.append(f"apk version: {apk_version.label()}")
    if apk_label:
        logs.append(f"apk label: {apk_label}")

    conflict_targets: dict[str, AppVersionInfo] = {}
    if apk_pkg and is_package_installed(apk_pkg):
        conflict_targets[apk_pkg] = get_installed_version(apk_pkg)

    if apk_pkg and apk_pkg in conflict_targets:
        same_package_version = compare_app_versions(apk_version, conflict_targets[apk_pkg])
        if same_package_version == 0:
            logs.append(f"上傳版本與已安裝相同，跳過安裝（{apk_version.label()}）")
            return False, logs

    if apk_label:
        for other_pkg in list_user_packages():
            if apk_pkg and other_pkg == apk_pkg:
                continue
            other_label = get_package_label(other_pkg)
            if labels_match(apk_label, other_label):
                conflict_targets[other_pkg] = get_installed_version(other_pkg)
                logs.append(f"same app label detected: {other_pkg} (label={other_label})")

    for inst_pkg, installed_version in conflict_targets.items():
        logs.append(f"installed version ({inst_pkg}): {installed_version.label()}")
        cmp = compare_app_versions(apk_version, installed_version)
        if cmp is not None and cmp < 0:
            raise LdPlayerError(
                "拒絕安裝：上傳 APK 版本較舊（"
                f"{apk_version.label()}），模擬器已存在較新版本（"
                f"{inst_pkg} / {installed_version.label()}）"
            )
        if cmp is None and inst_pkg != apk_pkg:
            raise LdPlayerError(
                "拒絕安裝：無法比較版本，但模擬器已有同名 App（"
                f"{inst_pkg} / {installed_version.label()}）"
            )
        if cmp == 0 and inst_pkg == apk_pkg:
            logs.append(f"上傳版本與已安裝相同，跳過安裝（{apk_version.label()}）")
            return False, logs

    if apk_pkg and is_package_installed(apk_pkg):
        need_install, reason = should_install_apk_update(apk_version, get_installed_version(apk_pkg))
        logs.append(reason)
        if not need_install:
            return False, logs

    return True, logs


def try_extract_application_label_from_aapt(apk_path: Path, *, ldplayer_home: Path | None = None) -> str | None:
    """從 APK 解析預設 application-label。"""
    badging = _run_aapt_dump_badging(apk_path, ldplayer_home=ldplayer_home)
    if badging:
        return parse_application_label_from_aapt_badging(badging)
    return None


def _parse_axml_manifest_attributes(raw: bytes) -> dict[str, str] | None:
    """解析 Android 二進位 XML，取 manifest 標籤的 package／versionCode／versionName。

    字串池同時支援 UTF-8 與 UTF-16LE；解析失敗回傳 None。
    """

    import struct

    if len(raw) < 8 or raw[0:2] != b"\x03\x00":
        return None
    pos = 8
    strings: list[str] = []
    while pos + 8 <= len(raw):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", raw, pos)
        if chunk_size < 8 or pos + chunk_size > len(raw):
            return None
        if chunk_type == 0x0001:  # RES_STRING_POOL_TYPE
            count, _style_count, flags, strings_start, _styles_start = struct.unpack_from(
                "<IIIII", raw, pos + 8
            )
            is_utf8 = bool(flags & (1 << 8))
            offsets_at = pos + header_size
            data_at = pos + strings_start
            for index in range(count):
                off_pos = offsets_at + index * 4
                if off_pos + 4 > len(raw):
                    break
                (offset,) = struct.unpack_from("<I", raw, off_pos)
                at = data_at + offset
                if at >= len(raw):
                    break
                try:
                    if is_utf8:
                        cursor = at
                        for _ in range(2):
                            length = raw[cursor]
                            cursor += 1
                            if length & 0x80:
                                length = ((length & 0x7F) << 8) | raw[cursor]
                                cursor += 1
                        strings.append(
                            raw[cursor : cursor + length].decode("utf-8", "replace")
                        )
                    else:
                        (length,) = struct.unpack_from("<H", raw, at)
                        if length & 0x8000:
                            (extra,) = struct.unpack_from("<H", raw, at + 2)
                            length = ((length & 0x7FFF) << 16) | extra
                            body = at + 4
                        else:
                            body = at + 2
                        strings.append(
                            raw[body : body + length * 2].decode("utf-16le", "replace")
                        )
                except Exception:  # noqa: BLE001
                    strings.append("")
        elif chunk_type == 0x0102:  # RES_XML_START_ELEMENT_TYPE
            body = pos + header_size
            if body + 20 > len(raw):
                return None
            _ns, name_idx, attr_start, _attr_size, attr_count = struct.unpack_from(
                "<IIHHH", raw, body
            )
            if 0 <= name_idx < len(strings) and strings[name_idx] == "manifest":
                attrs_at = body + attr_start
                found: dict[str, str] = {}
                for index in range(attr_count):
                    at = attrs_at + index * 20
                    if at + 20 > len(raw):
                        break
                    _a_ns, a_name, a_raw_value, size_type, a_data = struct.unpack_from(
                        "<IIIIi", raw, at
                    )
                    if not (0 <= a_name < len(strings)):
                        continue
                    key = strings[a_name]
                    if key not in {"package", "versionCode", "versionName"}:
                        continue
                    value = ""
                    for candidate in (a_raw_value, a_data):
                        if 0 <= candidate < len(strings):
                            value = strings[candidate].strip()
                            if value:
                                break
                    if not value and (size_type >> 24) == 0x10:  # TYPE_INT_DEC
                        value = str(a_data)
                    if value:
                        found.setdefault(key, value)
                return found or None
        pos += chunk_size
    return None


def _read_axml_manifest_attributes(apk_path: Path) -> dict[str, str] | None:
    """讀 APK 內二進位 AndroidManifest.xml 並解析 manifest 屬性；失敗回傳 None。"""

    import zipfile

    try:
        with zipfile.ZipFile(apk_path) as zf:
            raw = zf.read("AndroidManifest.xml")
    except Exception:
        return None
    try:
        return _parse_axml_manifest_attributes(raw)
    except Exception:  # noqa: BLE001
        return None


def try_extract_apk_version_from_manifest(apk_path: Path) -> AppVersionInfo:
    """不依賴 aapt：從二進位 AndroidManifest 取 versionCode／versionName。"""

    attributes = _read_axml_manifest_attributes(apk_path) or {}
    raw_code = attributes.get("versionCode")
    try:
        version_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        version_code = None
    version_name = attributes.get("versionName") or None
    return AppVersionInfo(version_code=version_code, version_name=version_name)


def try_extract_apk_version_from_aapt(apk_path: Path, *, ldplayer_home: Path | None = None) -> AppVersionInfo:
    """從 APK 解析 versionCode / versionName：優先 aapt，失敗則掃 AndroidManifest。"""
    badging = _run_aapt_dump_badging(apk_path, ldplayer_home=ldplayer_home)
    if badging:
        version = parse_version_from_aapt_badging(badging)
        if version.version_code is not None or version.version_name is not None:
            return version
    return try_extract_apk_version_from_manifest(apk_path)


def try_extract_package_name_from_aapt(apk_path: Path, *, ldplayer_home: Path | None = None) -> str | None:
    """嘗試解析 APK 包名：優先 aapt，失敗則掃 AndroidManifest。"""
    badging = _run_aapt_dump_badging(apk_path, ldplayer_home=ldplayer_home)
    if badging:
        m = re.search(r"package:\s+name='([^']+)'", badging)
        if m:
            return m.group(1).strip() or None

    return try_extract_package_name_from_apk_manifest(apk_path)


def wait_emulator_boot(
    *,
    seconds: float = 15.0,
    controller: LdPlayerController | None = None,
    index: int = 0,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """等待模擬器完成啟動；若提供 controller 則等待 ADB 上線且 boot_completed=1。

    回傳就緒的 adb serial；無 controller 時僅 sleep。
    """
    min_wait = max(0.0, seconds)
    if controller is None:
        time.sleep(min_wait)
        return None

    timeout = float(os.getenv("ALIOTH_LDPLAYER_ADB_WAIT_SECONDS", "90"))
    # 至少等待使用者設定的 boot 秒數
    timeout = max(timeout, min_wait + 30.0)
    return controller.wait_for_adb_device(timeout_seconds=timeout, log=log)
