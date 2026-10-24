"""後端無關的 ADB 裝置原語。

唯一允許直接執行 tap / swipe / back / type_text / screencap 的基礎設施邊界；
Interaction Runtime / SurfaceDriver 經此操作裝置，API 層不得直呼。
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.api.app.infrastructure.device.adb_ui_hierarchy_fallback import (
    dump_current_ui_hierarchy,
)
from services.api.app.infrastructure.device.app_tap_audit_log import log_app_tap_audit
from services.api.app.infrastructure.device.backend import (
    BACKEND_CLOUD_PHONE,
    BACKEND_GOOGLE_EMULATOR,
    BACKEND_LDPLAYER,
    BACKEND_REDROID,
)

# 云手机直播可见触点：同像素短按，短于系统长按阈值。
LIVE_TOUCH_INDICATOR_DWELL_MS = 240


def cloud_phone_live_touch_indicator_enabled() -> bool:
    raw = (os.getenv("ALIOTH_CLOUD_PHONE_SHOW_TOUCHES") or "1").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def tap_shell_args(x: int, y: int, *, live_visible: bool) -> list[str]:
    """直播可见时用同点短 swipe；其它后端保持 input tap。"""

    if live_visible:
        return [
            "input",
            "swipe",
            str(int(x)),
            str(int(y)),
            str(int(x)),
            str(int(y)),
            str(LIVE_TOUCH_INDICATOR_DWELL_MS),
        ]
    return ["input", "tap", str(int(x)), str(int(y))]

CANONICAL_WIDTH = 1080
CANONICAL_HEIGHT = 1920
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
APP_CAPTURE_PIPELINE_LATENCY_VERSION = "app-capture-pipeline-latency.v1"
# 截图与设备宽高比差超过该阈值时启用居中 letterbox 内容矩形映射。
_ASPECT_RATIO_LETTERBOX_TOLERANCE = 0.02
_LOGGER = logging.getLogger(__name__)
# 默认强制全部 App 直屏；设 ALIOTH_FORCE_PORTRAIT=0 可关闭。
_FORCE_PORTRAIT_ENV = "ALIOTH_FORCE_PORTRAIT"


def force_portrait_lock_enabled() -> bool:
    """是否启用全局直屏锁定（默认开）。"""

    raw = (os.getenv(_FORCE_PORTRAIT_ENV) or "1").strip().casefold()
    return raw not in {"0", "false", "off", "no", "-", "keep"}


def apply_force_portrait_lock(
    *,
    shell: Callable[[list[str], float], None],
    shell_output: Callable[[list[str], float], str] | None = None,
) -> list[str]:
    """强制设备保持直屏，并忽略 App 自行请求的横屏。

    顺序：先禁止 App 改方向，再把用户旋转锁回 0。已是横屏的会跟着锁回直屏。
    任一步失败只记日志，不阻断启动；旧 ROM 可能没有后几项命令。
    """

    if not force_portrait_lock_enabled():
        return ["portrait lock skipped (ALIOTH_FORCE_PORTRAIT off)"]

    logs: list[str] = []

    def _try(args: list[str], *, label: str, timeout: float = 10.0) -> bool:
        try:
            shell(list(args), timeout)
            logs.append(label)
            return True
        except Exception as exc:  # noqa: BLE001 - 直屏锁任一步失败不阻断探索
            logs.append(f"{label} skipped: {type(exc).__name__}")
            return False

    _try(
        ["settings", "put", "system", "accelerometer_rotation", "0"],
        label="accelerometer_rotation=0",
    )
    # 先挡住 App／浏览器的 screenOrientation，否则后面的 user_rotation=0 会被横屏请求盖掉。
    if not _try(
        ["wm", "set-fix-to-user-rotation", "enabled"],
        label="wm set-fix-to-user-rotation enabled",
    ):
        _try(
            ["wm", "set-fix-to-user-rotation", "-d", "0", "enabled"],
            label="wm set-fix-to-user-rotation -d 0 enabled",
        )
    if not _try(
        ["wm", "set-ignore-orientation-request", "true"],
        label="wm set-ignore-orientation-request true",
    ):
        _try(
            ["wm", "set-ignore-orientation-request", "-d", "0", "true"],
            label="wm set-ignore-orientation-request -d 0 true",
        )
    # API 30 用 set-user-rotation；API 31+ 用 user-rotation。两条都试，把当前横屏压回 0。
    _try(
        ["settings", "put", "system", "user_rotation", "0"],
        label="user_rotation=0",
    )
    _try(["wm", "set-user-rotation", "lock", "0"], label="wm set-user-rotation lock 0")
    _try(["wm", "user-rotation", "lock", "0"], label="wm user-rotation lock 0")

    # 若当前 Override 已是横屏（宽>高），尽量 reset 回物理竖屏，再保持锁。
    if shell_output is not None:
        try:
            size_text = str(shell_output(["wm", "size"], 15.0) or "")
            width, height = resolve_wm_display_size(size_text, display_rotation=None)
            if width > height:
                if _try(["wm", "size", "reset"], label="wm size reset (landscape override)"):
                    _try(
                        ["settings", "put", "system", "user_rotation", "0"],
                        label="user_rotation=0 (after size reset)",
                    )
                    _try(
                        ["wm", "set-user-rotation", "lock", "0"],
                        label="wm set-user-rotation lock 0 (after size reset)",
                    )
                else:
                    logs.append(
                        f"portrait size keep landscape override={width}x{height}"
                    )
            else:
                logs.append(f"portrait size ok={width}x{height}")
        except Exception as exc:  # noqa: BLE001
            logs.append(f"portrait size check skipped: {type(exc).__name__}")

    logs.append("force_portrait_lock applied")
    return logs


class CoordinateMappingError(ValueError):
    """坐标换算 fail-closed；error_code 可供 Resolver 透传。"""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, kw_only=True)
class MappedDevicePoint:
    """设备像素落点及换算审计字段。"""

    x: int
    y: int
    letterboxed: bool = False
    rotation_unproven: bool = True
    display_rotation: int | None = None
    content_left: float = 0.0
    content_top: float = 0.0
    content_width: float = 0.0
    content_height: float = 0.0


@dataclass(frozen=True, kw_only=True)
class DisplaySafeArea:
    """设备逻辑像素安全区（相对屏幕原点的 inset）。"""

    left: int
    top: int
    right: int
    bottom: int
    width: int
    height: int
    source: str = "display_bounds"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> DisplaySafeArea | None:
        if not isinstance(raw, Mapping):
            return None
        try:
            left = int(raw["left"])
            top = int(raw["top"])
            right = int(raw["right"])
            bottom = int(raw["bottom"])
            width = int(raw.get("width") or 0)
            height = int(raw.get("height") or 0)
            source = str(raw.get("source") or "display_bounds")
        except (KeyError, TypeError, ValueError):
            return None
        if left < 0 or top < 0 or right < 0 or bottom < 0:
            return None
        return cls(
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            width=width,
            height=height,
            source=source,
        )

    def contains_device_point(self, x: int, y: int, *, device_w: int, device_h: int) -> bool:
        """落点是否在状态栏／导航栏之外的安全内容区。"""
        return (
            self.left <= x < device_w - self.right
            and self.top <= y < device_h - self.bottom
        )


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


# 同机连跑清残留时跳过的系统／桌面／厂商壳；第三方质检 APK 不在此列。
_LEFTOVER_QC_SYSTEM_PREFIXES = (
    "android.",
    "com.android.",
    "com.google.android.",
    "com.qualcomm.",
    "com.samsung.",
    "com.sec.",
    "com.miui.",
    "com.xiaomi.",
    "com.huawei.",
    "com.hihonor.",
    "com.oplus.",
    "com.coloros.",
    "com.oneplus.",
    "com.realme.",
    "com.vivo.",
    "com.bbk.",
    "com.meizu.",
    "com.morelogin.",
)
_LEFTOVER_QC_SYSTEM_EXACT = frozenset({"android", "system"})


def is_clearable_leftover_qc_package(package_name: str) -> bool:
    """判断前台包是否可作为质检残留予以 force-stop（非系统桌面）。"""
    pkg = str(package_name or "").strip()
    if not pkg or "/" in pkg or " " in pkg or "." not in pkg:
        return False
    lowered = pkg.casefold()
    if lowered in _LEFTOVER_QC_SYSTEM_EXACT:
        return False
    return not any(lowered.startswith(prefix) for prefix in _LEFTOVER_QC_SYSTEM_PREFIXES)


# 故障辅助证据是 soft-optional；不得让单个 ADB 命令拖住整条动作链。
_FAULT_CAPTURE_BUDGET_SECONDS = 12.0
_FAULT_DROPBOX_TAGS = (
    "data_app_anr",
    "data_app_crash",
    "system_app_anr",
    "system_app_crash",
)

# 公网 ADB 截图是 Official HTTP 请求的一部分；必须给后续前台身份读取
# 留出预算，同时保留远端落盘 + pull 恢复路径。
# 云手机 pull 上限配合 Capture HTTP floor（≥270s）：同请求内
# 首次 pull + 最多 2 次（短超时 refresh + 短 pull）不得撑爆客户端超时。
_SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS = 5.0
_SCREENSHOT_REMOTE_TIMEOUT_SECONDS = 10.0
_SCREENSHOT_PULL_TIMEOUT_SECONDS = 35.0
_SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS = 90.0
_SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS = 45.0
# 首次 screencap 失败后，最多再 refresh+截图的次数（Bridge／SurfaceDriver 共用）
_CAPTURE_IN_FRAME_FAILURE_RETRIES = 2
_CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0)
_SCREENSHOT_CLEANUP_TIMEOUT_SECONDS = 3.0
_UI_HIERARCHY_CLEANUP_TIMEOUT_SECONDS = 3.0
_SCREENSHOT_FALLBACK_MAX_SECONDS = (
    _SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS
    + _SCREENSHOT_REMOTE_TIMEOUT_SECONDS
    + _SCREENSHOT_PULL_TIMEOUT_SECONDS
    + _SCREENSHOT_CLEANUP_TIMEOUT_SECONDS
)


def _unique_remote_ui_dump_path(remote_path: str) -> str:
    """为本次 UI dump 保留目录和文件名，并追加唯一标识。"""

    directory, basename = posixpath.split(remote_path)
    stem, suffix = posixpath.splitext(basename)
    if not stem:
        stem = basename or "alioth_ui_dump"
    unique_basename = f"{stem}.{uuid4().hex}{suffix}"
    return posixpath.join(directory, unique_basename) if directory else unique_basename


def _require_shell_success(
    response: subprocess.CompletedProcess[str],
) -> subprocess.CompletedProcess[str]:
    """把 ADB 非零退出转为异常，禁止上层把失败当作动作成功。"""

    if response.returncode != 0:
        detail = "\n".join(
            part.strip()
            for part in (response.stderr or "", response.stdout or "")
            if part and part.strip()
        )[:240]
        raise RuntimeError(
            "adb shell 命令失败："
            f"returncode={response.returncode}"
            + (f" detail={detail}" if detail else "")
        )
    return response


@dataclass
class ScreencapStageTrace:
    """screencap 路径分段：远端生成／pull／cleanup（或 exec-out 直取）。"""

    path_mode: str
    remote_screencap_ms: int | None = None
    adb_pull_ms: int | None = None
    cleanup_ms: int | None = None
    exec_out_ms: int | None = None
    png_bytes: int = 0
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_mode": self.path_mode,
            "remote_screencap_ms": self.remote_screencap_ms,
            "adb_pull_ms": self.adb_pull_ms,
            "cleanup_ms": self.cleanup_ms,
            "exec_out_ms": self.exec_out_ms,
            "png_bytes": int(self.png_bytes),
            "error_code": self.error_code,
        }


@dataclass
class AppCapturePipelineLatency:
    """云机截图五段 span：remote screencap／adb pull／cleanup／wm-size／identity。"""

    protocol_version: str = APP_CAPTURE_PIPELINE_LATENCY_VERSION
    captured_at: str = ""
    path_mode: str = "unknown"
    remote_screencap_ms: int | None = None
    adb_pull_ms: int | None = None
    cleanup_ms: int | None = None
    exec_out_ms: int | None = None
    wm_size_ms: int = 0
    wm_size_source: str = "cached"
    identity_ms: int = 0
    png_bytes: int = 0
    total_ms: int = 0
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "captured_at": self.captured_at,
            "path_mode": self.path_mode,
            "remote_screencap_ms": self.remote_screencap_ms,
            "adb_pull_ms": self.adb_pull_ms,
            "cleanup_ms": self.cleanup_ms,
            "exec_out_ms": self.exec_out_ms,
            "wm_size_ms": int(self.wm_size_ms),
            "wm_size_source": self.wm_size_source,
            "identity_ms": int(self.identity_ms),
            "png_bytes": int(self.png_bytes),
            "total_ms": int(self.total_ms),
            "error_code": self.error_code,
        }


@dataclass
class AdbDeviceOps:
    """後端無關的最小裝置操作。"""

    backend: str
    width: int
    height: int
    _log: Callable[[str], None]
    _screencap_fn: Callable[[Path], ScreencapStageTrace | None]
    _shell_fn: Callable[[list[str], float], None]
    _shell_output_fn: Callable[[list[str], float], str] | None = None
    last_screencap_trace: ScreencapStageTrace | None = field(default=None, repr=False)

    def screencap(self, dest: Path) -> ScreencapStageTrace:
        dest.parent.mkdir(parents=True, exist_ok=True)
        trace = self._screencap_fn(dest)
        if not dest.is_file() or dest.stat().st_size < 100:
            raise RuntimeError(f"screencap 失敗：{dest}")
        if trace is None:
            trace = ScreencapStageTrace(
                path_mode="backend_native",
                png_bytes=int(dest.stat().st_size),
            )
        elif trace.png_bytes <= 0:
            trace.png_bytes = int(dest.stat().st_size)
        self.last_screencap_trace = trace
        return trace

    def query_wm_size(self) -> tuple[tuple[int, int], str]:
        """读取当前触控／UIA 坐标系尺寸；有 shell 时现场查询，否则退回缓存。"""

        if self._shell_output_fn is None:
            return (self.width, self.height), "cached"
        text = self._shell_output_fn(["wm", "size"], 15.0)
        rotation: int | None = None
        try:
            rotation = self.query_display_rotation()
        except Exception:  # noqa: BLE001 - 旋转旁路失败时仍可用 Override／Physical
            rotation = None
        width, height = resolve_wm_display_size(text, display_rotation=rotation)
        self.width = width
        self.height = height
        return (width, height), "shell"

    def shell_output(self, args: list[str], *, timeout: float = 30.0) -> str:
        """执行受控只读 shell 命令并返回设备输出。"""
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        return self._shell_output_fn(list(args), timeout)

    def dump_ui_hierarchy(
        self,
        *,
        remote_path: str = "/sdcard/alioth_ui_dump.xml",
    ) -> str:
        """经 uiautomator dump 拉取有界 UI Hierarchy XML。"""
        if self._shell_output_fn is None:
            raise RuntimeError("当前 ADB 后端未装配 shell 输出能力，无法 dump UI Tree")
        requested_remote = str(remote_path or "").strip() or "/sdcard/alioth_ui_dump.xml"
        remote = _unique_remote_ui_dump_path(requested_remote)
        try:
            output = self._shell_output_fn(["uiautomator", "dump", remote], 45.0)
            if "ERROR: could not get idle state." in output:
                return dump_current_ui_hierarchy(
                    self._shell_output_fn, self._shell_fn, self._log
                )
            if "ERROR:" in output:
                raise RuntimeError("uiautomator dump 未返回有效 hierarchy XML：设备采集失败")
            xml = self._shell_output_fn(["cat", remote], 30.0)
            text = str(xml or "").strip()
            if "<hierarchy" not in text and not text.startswith("<?xml"):
                # 回退：部分模拟器仅支持 dump 到 stdout
                try:
                    text = str(
                        self._shell_output_fn(["uiautomator", "dump", "/dev/tty"], 30.0)
                        or ""
                    ).strip()
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError("uiautomator dump 未返回有效 hierarchy XML") from exc
            if "<hierarchy" not in text and not text.startswith("<?xml"):
                raise RuntimeError("uiautomator dump 未返回有效 hierarchy XML")
            self._log(f"ui_tree dump bytes={len(text.encode('utf-8'))}")
            return text
        finally:
            try:
                self._shell_fn(
                    ["rm", "-f", remote],
                    _UI_HIERARCHY_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - 清理失败不能覆盖原始结果
                self._log(f"ui_tree cleanup skipped: {type(exc).__name__}")

    def dump_logcat(
        self,
        *,
        package_name: str | None = None,
        max_chars: int = 24_000,
    ) -> str:
        """拉取有界 logcat 快照（-d 不清空缓冲区）。"""
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        # 使用 dump 模式；包名仅作客户端过滤提示，避免任意 shell 注入
        raw = self._shell_output_fn(["logcat", "-d", "-t", "200"], 45.0)
        text = str(raw or "").replace("\x00", "")
        pkg = (package_name or "").strip()
        if pkg:
            filtered = "\n".join(
                line for line in text.splitlines() if pkg in line
            )
            # 过滤为空时保留尾部原文，避免“假空日志”
            text = filtered if filtered.strip() else text
        if len(text) > max_chars:
            text = text[-max_chars:]
        self._log(f"logcat dump chars={len(text)} package={pkg or '-'}")
        return text

    def force_stop(self, package_name: str) -> None:
        """强制停止应用进程（冷启动前置）。"""
        pkg = str(package_name or "").strip()
        if not pkg or "/" in pkg or " " in pkg:
            raise ValueError("package_name 非法")
        self._shell_fn(["am", "force-stop", pkg], 30.0)

    def force_portrait_lock(self) -> list[str]:
        """强制当前设备保持直屏；忽略目标 App 的横屏请求。"""

        logs = apply_force_portrait_lock(
            shell=self._shell_fn,
            shell_output=self._shell_output_fn,
        )
        for line in logs:
            self._log(f"portrait_lock {line}")
        # 锁方向后刷新触控坐标系缓存。
        try:
            self.query_wm_size()
        except Exception:  # noqa: BLE001 - 尺寸刷新失败不阻断锁定结果
            pass
        return logs

    def home(self) -> None:
        """回到桌面（KEYCODE_HOME=3），让下一颗 APK 冷启动有干净前台。"""
        self._shell_fn(["input", "keyevent", "3"], 15.0)

    def clear_leftover_qc_app(
        self,
        *,
        package_name: str | None = None,
        press_home: bool = True,
    ) -> list[str]:
        """关闭质检残留 App，避免同机连跑第二颗时撞上未关掉的第一颗。

        - 显式 ``package_name``：必做 force-stop（失败只记日志）
        - 当前前台若为可清理的第三方包且与显式包不同：再 force-stop
        - 默认按 HOME，离开残留前台
        """
        logs: list[str] = []
        explicit = str(package_name or "").strip()
        if explicit:
            try:
                self.force_stop(explicit)
                logs.append(f"force_stop explicit={explicit}")
            except Exception as exc:  # noqa: BLE001 - 收口清理不得阻断连跑
                logs.append(f"force_stop explicit skipped: {type(exc).__name__}")

        identity: tuple[str, str | None] | None = None
        try:
            identity = self.current_runtime_identity()
        except Exception as exc:  # noqa: BLE001
            logs.append(f"foreground identity skipped: {type(exc).__name__}")
        if identity is not None:
            foreground = str(identity[0] or "").strip()
            if (
                foreground
                and foreground != explicit
                and is_clearable_leftover_qc_package(foreground)
            ):
                try:
                    self.force_stop(foreground)
                    logs.append(f"force_stop foreground={foreground}")
                except Exception as exc:  # noqa: BLE001
                    logs.append(
                        f"force_stop foreground skipped: {type(exc).__name__}"
                    )

        if press_home:
            try:
                self.home()
                logs.append("press HOME")
            except Exception as exc:  # noqa: BLE001
                logs.append(f"press HOME skipped: {type(exc).__name__}")
        if not logs:
            logs.append("leftover cleanup noop")
        return logs

    def clear_app_data(self, package_name: str) -> None:
        """清空应用数据容器（reset_policy=clear_data）。"""
        pkg = str(package_name or "").strip()
        if not pkg or "/" in pkg or " " in pkg:
            raise ValueError("package_name 非法")
        try:
            self._shell_fn(["pm", "clear", "--user", "0", pkg], 60.0)
        except Exception:  # noqa: BLE001 - 旧设备可能不支持 --user
            self._shell_fn(["pm", "clear", pkg], 60.0)

    def apply_reset_policy(
        self,
        package_name: str,
        *,
        reset_policy: str | None = None,
        activity: str | None = None,
    ) -> dict[str, str | bool]:
        """按 Session Spec reset_policy 执行恢复原语。"""
        pkg = str(package_name or "").strip()
        if not pkg or "/" in pkg or " " in pkg:
            raise ValueError("package_name 非法")
        policy = str(reset_policy or "force_stop").strip().casefold() or "force_stop"
        if policy == "none":
            return {"mode": "none", "container_rotated": False}
        if policy == "warm_preferred":
            self.warm_bring_to_front(pkg, activity=activity)
            return {"mode": "warm", "container_rotated": False}
        if policy == "clear_data":
            self.clear_app_data(pkg)
            self.warm_bring_to_front(pkg, activity=activity)
            return {"mode": "clear_data", "container_rotated": True}
        # force_stop（默认冷启动）
        self.force_stop(pkg)
        self.warm_bring_to_front(pkg, activity=activity)
        return {"mode": "cold", "container_rotated": False}

    def warm_bring_to_front(self, package_name: str, *, activity: str | None = None) -> None:
        """热恢复：不 force-stop，仅把已有任务带回前台。"""
        pkg = str(package_name or "").strip()
        if not pkg or "/" in pkg or " " in pkg:
            raise ValueError("package_name 非法")
        act = str(activity or "").strip() or None
        if act and (" " in act or ";" in act):
            raise ValueError("activity 非法")
        if act:
            component = act if "/" in act else f"{pkg}/{act}"
            self._shell_fn(
                ["am", "start", "--activity-single-top", "-n", component],
                30.0,
            )
            return
        # monkey 是常见“唤起已安装包到前台”的无副作用路径
        self._shell_fn(
            [
                "monkey",
                "-p",
                pkg,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            30.0,
        )

    def start_deep_link(self, uri: str, *, package_name: str | None = None) -> None:
        """通过 VIEW Intent 打开 Deep Link。"""
        link = str(uri or "").strip()
        if not link or any(ch in link for ch in (" ", ";", "|", "&", "`", "$", "\n", "\r")):
            raise ValueError("deep_link URI 非法")
        args = [
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            link,
        ]
        pkg = str(package_name or "").strip() or None
        if pkg:
            if "/" in pkg or " " in pkg:
                raise ValueError("package_name 非法")
            args.extend(["-p", pkg])
        self._shell_fn(args, 30.0)

    def dump_fault_signals(self, *, package_name: str | None = None) -> str:
        """拉取有界 crash/ANR 信号：dropbox + ANR traces + tombstone 列表。"""
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        chunks: list[str] = []
        deadline = time.monotonic() + _FAULT_CAPTURE_BUDGET_SECONDS

        def read_bounded(
            args: list[str],
            timeout: float,
            *,
            source: str,
        ) -> str:
            """在共享预算内执行只读命令，并把不可用原因留在原始摘要里。"""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log(f"fault auxiliary {source} skipped: budget exhausted")
                chunks.append("fault_aux_skipped:budget_exhausted")
                return ""
            try:
                return str(
                    self._shell_output_fn(
                        list(args), min(float(timeout), max(0.1, remaining))
                    )
                    or ""
                )
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"fault auxiliary {source} unavailable: {type(exc).__name__}"
                )
                chunks.append(f"fault_aux_unavailable:{type(exc).__name__}")
                return ""

        # `--print` 不带标签会展开整个 dropbox；MoreLogin 上可能卡满 ADB 超时。
        # 先取轻量索引，仅对近期存在的故障标签按共享预算读取正文。
        dropbox_listing = read_bounded(
            ["dumpsys", "dropbox"],
            2.5,
            source="dropbox_listing",
        )
        if dropbox_listing:
            chunks.append(dropbox_listing)
            for tag in _FAULT_DROPBOX_TAGS:
                if tag not in dropbox_listing:
                    continue
                body = read_bounded(
                    ["dumpsys", "dropbox", "--print", tag],
                    3.0,
                    source=f"dropbox_{tag}",
                )
                if body:
                    chunks.append(f"--- dropbox:{tag} ---\n{body}")

        anr_listing = read_bounded(
            ["ls", "-lt", "/data/anr"],
            1.5,
            source="anr_listing",
        )
        if anr_listing:
            chunks.append(anr_listing)
            for line in anr_listing.splitlines()[:3]:
                name = line.strip().split()[-1] if line.strip() else ""
                if not name or name in {".", ".."} or "/" in name:
                    continue
                remote = f"/data/anr/{name}"
                body = read_bounded(
                    ["tail", "-c", "6000", remote],
                    1.5,
                    source=f"anr_file_{name}",
                )
                if body:
                    chunks.append(f"--- anr_file:{name} ---\n{body}")

        tomb_listing = read_bounded(
            ["ls", "-lt", "/data/tombstones"],
            1.5,
            source="tombstone_listing",
        )
        if tomb_listing:
            chunks.append(tomb_listing)
            for line in tomb_listing.splitlines()[:2]:
                name = line.strip().split()[-1] if line.strip() else ""
                if not name.startswith("tombstone") or "/" in name:
                    continue
                remote = f"/data/tombstones/{name}"
                body = read_bounded(
                    ["tail", "-c", "4000", remote],
                    1.5,
                    source=f"tombstone_file_{name}",
                )
                if body:
                    chunks.append(f"--- tombstone_file:{name} ---\n{body}")
        text = "\n".join(chunks).replace("\x00", "")
        pkg = (package_name or "").strip()
        if pkg:
            filtered = "\n".join(line for line in text.splitlines() if pkg in line)
            if filtered.strip():
                text = filtered
        return text[-24_000:]

    def dump_view_hierarchy(self) -> str:
        """独立 View Hierarchy 通道（dumpsys），非 Accessibility。

        除 activity top／window 外追加 webview 与 activities，便于抽 mUrl／Intent dat。
        """
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        parts: list[str] = []
        for args in (
            ["dumpsys", "activity", "top"],
            ["dumpsys", "activity", "activities"],
            ["dumpsys", "window", "windows"],
            ["dumpsys", "webview"],
        ):
            try:
                parts.append(self._shell_output_fn(list(args), 35.0))
            except Exception as exc:  # noqa: BLE001
                parts.append(f"{' '.join(args)}:unavailable:{type(exc).__name__}")
        return "\n".join(parts).replace("\x00", "")[-40_000:]

    def set_clipboard(self, text: str) -> None:
        """写入设备剪贴板（CJK 输入前置）。"""
        raw = str(text or "")
        if not raw:
            return
        if len(raw) > 4000:
            raise ValueError("clipboard 文本过长")
        import base64

        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        # Android 13+ cmd clipboard；失败则写临时文件再尝试
        script = (
            f"TXT=$(echo '{encoded}' | base64 -d) && "
            f'(cmd clipboard set-text "$TXT" || '
            f'service call clipboard 2 i32 1 i32 1 s16 "$TXT" >/dev/null)'
        )
        self._shell_fn(["sh", "-c", script], 20.0)

    def get_clipboard_text(self) -> str:
        """尽力读取剪贴板明文；失败返回空串，不 invent。

        通道顺序：cmd get-primary-clip → get-text → dumpsys clipboard → service call。
        云机禁 cmd 时仍可能从 dumpsys／Parcel 扫到 http(s)。
        """

        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        channels: list[tuple[str, list[str]]] = [
            ("cmd_primary", ["cmd", "clipboard", "get-primary-clip"]),
            ("cmd_text", ["cmd", "clipboard", "get-text"]),
            ("dumpsys", ["dumpsys", "clipboard"]),
            ("service_call", ["service", "call", "clipboard", "1"]),
        ]
        raw = ""
        for _name, args in channels:
            try:
                raw = self._shell_output_fn(list(args), 15.0)
            except Exception:  # noqa: BLE001 - 命令不可用时换下一路
                raw = ""
            if str(raw or "").strip():
                break
        text = str(raw or "").replace("\x00", "").strip()
        if not text:
            return ""
        # Parcel／dumpsys 噪声大时优先抽出其中的 http(s) 片段（避免循环依赖不引 surface 模块）
        match = re.search(r"https?://[^\s\"'<>\]\)}]+", text, flags=re.IGNORECASE)
        if match:
            return match.group(0).rstrip(".,;:)\"'")[:4000]
        if len(text) > 4000:
            text = text[:4000]
        return text

    def start_external_intent(
        self,
        *,
        action: str,
        data_uri: str | None = None,
        package_name: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        """白名单外部 Intent（需上层授权门禁）。"""
        allowed = {
            "android.intent.action.VIEW",
            "android.intent.action.SEND",
            "android.intent.action.DIAL",
        }
        act = str(action or "").strip()
        if act not in allowed:
            raise ValueError("external action 不在白名单")
        args = ["am", "start", "-a", act]
        if data_uri:
            uri = str(data_uri).strip()
            if " " in uri or ";" in uri:
                raise ValueError("data_uri 非法")
            args.extend(["-d", uri])
        if mime_type:
            mime = str(mime_type).strip()
            if " " in mime:
                raise ValueError("mime_type 非法")
            args.extend(["-t", mime])
        pkg = str(package_name or "").strip() or None
        if pkg:
            if "/" in pkg or " " in pkg:
                raise ValueError("package_name 非法")
            args.extend(["-p", pkg])
        self._shell_fn(args, 30.0)

    def apply_account_environment(
        self,
        *,
        account_environment_ref: str,
        package_name: str | None = None,
    ) -> dict[str, str | bool]:
        """设备侧记录账号环境引用（不写明文凭证）。"""
        ref = str(account_environment_ref or "").strip()
        if not ref or " " in ref or len(ref) > 200:
            raise ValueError("account_environment_ref 非法")
        self._shell_fn(
            ["settings", "put", "global", "alioth_account_environment_ref", ref],
            15.0,
        )
        pkg = str(package_name or "").strip() or None
        if pkg and "/" not in pkg and " " not in pkg:
            marker = f"/sdcard/Download/alioth_account_{pkg.replace('.', '_')}.ref"
            self._shell_fn(
                ["sh", "-c", f"printf '%s' '{ref}' > {marker}"],
                15.0,
            )
        return {"bound": True, "account_environment_ref": ref}

    def dump_network_summary(self, *, package_name: str | None = None) -> str:
        """拉取有界网络摘要（无 MITM 时的最小 network_trace 来源）。"""
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        parts: list[str] = []
        for args in (
            ["dumpsys", "connectivity"],
            ["dumpsys", "netstats", "detail", "full"],
        ):
            try:
                parts.append(self._shell_output_fn(list(args), 30.0))
            except Exception as exc:  # noqa: BLE001
                parts.append(f"{' '.join(args)}:unavailable:{type(exc).__name__}")
        text = "\n".join(parts).replace("\x00", "")
        pkg = (package_name or "").strip()
        if pkg:
            filtered = "\n".join(line for line in text.splitlines() if pkg in line)
            if filtered.strip():
                text = filtered
        return text[-8_000:]

    def current_runtime_identity(self) -> tuple[str, str | None] | None:
        """读取当前 Android 前台运行身份。"""
        if self._shell_output_fn is None:
            return None
        for args in (
            ["dumpsys", "activity", "activities"],
            ["dumpsys", "window", "windows"],
        ):
            try:
                identity = parse_android_runtime_identity(
                    self._shell_output_fn(list(args), 20.0)
                )
            except Exception:  # noqa: BLE001
                identity = None
            if identity is not None:
                return identity
        return None

    def tap(self, x: int, y: int) -> None:
        # 云手机直播看 tap 几乎看不见；开 show_touches 后用同点短 swipe 让圆点停约 240ms。
        # 坐标不变，不改成 pointer_location，避免顶栏挤走课纲栏位。
        self._ensure_live_touch_indicator()
        live_visible = self._live_touch_indicator_on()
        shell_args = tap_shell_args(x, y, live_visible=live_visible)
        log_app_tap_audit(
            "adb_tap",
            device_x=int(x),
            device_y=int(y),
            device_width=getattr(self, "width", None),
            device_height=getattr(self, "height", None),
            backend=getattr(self, "backend", None),
            serial=getattr(self, "serial", None),
            live_visible=live_visible,
            shell=" ".join(str(part) for part in shell_args),
        )
        self._shell_fn(shell_args, 20.0)
        self._log(f"tap actual=({int(x)},{int(y)}) shell={' '.join(str(p) for p in shell_args)}")

    def _live_touch_indicator_on(self) -> bool:
        return self.backend == BACKEND_CLOUD_PHONE and cloud_phone_live_touch_indicator_enabled()

    def _ensure_live_touch_indicator(self) -> None:
        if getattr(self, "_live_touch_indicator_ready", False):
            return
        if not self._live_touch_indicator_on():
            return
        try:
            self._shell_fn(["settings", "put", "system", "show_touches", "1"], 10.0)
            self._shell_fn(["settings", "put", "system", "pointer_location", "0"], 10.0)
            self._log("live_touch_indicator show_touches=1 pointer_location=0")
        except Exception as exc:  # noqa: BLE001 - 指示器失败不得挡住真实点击
            self._log(f"live_touch_indicator skipped: {exc}")
        self._live_touch_indicator_ready = True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, ms: int = 350) -> None:
        self._shell_fn(
            ["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms)],
            20.0,
        )

    def back(self) -> None:
        self._shell_fn(["input", "keyevent", "4"], 15.0)

    def type_text(self, text: str) -> None:
        """键入文字：ASCII 走 input text；CJK 走剪贴板 + PASTE。"""
        raw = str(text or "")
        if not raw:
            return
        if all(ord(ch) < 128 for ch in raw):
            escaped = (
                raw.replace("\\", "\\\\")
                .replace("%", "%%")
                .replace(" ", "%s")
                .replace("'", "\\'")
                .replace('"', '\\"')
                .replace("`", "\\`")
                .replace("$", "\\$")
                .replace("&", "\\&")
                .replace("|", "\\|")
                .replace(";", "\\;")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .replace("<", "\\<")
                .replace(">", "\\>")
            )
            self._shell_fn(["input", "text", escaped], 30.0)
            return
        self.set_clipboard(raw)
        # KEYCODE_PASTE = 279
        self._shell_fn(["input", "keyevent", "279"], 15.0)

    def wm_size(self) -> tuple[int, int]:
        return self.width, self.height

    def dump_foreground_identity(self) -> dict[str, str | None]:
        """从 dumpsys 解析当前前景 package / activity。"""
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        activity_dump = ""
        window_dump = ""
        try:
            activity_dump = self._shell_output_fn(
                ["dumpsys", "activity", "activities"], 20.0
            )
        except Exception as exc:  # noqa: BLE001
            activity_dump = f"unavailable:{type(exc).__name__}"
        try:
            window_dump = self._shell_output_fn(["dumpsys", "window"], 20.0)
        except Exception as exc:  # noqa: BLE001
            window_dump = f"unavailable:{type(exc).__name__}"
        from services.api.app.application.app_session_events import parse_focus_component
        from services.api.app.infrastructure.device.google_emulator import (
            parse_foreground_evidence_lines,
        )

        lines = parse_foreground_evidence_lines(
            window_dump=window_dump,
            activity_dump=activity_dump,
        )
        pkg, act = parse_focus_component(lines)
        return {
            "package_name": pkg,
            "activity": act,
            "focus_line": lines[0] if lines else None,
        }

    def dump_foreground_evidence_lines(self) -> list[str]:
        """汇总前景判定证据行。"""
        if self._shell_output_fn is None:
            raise RuntimeError("ADB 设备未提供 shell 输出通道")
        activity_dump = ""
        window_dump = ""
        try:
            activity_dump = str(
                self._shell_output_fn(["dumpsys", "activity", "activities"], 20.0) or ""
            )
        except Exception:  # noqa: BLE001
            activity_dump = ""
        try:
            window_dump = str(self._shell_output_fn(["dumpsys", "window"], 20.0) or "")
        except Exception:  # noqa: BLE001
            window_dump = ""
        from services.api.app.infrastructure.device.google_emulator import (
            parse_foreground_evidence_lines,
        )

        return parse_foreground_evidence_lines(
            window_dump=window_dump,
            activity_dump=activity_dump,
        )

    def dump_display_safe_area(self) -> dict[str, int | str]:
        """读取显示安全区；无 insets 时退回 wm size 全屏边界。"""
        from services.api.app.application.app_session_events import build_display_safe_area

        width, height = self.wm_size()
        area = build_display_safe_area(width=width, height=height)
        if self._shell_output_fn is None:
            return area
        try:
            raw = str(self._shell_output_fn(["dumpsys", "window", "displays"], 15.0) or "")
        except Exception:  # noqa: BLE001
            return area
        # 尽力解析 StableInsets=[left,top][right,bottom]；失败保持 display_bounds
        match = re.search(
            r"StableInsets=\[\s*(-?\d+),\s*(-?\d+)\s*\]\[\s*(-?\d+),\s*(-?\d+)\s*\]",
            raw,
        )
        if match is None:
            match = re.search(
                r"Insets\{left=(\d+),\s*top=(\d+),\s*right=(\d+),\s*bottom=(\d+)\}",
                raw,
            )
        if match is not None:
            left, top, right, bottom = (abs(int(match.group(i))) for i in range(1, 5))
            area.update(
                {
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": max(0, width - left - right),
                    "height": max(0, height - top - bottom),
                    "source": "window_insets",
                }
            )
        return area

    def query_display_rotation(self) -> int | None:
        """读取可证明的 display Surface 旋转；失败返回 None（不得猜测）。"""

        if self._shell_output_fn is None:
            return None
        blobs: list[str] = []
        for args in (
            ["dumpsys", "window", "displays"],
            ["dumpsys", "display"],
            ["dumpsys", "window"],
        ):
            try:
                blobs.append(str(self._shell_output_fn(args, 15.0) or ""))
            except Exception:  # noqa: BLE001 - 旁路读取不得阻断主路径
                continue
        return parse_display_rotation("\n".join(blobs))

    def capture_same_frame_observation(
        self,
        *,
        screenshot_dest: Path,
    ) -> dict[str, object]:
        """同会话紧邻截图＋UIA：共享 epoch，标明非内核级原子但仍同帧绑定。"""

        import time as _time
        import uuid

        epoch = f"same-frame:{uuid.uuid4().hex[:16]}"
        started = _time.perf_counter()
        identity_before = self.current_runtime_identity()
        screencap_trace = self.screencap(screenshot_dest)
        ui_xml: str | None = None
        ui_error: str | None = None
        try:
            ui_xml = self.dump_ui_hierarchy()
        except Exception as exc:  # noqa: BLE001 - 结构失败不得丢掉截图
            ui_error = f"{type(exc).__name__}: {exc}"
        identity_after = self.current_runtime_identity()
        width, height = self.wm_size()
        rotation = self.query_display_rotation()
        from services.api.app.infrastructure.device.android_surface_tech import (
            infer_android_surface_tech,
        )

        surface_tech = infer_android_surface_tech(ui_xml)
        return {
            "protocol_version": "app-same-frame-observation.v1",
            "observation_epoch": epoch,
            "atomic_kernel_guarantee": False,
            "same_frame_bound": True,
            "elapsed_ms": int(round((_time.perf_counter() - started) * 1000)),
            "screenshot_path": str(screenshot_dest),
            "screencap_trace": (
                screencap_trace.to_dict()
                if hasattr(screencap_trace, "to_dict")
                else {"path_mode": getattr(screencap_trace, "path_mode", None)}
            ),
            "ui_hierarchy_xml": ui_xml,
            "ui_hierarchy_error": ui_error,
            "ui_hierarchy_available": ui_xml is not None and "<hierarchy" in (ui_xml or ""),
            "display": {
                "width": width,
                "height": height,
                "rotation": rotation,
                "rotation_proven": rotation is not None,
            },
            "identity_before": (
                {"package_name": identity_before[0], "activity": identity_before[1]}
                if isinstance(identity_before, tuple) and identity_before
                else None
            ),
            "identity_after": (
                {"package_name": identity_after[0], "activity": identity_after[1]}
                if isinstance(identity_after, tuple) and identity_after
                else None
            ),
            "surface_tech": surface_tech,
            "replaces_formal_capture": False,
        }


def parse_wm_size(text: str) -> tuple[int, int]:
    """解析 `wm size` 输出。

    优先 ``Override size``（当前逻辑尺寸，常随横竖屏变化）；
    否则 ``Physical size``；再否则取首个 ``WxH``。注意 Physical 多为自然竖屏，
    横屏会话还需配合 :func:`resolve_wm_display_size` 按 rotation 校正。
    """

    payload = text or ""
    override = re.search(
        r"Override\s+size:\s*(\d+)\s*x\s*(\d+)",
        payload,
        flags=re.IGNORECASE,
    )
    if override is not None:
        return int(override.group(1)), int(override.group(2))
    physical = re.search(
        r"Physical\s+size:\s*(\d+)\s*x\s*(\d+)",
        payload,
        flags=re.IGNORECASE,
    )
    if physical is not None:
        return int(physical.group(1)), int(physical.group(2))
    match = re.search(r"(\d+)\s*x\s*(\d+)", payload)
    if not match:
        return CANONICAL_WIDTH, CANONICAL_HEIGHT
    return int(match.group(1)), int(match.group(2))


def align_wm_size_to_current_surface(
    width: int,
    height: int,
    *,
    display_rotation: int | None,
    wm_size_text: str = "",
) -> tuple[int, int]:
    """将自然方向 Physical 尺寸对齐到当前 Surface／input／uiautomator 坐标系。

    已出现 Override 时不得再交换轴。仅 Physical（或无 Override 的回退）
    且 rotation 为 90／270、尺寸为竖屏时交换为横屏。
    """

    if width <= 0 or height <= 0:
        return width, height
    if re.search(
        r"Override\s+size:\s*\d+\s*x\s*\d+",
        wm_size_text or "",
        flags=re.IGNORECASE,
    ):
        return width, height
    rot = normalize_display_rotation(display_rotation)
    if rot in {90, 270} and width < height:
        return height, width
    return width, height


def resolve_wm_display_size(
    text: str,
    *,
    display_rotation: int | None = None,
) -> tuple[int, int]:
    """解析并校正为当前显示坐标系尺寸（供 tap／UIA 校验共用）。"""

    width, height = parse_wm_size(text)
    return align_wm_size_to_current_surface(
        width,
        height,
        display_rotation=display_rotation,
        wm_size_text=text,
    )


def parse_android_runtime_identity(text: str) -> tuple[str, str | None] | None:
    """从 dumpsys activity/window 文本解析前台 package 和 activity。"""
    from services.api.app.application.app_session_events import parse_focus_component
    from services.api.app.infrastructure.device.google_emulator import (
        parse_foreground_evidence_lines,
    )

    payload = str(text or "")
    lines = parse_foreground_evidence_lines(
        activity_dump=payload,
        window_dump=payload,
    )
    package_name, activity = parse_focus_component(lines)
    if not package_name:
        return None
    return package_name, _normalize_android_activity(package_name, activity)


def _normalize_android_activity(
    package_name: str,
    activity: str | None,
) -> str | None:
    """将 `.MainActivity` 这类相对 Activity 统一成完整类名。"""
    act = (activity or "").strip()
    if not act:
        return None
    if act.startswith("."):
        return f"{package_name}{act}"
    return act


def parse_display_rotation(text: str) -> int | None:
    """从 dumpsys window／display 文本解析 Surface 旋转（度：0／90／180／270）。

    无法证明时返回 None；禁止把模糊文本猜成已校正。
    """

    raw = str(text or "")
    if not raw.strip():
        return None
    patterns = (
        r"mCurrentOrientation\s*=\s*(\d+)",
        r"SurfaceOrientation\s*[:=]\s*(\d+)",
        r"mRotation\s*=\s*(\d+)",
        r"\brotation\s*[:=]\s*(\d+)\b",
        r"displayRotation\s*[:=]\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match is None:
            continue
        value = int(match.group(1))
        # Android Surface：0..3 或已是角度。
        if value in {0, 1, 2, 3}:
            return (0, 90, 180, 270)[value]
        if value in {0, 90, 180, 270}:
            return value
    return None


def normalize_display_rotation(value: int | None) -> int | None:
    """把旋转规范化为 0／90／180／270；非法则视为未证明。"""

    if value is None:
        return None
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw in {0, 1, 2, 3}:
        return (0, 90, 180, 270)[raw]
    if raw % 360 in {0, 90, 180, 270}:
        return raw % 360
    return None


def apply_display_rotation_to_point(
    x: float,
    y: float,
    *,
    width: int,
    height: int,
    rotation: int,
) -> tuple[float, float]:
    """将相对 ROTATION_0 逻辑坐标映射到当前显示坐标（Android Surface 约定）。"""

    rot = normalize_display_rotation(rotation)
    if rot is None:
        raise CoordinateMappingError(
            "display rotation 无法规范化，拒绝仿射校正",
            error_code="ANDROID_POINT_ROTATION_INVALID",
        )
    if width <= 0 or height <= 0:
        raise CoordinateMappingError(
            "旋转仿射缺少有效宽高",
            error_code="ANDROID_POINT_DISPLAY_CONTEXT_INVALID",
        )
    if rot == 0:
        return float(x), float(y)
    if rot == 90:
        # (x,y) in natural WxH → current HxW
        return float(y), float(width - 1) - float(x)
    if rot == 180:
        return float(width - 1) - float(x), float(height - 1) - float(y)
    # 270
    return float(height - 1) - float(y), float(x)


def _capture_assumes_natural_orientation(
    *,
    base_w: int,
    base_h: int,
    device_w: int,
    device_h: int,
    rotation: int | None,
) -> bool:
    """截图基准像「未旋转自然方向」，而 wm size 已是当前方向时才需仿射。"""

    rot = normalize_display_rotation(rotation)
    if rot is None or rot == 0:
        return False
    if rot in {90, 270}:
        # 自然竖屏截图 + 当前横屏设备（或相反）
        return (base_w < base_h) != (device_w < device_h)
    # 180：同宽高比即可证明需翻转
    return abs((base_w / base_h) - (device_w / device_h)) <= _ASPECT_RATIO_LETTERBOX_TOLERANCE


def infer_centered_letterbox_content_rect(
    *,
    base_w: int,
    base_h: int,
    device_w: int,
    device_h: int,
) -> tuple[float, float, float, float, bool]:
    """在截图坐标系内推断居中 letterbox 内容矩形。

    返回 ``(left, top, width, height, letterboxed)``。宽高比一致时内容区即整张截图。
    """
    if base_w <= 0 or base_h <= 0 or device_w <= 0 or device_h <= 0:
        raise CoordinateMappingError(
            "坐标换算缺少有效截图或设备尺寸",
            error_code="ANDROID_POINT_DISPLAY_CONTEXT_INVALID",
        )
    capture_aspect = base_w / base_h
    device_aspect = device_w / device_h
    if abs(capture_aspect - device_aspect) <= _ASPECT_RATIO_LETTERBOX_TOLERANCE:
        return 0.0, 0.0, float(base_w), float(base_h), False
    if capture_aspect > device_aspect:
        # 截图更宽：左右黑边（pillarbox）
        content_h = float(base_h)
        content_w = content_h * device_aspect
        left = (base_w - content_w) / 2.0
        top = 0.0
    else:
        # 截图更高：上下黑边（letterbox）
        content_w = float(base_w)
        content_h = content_w / device_aspect
        left = 0.0
        top = (base_h - content_h) / 2.0
    if content_w <= 1.0 or content_h <= 1.0:
        raise CoordinateMappingError(
            "letterbox 内容矩形无效，拒绝猜测落点",
            error_code="ANDROID_POINT_LETTERBOX_UNRESOLVED",
        )
    return left, top, content_w, content_h, True


def map_point_to_device(
    x: float,
    y: float,
    *,
    device_w: int,
    device_h: int,
    base_w: int = CANONICAL_WIDTH,
    base_h: int = CANONICAL_HEIGHT,
    space: str = "canonical_px",
    display_rotation: int | None = None,
) -> MappedDevicePoint:
    """带 letterbox／旋转审计的坐标换算。

    ``device_px``／``normalized_01`` 不走 letterbox。
    ``capture_px``／``canonical_px``：宽高比显著不一致时按截图内居中内容区映射。
    点落在黑边内 → fail-closed。
    当 rotation 可证明且截图基准相对设备呈「自然方向」错位时，做 Surface 仿射校正。
    """
    rot = normalize_display_rotation(display_rotation)
    rotation_unproven = rot is None
    if space == "device_px":
        return MappedDevicePoint(
            x=int(round(x)),
            y=int(round(y)),
            letterboxed=False,
            rotation_unproven=rotation_unproven,
            display_rotation=rot,
            content_left=0.0,
            content_top=0.0,
            content_width=float(device_w),
            content_height=float(device_h),
        )
    if space == "normalized_01":
        return MappedDevicePoint(
            x=int(round(float(x) * device_w)),
            y=int(round(float(y) * device_h)),
            letterboxed=False,
            rotation_unproven=rotation_unproven,
            display_rotation=rot,
            content_left=0.0,
            content_top=0.0,
            content_width=float(device_w),
            content_height=float(device_h),
        )
    if space not in {"capture_px", "canonical_px"}:
        raise CoordinateMappingError(
            f"不支持的坐标空间：{space}",
            error_code="ANDROID_POINT_SPACE_UNSUPPORTED",
        )
    bw = base_w or CANONICAL_WIDTH
    bh = base_h or CANONICAL_HEIGHT
    apply_rotation = _capture_assumes_natural_orientation(
        base_w=bw,
        base_h=bh,
        device_w=device_w,
        device_h=device_h,
        rotation=rot,
    )
    # 需仿射时：先按「自然方向」设备尺寸做 letterbox，再旋转到当前 wm size。
    map_w, map_h = device_w, device_h
    if apply_rotation and rot in {90, 270}:
        map_w, map_h = device_h, device_w
    left, top, content_w, content_h, letterboxed = infer_centered_letterbox_content_rect(
        base_w=bw,
        base_h=bh,
        device_w=map_w,
        device_h=map_h,
    )
    # 黑边内的点不是设备内容，禁止映射猜测。
    if (
        x < left - 0.5
        or y < top - 0.5
        or x > left + content_w + 0.5
        or y > top + content_h + 0.5
    ):
        raise CoordinateMappingError(
            "截图坐标落在 letterbox 黑边内，拒绝映射到设备",
            error_code="ANDROID_POINT_IN_LETTERBOX",
        )
    nx = (float(x) - left) / content_w
    ny = (float(y) - top) / content_h
    mapped_x = nx * map_w
    mapped_y = ny * map_h
    if apply_rotation and rot is not None and rot != 0:
        mapped_x, mapped_y = apply_display_rotation_to_point(
            mapped_x,
            mapped_y,
            width=map_w,
            height=map_h,
            rotation=rot,
        )
    return MappedDevicePoint(
        x=int(round(mapped_x)),
        y=int(round(mapped_y)),
        letterboxed=letterboxed,
        rotation_unproven=rotation_unproven,
        display_rotation=rot,
        content_left=left,
        content_top=top,
        content_width=content_w,
        content_height=content_h,
    )


def map_point(
    x: float,
    y: float,
    *,
    device_w: int,
    device_h: int,
    base_w: int = CANONICAL_WIDTH,
    base_h: int = CANONICAL_HEIGHT,
    space: str = "canonical_px",
    display_rotation: int | None = None,
) -> tuple[int, int]:
    """座標換算（供 Adapter 與測試共用）。

    ``device_px``：已是裝置像素，不換算。
    ``normalized_01``：相對裝置邏輯尺寸。
    ``capture_px``／``canonical_px``：相對 ``base_w``×``base_h`` 映射到裝置；
    寬高比顯著不一致時按居中 letterbox 內容區換算。
    """
    mapped = map_point_to_device(
        x,
        y,
        device_w=device_w,
        device_h=device_h,
        base_w=base_w,
        base_h=base_h,
        space=space,
        display_rotation=display_rotation,
    )
    return mapped.x, mapped.y


def assert_device_point_in_safe_area(
    x: int,
    y: int,
    *,
    device_w: int,
    device_h: int,
    safe_area: DisplaySafeArea | Mapping[str, object] | None,
) -> None:
    """系统栏 inset 外的点一律拒绝，避免 silently 夹到状态栏／导航栏。"""
    area = (
        safe_area
        if isinstance(safe_area, DisplaySafeArea)
        else DisplaySafeArea.from_mapping(safe_area)
    )
    if area is None:
        return
    # source=display_bounds 且 insets 全 0 时等于全屏，不额外拒绝。
    if area.left == 0 and area.top == 0 and area.right == 0 and area.bottom == 0:
        return
    if not area.contains_device_point(x, y, device_w=device_w, device_h=device_h):
        raise CoordinateMappingError(
            f"设备落点 ({x}, {y}) 位于系统栏 inset 外，拒绝点击",
            error_code="ANDROID_POINT_OUTSIDE_SAFE_AREA",
        )


def read_png_ihdr_dimensions(png: bytes) -> tuple[int, int] | None:
    """從 PNG IHDR 讀取像素尺寸；非 PNG 或殘缺時回傳 None。"""
    if len(png) < 24 or not png.startswith(PNG_MAGIC) or png[12:16] != b"IHDR":
        return None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def screencap_exec_out(
    adb_path: Path,
    serial: str,
    dest: Path,
    *,
    prefer_remote_pull: bool = False,
    remote_pull_timeout_seconds: float | None = None,
) -> ScreencapStageTrace:
    """拉取 PNG；公网云手机可跳过易挂起的 exec-out，直接远端落盘。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    trace = ScreencapStageTrace(path_mode="remote_pull" if prefer_remote_pull else "exec_out")
    if not prefer_remote_pull:
        cmd = [str(adb_path), "-s", serial, "exec-out", "screencap", "-p"]
        exec_started = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=_SCREENSHOT_EXEC_OUT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # 普通远端 ADB 的 exec-out 可能只挂住该通道；设备 shell 仍可用时，
            # 应继续走远端落盘 + pull，而不是把可恢复截图误报为 502。
            result = subprocess.CompletedProcess(
                cmd,
                124,
                stdout=b"",
                stderr=b"exec-out timeout",
            )
        trace.exec_out_ms = _elapsed_ms(exec_started)
        data = result.stdout or b""
        if result.returncode == 0 and data.startswith(PNG_MAGIC):
            dest.write_bytes(data)
            trace.path_mode = "exec_out"
            trace.png_bytes = len(data)
            return trace
        fixed = data.replace(b"\r\n", b"\n")
        if result.returncode == 0 and fixed.startswith(PNG_MAGIC):
            dest.write_bytes(fixed)
            trace.path_mode = "exec_out"
            trace.png_bytes = len(fixed)
            return trace
        trace.path_mode = "remote_pull_fallback"
    remote = "/sdcard/alioth_agent_cap.png"
    pull_timeout_seconds = (
        _SCREENSHOT_PULL_TIMEOUT_SECONDS
        if remote_pull_timeout_seconds is None
        else max(1.0, float(remote_pull_timeout_seconds))
    )
    try:
        remote_started = time.perf_counter()
        remote_result = subprocess.run(
            [str(adb_path), "-s", serial, "shell", "screencap", "-p", remote],
            capture_output=True,
            timeout=_SCREENSHOT_REMOTE_TIMEOUT_SECONDS,
            check=False,
        )
        trace.remote_screencap_ms = _elapsed_ms(remote_started)
        if remote_result.returncode != 0:
            detail = (remote_result.stderr or b"").decode("utf-8", errors="replace")[:200]
            trace.error_code = "remote_screencap_failed"
            raise RuntimeError(f"adb shell screencap 失败：{detail or 'unknown error'}")
        pull_started = time.perf_counter()
        pull_result = subprocess.run(
            [str(adb_path), "-s", serial, "pull", remote, str(dest)],
            capture_output=True,
            timeout=pull_timeout_seconds,
            check=False,
        )
        trace.adb_pull_ms = _elapsed_ms(pull_started)
        if pull_result.returncode != 0:
            detail = (pull_result.stderr or b"").decode("utf-8", errors="replace")[:200]
            trace.error_code = "adb_pull_failed"
            raise RuntimeError(f"adb pull screencap 失败：{detail or 'unknown error'}")
    finally:
        cleanup_started = time.perf_counter()
        try:
            subprocess.run(
                [str(adb_path), "-s", serial, "shell", "rm", "-f", remote],
                capture_output=True,
                timeout=_SCREENSHOT_CLEANUP_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        trace.cleanup_ms = _elapsed_ms(cleanup_started)
    if (
        not dest.is_file()
        or dest.stat().st_size < 100
        or not dest.read_bytes().startswith(PNG_MAGIC)
    ):
        trace.error_code = "invalid_pulled_png"
        raise RuntimeError(f"adb pull 未生成有效截图：{dest}")
    trace.png_bytes = int(dest.stat().st_size)
    return trace


def open_adb_device_ops(
    backend: str,
    *,
    ld_index: int = 0,
    log: Callable[[str], None] | None = None,
    cloud_phone_controller: Any | None = None,
    remote_pull_timeout_seconds: float | None = None,
) -> AdbDeviceOps:
    """依 Demo 後端開啟唯一一組 ADB 原語。"""
    logger = log or (lambda _msg: None)
    if backend == BACKEND_LDPLAYER:
        from services.api.app.infrastructure.device.ldplayer import LdPlayerController

        ctl = LdPlayerController(index=ld_index)

        def screencap_fn(dest: Path) -> ScreencapStageTrace | None:
            ctl.screencap_to_file(dest)
            return ScreencapStageTrace(
                path_mode="ldplayer_native",
                png_bytes=int(dest.stat().st_size) if dest.is_file() else 0,
            )

        def shell_fn(args: list[str], timeout: float) -> None:
            _require_shell_success(
                ctl.adb("shell " + " ".join(args), timeout_seconds=timeout)
            )

        def shell_output_fn(args: list[str], timeout: float) -> str:
            response = _require_shell_success(
                ctl.adb("shell " + " ".join(args), timeout_seconds=timeout)
            )
            return "\n".join(part for part in (response.stdout, response.stderr) if part)

        def size_fn() -> tuple[int, int]:
            response = ctl.adb("shell wm size", timeout_seconds=15.0)
            text = "\n".join(part for part in (response.stdout, response.stderr) if part)
            return resolve_wm_display_size(text)

        width, height = size_fn()
        logger(f"adb ops backend={backend} size={width}x{height}")
        return AdbDeviceOps(
            backend=backend,
            width=width,
            height=height,
            _log=logger,
            _screencap_fn=screencap_fn,
            _shell_fn=shell_fn,
            _shell_output_fn=shell_output_fn,
        )

    if backend == BACKEND_CLOUD_PHONE:
        from services.api.app.infrastructure.device.cloud_phone import CloudPhoneController
        from services.api.app.infrastructure.device.morelogin_device_api import (
            MoreLoginShellCommandUnsupportedError,
            morelogin_execute_shell_command,
            morelogin_screenshot_png_bytes,
            morelogin_screenshot_png_bytes_via_url,
            resolve_cloud_phone_morelogin_device_api_mode,
            shell_args_to_command,
            should_use_morelogin_device_api,
        )

        ctl = cloud_phone_controller or CloudPhoneController()
        serial = str(getattr(ctl, "_serial", None) or "").strip()
        if not serial:
            serial, _logs = ctl.ensure_ready()
        logger(f"adb ops cloud_phone serial={serial}")
        adb_path = ctl.adb_path
        morelogin_env_id = str(getattr(ctl, "_morelogin_env_id", None) or "").strip()
        morelogin_api_mode = resolve_cloud_phone_morelogin_device_api_mode()
        use_morelogin_api = should_use_morelogin_device_api(
            morelogin_env_id,
            mode=morelogin_api_mode,
        )
        if use_morelogin_api:
            logger(
                f"adb ops cloud_phone morelogin_api mode={morelogin_api_mode} "
                f"env_id={morelogin_env_id}"
            )

        def _screencap_via_adb(dest: Path) -> ScreencapStageTrace | None:
            # MoreLogin 公网 ADB 的 exec-out 长连接可能随会话老化后持续挂起，
            # 设备端落盘 + pull 在相同连接上仍可稳定完成；云手机固定优先该路径。
            pull_budget = (
                _SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS
                if remote_pull_timeout_seconds is None
                else max(1.0, float(remote_pull_timeout_seconds))
            )
            return screencap_exec_out(
                adb_path,
                serial,
                dest,
                prefer_remote_pull=True,
                remote_pull_timeout_seconds=pull_budget,
            )

        def _screencap_via_morelogin_api(dest: Path) -> ScreencapStageTrace | None:
            started = time.perf_counter()
            timeout = max(
                30.0,
                float(remote_pull_timeout_seconds or _SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS),
            )
            try:
                png = morelogin_screenshot_png_bytes(
                    morelogin_env_id,
                    timeout_seconds=timeout,
                )
            except Exception as base64_exc:  # noqa: BLE001
                logger(
                    f"morelogin screenCapBase64 failed: {type(base64_exc).__name__}; "
                    "try screenCap url"
                )
                png = morelogin_screenshot_png_bytes_via_url(
                    morelogin_env_id,
                    timeout_seconds=timeout,
                )
            dest.write_bytes(png)
            elapsed = _elapsed_ms(started)
            return ScreencapStageTrace(
                path_mode="morelogin_api_screenCapBase64",
                png_bytes=len(png),
                adb_pull_ms=elapsed,
            )

        def screencap_fn(dest: Path) -> ScreencapStageTrace | None:
            if use_morelogin_api:
                try:
                    trace = _screencap_via_morelogin_api(dest)
                    # API 常回預覽縮圖（如 322×572）；與 wm size 不一致時改 ADB 全分辨率，
                    # 避免視覺候選座標與 input tap 裝置像素錯位。
                    try:
                        dims = read_png_ihdr_dimensions(dest.read_bytes())
                        if dims is not None:
                            display_w, display_h = size_fn()
                            if dims != (display_w, display_h):
                                logger(
                                    "morelogin api screencap downscaled "
                                    f"{dims[0]}x{dims[1]} != display "
                                    f"{display_w}x{display_h}; fallback adb"
                                )
                                try:
                                    return _screencap_via_adb(dest)
                                except Exception as adb_exc:  # noqa: BLE001
                                    logger(
                                        "morelogin downscaled adb fallback failed: "
                                        f"{type(adb_exc).__name__}; keep api bytes"
                                    )
                                    return trace
                    except Exception as size_exc:  # noqa: BLE001
                        logger(
                            "morelogin api screencap size check failed: "
                            f"{type(size_exc).__name__}; keep api bytes"
                        )
                    return trace
                except Exception as exc:  # noqa: BLE001
                    if morelogin_api_mode == "api":
                        raise
                    logger(
                        f"morelogin api screencap fallback adb: {type(exc).__name__}"
                    )
            return _screencap_via_adb(dest)

        def _shell_via_adb(args: list[str], timeout: float) -> str:
            response = _require_shell_success(
                ctl._adb(
                    ["-s", serial, "shell", *args], timeout_seconds=timeout
                )
            )
            return "\n".join(part for part in (response.stdout, response.stderr) if part)

        # 少数云手机镜像的 MoreLogin Agent 不放行 shell，命中后本会话不再重试 API。
        shell_api_blocked: dict[str, bool] = {"value": False}

        def _shell_api_available() -> bool:
            return use_morelogin_api and not shell_api_blocked["value"]

        def _note_shell_api_blocked(command_hint: str) -> None:
            if shell_api_blocked["value"]:
                return
            shell_api_blocked["value"] = True
            logger(
                "morelogin api shell unsupported by device agent; "
                f"switch to adb for this session (first={command_hint})"
            )

        def _shell_via_morelogin_api(args: list[str], timeout: float) -> str:
            return morelogin_execute_shell_command(
                morelogin_env_id,
                shell_args_to_command(args),
                timeout_seconds=max(5.0, float(timeout)),
            )

        def shell_fn(args: list[str], timeout: float) -> None:
            if _shell_api_available():
                try:
                    _shell_via_morelogin_api(args, timeout)
                    return
                except MoreLoginShellCommandUnsupportedError:
                    if morelogin_api_mode == "api":
                        raise
                    _note_shell_api_blocked(" ".join(args[:2]))
                except Exception as exc:  # noqa: BLE001
                    if morelogin_api_mode == "api":
                        raise
                    logger(
                        f"morelogin api shell fallback adb: {type(exc).__name__}"
                    )
            _require_shell_success(
                ctl._adb(["-s", serial, "shell", *args], timeout_seconds=timeout)
            )

        def shell_output_fn(args: list[str], timeout: float) -> str:
            if _shell_api_available():
                try:
                    return _shell_via_morelogin_api(args, timeout)
                except MoreLoginShellCommandUnsupportedError:
                    if morelogin_api_mode == "api":
                        raise
                    _note_shell_api_blocked(" ".join(args[:2]))
                except Exception as exc:  # noqa: BLE001
                    if morelogin_api_mode == "api":
                        raise
                    logger(
                        f"morelogin api shell_output fallback adb: {type(exc).__name__}"
                    )
            return _shell_via_adb(args, timeout)

        def size_fn() -> tuple[int, int]:
            response = ctl._adb(["-s", serial, "shell", "wm", "size"], timeout_seconds=15.0)
            text = "\n".join(part for part in (response.stdout, response.stderr) if part)
            return resolve_wm_display_size(text)

        width, height = size_fn()
        logger(f"adb ops backend={backend} size={width}x{height}")
        return AdbDeviceOps(
            backend=backend,
            width=width,
            height=height,
            _log=logger,
            _screencap_fn=screencap_fn,
            _shell_fn=shell_fn,
            _shell_output_fn=shell_output_fn,
        )

    if backend == BACKEND_GOOGLE_EMULATOR:
        from services.api.app.infrastructure.device.google_emulator import (
            GoogleEmulatorController,
        )

        ctl = GoogleEmulatorController()
        serial, _ = ctl.ensure_ready()
        adb_path = ctl.adb_path

        def screencap_fn(dest: Path) -> ScreencapStageTrace | None:
            return screencap_exec_out(adb_path, serial, dest)

        def shell_fn(args: list[str], timeout: float) -> None:
            _require_shell_success(
                ctl._run_adb(["shell", *args], serial=serial, timeout_seconds=timeout)
            )

        def shell_output_fn(args: list[str], timeout: float) -> str:
            response = _require_shell_success(
                ctl._run_adb(
                    ["shell", *args], serial=serial, timeout_seconds=timeout
                )
            )
            return "\n".join(part for part in (response.stdout, response.stderr) if part)

        def size_fn() -> tuple[int, int]:
            response = ctl._run_adb(
                ["shell", "wm", "size"],
                serial=serial,
                timeout_seconds=15.0,
            )
            text = "\n".join(part for part in (response.stdout, response.stderr) if part)
            return resolve_wm_display_size(text)

        width, height = size_fn()
        logger(f"adb ops backend={backend} size={width}x{height}")
        return AdbDeviceOps(
            backend=backend,
            width=width,
            height=height,
            _log=logger,
            _screencap_fn=screencap_fn,
            _shell_fn=shell_fn,
            _shell_output_fn=shell_output_fn,
        )

    if backend != BACKEND_REDROID:
        # 未知後端以 redroid 相容路徑兜底（與歷史 agent 行為一致）
        logger(f"adb ops unknown backend={backend!r}, fallback redroid")

    from services.api.app.infrastructure.device.redroid import RedroidController

    ctl = RedroidController()
    serial, _ = ctl.ensure_ready()
    adb_path = ctl.adb_path
    logger(f"adb ops redroid serial={serial}")

    def screencap_fn(dest: Path) -> ScreencapStageTrace | None:
        return screencap_exec_out(adb_path, serial, dest)

    def shell_fn(args: list[str], timeout: float) -> None:
        _require_shell_success(
            ctl._emu._run_adb(["shell", *args], serial=serial, timeout_seconds=timeout)
        )

    def shell_output_fn(args: list[str], timeout: float) -> str:
        response = _require_shell_success(
            ctl._emu._run_adb(
                ["shell", *args], serial=serial, timeout_seconds=timeout
            )
        )
        return "\n".join(part for part in (response.stdout, response.stderr) if part)

    def size_fn() -> tuple[int, int]:
        response = ctl._emu._run_adb(
            ["shell", "wm", "size"],
            serial=serial,
            timeout_seconds=15.0,
        )
        text = "\n".join(part for part in (response.stdout, response.stderr) if part)
        return resolve_wm_display_size(text)

    width, height = size_fn()
    logger(f"adb ops backend={BACKEND_REDROID if backend != BACKEND_REDROID else backend} size={width}x{height}")
    return AdbDeviceOps(
        backend=BACKEND_REDROID if backend != BACKEND_REDROID else backend,
        width=width,
        height=height,
        _log=logger,
        _screencap_fn=screencap_fn,
        _shell_fn=shell_fn,
        _shell_output_fn=shell_output_fn,
    )
