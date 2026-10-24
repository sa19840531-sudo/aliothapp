"""ReDroid（遠端 Docker Android）控制：adb connect → 安裝 → 以 package 啟動。

與 google_emulator 共用安裝／啟動邏輯；不啟動本機 AVD。
環境變數：
- ALIOTH_REDROID_ADB：預設 127.0.0.1:5555（Docker 映射）；容器內 Mobile Host 用 alioth-redroid:5555
- ANDROID_HOME：本機 SDK adb（Host Runner）
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from services.api.app.infrastructure.device.google_emulator import (
    GoogleEmulatorController,
    GoogleEmulatorError,
)
from services.api.app.infrastructure.device.ldplayer import normalize_adb_serial


class RedroidError(GoogleEmulatorError):
    """ReDroid / 遠端 adb 操作失敗。"""


def resolve_redroid_endpoint() -> str:
    return (os.getenv("ALIOTH_REDROID_ADB") or "127.0.0.1:5555").strip()


class RedroidController:
    """遠端 ReDroid：先 adb connect，再複用 GoogleEmulatorController 的安裝與啟動。"""

    def __init__(self, *, bind_process_env: bool = True) -> None:
        self._endpoint = resolve_redroid_endpoint()
        self._bind_process_env = bind_process_env
        # 強制走指定 serial，避免誤連本機 emulator
        if bind_process_env:
            os.environ["ALIOTH_ADB_SERIAL"] = self._endpoint
        self._emu = GoogleEmulatorController()
        self._serial: str | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def android_home(self) -> Path:
        return self._emu.android_home

    @property
    def adb_path(self) -> Path:
        return self._emu.adb_path

    def ensure_sdk_adb_server(self) -> list[str]:
        return self._emu.ensure_sdk_adb_server()

    def _adb(self, args: list[str], *, timeout_seconds: float = 60.0) -> subprocess.CompletedProcess[str]:
        cmd = [str(self.adb_path), *args]
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
            raise RedroidError(f"adb 逾時：{' '.join(cmd)}") from exc
        except Exception as exc:
            raise RedroidError(f"adb 執行失敗：{' '.join(cmd)}") from exc

    def ensure_ready(self, *, timeout_seconds: float | None = None) -> tuple[str, list[str]]:
        logs: list[str] = []
        logs.append(f"redroid endpoint={self._endpoint}")
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
                    or (normalize_adb_serial(self._endpoint) and s == normalize_adb_serial(self._endpoint))
                    or self._endpoint in s
                    or s.endswith(":5555")
                )
            ]
            if not online:
                # 相容：任意 host:port / ip:port 線上裝置
                online = [s for s, st in devices if st == "device" and ":" in s]
            if online:
                serial = online[0]
                boot = self._emu._run_adb(
                    ["shell", "getprop", "sys.boot_completed"],
                    serial=serial,
                    timeout_seconds=20.0,
                )
                if (boot.stdout or "").strip() == "1":
                    self._serial = serial
                    self._emu._serial = serial
                    if self._bind_process_env:
                        os.environ["ALIOTH_ADB_SERIAL"] = serial
                    logs.append(f"adb ready: {serial}")
                    return serial, logs
                last = f"boot_completed={(boot.stdout or '').strip() or '(empty)'}"
            else:
                last = f"devices={devices or 'empty'}"
            now = time.time()
            if now - last_progress >= 15.0:
                logs.append(f"waiting redroid adb: {last}")
                last_progress = now
            time.sleep(3.0)

        raise RedroidError(f"等待 ReDroid ADB 就緒逾時（{timeout}s）：{last}")

    def ensure_apk_installed(self, apk_path: Path, *, timeout_seconds: float = 180.0):
        return self._emu.ensure_apk_installed(apk_path, timeout_seconds=timeout_seconds)

    def prepare_app_for_script(
        self,
        package_name: str,
        *,
        settle_seconds: float = 5.0,
        require_foreground: bool = True,
    ) -> str:
        # 與 google 相同：package 啟動，不點雷電桌面圖示
        return self._emu.prepare_app_for_script(
            package_name,
            settle_seconds=settle_seconds,
            require_foreground=require_foreground,
        )

    def airtest_device_uri(self) -> str:
        return self._emu.airtest_device_uri()
