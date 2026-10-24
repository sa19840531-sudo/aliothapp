"""雲手機（遠端 ADB）控制：與 ReDroid 相同協定，僅端點與文案不同。

環境變數：
- ALIOTH_CLOUD_PHONE_ADB：雲手機 ADB（如 1.2.3.4:5555）；未設則回退 ALIOTH_REDROID_ADB
- ALIOTH_CLOUD_PHONE_ADB_AUTH：可選；MoreLogin 等廠商的 ADB 連線碼（`adb shell <code>`）
- ALIOTH_MORELOGIN_CLOUDPHONE_ID：設定後開跑前自動喚醒（可關）並拉最新 ADB
- ALIOTH_MORELOGIN_API_ID／ALIOTH_MORELOGIN_API_KEY：官方 Open API；有憑證時伺服器不必安裝 MoreLogin
- ALIOTH_CLOUD_PHONE_INSTALL_TIMEOUT_SECONDS：遠端安裝逾時（預設依 APK 大小估算，至少 300）
- ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED：是否拉取其他已安装 App 比对同名降版风险（默认开）。
  设为 0 时仍须任务备注携带授权标记，否则 fail-closed 继续扫描。
- ANDROID_HOME：本機 SDK adb（Host Runner）
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from services.api.app.infrastructure.device.adb_ops import (
    cloud_phone_live_touch_indicator_enabled,
)
from services.api.app.infrastructure.device.ldplayer import (
    adb_output_has_device_error,
    normalize_adb_serial,
    try_extract_package_name_from_apk_manifest,
)
from services.api.app.infrastructure.device.morelogin_adb import (
    MoreLoginAdbError,
    refresh_cloud_phone_adb_from_morelogin,
)
from services.api.app.infrastructure.device.redroid import RedroidController, RedroidError

logger = logging.getLogger(__name__)

# 任务备注／报告须包含其一，才允许在 SAME_LABEL_SCAN_ENABLED=0 时跳过同名扫描。
SAME_LABEL_SKIP_AUTH_MARKERS: tuple[str, ...] = (
    "SAME_LABEL_SCAN_SKIP_AUTHORIZED",
    "授权跳过同名降版扫描",
    "授權跳過同名降版掃描",
)

_same_label_skip_authorization: ContextVar[str | None] = ContextVar(
    "alioth_same_label_skip_authorization",
    default=None,
)


class CloudPhoneError(RedroidError):
    """雲手機遠端 ADB 操作失敗。"""


def resolve_cloud_phone_endpoint() -> str:
    """解析雲手機 ADB 端點（隧道模式：公网 → 跳板 ``JUMP:3xxxx``）。"""
    from services.api.app.infrastructure.device.morelogin_adb import (
        rewrite_loopback_adb_endpoint,
    )
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        list_onirent_adb_tunnel_endpoints,
        map_onirent_public_adb_to_tunnel,
        onirent_adb_tunnel_enabled,
    )

    raw = (os.getenv("ALIOTH_CLOUD_PHONE_ADB") or "").strip()
    if not raw:
        raw = (os.getenv("ALIOTH_REDROID_ADB") or "").strip()
    if onirent_adb_tunnel_enabled():
        if not raw:
            endpoints = list_onirent_adb_tunnel_endpoints()
            raw = endpoints[0] if endpoints else ""
        else:
            raw = map_onirent_public_adb_to_tunnel(raw)
        return rewrite_loopback_adb_endpoint(raw) if raw else "172.104.172.119:30615"
    if raw:
        return rewrite_loopback_adb_endpoint(raw)
    return "127.0.0.1:5555"


def resolve_cloud_phone_adb_auth() -> str:
    """解析雲手機 ADB 連線碼（勿寫入日誌全文）。"""
    return (os.getenv("ALIOTH_CLOUD_PHONE_ADB_AUTH") or "").strip()


class CloudPhoneController(RedroidController):
    """雲手機：adb connect →（可選）連線碼認證 → boot 檢查 → 安裝／啟動。"""

    def __init__(
        self,
        *,
        morelogin_env_id: str | None = None,
        allow_global_morelogin_fallback: bool = True,
        apply_morelogin_to_process_env: bool | None = None,
    ) -> None:
        self._morelogin_env_id = str(morelogin_env_id or "").strip()
        self._allow_global_morelogin_fallback = bool(allow_global_morelogin_fallback)
        self._apply_morelogin_to_process_env = (
            self._allow_global_morelogin_fallback
            if apply_morelogin_to_process_env is None
            else bool(apply_morelogin_to_process_env)
        )
        self._adb_auth = resolve_cloud_phone_adb_auth()
        endpoint = resolve_cloud_phone_endpoint()
        if self._apply_morelogin_to_process_env:
            os.environ["ALIOTH_REDROID_ADB"] = endpoint
        super().__init__(bind_process_env=self._apply_morelogin_to_process_env)
        self._endpoint = endpoint

    def fresh_for_adb_refresh(self) -> CloudPhoneController:
        """用相同 MoreLogin 租户配置重建长会话控制器。

        公网 ADB 续期失败后，旧控制器内部的端点、认证码或 Emulator 绑定
        可能已经不同步。恢复只重建控制器，不更换云手机环境，也不改变进程级
        回退策略；调用方仍须限制重试次数。
        """

        return CloudPhoneController(
            morelogin_env_id=self._morelogin_env_id,
            allow_global_morelogin_fallback=self._allow_global_morelogin_fallback,
            apply_morelogin_to_process_env=self._apply_morelogin_to_process_env,
        )

    def ensure_sdk_adb_server(self) -> list[str]:
        """確保共享 SDK ADB server 可用，不中斷其他並行雲手機會話。"""
        logs: list[str] = []
        adb = str(self.adb_path)
        logs.append(f"ensure shared sdk adb server: {adb}")
        try:
            # 多台真實雲手機共用 Host Runner 的 ADB daemon，禁止在單一任務中 kill-server。
            subprocess.run(
                [adb, "start-server"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            logs.append("shared sdk adb server ready")
        except Exception as exc:
            logs.append(f"shared sdk adb server warn: {exc}")
        return logs

    def _shell_probe_ok(self, serial: str) -> bool:
        """探測 adb shell 是否已可用（未認證時常見 error: closed）。"""
        probe = self._adb(
            ["-s", serial, "shell", "echo", "alioth_ok"],
            timeout_seconds=12.0,
        )
        text = "\n".join(p for p in (probe.stdout, probe.stderr) if p)
        return "alioth_ok" in text

    def _authenticate_adb_shell(self, serial: str, logs: list[str]) -> None:
        """MoreLogin Android 12：`adb shell <連線碼>`；認證後送 exit 結束互動 shell。"""
        auth = self._adb_auth or resolve_cloud_phone_adb_auth()
        if not auth:
            logs.append("cloud_phone adb auth skipped (ALIOTH_CLOUD_PHONE_ADB_AUTH unset)")
            return
        if self._shell_probe_ok(serial):
            logs.append("cloud_phone adb shell already ok")
            return

        logs.append("cloud_phone adb shell auth with connection code…")
        cmd = [str(self.adb_path), "-s", serial, "shell", auth]
        try:
            completed = subprocess.run(
                cmd,
                input="exit\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15.0,
                check=False,
            )
            err = (completed.stderr or "").strip()
            if err and "closed" in err.lower():
                logs.append(f"cloud_phone adb auth stderr hint: {err[:120]}")
        except subprocess.TimeoutExpired:
            # 認證成功後常停在互動 shell；逾時視為連線碼已送出
            logs.append("cloud_phone adb auth timed out (treat as submitted)")

        if not self._shell_probe_ok(serial):
            raise CloudPhoneError(
                "雲手機 ADB 已連線但 shell 不可用；請確認 ALIOTH_CLOUD_PHONE_ADB_AUTH 連線碼"
            )
        logs.append("cloud_phone adb shell auth ok")

    def _refresh_endpoint_from_morelogin(self, logs: list[str]) -> None:
        """開跑前向 MoreLogin 拉最新 IP:埠／連線碼並更新本控制器端點。"""
        try:
            info, refresh_logs = refresh_cloud_phone_adb_from_morelogin(
                self._morelogin_env_id or None,
                allow_global_fallback=self._allow_global_morelogin_fallback,
                apply_to_process_env=self._apply_morelogin_to_process_env,
            )
        except MoreLoginAdbError as exc:
            raise CloudPhoneError(f"MoreLogin 自動刷新 ADB 失敗：{exc}") from exc
        logs.extend(refresh_logs)
        if info is None:
            return
        self._endpoint = info.endpoint
        self._adb_auth = info.auth
        if getattr(self, "_apply_morelogin_to_process_env", True):
            os.environ["ALIOTH_REDROID_ADB"] = info.endpoint
            os.environ["ALIOTH_ADB_SERIAL"] = info.endpoint

    def ensure_ready(self, *, timeout_seconds: float | None = None) -> tuple[str, list[str]]:
        """連線後先做連線碼認證，再檢查 boot_completed（避免未認證時 shell closed 空轉）。"""
        logs: list[str] = []
        self._refresh_endpoint_from_morelogin(logs)
        logs.append(f"cloud_phone endpoint={self._endpoint}")
        logs.append(f"adb={self.adb_path}")
        logs.extend(self.ensure_sdk_adb_server())

        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("ALIOTH_REDROID_ADB_WAIT_SECONDS", "120")
        )
        deadline = time.time() + max(30.0, timeout)
        last = "connecting"
        last_progress = 0.0
        authenticated = False
        boot_probe_reconnect_attempted = False

        while time.time() < deadline:
            conn = self._adb(["connect", self._endpoint], timeout_seconds=30.0)
            out = "\n".join(p for p in (conn.stdout, conn.stderr) if p).strip()
            if out:
                logs.append(out[:300])
            devices = self._emu.list_adb_devices()
            online = [
                s
                for s, st in devices
                if st == "device"
                and (
                    s == self._endpoint
                    or s == normalize_adb_serial(self._endpoint)
                    or self._endpoint in s
                )
            ]
            if not online:
                last = f"devices={devices or 'empty'}; want={self._endpoint}"
            else:
                serial = online[0]
                if not authenticated:
                    self._authenticate_adb_shell(serial, logs)
                    authenticated = True
                try:
                    boot = self._emu._run_adb(
                        ["shell", "getprop", "sys.boot_completed"],
                        serial=serial,
                        timeout_seconds=20.0,
                    )
                except Exception as exc:  # noqa: BLE001 - 只恢复 boot probe 传输超时
                    timeout_cause = (
                        exc
                        if isinstance(exc, subprocess.TimeoutExpired)
                        else exc.__cause__
                    )
                    if (
                        boot_probe_reconnect_attempted
                        or not isinstance(timeout_cause, subprocess.TimeoutExpired)
                    ):
                        raise
                    # 不重置 readiness deadline；剩余预算不足时保留原超时失败。
                    if time.time() >= deadline:
                        raise
                    boot_probe_reconnect_attempted = True
                    logs.append(
                        "cloud_phone boot probe timed out; reopen adb session "
                        f"endpoint={self._endpoint}"
                    )
                    self._reopen_adb_session(serial, logs)
                    continue
                if (boot.stdout or "").strip() == "1":
                    self._serial = serial
                    self._emu._serial = serial
                    if getattr(self, "_apply_morelogin_to_process_env", True):
                        os.environ["ALIOTH_ADB_SERIAL"] = serial
                    logs.append(f"adb ready: {serial}")
                    return serial, logs
                last = f"boot_completed={(boot.stdout or '').strip() or '(empty)'}"

            now = time.time()
            if now - last_progress >= 15.0:
                logs.append(f"waiting cloud_phone adb: {last}")
                last_progress = now
            time.sleep(3.0)

        raise CloudPhoneError(f"等待雲手機 ADB 就緒逾時（{timeout}s）：{last}")

    def _resolve_install_timeout(self, apk_path: Path, *, timeout_seconds: float) -> float:
        """WAN 傳輸大 APK 需更長逾時；可用環境變數覆蓋下限。"""
        try:
            size_mb = max(1.0, apk_path.stat().st_size / (1024 * 1024))
        except OSError:
            size_mb = 40.0
        # 約按 1MB/3s 估算，並設下限
        estimated = 120.0 + size_mb * 3.0
        env_raw = (os.getenv("ALIOTH_CLOUD_PHONE_INSTALL_TIMEOUT_SECONDS") or "").strip()
        try:
            env_floor = float(env_raw) if env_raw else 300.0
        except ValueError:
            env_floor = 300.0
        return max(float(timeout_seconds), estimated, env_floor)

    @staticmethod
    def _is_stream_install_fragile_error(text: str) -> bool:
        """判斷 streamed install / 連線中斷類錯誤（雲手機常見）。"""
        t = (text or "").lower()
        needles = (
            "abb_exec",
            "error: closed",
            "connect error for write",
            "connection reset",
            "device offline",
            "protocol fault",
            "broken pipe",
        )
        return any(n in t for n in needles)

    @staticmethod
    def _install_succeeded(returncode: int, text: str) -> bool:
        return returncode == 0 and "Success" in (text or "")

    def _reopen_adb_session(self, serial: str, logs: list[str]) -> str:
        """安裝前重連並必要時重做連線碼認證，降低半開連線。"""
        endpoint = self._endpoint
        logs.append(f"cloud_phone reopen adb session endpoint={endpoint}")
        self._adb(["disconnect", endpoint], timeout_seconds=15.0)
        conn = self._adb(["connect", endpoint], timeout_seconds=30.0)
        out = "\n".join(p for p in (conn.stdout, conn.stderr) if p).strip()
        if out:
            logs.append(out[:300])
        # 連線後 serial 可能仍是 host:port
        target = serial if serial else endpoint
        if not self._shell_probe_ok(target):
            self._authenticate_adb_shell(target, logs)
        self._serial = target
        self._emu._serial = target
        if getattr(self, "_apply_morelogin_to_process_env", True):
            os.environ["ALIOTH_ADB_SERIAL"] = target
        return target

    def _try_device_side_download_install(
        self,
        apk_path: Path,
        *,
        timeout: float,
        logs: list[str],
        run_fn: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> str | None:
        """雲機自 curl 下載 APK 後就地安裝，繞開遠端 ADB 傳大檔。

        瓶頸只在「把幾十 MB 推過遠端 ADB」：慢且常中途掉線（error: closed）。
        因此下載走 MoreLogin exeCommand（雲機本地網直連 CDN，實測快兩個數量級），
        安裝仍走 adb shell pm install——指令短不受傳輸影響，且 exeCommand 對 pm
        並非始終可用（雲機剛開機時會回 Command not found）。

        僅在 ``<apk>.srcurl`` sidecar 提供可信下載鏈接且具備 MoreLogin env_id 時啟用；
        校驗下載體積防截斷，並以 pm list 確認包確實落地；任一環節不成立回傳 None
        交回原 ADB 安裝路徑。
        """
        sidecar = Path(f"{apk_path}.srcurl")
        try:
            url = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
        except OSError:
            url = ""
        if not url or not url.lower().startswith(("http://", "https://")):
            logs.append(f"cloud_phone device-download skip: 缺少可用 srcurl（{sidecar.name}）")
            return None
        env_id = self._morelogin_env_id
        if not env_id:
            logs.append("cloud_phone device-download skip: 未綁定 MoreLogin env_id")
            return None
        try:
            from services.api.app.infrastructure.device.morelogin_device_api import (
                morelogin_execute_shell_command,
            )
        except ImportError:
            logs.append("cloud_phone device-download skip: MoreLogin 設備 API 不可用")
            return None

        def dev_sh(cmd: str, *, timeout_seconds: float) -> str:
            try:
                return morelogin_execute_shell_command(
                    env_id, cmd, timeout_seconds=timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001
                return f"ALIOTHEXEERR:{exc}"

        if "curl" not in dev_sh("command -v curl || echo NOCURL", timeout_seconds=30.0):
            logs.append("cloud_phone device-download skip: 雲機無 curl")
            return None
        expected_size = apk_path.stat().st_size
        remote = f"/data/local/tmp/alioth_{int(time.time())}_{apk_path.name}"
        curl_max = int(min(600.0, max(120.0, timeout)))
        logs.append(
            f"cloud_phone device-download(exeCommand): curl(max={curl_max}s) -> {remote}"
        )
        dl_out = dev_sh(
            f"curl -sL --fail --max-time {curl_max} "
            f"-w 'ALIOTHDL=%{{size_download}}' -o {remote} '{url}'",
            timeout_seconds=float(curl_max) + 30.0,
        )
        got_size = -1
        if "ALIOTHDL=" in dl_out:
            digits = "".join(
                ch for ch in dl_out.rsplit("ALIOTHDL=", 1)[-1] if ch.isdigit()
            )
            got_size = int(digits or "-1")
        if got_size != expected_size:
            logs.append(
                "cloud_phone device-download 放棄："
                f"expected={expected_size} got={got_size} out={dl_out[:120]}"
            )
            dev_sh(f"rm -f {remote}", timeout_seconds=30.0)
            return None
        def shell(args: list[str]) -> tuple[int, str]:
            """安裝側指令：adb shell 為主，半開連線重連重試，最後由 exeCommand 兜底。

            雲機剛開機時兩條通道各自都可能短暫不可用（adb 回 error: closed、
            exeCommand 回 Command not found），互為備援比單押一條穩。
            """
            if run_fn is None:
                out = dev_sh(" ".join(args), timeout_seconds=240.0)
                return (1 if out.startswith("ALIOTHEXEERR:") else 0), out
            code, text = run_fn(["shell", *args])
            if code != 0 and self._is_stream_install_fragile_error(text):
                serial = self._reopen_adb_session(self._serial or self._endpoint, logs)
                if not self._shell_probe_ok(serial):
                    logs.append("cloud_phone device-download: adb shell 未就緒，改用 exeCommand")
                    out = dev_sh(" ".join(args), timeout_seconds=240.0)
                    if not out.startswith("ALIOTHEXEERR:") and "not found" not in out.lower():
                        return 0, out
                    return 1, out
                code, text = run_fn(["shell", *args])
            return code, text

        pm_code, pm_out = shell(["pm", "install", "-r", remote])
        shell(["rm", "-f", remote])
        if pm_code != 0 or "Success" not in pm_out:
            logs.append(f"cloud_phone device-download pm install 失敗：{pm_out[:200]}")
            return None
        # pm install 回 Success 不等于装上：必须以设备事实确认包存在，否则交回 ADB 路径
        expected = try_extract_package_name_from_apk_manifest(apk_path)
        if expected:
            _code, listed = shell(["pm", "list", "packages", expected])
            if f"package:{expected}" not in listed:
                logs.append(
                    "cloud_phone device-download 放棄：pm install 回 Success 但"
                    f" pm list 查無 {expected}（回退 ADB 安裝）"
                )
                return None
            logs.append(f"cloud_phone device-download install 成功（已確認 {expected}）")
            return "\n".join(logs)
        logs.append("cloud_phone device-download install 成功（無包名可核對）")
        return "\n".join(logs)

    def install_apk(self, apk_path: Path, *, timeout_seconds: float = 180.0) -> str:
        """雲手機安裝：優先非串流；失敗則 push + pm install（避開 abb_exec closed）。"""
        if not apk_path.exists():
            raise CloudPhoneError(f"APK 不存在：{apk_path}")
        serial = self._serial or getattr(self._emu, "_serial", None) or self._endpoint
        apk_str = str(apk_path.resolve())
        timeout = self._resolve_install_timeout(apk_path, timeout_seconds=timeout_seconds)
        logs: list[str] = [f"cloud_phone install timeout={timeout:.0f}s"]

        serial = self._reopen_adb_session(str(serial), logs)

        def _run(args: list[str]) -> tuple[int, str]:
            logs.append(f"adb -s {serial} {' '.join(args)}")
            result = self._emu._run_adb(args, serial=serial, timeout_seconds=timeout)
            combined = "\n".join(p for p in (result.stdout, result.stderr) if p).strip()
            if combined:
                logs.append(combined)
            return int(result.returncode), combined

        # 0) 優先讓雲機自己下載 APK 再 pm install，繞開遠端 ADB 大檔慢傳/掛起
        device_download = self._try_device_side_download_install(
            apk_path, timeout=timeout, logs=logs, run_fn=_run
        )
        if device_download is not None:
            return device_download

        # 1) 非串流安裝（遠端 ADB 更穩）
        code, text = _run(["install", "--no-streaming", "-r", apk_str])
        if self._install_succeeded(code, text):
            return "\n".join(logs)

        unsupported = any(
            x in text.lower()
            for x in ("unknown option", "unrecognized option", "unrecognized flag")
        )
        # 2) 舊 adb 不支援 --no-streaming 時再試一次預設 install
        if unsupported:
            code, text = _run(["install", "-r", apk_str])
            if self._install_succeeded(code, text):
                return "\n".join(logs)

        if adb_output_has_device_error(text) and not self._is_stream_install_fragile_error(text):
            raise CloudPhoneError("ADB 未就緒，無法安裝 APK：\n" + "\n".join(logs))

        # 3) fallback：整包 push 後 pm install（避開 streamed abb_exec）
        logs.append("cloud_phone install fallback: push + pm install")
        serial = self._reopen_adb_session(serial, logs)
        remote = f"/data/local/tmp/alioth_{int(time.time())}_{apk_path.name}"
        push_code, push_text = _run(["push", apk_str, remote])
        if push_code != 0 and self._is_stream_install_fragile_error(push_text):
            serial = self._reopen_adb_session(serial, logs)
            push_code, push_text = _run(["push", apk_str, remote])
        if push_code != 0:
            raise CloudPhoneError("雲手機 adb push APK 失敗：\n" + "\n".join(logs))

        pm_code, pm_text = _run(["shell", "pm", "install", "-r", remote])
        # 清理遠端暫存（失敗不影響結果）
        try:
            self._emu._run_adb(
                ["shell", "rm", "-f", remote],
                serial=serial,
                timeout_seconds=30.0,
            )
        except Exception:  # noqa: BLE001 - 清理失敗可忽略
            logs.append(f"cloud_phone cleanup skipped: {remote}")

        if self._install_succeeded(pm_code, pm_text):
            return "\n".join(logs)
        # 部分機型 pm 只回 Success 在 stdout 一行
        if pm_code == 0 and "failure" not in pm_text.lower() and "error" not in pm_text.lower():
            logs.append("cloud_phone pm install exit=0（視為成功）")
            return "\n".join(logs)
        raise CloudPhoneError("雲手機安裝 APK 失敗：\n" + "\n".join(logs))

    def ensure_apk_installed(
        self,
        apk_path: Path,
        *,
        timeout_seconds: float = 300.0,
        same_label_skip_authorization: str | None = None,
    ):
        """覆寫：讓 GoogleEmulator 安裝計畫走雲手機穩健 install_apk。"""
        original = self._emu.install_apk
        original_list_user_packages = self._emu.list_user_installed_packages
        decision = resolve_same_label_scan_decision(
            authorization_text=same_label_skip_authorization
        )

        def _bound(path: Path, *, timeout_seconds: float = 180.0) -> str:
            return self.install_apk(path, timeout_seconds=timeout_seconds)

        self._emu.install_apk = _bound  # type: ignore[method-assign]
        if not decision.scan_enabled:
            # 同名扫描会逐个拉取设备上其他 APK；公网 ADB 下可能在真正安装前
            # 耗尽 Official API 预算。仅在调用方显式承担同名降版风险时跳过。
            self._emu.list_user_installed_packages = (  # type: ignore[method-assign]
                lambda **_kwargs: []
            )
        try:
            logs, package_name, installed_now = self._emu.ensure_apk_installed(
                apk_path,
                timeout_seconds=self._resolve_install_timeout(
                    apk_path, timeout_seconds=timeout_seconds
                ),
            )
            if decision.audit_line:
                logs = f"{decision.audit_line}\n{logs}" if logs else decision.audit_line
            return logs, package_name, installed_now
        finally:
            self._emu.install_apk = original
            self._emu.list_user_installed_packages = original_list_user_packages

    def prepare_app_for_script(
        self,
        package_name: str,
        *,
        settle_seconds: float = 5.0,
        require_foreground: bool = True,
    ) -> str:
        """雲手機對齊 AirtestIDE：保留原生 wm size／density，勿強制 1080x1920/320。"""
        serial = self._serial or getattr(self._emu, "_serial", None) or self._endpoint
        logs: list[str] = []
        # 清除先前 Demo 殘留的 override，避免與 IDE 所見密度不一致
        prepare_cmds: list[tuple[list[str], str]] = [
            (["shell", "wm", "size", "reset"], "wm size reset"),
            (["shell", "wm", "density", "reset"], "wm density reset"),
        ]
        if cloud_phone_live_touch_indicator_enabled():
            # 直播看 tap 落点：只开触点圆，不开 pointer_location（会挤栏位）。
            prepare_cmds.extend(
                (
                    (
                        ["shell", "settings", "put", "system", "show_touches", "1"],
                        "show_touches on",
                    ),
                    (
                        ["shell", "settings", "put", "system", "pointer_location", "0"],
                        "pointer_location off",
                    ),
                )
            )
        for args, label in prepare_cmds:
            try:
                self._emu._run_adb(args, serial=str(serial), timeout_seconds=15.0)
                logs.append(label)
            except Exception as exc:  # noqa: BLE001 - 重置失敗不阻擋啟動
                logs.append(f"{label} skipped: {exc}")

        prev_size = os.environ.get("ALIOTH_EMULATOR_WM_SIZE")
        prev_density = os.environ.get("ALIOTH_EMULATOR_WM_DENSITY")
        os.environ["ALIOTH_EMULATOR_WM_SIZE"] = "keep"
        os.environ["ALIOTH_EMULATOR_WM_DENSITY"] = "keep"
        try:
            prep = self._emu.prepare_app_for_script(
                package_name,
                settle_seconds=settle_seconds,
                require_foreground=require_foreground,
            )
        finally:
            if prev_size is None:
                os.environ.pop("ALIOTH_EMULATOR_WM_SIZE", None)
            else:
                os.environ["ALIOTH_EMULATOR_WM_SIZE"] = prev_size
            if prev_density is None:
                os.environ.pop("ALIOTH_EMULATOR_WM_DENSITY", None)
            else:
                os.environ["ALIOTH_EMULATOR_WM_DENSITY"] = prev_density
        combined = "\n".join([*logs, prep] if prep else logs)
        return combined


def bind_same_label_skip_authorization(text: str | None) -> Token:
    """绑定当前协程／线程的同名扫描跳过授权（通常来自任务备注）。"""

    return _same_label_skip_authorization.set(
        str(text).strip() if text is not None and str(text).strip() else None
    )


def reset_same_label_skip_authorization(token: Token) -> None:
    """恢复同名扫描跳过授权上下文。"""

    _same_label_skip_authorization.reset(token)


@contextmanager
def same_label_skip_authorization_scope(text: str | None) -> Iterator[None]:
    """在安装路径内临时绑定任务备注授权。"""

    token = bind_same_label_skip_authorization(text)
    try:
        yield
    finally:
        reset_same_label_skip_authorization(token)


def same_label_skip_authorization_present(text: str | None) -> bool:
    """任务备注／授权正文是否包含明确跳过标记。"""

    raw = str(text or "").strip()
    if not raw:
        return False
    return any(marker in raw for marker in SAME_LABEL_SKIP_AUTH_MARKERS)


def same_label_risk_acceptance_payload(remark: str | None) -> dict[str, object] | None:
    """从任务备注提取可供报告保留的同名降版风险授权范围。"""

    raw = str(remark or "").strip()
    if not same_label_skip_authorization_present(raw):
        return None
    return {
        "accepted": True,
        "marker": next(
            marker for marker in SAME_LABEL_SKIP_AUTH_MARKERS if marker in raw
        ),
        "authorization_scope": raw[:500],
        "source": "task_remark",
    }


class _SameLabelScanDecision:
    """同名扫描是否启用及可审计说明。"""

    __slots__ = ("scan_enabled", "audit_line", "authorization_scope")

    def __init__(
        self,
        *,
        scan_enabled: bool,
        audit_line: str | None = None,
        authorization_scope: str | None = None,
    ) -> None:
        self.scan_enabled = scan_enabled
        self.audit_line = audit_line
        self.authorization_scope = authorization_scope


def resolve_same_label_scan_decision(
    *,
    authorization_text: str | None = None,
) -> _SameLabelScanDecision:
    """解析同名降版扫描决策：环境开关 + 任务备注授权，缺一不可。

    - 环境未显式关闭：保持扫描。
    - 环境关闭但备注无授权标记：fail-closed，继续扫描并写审计告警。
    - 环境关闭且备注含授权标记：跳过扫描，审计保留授权范围摘要。
    """

    if _env_same_label_scan_enabled():
        return _SameLabelScanDecision(scan_enabled=True)

    auth = str(
        authorization_text
        if authorization_text is not None
        else (_same_label_skip_authorization.get() or "")
    ).strip()
    if not same_label_skip_authorization_present(auth):
        logger.warning(
            "ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED=0 但任务备注缺少授权标记，"
            "继续执行同名降版扫描"
        )
        return _SameLabelScanDecision(
            scan_enabled=True,
            audit_line=(
                "cloud_phone same-label risk scan kept "
                "(env=0 but task remark missing "
                f"{SAME_LABEL_SKIP_AUTH_MARKERS[0]})"
            ),
        )

    scope = auth[:200]
    return _SameLabelScanDecision(
        scan_enabled=False,
        authorization_scope=auth[:500],
        audit_line=(
            "cloud_phone same-label risk scan skipped "
            "(ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED=0; "
            f"authorization_scope={scope!r})"
        ),
    )


def _env_same_label_scan_enabled() -> bool:
    """环境层同名扫描开关；默认开启。"""

    raw = (os.getenv("ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED") or "1").strip()
    return raw.casefold() not in {"0", "false", "no", "off"}


def _same_label_scan_enabled() -> bool:
    """兼容旧调用：仅看最终是否扫描，不含审计细节。"""

    return resolve_same_label_scan_decision().scan_enabled
