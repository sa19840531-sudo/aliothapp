"""PNG-only Official App Runtime，不承担数据库、队列或 Agent 职责。"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from services.api.app.domain.interaction import (
    ActionIntent,
    AppBootstrapPort,
    AppInstallRequest,
    AppInstallResult,
    AppOpenRequest,
    AppOpenResult,
    CaptureProfile,
    RawActionResult,
    RawArtifact,
    RawArtifactSet,
    RawSurfaceObservation,
    SurfaceCapabilityManifest,
    SurfaceDriver,
    SurfaceDriverError,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.domain.interaction.validation import require_text
from services.api.app.infrastructure.app_runtime.official_api import (
    ACTION_OUTCOME_UNKNOWN_ERROR_CODE,
    OfficialAppApiClient,
    OfficialAppApiError,
    OfficialCaptureResponse,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ACTION_TRACE_TEXT_FIELDS = frozenset(
    {
        "action",
        "op",
        "semantic_target",
        "resolution_method",
        "runtime_ui_tree",
        "command",
        "command_result",
        "result",
        "status",
        "error_code",
        "request_id",
        "action_execution_id",
        "occurred_at",
    }
)
_ACTION_TRACE_BOOL_FIELDS = frozenset({"succeeded", "retryable"})
_ACTION_TRACE_NUMBER_FIELDS = frozenset(
    {
        "duration_ms",
        "resolved_x",
        "resolved_y",
        "resolved_x1",
        "resolved_y1",
        "resolved_x2",
        "resolved_y2",
        "bounds_left",
        "bounds_top",
        "bounds_right",
        "bounds_bottom",
    }
)


class OfficialAppRuntimeError(SurfaceDriverError):
    """Official Runtime 对上层暴露的结构化失败。"""


@dataclass
class _OfficialSession:
    spec: SurfaceSessionSpec
    reference: SurfaceSessionRef
    opened: bool = False
    action_results: dict[str, tuple[str, RawActionResult]] = field(default_factory=dict)
    action_trace: list[Mapping[str, Any]] = field(default_factory=list)


class OfficialAppRuntime(AppBootstrapPort, SurfaceDriver):
    """把星语 API 映射为统一 App Bootstrap 与 Surface Driver 契约。"""

    def __init__(
        self,
        *,
        api_client: OfficialAppApiClient,
        max_screenshot_bytes: int,
    ) -> None:
        if not isinstance(max_screenshot_bytes, int) or isinstance(max_screenshot_bytes, bool):
            raise TypeError("max_screenshot_bytes 必须是正整数")
        if max_screenshot_bytes <= 0:
            raise ValueError("max_screenshot_bytes 必须大于 0")
        self._api_client = api_client
        self._max_screenshot_bytes = max_screenshot_bytes
        self._sessions: dict[str, _OfficialSession] = {}
        self._manifest = build_android_screenshot_only_manifest(
            adapter_id="xingyu.official",
            adapter_version="1.0.0",
            backend="official-api",
        )

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if not isinstance(spec, SurfaceSessionSpec):
            raise TypeError("spec 必须是 SurfaceSessionSpec")
        if spec.surface_kind is not SurfaceKind.ANDROID:
            raise OfficialAppRuntimeError(
                "Official App Runtime 仅支持 Android",
                error_code="CAPABILITY_MISMATCH",
                retryable=False,
            )
        if spec.platform_build_ref is None:
            raise OfficialAppRuntimeError(
                "App Session 缺少 platform_build_ref",
                error_code="PACKAGE_MISSING",
                retryable=False,
            )
        existing = self._sessions.get(spec.session_id)
        if existing is not None:
            if existing.spec != spec:
                raise OfficialAppRuntimeError(
                    "Session 幂等身份冲突",
                    error_code="SESSION_CRASHED",
                    retryable=False,
                )
            return existing.reference
        try:
            await self._api_client.start_session(spec)
        except Exception as error:
            raise _api_error("建立 Official App Session 失败", error) from error
        reference = SurfaceSessionRef(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref=app_primary_surface_ref(spec.session_id),
        )
        self._sessions[spec.session_id] = _OfficialSession(spec=spec, reference=reference)
        return reference

    async def reattach_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if not isinstance(spec, SurfaceSessionSpec):
            raise TypeError("spec 必须是 SurfaceSessionSpec")
        existing = self._sessions.get(spec.session_id)
        if existing is not None:
            if existing.spec != spec:
                raise OfficialAppRuntimeError(
                    "Session 幂等身份冲突",
                    error_code="STALE_SESSION_LEASE",
                    retryable=False,
                )
            existing.opened = True
            return existing.reference
        try:
            response = await self._api_client.reattach_session(spec)
        except Exception as error:
            raise _api_error("Official App Session 重连失败", error) from error
        reference = SurfaceSessionRef(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref=app_primary_surface_ref(spec.session_id),
        )
        if response.session_handle:
            self._sessions[spec.session_id] = _OfficialSession(
                spec=spec,
                reference=reference,
                opened=True,
            )
        return reference

    async def ensure_installed(self, request: AppInstallRequest) -> AppInstallResult:
        state = self._require_session(request.task_id, request.session_id)
        try:
            result = await self._api_client.install(request)
        except Exception as error:
            if _is_session_lost_error(error):
                await self.reattach_session(state.spec)
                try:
                    result = await self._api_client.install(request)
                except Exception as retry_error:
                    raise _api_error("Official App 安装调用失败", retry_error) from retry_error
            else:
                raise _api_error("Official App 安装调用失败", error) from error
        return AppInstallResult(
            request_id=result.request_id,
            session_id=state.reference.session_id,
            succeeded=result.succeeded,
            retryable=result.retryable,
            installed_package=result.installed_package,
            reused_existing=result.reused_existing,
            error_code=result.error_code,
        )

    async def open_app(self, request: AppOpenRequest) -> AppOpenResult:
        state = self._require_session(request.task_id, request.session_id)
        try:
            result = await self._api_client.open(request)
        except Exception as error:
            if _is_session_lost_error(error):
                await self.reattach_session(state.spec)
                try:
                    result = await self._api_client.open(request)
                except Exception as retry_error:
                    raise _api_error("Official App 打开调用失败", retry_error) from retry_error
            else:
                raise _api_error("Official App 打开调用失败", error) from error
        if result.succeeded:
            state.opened = True
        return AppOpenResult(
            request_id=result.request_id,
            session_id=state.reference.session_id,
            succeeded=result.succeeded,
            retryable=result.retryable,
            package_name=result.package_name,
            foreground_verified=result.foreground_verified,
            activity=result.activity,
            error_code=result.error_code,
        )

    async def observe(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawSurfaceObservation:
        capture = await self._capture_response(session=session, profile=profile)
        metadata = _capture_metadata(capture)
        metadata["safe_area"] = {
            "top": 0,
            "right": 0,
            "bottom": 0,
            "left": 0,
            "width": int(metadata.get("width") or 0),
            "height": int(metadata.get("height") or 0),
            "source": "display_bounds",
        }
        metadata["device_log_status"] = "ready"
        if not metadata.get("lifecycle_phase"):
            metadata["lifecycle_phase"] = "foreground"
        metadata.setdefault("process_alive", True)
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=SurfaceKind.ANDROID,
            surface_ref=session.primary_surface_ref,
            payload=metadata,
        )

    async def inject_account(self, request):
        """绑定账号环境引用；星语侧仅记录 Session 身份，不落明文凭证。"""
        from services.api.app.domain.interaction.app_account import (
            AppAccountInjectRequest,
            AppAccountInjectResult,
            bind_account_ref_on_options,
        )
        from services.api.app.domain.interaction.surface_driver import SurfaceSessionSpec

        if not isinstance(request, AppAccountInjectRequest):
            raise TypeError("request 必须为 AppAccountInjectRequest")
        state = self._require_session(request.task_id, request.session_id)
        options = dict(state.spec.options or {})
        options = bind_account_ref_on_options(
            options, account_environment_ref=request.account_environment_ref
        )
        state.spec = SurfaceSessionSpec(
            task_id=state.spec.task_id,
            session_id=state.spec.session_id,
            surface_kind=state.spec.surface_kind,
            account_environment_ref=request.account_environment_ref,
            platform_build_ref=state.spec.platform_build_ref,
            device_ref=state.spec.device_ref,
            reset_policy=state.spec.reset_policy,
            options=options,
        )
        return AppAccountInjectResult(
            request_id=f"inject:{request.idempotency_key}",
            session_id=request.session_id,
            succeeded=True,
            retryable=False,
            account_environment_ref=request.account_environment_ref,
            bound=True,
        )

    async def reset_app(self, request: AppOpenRequest) -> AppOpenResult:
        """冷启动恢复。

        clear_data 时轮转 app_data_container_ref；API 无 pm clear 时仍改会话身份。
        """
        state = self._require_session(request.task_id, request.session_id)
        policy = (
            str(
                state.spec.reset_policy
                or (state.spec.options or {}).get("reset_policy")
                or "force_stop"
            )
            .strip()
            .casefold()
        )
        if policy == "clear_data":
            from services.api.app.domain.interaction import with_rotated_app_data_container

            state.spec = with_rotated_app_data_container(state.spec)
        try:
            await self._api_client.close(state.reference, "reset_app")
        except Exception:  # noqa: BLE001 - 关闭失败仍尝试冷启动
            pass
        state.opened = False
        cold = AppOpenRequest(
            task_id=request.task_id,
            session_id=request.session_id,
            package_name=request.package_name,
            activity=request.activity,
            cold_start=True,
        )
        return await self.open_app(cold)

    async def warm_restore(self, request: AppOpenRequest) -> AppOpenResult:
        """热恢复：禁止 force-stop，仅把同会话 App 带回前台。"""
        warm = AppOpenRequest(
            task_id=request.task_id,
            session_id=request.session_id,
            package_name=request.package_name,
            activity=request.activity,
            cold_start=False,
        )
        return await self.open_app(warm)

    async def execute(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
    ) -> RawActionResult:
        return await self._execute_with_context(
            session=session,
            intent=intent,
            action_execution_id=action_execution_id,
            execution_context=None,
        )

    async def execute_with_context(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
        execution_context: Mapping[str, Any],
    ) -> RawActionResult:
        """把 Coordinator 的外层租约上下文传给 Official API。

        Official Runtime 位于 Coordinator 下游，不能再次申请 Session 命令租约；
        这里仅做上下文透传，真正的 task/session/epoch/lease 校验由 Xingyu 服务端
        完成。上下文不写入动作轨迹或日志。
        """
        if not isinstance(execution_context, Mapping) or not execution_context:
            raise OfficialAppRuntimeError(
                "Official App 动作缺少 Coordinator 执行上下文",
                error_code="EXECUTION_CONTEXT_REQUIRED",
                retryable=False,
            )
        _validate_execution_context(
            execution_context=execution_context,
            session=session,
            intent=intent,
            action_execution_id=action_execution_id,
        )
        return await self._execute_with_context(
            session=session,
            intent=intent,
            action_execution_id=action_execution_id,
            execution_context=execution_context,
        )

    async def _execute_with_context(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
        execution_context: Mapping[str, Any] | None,
    ) -> RawActionResult:
        require_text(action_execution_id, "action_execution_id")
        state = self._require_reference(session)
        if not state.opened:
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="OPEN_REQUIRED",
            )
        if intent.task_id != session.task_id or intent.session_id != session.session_id:
            raise OfficialAppRuntimeError(
                "ActionIntent 与 Session 身份不一致",
                error_code="STALE_SESSION_LEASE",
                retryable=False,
            )
        existing = state.action_results.get(action_execution_id)
        if existing is not None:
            if existing[0] != intent.intent_id:
                raise OfficialAppRuntimeError(
                    "Action Execution 幂等身份冲突",
                    error_code="ACTION_BLOCKED",
                    retryable=False,
                )
            return existing[1]
        try:
            if execution_context is None:
                response = await self._api_client.execute(session, intent, action_execution_id)
            else:
                response = await self._api_client.execute(
                    session,
                    intent,
                    action_execution_id,
                    execution_context=execution_context,
                )
        except TypeError as error:
            # 旧的替换客户端没有实现上下文合同时必须 fail-closed，不能
            # 偷退回无上下文动作；普通 execute() 的兼容路径不受影响。
            if execution_context:
                raise OfficialAppRuntimeError(
                    "Official App API 客户端不支持执行上下文",
                    error_code="EXECUTION_CONTEXT_UNSUPPORTED",
                    retryable=False,
                ) from error
            raise _api_error("Official App 动作调用失败", error) from error
        except OfficialAppApiError as error:
            if error.error_code == ACTION_OUTCOME_UNKNOWN_ERROR_CODE:
                # 请求可能已经触发设备动作但没有返回可确认结果；把未知终态
                # 写入本地动作账本，后续同 action 只能读取该事实，不能重放 API。
                result = RawActionResult(
                    action_execution_id=action_execution_id,
                    succeeded=False,
                    retryable=False,
                    error_code=ACTION_OUTCOME_UNKNOWN_ERROR_CODE,
                )
                state.action_results[action_execution_id] = (intent.intent_id, result)
                return result
            raise _api_error("Official App 动作调用失败", error) from error
        except Exception as error:
            raise _api_error("Official App 动作调用失败", error) from error
        if response.action_execution_id != action_execution_id:
            raise OfficialAppRuntimeError(
                "Official App 动作响应身份不一致",
                error_code="ACTION_BLOCKED",
                retryable=False,
            )
        payload: dict[str, Any] = {}
        if response.action_trace:
            safe_trace = tuple(_sanitize_action_trace(item) for item in response.action_trace)
            payload["action_trace"] = list(safe_trace)
            state.action_trace.extend(safe_trace)
        result = RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=response.succeeded,
            retryable=response.retryable,
            payload=payload,
            error_code=response.error_code,
        )
        state.action_results[action_execution_id] = (intent.intent_id, result)
        return result

    async def capture(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawArtifactSet:
        import asyncio
        import time

        state = self._require_reference(session)
        requested = set(profile.before.artifact_types) | set(profile.after.artifact_types)
        if profile.profile_id == "app-observation-v1":
            # 轻量观察默认不拉 soft-optional；显式请求 temporal_trace 时保留连拍
            keep_temporal = "temporal_trace" in requested
            requested -= {
                "ui_snapshot",
                "visible_text",
                "device_log",
                "network_trace",
                "har",
                "fault_artifact",
                "temporal_trace",
            }
            if keep_temporal:
                requested.add("temporal_trace")

        from services.api.app.application.app_surface_evidence import (
            build_app_temporal_trace,
            build_app_timed_capture_audit,
            resolve_app_timed_capture_params,
            resolve_app_timed_capture_settle_ms,
        )

        timed_params = resolve_app_timed_capture_params(
            profile_id=profile.profile_id,
            requested_artifact_types=requested,
        )
        settle_ms = resolve_app_timed_capture_settle_ms(profile_id=profile.profile_id)
        proxy_shape: dict[str, Any] | None = None
        weak_profile = any(
            token in str(profile.profile_id or "").casefold()
            for token in ("weak_network", "loading_approx", "line_loading")
        )
        if weak_profile:
            try:
                from services.api.app.application.app_network_proxy import (
                    get_app_network_proxy_hub,
                )

                proxy_shape = get_app_network_proxy_hub().apply_weak_network_shape(
                    latency_ms=800
                )
            except Exception:  # noqa: BLE001 - 代理未启用不阻断连拍近似
                proxy_shape = {
                    "enabled": False,
                    "network_throttle_applied": False,
                    "latency_ms": 0,
                }
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
        frame_pngs: list[tuple[float, bytes, OfficialCaptureResponse]] = []
        try:
            if timed_params is None:
                capture = await self._capture_response(session=session, profile=profile)
                frame_pngs.append((0.0, capture.png_bytes, capture))
            else:
                frame_count, interval_ms = timed_params
                started = time.perf_counter()
                last_offset = -1.0
                for index in range(frame_count):
                    if index > 0 and interval_ms > 0:
                        await asyncio.sleep(interval_ms / 1000.0)
                    frame_capture = await self._capture_response(session=session, profile=profile)
                    offset_ms = (time.perf_counter() - started) * 1000.0
                    if offset_ms <= last_offset:
                        offset_ms = last_offset + 0.001
                    last_offset = offset_ms
                    frame_pngs.append((offset_ms, frame_capture.png_bytes, frame_capture))
                capture = frame_pngs[0][2]
        finally:
            if weak_profile:
                try:
                    from services.api.app.application.app_network_proxy import (
                        get_app_network_proxy_hub,
                    )

                    get_app_network_proxy_hub().clear_weak_network_shape()
                except Exception:  # noqa: BLE001
                    pass

        artifacts = [RawArtifact(artifact_type="screenshot", content=capture.png_bytes)]
        metadata = _capture_metadata(capture)
        if timed_params is not None:
            metadata["timed_capture"] = build_app_timed_capture_audit(
                profile_id=profile.profile_id,
                frame_count=len(frame_pngs),
                requested_interval_ms=timed_params[1],
                produced_offsets_ms=[item[0] for item in frame_pngs],
                settle_delay_ms=settle_ms,
                proxy_shape=proxy_shape,
            )
        elif settle_ms > 0:
            metadata["timed_capture"] = build_app_timed_capture_audit(
                profile_id=profile.profile_id,
                frame_count=1,
                requested_interval_ms=0,
                produced_offsets_ms=[0.0],
                settle_delay_ms=settle_ms,
                proxy_shape=proxy_shape,
            )
        if "action_trace" in requested:
            artifacts.append(
                RawArtifact(
                    artifact_type="action_trace",
                    content=tuple(dict(item) for item in state.action_trace),
                )
            )
        ui_xml: str | None = None
        if (
            "ui_tree" in requested
            or "ui_snapshot" in requested
            or "visible_text" in requested
            or "media_state" in requested
            or timed_params is not None
        ):
            try:
                ui_tree = await self._api_client.capture_ui_tree(session, profile)
                ui_xml = ui_tree.xml_text
                if "ui_tree" in requested:
                    artifacts.append(
                        RawArtifact(
                            artifact_type="ui_tree",
                            content=ui_xml.encode("utf-8"),
                        )
                    )
            except Exception as error:  # noqa: BLE001 - UI Tree 失败不阻断截图主证据
                import logging

                logging.getLogger(__name__).warning(
                    "Official App UI Tree 采集失败，降级为截图+action_trace: %s",
                    error,
                )
        snapshot: dict[str, Any] | None = None
        if ui_xml and ({"ui_snapshot", "media_state"} & requested):
            from services.api.app.application.app_surface_evidence import (
                project_ui_snapshot_from_hierarchy,
            )

            try:
                snapshot = project_ui_snapshot_from_hierarchy(
                    ui_xml,
                    package_name=capture.package_name,
                    activity=capture.activity,
                )
                if "ui_snapshot" in requested:
                    artifacts.append(RawArtifact(artifact_type="ui_snapshot", content=snapshot))
                # screen_id/structure_signature 属于 UI Snapshot 证据，不复制到
                # Screenshot 顶层 metadata，避免把页面结构事实伪装成截图属性。
            except Exception as error:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("Official App ui_snapshot 投影失败: %s", error)
        if snapshot is not None and "media_state" in requested:
            from services.api.app.application.app_surface_evidence import (
                project_media_state_from_ui_snapshot,
            )

            artifacts.append(
                RawArtifact(
                    artifact_type="media_state",
                    content=project_media_state_from_ui_snapshot(
                        snapshot,
                    ),
                )
            )
        visible_text_value = ""
        if ui_xml and ("visible_text" in requested or timed_params is not None):
            from services.api.app.application.app_surface_evidence import (
                project_visible_text_from_hierarchy,
            )

            try:
                visible = project_visible_text_from_hierarchy(ui_xml)
                visible_text_value = str(visible.get("text") or "")
                if "visible_text" in requested:
                    artifacts.append(
                        RawArtifact(
                            artifact_type="visible_text",
                            # 保留 source/截断状态，由证据存储适配器投影为文本正文
                            # 加受控 artifact metadata；不能在 Screenshot metadata 中散落。
                            content=visible,
                        )
                    )
            except Exception as error:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("Official App visible_text 投影失败: %s", error)
        clipboard_text = ""
        dumpsys_excerpt = ""
        try:
            aux = await self._api_client.capture_aux(
                session,
                artifact_type="clipboard",
                package_name=capture.package_name,
            )
            payload = aux.payload if isinstance(getattr(aux, "payload", None), Mapping) else {}
            if isinstance(payload, Mapping):
                clipboard_text = str(payload.get("text") or "")
                dumpsys_excerpt = str(payload.get("dumpsys_excerpt") or "")
        except Exception:  # noqa: BLE001 - clipboard／dumpsys 不可用时不阻断
            clipboard_text = ""
            dumpsys_excerpt = ""
        from services.api.app.application.app_surface_evidence import (
            resolve_outbound_landing_from_channels,
        )

        outbound = resolve_outbound_landing_from_channels(
            clipboard_text=clipboard_text,
            dumpsys_text=dumpsys_excerpt,
            hierarchy_xml=ui_xml or "",
            visible_text=visible_text_value,
            package_name=capture.package_name,
        )
        if outbound is not None:
            metadata["outbound_landing"] = outbound
        if timed_params is not None and "temporal_trace" in requested:
            samples = []
            for index, (offset_ms, png_bytes, _frame) in enumerate(frame_pngs):
                samples.append(
                    {
                        "offset_ms": offset_ms,
                        # 首帧尽量带可见文字；后续帧保留空串，避免复制同文案冒充实时变化
                        "visible_text": visible_text_value if index == 0 else "",
                        "image_sha256": f"sha256:{hashlib.sha256(png_bytes).hexdigest()}",
                    }
                )
            artifacts.append(
                RawArtifact(
                    artifact_type="temporal_trace",
                    content=build_app_temporal_trace(
                        samples=samples,
                        requested_interval_ms=timed_params[1],
                        package_name=capture.package_name,
                        activity=capture.activity,
                    ),
                )
            )
        for artifact_type in ("device_log", "network_trace", "fault_artifact"):
            if artifact_type not in requested:
                continue
            try:
                aux = await self._api_client.capture_aux(
                    session,
                    artifact_type=artifact_type,
                    package_name=capture.package_name,
                )
                payload = dict(aux.payload)
                artifacts.append(
                    RawArtifact(artifact_type=artifact_type, content=payload)
                )
                # har 不走 capture-aux；由 network_trace 投影为 Web 兼容 network_events
                if artifact_type == "network_trace" and "har" in requested:
                    from services.api.app.application.app_surface_evidence import (
                        project_har_from_network_trace,
                    )

                    artifacts.append(
                        RawArtifact(
                            artifact_type="har",
                            content=project_har_from_network_trace(payload),
                        )
                    )
            except Exception as error:  # noqa: BLE001 - 辅助证据可降级
                import logging

                logging.getLogger(__name__).warning(
                    "Official App %s 采集失败（可降级）: %s",
                    artifact_type,
                    error,
                )
        return RawArtifactSet(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_ref=session.primary_surface_ref,
            artifacts=tuple(artifacts),
            metadata=metadata,
        )

    def describe_capabilities(self) -> SurfaceCapabilityManifest:
        return self._manifest

    async def close_session(self, *, session: SurfaceSessionRef, reason: str) -> None:
        require_text(reason, "reason")
        self._require_reference(session)
        try:
            await self._api_client.close(session, reason)
        except Exception as error:
            raise _api_error("Official App 关闭调用失败", error) from error
        self._sessions.pop(session.session_id, None)

    async def _capture_response(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> OfficialCaptureResponse:
        state = self._require_reference(session)
        if not state.opened:
            raise OfficialAppRuntimeError(
                "App 尚未打开，不能截图",
                error_code="OPEN_REQUIRED",
                retryable=False,
            )
        _validate_capture_profile(profile)
        try:
            response = await self._api_client.capture_png(session, profile)
        except OfficialAppRuntimeError:
            raise
        except Exception as error:
            raise _api_error("Official App 截图调用失败", error) from error
        _validate_png(response, max_bytes=self._max_screenshot_bytes)
        return response

    def _require_session(self, task_id: str, session_id: str) -> _OfficialSession:
        state = self._sessions.get(session_id)
        if state is None or state.spec.task_id != task_id:
            raise OfficialAppRuntimeError(
                "Official App Session 不存在或已经关闭",
                error_code="SESSION_CRASHED",
                retryable=True,
            )
        return state

    def _require_reference(self, reference: SurfaceSessionRef) -> _OfficialSession:
        state = self._require_session(reference.task_id, reference.session_id)
        if state.reference != reference:
            raise OfficialAppRuntimeError(
                "Surface Session 引用不一致",
                error_code="STALE_SESSION_LEASE",
                retryable=False,
            )
        return state


def _validate_execution_context(
    *,
    execution_context: Mapping[str, Any],
    session: SurfaceSessionRef,
    intent: ActionIntent,
    action_execution_id: str,
) -> None:
    """校验下游 Official API 必须携带的外层命令上下文形状。"""

    required_text_fields = (
        "task_id",
        "session_id",
        "surface_kind",
        "action_execution_id",
        "source_capture_id",
        "lease_owner",
        "lease_token",
    )
    for field_name in required_text_fields:
        value = execution_context.get(field_name)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise OfficialAppRuntimeError(
                f"Official App execution_context.{field_name} 无效",
                error_code="EXECUTION_CONTEXT_INVALID",
                retryable=False,
            )
    if (
        execution_context.get("task_id") != session.task_id
        or execution_context.get("session_id") != session.session_id
        or execution_context.get("surface_kind") != SurfaceKind.ANDROID.value
        or execution_context.get("action_execution_id") != action_execution_id
        or execution_context.get("source_capture_id") != intent.source_capture_id
        or intent.task_id != session.task_id
        or intent.session_id != session.session_id
        or intent.surface_kind is not SurfaceKind.ANDROID
    ):
        raise OfficialAppRuntimeError(
            "Official App execution_context 与动作身份不一致",
            error_code="ACTION_CONTEXT_MISMATCH",
            retryable=False,
        )
    command_sequence = execution_context.get("command_sequence")
    finalization_epoch = execution_context.get("finalization_epoch")
    if (
        isinstance(command_sequence, bool)
        or not isinstance(command_sequence, int)
        or command_sequence <= 0
        or isinstance(finalization_epoch, bool)
        or not isinstance(finalization_epoch, int)
        or finalization_epoch < 0
    ):
        raise OfficialAppRuntimeError(
            "Official App execution_context 命令版本无效",
            error_code="EXECUTION_CONTEXT_INVALID",
            retryable=False,
        )


def _validate_capture_profile(profile: CaptureProfile) -> None:
    from services.api.app.application.capture_profiles import APP_CAPTURE_ARTIFACT_TYPES

    requested = set(profile.before.artifact_types) | set(profile.after.artifact_types)
    if "screenshot" not in requested or requested - set(APP_CAPTURE_ARTIFACT_TYPES):
        raise OfficialAppRuntimeError(
            "Official App Capture Profile 含不受支持的制品类型",
            error_code="CAPABILITY_MISMATCH",
            retryable=False,
        )


def _validate_png(response: OfficialCaptureResponse, *, max_bytes: int) -> None:
    content_type = response.content_type.split(";", 1)[0].strip().casefold()
    if content_type != "image/png":
        raise _capture_invalid("Official 截图 MIME 必须是 image/png")
    if not isinstance(response.png_bytes, bytes) or not response.png_bytes:
        raise _capture_invalid("Official 截图不能为空")
    if not response.png_bytes.startswith(_PNG_MAGIC):
        raise _capture_invalid("Official 截图不是 PNG")
    if len(response.png_bytes) > max_bytes:
        raise _capture_invalid("Official 截图超过最大字节数")
    for field_name in ("width", "height"):
        value = getattr(response, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _capture_invalid(f"Official 截图 {field_name} 必须是正整数")
    if response.declared_sha256 is not None:
        declared = response.declared_sha256.strip().casefold()
        actual = hashlib.sha256(response.png_bytes).hexdigest()
        if len(declared) != 64 or not hmac.compare_digest(declared, actual):
            raise _capture_invalid("Official 截图 SHA-256 不一致")


def _capture_metadata(response: OfficialCaptureResponse) -> dict[str, Any]:
    return {
        "package_name": response.package_name,
        "activity": response.activity,
        "width": response.width,
        "height": response.height,
        "orientation": response.orientation,
        "image_signature": f"sha256:{hashlib.sha256(response.png_bytes).hexdigest()}",
    }


def _sanitize_action_trace(value: Mapping[str, Any]) -> dict[str, Any]:
    """只保留稳定审计字段，外部句柄、凭据与二进制引用一律丢弃。"""
    safe: dict[str, Any] = {}
    for key in _ACTION_TRACE_TEXT_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and item.strip() and item == item.strip():
            safe[key] = item
    for key in _ACTION_TRACE_BOOL_FIELDS:
        item = value.get(key)
        if isinstance(item, bool):
            safe[key] = item
    for key in _ACTION_TRACE_NUMBER_FIELDS:
        item = value.get(key)
        if (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(item)
            and item >= 0
        ):
            safe[key] = item
    return safe


def _capture_invalid(message: str) -> OfficialAppRuntimeError:
    return OfficialAppRuntimeError(message, error_code="CAPTURE_INVALID", retryable=False)


def _is_session_lost_error(error: Exception) -> bool:
    """API --reload 清空内存 session 后，安装／打开会收到 404 SESSION_NOT_FOUND。"""

    if isinstance(error, OfficialAppApiError):
        code = str(error.error_code or "").strip().upper()
        return code in {"OFFICIAL_HTTP_404", "SESSION_NOT_FOUND"}
    return False


def _api_error(message: str, error: Exception) -> OfficialAppRuntimeError:
    """仅透传受控 API 错误码，避免第三方异常文本或属性泄漏到运行时。"""
    if isinstance(error, OfficialAppApiError):
        return OfficialAppRuntimeError(
            message,
            error_code=error.error_code,
            retryable=error.retryable,
        )
    return OfficialAppRuntimeError(
        message,
        error_code="OFFICIAL_API_ERROR",
        retryable=True,
    )
