"""星語 Official 裝置邊界：乾跑／真實雲手機。"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.xingyu_runtime.action_normalize import (
    normalize_action_type,
    normalize_execute_parameters,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.device.adb_ops import (
    _CAPTURE_IN_FRAME_FAILURE_RETRIES,
    _CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS,
    _SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS,
    AdbDeviceOps,
    open_adb_device_ops,
)
from services.api.app.infrastructure.device.adb_session_health import (
    AdbSessionHealthState,
    recover_cloud_phone_adb_binding,
    run_with_cloud_phone_adb_reconnect,
)
from services.api.app.infrastructure.device.android_runtime_observation import (
    AndroidRuntimeObservation,
    parse_android_ui_hierarchy,
)
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
)
from services.api.app.infrastructure.device.app_tap_audit_log import log_app_tap_audit
from services.api.app.infrastructure.device.backend import BACKEND_CLOUD_PHONE

logger = logging.getLogger(__name__)

_CAPTURE_IDENTITY_ATTEMPTS = 3
_CAPTURE_IDENTITY_SETTLE_SECONDS = 0.75

# Capture 刷新 ADB 不得占用云手机 boot 的 120s 预算，否则必然撑爆 Official HTTP 超时。
# 具体超时由 adb_session_health.refresh_cloud_phone_adb_binding 统一管理。

# 1×1 透明 PNG（乾跑截圖）
_DRY_RUN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """读取 PNG IHDR 像素尺寸，禁止用设备逻辑尺寸冒充截图尺寸。"""

    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n") or png[12:16] != b"IHDR":
        raise RuntimeError("screencap 未生成有效 PNG")
    width = int.from_bytes(png[16:20], byteorder="big")
    height = int.from_bytes(png[20:24], byteorder="big")
    if width <= 0 or height <= 0:
        raise RuntimeError("screencap PNG 像素尺寸无效")
    return width, height


@dataclass(frozen=True)
class CaptureShot:
    """截圖結果（二進位 + 元資料）。"""

    png_bytes: bytes
    width: int
    height: int
    orientation: str
    package_name: str | None = None
    activity: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png_bytes).hexdigest()


@dataclass(frozen=True)
class ActionOutcome:
    """動作執行結果與審計軌跡。"""

    succeeded: bool
    retryable: bool
    error_code: str | None
    action_trace: tuple[dict[str, Any], ...]


class XingyuDeviceBridge(Protocol):
    """Official 服務持有的裝置埠。"""

    def start(
        self,
        *,
        backend: str,
        morelogin_env_id: str | None = None,
        clear_leftover: bool = True,
    ) -> tuple[str | None, Any]:
        """就緒裝置；回傳 (serial, opaque_device)。

        ``clear_leftover=True`` 仅用于新 session 建会（清上一颗 APK）。
        store-loss／跨 Worker reattach 必须传 False，禁止误杀当前任务前台。
        """

    def install(
        self,
        device: Any,
        apk_path: Path,
        *,
        package_hint: str | None,
        same_label_skip_authorization: str | None = None,
    ) -> str:
        """安裝並回傳 installed_package。"""

    def open_app(
        self,
        device: Any,
        package_name: str,
        *,
        cold_start: bool,
        activity: str | None,
    ) -> tuple[bool, str | None]:
        """開啟 App；回傳 (foreground_verified, activity)。"""

    def capture(self, device: Any, *, package_name: str | None, activity: str | None) -> CaptureShot:
        """截取 PNG。"""

    def dump_ui_tree(self, device: Any) -> str:
        """拉取 UI Hierarchy XML。"""

    def dump_device_log(self, device: Any, *, package_name: str | None) -> str:
        """拉取有界 logcat。"""

    def dump_network_trace(
        self,
        device: Any,
        *,
        package_name: str | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """拉取有界 network_trace 载荷。"""

    def dump_fault_artifact(self, device: Any, *, package_name: str | None) -> dict[str, Any]:
        """拉取有界 crash/ANR 载荷。"""

    def execute(
        self,
        device: Any,
        *,
        action_type: str,
        parameters: dict[str, Any],
        semantic_target: str | None,
    ) -> ActionOutcome:
        """執行 tap／scroll／back／observe／deep_link／warm_restore／reset_app。"""

    def close(self, device: Any, *, package_name: str | None, reason: str) -> None:
        """釋放裝置綁定（可選 force-stop）。"""


class DryRunDeviceBridge:
    """不連真實裝置的契約／單測橋接。"""

    def start(
        self,
        *,
        backend: str,
        morelogin_env_id: str | None = None,
        clear_leftover: bool = True,
    ) -> tuple[str | None, Any]:
        del morelogin_env_id, clear_leftover
        return f"dryrun:{backend}", {"backend": backend, "kind": "dry_run"}

    def install(
        self,
        device: Any,
        apk_path: Path,
        *,
        package_hint: str | None,
        same_label_skip_authorization: str | None = None,
    ) -> str:
        del device, apk_path, same_label_skip_authorization
        return (package_hint or "com.example.dryrun").strip()

    def open_app(
        self,
        device: Any,
        package_name: str,
        *,
        cold_start: bool,
        activity: str | None,
    ) -> tuple[bool, str | None]:
        del device
        suffix = "cold" if cold_start else "warm"
        return True, activity or f"{package_name}/.MainActivity#{suffix}"

    def capture(self, device: Any, *, package_name: str | None, activity: str | None) -> CaptureShot:
        del device
        return CaptureShot(
            png_bytes=_DRY_RUN_PNG,
            width=1,
            height=1,
            orientation="portrait",
            package_name=package_name,
            activity=activity,
        )

    def dump_ui_tree(self, device: Any) -> str:
        del device
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<hierarchy rotation="0">'
            '<node index="0" text="Home" class="android.widget.FrameLayout" '
            'package="com.example.dryrun" resource-id="com.example.dryrun:id/root" '
            'clickable="true" bounds="[0,0][1080,1920]"/>'
            "</hierarchy>\n"
        )

    def dump_device_log(self, device: Any, *, package_name: str | None) -> str:
        del device
        pkg = package_name or "com.example.dryrun"
        return f"I/ActivityManager: Start proc {pkg} for activity {pkg}/.MainActivity\n"

    def dump_network_trace(
        self,
        device: Any,
        *,
        package_name: str | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        del device
        from services.api.app.application.app_network_proxy import get_app_network_proxy_hub

        hub = get_app_network_proxy_hub()
        if session_id:
            hub.bind_session(session_id)
            hub.record_entry(
                session_id,
                {
                    "method": "GET",
                    "url_host": "example.invalid",
                    "path": "/health",
                    "status_code": 200,
                    "bytes": 128,
                },
            )
            return hub.build_network_trace(
                session_id=session_id,
                package_name=package_name,
            )
        from services.api.app.application.app_surface_evidence import (
            bound_network_trace_payload,
        )

        return bound_network_trace_payload(
            package_name=package_name,
            proxy_attached=False,
            entries=(
                {
                    "method": "GET",
                    "url_host": "example.invalid",
                    "path": "/health",
                    "status_code": 200,
                    "bytes": 128,
                },
            ),
            status="captured",
            reason="dry_run_summary",
        )

    def dump_fault_artifact(self, device: Any, *, package_name: str | None) -> dict[str, Any]:
        del device
        from services.api.app.application.app_surface_evidence import (
            bound_fault_artifact_payload,
        )

        return bound_fault_artifact_payload(
            raw_text="",
            package_name=package_name,
            fault_kind="none",
            status="captured",
            reason="dry_run_no_fault",
        )

    def execute(
        self,
        device: Any,
        *,
        action_type: str,
        parameters: dict[str, Any],
        semantic_target: str | None,
    ) -> ActionOutcome:
        del device, semantic_target
        op = normalize_action_type(action_type)
        if op == "input":
            return ActionOutcome(
                succeeded=False,
                retryable=False,
                error_code="ACTION_UNSUPPORTED",
                action_trace=({"op": op, "result": "unsupported"},),
            )
        if op not in {
            "tap",
            "scroll",
            "back",
            "observe",
            "timed_capture",
            "deep_link",
            "warm_restore",
            "reset_app",
        }:
            return ActionOutcome(
                succeeded=False,
                retryable=False,
                error_code="ACTION_UNSUPPORTED",
                action_trace=({"op": op, "result": "unsupported"},),
            )
        return ActionOutcome(
            succeeded=True,
            retryable=False,
            error_code=None,
            action_trace=({"op": op, "parameters": parameters, "result": "ok"},),
        )

    def close(self, device: Any, *, package_name: str | None, reason: str) -> None:
        del device, package_name, reason


@dataclass
class _LiveDevice:
    """真實裝置會話：控制器 + ADB 原語。"""

    backend: str
    serial: str
    ops: AdbDeviceOps
    controller: Any
    resolver: CanonicalPointTargetResolver
    capture_refresh_required: bool = False
    adb_session_health: AdbSessionHealthState = field(default_factory=AdbSessionHealthState)


class LiveCloudPhoneDeviceBridge:
    """接 CloudPhoneController + AdbDeviceOps。"""

    def start(
        self,
        *,
        backend: str,
        morelogin_env_id: str | None = None,
        clear_leftover: bool = True,
    ) -> tuple[str | None, Any]:
        chosen = (backend or BACKEND_CLOUD_PHONE).strip().lower() or BACKEND_CLOUD_PHONE
        if chosen != BACKEND_CLOUD_PHONE:
            # 第一版僅雲手機；其餘後端可後續擴展
            raise RuntimeError(f"Official 暫不支援 backend={chosen}")
        from services.api.app.application.device_pool import allow_global_morelogin_fallback
        from services.api.app.infrastructure.device.cloud_phone import CloudPhoneController

        env_id = str(morelogin_env_id or "").strip()
        allow_fallback = allow_global_morelogin_fallback()
        if not env_id and not allow_fallback:
            raise RuntimeError(
                "多机路径缺少租约 morelogin_env_id，拒绝使用全域 ALIOTH_MORELOGIN_CLOUDPHONE_ID"
            )
        ctl = CloudPhoneController(
            morelogin_env_id=env_id or None,
            allow_global_morelogin_fallback=allow_fallback,
            apply_morelogin_to_process_env=False,
        )
        serial, logs = ctl.ensure_ready()
        for line in logs:
            logger.info("xingyu_official start_session %s", line)
        ops = open_adb_device_ops(
            BACKEND_CLOUD_PHONE,
            log=lambda msg: logger.info("%s", msg),
            cloud_phone_controller=ctl,
        )
        # 同机连跑：上一颗 APK 若未收口关掉，会占前台并导致本 session 建会 503。
        # store-loss／reattach 重建绑定禁止清残留，否则会 force-stop 当前任务 App 并 HOME。
        if clear_leftover:
            try:
                for line in ops.clear_leftover_qc_app():
                    logger.info("xingyu_official start_session leftover_cleanup %s", line)
            except Exception as exc:  # noqa: BLE001 - 清理失败仍继续建会，由后续步骤 fail-closed
                logger.info(
                    "xingyu_official start_session leftover_cleanup skipped: %s",
                    type(exc).__name__,
                )
        else:
            logger.info(
                "xingyu_official start_session leftover_cleanup skipped: preserve_active_task"
            )
        resolver = CanonicalPointTargetResolver.from_device_ops(ops)
        device = _LiveDevice(
            backend=BACKEND_CLOUD_PHONE,
            serial=serial,
            ops=ops,
            controller=ctl,
            resolver=resolver,
        )
        return serial, device

    def install(
        self,
        device: Any,
        apk_path: Path,
        *,
        package_hint: str | None,
        same_label_skip_authorization: str | None = None,
    ) -> str:
        live = _as_live(device)
        logs, pkg, _reused = live.controller.ensure_apk_installed(
            apk_path,
            same_label_skip_authorization=same_label_skip_authorization,
        )
        logger.info("xingyu_official install done package=%s", pkg)
        for line in (logs or "").splitlines()[:20]:
            logger.info("xingyu_official install %s", line)
        extracted = (pkg or "").strip()
        hint = (package_hint or "").strip()
        installed = extracted or hint
        # 任務側 package_hint 若已在裝置上，優先採用，避免 Manifest 誤掃成 OEM／設備 ID 包名後無法打開。
        if hint and hint != extracted:
            checker = None
            emu = getattr(live.controller, "_emu", None)
            if emu is not None:
                checker = getattr(emu, "is_package_installed", None)
            if not callable(checker):
                checker = getattr(live.controller, "is_package_installed", None)
            try:
                if callable(checker) and checker(hint):
                    logger.warning(
                        "xingyu_official install prefer package_hint=%s over extracted=%s",
                        hint,
                        extracted or "(empty)",
                    )
                    installed = hint
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "xingyu_official install hint verify skipped: %s",
                    type(exc).__name__,
                )
        if not installed:
            raise RuntimeError("安裝成功但無法解析 package_name")
        return installed

    def open_app(
        self,
        device: Any,
        package_name: str,
        *,
        cold_start: bool,
        activity: str | None,
    ) -> tuple[bool, str | None]:
        live = _as_live(device)
        pkg = str(package_name or "").strip()
        if cold_start:
            try:
                live.ops.force_stop(pkg)
            except Exception as exc:  # noqa: BLE001
                logger.info("xingyu_official cold force-stop skipped: %s", exc)
            prep = live.controller.prepare_app_for_script(pkg, require_foreground=True)
            logger.info("xingyu_official open cold package=%s", pkg)
            for line in (prep or "").splitlines()[:30]:
                logger.info("xingyu_official open %s", line)
            try:
                for line in live.ops.force_portrait_lock():
                    logger.info("xingyu_official open portrait %s", line)
            except Exception as exc:  # noqa: BLE001 - 直屏锁失败不阻断打开
                logger.info("xingyu_official open portrait skipped: %s", type(exc).__name__)
        else:
            try:
                live.ops.warm_bring_to_front(pkg, activity=activity)
            except Exception as exc:  # noqa: BLE001
                logger.info("xingyu_official warm restore fallback prepare: %s", exc)
                live.controller.prepare_app_for_script(pkg, require_foreground=True)
            logger.info("xingyu_official open warm package=%s", pkg)
            try:
                for line in live.ops.force_portrait_lock():
                    logger.info("xingyu_official open portrait %s", line)
            except Exception as exc:  # noqa: BLE001
                logger.info("xingyu_official open portrait skipped: %s", type(exc).__name__)
        focus = ""
        try:
            focus = live.controller._emu.current_focus_line()  # noqa: SLF001 - 前景證據
        except Exception:  # noqa: BLE001
            focus = ""
        return True, focus or activity or None

    def capture(self, device: Any, *, package_name: str | None, activity: str | None) -> CaptureShot:
        live = _as_live(device)
        # 每次截图前重锁直屏。开屏广告和浏览器会在会话中途把云手机转横，只在 open 时锁一次不够。
        _reassert_portrait_lock(live, where="capture")
        # MoreLogin 公网 ADB 可能在首帧后的 Agent 思考窗口内老化。
        # 若上一帧已标记需刷新，Capture 开始前先短超时刷新并重建 ops。
        if live.capture_refresh_required:
            _refresh_capture_binding(live, health_reason="capture_failure")
        else:
            live.adb_session_health.last_refreshed = False
            should, reason = live.adb_session_health.should_proactive_refresh()
            if should:
                try:
                    _refresh_capture_binding(live, health_reason=reason or "proactive")
                except Exception as exc:  # noqa: BLE001 - 预防刷新失败仍尝试本帧截图
                    logger.info(
                        "xingyu_official proactive adb refresh skipped reason=%s error=%s",
                        reason,
                        type(exc).__name__,
                    )
        with tempfile.TemporaryDirectory(prefix="xingyu-cap-") as tmp:
            dest = Path(tmp) / f"cap_{uuid4().hex[:10]}.png"
            runtime_identity = None
            for attempt in range(_CAPTURE_IDENTITY_ATTEMPTS):
                before_identity = _read_capture_runtime_identity(live)
                _capture_live_png_with_retry(live, dest)
                runtime_identity = _read_capture_runtime_identity(live)
                pull_ms = (
                    live.ops.last_screencap_trace.adb_pull_ms
                    if live.ops.last_screencap_trace is not None
                    else None
                )
                live.adb_session_health.record_pull(pull_ms)
                if before_identity == runtime_identity:
                    break
                # 先截旧 App、拉图期间切到 Chrome 时，必须重截，不能拼接两时刻事实。
                logger.info(
                    "xingyu_official capture foreground changed before=%s after=%s "
                    "attempt=%s/%s pull_ms=%s",
                    before_identity,
                    runtime_identity,
                    attempt + 1,
                    _CAPTURE_IDENTITY_ATTEMPTS,
                    pull_ms,
                )
                if attempt + 1 == _CAPTURE_IDENTITY_ATTEMPTS:
                    raise RuntimeError(
                        "APP_CAPTURE_FOREGROUND_UNSTABLE: 截图期间前台身份持续变化"
                    )
                time.sleep(_CAPTURE_IDENTITY_SETTLE_SECONDS)
            live.capture_refresh_required = False
            png = dest.read_bytes()
            try:
                width, height = _png_dimensions(png)
            except RuntimeError:
                live.capture_refresh_required = True
                raise
        pull_ms = (
            live.ops.last_screencap_trace.adb_pull_ms
            if live.ops.last_screencap_trace is not None
            else None
        )
        health = live.adb_session_health.to_dict()
        logger.info(
            "xingyu_official capture adb_health score=%s refreshed=%s "
            "pull_ms=%s reason=%s refresh_count=%s",
            health.get("score"),
            health.get("refreshed"),
            pull_ms,
            health.get("last_decision_reason"),
            health.get("refresh_count"),
        )
        display_width, display_height = live.ops.wm_size()
        if (width, height) != (display_width, display_height):
            logger.warning(
                "xingyu capture pixel/display dimensions differ: image=%sx%s display=%sx%s",
                width,
                height,
                display_width,
                display_height,
            )
        orientation = "portrait" if height >= width else "landscape"

        observed_package_name, observed_activity = runtime_identity or (None, None)
        return CaptureShot(
            png_bytes=png,
            width=width,
            height=height,
            orientation=orientation,
            package_name=observed_package_name,
            activity=observed_activity,
        )

    def dump_ui_tree(self, device: Any) -> str:
        live = _as_live(device)
        return _run_live_adb_with_reconnect(
            live, lambda: live.ops.dump_ui_hierarchy()
        )

    def dump_device_log(self, device: Any, *, package_name: str | None) -> str:
        live = _as_live(device)
        return live.ops.dump_logcat(package_name=package_name)

    def dump_clipboard_text(self, device: Any) -> str:
        """读取剪贴板明文；不可用时返回空串。"""

        live = _as_live(device)
        try:
            return str(live.ops.get_clipboard_text() or "")
        except Exception:  # noqa: BLE001 - 云机可能禁 clipboard 命令
            return ""

    def dump_view_hierarchy_text(self, device: Any) -> str:
        """dumpsys activity top／window，供外跳 URL 抽取。"""

        live = _as_live(device)
        try:
            return _run_live_adb_with_reconnect(
                live, lambda: live.ops.dump_view_hierarchy()
            )
        except Exception:  # noqa: BLE001
            return ""

    def dump_network_trace(
        self,
        device: Any,
        *,
        package_name: str | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from services.api.app.application.app_network_proxy import get_app_network_proxy_hub

        live = _as_live(device)
        hub = get_app_network_proxy_hub()
        summary = None
        try:
            summary = live.ops.dump_network_summary(package_name=package_name)
        except Exception:  # noqa: BLE001
            summary = None
        if session_id:
            if not hub.is_session_bound(session_id):
                hub.bind_session(session_id)
            return hub.build_network_trace(
                session_id=session_id,
                package_name=package_name,
                fallback_summary=summary,
            )
        from services.api.app.application.app_surface_evidence import (
            bound_network_trace_payload,
        )

        if summary:
            return bound_network_trace_payload(
                package_name=package_name,
                proxy_attached=False,
                raw_summary=summary,
                status="captured",
                reason="dumpsys_network_summary",
            )
        return bound_network_trace_payload(
            package_name=package_name,
            proxy_attached=False,
            status="partial_unavailable",
            reason="network_summary_unavailable",
        )

    def dump_fault_artifact(self, device: Any, *, package_name: str | None) -> dict[str, Any]:
        from services.api.app.application.app_surface_evidence import (
            bound_fault_artifact_payload,
            detect_fault_kind,
        )

        live = _as_live(device)
        try:
            raw = live.ops.dump_fault_signals(package_name=package_name)
            kind = detect_fault_kind(raw)
            return bound_fault_artifact_payload(
                raw_text=raw,
                package_name=package_name,
                fault_kind=kind,
                status="captured",
            )
        except Exception as exc:  # noqa: BLE001
            return bound_fault_artifact_payload(
                raw_text="",
                package_name=package_name,
                fault_kind="unknown",
                status="partial_unavailable",
                reason=f"{type(exc).__name__}",
            )

    def execute(
        self,
        device: Any,
        *,
        action_type: str,
        parameters: dict[str, Any],
        semantic_target: str | None,
    ) -> ActionOutcome:
        live = _as_live(device)
        _reassert_portrait_lock(live, where="execute")
        op_raw = normalize_action_type(action_type)
        if op_raw == "input":
            return ActionOutcome(
                succeeded=False,
                retryable=False,
                error_code="ACTION_UNSUPPORTED",
                action_trace=(
                    _action_trace(
                        op_raw,
                        semantic_target=semantic_target,
                        resolution_method="parameters.input",
                        result="unsupported",
                        command_result="not_executed",
                    ),
                ),
            )
        # 恢复／Deep Link 原语：不经坐标 Resolver
        if op_raw in {"deep_link", "warm_restore", "reset_app"}:
            return _execute_restore_primitive(
                live,
                op_raw=op_raw,
                parameters=parameters,
            )
        try:
            op = InteractionOperation(op_raw)
        except ValueError:
            return ActionOutcome(
                succeeded=False,
                retryable=False,
                error_code="ACTION_UNSUPPORTED",
                action_trace=(
                    _action_trace(
                        op_raw,
                        semantic_target=semantic_target,
                        resolution_method="action_type",
                        result="unsupported",
                        command_result="not_executed",
                    ),
                ),
            )
        parameters = normalize_execute_parameters(parameters)
        # 横竖屏切换后 Physical wm 与 UIA／截图坐标系会错位；动作前刷新尺寸与 resolver。
        try:
            live.ops.query_wm_size()
            live.resolver = CanonicalPointTargetResolver.from_device_ops(live.ops)
        except Exception:  # noqa: BLE001 - 刷新失败仍用既有 resolver，由落点校验 fail-closed
            pass
        runtime_observation: AndroidRuntimeObservation | None = None
        ui_tree_error: str | None = None
        if op in {InteractionOperation.TAP, InteractionOperation.CLICK} and semantic_target:
            try:
                runtime_observation = parse_android_ui_hierarchy(
                    _run_live_adb_with_reconnect(
                        live, lambda: live.ops.dump_ui_hierarchy()
                    ),
                    width=live.ops.width,
                    height=live.ops.height,
                )
            except Exception as exc:  # noqa: BLE001 - UI Tree 只能形成结构化事实
                ui_tree_error = type(exc).__name__

            # 系统弹窗动作必须命中当前 UI Tree 的明确可点击节点；视觉点
            # 只能作为普通 App 画布候选的受约束输入，不能绕过弹窗门禁。
            if (
                parameters.get("surface_phase_key") == "system_dialog"
                and runtime_observation is None
            ):
                return ActionOutcome(
                    succeeded=False,
                    retryable=True,
                    error_code="ANDROID_UI_TREE_UNAVAILABLE",
                    action_trace=(
                        _action_trace(
                            op_raw,
                            semantic_target=semantic_target,
                            resolution_method="uiautomator.unavailable",
                            result="error",
                            command_result="not_executed",
                            runtime_ui_tree=ui_tree_error or "unavailable",
                        ),
                    ),
                )
        try:
            resolved = live.resolver.resolve_android(
                SemanticTargetResolutionRequest(
                    task_id="xingyu",
                    session_id="xingyu",
                    source_capture_id="xingyu",
                    action_type=op,
                    semantic_target=semantic_target,
                    parameters=parameters,
                ),
                runtime_observation=runtime_observation,
            )
        except SemanticTargetResolutionError as exc:
            return ActionOutcome(
                succeeded=False,
                retryable=exc.retryable,
                error_code=exc.error_code,
                action_trace=(
                    _action_trace(
                        op_raw,
                        semantic_target=semantic_target,
                        resolution_method="uiautomator.exact",
                        result="error",
                        command_result="not_executed",
                        error_code=exc.error_code,
                    ),
                ),
            )

        if (
            parameters.get("surface_phase_key") == "system_dialog"
            and semantic_target
            and not resolved.resolution_method.startswith("uiautomator.")
        ):
            return ActionOutcome(
                succeeded=False,
                retryable=True,
                error_code="ANDROID_TARGET_NOT_FOUND",
                action_trace=(
                    _action_trace(
                        op_raw,
                        semantic_target=semantic_target,
                        resolution_method=resolved.resolution_method,
                        result="error",
                        command_result="not_executed",
                        runtime_ui_tree="target_not_found",
                    ),
                ),
            )

        try:
            if resolved.operation is InteractionOperation.TAP:
                log_app_tap_audit(
                    "driver_execute",
                    channel="xingyu_device_bridge",
                    semantic_target=semantic_target,
                    device_x=resolved.tap_x,
                    device_y=resolved.tap_y,
                    resolution_method=resolved.resolution_method,
                    mapping_audit=resolved.mapping_audit,
                    backend=getattr(live.ops, "backend", None),
                    serial=getattr(live.ops, "serial", None),
                )
                _run_live_adb_with_reconnect(
                    live,
                    lambda: live.ops.tap(resolved.tap_x or 0, resolved.tap_y or 0),
                )
                trace = _action_trace(
                    op_raw,
                    semantic_target=semantic_target,
                    resolution_method=resolved.resolution_method,
                    resolved=resolved,
                    result="ok",
                    command="input tap",
                    command_result="ok",
                    runtime_ui_tree=(
                        "matched"
                        if resolved.resolution_method.startswith("uiautomator.")
                        else ui_tree_error or "not_used"
                    ),
                )
            elif resolved.operation is InteractionOperation.SCROLL:
                _run_live_adb_with_reconnect(
                    live,
                    lambda: live.ops.swipe(
                        resolved.swipe_x1 or 0,
                        resolved.swipe_y1 or 0,
                        resolved.swipe_x2 or 0,
                        resolved.swipe_y2 or 0,
                        ms=resolved.swipe_ms,
                    ),
                )
                trace = _action_trace(
                    op_raw,
                    semantic_target=semantic_target,
                    resolution_method=resolved.resolution_method,
                    resolved=resolved,
                    result="ok",
                    command="input swipe",
                    command_result="ok",
                )
            elif resolved.operation is InteractionOperation.BACK:
                _run_live_adb_with_reconnect(live, lambda: live.ops.back())
                trace = _action_trace(
                    op_raw,
                    semantic_target=semantic_target,
                    resolution_method=resolved.resolution_method,
                    result="ok",
                    command="input keyevent",
                    command_result="ok",
                )
            elif resolved.operation in {
                InteractionOperation.OBSERVE,
                InteractionOperation.TIMED_CAPTURE,
            }:
                trace = _action_trace(
                    op_raw,
                    semantic_target=semantic_target,
                    resolution_method=resolved.resolution_method,
                    result="ok",
                    command_result="not_applicable",
                )
            else:
                return ActionOutcome(
                    succeeded=False,
                    retryable=False,
                    error_code="ACTION_UNSUPPORTED",
                    action_trace=(
                        _action_trace(
                            op_raw,
                            semantic_target=semantic_target,
                            resolution_method=resolved.resolution_method,
                            result="unsupported",
                            command_result="not_executed",
                        ),
                    ),
                )
        except Exception:  # noqa: BLE001
            return ActionOutcome(
                succeeded=False,
                retryable=True,
                error_code="SURFACE_DRIVER_FAILED",
                action_trace=(
                    _action_trace(
                        op_raw,
                        semantic_target=semantic_target,
                        resolution_method=resolved.resolution_method,
                        resolved=resolved,
                        result="error",
                        command_result="failed",
                        error_code="SURFACE_DRIVER_FAILED",
                    ),
                ),
            )
        return ActionOutcome(
            succeeded=True,
            retryable=False,
            error_code=None,
            action_trace=(trace,),
        )

    def close(self, device: Any, *, package_name: str | None, reason: str) -> None:
        live = _as_live(device)
        logger.info(
            "xingyu_official close backend=%s serial_set=%s reason=%s",
            live.backend,
            bool(live.serial),
            reason,
        )
        # 收口必须关掉本任务 APK，并清掉残留前台，供同机下一颗 APK 冷启动。
        try:
            for line in live.ops.clear_leftover_qc_app(
                package_name=(package_name or "").strip() or None
            ):
                logger.info("xingyu_official close leftover_cleanup %s", line)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "xingyu_official close leftover_cleanup skipped: %s",
                type(exc).__name__,
            )


def _action_trace(
    op: str,
    *,
    semantic_target: str | None,
    resolution_method: str,
    result: str,
    command_result: str,
    command: str | None = None,
    resolved: Any | None = None,
    runtime_ui_tree: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """生成不含句柄／原始 XML 的最小动作审计事实。"""

    trace: dict[str, Any] = {
        "op": op,
        "resolution_method": resolution_method,
        "result": result,
        "command_result": command_result,
    }
    if semantic_target:
        trace["semantic_target"] = semantic_target
    if command:
        trace["command"] = command
    if runtime_ui_tree:
        trace["runtime_ui_tree"] = runtime_ui_tree
    if error_code:
        trace["error_code"] = error_code
    if resolved is not None:
        if resolved.tap_x is not None:
            trace["resolved_x"] = resolved.tap_x
        if resolved.tap_y is not None:
            trace["resolved_y"] = resolved.tap_y
        if resolved.swipe_x1 is not None:
            trace["resolved_x1"] = resolved.swipe_x1
        if resolved.swipe_y1 is not None:
            trace["resolved_y1"] = resolved.swipe_y1
        if resolved.swipe_x2 is not None:
            trace["resolved_x2"] = resolved.swipe_x2
        if resolved.swipe_y2 is not None:
            trace["resolved_y2"] = resolved.swipe_y2
        bounds = resolved.resolved_bounds
        if bounds is not None:
            trace.update(
                {
                    "bounds_left": bounds.left,
                    "bounds_top": bounds.top,
                    "bounds_right": bounds.right,
                    "bounds_bottom": bounds.bottom,
                }
            )
        mapping_audit = getattr(resolved, "mapping_audit", None)
        if isinstance(mapping_audit, Mapping):
            trace["coordinate_mapping"] = dict(mapping_audit)
    return trace


def _execute_restore_primitive(
    live: _LiveDevice,
    *,
    op_raw: str,
    parameters: dict[str, Any],
) -> ActionOutcome:
    pkg = str(parameters.get("package_name") or "").strip()
    activity = str(parameters.get("activity") or "").strip() or None
    deep_link = str(
        parameters.get("deep_link") or parameters.get("uri") or parameters.get("url") or ""
    ).strip()
    try:
        if op_raw == "deep_link":
            if not deep_link:
                return ActionOutcome(
                    succeeded=False,
                    retryable=False,
                    error_code="DEEP_LINK_MISSING",
                    action_trace=({"op": op_raw, "result": "missing_uri"},),
                )
            _run_live_adb_with_reconnect(
                live,
                lambda: live.ops.start_deep_link(
                    deep_link, package_name=pkg or None
                ),
            )
            detail = {"deep_link": deep_link, "package_name": pkg or None}
        elif op_raw == "warm_restore":
            if not pkg:
                return ActionOutcome(
                    succeeded=False,
                    retryable=False,
                    error_code="PACKAGE_MISSING",
                    action_trace=({"op": op_raw, "result": "missing_package"},),
                )
            _run_live_adb_with_reconnect(
                live,
                lambda: live.ops.warm_bring_to_front(pkg, activity=activity),
            )
            detail = {"package_name": pkg, "activity": activity, "mode": "warm"}
        else:  # reset_app
            if not pkg:
                return ActionOutcome(
                    succeeded=False,
                    retryable=False,
                    error_code="PACKAGE_MISSING",
                    action_trace=({"op": op_raw, "result": "missing_package"},),
                )
            reset_policy = str(
                parameters.get("reset_policy")
                or "force_stop"
            ).strip()
            applied = _run_live_adb_with_reconnect(
                live,
                lambda: live.ops.apply_reset_policy(
                    pkg,
                    reset_policy=reset_policy,
                    activity=activity,
                ),
            )
            if not applied.get("container_rotated"):
                # force_stop / warm 路径仍走既有前台确认
                try:
                    live.controller.prepare_app_for_script(pkg, require_foreground=True)
                except Exception:  # noqa: BLE001
                    pass
            detail = {
                "package_name": pkg,
                "mode": applied.get("mode") or "cold",
                "reset_policy": reset_policy,
                "container_rotated": bool(applied.get("container_rotated")),
            }
    except Exception as exc:  # noqa: BLE001
        return ActionOutcome(
            succeeded=False,
            retryable=True,
            error_code="SURFACE_DRIVER_FAILED",
            action_trace=({"op": op_raw, "error": f"{type(exc).__name__}: {exc}"},),
        )
    return ActionOutcome(
        succeeded=True,
        retryable=False,
        error_code=None,
        action_trace=({"op": op_raw, "detail": detail, "result": "ok"},),
    )


def _reassert_portrait_lock(live: _LiveDevice, *, where: str) -> None:
    """App 质检每帧重锁直屏。横屏 App 在截图或点击前压回，失败只记日志。"""

    try:
        for line in live.ops.force_portrait_lock():
            logger.info("xingyu_official %s portrait %s", where, line)
    except Exception as exc:  # noqa: BLE001 - 直屏锁失败不阻断质检
        logger.info(
            "xingyu_official %s portrait skipped: %s",
            where,
            type(exc).__name__,
        )


def _as_live(device: Any) -> _LiveDevice:
    if not isinstance(device, _LiveDevice):
        raise TypeError("device 必須為 LiveCloudPhone 會話")
    return device


def _read_capture_runtime_identity(live: _LiveDevice) -> tuple[str, str | None] | None:
    """读取真实前台；读取失败保持未知，禁止拿 Bootstrap 包名补造截图事实。"""

    try:
        return live.ops.current_runtime_identity()
    except Exception as exc:  # noqa: BLE001 - 身份不可用必须受控降级
        logger.info("xingyu capture runtime identity unavailable: %s", type(exc).__name__)
        return None


def _capture_live_png_with_retry(live: _LiveDevice, dest: Path) -> None:
    """保留原有同帧 ADB 刷新重试，供前台身份稳定采集复用。"""

    try:
        live.ops.screencap(dest)
        return
    except Exception as first_exc:  # noqa: BLE001 - 原有截图通道有界恢复
        last_exc: BaseException = first_exc
    for retry_i in range(_CAPTURE_IN_FRAME_FAILURE_RETRIES):
        backoff_i = min(retry_i, len(_CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS) - 1)
        time.sleep(_CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS[backoff_i])
        try:
            _refresh_capture_binding(
                live,
                pull_timeout_seconds=_SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS,
                health_reason=f"capture_failure_retry_{retry_i + 1}",
            )
        except Exception:
            live.capture_refresh_required = True
            raise last_exc from None
        try:
            live.ops.screencap(dest)
            return
        except Exception as retry_exc:  # noqa: BLE001 - 不覆盖首次动作结果
            last_exc = retry_exc
            logger.info(
                "xingyu_official capture in-frame retry failed attempt=%s/%s error=%s",
                retry_i + 1,
                _CAPTURE_IN_FRAME_FAILURE_RETRIES,
                type(retry_exc).__name__,
            )
    live.capture_refresh_required = True
    raise last_exc


def _run_live_adb_with_reconnect[T](live: _LiveDevice, run: Callable[[], T]) -> T:
    """动作／UI dump：公网 ADB 瞬断时有界重连再试。"""

    return run_with_cloud_phone_adb_reconnect(
        run=run,
        reconnect=lambda: _refresh_capture_binding(
            live, health_reason="action_adb_reconnect"
        ),
        log=lambda message: logger.info("%s", message),
    )


def _refresh_capture_binding(
    live: _LiveDevice,
    *,
    pull_timeout_seconds: float | None = None,
    health_reason: str = "capture_refresh",
) -> None:
    """刷新 MoreLogin ADB 绑定：短超时 ensure_ready + 强制重连并重建 ops。"""

    controller, serial, ops, logs = recover_cloud_phone_adb_binding(
        controller=live.controller,
        log=lambda message: logger.info("%s", message),
        pull_timeout_seconds=pull_timeout_seconds,
    )
    for line in logs:
        logger.info("xingyu_official capture_refresh %s", line)
    # 无论 serial 字面是否变化，都必须重建 ops：闭包内 serial／半开连接会残留。
    live.controller = controller
    live.serial = serial
    live.ops = ops
    live.resolver = CanonicalPointTargetResolver.from_device_ops(live.ops)
    live.capture_refresh_required = False
    live.adb_session_health.mark_refreshed(reason=health_reason)


def build_device_bridge(*, dry_run: bool) -> XingyuDeviceBridge:
    if dry_run:
        return DryRunDeviceBridge()
    return LiveCloudPhoneDeviceBridge()
