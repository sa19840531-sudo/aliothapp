"""星語 Official App Runtime 編排：會話／安裝／開啟／截圖／動作／關閉。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.api.v1.xingyu_official.gate import xingyu_official_dry_run
from services.api.app.application.xingyu_runtime.action_normalize import (
    normalize_action_type,
    normalize_execute_parameters,
)
from services.api.app.application.xingyu_runtime.build_artifact_fetch import (
    BuildArtifactError,
    resolve_apk_path,
    verify_sha256,
)
from services.api.app.application.xingyu_runtime.device_bridge import (
    CaptureShot,
    XingyuDeviceBridge,
    build_device_bridge,
)
from services.api.app.application.xingyu_runtime.idempotency import (
    IdempotencyConflict,
    IdempotentRecord,
    XingyuIdempotencyStore,
)
from services.api.app.application.xingyu_runtime.resolved_target import (
    AppResolvedTargetReader,
    read_verified_target,
)
from services.api.app.application.xingyu_runtime.session_store import (
    XingyuSessionRecord,
    XingyuSessionStore,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CommandPolicy,
    CommandPolicyError,
    InteractionValidationError,
    SurfaceKind,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.infrastructure.db.app_resolved_target_reader import (
    PostgresAppResolvedTargetReader,
)
from services.api.app.infrastructure.db.command_scope_reader import (
    PostgresTaskCommandScopeReader,
)
from services.api.app.infrastructure.device.backend import BACKEND_CLOUD_PHONE

logger = logging.getLogger(__name__)


class XingyuExecutionAuthorizer(Protocol):
    """校验 Coordinator 已持有的命令上下文，不申请新的设备租约。"""

    async def verify_execution_context(self, **kwargs: Any) -> object | None: ...


class XingyuOfficialError(Exception):
    """業務失敗（轉 JSON／HTTP）。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
        status_code: int = 400,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id or f"req_{uuid4().hex[:16]}"


class XingyuOfficialService:
    """Official HTTP 邊界後方的唯一編排入口。"""

    def __init__(
        self,
        *,
        sessions: XingyuSessionStore | None = None,
        idempotency: XingyuIdempotencyStore | None = None,
        bridge: XingyuDeviceBridge | None = None,
        command_scope_reader: XingyuExecutionAuthorizer | None = None,
        resolved_target_reader: AppResolvedTargetReader | None = None,
        require_execution_context: bool | None = None,
    ) -> None:
        self.sessions = sessions or XingyuSessionStore()
        self.idempotency = idempotency or XingyuIdempotencyStore()
        # 测试可注入固定 bridge；生产路径按当前 DRY_RUN 动态解析，避免单例锁死 DryRun
        self._injected_bridge = bridge
        self._bridge: XingyuDeviceBridge | None = bridge
        self._command_scope_reader = command_scope_reader
        self._resolved_target_reader = resolved_target_reader
        # 生产服务（无注入 bridge）必须有 Coordinator 上下文；测试显式注入
        # DryRun bridge 时保持既有 HTTP 契约，但不会影响 from_env 的生产边界。
        self._require_execution_context = (
            bool(require_execution_context)
            if require_execution_context is not None
            else bridge is None
        )

    @property
    def bridge(self) -> XingyuDeviceBridge:
        if self._injected_bridge is not None:
            return self._injected_bridge
        dry_run = xingyu_official_dry_run()
        current = self._bridge
        from services.api.app.application.xingyu_runtime.device_bridge import (
            DryRunDeviceBridge,
        )

        need_dry = isinstance(current, DryRunDeviceBridge) if current is not None else None
        if current is None or need_dry != dry_run:
            self._bridge = build_device_bridge(dry_run=dry_run)
        assert self._bridge is not None
        return self._bridge

    @classmethod
    def from_env(cls) -> XingyuOfficialService:
        return cls(
            command_scope_reader=PostgresTaskCommandScopeReader(),
            resolved_target_reader=PostgresAppResolvedTargetReader(),
            require_execution_context=True,
        )

    def begin_idempotent(self, key: str | None, payload: Any) -> IdempotentRecord | None:
        if not key:
            return None
        try:
            return self.idempotency.lookup(key, payload=payload)
        except IdempotencyConflict as exc:
            raise XingyuOfficialError(
                "Idempotency-Key 與請求體衝突",
                error_code="IDEMPOTENCY_CONFLICT",
                retryable=False,
                status_code=409,
            ) from exc

    def start_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _require_text(payload, "session_id")
        task_id = _require_text(payload, "task_id")
        backend = _resolve_backend(payload)
        morelogin_env_id = _resolve_morelogin_env_id(payload)
        request_id = f"req_{uuid4().hex[:16]}"
        try:
            serial, device = self.bridge.start(
                backend=backend,
                morelogin_env_id=morelogin_env_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "xingyu start_session failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            raise XingyuOfficialError(
                f"start_session 失敗：{exc}",
                error_code="SESSION_START_FAILED",
                retryable=True,
                status_code=503,
                request_id=request_id,
            ) from exc
        record = self.sessions.create(
            session_id=session_id,
            task_id=task_id,
            backend=backend,
            serial=serial,
            device=device,
            task_remark=_resolve_task_remark(payload),
            morelogin_env_id=morelogin_env_id,
        )
        _bind_app_network_proxy(session_id=session_id, device=device, bridge=self.bridge)
        logger.info(
            "xingyu_official session started session_id=%s backend=%s serial_set=%s dry_run=%s",
            session_id,
            backend,
            bool(serial),
            xingyu_official_dry_run(),
        )
        return {"request_id": request_id, "session_handle": record.session_handle}

    def reattach_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """跨 Worker 恢复：按 session_id 重建装置绑定并返回可用 session_handle。"""
        session_id = _require_text(payload, "session_id")
        task_id = _require_text(payload, "task_id")
        backend = _resolve_backend(payload)
        request_id = f"req_{uuid4().hex[:16]}"
        existing = self.sessions.get_by_session_id(session_id)
        persisted = self.sessions.lookup_persisted_by_session_id(session_id)
        package_name = (
            (existing.package_name if existing is not None else None)
            or _optional_text(payload.get("package_name"))
            or (
                _optional_text(persisted.get("package_name"))
                if isinstance(persisted, dict)
                else None
            )
        )
        activity = (
            (existing.activity if existing is not None else None)
            or _optional_text(payload.get("activity"))
            or (
                _optional_text(persisted.get("activity"))
                if isinstance(persisted, dict)
                else None
            )
        )
        try:
            serial, device = self.bridge.start(
                backend=backend,
                morelogin_env_id=_resolve_morelogin_env_id(payload)
                or (
                    _optional_text(persisted.get("morelogin_env_id"))
                    if isinstance(persisted, dict)
                    else None
                )
                or (existing.morelogin_env_id if existing is not None else None),
                clear_leftover=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("xingyu reattach_session failed: %s", type(exc).__name__)
            raise XingyuOfficialError(
                f"reattach_session 失败：{exc}",
                error_code="SESSION_REATTACH_FAILED",
                retryable=True,
                status_code=502,
                request_id=request_id,
            ) from exc
        record = self.sessions.create(
            session_id=session_id,
            task_id=task_id,
            backend=backend,
            serial=serial,
            device=device,
            task_remark=_resolve_task_remark(payload)
            or (existing.task_remark if existing is not None else None)
            or (
                _optional_text(persisted.get("task_remark"))
                if isinstance(persisted, dict)
                else None
            ),
            morelogin_env_id=_resolve_morelogin_env_id(payload)
            or (existing.morelogin_env_id if existing is not None else None)
            or (
                _optional_text(persisted.get("morelogin_env_id"))
                if isinstance(persisted, dict)
                else None
            ),
        )
        if package_name:
            self.sessions.update(
                session_id,
                package_name=package_name,
                activity=activity,
            )
            record = self.sessions.get_by_session_id(session_id) or record
        _bind_app_network_proxy(session_id=session_id, device=device, bridge=self.bridge)
        logger.info(
            "xingyu_official session reattached session_id=%s backend=%s "
            "serial_set=%s package_set=%s",
            session_id,
            backend,
            bool(serial),
            bool(record.package_name),
        )
        return {"request_id": request_id, "session_handle": record.session_handle}

    def install(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"req_{uuid4().hex[:16]}"
        record = self._require_session(payload)
        platform_build_ref = _require_text(payload, "platform_build_ref")
        sha256 = _require_text(payload, "sha256")
        package_hint = _optional_text(payload.get("package_name"))
        try:
            from pathlib import Path

            if xingyu_official_dry_run():
                # 乾跑：有制品则仍校验 sha256；找不到制品时不阻断（不连真机）
                try:
                    apk_path = resolve_apk_path(platform_build_ref, sha256=sha256)
                    verify_sha256(apk_path, sha256)
                except BuildArtifactError as exc:
                    if exc.error_code == "INSTALL_CHECKSUM_MISMATCH":
                        raise
                    if exc.error_code in {
                        "INSTALL_ARTIFACT_NOT_FOUND",
                        "INSTALL_CHECKSUM_INVALID",
                    }:
                        apk_path = Path("dryrun.apk")
                    else:
                        raise
            else:
                apk_path = resolve_apk_path(platform_build_ref, sha256=sha256)
                verify_sha256(apk_path, sha256)
            installed = self.bridge.install(
                record.device,
                apk_path,
                package_hint=package_hint,
                same_label_skip_authorization=_resolve_task_remark(payload)
                or record.task_remark,
            )
        except BuildArtifactError as exc:
            return {
                "request_id": request_id,
                "succeeded": False,
                "retryable": exc.retryable,
                "installed_package": None,
                "reused_existing": False,
                "error_code": exc.error_code,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "xingyu install failed: %s: %s",
                type(exc).__name__,
                str(exc)[:1500],
            )
            return {
                "request_id": request_id,
                "succeeded": False,
                "retryable": True,
                "installed_package": None,
                "reused_existing": False,
                "error_code": "INSTALL_FAILED",
            }
        # 客户端超时后可能已 close session，但设备侧安装已成功；不得再 KeyError 成 500。
        try:
            self.sessions.update(record.session_id, package_name=installed)
        except KeyError:
            logger.warning(
                "xingyu install session already closed after success session_id=%s package=%s",
                record.session_id,
                installed,
            )
        return {
            "request_id": request_id,
            "succeeded": True,
            "retryable": False,
            "installed_package": installed,
            "reused_existing": False,
            "error_code": None,
        }

    def open_app(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"req_{uuid4().hex[:16]}"
        record = self._require_session(payload)
        package_name = _require_text(payload, "package_name")
        cold_start = bool(payload.get("cold_start", True))
        activity = _optional_text(payload.get("activity"))
        try:
            verified, focus = self.bridge.open_app(
                record.device,
                package_name,
                cold_start=cold_start,
                activity=activity,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "xingyu open failed: %s: %s",
                type(exc).__name__,
                str(exc)[:1500],
            )
            return {
                "request_id": request_id,
                "succeeded": False,
                "retryable": True,
                "package_name": package_name,
                "foreground_verified": False,
                "activity": None,
                "error_code": "OPEN_FAILED",
            }
        self.sessions.update(
            record.session_id,
            package_name=package_name,
            activity=focus or activity,
        )
        return {
            "request_id": request_id,
            "succeeded": True,
            "retryable": False,
            "package_name": package_name,
            "foreground_verified": bool(verified),
            "activity": focus or activity,
            "error_code": None,
        }

    def capture(self, payload: dict[str, Any]) -> tuple[CaptureShot, str]:
        request_id = f"req_{uuid4().hex[:16]}"
        record = self._require_session(payload)
        if _truthy_force_adb_refresh(payload.get("force_adb_refresh")):
            try:
                live = record.device
                if getattr(live, "capture_refresh_required", None) is not None:
                    live.capture_refresh_required = True
            except Exception:  # noqa: BLE001 - 强制刷新标记失败仍继续截图
                pass
        try:
            shot = self.bridge.capture(
                record.device,
                package_name=record.package_name,
                activity=record.activity,
            )
        except Exception as exc:  # noqa: BLE001
            raise XingyuOfficialError(
                f"capture 失敗：{exc}",
                error_code="CAPTURE_FAILED",
                retryable=True,
                status_code=502,
                request_id=request_id,
            ) from exc
        return shot, request_id

    def capture_ui_tree(self, payload: dict[str, Any]) -> tuple[str, str]:
        request_id = f"req_{uuid4().hex[:16]}"
        record = self._require_session(payload)
        try:
            xml = self.bridge.dump_ui_tree(record.device)
        except Exception as exc:  # noqa: BLE001
            raise XingyuOfficialError(
                f"ui_tree dump 失敗：{exc}",
                error_code="CAPTURE_FAILED",
                retryable=True,
                status_code=502,
                request_id=request_id,
            ) from exc
        text = str(xml or "").strip()
        if "<hierarchy" not in text and not text.startswith("<?xml"):
            raise XingyuOfficialError(
                "ui_tree 內容無效",
                error_code="CAPTURE_FAILED",
                retryable=True,
                status_code=502,
                request_id=request_id,
            )
        return text, request_id

    def capture_aux(self, payload: dict[str, Any]) -> dict[str, Any]:
        """采集 device_log / network_trace / fault_artifact 有界 JSON 制品。"""
        request_id = f"req_{uuid4().hex[:16]}"
        record = self._require_session(payload)
        artifact_type = str(payload.get("artifact_type") or "").strip()
        package_name = _optional_text(payload.get("package_name")) or record.package_name
        try:
            if artifact_type == "device_log":
                from services.api.app.application.app_surface_evidence import (
                    bound_device_log_payload,
                )

                raw = self.bridge.dump_device_log(record.device, package_name=package_name)
                body = bound_device_log_payload(raw_text=raw, package_name=package_name)
            elif artifact_type == "network_trace":
                body = self.bridge.dump_network_trace(
                    record.device,
                    package_name=package_name,
                    session_id=record.session_id,
                )
            elif artifact_type == "fault_artifact":
                body = self.bridge.dump_fault_artifact(
                    record.device, package_name=package_name
                )
            elif artifact_type == "clipboard":
                clip_text = ""
                dumpsys_text = ""
                dump_clip = getattr(self.bridge, "dump_clipboard_text", None)
                dump_vh = getattr(self.bridge, "dump_view_hierarchy_text", None)
                if callable(dump_clip):
                    try:
                        clip_text = str(dump_clip(record.device) or "")
                    except Exception:  # noqa: BLE001
                        clip_text = ""
                if callable(dump_vh):
                    try:
                        dumpsys_text = str(dump_vh(record.device) or "")
                    except Exception:  # noqa: BLE001
                        dumpsys_text = ""
                status = "captured" if (clip_text.strip() or dumpsys_text.strip()) else "empty"
                body = {
                    "status": status,
                    "text": clip_text[:4000],
                    "dumpsys_excerpt": dumpsys_text[:20_000],
                    "package_name": package_name,
                }
            else:
                raise XingyuOfficialError(
                    "artifact_type 不受支持",
                    error_code="CAPABILITY_MISMATCH",
                    retryable=False,
                    status_code=422,
                    request_id=request_id,
                )
        except XingyuOfficialError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise XingyuOfficialError(
                f"capture-aux 失敗：{exc}",
                error_code="CAPTURE_FAILED",
                retryable=True,
                status_code=502,
                request_id=request_id,
            ) from exc
        return {
            "request_id": request_id,
            "artifact_type": artifact_type,
            "content_type": "application/json",
            "payload": body,
        }

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"req_{uuid4().hex[:16]}"
        record = self._require_session(payload)
        action_execution_id = _require_text(payload, "action_execution_id")
        scope = await self._authorize_execution_context(
            payload=payload,
            record=record,
            action_execution_id=action_execution_id,
        )
        intent = payload.get("intent")
        if not isinstance(intent, dict):
            raise XingyuOfficialError(
                "intent 必須為物件",
                error_code="ACTION_INTENT_INVALID",
                retryable=False,
                status_code=422,
                request_id=request_id,
            )
        parsed_intent = None
        if scope is not None:
            parsed_intent = self._parse_scoped_intent(
                intent,
                request_id=request_id,
            )
            record = self._heal_session_package_name(
                record=record,
                intent=parsed_intent,
                scope=scope,
            )
            await self._validate_scoped_intent(
                intent=parsed_intent,
                payload=payload,
                record=record,
                scope=scope,
                request_id=request_id,
            )
        action_type = normalize_action_type(str(intent.get("action_type") or ""))
        raw_params = intent.get("parameters") if isinstance(intent.get("parameters"), dict) else {}
        parameters = normalize_execute_parameters(dict(raw_params))
        semantic_target = _optional_text(intent.get("semantic_target"))
        # 解析 ActionIntent 与参数归一化期间可能发生 Session 关闭、租约
        # 轮换或 Finalization Epoch 前进；设备 dispatch 前重新读取同一
        # Coordinator 上下文，不能把第一次通过当作永久授权。
        if scope is not None:
            scope = await self._authorize_execution_context(
                payload=payload,
                record=record,
                action_execution_id=action_execution_id,
            )
            if scope is None:
                raise XingyuOfficialError(
                    "Coordinator execution_context 已过期或不属于当前 Session",
                    error_code="STALE_SESSION_LEASE",
                    retryable=True,
                    status_code=409,
                    request_id=request_id,
                )
            assert parsed_intent is not None
            record = self._heal_session_package_name(
                record=record,
                intent=parsed_intent,
                scope=scope,
            )
            await self._validate_scoped_intent(
                intent=parsed_intent,
                payload=payload,
                record=record,
                scope=scope,
                request_id=request_id,
            )
        # XingyuDeviceBridge 是同步 ADB/设备端口；执行上下文校验在事件循环内
        # 完成后，把真实动作放到工作线程，避免阻塞异步 HTTP 路由。
        try:
            outcome = await asyncio.to_thread(
                self.bridge.execute,
                record.device,
                action_type=action_type,
                parameters=parameters,
                semantic_target=semantic_target,
            )
        except Exception as exc:  # noqa: BLE001 - 设备异常转换为稳定边界结果
            raise XingyuOfficialError(
                "设备动作执行失败",
                error_code="ACTION_EXECUTION_FAILED",
                retryable=True,
                status_code=502,
                request_id=request_id,
            ) from exc
        return {
            "request_id": request_id,
            "action_execution_id": action_execution_id,
            "succeeded": outcome.succeeded,
            "retryable": outcome.retryable,
            "action_trace": list(outcome.action_trace),
            "error_code": outcome.error_code,
        }

    async def _authorize_execution_context(
        self,
        *,
        payload: Mapping[str, Any],
        record: XingyuSessionRecord,
        action_execution_id: str,
    ) -> object | None:
        """校验外层 Coordinator 租约，禁止底层动作自行绕过控制面。"""
        context = payload.get("execution_context")
        if not self._require_execution_context and context is None:
            return None
        if not isinstance(context, Mapping):
            raise XingyuOfficialError(
                "缺少 Coordinator execution_context",
                error_code="EXECUTION_CONTEXT_REQUIRED",
                retryable=False,
                status_code=403,
            )

        def text_field(name: str) -> str:
            value = context.get(name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise XingyuOfficialError(
                    f"execution_context.{name} 无效",
                    error_code="EXECUTION_CONTEXT_INVALID",
                    retryable=False,
                    status_code=403,
                )
            return value

        task_id = text_field("task_id")
        session_id = text_field("session_id")
        surface_kind = text_field("surface_kind")
        lease_owner = text_field("lease_owner")
        lease_token = text_field("lease_token")
        if task_id != record.task_id or session_id != record.session_id:
            raise XingyuOfficialError(
                "execution_context 与 Session 归属不一致",
                error_code="STALE_SESSION_LEASE",
                retryable=False,
                status_code=403,
            )
        if surface_kind != SurfaceKind.ANDROID.value:
            raise XingyuOfficialError(
                "Xingyu 只接受 Android execution_context",
                error_code="EXECUTION_CONTEXT_INVALID",
                retryable=False,
                status_code=403,
            )
        if context.get("action_execution_id") != action_execution_id:
            raise XingyuOfficialError(
                "execution_context 与 Action Execution 身份不一致",
                error_code="ACTION_CONTEXT_MISMATCH",
                retryable=False,
                status_code=403,
            )
        command_sequence = context.get("command_sequence")
        finalization_epoch = context.get("finalization_epoch")
        if (
            isinstance(command_sequence, bool)
            or not isinstance(command_sequence, int)
            or command_sequence <= 0
            or isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise XingyuOfficialError(
                "execution_context 命令版本无效",
                error_code="EXECUTION_CONTEXT_INVALID",
                retryable=False,
                status_code=403,
            )
        surface_ref = _optional_text(payload.get("surface_ref"))
        if surface_ref != app_primary_surface_ref(record.session_id):
            raise XingyuOfficialError(
                "Surface 身份与 Session 不一致",
                error_code="STALE_SESSION_LEASE",
                retryable=False,
                status_code=403,
            )
        reader = self._command_scope_reader
        if reader is None:
            raise XingyuOfficialError(
                "未配置服务端命令范围读取器",
                error_code="COMMAND_SCOPE_UNAVAILABLE",
                retryable=True,
                status_code=503,
            )
        verifier = getattr(reader, "verify_execution_context", None)
        if not callable(verifier):
            raise XingyuOfficialError(
                "命令范围读取器不支持执行上下文校验",
                error_code="COMMAND_SCOPE_UNAVAILABLE",
                retryable=True,
                status_code=503,
            )
        try:
            scope = await verifier(
                task_id=task_id,
                session_id=session_id,
                surface_kind=SurfaceKind.ANDROID,
                lease_owner=lease_owner,
                lease_token=lease_token,
                command_sequence=command_sequence,
                finalization_epoch=finalization_epoch,
            )
        except XingyuOfficialError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise XingyuOfficialError(
                "服务端命令范围暂时不可读，已拒绝设备动作",
                error_code="COMMAND_SCOPE_UNAVAILABLE",
                retryable=True,
                status_code=503,
            ) from exc
        if scope is None:
            raise XingyuOfficialError(
                "Coordinator execution_context 已过期或不属于当前 Session",
                error_code="STALE_SESSION_LEASE",
                retryable=True,
                status_code=409,
            )
        return scope

    @staticmethod
    def _parse_scoped_intent(
        payload: Mapping[str, Any],
        *,
        request_id: str,
    ) -> ActionIntent:
        """带 Coordinator 上下文的请求必须是完整 ActionIntent。"""

        try:
            return ActionIntent.from_dict(payload)
        except InteractionValidationError as exc:
            raise XingyuOfficialError(
                "execution_context 下的 ActionIntent 无效",
                error_code="ACTION_INTENT_INVALID",
                retryable=False,
                status_code=422,
                request_id=request_id,
            ) from exc

    def _heal_session_package_name(
        self,
        *,
        record: XingyuSessionRecord,
        intent: ActionIntent,
        scope: object,
    ) -> XingyuSessionRecord:
        """API 重启／reattach 后内存会话可能丢包名：从策略白名单或磁盘索引补回。"""

        if isinstance(record.package_name, str) and record.package_name.strip():
            return record
        package_name: str | None = None
        params = intent.parameters if isinstance(intent.parameters, Mapping) else {}
        for key in ("package_name", "installed_package"):
            raw = params.get(key)
            if isinstance(raw, str) and raw.strip():
                package_name = raw.strip()
                break
        if package_name is None:
            policy = getattr(scope, "policy", None)
            allowed = getattr(policy, "allowed_packages", None) if policy is not None else None
            candidates = tuple(
                item.strip()
                for item in (allowed or ())
                if isinstance(item, str) and item.strip()
            )
            if len(candidates) == 1:
                package_name = candidates[0]
        if package_name is None:
            persisted = self.sessions.lookup_persisted_by_session_id(record.session_id)
            if isinstance(persisted, dict):
                package_name = _optional_text(persisted.get("package_name"))
        if not package_name:
            return record
        try:
            return self.sessions.update(record.session_id, package_name=package_name)
        except KeyError:
            return record

    async def _validate_scoped_intent(
        self,
        *,
        intent: ActionIntent,
        payload: Mapping[str, Any],
        record: XingyuSessionRecord,
        scope: object,
        request_id: str,
    ) -> None:
        """在设备 dispatch 前再次校验身份、动作策略与运行时包边界。"""

        context = payload.get("execution_context")
        if not isinstance(context, Mapping):
            raise XingyuOfficialError(
                "缺少 Coordinator execution_context",
                error_code="EXECUTION_CONTEXT_REQUIRED",
                retryable=False,
                status_code=403,
                request_id=request_id,
            )
        context_source_capture_id = context.get("source_capture_id")
        if (
            intent.task_id != record.task_id
            or intent.session_id != record.session_id
            or intent.surface_kind is not SurfaceKind.ANDROID
            or not isinstance(context_source_capture_id, str)
            or context_source_capture_id != intent.source_capture_id
        ):
            raise XingyuOfficialError(
                "ActionIntent 与 Coordinator execution_context 身份不一致",
                error_code="ACTION_CONTEXT_MISMATCH",
                retryable=False,
                status_code=403,
                request_id=request_id,
            )
        policy = getattr(scope, "policy", None)
        if not isinstance(policy, CommandPolicy):
            raise XingyuOfficialError(
                "服务端命令范围缺少有效策略",
                error_code="COMMAND_SCOPE_INVALID",
                retryable=True,
                status_code=503,
                request_id=request_id,
            )
        try:
            # 不能只验证 lease：直接调用 /apps/execute 的调用者也不能把
            # 低风险自报改造成支付、删除或授权确认等高副作用动作。
            policy.validate(intent)
            candidate = await read_verified_target(
                reader=self._resolved_target_reader,
                intent=intent,
                context=context,
                package_name=record.package_name,
            )
            policy.validate_resolved_target(
                intent,
                trusted_target_texts=(
                    candidate.semantic_name,
                    candidate.purpose,
                    candidate.expected_state_hint,
                ) if candidate is not None else (),
                target_trust="app_visual_candidate" if candidate is not None else None,
                target_role=candidate.control_role if candidate is not None else None,
            )
            policy.validate_runtime_location(
                surface_kind=SurfaceKind.ANDROID,
                package_name=record.package_name,
            )
        except CommandPolicyError as exc:
            # 缺包名多为 API 重启后会话丢字段，允许下一轮补全后重试。
            retryable = exc.retryable or (
                exc.error_code == "COMMAND_RUNTIME_LOCATION_INVALID"
                and not (isinstance(record.package_name, str) and record.package_name.strip())
            )
            raise XingyuOfficialError(
                str(exc),
                error_code=exc.error_code,
                retryable=retryable,
                status_code=403,
                request_id=request_id,
            ) from exc

    def close(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"req_{uuid4().hex[:16]}"
        handle = _optional_text(payload.get("session_handle"))
        session_id = _optional_text(payload.get("session_id"))
        reason = _optional_text(payload.get("reason")) or "closed"
        record: XingyuSessionRecord | None = None
        if handle:
            record = self.sessions.get_by_handle(handle)
        elif session_id:
            record = self.sessions.get_by_session_id(session_id)
        if record is None:
            # 關閉冪等：已不存在視為成功
            return {"request_id": request_id, "closed": True}
        try:
            self.bridge.close(
                record.device,
                package_name=record.package_name,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("xingyu close device cleanup: %s", type(exc).__name__)
        try:
            from services.api.app.application.app_network_proxy import (
                clear_device_http_proxy,
                get_app_network_proxy_hub,
            )

            get_app_network_proxy_hub().unbind_session(record.session_id)
            if hasattr(record.device, "ops"):
                clear_device_http_proxy(record.device.ops)
        except Exception:  # noqa: BLE001
            pass
        self.sessions.pop(record.session_id)
        return {"request_id": request_id, "closed": True}

    def _require_session(self, payload: dict[str, Any]) -> XingyuSessionRecord:
        handle = _optional_text(payload.get("session_handle"))
        if not handle:
            raise XingyuOfficialError(
                "缺少 session_handle",
                error_code="SESSION_HANDLE_MISSING",
                retryable=False,
                status_code=422,
            )
        record = self.sessions.get_by_handle(handle)
        if record is not None:
            return record
        recovered = self._recover_session_after_store_loss(payload, handle=handle)
        if recovered is not None:
            return recovered
        raise XingyuOfficialError(
            "session 不存在或已關閉",
            error_code="SESSION_NOT_FOUND",
            retryable=False,
            status_code=404,
        )

    def _recover_session_after_store_loss(
        self,
        payload: dict[str, Any],
        *,
        handle: str,
    ) -> XingyuSessionRecord | None:
        """API --reload 清空内存后：按磁盘索引 + 客户端既有 handle 重建装置绑定。

        必须命中磁盘活跃索引；显式 close / 无索引时不得仅凭 payload 复活。
        """

        meta = self.sessions.lookup_persisted_by_handle(handle)
        if not isinstance(meta, dict) or not meta:
            return None
        session_id = _optional_text(meta.get("session_id")) or _optional_text(
            payload.get("session_id")
        )
        task_id = _optional_text(meta.get("task_id")) or _optional_text(
            payload.get("task_id")
        )
        if not session_id or not task_id:
            return None
        backend = _resolve_backend(payload) or str(meta.get("backend") or "").strip() or (
            BACKEND_CLOUD_PHONE
        )
        morelogin_env_id = _resolve_morelogin_env_id(payload) or _optional_text(
            meta.get("morelogin_env_id")
        )
        try:
            serial, device = self.bridge.start(
                backend=backend,
                morelogin_env_id=morelogin_env_id,
                clear_leftover=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "xingyu session recover failed session_id=%s err=%s",
                session_id,
                type(exc).__name__,
            )
            return None
        record = self.sessions.create(
            session_id=session_id,
            task_id=task_id,
            backend=backend,
            serial=serial,
            device=device,
            task_remark=_resolve_task_remark(payload)
            or _optional_text(meta.get("task_remark")),
            morelogin_env_id=morelogin_env_id,
            session_handle=handle,
        )
        package_name = _optional_text(meta.get("package_name"))
        activity = _optional_text(meta.get("activity"))
        if package_name:
            self.sessions.update(
                session_id,
                package_name=package_name,
                activity=activity,
            )
            record = self.sessions.get_by_handle(handle) or record
        _bind_app_network_proxy(session_id=session_id, device=device, bridge=self.bridge)
        logger.info(
            "xingyu_official session recovered after store loss session_id=%s backend=%s",
            session_id,
            backend,
        )
        return record


_SERVICE: XingyuOfficialService | None = None


def get_xingyu_official_service() -> XingyuOfficialService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = XingyuOfficialService.from_env()
    return _SERVICE


def reset_xingyu_official_service(
    service: XingyuOfficialService | None = None,
) -> XingyuOfficialService | None:
    """測試用：替換或清空單例。"""
    global _SERVICE
    previous = _SERVICE
    _SERVICE = service
    return previous


def _require_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise XingyuOfficialError(
            f"缺少欄位 {field_name}",
            error_code="REQUEST_INVALID",
            retryable=False,
            status_code=422,
        )
    return value.strip()


def _resolve_backend(payload: dict[str, Any]) -> str:
    """优先 options.backend，其次 device_ref 前缀，默认 cloud_phone。"""
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    raw = str(options.get("backend") or "").strip()
    if raw:
        return raw
    device_ref = payload.get("device_ref")
    if isinstance(device_ref, str) and device_ref.strip():
        head = device_ref.strip().split(":", 1)[0].strip()
        if head:
            return head
    return BACKEND_CLOUD_PHONE


def _resolve_morelogin_env_id(payload: dict[str, Any]) -> str | None:
    """优先 Session options 里租约绑定的 envId。"""
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    raw = str(options.get("morelogin_env_id") or "").strip()
    return raw or None


def _resolve_task_remark(payload: dict[str, Any]) -> str | None:
    """从 payload／options 读取任务备注（同名扫描跳过授权来源）。"""
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    for key in (
        "task_remark",
        "remark",
        "same_label_skip_authorization",
    ):
        text = _optional_text(payload.get(key)) or _optional_text(options.get(key))
        if text:
            return text
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _truthy_force_adb_refresh(value: Any) -> bool:
    """解析 Official Capture 请求中的强制换通道标记。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    text = str(value or "").strip().casefold()
    return text in {"1", "true", "yes", "on"}


def _bind_app_network_proxy(*, session_id: str, device: Any, bridge: XingyuDeviceBridge) -> None:
    """按配置启动录音代理、绑定会话，并尽量设置设备 http_proxy。"""
    try:
        from services.api.app.application.app_network_proxy import (
            AppNetworkProxySettings,
            attach_device_http_proxy,
            get_app_network_proxy_hub,
        )
        from services.api.app.core.config import get_settings

        wf = get_settings().workflow
        settings = AppNetworkProxySettings(
            enabled=bool(wf.app_network_proxy_enabled),
            listen_host=wf.app_network_proxy_listen_host,
            listen_port=int(wf.app_network_proxy_listen_port),
            advertise_host=wf.app_network_proxy_advertise_host,
            device_proxy_host=wf.app_network_proxy_device_host,
            mitm_enabled=bool(getattr(wf, "app_network_proxy_mitm_enabled", False)),
            mitm_ca_dir=getattr(wf, "app_network_proxy_mitm_ca_dir", None),
        )
        hub = get_app_network_proxy_hub()
        hub.configure(settings)
        if not settings.enabled:
            return
        started = hub.ensure_proxy_started(settings)
        hub.bind_session(session_id, endpoint_ref=settings.endpoint_ref())
        if not started:
            return
        # DryRun 无真实 ops；Live 设备尝试挂代理
        ops = getattr(device, "ops", None)
        if ops is not None:
            try:
                attach_device_http_proxy(ops, endpoint_ref=settings.endpoint_ref())
            except Exception as exc:  # noqa: BLE001
                logger.info("设备 http_proxy 挂载跳过: %s", exc)
            if settings.mitm_enabled:
                try:
                    from services.api.app.application.app_network_proxy import (
                        install_device_mitm_ca,
                    )

                    ca_pem = hub.mitm_ca_cert_pem()
                    if ca_pem:
                        install_result = install_device_mitm_ca(ops, ca_cert_pem=ca_pem)
                        hub.record_ca_install_result(session_id, install_result)
                        logger.info(
                            "MITM CA 设备安装 status=%s reason=%s staged=%s system=%s "
                            "user=%s verified=%s installer=%s",
                            install_result.get("status"),
                            install_result.get("reason"),
                            install_result.get("staged_path"),
                            install_result.get("system_path"),
                            install_result.get("user_path"),
                            install_result.get("verified"),
                            install_result.get("installer_launched"),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.info("MITM CA 设备安装跳过: %s", exc)
        elif xingyu_official_dry_run():
            # 乾跑注入一条可审计代理流量，证明 session_proxy 链路
            hub.record_entry(
                session_id,
                {
                    "method": "GET",
                    "url_host": "proxy.alioth.local",
                    "path": "/session-bind",
                    "status_code": 204,
                    "bytes": 0,
                },
            )
        del bridge
    except Exception:  # noqa: BLE001
        logger.warning("绑定 App 网络代理失败", exc_info=True)
