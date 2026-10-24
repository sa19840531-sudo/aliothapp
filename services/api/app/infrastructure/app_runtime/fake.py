"""用于青云侧闭环测试的确定性 App Runtime。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any

from services.api.app.domain.interaction import (
    ActionIntent,
    AppInstallReplacePolicy,
    AppInstallRequest,
    AppInstallResult,
    AppOpenRequest,
    AppOpenResult,
    CaptureProfile,
    InteractionOperation,
    RawActionResult,
    RawArtifact,
    RawArtifactSet,
    RawSurfaceObservation,
    SurfaceCapabilityManifest,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref

_DEFAULT_SCREENSHOT = base64.b64decode(
    # 108x192 深灰 PNG：Fake 占位图，避免被误认为真机黑屏
    "iVBORw0KGgoAAAANSUhEUgAAAGwAAADACAIAAABu2hEPAAABuklEQVR4nO3SoREAIRAEQfhoEEjyj+2DuJHdfs3U7nPfYuYb7hGx4YkBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIgYEDEgYkDEgIgBEQMiBkQMiBgQMSBiQMSAiAERAyIGRAyIGBAxIGJAxICIAREDIgZEDIi45n5S+wIINvmL+QAAAABJRU5ErkJggg=="
)
_RETRYABLE_ERROR_CODES = frozenset({"DEVICE_BUSY", "ADB_TIMEOUT", "SESSION_CRASHED"})
_SUPPORTED_ACTIONS = frozenset(
    {
        InteractionOperation.CLICK,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.INPUT,
        InteractionOperation.OBSERVE,
        InteractionOperation.DEEP_LINK,
        InteractionOperation.WARM_RESTORE,
        InteractionOperation.RESET_APP,
        InteractionOperation.SET_CLIPBOARD,
        InteractionOperation.OPEN_EXTERNAL,
    }
)


@dataclass(frozen=True, kw_only=True)
class FakeAppScenario:
    """声明一个可重复的 Fake 设备场景。"""

    package_name: str = "com.example.target"
    activity: str = ".MainActivity"
    screenshot_png: bytes = _DEFAULT_SCREENSHOT
    screenshot_sequence: tuple[bytes, ...] = ()
    width: int = 1
    height: int = 1
    orientation: str = "portrait"
    start_error_code: str | None = None
    install_error_code: str | None = None
    open_error_code: str | None = None
    capture_error_code: str | None = None
    action_error_code: str | None = None
    crash_after_action_count: int | None = None
    clipboard_text: str = ""

    def __post_init__(self) -> None:
        for field_name in ("package_name", "activity", "orientation"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.screenshot_png, bytes) or not self.screenshot_png.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise ValueError("screenshot_png 必须是有效 PNG 字节")
        if not isinstance(self.screenshot_sequence, tuple):
            raise TypeError("screenshot_sequence 必须为 tuple")
        if any(
            not isinstance(frame, bytes) or not frame.startswith(b"\x89PNG\r\n\x1a\n")
            for frame in self.screenshot_sequence
        ):
            raise ValueError("screenshot_sequence 只能包含有效 PNG 字节")
        for field_name in ("width", "height"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} 必须为正整数")
        for field_name in (
            "start_error_code",
            "install_error_code",
            "open_error_code",
            "capture_error_code",
            "action_error_code",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if self.crash_after_action_count is not None and (
            isinstance(self.crash_after_action_count, bool)
            or not isinstance(self.crash_after_action_count, int)
            or self.crash_after_action_count < 0
        ):
            raise ValueError("crash_after_action_count 必须为非负整数")


class FakeAppRuntimeError(RuntimeError):
    """Fake Runtime 在无结果 DTO 的端口上返回的结构化错误。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool) -> None:
        _require_text(message, "message")
        _require_text(error_code, "error_code")
        if not isinstance(retryable, bool):
            raise TypeError("retryable 必须为布尔值")
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


@dataclass
class _FakeSession:
    spec: SurfaceSessionSpec
    reference: SurfaceSessionRef
    installed_sha256: str | None = None
    installed_package: str | None = None
    opened: bool = False
    closed: bool = False
    action_count: int = 0
    capture_count: int = 0
    action_results: dict[str, tuple[str, RawActionResult]] = field(default_factory=dict)
    action_trace: list[dict[str, Any]] = field(default_factory=list)


class FakeAppRuntime:
    """实现 AppBootstrapPort 与 SurfaceDriver 的内存测试替身。"""

    def __init__(self, *, scenario: FakeAppScenario | None = None) -> None:
        self._scenario = scenario or FakeAppScenario()
        self._sessions: dict[str, _FakeSession] = {}
        self._lock = asyncio.Lock()
        self._manifest = build_android_screenshot_only_manifest(
            adapter_id="fake-app-runtime",
            adapter_version="1.0.0",
            backend="fake",
        )

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if not isinstance(spec, SurfaceSessionSpec):
            raise TypeError("spec 必须为 SurfaceSessionSpec")
        if spec.surface_kind is not SurfaceKind.ANDROID:
            raise FakeAppRuntimeError(
                "Fake App Runtime 只支持 Android",
                error_code="CAPABILITY_MISMATCH",
                retryable=False,
            )
        if spec.platform_build_ref is None:
            raise FakeAppRuntimeError(
                "App Session 缺少 platform_build_ref",
                error_code="PACKAGE_MISSING",
                retryable=False,
            )
        if self._scenario.start_error_code is not None:
            raise _runtime_error(self._scenario.start_error_code, "建立 App Session 失败")
        async with self._lock:
            existing = self._sessions.get(spec.session_id)
            if existing is not None:
                if existing.spec != spec or existing.closed:
                    raise FakeAppRuntimeError(
                        "Session 幂等身份冲突或已经关闭",
                        error_code="SESSION_CRASHED",
                        retryable=False,
                    )
                return existing.reference
            reference = SurfaceSessionRef(
                task_id=spec.task_id,
                session_id=spec.session_id,
                surface_kind=SurfaceKind.ANDROID,
                primary_surface_ref=app_primary_surface_ref(spec.session_id),
            )
            self._sessions[spec.session_id] = _FakeSession(
                spec=spec,
                reference=reference,
            )
            return reference

    async def reattach_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if not isinstance(spec, SurfaceSessionSpec):
            raise TypeError("spec 必须为 SurfaceSessionSpec")
        async with self._lock:
            state = self._sessions.get(spec.session_id)
            if state is None or state.spec != spec or state.closed:
                raise FakeAppRuntimeError(
                    "Fake App Session 不存在或已失效",
                    error_code="SESSION_CRASHED",
                    retryable=True,
                )
            return state.reference

    async def ensure_installed(self, request: AppInstallRequest) -> AppInstallResult:
        if not isinstance(request, AppInstallRequest):
            raise TypeError("request 必须为 AppInstallRequest")
        async with self._lock:
            session = self._require_session(request.task_id, request.session_id)
            request_id = _request_id("install", request.idempotency_key)
            if self._scenario.install_error_code is not None:
                return _install_failure(
                    request,
                    request_id=request_id,
                    error_code=self._scenario.install_error_code,
                )
            if session.installed_sha256 == request.sha256:
                return AppInstallResult(
                    request_id=request_id,
                    session_id=request.session_id,
                    succeeded=True,
                    retryable=False,
                    installed_package=session.installed_package,
                    reused_existing=True,
                )
            if (
                session.installed_sha256 is not None
                and request.replace_policy is AppInstallReplacePolicy.SAME_CHECKSUM_ONLY
            ):
                return _install_failure(
                    request,
                    request_id=request_id,
                    error_code="INSTALL_CHECKSUM_CONFLICT",
                )
            package_name = request.package_name or self._scenario.package_name
            session.installed_sha256 = request.sha256
            session.installed_package = package_name
            session.opened = False
            return AppInstallResult(
                request_id=request_id,
                session_id=request.session_id,
                succeeded=True,
                retryable=False,
                installed_package=package_name,
            )

    async def open_app(self, request: AppOpenRequest) -> AppOpenResult:
        if not isinstance(request, AppOpenRequest):
            raise TypeError("request 必须为 AppOpenRequest")
        async with self._lock:
            session = self._require_session(request.task_id, request.session_id)
            request_id = _request_id("open", request.idempotency_key)
            if session.installed_package != request.package_name:
                return _open_failure(
                    request,
                    request_id=request_id,
                    error_code="PACKAGE_MISSING",
                )
            if self._scenario.open_error_code is not None:
                return _open_failure(
                    request,
                    request_id=request_id,
                    error_code=self._scenario.open_error_code,
                )
            session.opened = True
            return AppOpenResult(
                request_id=request_id,
                session_id=request.session_id,
                succeeded=True,
                retryable=False,
                package_name=request.package_name,
                foreground_verified=True,
                activity=request.activity or self._scenario.activity,
            )

    async def warm_restore(self, request: AppOpenRequest) -> AppOpenResult:
        """热恢复：不卸载、不强制停，仅标记前台。"""
        if not isinstance(request, AppOpenRequest):
            raise TypeError("request 必须为 AppOpenRequest")
        async with self._lock:
            session = self._require_session(request.task_id, request.session_id)
            if session.installed_package != request.package_name:
                return _open_failure(
                    request,
                    request_id=_request_id("warm_restore", request.idempotency_key),
                    error_code="PACKAGE_MISSING",
                )
            session.opened = True
            return AppOpenResult(
                request_id=_request_id("warm_restore", request.idempotency_key),
                session_id=request.session_id,
                succeeded=True,
                retryable=False,
                package_name=request.package_name,
                foreground_verified=True,
                activity=request.activity or self._scenario.activity,
            )

    async def inject_account(self, request):
        """绑定账号环境引用到会话 options。"""
        from services.api.app.domain.interaction.app_account import (
            AppAccountInjectRequest,
            AppAccountInjectResult,
            bind_account_ref_on_options,
        )
        from services.api.app.domain.interaction.surface_driver import SurfaceSessionSpec

        if not isinstance(request, AppAccountInjectRequest):
            raise TypeError("request 必须为 AppAccountInjectRequest")
        async with self._lock:
            session = self._require_session(request.task_id, request.session_id)
            options = dict(session.spec.options or {})
            options = bind_account_ref_on_options(
                options, account_environment_ref=request.account_environment_ref
            )
            session.spec = SurfaceSessionSpec(
                task_id=session.spec.task_id,
                session_id=session.spec.session_id,
                surface_kind=session.spec.surface_kind,
                account_environment_ref=request.account_environment_ref,
                platform_build_ref=session.spec.platform_build_ref,
                device_ref=session.spec.device_ref,
                reset_policy=session.spec.reset_policy,
                options=options,
            )
        return AppAccountInjectResult(
            request_id=_request_id("inject_account", request.idempotency_key),
            session_id=request.session_id,
            succeeded=True,
            retryable=False,
            account_environment_ref=request.account_environment_ref,
            bound=True,
        )

    async def reset_app(self, request: AppOpenRequest) -> AppOpenResult:
        """冷启动恢复；clear_data 时轮转数据容器世代。"""
        if not isinstance(request, AppOpenRequest):
            raise TypeError("request 必须为 AppOpenRequest")
        async with self._lock:
            session = self._require_session(request.task_id, request.session_id)
            policy = (
                str(
                    session.spec.reset_policy
                    or (session.spec.options or {}).get("reset_policy")
                    or "force_stop"
                )
                .strip()
                .casefold()
            )
            if policy == "clear_data":
                from services.api.app.domain.interaction import with_rotated_app_data_container

                session.spec = with_rotated_app_data_container(session.spec)
            session.opened = False
        cold = AppOpenRequest(
            task_id=request.task_id,
            session_id=request.session_id,
            package_name=request.package_name,
            activity=request.activity,
            cold_start=True,
        )
        return await self.open_app(cold)

    async def observe(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawSurfaceObservation:
        state = await self._ready_session(session)
        _validate_profile(profile)
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=SurfaceKind.ANDROID,
            surface_ref=session.primary_surface_ref,
            payload={
                "package_name": state.installed_package,
                "activity": self._scenario.activity,
                "width": self._scenario.width,
                "height": self._scenario.height,
                "orientation": self._scenario.orientation,
                "image_signature": _image_signature(self._scenario.screenshot_png),
                "safe_area": {
                    "top": 0,
                    "right": 0,
                    "bottom": 0,
                    "left": 0,
                    "width": self._scenario.width,
                    "height": self._scenario.height,
                    "source": "display_bounds",
                },
                "lifecycle_phase": "foreground",
                "session_event": None,
                "process_alive": True,
            },
        )

    async def execute(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
    ) -> RawActionResult:
        _require_text(action_execution_id, "action_execution_id")
        async with self._lock:
            state = self._require_session(session.task_id, session.session_id)
            self._validate_session_reference(state, session)
            if not state.opened:
                return _action_failure(action_execution_id, "OPEN_REQUIRED")
            if intent.task_id != session.task_id or intent.session_id != session.session_id:
                raise FakeAppRuntimeError(
                    "ActionIntent 与 Session 身份不一致",
                    error_code="STALE_SESSION_LEASE",
                    retryable=False,
                )
            existing = state.action_results.get(action_execution_id)
            if existing is not None:
                existing_intent_id, result = existing
                if existing_intent_id != intent.intent_id:
                    raise FakeAppRuntimeError(
                        "Action Execution 幂等身份冲突",
                        error_code="ACTION_BLOCKED",
                        retryable=False,
                    )
                return result
            if (
                self._scenario.crash_after_action_count is not None
                and state.action_count >= self._scenario.crash_after_action_count
            ):
                result = _action_failure(action_execution_id, "SESSION_CRASHED")
            elif intent.action_type not in _SUPPORTED_ACTIONS:
                result = _action_failure(action_execution_id, "ACTION_UNSUPPORTED")
            elif self._scenario.action_error_code is not None:
                result = _action_failure(
                    action_execution_id,
                    self._scenario.action_error_code,
                )
            else:
                trace = {
                    "action": intent.action_type.value,
                    "semantic_target": intent.semantic_target,
                    "source_capture_id": intent.source_capture_id,
                }
                payload: dict[str, Any] = {"action_trace": [trace]}
                if intent.action_type is InteractionOperation.RESET_APP:
                    policy = (
                        str(
                            intent.parameters.get("reset_policy")
                            or state.spec.reset_policy
                            or (state.spec.options or {}).get("reset_policy")
                            or "force_stop"
                        )
                        .strip()
                        .casefold()
                    )
                    if policy == "clear_data":
                        from services.api.app.domain.interaction import (
                            with_rotated_app_data_container,
                        )

                        state.spec = with_rotated_app_data_container(state.spec)
                        payload["mode"] = "clear_data"
                        payload["container_rotated"] = True
                        payload["reset_policy"] = "clear_data"
                    else:
                        payload["mode"] = "cold"
                        payload["container_rotated"] = False
                        payload["reset_policy"] = policy
                state.action_trace.append(trace)
                state.action_count += 1
                result = RawActionResult(
                    action_execution_id=action_execution_id,
                    succeeded=True,
                    retryable=False,
                    payload=payload,
                )
            state.action_results[action_execution_id] = (intent.intent_id, result)
            return result

    async def capture(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawArtifactSet:
        state = await self._ready_session(session)
        requested_types = _validate_profile(profile)
        if self._scenario.capture_error_code is not None:
            raise _runtime_error(self._scenario.capture_error_code, "App 截图失败")
        screenshots = self._scenario.screenshot_sequence or (self._scenario.screenshot_png,)
        from services.api.app.application.app_surface_evidence import (
            build_app_temporal_trace,
            build_app_timed_capture_audit,
            resolve_app_timed_capture_params,
            resolve_app_timed_capture_settle_ms,
        )

        timed_params = resolve_app_timed_capture_params(
            profile_id=profile.profile_id,
            requested_artifact_types=requested_types,
        )
        settle_ms = resolve_app_timed_capture_settle_ms(profile_id=profile.profile_id)
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
        if timed_params is None:
            screenshot = screenshots[min(state.capture_count, len(screenshots) - 1)]
            state.capture_count += 1
            frame_pngs = [(0.0, screenshot)]
        else:
            frame_count, interval_ms = timed_params
            frame_pngs = []
            for index in range(frame_count):
                png = screenshots[min(state.capture_count + index, len(screenshots) - 1)]
                frame_pngs.append((float(index * max(interval_ms, 1)), png))
            state.capture_count += 1
            screenshot = frame_pngs[0][1]
        artifacts = [RawArtifact(artifact_type="screenshot", content=screenshot)]
        if "action_trace" in requested_types:
            artifacts.append(
                RawArtifact(
                    artifact_type="action_trace",
                    content={"entries": list(state.action_trace)},
                )
            )
        if "ui_tree" in requested_types:
            artifacts.append(
                RawArtifact(
                    artifact_type="ui_tree",
                    content=_FAKE_UI_TREE.encode("utf-8"),
                )
            )
        metadata: dict[str, Any] = {
            "package_name": state.installed_package,
            "activity": self._scenario.activity,
            "width": self._scenario.width,
            "height": self._scenario.height,
            "orientation": self._scenario.orientation,
            "image_signature": _image_signature(screenshot),
        }
        if timed_params is not None:
            metadata["timed_capture"] = build_app_timed_capture_audit(
                profile_id=profile.profile_id,
                frame_count=len(frame_pngs),
                requested_interval_ms=timed_params[1],
                produced_offsets_ms=[item[0] for item in frame_pngs],
                settle_delay_ms=settle_ms,
            )
        elif settle_ms > 0:
            metadata["timed_capture"] = build_app_timed_capture_audit(
                profile_id=profile.profile_id,
                frame_count=1,
                requested_interval_ms=0,
                produced_offsets_ms=[0.0],
                settle_delay_ms=settle_ms,
            )
        snapshot: dict[str, Any] | None = None
        if "ui_snapshot" in requested_types or "media_state" in requested_types:
            from services.api.app.application.app_surface_evidence import (
                project_ui_snapshot_from_hierarchy,
            )

            snapshot = project_ui_snapshot_from_hierarchy(
                _FAKE_UI_TREE,
                package_name=state.installed_package,
                activity=self._scenario.activity,
            )
            if "ui_snapshot" in requested_types:
                artifacts.append(RawArtifact(artifact_type="ui_snapshot", content=snapshot))
            # screen_id／structure_signature 留在 ui_snapshot 制品内，不写入 Capture metadata 白名单外字段
        if snapshot is not None and "media_state" in requested_types:
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
        if "visible_text" in requested_types:
            from services.api.app.application.app_surface_evidence import (
                project_visible_text_from_hierarchy,
            )

            visible = project_visible_text_from_hierarchy(_FAKE_UI_TREE)
            visible_text_value = str(visible.get("text") or "")
            artifacts.append(
                RawArtifact(
                    artifact_type="visible_text",
                    content=visible_text_value.encode("utf-8"),
                )
            )
            # visible_text_source 记在制品侧语义，不写入 Capture metadata 白名单外字段
        outbound_source = _FAKE_UI_TREE if (
            "ui_snapshot" in requested_types
            or "ui_tree" in requested_types
            or "media_state" in requested_types
            or "visible_text" in requested_types
        ) else ""
        clipboard_text = str(self._scenario.clipboard_text or "")
        from services.api.app.application.app_surface_evidence import (
            resolve_outbound_landing_from_channels,
        )

        outbound = resolve_outbound_landing_from_channels(
            clipboard_text=clipboard_text,
            hierarchy_xml=outbound_source,
            visible_text=visible_text_value,
            package_name=state.installed_package,
        )
        if outbound is not None:
            metadata["outbound_landing"] = outbound
        if "device_log" in requested_types:
            from services.api.app.application.app_surface_evidence import (
                bound_device_log_payload,
            )

            artifacts.append(
                RawArtifact(
                    artifact_type="device_log",
                    content=bound_device_log_payload(
                        raw_text=f"I/FakeRuntime: package={state.installed_package}\n",
                        package_name=state.installed_package,
                    ),
                )
            )
        if "network_trace" in requested_types:
            from services.api.app.application.app_surface_evidence import (
                bound_network_trace_payload,
                project_har_from_network_trace,
            )

            network_payload = bound_network_trace_payload(
                package_name=state.installed_package,
                status="captured",
                reason="fake_runtime",
                entries=(),
            )
            artifacts.append(
                RawArtifact(
                    artifact_type="network_trace",
                    content=network_payload,
                )
            )
            if "har" in requested_types:
                artifacts.append(
                    RawArtifact(
                        artifact_type="har",
                        content=project_har_from_network_trace(network_payload),
                    )
                )
        if "temporal_trace" in requested_types and timed_params is not None:
            samples = [
                {
                    "offset_ms": offset_ms,
                    "visible_text": f"fake-frame-{index}",
                    "image_sha256": _image_signature(png),
                }
                for index, (offset_ms, png) in enumerate(frame_pngs)
            ]
            artifacts.append(
                RawArtifact(
                    artifact_type="temporal_trace",
                    content=build_app_temporal_trace(
                        samples=samples,
                        requested_interval_ms=timed_params[1],
                        package_name=state.installed_package,
                        activity=self._scenario.activity,
                    ),
                )
            )
        if "view_hierarchy" in requested_types:
            from services.api.app.application.app_surface_evidence import (
                bound_view_hierarchy_payload,
            )

            artifacts.append(
                RawArtifact(
                    artifact_type="view_hierarchy",
                    content=bound_view_hierarchy_payload(
                        raw_text="ACTIVITY top fake-view-hierarchy",
                        package_name=state.installed_package,
                        activity=self._scenario.activity,
                        status="captured",
                    ),
                )
            )
        if "fault_artifact" in requested_types:
            from services.api.app.application.app_surface_evidence import (
                bound_fault_artifact_payload,
            )

            artifacts.append(
                RawArtifact(
                    artifact_type="fault_artifact",
                    content=bound_fault_artifact_payload(
                        raw_text="",
                        package_name=state.installed_package,
                        fault_kind="none",
                        status="captured",
                        reason="fake_runtime_no_fault",
                    ),
                )
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
        _require_text(reason, "reason")
        async with self._lock:
            state = self._require_session(session.task_id, session.session_id)
            self._validate_session_reference(state, session)
            state.closed = True
            state.opened = False

    async def _ready_session(self, reference: SurfaceSessionRef) -> _FakeSession:
        async with self._lock:
            state = self._require_session(reference.task_id, reference.session_id)
            self._validate_session_reference(state, reference)
            if not state.opened:
                raise FakeAppRuntimeError(
                    "App 尚未打开，不能观察或截图",
                    error_code="OPEN_REQUIRED",
                    retryable=False,
                )
            return state

    def _require_session(self, task_id: str, session_id: str) -> _FakeSession:
        state = self._sessions.get(session_id)
        if state is None or state.spec.task_id != task_id or state.closed:
            raise FakeAppRuntimeError(
                "App Session 不存在或已失效",
                error_code="SESSION_CRASHED",
                retryable=True,
            )
        return state

    @staticmethod
    def _validate_session_reference(
        state: _FakeSession,
        reference: SurfaceSessionRef,
    ) -> None:
        if state.reference != reference:
            raise FakeAppRuntimeError(
                "Surface Session 引用不一致",
                error_code="STALE_SESSION_LEASE",
                retryable=False,
            )


def _validate_profile(profile: CaptureProfile) -> frozenset[str]:
    if not isinstance(profile, CaptureProfile):
        raise TypeError("profile 必须为 CaptureProfile")
    from services.api.app.application.capture_profiles import APP_CAPTURE_ARTIFACT_TYPES

    requested = set(profile.before.artifact_types) | set(profile.after.artifact_types)
    unsupported = requested - set(APP_CAPTURE_ARTIFACT_TYPES)
    if unsupported or "screenshot" not in requested:
        raise FakeAppRuntimeError(
            "App Capture Profile 请求了不可用制品",
            error_code="CAPABILITY_MISMATCH",
            retryable=False,
        )
    return frozenset(requested)


_FAKE_UI_TREE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<hierarchy rotation="0">'
    '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
    'package="com.example.app" content-desc="" checkable="false" checked="false" '
    'clickable="false" enabled="true" focusable="false" focused="false" '
    'scrollable="false" long-clickable="false" password="false" selected="false" '
    'bounds="[0,0][1080,1920]"/>'
    "</hierarchy>\n"
)


def _install_failure(
    request: AppInstallRequest,
    *,
    request_id: str,
    error_code: str,
) -> AppInstallResult:
    return AppInstallResult(
        request_id=request_id,
        session_id=request.session_id,
        succeeded=False,
        retryable=error_code in _RETRYABLE_ERROR_CODES,
        error_code=error_code,
    )


def _open_failure(
    request: AppOpenRequest,
    *,
    request_id: str,
    error_code: str,
) -> AppOpenResult:
    return AppOpenResult(
        request_id=request_id,
        session_id=request.session_id,
        succeeded=False,
        retryable=error_code in _RETRYABLE_ERROR_CODES,
        package_name=request.package_name,
        foreground_verified=False,
        error_code=error_code,
    )


def _action_failure(action_execution_id: str, error_code: str) -> RawActionResult:
    return RawActionResult(
        action_execution_id=action_execution_id,
        succeeded=False,
        retryable=error_code in _RETRYABLE_ERROR_CODES,
        error_code=error_code,
    )


def _runtime_error(error_code: str, message: str) -> FakeAppRuntimeError:
    return FakeAppRuntimeError(
        message,
        error_code=error_code,
        retryable=error_code in _RETRYABLE_ERROR_CODES,
    )


def _request_id(prefix: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"fake-{prefix}:{digest}"


def _image_signature(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
