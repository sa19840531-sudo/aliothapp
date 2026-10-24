"""Main Agent page_capture_tool → ActionIntent → CaptureCoordinator 双采桥接。

定位器只在 Runtime 内变成 ResolvedInteractionTarget，不得写入 ActionIntent.parameters。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationStage,
    CaptureWriteRequest,
)
from services.api.app.application.capture_coordinator import (
    CaptureCoordinator,
    InProcessSessionCommandGate,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedTaskCommandScopeReader,
)
from services.api.app.application.interaction_runtime import (
    ActionEvidenceBuffer,
    InMemoryActionEvidenceBuffer,
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
    TrustedUiSnapshotReader,
)
from services.api.app.application.page_capture_persistence import (
    SUPPORTED_CAPTURE_ARTIFACT_TYPES,
    PersistedEvidenceArtifact,
    PersistedPageCapture,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.browser.page_capture import (
    BlockedPageCaptureActionError,
    PageCaptureArtifact,
    PageCaptureBrowser,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PageTriggerAction,
)

_UNPROVEN_OBSTRUCTION_DISMISSAL_RESULTS = {
    "popup_dismissal_target_not_proven",
    "popup_overlay_outside_point_not_proven",
}
_SENTINEL_PARENT_PREFIXES = ("legacy-agent-entry", "agent-entry:")
AgentPageCaptureSupplementalSource = Literal[
    "rule_probe",
    "network_fault_probe",
    "evidence_role",
]


class AgentPageCaptureIntentError(RuntimeError):
    """ActionIntent／Coordinator 桥接失败；由 Tool 层转成结构化页面失败。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
        failure_stage: str = "browser_interaction",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.failure_stage = failure_stage


@dataclass(frozen=True, kw_only=True)
class AgentPageCaptureSpec:
    """Tool 参数到 Intent／内部绑定的中间规格。"""

    task_id: str
    session_id: str
    agent_run_id: str
    mode: PageCaptureMode
    url: str | None = None
    selector: str | None = None
    trigger_action: str | None = None
    probe_text: str | None = None
    submit_selector: str | None = None
    fault_url_pattern: str | None = None
    navigation_depth: int | None = None
    source_capture_id: str | None = None
    clear_obstructions: bool = False
    capture_role: str = "primary"
    inherit_parent_semantics: bool = False
    capture_dom: bool = True
    capture_visible_text: bool = True
    capture_before_context: bool = False
    capture_screenshot: bool = True
    full_page: bool = True
    frame_count: int = 3
    interval_ms: int = 1000
    timeout_ms: int = 30000
    viewport_width: int | None = None
    viewport_height: int | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    device_scale_factor: float = 1.0
    is_mobile: bool = True
    has_touch: bool = True
    timeout_seconds: float = 180.0
    persistence_timeout_seconds: float = 60.0
    supplemental_evidence_source: AgentPageCaptureSupplementalSource | None = None


@dataclass(frozen=True, kw_only=True)
class AgentPageCaptureOutcome:
    """Coordinator 双采完成后的 Tool 兼容结果。"""

    action_browser_result: PageCaptureResult
    before_capture_id: str
    after_capture_id: str
    persisted: PersistedPageCapture
    artifact_ids: tuple[str, ...]


class FixedResolvedTargetResolver:
    """把 Tool 的 selector 绑定到本轮可信 Snapshot 后再交给 Driver。

    selector 仍然来自 Tool 请求，但它只有在平台保存的 before Capture 中
    唯一命中、且与当前动作类型兼容时才会被视为可执行目标。目标文案、role
    和可交互性一律取 Snapshot，不能用 Agent 的 selector 文案或 intent 语义
    代替。没有 Snapshot reader 时保留单元测试/离线 Demo 的旧绑定行为；正式
    Runtime 必须注入 reader。
    """

    def __init__(
        self,
        target: ResolvedInteractionTarget,
        *,
        snapshot_reader: TrustedUiSnapshotReader | None = None,
    ) -> None:
        self._target = target
        self._snapshot_reader = snapshot_reader

    async def resolve(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        if self._snapshot_reader is None or request.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
            InteractionOperation.INPUT,
            InteractionOperation.HOVER,
        }:
            return self._target
        try:
            snapshot = await self._snapshot_reader.read_ui_snapshot(
                task_id=request.task_id,
                page_capture_id=request.source_capture_id,
            )
        except SemanticTargetResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读取失败必须要求重新观察
            raise SemanticTargetResolutionError(
                "当前 before Capture 的可信 UI Snapshot 暂不可用",
                error_code="UI_SNAPSHOT_UNAVAILABLE",
                retryable=True,
            ) from exc
        return _bind_fixed_target_to_snapshot(
            target=self._target,
            snapshot=snapshot,
            operation=request.action_type,
        )


def _bind_fixed_target_to_snapshot(
    *,
    target: ResolvedInteractionTarget,
    snapshot: Mapping[str, Any],
    operation: InteractionOperation,
) -> ResolvedInteractionTarget:
    """按精确 selector 唯一绑定 Snapshot 目标并提取真实语义。"""

    if not isinstance(snapshot, Mapping):
        raise SemanticTargetResolutionError(
            "可信 UI Snapshot 根节点无效",
            error_code="UI_SNAPSHOT_INVALID",
            retryable=True,
        )
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    selectors: list[tuple[str, InteractionOperation]] = []
    if target.selector:
        selectors.append((target.selector, operation))
    if operation is InteractionOperation.INPUT and target.submit_selector:
        selectors.append((target.submit_selector, InteractionOperation.CLICK))
    if not selectors:
        raise SemanticTargetResolutionError(
            "动作缺少可在可信 Snapshot 中绑定的 selector",
            error_code="SEMANTIC_TARGET_MISSING",
        )

    matched: list[Mapping[str, Any]] = []
    for selector, selector_operation in selectors:
        candidates = [
            item
            for item in elements
            if isinstance(item, Mapping)
            and str(item.get("selector") or "").strip() == selector
            and _snapshot_element_supports_operation(item, selector_operation)
        ]
        if not candidates:
            raise SemanticTargetResolutionError(
                f"当前 before Capture 未找到可执行 selector：{selector}",
                error_code="SEMANTIC_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(candidates) != 1:
            raise SemanticTargetResolutionError(
                f"当前 before Capture 中 selector 不唯一：{selector}",
                error_code="SEMANTIC_TARGET_AMBIGUOUS",
            )
        matched.append(candidates[0])

    trusted_texts: list[str] = []
    for element in matched:
        for key in (
            "text",
            "accessible_name",
            "aria_label",
            "ocr_text",
            "placeholder",
            "name",
            "type",
            "role",
            "tag",
        ):
            value = _snapshot_text(element.get(key))
            if value and value not in trusted_texts:
                trusted_texts.append(value)
    trusted_role = _snapshot_text(matched[0].get("role"))
    if trusted_role is None and len(matched) > 1:
        trusted_role = _snapshot_text(matched[1].get("role"))
    return replace(
        target,
        target_trust="dom_snapshot",
        trusted_target_texts=tuple(trusted_texts),
        trusted_target_role=trusted_role,
    )


def _snapshot_element_supports_operation(
    element: Mapping[str, Any],
    operation: InteractionOperation,
) -> bool:
    if element.get("disabled") is True:
        return False
    tag = str(element.get("tag") or "").strip().casefold()
    role = str(element.get("role") or "").strip().casefold()
    if operation is InteractionOperation.INPUT:
        return (
            element.get("read_only") is not True
            and (
                tag in {"input", "textarea", "select"}
                or element.get("content_editable") is True
            )
        )
    if operation in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        if element.get("interactive") is True:
            return True
        class_tokens = element.get("class_tokens")
        classes = (
            {str(item).strip().casefold() for item in class_tokens if str(item).strip()}
            if isinstance(class_tokens, list)
            else set()
        )
        return tag in {"a", "button", "input", "select", "textarea", "summary"} or (
            role
            in {
                "button",
                "checkbox",
                "link",
                "menuitem",
                "option",
            }
            or "btn" in classes
        )
    return True


def _snapshot_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _default_evidence_exit_gate() -> Any:
    """E16：按灰度模式装配 Web 离场门禁（默认 off 与改造前一致）。"""
    from services.api.app.application.web_evidence_exit_gate import (
        build_web_evidence_exit_gate,
    )

    return build_web_evidence_exit_gate()


class InMemoryPageCapturePersistence:
    """无 DB 时的内存持久化，供单元测试与 unbound Run。"""

    async def persist(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        result: PageCaptureResult,
        parent_page_capture_id: str | None = None,
        capture_role: str = "primary",
        inherit_parent_semantics: bool = False,
        **_kwargs: Any,
    ) -> PersistedPageCapture:
        del task_id, agent_run_id, parent_page_capture_id, inherit_parent_semantics
        page_capture_id = str(uuid4())
        artifacts: list[PersistedEvidenceArtifact] = []
        for item in result.artifacts:
            if item.artifact_type not in SUPPORTED_CAPTURE_ARTIFACT_TYPES:
                continue
            artifacts.append(
                PersistedEvidenceArtifact(
                    artifact_id=str(uuid4()),
                    artifact_type=item.artifact_type,
                    storage_path=f"mem://{page_capture_id}/{item.artifact_type}",
                    content_type="application/octet-stream",
                    checksum="mem",
                    size_bytes=0,
                    metadata={"capture_role": capture_role},
                )
            )
        return PersistedPageCapture(
            page_capture_id=page_capture_id,
            page_seq=1,
            artifacts=artifacts,
        )


class AgentBrowserCaptureWriter:
    """入口 before 用内存锚点；after 才写入业务 persistence。"""

    def __init__(
        self,
        *,
        browser: PageCaptureBrowser,
        persistence: Any,
        evidence_buffer: ActionEvidenceBuffer,
        persistence_timeout_seconds: float = 60.0,
        business_parent_capture_id: str | None = None,
        persist_before_capture: bool = False,
    ) -> None:
        self._browser = browser
        self._after_persistence = persistence
        self._before_persistence = (
            persistence if persist_before_capture else InMemoryPageCapturePersistence()
        )
        self._evidence_buffer = evidence_buffer
        self._persistence_timeout_seconds = max(float(persistence_timeout_seconds), 0.001)
        self._business_parent_capture_id = business_parent_capture_id
        self._persist_before_capture = persist_before_capture
        self.last_persisted: PersistedPageCapture | None = None

    async def capture(self, request: CaptureWriteRequest) -> CaptureRef:
        from services.api.app.application.capture_coordination import CapturePhase

        original_parent = request.parent_capture_id
        if request.phase is CapturePhase.BEFORE_ACTION:
            if _is_sentinel_parent(original_parent) and not self._persist_before_capture:
                return await self._persist_result(
                    request,
                    persistence=self._before_persistence,
                    result=PageCaptureResult(
                        execution_status="success",
                        mode=PageCaptureMode.OBSERVE.value,
                        reachable=True,
                        final_url=None,
                        title=None,
                        status_code=None,
                        artifacts=[
                            PageCaptureArtifact(
                                artifact_type="screenshot",
                                artifact_id=None,
                                content={"encoding": "base64", "data": ""},
                            )
                        ],
                    ),
                    parent_override=original_parent,
                    persist_parent=None,
                    track_as_primary=False,
                )
            from services.api.app.infrastructure.browser.capture_runtime import (
                BrowserCaptureWriter,
            )

            inner = BrowserCaptureWriter(
                browser=self._browser,
                persistence=self._before_persistence,
                evidence_buffer=self._evidence_buffer,
            )
            return await inner.capture(request)

        if request.phase is CapturePhase.AFTER_ACTION:
            if request.action_execution_id is None:
                raise RuntimeError("动作后 Capture 缺少 action_execution_id")
            pending = await self._evidence_buffer.get(
                action_execution_id=request.action_execution_id
            )
            if pending is None or not isinstance(pending.browser_result, PageCaptureResult):
                raise RuntimeError("动作阶段证据不存在，禁止提交不完整的动作后 Capture")
            result = pending.browser_result
            required = set(request.directive.artifact_types)
            artifacts = list(result.artifacts)
            produced = {item.artifact_type for item in artifacts}
            if "action_trace" in required and "action_trace" not in produced:
                artifacts.append(
                    PageCaptureArtifact(
                        artifact_type="action_trace",
                        artifact_id=None,
                        content=list(result.action_trace or []),
                    )
                )
            if "har" in required and "har" not in produced:
                artifacts.append(
                    PageCaptureArtifact(
                        artifact_type="har",
                        artifact_id=None,
                        content={
                            "format": "network_events",
                            "entries": list(result.network_requests or []),
                        },
                    )
                )
            if "screenshot" in required and "screenshot" not in produced:
                artifacts.append(
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={"encoding": "base64", "data": ""},
                    )
                )
            result = replace(result, artifacts=artifacts)
            try:
                reference = await self._persist_result(
                    request,
                    persistence=self._after_persistence,
                    result=result,
                    parent_override=request.parent_capture_id,
                    persist_parent=_resolve_after_persist_parent(
                        persist_before_capture=self._persist_before_capture,
                        protocol_parent_capture_id=request.parent_capture_id,
                        business_parent_capture_id=self._business_parent_capture_id,
                    ),
                    track_as_primary=True,
                    timeout_seconds=self._persistence_timeout_seconds,
                )
            except AgentPageCaptureIntentError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not (
                    isinstance(exc, TimeoutError) or type(exc).__name__.endswith("TimeoutError")
                ):
                    raise AgentPageCaptureIntentError(
                        f"页面证据持久化失败：{exc}",
                        error_code="PAGE_CAPTURE_PERSISTENCE_FAILED",
                        failure_stage="artifact_persistence",
                    ) from exc
                compact_result, compacted = _compact_after_capture_result_for_persistence_retry(
                    result
                )
                if compacted:
                    try:
                        reference = await self._persist_result(
                            request,
                            persistence=self._after_persistence,
                            result=compact_result,
                            parent_override=request.parent_capture_id,
                            persist_parent=_resolve_after_persist_parent(
                                persist_before_capture=self._persist_before_capture,
                                protocol_parent_capture_id=request.parent_capture_id,
                                business_parent_capture_id=(
                                    self._business_parent_capture_id
                                ),
                            ),
                            track_as_primary=True,
                            timeout_seconds=self._persistence_timeout_seconds,
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        raise _page_capture_persistence_timeout_error(
                            self._persistence_timeout_seconds
                        ) from retry_exc
                else:
                    raise _page_capture_persistence_timeout_error(
                        self._persistence_timeout_seconds
                    ) from exc
            await self._evidence_buffer.discard(action_execution_id=request.action_execution_id)
            return reference

        raise RuntimeError(f"不支持的 Capture 阶段：{request.phase}")

    async def _persist_result(
        self,
        request: CaptureWriteRequest,
        *,
        persistence: Any,
        result: PageCaptureResult,
        parent_override: str | None,
        persist_parent: str | None,
        track_as_primary: bool,
        timeout_seconds: float | None = None,
    ) -> CaptureRef:
        import asyncio

        persist_coro = _call_persistence_persist(
            persistence,
            task_id=request.task_id,
            agent_run_id=request.agent_run_id,
            result=result,
            parent_page_capture_id=persist_parent,
            capture_role=request.directive.role,
            inherit_parent_semantics=request.directive.inherit_parent_semantics,
            surface_session_id=request.session_id,
            action_execution_id=request.action_execution_id,
            session_command_sequence=request.session_command_sequence,
            finalization_epoch=request.finalization_epoch,
        )
        if timeout_seconds is None:
            persisted = await persist_coro
        else:
            persisted = await asyncio.wait_for(persist_coro, timeout=timeout_seconds)
        if track_as_primary:
            self.last_persisted = persisted
        return CaptureRef(
            task_id=request.task_id,
            session_id=request.session_id,
            capture_id=persisted.page_capture_id,
            surface_kind=request.surface_kind,
            capture_kind=CaptureKind.PAGE,
            role=request.directive.role,
            artifact_ids=tuple(item.artifact_id for item in persisted.artifacts),
            parent_capture_id=parent_override,
            action_execution_id=request.action_execution_id,
        )


def _page_capture_persistence_timeout_error(
    persistence_timeout_seconds: float,
) -> AgentPageCaptureIntentError:
    return AgentPageCaptureIntentError(
        (f"页面证据持久化超过 {persistence_timeout_seconds:g} 秒，已跳过当前动作并继续后续页面"),
        error_code="PAGE_CAPTURE_PERSISTENCE_TIMEOUT",
        failure_stage="artifact_persistence",
    )


def _resolve_after_persist_parent(
    *,
    persist_before_capture: bool,
    protocol_parent_capture_id: str | None,
    business_parent_capture_id: str | None,
) -> str | None:
    """正式双采必须以本轮 before Capture 作为 after 的直接父节点。"""

    if persist_before_capture:
        return protocol_parent_capture_id
    return _business_persist_parent(business_parent_capture_id)


def _compact_after_capture_result_for_persistence_retry(
    result: PageCaptureResult,
) -> tuple[PageCaptureResult, bool]:
    """动作后完整证据写入超时时，保留规则闭环必需证据做一次瘦身重试。"""

    keep_first_artifact_types = {
        "action_trace",
        "screenshot",
        "temporal_trace",
        "ui_snapshot",
        "visible_text",
    }
    kept_artifacts: list[PageCaptureArtifact] = []
    kept_types: set[str] = set()
    dropped_types: list[str] = []

    for artifact in result.artifacts:
        artifact_type = str(artifact.artifact_type or "").strip()
        if artifact_type in keep_first_artifact_types and artifact_type not in kept_types:
            kept_artifacts.append(artifact)
            kept_types.add(artifact_type)
            continue
        dropped_types.append(artifact_type or "unknown")

    if not dropped_types:
        return result, False

    metrics = dict(result.metrics or {})
    metrics["persistence_retry"] = {
        "strategy": "compact_after_capture_artifacts",
        "reason": "persistence_timeout",
        "original_artifact_count": len(result.artifacts),
        "compacted_artifact_count": len(kept_artifacts),
        "dropped_artifact_types": sorted(set(dropped_types)),
    }
    return replace(result, artifacts=kept_artifacts, metrics=metrics), True


class AgentToolInteractionExecutor:
    """固定目标解析，并在动作后拦截未证明的遮挡关闭。"""

    def __init__(
        self,
        *,
        executor: Any,
        evidence_buffer: ActionEvidenceBuffer,
    ) -> None:
        self._executor = executor
        self._evidence_buffer = evidence_buffer
        self.last_action_browser_result: PageCaptureResult | None = None

    async def execute(self, request) -> InteractionResult:  # noqa: ANN001
        result = await self._executor.execute(
            request,
            expose_driver_error_details=True,
        )
        if result.status is not InteractionResultStatus.EVIDENCE_PENDING:
            return result
        pending = await self._evidence_buffer.get(action_execution_id=result.action_execution_id)
        if pending is None or not isinstance(pending.browser_result, PageCaptureResult):
            return result
        self.last_action_browser_result = pending.browser_result
        unproven = _unproven_obstruction_dismissal(pending.browser_result)
        if unproven is None:
            return result
        await self._evidence_buffer.discard(action_execution_id=result.action_execution_id)
        self.last_action_browser_result = pending.browser_result
        failed_selector = str(unproven.get("requested_selector") or unproven.get("selector") or "")
        result_code = str(unproven.get("result") or "unknown")
        return InteractionResult(
            task_id=result.task_id,
            session_id=result.session_id,
            intent_id=result.intent_id,
            action_execution_id=result.action_execution_id,
            source_capture_id=result.source_capture_id,
            status=InteractionResultStatus.FAILED,
            error_code="PAGE_OBSTRUCTION_DISMISSAL_NOT_PROVEN",
            error_message=(
                "页面遮挡关闭动作未证明命中有效关闭控件；"
                f"selector={failed_selector!r}, result={result_code}"
            ),
            retryable=False,
            state_summary=result.state_summary,
            state_diff=result.state_diff,
        )


def build_action_intent_for_agent_page_capture(spec: AgentPageCaptureSpec) -> ActionIntent:
    """把 Tool 模式映射为正式 ActionIntent（不含平台 locator）。"""
    operation = _map_operation(spec)
    intent_id = f"page-capture:{uuid4()}"
    source_capture_id = (
        str(spec.source_capture_id).strip()
        if isinstance(spec.source_capture_id, str) and spec.source_capture_id.strip()
        else f"agent-entry:{spec.session_id}"
    )
    parameters: dict[str, Any] = {
        "capture_dom": bool(spec.capture_dom),
        "capture_visible_text": bool(spec.capture_visible_text),
        "capture_screenshot": bool(spec.capture_screenshot),
        "full_page": bool(spec.full_page),
        "frame_count": int(spec.frame_count),
        "interval_ms": int(spec.interval_ms),
        "legacy_tool_mode": spec.mode.value,
        "coordinator_dual_capture": True,
    }
    if spec.navigation_depth is not None:
        parameters["navigation_depth"] = int(spec.navigation_depth)
    if spec.viewport_width is not None:
        parameters["viewport_width"] = int(spec.viewport_width)
    if spec.viewport_height is not None:
        parameters["viewport_height"] = int(spec.viewport_height)
    if spec.screen_width is not None:
        parameters["screen_width"] = int(spec.screen_width)
    if spec.screen_height is not None:
        parameters["screen_height"] = int(spec.screen_height)
    parameters["device_scale_factor"] = float(spec.device_scale_factor)
    parameters["is_mobile"] = bool(spec.is_mobile)
    parameters["has_touch"] = bool(spec.has_touch)
    if operation is InteractionOperation.NAVIGATE:
        if not spec.url or not str(spec.url).strip():
            raise AgentPageCaptureIntentError(
                "goto 模式必须提供 url",
                error_code="ACTION_REQUEST_REJECTED",
                failure_stage="browser_capture",
            )
        parameters["destination_url"] = str(spec.url).strip()
    if operation is InteractionOperation.NETWORK_FAULT:
        if not spec.fault_url_pattern or not str(spec.fault_url_pattern).strip():
            raise AgentPageCaptureIntentError(
                "network_fault 必须提供 fault_url_pattern",
                error_code="ACTION_REQUEST_REJECTED",
                failure_stage="browser_interaction",
            )
        parameters["fault_url_pattern"] = str(spec.fault_url_pattern).strip()
    if operation is InteractionOperation.INPUT and spec.probe_text is not None:
        parameters["input_text"] = spec.probe_text
    if spec.clear_obstructions:
        parameters["clear_obstructions"] = True
    supplemental_evidence_source = (
        spec.supplemental_evidence_source
        or classify_agent_page_capture_supplemental_source(
            mode=spec.mode,
            trigger_action=spec.trigger_action,
            capture_role=spec.capture_role,
        )
    )
    if supplemental_evidence_source is not None:
        parameters["supplemental_evidence_source"] = supplemental_evidence_source

    return ActionIntent(
        task_id=spec.task_id,
        session_id=spec.session_id,
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=source_capture_id,
        action_type=operation,
        semantic_target=_semantic_target_for(operation),
        purpose=f"main-agent page_capture_tool mode={spec.mode.value}",
        capture_profile=_agent_tool_capture_profile(
            operation,
        capture_role=spec.capture_role,
        inherit_parent_semantics=spec.inherit_parent_semantics,
        capture_dom=spec.capture_dom,
        capture_visible_text=spec.capture_visible_text,
        capture_before_context=spec.capture_before_context,
        ),
        deadline=datetime.now(UTC) + timedelta(seconds=max(float(spec.timeout_seconds), 0.001)),
        parameters=parameters,
        risk_level=InteractionRiskLevel.LOW,
    )


def classify_agent_page_capture_supplemental_source(
    *,
    mode: PageCaptureMode,
    trigger_action: str | None,
    capture_role: str,
) -> AgentPageCaptureSupplementalSource | None:
    """识别服务端可确定的 H5 补采来源；普通观察和探索动作不在此推断。"""
    normalized_action = str(trigger_action or "").strip().casefold()
    if mode is PageCaptureMode.SINGLE_TRIGGER and normalized_action == "submit_probe":
        return "rule_probe"
    if mode is PageCaptureMode.SINGLE_TRIGGER and normalized_action == "network_fault":
        return "network_fault_probe"
    normalized_role = str(capture_role or "").strip().casefold()
    if normalized_role in {
        "probe_before",
        "probe_result_replay",
        "rule_supplement",
        "supplemental",
    }:
        return "evidence_role"
    return None


def build_resolved_target_for_agent_page_capture(
    spec: AgentPageCaptureSpec,
) -> ResolvedInteractionTarget:
    """Runtime 内部绑定；selector 不得进入 ActionIntent。"""
    operation = _map_operation(spec)
    if operation is InteractionOperation.NAVIGATE:
        return ResolvedInteractionTarget(destination_url=str(spec.url).strip())
    if operation is InteractionOperation.NETWORK_FAULT:
        return ResolvedInteractionTarget(
            fault_url_pattern=str(spec.fault_url_pattern or "").strip() or None
        )
    if operation in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.HOVER,
    }:
        return ResolvedInteractionTarget(
            selector=spec.selector,
            clear_obstructions=bool(spec.clear_obstructions),
        )
    if operation is InteractionOperation.INPUT:
        return ResolvedInteractionTarget(
            selector=spec.selector,
            submit_selector=spec.submit_selector,
            input_text=spec.probe_text,
        )
    return ResolvedInteractionTarget(
        clear_obstructions=bool(spec.clear_obstructions),
    )


async def coordinate_agent_page_capture(
    *,
    browser: PageCaptureBrowser,
    spec: AgentPageCaptureSpec,
    persistence_service: Any | None = None,
    supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    evidence_exit_gate: Any | None = None,
    session_command_gate: Any | None = None,
    action_execution_repository: Any | None = None,
    action_recovery_captures: Any | None = None,
    command_scope_reader: TrustedTaskCommandScopeReader | None = None,
    command_validator: InteractionCommandValidator | None = None,
    snapshot_reader: TrustedUiSnapshotReader | None = None,
    persist_before_capture: bool = False,
) -> AgentPageCaptureOutcome:
    """经 CaptureCoordinator 完成 before → action → after 双采。

    E16：`evidence_exit_gate` 省略时按灰度配置装配（默认 off＝空门禁）；
    真门禁需要 DB Session Lease，因此启用时必须同时传入
    `session_command_gate`（持久化 Session 命令闸门）。
    """
    from services.api.app.infrastructure.browser.capture_runtime import (
        BrowserInteractionExecutor,
    )

    _validate_agent_page_capture_spec(spec)
    if command_validator is not None and command_scope_reader is not None:
        raise ValueError(
            "command_validator 不能与 command_scope_reader 同时提供"
        )
    # 旧 Tool、H5 Workflow 和测试注入路径必须共享同一份动作校验器：
    # Coordinator 的范围检查与 Browser Driver 前的目标检查不能各自退回
    # 默认静态策略，否则撤销的任务范围仍可能从旧入口执行。
    shared_command_validator = command_validator or InteractionCommandValidator(
        scope_reader=command_scope_reader,
    )
    intent = build_action_intent_for_agent_page_capture(spec)
    target = build_resolved_target_for_agent_page_capture(spec)
    evidence_buffer = InMemoryActionEvidenceBuffer()
    persistence = persistence_service or InMemoryPageCapturePersistence()
    writer = AgentBrowserCaptureWriter(
        browser=browser,
        persistence=persistence,
        evidence_buffer=evidence_buffer,
        persistence_timeout_seconds=spec.persistence_timeout_seconds,
        business_parent_capture_id=spec.source_capture_id,
        persist_before_capture=persist_before_capture,
    )
    base_executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=FixedResolvedTargetResolver(
            target,
            snapshot_reader=snapshot_reader,
        ),
        evidence_buffer=evidence_buffer,
        command_validator=shared_command_validator,
    )
    executor = AgentToolInteractionExecutor(
        executor=base_executor,
        evidence_buffer=evidence_buffer,
    )
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=(
            evidence_exit_gate if evidence_exit_gate is not None else _default_evidence_exit_gate()
        ),
        session_command_gate=(
            session_command_gate
            if session_command_gate is not None
            else InProcessSessionCommandGate()
        ),
        action_execution_repository=action_execution_repository,
        action_recovery_captures=action_recovery_captures,
        command_validator=shared_command_validator,
        supplemental_evidence_policy=supplemental_evidence_policy,
    )
    try:
        coordination = await coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=spec.agent_run_id,
                intent=intent,
            )
        )
    except CaptureCoordinationError as exc:
        raise _map_coordination_error(exc) from exc
    except SupplementalEvidenceDisabledError as exc:
        raise AgentPageCaptureIntentError(
            str(exc),
            error_code=str(exc.reason_code),
            retryable=False,
            failure_stage="browser_capture",
        ) from exc

    action_result = executor.last_action_browser_result
    if action_result is None:
        raise AgentPageCaptureIntentError(
            "CaptureCoordinator 完成后缺少动作阶段浏览器证据",
            error_code="ACTION_EVIDENCE_MISSING",
            failure_stage="browser_capture",
        )
    persisted = writer.last_persisted
    if persisted is None or persisted.page_capture_id != coordination.after_capture.capture_id:
        persisted = PersistedPageCapture(
            page_capture_id=coordination.after_capture.capture_id,
            page_seq=1,
            artifacts=[
                PersistedEvidenceArtifact(
                    artifact_id=artifact_id,
                    artifact_type="unknown",
                    storage_path="",
                    content_type="",
                    checksum="",
                    size_bytes=0,
                    metadata={},
                )
                for artifact_id in coordination.after_capture.artifact_ids
            ],
        )
    # 用动作阶段完整产物（含 DOM／可见文本）供 LLM 摘要；page_capture_id 用 after
    return AgentPageCaptureOutcome(
        action_browser_result=action_result,
        before_capture_id=coordination.before_capture.capture_id,
        after_capture_id=coordination.after_capture.capture_id,
        persisted=persisted,
        artifact_ids=tuple(coordination.after_capture.artifact_ids),
    )


async def execute_agent_page_capture_with_browser(
    *,
    browser: PageCaptureBrowser,
    spec: AgentPageCaptureSpec,
    persistence_service: Any | None = None,
    supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    evidence_exit_gate: Any | None = None,
    session_command_gate: Any | None = None,
    action_execution_repository: Any | None = None,
    action_recovery_captures: Any | None = None,
    command_scope_reader: TrustedTaskCommandScopeReader | None = None,
    command_validator: InteractionCommandValidator | None = None,
    snapshot_reader: TrustedUiSnapshotReader | None = None,
    persist_before_capture: bool = False,
) -> AgentPageCaptureOutcome:
    """测试注入 browser 时走同一 Coordinator 双采路径。"""
    return await coordinate_agent_page_capture(
        browser=browser,
        spec=spec,
        persistence_service=persistence_service,
        supplemental_evidence_policy=supplemental_evidence_policy,
        evidence_exit_gate=evidence_exit_gate,
        session_command_gate=session_command_gate,
        action_execution_repository=action_execution_repository,
        action_recovery_captures=action_recovery_captures,
        command_scope_reader=command_scope_reader,
        command_validator=command_validator,
        snapshot_reader=snapshot_reader,
        persist_before_capture=persist_before_capture,
    )


def _agent_tool_capture_profile(
    operation: InteractionOperation,
    *,
    capture_role: str,
    inherit_parent_semantics: bool,
    capture_dom: bool,
    capture_visible_text: bool,
    capture_before_context: bool,
) -> CaptureProfile:
    """Agent Tool 双采配置：before 最小截图，after 合并动作证据。"""
    before_types: list[str] = ["screenshot"]
    if capture_before_context:
        if capture_dom:
            before_types.append("dom")
        if capture_visible_text:
            before_types.append("visible_text")
        before_types.append("ui_snapshot")
    after_types: list[str] = ["screenshot", "action_trace"]
    if capture_dom:
        after_types.append("dom")
    if capture_visible_text:
        after_types.append("visible_text")
    if operation is InteractionOperation.TIMED_CAPTURE:
        after_types.append("temporal_trace")
    if operation is InteractionOperation.NETWORK_FAULT:
        after_types.append("har")
    after_role = capture_role.strip() if capture_role.strip() else "primary"
    return CaptureProfile(
        profile_id=f"web-agent-tool-{operation.value}-v1",
        before=CaptureDirective(
            role="before_action",
            artifact_types=tuple(before_types),
        ),
        after=CaptureDirective(
            role=after_role,
            artifact_types=tuple(after_types),
            inherit_parent_semantics=inherit_parent_semantics,
        ),
    )


def _validate_agent_page_capture_spec(spec: AgentPageCaptureSpec) -> None:
    """复用浏览器层请求校验，保留旧 Tool 错误码与文案。"""
    from services.api.app.infrastructure.browser.page_capture import _validate_request

    try:
        _validate_request(
            PageCaptureRequest(
                mode=spec.mode,
                url=spec.url,
                selector=spec.selector,
                trigger_action=spec.trigger_action,
                probe_text=spec.probe_text,
                submit_selector=spec.submit_selector,
                fault_url_pattern=spec.fault_url_pattern,
                navigation_depth=spec.navigation_depth,
                clear_obstructions=spec.clear_obstructions,
                capture_dom=spec.capture_dom,
                capture_visible_text=spec.capture_visible_text,
                capture_screenshot=spec.capture_screenshot,
                full_page=spec.full_page,
                frame_count=spec.frame_count,
                interval_ms=spec.interval_ms,
                timeout_ms=spec.timeout_ms,
                session_id=spec.session_id,
                viewport_width=spec.viewport_width,
                viewport_height=spec.viewport_height,
                screen_width=spec.screen_width,
                screen_height=spec.screen_height,
                device_scale_factor=spec.device_scale_factor,
                is_mobile=spec.is_mobile,
                has_touch=spec.has_touch,
            )
        )
    except BlockedPageCaptureActionError as exc:
        raise AgentPageCaptureIntentError(
            str(exc),
            error_code=exc.error_code,
            failure_stage="browser_interaction",
        ) from exc


def _map_coordination_error(exc: CaptureCoordinationError) -> AgentPageCaptureIntentError:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, AgentPageCaptureIntentError):
            return current
        current = current.__cause__ or current.__context__
    interaction = exc.interaction
    error_code = exc.error_code
    message = str(exc)
    retryable = False
    failure_stage = "browser_capture"
    if interaction is not None:
        if interaction.error_code:
            error_code = interaction.error_code
        if interaction.error_message:
            message = interaction.error_message
        retryable = bool(interaction.retryable)
    if exc.stage is CaptureCoordinationStage.ACTION:
        failure_stage = "browser_interaction"
    elif exc.stage is CaptureCoordinationStage.AFTER_CAPTURE:
        failure_stage = "artifact_persistence"
        if error_code == "after_capture_failed":
            error_code = "PAGE_CAPTURE_PERSISTENCE_FAILED"
    elif exc.stage is CaptureCoordinationStage.BEFORE_CAPTURE:
        failure_stage = "browser_capture"
        if error_code == "before_capture_failed":
            error_code = "PAGE_CAPTURE_FAILED"
    if error_code in {"ACTION_TIMED_OUT", "ACTION_DEADLINE_EXCEEDED"}:
        error_code = "PAGE_CAPTURE_TIMEOUT"
        failure_stage = "browser_capture"
    return AgentPageCaptureIntentError(
        message,
        error_code=error_code,
        retryable=retryable,
        failure_stage=failure_stage,
    )


def _unproven_obstruction_dismissal(result: PageCaptureResult) -> dict[str, Any] | None:
    for item in result.action_trace or []:
        if not isinstance(item, dict):
            continue
        if item.get("result") in _UNPROVEN_OBSTRUCTION_DISMISSAL_RESULTS:
            return item
    return None


def _is_sentinel_parent(value: str | None) -> bool:
    if value is None:
        return True
    text = value.strip()
    return text == "legacy-agent-entry" or text.startswith("agent-entry:")


def _business_persist_parent(value: str | None) -> str | None:
    """业务落库父级只保留显式 supersedes；哨兵入口不写 parent。"""
    if _is_sentinel_parent(value):
        return None
    return value.strip() if isinstance(value, str) else None


async def _call_persistence_persist(persistence: Any, **kwargs: Any) -> PersistedPageCapture:
    """兼容仅接受最小参数的测试 Fake persistence。"""
    try:
        return await persistence.persist(**kwargs)
    except TypeError:
        return await persistence.persist(
            task_id=kwargs["task_id"],
            agent_run_id=kwargs["agent_run_id"],
            result=kwargs["result"],
        )


def _map_operation(spec: AgentPageCaptureSpec) -> InteractionOperation:
    if spec.mode is PageCaptureMode.GOTO:
        return InteractionOperation.NAVIGATE
    if spec.mode is PageCaptureMode.OBSERVE:
        return InteractionOperation.OBSERVE
    if spec.mode is PageCaptureMode.TIMED_CAPTURE:
        return InteractionOperation.TIMED_CAPTURE
    if spec.mode is not PageCaptureMode.SINGLE_TRIGGER:
        raise AgentPageCaptureIntentError(
            f"不支持的采集模式：{spec.mode}",
            error_code="WEB_ACTION_UNSUPPORTED",
        )
    action = str(spec.trigger_action or "").strip().casefold()
    mapping = {
        PageTriggerAction.CLICK.value: InteractionOperation.CLICK,
        PageTriggerAction.HOVER.value: InteractionOperation.HOVER,
        PageTriggerAction.FILL.value: InteractionOperation.INPUT,
        PageTriggerAction.SUBMIT_PROBE.value: InteractionOperation.INPUT,
        PageTriggerAction.SCROLL.value: InteractionOperation.SCROLL,
        PageTriggerAction.BACK.value: InteractionOperation.BACK,
        PageTriggerAction.REFRESH.value: InteractionOperation.REFRESH,
        PageTriggerAction.NETWORK_FAULT.value: InteractionOperation.NETWORK_FAULT,
    }
    operation = mapping.get(action)
    if operation is None:
        raise AgentPageCaptureIntentError(
            f"不支持的 trigger_action：{spec.trigger_action!r}",
            error_code="WEB_ACTION_UNSUPPORTED",
            failure_stage="browser_interaction",
        )
    return operation


def _semantic_target_for(operation: InteractionOperation) -> str | None:
    if operation is InteractionOperation.NAVIGATE:
        return "entry-navigation"
    if operation in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.HOVER,
        InteractionOperation.INPUT,
    }:
        return "agent-requested-interaction"
    if operation is InteractionOperation.NETWORK_FAULT:
        return "network-fault-injection"
    return None


# 兼容旧测试导入名
async def execute_agent_page_capture_intent(**kwargs: Any) -> PageCaptureResult:
    """已废弃：请改用 coordinate_agent_page_capture。"""
    raise AgentPageCaptureIntentError(
        "execute_agent_page_capture_intent 已切换为 CaptureCoordinator 双采",
        error_code="WEB_ACTION_UNSUPPORTED",
    )
