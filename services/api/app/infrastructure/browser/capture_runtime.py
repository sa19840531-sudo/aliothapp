"""把 V2 Capture/Interaction 应用端口接到现有 Playwright 与证据存储。"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CapturePhase,
    CaptureWriteRequest,
    ExecuteInteractionRequest,
    SessionCommandGate,
)
from services.api.app.application.capture_coordinator import (
    CaptureCoordinator,
)
from services.api.app.application.h5_exploration_workflow import (
    H5_SURFACE_SESSION_OWNER,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    InteractionCommandValidator,
)
from services.api.app.application.interaction_runtime import (
    H5_DYNAMIC_LOGICAL_TARGET_MODES,
    ActionEvidenceBuffer,
    InMemoryActionEvidenceBuffer,
    LogicalTargetRuntimeBinding,
    PendingActionEvidence,
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
    SemanticTargetResolver,
    TrustedUiSnapshotReader,
)
from services.api.app.application.page_capture_persistence import (
    SUPPORTED_CAPTURE_ARTIFACT_TYPES,
    PageCapturePersistenceService,
    PageCaptureRecoveryService,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.application.web_ad_landing_evidence import (
    WebAdDirectCaptureCommand,
    WebAdLandingCaptureError,
    WebAdLandingCaptureReceipt,
)
from services.api.app.application.web_exploration_session_cleanup import (
    WebExplorationSessionCleanup,
)
from services.api.app.core.config import (
    DEFAULT_WEB_INTERACTION_TIMEOUT_SECONDS,
    get_settings,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
    SurfaceSessionStatus,
    build_action_execution_id,
)
from services.api.app.infrastructure.browser.page_capture import (
    FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR,
    SAFE_PROBE_TEXT_PATTERN,
    STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL,
    VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_CODE,
    VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_MESSAGE,
    BlockedPageCaptureActionError,
    BrowserSessionCrashedError,
    PageCaptureArtifact,
    PageCaptureBrowser,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PageInteractionFailedError,
    PageTriggerAction,
    PlaywrightPageCaptureBrowser,
    VisualActionCommittedCleanupInterruptedError,
    _visual_action_was_committed,
    capture_page_with_timeout,
)
from services.api.app.infrastructure.browser.playwright_cli_process import PlaywrightCliError
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
    WebDesktopCapturePolicyBrowser,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.command_scope_reader import PostgresTaskCommandScopeReader
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    PostgresEvidenceExitGate,
)
from services.api.app.infrastructure.db.h5_probe_action_execution_repository import (
    PostgresH5ProbeActionExecutionRepository,
)
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.app.infrastructure.db.session_command_gate import (
    PostgresSessionCommandGate,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.web_exploration_session_cleanup_repository import (
    PostgresWebExplorationSessionCleanupRepository,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

_REOBSERVABLE_BLOCKED_ACTION_ERROR_CODES = frozenset(
    {
        "POPUP_DISMISSAL_NOT_FOUND",
        "POPUP_DISMISSAL_NOT_OBSERVED",
        "STARTUP_AD_DISMISSAL_NOT_FOUND",
        "STARTUP_AD_DISMISSAL_NOT_OBSERVED",
        "TOPMOST_OBSTRUCTION_STILL_VISIBLE",
        "LOGICAL_SOURCE_STATE_STALE",
        "VISUAL_SOURCE_PAGE_STALE",
        "VISUAL_TARGET_NOT_AT_COORDINATE",
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "VISUAL_TARGET_NOT_UNIQUE",
        "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
        "VISUAL_NAVIGATION_NO_EFFECT",
    }
)
_ACTION_RUNTIME_ARTIFACT_TYPES = frozenset({"action_trace", "har", "console_log"})
_ACTION_BOUND_SCREENSHOT_PURPOSES = frozenset({"ad_landing_consistency", "transient_message"})
_H5_PAYMENT_HORIZONTAL_CAPTURE_ROLE = "h5_payment_horizontal_collection"
_H5_VERTICAL_SCROLL_CAPTURE_ROLE = "h5_vertical_scroll_collection"
_VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED_CODE = "VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED"
_VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED_MESSAGE = (
    "视觉点击已提交，但动作后页面条件未完成，禁止自动重试"
)
_COMMITTED_REOBSERVABLE_VISUAL_CODES = frozenset(
    {"VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT", "VISUAL_NAVIGATION_NO_EFFECT"}
)


class BrowserInteractionExecutor:
    """唯一把 Web ActionIntent 翻译为 Playwright 请求的生产适配器。"""

    def __init__(
        self,
        *,
        browser: PageCaptureBrowser,
        target_resolver: SemanticTargetResolver,
        evidence_buffer: ActionEvidenceBuffer,
        action_timeout_seconds: float = DEFAULT_WEB_INTERACTION_TIMEOUT_SECONDS,
        command_validator: InteractionCommandValidator | None = None,
    ) -> None:
        if action_timeout_seconds <= 0:
            raise ValueError("浏览器单动作超时必须大于 0")
        self._browser = browser
        self._target_resolver = target_resolver
        self._evidence_buffer = evidence_buffer
        self._action_timeout_seconds = float(action_timeout_seconds)
        self._command_validator = command_validator
        self._execution_registry_lock = asyncio.Lock()
        self._execution_locks: dict[str, asyncio.Lock] = {}
        self._execution_users: dict[str, int] = {}

    async def execute(
        self,
        request: ExecuteInteractionRequest,
        *,
        resolved_target: Any | None = None,
        expose_driver_error_details: bool = False,
    ) -> InteractionResult:
        intent = request.intent
        action_execution_id = request.action_execution_id or build_action_execution_id(intent)
        async with self._hold_execution(action_execution_id):
            existing = await self._evidence_buffer.get(action_execution_id=action_execution_id)
            if existing is not None:
                if (
                    existing.interaction.intent_id == intent.intent_id
                    and existing.interaction.source_capture_id == request.source_capture_id
                ):
                    return existing.interaction
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code="ACTION_ALREADY_EXECUTED",
                    error_message="相同幂等动作已经执行，禁止对另一份前 Capture 重放",
                )
            if intent.surface_kind is not SurfaceKind.WEB:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code="SURFACE_DRIVER_UNAVAILABLE",
                    error_message="当前适配器只支持 Web Surface",
                )
            remaining_timeout_seconds = _remaining_timeout_seconds(intent.deadline)
            if remaining_timeout_seconds <= 0:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.TIMED_OUT,
                    error_code="ACTION_DEADLINE_EXCEEDED",
                    error_message="ActionIntent deadline 已到",
                    retryable=True,
                )
            timeout_seconds = min(
                remaining_timeout_seconds,
                self._action_timeout_seconds,
            )
            browser_request: PageCaptureRequest | None = None
            try:
                if intent.parameters.get("capture_ad_landing_evidence") is True:
                    raise BlockedPageCaptureActionError(
                        "广告证据只能由 href 隔离直开适配器执行",
                        error_code="WEB_AD_CLICK_FORBIDDEN",
                    )
                # Resolver 与 Browser 必须共享单动作墙钟预算；否则前者或页面事件
                # 任一挂起都可能独占 Worker 直到整条 Run 的 90 分钟 deadline。
                async with asyncio.timeout(timeout_seconds):
                    # resolved_target 仅限 Runtime 内部桥接（如 Main Agent Tool）；
                    # 正式 Explorer 路径必须走 SemanticTargetResolver。
                    target = resolved_target or await self._target_resolver.resolve(
                        SemanticTargetResolutionRequest(
                            task_id=intent.task_id,
                            session_id=intent.session_id,
                            source_capture_id=request.source_capture_id,
                            action_type=intent.action_type,
                            semantic_target=intent.semantic_target,
                            parameters=intent.parameters,
                        )
                    )
                    if self._command_validator is not None:
                        command = CaptureCoordinationCommand(
                            agent_run_id=request.agent_run_id,
                            intent=intent,
                        )
                        if intent.action_type is InteractionOperation.NAVIGATE:
                            # URL 导航没有可供 DOM/视觉解析的控件语义；其安全边界是
                            # 服务端冻结的动作范围与 allowed_hosts，仍需在 Driver 前复核。
                            await self._command_validator.validate_for(
                                command,
                                expected_finalization_epoch=request.finalization_epoch,
                            )
                        else:
                            await self._command_validator.validate_resolved_target_for(
                                command,
                                trusted_target_texts=target.trusted_target_texts,
                                target_trust=target.target_trust
                                or ("trusted" if target.trusted_target_texts else None),
                                target_role=target.trusted_target_role,
                                expected_finalization_epoch=request.finalization_epoch,
                            )
                    browser_request = _build_browser_action_request(
                        request,
                        target=target,
                        timeout_seconds=timeout_seconds,
                    )
                    if self._command_validator is not None:
                        command = CaptureCoordinationCommand(
                            agent_run_id=request.agent_run_id, intent=intent
                        )

                        async def validate_current_location(url: str) -> None:
                            # goto 只校验其受信目的地址，允许从空白页启动或从外站回源；
                            # 其余动作必须在真实浏览器当前页仍属于业务范围时执行。
                            if intent.action_type is InteractionOperation.NAVIGATE:
                                await self._command_validator.validate_for(
                                    command, expected_finalization_epoch=request.finalization_epoch
                                )
                            else:
                                await self._command_validator.validate_runtime_location_for(
                                    command,
                                    url=url,
                                    expected_finalization_epoch=request.finalization_epoch,
                                )

                        browser_request = replace(
                            browser_request, _pre_action_validation=validate_current_location
                        )
                        bootstrap = (
                            (intent.intent_id == f"h5-bootstrap:{request.agent_run_id}"
                             or (intent.intent_id.startswith("h5-recapture-bootstrap:")
                                 and re.fullmatch(
                                     rf"h5:{re.escape(request.agent_run_id)}:recapture:[0-9a-f]{{20}}",
                                     intent.session_id,
                                 )))
                            and intent.source_capture_id == f"h5-entry:{request.agent_run_id}"
                            and intent.parameters.get("initial_surface_action") is True
                        )
                        recovery_prefix = f"h5:{request.agent_run_id}:recovery:"
                        recovery_suffix = intent.session_id.removeprefix(recovery_prefix)
                        recovery_source = str(
                            intent.parameters.get("recovery_source_session_id") or ""
                        )
                        source_suffix = recovery_source.removeprefix(recovery_prefix)
                        recovery = (
                            intent.parameters.get("h5_browser_recovery") is True
                            and intent.intent_id.startswith("h5-browser-recovery:")
                            and intent.session_id.startswith(recovery_prefix)
                            and len(recovery_suffix) == 20
                            and all(c in "0123456789abcdef" for c in recovery_suffix)
                            and (
                                recovery_source == f"h5:{request.agent_run_id}"
                                or (
                                    recovery_source.startswith(recovery_prefix)
                                    and len(source_suffix) == 20
                                    and all(c in "0123456789abcdef" for c in source_suffix)
                                )
                            )
                        )
                        # 同 Run 的受控恢复也可能重新落入原生身份重定向，仍复用冻结边界。
                        if intent.action_type is InteractionOperation.NAVIGATE and (
                            bootstrap or recovery
                        ):
                            async def validate_h5_entry_landing(url: str) -> bool:
                                try:
                                    await self._command_validator.validate_runtime_location_for(
                                        command,
                                        url=url,
                                        expected_finalization_epoch=request.finalization_epoch,
                                    )
                                except InteractionCommandValidationError as exc:
                                    if exc.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION":
                                        return False
                                    raise
                                return True

                            browser_request = replace(
                                browser_request,
                                _h5_entry_landing_validation=validate_h5_entry_landing,
                            )
                    browser_result = await capture_page_with_timeout(
                        request=browser_request,
                        browser=self._browser,
                        timeout_seconds=timeout_seconds,
                    )
                    if browser_result.execution_status != "success":
                        raise RuntimeError(browser_result.error_message or "浏览器动作未成功")
                    landing_failure = await self._validate_navigation_landing(
                        request,
                        action_execution_id=action_execution_id,
                        browser_result=browser_result,
                    )
                    if landing_failure is not None:
                        return landing_failure
                    document_failure = await self._validate_navigation_document(
                        request,
                        action_execution_id=action_execution_id,
                        browser_result=browser_result,
                    )
                    if document_failure is not None:
                        return document_failure
                    _attach_logical_target_audit(
                        browser_result,
                        target=target,
                    )
                    _annotate_trusted_startup_ad_layer_clearance(
                        browser_result,
                        parameters=intent.parameters,
                        runtime_binding=(
                            browser_request._logical_target_binding
                            if str(browser_request.session_id or "").startswith("h5:")
                            else None
                        ),
                    )
                    _annotate_trusted_popup_clearance(
                        browser_result,
                        parameters=intent.parameters,
                        runtime_binding=browser_request._logical_target_binding,
                        session_id=intent.session_id,
                    )
            except InteractionCommandValidationError as exc:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    retryable=exc.retryable,
                    state_diff={"driver_invoked": False, "command_scope_verified": False},
                )
            except SemanticTargetResolutionError as exc:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    retryable=exc.retryable,
                )
            except BlockedPageCaptureActionError as exc:
                popup_trace = getattr(exc, "_h5_popup_action_trace", None)
                if (intent.session_id.startswith("h5:")
                    and exc.error_code == "POPUP_DISMISSAL_NOT_OBSERVED"
                    and isinstance(popup_trace, list) and popup_trace):
                    interaction = _error_result(
                        request, action_execution_id=action_execution_id,
                        status=InteractionResultStatus.BLOCKED, error_code=exc.error_code,
                        error_message=str(exc), retryable=True,
                        state_summary={"post_action_evidence_available": True},
                        state_diff={"driver_invoked": True, "popup_dismissal_verified": False},
                    )
                    await self._evidence_buffer.put(PendingActionEvidence(
                        interaction=interaction,
                        browser_result=PageCaptureResult(
                            execution_status="failed", mode=browser_request.mode.value,
                            reachable=None, final_url=None, title=None, status_code=None,
                            metrics={"postcondition_complete": False},
                            action_trace=popup_trace, artifacts=[PageCaptureArtifact(
                                artifact_type="action_trace", artifact_id=None, content=popup_trace,
                            )],
                        ),
                    ))
                    return interaction
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                if committed_result is not None:
                    return committed_result
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code=exc.error_code,
                    error_message=str(exc) or "页面动作被安全策略阻断",
                    retryable=exc.error_code in _REOBSERVABLE_BLOCKED_ACTION_ERROR_CODES,
                )
            except PageInteractionFailedError as exc:
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                if committed_result is not None:
                    return committed_result
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.FAILED,
                    error_code=exc.error_code,
                    error_message=(
                        str(exc)
                        if exc.error_code == "VERIFIED_CARD_TARGET_STALE"
                        else "页面动作未完成"
                    ),
                    retryable=exc.retryable,
                )
            except BrowserSessionCrashedError as exc:
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                if committed_result is not None:
                    return committed_result
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.FAILED,
                    error_code=exc.error_code,
                    error_message="浏览器会话不可用",
                    retryable=True,
                )
            except VisualActionCommittedCleanupInterruptedError as exc:
                assert browser_request is not None
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                assert committed_result is not None
                return committed_result
            except TimeoutError as exc:
                if (
                    browser_request is not None
                    and browser_request._cli_action_state.get("outcome") == "unknown"
                ):
                    return await self._record_cli_uncertain_action(
                        request,
                        action_execution_id=action_execution_id,
                        error_code="CLI_ACTION_TIMED_OUT",
                        timed_out=True,
                    )
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                if committed_result is not None:
                    return committed_result
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.TIMED_OUT,
                    error_code="ACTION_TIMED_OUT",
                    error_message="浏览器动作超时",
                    retryable=True,
                )
            except PlaywrightCliError as exc:
                if exc.action_uncertain:
                    return await self._record_cli_uncertain_action(
                        request,
                        action_execution_id=action_execution_id,
                        error_code=exc.error_code,
                    )
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code=exc.error_code,
                    error_message="CLI 操作在执行前被拒绝",
                    retryable=False,
                    state_diff={"driver_invoked": False},
                )
            except ValueError as exc:
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                if committed_result is not None:
                    return committed_result
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code="ACTION_REQUEST_REJECTED",
                    error_message=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - 驱动异常必须转为结构化结果
                committed_result = await self._committed_visual_action_failure(
                    request,
                    action_execution_id=action_execution_id,
                    browser_request=browser_request,
                    exc=exc,
                )
                if committed_result is not None:
                    return committed_result
                if _is_playwright_timeout_error(exc):
                    return _error_result(
                        request,
                        action_execution_id=action_execution_id,
                        status=InteractionResultStatus.TIMED_OUT,
                        error_code="ACTION_TIMED_OUT",
                        error_message="浏览器动作超时",
                        retryable=True,
                    )
                if _is_playwright_navigation_connection_refused(exc):
                    return _error_result(
                        request,
                        action_execution_id=action_execution_id,
                        status=InteractionResultStatus.FAILED,
                        error_code="TARGET_CONNECTION_REFUSED",
                        error_message="目标站点拒绝连接",
                        retryable=False,
                        state_summary={
                            "cause_type": type(exc).__name__[:120],
                            "navigation_error": "ERR_CONNECTION_REFUSED",
                        },
                    )
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.FAILED,
                    error_code="SURFACE_DRIVER_FAILED",
                    error_message=(
                        f"Surface Driver 执行失败：{exc}"
                        if expose_driver_error_details
                        else "Surface Driver 执行失败"
                    ),
                    retryable=True,
                    state_summary={"cause_type": type(exc).__name__[:120]},
                )

            state_summary = _public_state_summary(browser_result)
            state_diff = _public_state_diff(browser_result)
            postcondition_satisfied = _logical_navigation_postcondition_satisfied(
                parameters=intent.parameters,
                state_diff=state_diff,
            )
            if not postcondition_satisfied:
                if browser_request is not None and _visual_action_was_committed(browser_request):
                    state_diff = {
                        **state_diff,
                        "visual_action_committed": True,
                    }
                postcondition_error = _logical_navigation_postcondition_error(
                    parameters=intent.parameters,
                    state_diff=state_diff,
                )
                assert postcondition_error is not None
                is_visual_no_effect = postcondition_error == "VISUAL_NAVIGATION_NO_EFFECT"
                is_visual_insufficient = (
                    postcondition_error == "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT"
                )
                is_scroll_no_effect = postcondition_error == "SCROLL_EFFECT_NOT_OBSERVED"
                is_popup_layer_advanced = (
                    postcondition_error == "H5_POPUP_CHAIN_REMAINS_BLOCKED"
                )
                interaction = InteractionResult(
                    task_id=intent.task_id,
                    session_id=intent.session_id,
                    intent_id=intent.intent_id,
                    action_execution_id=action_execution_id,
                    source_capture_id=request.source_capture_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code=postcondition_error,
                    error_message=(
                        "视觉动作未产生可持续页面变化"
                        if is_visual_no_effect
                        else "视觉动作后的稳定帧证据不足"
                        if is_visual_insufficient
                        else "滑动动作未观察到目标容器发生位移"
                        if is_scroll_no_effect
                        else "当前弹层已关闭，但仍有下一层弹窗需要继续处理"
                        if is_popup_layer_advanced
                        else "动作后证据未证明进入新的逻辑页面"
                    ),
                    retryable=True,
                    state_summary={
                        **state_summary,
                        "post_action_evidence_available": True,
                    },
                    state_diff=state_diff,
                )
                await self._evidence_buffer.put(
                    PendingActionEvidence(
                        interaction=interaction,
                        browser_result=browser_result,
                    )
                )
                return interaction

            interaction = InteractionResult(
                task_id=intent.task_id,
                session_id=intent.session_id,
                intent_id=intent.intent_id,
                action_execution_id=action_execution_id,
                source_capture_id=request.source_capture_id,
                status=InteractionResultStatus.EVIDENCE_PENDING,
                state_summary=state_summary,
                state_diff=state_diff,
            )
            await self._evidence_buffer.put(
                PendingActionEvidence(
                    interaction=interaction,
                    browser_result=browser_result,
                )
            )
            return interaction

    async def _validate_navigation_landing(
        self,
        request: ExecuteInteractionRequest,
        *,
        action_execution_id: str,
        browser_result: PageCaptureResult,
    ) -> InteractionResult | None:
        """导航已经发生后复核真实落点，越界仍保留动作后诊断证据。"""
        if (
            request.intent.action_type is not InteractionOperation.NAVIGATE
            or self._command_validator is None
        ):
            return None
        navigation_scope = {
            "verified": False,
            "requested_url": _public_location(request.intent.parameters.get("destination_url")),
            "final_url": _public_location(browser_result.final_url),
            "main_response_status": browser_result.status_code,
        }
        try:
            await self._command_validator.validate_runtime_location_for(
                CaptureCoordinationCommand(
                    agent_run_id=request.agent_run_id,
                    intent=request.intent,
                ),
                url=browser_result.final_url,
                expected_finalization_epoch=request.finalization_epoch,
            )
        except InteractionCommandValidationError as exc:
            # 不走前置拒绝分支：浏览器已导航，必须如实记录副作用并封存后态。
            interaction = _error_result(
                request,
                action_execution_id=action_execution_id,
                status=InteractionResultStatus.BLOCKED,
                error_code=exc.error_code,
                error_message=str(exc),
                retryable=exc.retryable,
                state_summary={
                    **_public_state_summary(browser_result),
                    "post_action_evidence_available": True,
                },
                state_diff={
                    **_public_state_diff(browser_result),
                    "driver_invoked": True,
                    "command_scope_verified": False,
                    "arrival_verified": False,
                },
            )
            diagnostic_result = replace(
                browser_result,
                execution_status="failed",
                error_message=str(exc),
                metrics={
                    **browser_result.metrics,
                    "diagnostic_capture": True,
                    "diagnostic_role": "post_navigation_out_of_scope",
                    "postcondition_complete": False,
                    "navigation_scope": {
                        **navigation_scope,
                        "error_code": exc.error_code,
                    },
                },
            )
            await self._evidence_buffer.put(
                PendingActionEvidence(
                    interaction=interaction,
                    browser_result=diagnostic_result,
                )
            )
            return interaction
        # 域边界验证只证明落点属于任务范围，不代替页面类型或业务到达验证。
        browser_result.metrics["navigation_scope"] = {**navigation_scope, "verified": True}
        return None

    async def _validate_navigation_document(
        self,
        request: ExecuteInteractionRequest,
        *,
        action_execution_id: str,
        browser_result: PageCaptureResult,
    ) -> InteractionResult | None:
        """即使主文档同域，也不能把空白、不可读或错误响应提交为业务页面。"""
        if request.intent.action_type is not InteractionOperation.NAVIGATE:
            return None
        scope = browser_result.metrics.get("navigation_scope")
        navigation_scope = dict(scope) if isinstance(scope, Mapping) else {
            "verified": False,
            "requested_url": _public_location(request.intent.parameters.get("destination_url")),
            "final_url": _public_location(browser_result.final_url),
            "main_response_status": browser_result.status_code,
        }
        document = browser_result.metrics.get("navigation_document")
        document_error = (
            str(document.get("error_code") or "WEB_NAVIGATION_DOCUMENT_UNAVAILABLE")
            if isinstance(document, Mapping) and document.get("inspectable") is False
            else "WEB_NAVIGATION_HTTP_ERROR"
            if browser_result.status_code is not None
            and (browser_result.status_code >= 400 or browser_result.status_code in {204, 205})
            else None
        )
        if document_error is not None:
            message = "目标导航未形成可读取的业务文档，已保留实际落点的诊断证据"
            interaction = _error_result(
                request,
                action_execution_id=action_execution_id,
                status=InteractionResultStatus.FAILED,
                error_code=document_error,
                error_message=message,
                retryable=False,
                state_summary={
                    **_public_state_summary(browser_result),
                    "post_action_evidence_available": True,
                },
                state_diff={
                    **_public_state_diff(browser_result),
                    "driver_invoked": True,
                    "command_scope_verified": navigation_scope.get("verified") is True,
                    "arrival_verified": False,
                },
            )
            await self._evidence_buffer.put(
                PendingActionEvidence(
                    interaction=interaction,
                    browser_result=replace(
                        browser_result,
                        execution_status="failed",
                        error_message=message,
                        metrics={
                            **browser_result.metrics,
                            "diagnostic_capture": True,
                            "diagnostic_role": "post_navigation_document_unavailable",
                            "postcondition_complete": False,
                            "navigation_scope": {
                                **navigation_scope,
                                "error_code": document_error,
                            },
                        },
                    ),
                )
            )
            return interaction
        return None

    async def _record_cli_uncertain_action(
        self,
        request: ExecuteInteractionRequest,
        *,
        action_execution_id: str,
        error_code: str,
        timed_out: bool = False,
    ) -> InteractionResult:
        """CLI 已尝试发送动作但响应不可靠，只读留证且禁止换执行器重放。"""
        interaction = _error_result(
            request,
            action_execution_id=action_execution_id,
            status=(
                InteractionResultStatus.TIMED_OUT if timed_out else InteractionResultStatus.FAILED
            ),
            error_code=error_code,
            error_message="CLI 动作结果未知，已隔离动作能力并转为只读后态采集",
            retryable=False,
            state_summary={"post_action_evidence_available": True, "provider_outcome": "unknown"},
            state_diff={"provider": "playwright_cli", "arrival_verified": False},
        )
        safe_trace = [
            {
                "provider": "playwright_cli",
                "action": request.intent.action_type.value,
                "provider_outcome": "unknown",
                "error_code": error_code,
                "arrival_verified": False,
            }
        ]
        await self._evidence_buffer.put(
            PendingActionEvidence(
                interaction=interaction,
                browser_result=PageCaptureResult(
                    execution_status="failed",
                    mode="single_trigger",
                    reachable=None,
                    final_url=None,
                    title=None,
                    status_code=None,
                    metrics={"provider_outcome": "unknown", "postcondition_complete": False},
                    action_trace=safe_trace,
                    artifacts=[
                        PageCaptureArtifact(
                            artifact_type="action_trace",
                            artifact_id=None,
                            content=safe_trace,
                        )
                    ],
                ),
            )
        )
        return interaction

    async def _record_committed_visual_action(
        self,
        request: ExecuteInteractionRequest,
        *,
        action_execution_id: str,
        browser_request: PageCaptureRequest,
        status: InteractionResultStatus = InteractionResultStatus.FAILED,
        error_code: str = VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_CODE,
        error_message: str = VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_MESSAGE,
        retryable: bool = False,
        cleanup_complete: bool = False,
    ) -> InteractionResult:
        """记录已产生副作用的超时边界，确保同一动作身份不再触发 Driver。"""
        interaction = _error_result(
            request,
            action_execution_id=action_execution_id,
            status=status,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            state_diff={"visual_action_committed": True},
        )
        browser_result = PageCaptureResult(
            execution_status="failed",
            mode=browser_request.mode.value,
            reachable=None,
            final_url=None,
            title=None,
            status_code=None,
            error_message=error_message,
            metrics={
                "visual_action_committed": True,
                "cleanup_complete": cleanup_complete,
                "postcondition_complete": False,
            },
        )
        await self._evidence_buffer.put(
            PendingActionEvidence(
                interaction=interaction,
                browser_result=browser_result,
            )
        )
        return interaction

    async def _committed_visual_action_failure(
        self,
        request: ExecuteInteractionRequest,
        *,
        action_execution_id: str,
        browser_request: PageCaptureRequest | None,
        exc: Exception,
    ) -> InteractionResult | None:
        """先固化点击副作用，再按安全动作后类别映射普通异常。"""
        if browser_request is None or not _visual_action_was_committed(browser_request):
            return None
        if isinstance(exc, (VisualActionCommittedCleanupInterruptedError, TimeoutError)):
            return await self._record_committed_visual_action(
                request,
                action_execution_id=action_execution_id,
                browser_request=browser_request,
            )
        if (
            isinstance(exc, BlockedPageCaptureActionError)
            and exc.error_code in _COMMITTED_REOBSERVABLE_VISUAL_CODES
        ):
            message = (
                "视觉动作未产生可持续页面变化"
                if exc.error_code == "VISUAL_NAVIGATION_NO_EFFECT"
                else "视觉动作后的稳定帧证据不足"
            )
            return await self._record_committed_visual_action(
                request,
                action_execution_id=action_execution_id,
                browser_request=browser_request,
                status=InteractionResultStatus.BLOCKED,
                error_code=exc.error_code,
                error_message=message,
                retryable=True,
                cleanup_complete=True,
            )
        return await self._record_committed_visual_action(
            request,
            action_execution_id=action_execution_id,
            browser_request=browser_request,
            error_code=_VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED_CODE,
            error_message=_VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED_MESSAGE,
            cleanup_complete=True,
        )

    @asynccontextmanager
    async def _hold_execution(self, action_execution_id: str) -> AsyncIterator[None]:
        """串行化同一动作身份，并在最后一个等待者退出后回收锁。"""
        async with self._execution_registry_lock:
            lock = self._execution_locks.setdefault(action_execution_id, asyncio.Lock())
            self._execution_users[action_execution_id] = (
                self._execution_users.get(action_execution_id, 0) + 1
            )
        try:
            async with lock:
                yield
        finally:
            async with self._execution_registry_lock:
                remaining = self._execution_users[action_execution_id] - 1
                if remaining == 0:
                    self._execution_users.pop(action_execution_id, None)
                    self._execution_locks.pop(action_execution_id, None)
                else:
                    self._execution_users[action_execution_id] = remaining


class _H5ProbeInteractionExecutor:
    """仅为 H5 定向补采保留底层 Driver 失败原因。"""

    def __init__(self, delegate: BrowserInteractionExecutor) -> None:
        self._delegate = delegate

    async def execute(self, request: ExecuteInteractionRequest) -> InteractionResult:
        return await self._delegate.execute(
            request,
            expose_driver_error_details=True,
        )


def _is_playwright_timeout_error(exc: Exception) -> bool:
    error_type = type(exc)
    return error_type.__name__ == "TimeoutError" and error_type.__module__.startswith("playwright.")


def _is_playwright_navigation_connection_refused(exc: Exception) -> bool:
    """仅识别 Playwright 顶层导航被目标端拒绝，避免误报驱动故障。"""
    error_type = type(exc)
    message = str(exc)
    return (
        error_type.__name__ == "Error"
        and error_type.__module__.startswith("playwright.")
        and "Page.goto:" in message
        and "net::ERR_CONNECTION_REFUSED" in message
    )


class BrowserCaptureWriter:
    """采集当前会话并提交不可变 Capture，同时合并动作阶段审计产物。"""

    def __init__(
        self,
        *,
        browser: PageCaptureBrowser,
        persistence: PageCapturePersistenceService,
        evidence_buffer: ActionEvidenceBuffer,
    ) -> None:
        self._browser = browser
        self._persistence = persistence
        self._evidence_buffer = evidence_buffer

    async def capture(self, request: CaptureWriteRequest) -> CaptureRef:
        if request.surface_kind is not SurfaceKind.WEB:
            raise ValueError("BrowserCaptureWriter 仅接受 Web Surface")
        timeout_seconds = _remaining_timeout_seconds(request.deadline)
        if timeout_seconds <= 0:
            raise TimeoutError("Capture deadline 已到")
        required_types = set(request.directive.artifact_types)
        fresh_surface = (
            request.bootstrap_blank_surface or request.recovery_bootstrap_intent is not None
        )
        if fresh_surface and (
            request.phase is not CapturePhase.BEFORE_ACTION or request.parent_capture_id is not None
        ):
            raise RuntimeError("首导航空白 Surface 合同无效")
        pristine_blank_bootstrap = fresh_surface
        if pristine_blank_bootstrap:
            required_types.discard("screenshot")
        mode = (
            PageCaptureMode.TIMED_CAPTURE
            if "temporal_trace" in required_types
            else PageCaptureMode.OBSERVE
        )
        result = await capture_page_with_timeout(
            request=PageCaptureRequest(
                mode=mode,
                capture_dom="dom" in required_types,
                capture_visible_text="visible_text" in required_types,
                capture_screenshot="screenshot" in required_types,
                capture_media_state="media_state" in required_types,
                capture_checkpoint_observation="checkpoint_observation" in required_types,
                session_id=request.session_id,
                timeout_ms=max(1, int(timeout_seconds * 1_000)),
                bootstrap_blank_surface=fresh_surface,
                web_capture_stage=request.web_capture_stage,
                viewport_width=request.viewport_width,
                viewport_height=request.viewport_height,
                screen_width=request.screen_width,
                screen_height=request.screen_height,
                device_scale_factor=request.device_scale_factor,
                is_mobile=request.is_mobile,
                has_touch=request.has_touch,
                color_scheme=request.color_scheme,
            ),
            browser=self._browser,
            timeout_seconds=timeout_seconds,
        )
        if result.execution_status != "success":
            raise RuntimeError(result.error_message or "页面采集未成功")
        if pristine_blank_bootstrap and result.final_url != "about:blank":
            raise RuntimeError("首导航动作前空白 Surface 已漂移")
        pending: PendingActionEvidence | None = None
        if request.phase is CapturePhase.AFTER_ACTION:
            if request.action_execution_id is None:
                raise RuntimeError("动作后 Capture 缺少 action_execution_id")
            pending = await self._evidence_buffer.get(
                action_execution_id=request.action_execution_id
            )
            if pending is None or not isinstance(pending.browser_result, PageCaptureResult):
                raise RuntimeError("动作阶段证据不存在，禁止提交不完整的动作后 Capture")
            result = _merge_action_evidence(
                capture_result=result,
                action_result=pending.browser_result,
                required_types=required_types,
            )

        unsupported_types = sorted(required_types.difference(SUPPORTED_CAPTURE_ARTIFACT_TYPES))
        if unsupported_types:
            raise RuntimeError("Web Capture 不支持必需产物：" + ", ".join(unsupported_types))
        produced_types = {item.artifact_type for item in result.artifacts}
        missing_types = sorted(required_types.difference(produced_types))
        if missing_types:
            raise RuntimeError("Capture 缺少必需产物：" + ", ".join(missing_types))

        persisted = await self._persistence.persist(
            task_id=request.task_id,
            agent_run_id=request.agent_run_id,
            result=result,
            parent_page_capture_id=request.parent_capture_id,
            capture_role=request.directive.role,
            inherit_parent_semantics=request.directive.inherit_parent_semantics,
            surface_session_id=request.session_id,
            action_execution_id=request.action_execution_id,
            session_command_sequence=request.session_command_sequence,
            finalization_epoch=request.finalization_epoch,
        )
        artifact_types = {item.artifact_type for item in persisted.artifacts}
        if required_types.difference(artifact_types):
            raise RuntimeError("Capture 产物在持久化阶段发生不一致")
        reference = CaptureRef(
            task_id=request.task_id,
            session_id=request.session_id,
            capture_id=persisted.page_capture_id,
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role=request.directive.role,
            artifact_ids=tuple(item.artifact_id for item in persisted.artifacts),
            parent_capture_id=request.parent_capture_id,
            action_execution_id=request.action_execution_id,
        )
        if pending is not None and request.action_execution_id is not None:
            await self._evidence_buffer.discard(action_execution_id=request.action_execution_id)
        return reference


class _ExplorationTransitionGate:
    """纯探索动作只受 Frontier、安全边界和 Session 命令闸门约束。"""

    async def reserve(self, *, intent, permit):  # noqa: ANN001
        del intent, permit
        return None

    async def reserve_action_recovery(
        self,
        *,
        intent,  # noqa: ANN001
        permit,  # noqa: ANN001
        action_execution_id: str,
        before_capture_id: str | None,
    ):
        del intent, permit, action_execution_id, before_capture_id
        return None

    async def finalize_stale_action_reservations(
        self,
        *,
        intent,  # noqa: ANN001
        permit,  # noqa: ANN001
        action_execution_id: str,
        status,  # noqa: ANN001
    ) -> int:
        del intent, permit, action_execution_id, status
        return 0

    async def finalize(self, *, decision, status) -> None:  # noqa: ANN001
        del decision, status

    async def expire_stale(self, *, limit: int = 100) -> int:
        del limit
        return 0


def build_h5_exploration_capture_coordinator(
    *,
    capture_writer: BrowserCaptureWriter,
    interaction_executor: BrowserInteractionExecutor,
    session_command_gate: SessionCommandGate,
    action_execution_repository: Any,
    action_recovery_captures: PageCaptureRecoveryService,
    command_validator: InteractionCommandValidator,
) -> CaptureCoordinator:
    """构建 H5 纯探索专用 Coordinator，不加载质检离场判定。"""
    return CaptureCoordinator(
        capture_writer=capture_writer,
        interaction_executor=interaction_executor,
        evidence_exit_gate=_ExplorationTransitionGate(),
        session_command_gate=session_command_gate,
        action_execution_repository=action_execution_repository,
        action_recovery_captures=action_recovery_captures,
        command_validator=command_validator,
    )


class WebAdLandingCaptureAdapter:
    """在同一 Web Session 中直开广告新页并提交不可变证据。"""

    def __init__(
        self,
        *,
        browser: PageCaptureBrowser,
        persistence: PageCapturePersistenceService,
        session_command_gate: SessionCommandGate,
        timeout_seconds: float = DEFAULT_WEB_INTERACTION_TIMEOUT_SECONDS,
        outbound_claims: object | None = None,
        page_type_assessor: object | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("广告落地采集超时必须大于 0")
        self._browser = browser
        self._persistence = persistence
        self._session_command_gate = session_command_gate
        self._timeout_seconds = float(timeout_seconds)
        self._outbound_claims = outbound_claims
        self._page_type_assessor = page_type_assessor

    async def read_terminal(self, command):
        """读取持久化广告回执，不领取浏览器 Session 或触发新导航。"""
        if self._outbound_claims is None:
            return None
        return await self._outbound_claims.read_terminal(command=command)

    async def capture(
        self,
        command: WebAdDirectCaptureCommand,
    ) -> WebAdLandingCaptureReceipt:
        if callable(getattr(self._outbound_claims, "read_terminal", None)):
            completed = await self.read_terminal(command)
            if completed is not None:
                return completed
        phase_id = None
        failure = None
        from services.api.app.application.web_ad_failure_diagnostics import safe_ad_failure_detail

        try:
            async with self._session_command_gate.hold(
                task_id=command.task_id,
                session_id=command.session_id,
                surface_kind=SurfaceKind.WEB,
            ) as permit:
                if self._outbound_claims is not None:
                    phase_id = await self._outbound_claims.claim(command=command, permit=permit)
                    if phase_id is None:
                        raise WebAdLandingCaptureError(
                            reason_code="WEB_AD_LANDING_ALREADY_ATTEMPTED",
                            message="广告阶段已被消费或当前领取边界无效，禁止再次直开",
                        )
                from services.api.app.application.web_ad_outbound_policy import (
                    AdOutboundDenied,
                    validate_ad_url,
                )

                if (
                    not isinstance(command.source_capture_id, str)
                    or not command.source_capture_id.strip()
                ):
                    raise WebAdLandingCaptureError(
                        reason_code="WEB_AD_SOURCE_CAPTURE_REQUIRED",
                        message="广告隔离直开必须绑定来源 Capture",
                    )
                try:
                    validate_ad_url(command.destination_url, previous_url=command.source_url)
                except AdOutboundDenied as exc:
                    raise WebAdLandingCaptureError(
                        reason_code=exc.reason,
                        message="广告 href 被安全策略拒绝",
                    ) from None
                # 等待 Session 租约、广告领取后仍需复验冻结期限；已落盘回执可在上方复用。
                deadline = getattr(command, "deadline", None)
                if deadline is not None and datetime.now(UTC) >= deadline:
                    raise WebAdLandingCaptureError(
                        reason_code="WEB_EXPLORATION_DEADLINE_EXHAUSTED",
                        message="探索期限已耗尽，禁止启动新的广告导航",
                    )
                result = await capture_page_with_timeout(
                    request=PageCaptureRequest(
                        mode=PageCaptureMode.GOTO,
                        url=command.destination_url,
                        ad_landing_url=command.destination_url,
                        web_ad_javascript=True,
                        navigation_depth=0,
                        capture_dom=True,
                        capture_visible_text=True,
                        capture_screenshot=True,
                        capture_ad_landing_evidence=True,
                        full_page=False,
                        # 为错误页提交、成对截图和新页关闭保留外层清理预算。
                        timeout_ms=min(
                            10_000,
                            max(1_000, int(self._timeout_seconds * 1_000 / 2)),
                        ),
                        session_id=command.session_id,
                    ),
                    browser=self._browser,
                    timeout_seconds=self._timeout_seconds,
                )
                if result.execution_status != "success":
                    raise WebAdLandingCaptureError(
                        reason_code="WEB_AD_PAGE_OPEN_FAILED",
                        message="广告落地页采集未成功",
                    )
                (
                    isolated_open_proven,
                    source_restored,
                    landing_url,
                    landing_reachable,
                    landing_reason_code,
                ) = _direct_ad_capture_outcome(
                    result=result,
                    source_url=command.source_url,
                )
                persistence_options = {
                    "task_id": command.task_id,
                    "agent_run_id": command.agent_run_id,
                    "result": result,
                    "parent_page_capture_id": command.source_capture_id,
                    "capture_role": "supplemental",
                    "inherit_parent_semantics": False,
                    "surface_session_id": command.session_id,
                    "action_execution_id": None,
                    "session_command_sequence": permit.command_sequence,
                    "finalization_epoch": permit.finalization_epoch,
                }
                persistence_options.update(
                    capture_purpose="web_ad_page",
                    direct_page_type="ad_page" if isolated_open_proven else None,
                    capture_url=landing_url or command.destination_url,
                    requested_url=command.destination_url,
                )
                persisted = await self._persistence.persist(**persistence_options)
                artifact_refs = _direct_ad_artifact_refs(
                    result=result,
                    persisted_artifact_ids=tuple(item.artifact_id for item in persisted.artifacts),
                )
                page_type_decision = await _assess_direct_ad_page_type(
                    assessor=self._page_type_assessor,
                    artifact_refs=artifact_refs,
                )
                detection_writer = getattr(
                    self._persistence,
                    "record_ad_page_detection",
                    None,
                )
                if callable(detection_writer):
                    updated = await detection_writer(
                        task_id=command.task_id,
                        agent_run_id=command.agent_run_id,
                        page_capture_id=persisted.page_capture_id,
                        **page_type_decision,
                    )
                    if updated is not True:
                        raise WebAdLandingCaptureError(
                            reason_code="WEB_AD_PAGE_TYPE_PERSIST_FAILED",
                            message="广告页型判定审计写入失败",
                        )
                if phase_id is not None:
                    phase_completed = isolated_open_proven and source_restored and landing_reachable
                    finishing_phase_id, phase_id = phase_id, None
                    await self._outbound_claims.finish(
                        phase_id=finishing_phase_id,
                        status="completed" if phase_completed else "failed",
                        reason_code=(
                            landing_reason_code or "WEB_AD_LANDING_OBSERVED"
                            if (isolated_open_proven and source_restored)
                            else "WEB_AD_LANDING_RETURN_FAILED"
                            if isolated_open_proven
                            else "WEB_AD_LANDING_OPEN_EVIDENCE_INVALID"
                        ),
                        artifact_ids=tuple(item.artifact_id for item in persisted.artifacts),
                    )
                    phase_id = None
                return WebAdLandingCaptureReceipt(
                    capture_id=persisted.page_capture_id,
                    landing_url=landing_url,
                    artifact_ids=tuple(item.artifact_id for item in persisted.artifacts),
                    artifact_refs=artifact_refs,
                    source_restored=source_restored,
                    landing_reachable=(landing_reachable if isolated_open_proven else False),
                    reason_code=(
                        landing_reason_code
                        if isolated_open_proven
                        else "WEB_AD_LANDING_OPEN_EVIDENCE_INVALID"
                    ),
                    requested_url=(command.destination_url),
                    page_type=("ad_page" if isolated_open_proven else None),
                )
        except WebAdLandingCaptureError as exc:
            exc.detail = safe_ad_failure_detail(exc.detail, error=exc, phase_id=phase_id)
            failure = exc
            raise
        except asyncio.CancelledError as exc:
            failure = WebAdLandingCaptureError(
                reason_code="WEB_AD_CAPTURE_CANCELLED", message="广告采集已取消",
                detail=safe_ad_failure_detail(error=exc, phase_id=phase_id),
            )
            raise
        except BlockedPageCaptureActionError as exc:
            reason_code = (
                "WEB_AD_LANDING_RETURN_FAILED"
                if exc.error_code == "EXTERNAL_NAVIGATION_ISOLATION_FAILED"
                else exc.error_code
                if exc.error_code.startswith("WEB_AD_")
                else "WEB_AD_PAGE_OPEN_FAILED"
            )
            failure = WebAdLandingCaptureError(
                reason_code=reason_code,
                message="广告落地证据已失败关闭",
                detail=safe_ad_failure_detail(error=exc, phase_id=phase_id),
            )
            raise failure from exc
        except Exception as exc:
            failure = WebAdLandingCaptureError(
                reason_code="WEB_AD_PAGE_OPEN_FAILED",
                message="广告落地证据采集失败",
                detail=safe_ad_failure_detail(error=exc, phase_id=phase_id),
            )
            raise failure from exc
        finally:
            if phase_id is not None:
                # 包括取消与不确定结果：领取已经消费，后续 Worker 不得重试广告。
                try:
                    await self._outbound_claims.finish(
                        phase_id=phase_id,
                        status="failed",
                        reason_code=failure.reason_code if failure else "WEB_AD_PAGE_OPEN_FAILED",
                        artifact_ids=(),
                        detail=failure.detail if failure else {},
                    )
                except Exception:
                    # 收口失败不得覆盖首次业务失败或取消；领取记录仍阻止二次导航。
                    if failure is None:
                        raise


def _direct_ad_capture_outcome(
    *,
    result: PageCaptureResult,
    source_url: str,
) -> tuple[bool, bool, str, bool, str | None]:
    source = urlsplit(source_url)
    final = urlsplit(str(result.final_url or ""))
    source_restored = (
        source.scheme.casefold(),
        source.netloc.casefold(),
        source.path or "/",
        source.query,
        source.fragment,
    ) == (
        final.scheme.casefold(),
        final.netloc.casefold(),
        final.path or "/",
        final.query,
        final.fragment,
    )
    for action in reversed(result.action_trace):
        if not (
            action.get("action") == "direct_ad_outbound"
            or (
                action.get("action") == "navigate"
                and action.get("navigation_mode") == "isolated_context_goto"
            )
        ):
            continue
        navigation = action.get("navigation")
        evidence = action.get("ad_navigation_evidence")
        if not isinstance(navigation, Mapping) or not isinstance(evidence, Mapping):
            return (
                False,
                source_restored,
                "",
                False,
                None,
            )
        landing = evidence.get("landing")
        landing_url = str(landing.get("url") or "") if isinstance(landing, Mapping) else ""
        evidence_status = str(evidence.get("status") or "")
        landing_reachable = not (isinstance(landing, Mapping) and landing.get("reachable") is False)
        reason_code = (
            str(landing.get("reason_code") or "") or None if isinstance(landing, Mapping) else None
        )
        has_isolated_counts = (
            "isolated_new_page_count" in navigation
            or "isolated_closed_after_observation_count" in navigation
        )
        isolated_counts_proven = (
            navigation.get("isolated_new_page_count") == 1
            and navigation.get("isolated_closed_after_observation_count") == 1
            if has_isolated_counts
            else (
                navigation.get("current_url_changed") is not True
                and navigation.get("source_context_contaminated") is not True
                and navigation.get("new_page_count") == 1
                and navigation.get("closed_after_observation_count") == 1
            )
        )
        isolated_open_proven = bool(
            isolated_counts_proven
            and evidence_status in {"observed", "observed_unreachable"}
            and (
                evidence_status == "observed"
                or (not landing_reachable and reason_code == "WEB_AD_LANDING_UNREACHABLE")
            )
            and landing_url
        )
        source_restored = bool(
            source_restored
            and navigation.get("current_url_changed") is not True
            and navigation.get("source_context_contaminated") is not True
        )
        return (
            isolated_open_proven,
            source_restored,
            landing_url,
            landing_reachable,
            reason_code,
        )
    return (
        False,
        source_restored,
        "",
        False,
        None,
    )


def _direct_ad_artifact_refs(
    *,
    result: PageCaptureResult,
    persisted_artifact_ids: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """绑定广告直开证据的源页与落地页角色。"""

    source_artifacts = tuple(
        artifact
        for artifact in result.artifacts
        if artifact.artifact_type in SUPPORTED_CAPTURE_ARTIFACT_TYPES
    )
    if len(source_artifacts) != len(persisted_artifact_ids):
        raise WebAdLandingCaptureError(
            reason_code="WEB_AD_LANDING_OPEN_FAILED",
            message="广告落地证据附件数量不一致",
        )
    refs: dict[str, str] = {}
    for artifact, artifact_id in zip(
        source_artifacts,
        persisted_artifact_ids,
        strict=True,
    ):
        purpose = (
            str(artifact.content.get("purpose") or "")
            if isinstance(artifact.content, Mapping)
            else ""
        )
        if artifact.artifact_type == "dom":
            key = "landing_dom" if purpose == "ad_landing" else "source_dom"
            refs.setdefault(key, artifact_id)
        elif artifact.artifact_type == "screenshot" and purpose == "ad_landing_consistency":
            refs["source_screenshot"] = artifact_id
        elif artifact.artifact_type == "screenshot" and purpose == "ad_landing":
            refs["landing_screenshot"] = artifact_id
        elif artifact.artifact_type == "ui_snapshot" and purpose == "ad_landing":
            refs.setdefault("landing_ui_snapshot", artifact_id)
        elif artifact.artifact_type == "har" and purpose == "ad_landing":
            refs.setdefault("landing_har", artifact_id)
        elif artifact.artifact_type == "action_trace" and isinstance(artifact.content, list):
            if any(
                isinstance(item, Mapping)
                and item.get("action") == "navigate"
                and item.get("navigation_mode") == "isolated_context_goto"
                for item in artifact.content
            ):
                refs.setdefault("landing_trace", artifact_id)
    ordered_keys = (
        "source_dom",
        "source_screenshot",
        "landing_dom",
        "landing_screenshot",
        "landing_ui_snapshot",
        "landing_har",
        "landing_trace",
    )
    if any(not refs.get(key) for key in ordered_keys):
        raise WebAdLandingCaptureError(
            reason_code="WEB_AD_LANDING_OPEN_FAILED",
            message="广告落地证据角色附件不完整",
        )
    return tuple((key, refs[key]) for key in ordered_keys)


async def _assess_direct_ad_page_type(
    *,
    assessor: object | None,
    artifact_refs: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    """模型优先判定广告页；不可用或低可信时仅按已验签隔离谱系兜底。"""

    refs = dict(artifact_refs)
    assess = getattr(assessor, "assess_ad_page", None)
    if not callable(assess):
        return {
            "decision_source": "direct_ad_lineage_fallback",
            "fallback_reason": "WEB_AD_PAGE_TYPE_MODEL_UNAVAILABLE",
            "model_page_type": None,
            "model_confidence": None,
        }
    try:
        assessment = await assess(
            screenshot_artifact_id=refs["landing_screenshot"],
            dom_artifact_id=refs["landing_dom"],
            ui_snapshot_artifact_id=refs["landing_ui_snapshot"],
            har_artifact_id=refs["landing_har"],
            action_trace_artifact_id=refs["landing_trace"],
        )
    except Exception:  # noqa: BLE001 - 模型线路失败只允许进入显式可信谱系兜底
        return {
            "decision_source": "direct_ad_lineage_fallback",
            "fallback_reason": "WEB_AD_PAGE_TYPE_MODEL_UNAVAILABLE",
            "model_page_type": None,
            "model_confidence": None,
        }
    model_page_type = getattr(assessment, "page_type", None)
    raw_confidence = getattr(assessment, "confidence", None)
    model_confidence = (
        float(raw_confidence)
        if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool)
        else None
    )
    needs_review_reason = getattr(assessment, "needs_review_reason", None)
    if (
        model_page_type == "ad_page"
        and model_confidence is not None
        and model_confidence >= 0.7
        and not needs_review_reason
    ):
        return {
            "decision_source": "model",
            "fallback_reason": None,
            "model_page_type": "ad_page",
            "model_confidence": model_confidence,
        }
    fallback_reason = (
        "WEB_AD_PAGE_TYPE_MODEL_REVIEW_REQUIRED"
        if needs_review_reason
        else "WEB_AD_PAGE_TYPE_MODEL_LOW_CONFIDENCE"
        if model_page_type == "ad_page"
        else "WEB_AD_PAGE_TYPE_MODEL_UNSUPPORTED"
    )
    return {
        "decision_source": "direct_ad_lineage_fallback",
        "fallback_reason": fallback_reason,
        "model_page_type": model_page_type,
        "model_confidence": model_confidence,
    }


@dataclass(frozen=True, kw_only=True)
class WebCaptureRuntime:
    """供 Worker 复用的一组同生命周期 Web Interaction 组件。

    Playwright Driver（``PlaywrightPageCaptureBrowser``）只能由本 Runtime 持有；
    Explorer / Rule Judge 不得直接取用。Main Agent 的 ``page_capture_tool`` 必须经
    ``run_agent_page_capture`` 走 ActionIntent；``borrow_browser_for_legacy_agent_tool``
    仅保留给紧急诊断，不得作为生产主路径。
    """

    coordinator: CaptureCoordinator
    session_command_gate: SessionCommandGate
    _browser: PlaywrightPageCaptureBrowser = field(repr=False)
    _executor: BrowserInteractionExecutor = field(repr=False)
    _evidence_buffer: ActionEvidenceBuffer = field(repr=False)
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off"
    _h5_exploration_coordinator: CaptureCoordinator | None = field(default=None, repr=False)
    surface_sessions: PostgresSurfaceSessionRepository | None = None
    # Web Driver Adapter；延迟类型避免与 surface_driver 循环导入
    _driver: Any = field(default=None, repr=False)
    _trusted_ui_snapshot_reader: TrustedUiSnapshotReader | None = field(
        default=None,
        repr=False,
    )
    _h5_target_resolver: UiSnapshotSemanticTargetResolver | None = field(
        default=None,
        repr=False,
    )
    _web_desktop_capture_policy: WebDesktopCapturePolicyBrowser | None = field(
        default=None,
        repr=False,
    )
    _web_exploration_session_cleanup: WebExplorationSessionCleanup | None = field(
        default=None,
        repr=False,
    )
    _page_capture_persistence: PageCapturePersistenceService | None = field(
        default=None, repr=False
    )
    _artifact_store: Any = field(default=None, repr=False)
    _action_executions: PostgresActionExecutionRepository | None = field(default=None, repr=False)
    _capture_recovery: PageCaptureRecoveryService | None = field(default=None, repr=False)
    _command_validator: InteractionCommandValidator | None = field(default=None, repr=False)
    _pending_browser_stop_receipts: dict = field(default_factory=dict, repr=False, compare=False)

    def borrow_browser_for_legacy_agent_tool(self) -> PlaywrightPageCaptureBrowser:
        """仅供紧急诊断借用；生产 page_capture_tool 应走 run_agent_page_capture。"""
        return self._browser

    @property
    def web_login_session_browser(self):
        """Web 直登仅借用当前 Run 的会话锁与页面，不建立第二个浏览器。"""
        from services.api.app.infrastructure.browser.web_login_session import (
            WebLoginSessionBrowser,
        )

        return WebLoginSessionBrowser(self._browser)

    def create_web_login_session_browser(self, *, auth_policy):
        """用运行时已校验的认证源站配置构造隔离登录浏览器。"""
        from services.api.app.infrastructure.browser.web_login_session import (
            WebLoginSessionBrowser,
        )

        return WebLoginSessionBrowser(self._browser, auth_policy=auth_policy)

    @property
    def web_login_artifact_store(self):
        """登录隔离会话复用当前 Web Run 的证据对象存储。"""
        return self._artifact_store

    @property
    def web_ad_landing_capture_port(self) -> WebAdLandingCaptureAdapter:
        """向 Web V2 暴露受会话租约保护的广告直开证据端口。"""

        return self.build_web_ad_landing_capture_port()

    def build_web_ad_landing_capture_port(
        self,
        *,
        page_type_assessor: object | None = None,
    ) -> WebAdLandingCaptureAdapter:
        """构建模型优先、隔离谱系兜底的广告页证据端口。"""
        if self._page_capture_persistence is None:
            raise RuntimeError("Web Runtime 缺少广告落地证据持久化组件")
        from services.api.app.infrastructure.db.web_ad_outbound_claim_repository import (
            PostgresWebAdOutboundClaimRepository,
        )

        return WebAdLandingCaptureAdapter(
            browser=self._browser,
            persistence=self._page_capture_persistence,
            session_command_gate=self.session_command_gate,
            outbound_claims=PostgresWebAdOutboundClaimRepository(),
            page_type_assessor=page_type_assessor,
        )

    def build_web_guide_browser(self):
        """引导页只借用启动配置，创建全新的 Playwright 浏览器进程。"""
        from services.api.app.infrastructure.browser.web_guide_browser import WebGuidePageBrowser

        launcher = getattr(self._browser, "_launch_browser", None)
        if not callable(launcher):
            return None
        return WebGuidePageBrowser(browser_launcher=launcher)

    @property
    def web_guide_capture_persistence(self):
        """引导页只写 supplemental Capture，不接管主会话。"""
        if self._page_capture_persistence is None:
            raise RuntimeError("Web Runtime 缺少引导页证据持久化组件")
        return self._page_capture_persistence

    @property
    def surface_driver(self) -> Any:
        """统一 SurfaceDriver Adapter（PlaywrightSurfaceDriverAdapter）。"""
        return self._driver

    @property
    def h5_exploration_coordinator(self) -> CaptureCoordinator:
        """返回仅供 H5 页面探索使用的 Coordinator。"""
        if self._h5_exploration_coordinator is None:
            raise RuntimeError("Web Capture Runtime 缺少 H5 纯探索 Coordinator")
        return self._h5_exploration_coordinator

    def build_h5_probe_coordinator(
        self,
        *,
        supplemental_evidence_policy: SupplementalEvidencePolicy,
        probe_action_roles: bool = False,
        new_session_restoration: bool = False,
    ) -> CaptureCoordinator:
        """复用当前 H5 Session 的 Driver，构建独立补采 Coordinator。"""

        if not isinstance(supplemental_evidence_policy, SupplementalEvidencePolicy):
            raise TypeError("supplemental_evidence_policy 类型不正确")
        if not isinstance(probe_action_roles, bool):
            raise TypeError("probe_action_roles 必须为布尔值")
        if not isinstance(new_session_restoration, bool) or (
            new_session_restoration and probe_action_roles
        ):
            raise ValueError("H5 恢复阶段与正式 Probe 采集角色不能混用")
        from services.api.app.infrastructure.db.h5_restoration_exit_gate import (
            PostgresH5RestorationExitGate,
        )
        if any(
            component is None
            for component in (
                self._page_capture_persistence,
                self._action_executions,
                self._capture_recovery,
                self._command_validator,
            )
        ):
            raise RuntimeError("H5 Probe 缺少采集、动作账本或命令范围校验组件")
        return CaptureCoordinator(
            capture_writer=BrowserCaptureWriter(
                browser=self._browser,
                persistence=self._page_capture_persistence,
                evidence_buffer=self._evidence_buffer,
            ),
            interaction_executor=(
                _H5ProbeInteractionExecutor(self._executor)
                if probe_action_roles
                else self._executor
            ),
            evidence_exit_gate=(
                PostgresH5RestorationExitGate()
                if new_session_restoration else PostgresEvidenceExitGate()
            ),
            session_command_gate=self.session_command_gate,
            action_execution_repository=(
                PostgresH5ProbeActionExecutionRepository()
                if probe_action_roles
                else self._action_executions
            ),
            action_recovery_captures=self._capture_recovery,
            supplemental_evidence_policy=supplemental_evidence_policy,
            command_validator=self._command_validator,
        )

    @property
    def h5_capture_recovery(self) -> PageCaptureRecoveryService:
        """返回与当前 H5 Runtime 共用证据存储的可信 Capture 读取器。"""
        if self._capture_recovery is None:
            raise RuntimeError("Web Capture Runtime 缺少 H5 Capture 恢复读取器")
        return self._capture_recovery

    @property
    def trusted_ui_snapshot_reader(self) -> TrustedUiSnapshotReader:
        """返回与当前 Web Runtime 证据存储一致的受信 UI Snapshot 读取器。"""
        if self._trusted_ui_snapshot_reader is None:
            raise RuntimeError("Web Capture Runtime 缺少受信 UI Snapshot 读取器")
        return self._trusted_ui_snapshot_reader

    @property
    def h5_target_resolver(self) -> UiSnapshotSemanticTargetResolver:
        """返回 H5 秘密输入专用的确定性语义目标解析器。"""
        if self._h5_target_resolver is None:
            raise RuntimeError("Web Capture Runtime 缺少 H5 语义目标解析器")
        return self._h5_target_resolver

    @property
    def h5_secret_form_browser(self) -> PlaywrightPageCaptureBrowser:
        """仅向 H5 Secret Runtime 暴露受限的密码表单提交端口。"""
        return self._browser

    @property
    def web_desktop_capture_policy(self) -> WebDesktopCapturePolicyBrowser:
        """返回仅供 Web V2 Explorer 读取导航候选的受限策略端口。"""

        if self._web_desktop_capture_policy is None:
            raise RuntimeError("Web Capture Runtime 未启用桌面采集策略")
        return self._web_desktop_capture_policy

    @property
    def web_full_page_capture_browser(self) -> PlaywrightPageCaptureBrowser:
        """仅供 Web V2 组合根创建受 claim 保护的全页补图服务。"""

        return self._browser

    @property
    def web_page_capture_persistence(self) -> PageCapturePersistenceService:
        """暴露与当前 Web Runtime 同库同存储的采集持久化边界。"""

        if self._page_capture_persistence is None:
            raise RuntimeError("Web Runtime 缺少页面采集持久化组件")
        return self._page_capture_persistence

    def coordinate_secret_input_runtime(self, runtime: Any) -> Any:
        """专用秘密窗口复用正式执行组件，但不把秘密值交给普通动作执行器。"""
        from services.api.app.infrastructure.browser.h5_secret_input_coordination import (
            CoordinatedH5SecretInputRuntime,
        )

        if any(
            component is None
            for component in (
                self._page_capture_persistence,
                self._action_executions,
                self._capture_recovery,
                self._command_validator,
            )
        ):
            raise RuntimeError("秘密输入缺少正式采集、动作账本或命令范围校验")
        runtime._command_validator = self._command_validator

        def coordinator_factory(writer: Any, executor: Any) -> CaptureCoordinator:
            return build_h5_exploration_capture_coordinator(
                capture_writer=writer,
                interaction_executor=executor,
                session_command_gate=self.session_command_gate,
                action_execution_repository=self._action_executions,
                action_recovery_captures=self._capture_recovery,
                command_validator=self._command_validator,
            )

        return CoordinatedH5SecretInputRuntime(
            runtime=runtime,
            capture_writer=BrowserCaptureWriter(
                browser=self._browser,
                persistence=self._page_capture_persistence,
                evidence_buffer=self._evidence_buffer,
            ),
            evidence_buffer=self._evidence_buffer,
            coordinator_factory=coordinator_factory,
        )

    async def run_agent_page_capture(
        self,
        spec: Any,
        *,
        persistence_service: Any | None = None,
    ) -> Any:
        """Main Agent Tool 入口：ActionIntent → CaptureCoordinator 双采。"""
        from services.api.app.application.web_agent_page_capture import (
            coordinate_agent_page_capture,
        )
        from services.api.app.application.web_evidence_exit_gate import (
            build_web_evidence_exit_gate,
        )

        if any(
            component is None
            for component in (
                self._page_capture_persistence,
                self._action_executions,
                self._capture_recovery,
                self._command_validator,
                self._trusted_ui_snapshot_reader,
            )
        ):
            raise RuntimeError("Web Runtime 缺少正式操作记录或命令校验组件")
        gate = build_web_evidence_exit_gate()
        # 证据离场策略关闭不等于关闭操作权限、会话租约或动作记录。
        # 旧 Tool 与正式 Explorer 一样，始终持久化前后采集并复用同一 Session Gate。
        return await coordinate_agent_page_capture(
            browser=self._browser,
            spec=spec,
            persistence_service=persistence_service or self._page_capture_persistence,
            evidence_exit_gate=gate,
            session_command_gate=self.session_command_gate,
            action_execution_repository=self._action_executions,
            action_recovery_captures=self._capture_recovery,
            command_validator=self._command_validator,
            snapshot_reader=self._trusted_ui_snapshot_reader,
            persist_before_capture=True,
        )

    async def close_browser_session(self, session_id: str) -> None:
        """关闭底层浏览器会话；legacy Tool / Worker 释放 Run 时使用。"""
        if not session_id.startswith('h5:') or self.surface_sessions is None:
            await self._browser.close_session(session_id)
            return
        proof = self._pending_browser_stop_receipts.get(session_id)
        if proof is None:
            proof = await self._browser.close_session(session_id)
        if proof is None:
            proof = await self.surface_sessions.read_browser_stop(session_id=session_id)
        if proof is None:
            raise RuntimeError('H5_PHYSICAL_STOP_UNCONFIRMED：原实例停止证明缺失')
        # 写回失败只保留原回执；再次调用不得重建浏览器或猜测无句柄就是停止。
        self._pending_browser_stop_receipts[session_id] = proof
        await self.surface_sessions.record_browser_stop(session_id=session_id, proof=proof)
        self._pending_browser_stop_receipts.pop(session_id, None)

    async def close_session(self, *, task_id: str, session_id: str) -> None:
        await self._close_session(
            task_id=task_id,
            session_id=session_id,
            pure_exploration=False,
        )

    async def close_h5_exploration_session(self, *, task_id: str, session_id: str) -> None:
        """关闭 H5 Explorer Session，不等待质检 Manifest、Inventory 或 Judge。"""
        try:
            await self.close_exploration_session(task_id=task_id, session_id=session_id)
        except (Exception, asyncio.CancelledError) as error:
            # 封存失败不等于浏览器仍可使用。保留原始失败，同时尽力关闭
            # 物理会话、持久化 lost 并释放租约；绝不把未封存会话写成 closed。
            try:
                await self._abandon_h5_session(task_id=task_id, session_id=session_id)
            except (Exception, asyncio.CancelledError) as cleanup_error:
                error.add_note(
                    f"H5 会话异常收场失败: {type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    async def _abandon_h5_session(self, *, task_id: str, session_id: str) -> None:
        snapshot = (
            await self.surface_sessions.get(session_id=session_id)
            if self.surface_sessions is not None else None
        )
        if self.surface_sessions is not None and (
            snapshot is None or snapshot.task_id != task_id
        ):
            raise RuntimeError("Surface Session 不存在或不属于当前任务")
        try:
            await self._resolve_h5_physical_quarantine(task_id=task_id, session_id=session_id)
            await self.close_browser_session(session_id)
        finally:
            try:
                if snapshot is not None and snapshot.status not in {
                    SurfaceSessionStatus.CLOSED, SurfaceSessionStatus.LOST,
                }:
                    await self.surface_sessions.mark_lost(
                        task_id=task_id, session_id=session_id,
                        expected_row_version=snapshot.row_version,
                        reason="h5_exploration_shutdown_failed",
                    )
            finally:
                await self.session_command_gate.release_session(
                    task_id=task_id, session_id=session_id,
                )

    async def close_exploration_session(self, *, task_id: str, session_id: str) -> None:
        """关闭纯探索 Session，不进入任何平台的质检发布门禁。"""
        await self._close_session(
            task_id=task_id,
            session_id=session_id,
            pure_exploration=True,
        )

    async def _close_session(
        self, *, task_id: str, session_id: str, pure_exploration: bool
    ) -> None:
        await self._resolve_h5_physical_quarantine(task_id=task_id, session_id=session_id)
        if self.surface_sessions is None:
            await self.close_browser_session(session_id)
            await self.session_command_gate.release_session(
                task_id=task_id,
                session_id=session_id,
            )
            return

        snapshot = await self.surface_sessions.get(session_id=session_id)
        if snapshot is None or snapshot.task_id != task_id:
            raise RuntimeError("Surface Session 不存在或不属于当前任务")
        if snapshot.status is SurfaceSessionStatus.ACTIVE:
            snapshot = await self.surface_sessions.begin_draining(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version,
            )
        if snapshot.status is SurfaceSessionStatus.DRAINING:
            if pure_exploration and self._web_exploration_session_cleanup is not None:
                await self._web_exploration_session_cleanup.cleanup(
                    task_id=task_id,
                    session_id=session_id,
                )
            seal_for_close = (
                self.surface_sessions.seal_exploration_for_close
                if pure_exploration
                else self.surface_sessions.seal_for_close
            )
            snapshot = await seal_for_close(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version,
            )
        if snapshot.status is SurfaceSessionStatus.CLOSED:
            await self.session_command_gate.release_session(
                task_id=task_id,
                session_id=session_id,
            )
            return
        if snapshot.status is not SurfaceSessionStatus.CLOSING:
            raise RuntimeError(f"Surface Session 状态 {snapshot.status.value} 不能关闭")
        try:
            await self.close_browser_session(session_id)
        except Exception:
            await self.surface_sessions.mark_lost(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version,
                reason="surface_driver_close_failed",
            )
            await self.session_command_gate.release_session(
                task_id=task_id,
                session_id=session_id,
            )
            raise
        await self.surface_sessions.mark_closed(
            task_id=task_id,
            session_id=session_id,
            expected_row_version=snapshot.row_version,
        )
        await self.session_command_gate.release_session(
            task_id=task_id,
            session_id=session_id,
        )

    async def _resolve_h5_physical_quarantine(self, *, task_id: str, session_id: str) -> None:
        resolve = getattr(self.session_command_gate, "resolve_quarantine", None)
        if session_id.startswith("h5:") and callable(resolve):
            await resolve(task_id=task_id, session_id=session_id)


_WEB_CAPTURE_RUNTIME: WebCaptureRuntime | None = None
_WEB_CAPTURE_RUNTIME_LOCK = threading.Lock()


def _h5_browser_proxy_constructor_options(workflow: Any) -> dict[str, str]:
    """只在显式启用代理时向浏览器构造器增加参数。"""
    server = workflow.h5_browser_proxy_server
    if server is None:
        return {}
    options = {"proxy_server": server}
    if workflow.h5_browser_proxy_bypass is not None:
        options["proxy_bypass"] = workflow.h5_browser_proxy_bypass
    return options


def _web_browser_proxy_constructor_options(workflow: Any) -> dict[str, str]:
    """只向 Web V2 桌面浏览器注入独立代理。"""
    server = workflow.web_browser_proxy_server
    if server is None:
        return {}
    options = {"proxy_server": server}
    if workflow.web_browser_proxy_bypass is not None:
        options["proxy_bypass"] = workflow.web_browser_proxy_bypass
    return options


def get_or_create_web_capture_runtime(
    *,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] | None = None,
) -> WebCaptureRuntime:
    """进程级 Web Interaction Runtime；Playwright Driver 仅由此持有。"""
    if h5_visual_coordinate_mode not in {None, "off", "shadow", "enforced"}:
        raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
    global _WEB_CAPTURE_RUNTIME
    with _WEB_CAPTURE_RUNTIME_LOCK:
        if _WEB_CAPTURE_RUNTIME is None:
            settings = get_settings()
            effective_visual_mode = (
                h5_visual_coordinate_mode
                if h5_visual_coordinate_mode is not None
                else getattr(
                    settings.workflow,
                    "h5_visual_coordinate_runtime_mode",
                    "off",
                )
            )
            concurrency = max(
                int(getattr(settings.quality_optimization, "page_capture_concurrency", 1)),
                1,
            )
            _WEB_CAPTURE_RUNTIME = build_default_web_capture_runtime(
                browser=PlaywrightPageCaptureBrowser(
                    max_concurrent_captures=concurrency,
                    logical_surface_enabled=(settings.workflow.h5_logical_surface_enabled),
                    logical_surface_shadow=settings.workflow.h5_logical_surface_shadow,
                    h5_visual_coordinate_mode=effective_visual_mode,
                    cli_mode=settings.workflow.h5_cli_mode,
                    cli_script=settings.workflow.h5_cli_script,
                    cli_workspace_root=settings.workflow.h5_cli_workspace_root,
                    **_h5_browser_proxy_constructor_options(settings.workflow),
                )
            )
        elif (
            h5_visual_coordinate_mode is not None
            and _WEB_CAPTURE_RUNTIME.h5_visual_coordinate_mode != h5_visual_coordinate_mode
        ):
            raise RuntimeError("Web Capture Runtime 视觉导航模式与当前组合根不一致")
        return _WEB_CAPTURE_RUNTIME


def reset_web_capture_runtime_for_tests() -> None:
    """测试专用：清空进程级 Runtime，避免跨用例泄漏浏览器实例。"""
    global _WEB_CAPTURE_RUNTIME
    with _WEB_CAPTURE_RUNTIME_LOCK:
        _WEB_CAPTURE_RUNTIME = None


def set_web_capture_runtime_for_tests(runtime: WebCaptureRuntime | None) -> None:
    """测试专用：注入或清空进程级 Runtime。"""
    global _WEB_CAPTURE_RUNTIME
    with _WEB_CAPTURE_RUNTIME_LOCK:
        _WEB_CAPTURE_RUNTIME = runtime


def _web_exploration_cleanup_enabled(
    *,
    session_owner: str,
    include_h5_exploration: bool,
    web_desktop_policy_enabled: bool,
) -> bool:
    """仅允许独立 Web V2 桌面 Runtime 注入遗留动作清理器。"""

    return (
        session_owner.strip() == "web-v2-workflow"
        and not include_h5_exploration
        and web_desktop_policy_enabled
    )


def build_default_web_capture_runtime(
    *,
    browser: PlaywrightPageCaptureBrowser | None = None,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] | None = None,
    session_owner: str = H5_SURFACE_SESSION_OWNER,
    include_h5_exploration: bool = True,
    logical_surface_enabled: bool | None = None,
    logical_surface_shadow: bool | None = None,
    logical_targets_enabled: bool | None = None,
    web_desktop_policy_enabled: bool = False,
    web_readiness_profile: Any | None = None,
    web_run_policy: Any | None = None,
    evidence_exit_gate: Any | None = None,
) -> WebCaptureRuntime:
    """按当前配置构建单进程 Web Interaction Runtime + Web Driver Adapter。"""
    web_identity_options = {}
    if web_desktop_policy_enabled and web_run_policy is not None:
        from services.api.app.application.web_run_policy import materialize_web_policy

        frozen_policy = materialize_web_policy(web_run_policy)
        frozen_profile = frozen_policy.readiness_profile
        web_identity_options["web_identity_policy"] = frozen_policy.identity_policy
        if web_readiness_profile is not None and web_readiness_profile != frozen_profile:
            raise ValueError("Web profile 与冻结正文不一致")
        web_readiness_profile = frozen_profile
    # 延迟导入，避免与 surface_driver → BrowserInteractionExecutor 循环依赖
    from services.api.app.infrastructure.browser.surface_driver import (
        PlaywrightSurfaceDriverAdapter,
    )

    settings = get_settings()
    if not isinstance(session_owner, str) or not session_owner.strip():
        raise ValueError("session_owner 不能为空")
    if h5_visual_coordinate_mode not in {None, "off", "shadow", "enforced"}:
        raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
    browser_visual_mode = (
        getattr(browser, "_h5_visual_coordinate_mode", None) if browser is not None else None
    )
    if (
        h5_visual_coordinate_mode is not None
        and browser_visual_mode is not None
        and browser_visual_mode != h5_visual_coordinate_mode
    ):
        raise ValueError("Browser 视觉导航模式与 Runtime 组合根不一致")
    effective_visual_mode = (
        h5_visual_coordinate_mode
        if h5_visual_coordinate_mode is not None
        else browser_visual_mode
        if browser_visual_mode is not None
        else settings.workflow.h5_visual_coordinate_runtime_mode
    )
    if effective_visual_mode not in {"off", "shadow", "enforced"}:
        raise ValueError("Browser 缺少有效的 H5 视觉导航模式")
    resolved_logical_surface_enabled = (
        settings.workflow.h5_logical_surface_enabled
        if logical_surface_enabled is None
        else logical_surface_enabled
    )
    resolved_logical_surface_shadow = (
        settings.workflow.h5_logical_surface_shadow
        if logical_surface_shadow is None
        else logical_surface_shadow
    )
    resolved_logical_targets_enabled = (
        settings.workflow.h5_logical_targets_effective
        if logical_targets_enabled is None
        else logical_targets_enabled
    )
    artifact_store = build_evidence_artifact_store(settings.evidence_storage)
    proxy_options = (
        _web_browser_proxy_constructor_options(settings.workflow)
        if web_desktop_policy_enabled
        else _h5_browser_proxy_constructor_options(settings.workflow)
    )
    cli_options = (
        {
            "cli_mode": settings.workflow.h5_cli_mode,
            "cli_script": settings.workflow.h5_cli_script,
            "cli_workspace_root": settings.workflow.h5_cli_workspace_root,
        }
        if hasattr(settings.workflow, "h5_cli_mode")
        else {}
    )
    actual_browser = browser or PlaywrightPageCaptureBrowser(
        logical_surface_enabled=resolved_logical_surface_enabled,
        logical_surface_shadow=resolved_logical_surface_shadow,
        h5_visual_coordinate_mode=effective_visual_mode,
        **cli_options,
        **proxy_options,
    )
    if web_desktop_policy_enabled and not isinstance(
        actual_browser,
        WebDesktopCapturePolicyBrowser,
    ):
        policy_options = (
            {"readiness_profile": web_readiness_profile}
            if web_readiness_profile is not None
            else {}
        )
        if web_identity_options:
            policy_options["identity_policy"] = web_identity_options["web_identity_policy"]
        if web_run_policy is not None:
            policy_options["include_blocked_navigation"] = materialize_web_policy(
                web_run_policy
            ).discovery_policy.enabled
        actual_browser = WebDesktopCapturePolicyBrowser(browser=actual_browser, **policy_options)
    elif web_desktop_policy_enabled and web_readiness_profile is not None:
        if actual_browser._readiness_profile != web_readiness_profile:
            raise ValueError("Web Browser 已绑定不同冻结 profile，禁止跨 Run 复用")
    web_desktop_capture_policy = (
        actual_browser if isinstance(actual_browser, WebDesktopCapturePolicyBrowser) else None
    )
    evidence_buffer = InMemoryActionEvidenceBuffer()
    snapshot_reader = PostgresTrustedUiSnapshotReader(artifact_store=artifact_store)
    discovery_authorization = None
    if (
        web_desktop_policy_enabled
        and session_owner.strip() == "web-v2-workflow"
        and not include_h5_exploration
        and web_run_policy is not None
        and materialize_web_policy(web_run_policy).discovery_policy.enabled
    ):
        from services.api.app.infrastructure.db.web_discovery_action_authorization import (
            PostgresWebDiscoveryActionAuthorization,
        )

        discovery_authorization = PostgresWebDiscoveryActionAuthorization(snapshot=web_run_policy)
    target_resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=snapshot_reader,
        logical_targets_enabled=resolved_logical_targets_enabled,
        h5_visual_coordinate_mode=effective_visual_mode,
        web_discovery_authorizer=discovery_authorization,
        **web_identity_options,
    )
    surface_sessions = PostgresSurfaceSessionRepository()
    gate = PostgresSessionCommandGate(
        repository=surface_sessions,
        lease_owner=f"web-capture-runtime:{uuid4()}",
        # 必须与 H5 工作流注册 Surface Session 时的归属一致，否则命令闸门
        # 续期 Session 时 ON CONFLICT 匹配不上，报「Surface Session 归属无效」。
        # 对照 App 侧：两端同样固定为 "app-workflow"。
        session_owner=session_owner.strip(),
    )
    if include_h5_exploration:
        from services.api.app.infrastructure.db.h5_session_journey_gate import (
            PostgresH5SessionJourneyGate,
        )
        gate = PostgresH5SessionJourneyGate(delegate=gate)
    action_executions = PostgresActionExecutionRepository()
    command_validator = InteractionCommandValidator(scope_reader=PostgresTaskCommandScopeReader())
    interaction_executor = BrowserInteractionExecutor(
        browser=actual_browser,
        target_resolver=target_resolver,
        evidence_buffer=evidence_buffer,
        action_timeout_seconds=(settings.quality_optimization.web_interaction_timeout_seconds),
        command_validator=command_validator,
    )
    page_capture_repository = (
        PostgresPageCaptureRepository(web_run_policy=web_run_policy)
        if web_desktop_policy_enabled
        else PostgresPageCaptureRepository()
    )
    capture_recovery = PageCaptureRecoveryService(
        repository=page_capture_repository,
        artifact_store=artifact_store,
    )
    web_catalog_identity = None
    if web_desktop_policy_enabled and web_run_policy is not None:
        from services.api.app.application.web_page_type_assessment import (
            WebPageTypeCatalogIdentity,
        )

        policy_payload = web_run_policy.to_payload()
        web_catalog_identity = WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
            policy_payload["page_catalog"]
        )
    page_capture_persistence = PageCapturePersistenceService(
        repository=page_capture_repository,
        artifact_store=artifact_store,
        web_catalog_identity=web_catalog_identity,
    )
    capture_writer = BrowserCaptureWriter(
        browser=actual_browser,
        persistence=page_capture_persistence,
        evidence_buffer=evidence_buffer,
    )
    coordinator = CaptureCoordinator(
        capture_writer=capture_writer,
        interaction_executor=interaction_executor,
        evidence_exit_gate=(
            evidence_exit_gate if evidence_exit_gate is not None else PostgresEvidenceExitGate()
        ),
        session_command_gate=gate,
        action_execution_repository=action_executions,
        command_validator=command_validator,
    )
    h5_exploration_coordinator = (
        build_h5_exploration_capture_coordinator(
            capture_writer=capture_writer,
            interaction_executor=interaction_executor,
            session_command_gate=gate,
            action_execution_repository=action_executions,
            action_recovery_captures=capture_recovery,
            command_validator=command_validator,
        )
        if include_h5_exploration
        else None
    )
    driver = PlaywrightSurfaceDriverAdapter(
        browser=actual_browser,
        target_resolver=target_resolver,
        evidence_buffer=evidence_buffer,
        command_validator=command_validator,
    )
    web_exploration_session_cleanup = (
        WebExplorationSessionCleanup(repository=PostgresWebExplorationSessionCleanupRepository())
        if _web_exploration_cleanup_enabled(
            session_owner=session_owner,
            include_h5_exploration=include_h5_exploration,
            web_desktop_policy_enabled=web_desktop_policy_enabled,
        )
        else None
    )
    return WebCaptureRuntime(
        coordinator=coordinator,
        _browser=actual_browser,
        _executor=interaction_executor,
        _evidence_buffer=evidence_buffer,
        h5_visual_coordinate_mode=effective_visual_mode,
        session_command_gate=gate,
        _h5_exploration_coordinator=h5_exploration_coordinator,
        surface_sessions=surface_sessions,
        _driver=driver,
        _trusted_ui_snapshot_reader=snapshot_reader,
        _h5_target_resolver=target_resolver,
        _web_desktop_capture_policy=web_desktop_capture_policy,
        _web_exploration_session_cleanup=web_exploration_session_cleanup,
        _page_capture_persistence=page_capture_persistence,
        _artifact_store=artifact_store,
        _action_executions=action_executions,
        _capture_recovery=capture_recovery,
        _command_validator=command_validator,
    )


def _build_browser_action_request(
    request: ExecuteInteractionRequest,
    *,
    target: Any,
    timeout_seconds: float,
) -> PageCaptureRequest:
    intent = request.intent
    timeout_ms = max(1, int(timeout_seconds * 1_000))
    parameters = intent.parameters
    if parameters.get("capture_ad_landing_evidence") is True:
        raise BlockedPageCaptureActionError(
            "普通交互不能生成广告点击命令",
            error_code="WEB_AD_CLICK_FORBIDDEN",
        )
    h5_bounded_scroll = parameters.get("target_mode") in {
        "h5_detail_scroll_coverage",
        "h5_list_scroll_coverage",
    }
    h5_payment_horizontal = (
        parameters.get("target_mode") == "h5_payment_horizontal_collection"
    )
    artifact_flags = {
        "capture_dom": parameters.get("capture_dom", True) is not False,
        "capture_visible_text": parameters.get("capture_visible_text", True) is not False,
        "capture_screenshot": parameters.get("capture_screenshot", True) is not False,
        "full_page": parameters.get("full_page", True) is not False,
    }
    common = {
        "session_id": intent.session_id,
        "timeout_ms": timeout_ms,
        "navigation_depth": _optional_nonnegative_int(parameters.get("navigation_depth")),
        "navigation_family": str(parameters.get("navigation_family") or "").strip() or None,
        "navigation_index": _optional_nonnegative_int(parameters.get("navigation_index")),
        "navigation_label": str(parameters.get("navigation_label") or "").strip() or None,
        "interaction_pattern_signature": (
            str(parameters.get("interaction_pattern_signature") or "").strip() or None
        ),
        "viewport_width": _optional_nonnegative_int(parameters.get("viewport_width")),
        "viewport_height": _optional_nonnegative_int(parameters.get("viewport_height")),
        "screen_width": _optional_nonnegative_int(parameters.get("screen_width")),
        "screen_height": _optional_nonnegative_int(parameters.get("screen_height")),
        "device_scale_factor": _optional_number(parameters.get("device_scale_factor")),
        "is_mobile": _optional_bool(parameters.get("is_mobile")),
        "has_touch": _optional_bool(parameters.get("has_touch")),
        "color_scheme": _optional_color_scheme(parameters.get("color_scheme")),
        "scroll_max_segments": (
            _optional_nonnegative_int(parameters.get("scroll_max_segments"))
            if h5_bounded_scroll
            else None
        ),
        "horizontal_scroll_max_segments": (
            _optional_nonnegative_int(
                parameters.get("horizontal_scroll_max_segments")
            )
            if h5_payment_horizontal
            else None
        ),
        "split_scroll_screenshots": bool(
            (h5_bounded_scroll or h5_payment_horizontal)
            and parameters.get("split_scroll_screenshots") is True
        ),
        "web_capture_stage": _optional_web_capture_stage(parameters.get("web_capture_stage")),
        "capture_transient_messages": intent.action_type
        in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
            InteractionOperation.INPUT,
        },
        "capture_ad_landing_evidence": (parameters.get("capture_ad_landing_evidence") is True),
        "_logical_target_binding": _logical_target_runtime_binding(
            parameters=parameters,
            target=target,
            operation=intent.action_type,
        ),
        **artifact_flags,
    }
    operation = intent.action_type
    if operation is InteractionOperation.NAVIGATE:
        return PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url=target.destination_url,
            **common,
        )
    if operation in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        visual_navigation_target = getattr(target, "visual_navigation_target", None)
        click_common = dict(common)
        if visual_navigation_target is not None:
            click_common["full_page"] = False
        if parameters.get("target_mode") == "h5_probe_display_target":
            click_common["_h5_probe_display_contract"] = {
                "protocol_version": "h5-probe-display.v1",
                "goal": parameters.get("goal_capability"),
                "page_type": parameters.get("source_page_type"),
                "subject_key": parameters.get("probe_subject_key"),
            }
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=PageTriggerAction.CLICK,
            selector=target.selector,
            click_x_ratio=target.click_x_ratio,
            click_y_ratio=target.click_y_ratio,
            left_ratio=target.left_ratio,
            top_ratio=target.top_ratio,
            right_ratio=target.right_ratio,
            bottom_ratio=target.bottom_ratio,
            visual_confidence=target.visual_confidence,
            visual_source_url=target.visual_source_url,
            visual_entry_kind=target.visual_entry_kind,
            visual_expected_text=target.visual_expected_text,
            screenshot_index=target.screenshot_index,
            _visual_navigation_target=visual_navigation_target,
            clear_obstructions=target.clear_obstructions,
            allow_high_z_obstruction_hide=target.allow_high_z_obstruction_hide,
            h5_popup_dismissal_strategy=target.h5_popup_dismissal_strategy,
            allow_h5_popup_layer_continuation=(
                parameters.get("target_mode") == "popup_dismissal"
            ),
            verified_card_activation=target.verified_card_activation,
            expected_rect=dict(target.expected_rect) if target.expected_rect else None,
            **click_common,
        )
    if operation is InteractionOperation.HOVER:
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=PageTriggerAction.HOVER,
            selector=target.selector,
            **common,
        )
    if operation is InteractionOperation.SEEK_MEDIA:
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=PageTriggerAction.SEEK_MEDIA,
            selector=target.selector,
            seek_fraction=_optional_number(parameters.get("target_fraction")),
            seek_time_seconds=_optional_number(parameters.get("target_time_seconds")),
            **common,
        )
    if operation is InteractionOperation.BACK and target.selector:
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=PageTriggerAction.CLICK,
            selector=target.selector,
            expected_rect=dict(target.expected_rect) if target.expected_rect else None,
            **common,
        )
    if operation is InteractionOperation.INPUT:
        if target.submit_selector is not None:
            if SAFE_PROBE_TEXT_PATTERN.fullmatch(target.input_text or "") is None:
                raise BlockedPageCaptureActionError(
                    "自动提交只允许使用有界中性探针文本",
                    error_code="UNSAFE_INPUT_SUBMISSION_BLOCKED",
                )
            return PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.SUBMIT_PROBE,
                selector=target.selector,
                submit_selector=target.submit_selector,
                probe_text=target.input_text,
                **common,
            )
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=PageTriggerAction.FILL,
            selector=target.selector,
            input_text=target.input_text,
            **common,
        )
    if operation is InteractionOperation.BACK and target.selector is not None:
        # Flutter Navigator 可能不写浏览器 history；只有语义解析器已经从
        # 可信 Snapshot 验明页面内返回控件时，才把 canonical back 落为 click。
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=PageTriggerAction.CLICK,
            selector=target.selector,
            **common,
        )
    trigger_by_operation = {
        InteractionOperation.SCROLL: PageTriggerAction.SCROLL,
        InteractionOperation.BACK: PageTriggerAction.BACK,
        InteractionOperation.REFRESH: PageTriggerAction.REFRESH,
        InteractionOperation.NETWORK_FAULT: PageTriggerAction.NETWORK_FAULT,
    }
    if operation in trigger_by_operation:
        return PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action=trigger_by_operation[operation],
            selector=(
                target.selector
                if operation is InteractionOperation.SCROLL
                and (
                    h5_payment_horizontal
                    or parameters.get("target_mode")
                    in {"web_primary_content_candidate", "web_detail_candidate"}
                    and getattr(target, "target_source", None)
                    == parameters.get("target_mode")
                )
                else None
            ),
            fault_url_pattern=target.fault_url_pattern,
            **common,
        )
    if operation is InteractionOperation.TIMED_CAPTURE:
        return PageCaptureRequest(
            mode=PageCaptureMode.TIMED_CAPTURE,
            frame_count=max(1, int(parameters.get("frame_count") or 3)),
            interval_ms=max(1, int(parameters.get("interval_ms") or 1000)),
            **common,
        )
    if operation is InteractionOperation.OBSERVE:
        return PageCaptureRequest(mode=PageCaptureMode.OBSERVE, **common)
    raise BlockedPageCaptureActionError(
        f"Web Driver 不支持动作：{operation.value}",
        error_code="WEB_ACTION_UNSUPPORTED",
    )


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _logical_target_runtime_binding(
    *,
    parameters: Mapping[str, Any],
    target: ResolvedInteractionTarget,
    operation: InteractionOperation,
) -> LogicalTargetRuntimeBinding | None:
    """只把已验签且需要真实动作的来源状态送入浏览器私有通道。"""

    contract = parameters.get("logical_target")
    if not isinstance(contract, Mapping) or target.target_trust != "trusted":
        return None
    if not (
        contract.get("expects_logical_navigation") is True
        or operation is InteractionOperation.SCROLL
        and contract.get("expects_scroll_effect") is True
    ):
        return None
    identity = contract.get("source_identity_fingerprint")
    state = contract.get("source_state_fingerprint")
    signature = target.target_signature
    identity_signature = target.target_identity_signature
    target_state = target.source_state_fingerprint
    if not all(
        isinstance(value, str) and _valid_logical_fingerprint(value)
        for value in (identity, state, target_state, signature)
    ):
        raise BlockedPageCaptureActionError(
            "可信逻辑目标缺少有效的实时执行绑定",
            error_code="LOGICAL_TARGET_INVALID",
        )
    if state != target_state and not _allows_h5_live_target_revalidation(
        parameters=parameters,
        target=target,
        operation=operation,
    ):
        raise BlockedPageCaptureActionError(
            "可信逻辑目标缺少有效的实时执行绑定",
            error_code="LOGICAL_TARGET_INVALID",
        )
    allow_geometry_revalidation = _allows_h5_dynamic_geometry_revalidation(
        parameters=parameters,
        operation=operation,
    )
    if allow_geometry_revalidation and not (
        isinstance(identity_signature, str)
        and _valid_logical_fingerprint(identity_signature)
    ):
        raise BlockedPageCaptureActionError(
            "H5 动态导航目标缺少稳定身份签名",
            error_code="LOGICAL_TARGET_INVALID",
        )
    return LogicalTargetRuntimeBinding(
        source_identity_fingerprint=identity,
        source_state_fingerprint=target_state,
        target_signature=signature,
        target_source=target.target_source,
        target_identity_signature=identity_signature,
        allow_dynamic_state_revalidation=_allows_h5_live_target_revalidation(
            parameters=parameters,
            target=target,
            operation=operation,
        ),
        allow_dynamic_geometry_revalidation=allow_geometry_revalidation,
        allow_horizontal_offscreen_revalidation=bool(
            allow_geometry_revalidation
            and parameters.get("target_mode") == "page_tab_navigation"
        ),
    )


def _allows_h5_live_target_revalidation(
    *,
    parameters: Mapping[str, Any],
    target: ResolvedInteractionTarget,
    operation: InteractionOperation,
) -> bool:
    """允许 H5 受信关闭动作和只读导航在动态页面上复验目标结构。"""
    if operation is not InteractionOperation.CLICK:
        return False
    target_mode = parameters.get("target_mode")
    if target_mode == "startup_ad_dismissal":
        return isinstance(parameters.get("startup_ad_capability"), str)
    if target_mode == "popup_dismissal":
        obstruction_signature = parameters.get("obstruction_signature")
        return bool(
            isinstance(parameters.get("popup_dismissal_capability"), str)
            and isinstance(obstruction_signature, str)
            and obstruction_signature.startswith("h5-obstruction:")
            and target.h5_popup_dismissal_strategy == "safe_outside_then_close"
        )
    return _allows_h5_dynamic_geometry_revalidation(
        parameters=parameters,
        operation=operation,
    )


def _allows_h5_dynamic_geometry_revalidation(
    *,
    parameters: Mapping[str, Any],
    operation: InteractionOperation,
) -> bool:
    if operation is not InteractionOperation.CLICK:
        return False
    frontier_snapshot_id = parameters.get("frontier_snapshot_id")
    return bool(
        parameters.get("target_mode") in H5_DYNAMIC_LOGICAL_TARGET_MODES
        and isinstance(frontier_snapshot_id, str)
        and frontier_snapshot_id.startswith("h5-task-frontier:")
    )


def _optional_color_scheme(value: object) -> str | None:
    return value if value in {"light", "dark", "no-preference"} else None


def _optional_web_capture_stage(value: object) -> str | None:
    return (
        value
        if value
        in {
            "web_entry_probe",
            "web_page_probe",
            "web_page_ready_baseline",
            "web_primary_content_evidence",
        }
        else None
    )


def _merge_action_evidence(
    *,
    capture_result: PageCaptureResult,
    action_result: PageCaptureResult,
    required_types: set[str],
) -> PageCaptureResult:
    artifacts = [
        artifact
        for artifact in capture_result.artifacts
        if artifact.artifact_type not in _ACTION_RUNTIME_ARTIFACT_TYPES
    ]
    action_trace = [*action_result.action_trace, *capture_result.action_trace]
    console_logs = [*action_result.console_logs, *capture_result.console_logs]
    network_requests = [
        *action_result.network_requests,
        *capture_result.network_requests,
    ]
    if "action_trace" in required_types:
        # CLI 对比日志留在附件里，不进入用于动作后置验证的 canonical action_trace。
        provider_observations = [
            item
            for result in (action_result, capture_result)
            for artifact in result.artifacts
            if artifact.artifact_type == "action_trace" and isinstance(artifact.content, list)
            for item in artifact.content
            if isinstance(item, dict)
            and item.get("comparison_only") is True
            and "provider_observation" in item
        ]
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="action_trace",
                artifact_id=None,
                content=[*provider_observations, *action_trace],
            )
        )
    if "screenshot" in required_types:
        artifacts.extend(
            artifact
            for artifact in action_result.artifacts
            if artifact.artifact_type == "screenshot"
            and isinstance(artifact.content, dict)
            and artifact.content.get("sequence") is True
            and artifact.content.get("purpose") in _ACTION_BOUND_SCREENSHOT_PURPOSES
        )
        # H5 滚动截图须连同逐帧结构与覆盖缺口保存，不能被最后一次观察替代。
        artifacts.extend(
            artifact
            for artifact in action_result.artifacts
            if artifact.artifact_type in {"screenshot", "ui_snapshot", "page_coverage"}
            and (
                artifact.artifact_type in required_types
                or artifact.artifact_type == "page_coverage"
            )
            and isinstance(artifact.content, dict)
            and artifact.content.get("capture_role") == _H5_VERTICAL_SCROLL_CAPTURE_ROLE
        )
    artifacts.extend(
        artifact
        for artifact in action_result.artifacts
        if artifact.artifact_type in required_types
        and artifact.artifact_type in {"screenshot", "ui_snapshot", "page_coverage"}
        and isinstance(artifact.content, dict)
        and artifact.content.get("capture_role")
        == _H5_PAYMENT_HORIZONTAL_CAPTURE_ROLE
    )
    if "har" in required_types:
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="har",
                artifact_id=None,
                content={"format": "network_events", "entries": network_requests},
            )
        )
    elif action_result.mode == PageCaptureMode.GOTO.value:
        # 首跳之后的观察通常没有网络事件，仍须保留导航动作自身的真实 HAR。
        artifacts.extend(
            artifact for artifact in action_result.artifacts if artifact.artifact_type == "har"
        )
    if "console_log" in required_types:
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="console_log",
                artifact_id=None,
                content=console_logs,
            )
        )
    metrics = dict(capture_result.metrics)
    navigation_retry = action_result.metrics.get("navigation_retry")
    if isinstance(navigation_retry, Mapping):
        metrics["navigation_retry"] = dict(navigation_retry)
    navigation_scope = action_result.metrics.get("navigation_scope")
    if isinstance(navigation_scope, Mapping):
        metrics["navigation_scope"] = dict(navigation_scope)
    navigation_document = action_result.metrics.get("navigation_document")
    if isinstance(navigation_document, Mapping):
        metrics["navigation_document"] = dict(navigation_document)
    if action_result.metrics.get("diagnostic_role") in {
        "post_navigation_out_of_scope", "post_navigation_document_unavailable",
    }:
        # 后态观察成功不能把已经失败的导航落点或空白主文档改写成可信业务页面。
        metrics.update(
            diagnostic_capture=True,
            diagnostic_role=action_result.metrics["diagnostic_role"],
            postcondition_complete=False,
        )
    return replace(
        capture_result,
        artifacts=artifacts,
        console_logs=console_logs,
        network_requests=network_requests,
        action_trace=action_trace,
        metrics=metrics,
    )


def _error_result(
    request: ExecuteInteractionRequest,
    *,
    action_execution_id: str,
    status: InteractionResultStatus,
    error_code: str,
    error_message: str,
    retryable: bool = False,
    state_summary: dict[str, Any] | None = None,
    state_diff: dict[str, Any] | None = None,
) -> InteractionResult:
    intent = request.intent
    return InteractionResult(
        task_id=intent.task_id,
        session_id=intent.session_id,
        intent_id=intent.intent_id,
        action_execution_id=action_execution_id,
        source_capture_id=request.source_capture_id,
        status=status,
        error_code=error_code,
        error_message=error_message[:2_000],
        retryable=retryable,
        state_summary=state_summary or {},
        state_diff=state_diff or {},
    )


def _remaining_timeout_seconds(deadline: datetime) -> float:
    return max((deadline - datetime.now(UTC)).total_seconds(), 0.0)


def _public_state_summary(result: PageCaptureResult) -> dict[str, Any]:
    return {
        "location": _public_location(result.final_url),
        "title": str(result.title)[:300] if result.title is not None else None,
        "reachable": result.reachable,
        "status_code": result.status_code,
        "page_settle_complete": result.metrics.get("page_settle_complete"),
        "page_inspection_ready": result.metrics.get("page_inspection_ready"),
    }


def _attach_logical_target_audit(
    result: PageCaptureResult,
    *,
    target: ResolvedInteractionTarget,
) -> None:
    """把目标验签摘要写入动作轨迹，不向 Agent 暴露运行时 selector。"""
    if target.target_trust != "trusted" or not result.action_trace:
        return
    audit = {
        "target_source": target.target_source,
        "target_signature": target.target_signature,
        "source_state_fingerprint": target.source_state_fingerprint,
        "target_trust": target.target_trust,
    }
    entry = result.action_trace[-1]
    if isinstance(entry, dict):
        entry["logical_target"] = audit
    for artifact in result.artifacts:
        if artifact.artifact_type != "action_trace" or not isinstance(artifact.content, list):
            continue
        artifact_entry = artifact.content[-1] if artifact.content else None
        if isinstance(artifact_entry, dict):
            artifact_entry["logical_target"] = dict(audit)


def _annotate_trusted_startup_ad_layer_clearance(
    result: PageCaptureResult,
    *,
    parameters: Mapping[str, Any],
    runtime_binding: LogicalTargetRuntimeBinding | None = None,
) -> None:
    """把已验真的单层开屏退场标成受信中间进展，不把后继广告冒充首页。"""

    if parameters.get("target_mode") != "startup_ad_dismissal":
        return
    entries: list[dict[str, Any]] = []
    primary = result.action_trace[-1] if result.action_trace else None
    if isinstance(primary, dict):
        entries.append(primary)
    for artifact in result.artifacts:
        if artifact.artifact_type != "action_trace" or not isinstance(artifact.content, list):
            continue
        artifact_entry = artifact.content[-1] if artifact.content else None
        if isinstance(artifact_entry, dict) and all(artifact_entry is not item for item in entries):
            entries.append(artifact_entry)
    for entry in entries:
        _annotate_startup_ad_trace_entry(entry, runtime_binding=runtime_binding)


def _annotate_startup_ad_trace_entry(
    entry: dict[str, Any],
    *,
    runtime_binding: LogicalTargetRuntimeBinding | None = None,
) -> None:
    if entry.get("dismissal_verified") is not True:
        return
    selector = str(entry.get("selector") or "").strip()
    if selector not in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL and (
        selector != FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR
    ):
        return
    from services.api.app.infrastructure.browser.h5_obstruction_effect import (
        annotate_h5_obstruction_effect,
    )

    annotate_h5_obstruction_effect(entry, binding=runtime_binding, effect="startup_layer_cleared")
    target_audit = entry.get("logical_target")
    if not isinstance(target_audit, Mapping) or target_audit.get("target_trust") != "trusted":
        return
    ui_state_diff = entry.get("state_diff")
    transition = (
        ui_state_diff.get("logical_surface_transition")
        if isinstance(ui_state_diff, Mapping)
        else None
    )
    if not (
        isinstance(transition, Mapping)
        and transition.get("protocol_version") == "logical_surface_transition.v1"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "surface_state_changed"
        and "surface_state" in set(transition.get("corroborating_signals") or ())
    ):
        return
    before_identity = str(transition.get("before_identity_fingerprint") or "")
    after_identity = str(transition.get("after_identity_fingerprint") or "")
    before_state = str(transition.get("before_state_fingerprint") or "")
    after_state = str(transition.get("after_state_fingerprint") or "")
    source_state = str(target_audit.get("source_state_fingerprint") or "")
    if not (
        _valid_logical_fingerprint(before_identity)
        and before_identity == after_identity
        and _valid_logical_fingerprint(before_state)
        and _valid_logical_fingerprint(after_state)
        and before_state != after_state
        and _valid_logical_fingerprint(source_state)
        and (
            runtime_binding is None
            or (
                before_identity == runtime_binding.source_identity_fingerprint
                and target_audit.get("target_signature") == runtime_binding.target_signature
            )
        )
        and (
            source_state == before_state
            # H5 动作前已复验相同页面与唯一目标，倒计时变化不能否定真实退场。
            or (
                runtime_binding is not None
                and runtime_binding.allow_dynamic_state_revalidation
            )
        )
    ):
        return
    entry["state_diff"] = {
        **dict(ui_state_diff),
        "logical_surface_transition": {
            **dict(transition),
            "reason_code": "trusted_startup_ad_layer_clearance",
        },
    }


def _annotate_trusted_popup_clearance(
    result: PageCaptureResult,
    *,
    parameters: Mapping[str, Any],
    runtime_binding: LogicalTargetRuntimeBinding | None,
    session_id: str | None = None,
) -> None:
    """把已验真的同页遮挡退场标成受信状态变化。"""

    if parameters.get("target_mode") != "popup_dismissal" or runtime_binding is None:
        return
    entries: list[dict[str, Any]] = []
    primary = result.action_trace[-1] if result.action_trace else None
    if isinstance(primary, dict):
        entries.append(primary)
    for artifact in result.artifacts:
        if artifact.artifact_type != "action_trace" or not isinstance(artifact.content, list):
            continue
        artifact_entry = artifact.content[-1] if artifact.content else None
        if isinstance(artifact_entry, dict) and all(artifact_entry is not item for item in entries):
            entries.append(artifact_entry)
    final_obstruction_cleared = _result_final_obstruction_cleared(result)
    # 后续层找不到关闭点时，保留先前已验真的推进，但绝不声称弹窗链清空。
    prior_layer = next((
        item for item in reversed(result.action_trace[:-1])
        if item.get("action") == "click"
        and item.get("dismissal_verified") is True
        and item.get("popup_dismissal_outcome") == "next_popup_layer_visible"
        and item.get("obstruction_chain_index") == 1
        and isinstance(primary, Mapping)
        and item.get("requested_selector") == (
            primary.get("requested_selector") or primary.get("selector")
        )
    ), None) if (
        str(session_id or "").startswith("h5:")
        and parameters.get("popup_dismissal_capability")
        in {"rendered_modal_close", "modal_local_safe_acknowledgement"}
    ) else None
    for entry in entries:
        _annotate_popup_clearance_trace_entry(
            entry,
            runtime_binding=runtime_binding,
            final_obstruction_cleared=final_obstruction_cleared,
            allow_hidden_original_layer=(
                parameters.get("popup_dismissal_capability")
                in {"rendered_modal_close", "modal_local_safe_acknowledgement"}
                or (
                    str(session_id or "").startswith("h5:")
                    and parameters.get("popup_dismissal_capability")
                    == "profile_credential_screenshot_confirm"
                )
            ),
        )
        if prior_layer is not None and entry.get("result") in {
            "popup_dismissal_target_not_proven", "popup_dismissal_not_observed",
        }:
            summary = {
                **entry,
                "dismissal_verified": True,
                "popup_dismissal_outcome": "next_popup_layer_visible",
            }
            _annotate_popup_clearance_trace_entry(
                summary, runtime_binding=runtime_binding, allow_hidden_original_layer=True,
            )
            transition = summary.get("state_diff", {}).get("logical_surface_transition", {})
            if transition.get("reason_code") == "trusted_obstruction_layer_advanced":
                entry["state_diff"] = summary["state_diff"]
            if summary.get("state_diff", {}).get("h5_obstruction_effect"):
                entry["state_diff"] = summary["state_diff"]


def _result_final_obstruction_cleared(result: PageCaptureResult) -> bool:
    """以动作完成后的 UI Snapshot 判定弹窗链是否已经真正清空。"""
    for artifact in reversed(result.artifacts):
        if artifact.artifact_type != "ui_snapshot" or not isinstance(
            artifact.content, Mapping
        ):
            continue
        logical_surface = artifact.content.get("logical_surface")
        state_signals = (
            logical_surface.get("state_signals")
            if isinstance(logical_surface, Mapping)
            else None
        )
        if not isinstance(state_signals, Mapping):
            return False
        obstructions = state_signals.get("obstructions")
        return bool(
            str(state_signals.get("obstruction_count") or "") == "0"
            and isinstance(obstructions, list)
            and not obstructions
        )
    return False


def _annotate_popup_clearance_trace_entry(
    entry: dict[str, Any],
    *,
    runtime_binding: LogicalTargetRuntimeBinding,
    final_obstruction_cleared: bool = False,
    allow_hidden_original_layer: bool = False,
) -> None:
    dismissal_outcome = entry.get("popup_dismissal_outcome")
    accepted_outcomes = {
        "original_layer_absent",
        "next_popup_layer_visible",
    }
    # H5 原生关闭可保留隐藏节点；仍须经过浏览器的稳定退场证明。
    if allow_hidden_original_layer:
        accepted_outcomes.add("original_layer_hidden")
    if entry.get("dismissal_verified") is not True or dismissal_outcome not in accepted_outcomes:
        return
    if (
        dismissal_outcome == "next_popup_layer_visible"
        and not runtime_binding.allow_dynamic_state_revalidation
    ):
        return
    if allow_hidden_original_layer:
        from services.api.app.infrastructure.browser.h5_obstruction_effect import (
            annotate_h5_obstruction_effect,
        )

        annotate_h5_obstruction_effect(
            entry, binding=runtime_binding,
            effect=("cleared" if final_obstruction_cleared or dismissal_outcome in {
                "original_layer_absent", "original_layer_hidden",
            } else "layer_advanced"),
        )
    target_audit = entry.get("logical_target")
    if not (
        isinstance(target_audit, Mapping)
        and target_audit.get("target_trust") == "trusted"
        and target_audit.get("target_signature") == runtime_binding.target_signature
    ):
        return
    state_diff = entry.get("state_diff")
    transition = (
        state_diff.get("logical_surface_transition")
        if isinstance(state_diff, Mapping)
        else None
    )
    if not isinstance(transition, Mapping):
        return
    before_identity = str(transition.get("before_identity_fingerprint") or "")
    after_identity = str(transition.get("after_identity_fingerprint") or "")
    before_state = str(transition.get("before_state_fingerprint") or "")
    after_state = str(transition.get("after_state_fingerprint") or "")
    source_state = str(target_audit.get("source_state_fingerprint") or "")
    if not (
        transition.get("protocol_version") == "logical_surface_transition.v1"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "surface_state_changed"
        and "surface_state" in set(transition.get("corroborating_signals") or ())
        and _valid_logical_fingerprint(before_identity)
        and before_identity == after_identity == runtime_binding.source_identity_fingerprint
        and _valid_logical_fingerprint(before_state)
        and _valid_logical_fingerprint(after_state)
        and before_state != after_state
        and _valid_logical_fingerprint(source_state)
        and (
            source_state == before_state
            or runtime_binding.allow_dynamic_state_revalidation
        )
    ):
        return
    entry["state_diff"] = {
        **dict(state_diff),
        "logical_surface_transition": {
            **dict(transition),
            "reason_code": (
                "trusted_obstruction_clearance"
                if final_obstruction_cleared
                or dismissal_outcome in {"original_layer_absent", "original_layer_hidden"}
                else "trusted_obstruction_layer_advanced"
            ),
        },
    }


def _valid_logical_fingerprint(value: str) -> bool:
    return len(value) == 71 and value.startswith("sha256:")


def _logical_navigation_postcondition_error(
    *,
    parameters: Mapping[str, Any],
    state_diff: Mapping[str, Any],
) -> str | None:
    effect = state_diff.get("h5_obstruction_effect")
    if (
        isinstance(effect, Mapping)
        and effect.get("schema_version") == "h5-obstruction-effect.v1"
        and effect.get("verified") is True
    ):
        if parameters.get("target_mode") == "popup_dismissal":
            if effect.get("effect") == "cleared":
                return None
            if effect.get("effect") == "layer_advanced":
                return "H5_POPUP_CHAIN_REMAINS_BLOCKED"
        if parameters.get("target_mode") == "startup_ad_dismissal":
            if effect.get("effect") == "startup_layer_cleared":
                return None
    contract = parameters.get("logical_target")
    if not isinstance(contract, Mapping) or contract.get("expects_logical_navigation") is not True:
        if not (isinstance(contract, Mapping) and contract.get("expects_scroll_effect") is True):
            return None
        scroll_effect = state_diff.get("scroll_effect")
        if not (
            isinstance(scroll_effect, Mapping)
            and scroll_effect.get("protocol_version") == "scroll-effect.v1"
            and scroll_effect.get("observed") is True
        ):
            return "SCROLL_EFFECT_NOT_OBSERVED"
        return None
    transition = state_diff.get("logical_surface_transition")
    if parameters.get("target_mode") == "visual_navigation_coordinate":
        proof = transition.get("visual_proof") if isinstance(transition, Mapping) else None
        if _visual_navigation_proof_reports_no_effect(proof):
            return "VISUAL_NAVIGATION_NO_EFFECT"
        if not _trusted_visual_navigation_postcondition(
            transition=transition,
            proof=proof,
        ):
            return "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT"
        return None
    if (
        parameters.get("target_mode") == "popup_dismissal"
        and isinstance(transition, Mapping)
        and transition.get("protocol_version") == "logical_surface_transition.v1"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "surface_state_changed"
        and transition.get("reason_code") == "trusted_obstruction_clearance"
    ):
        return None
    if (
        parameters.get("target_mode") == "popup_dismissal"
        and isinstance(transition, Mapping)
        and transition.get("protocol_version") == "logical_surface_transition.v1"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "surface_state_changed"
        and transition.get("reason_code") == "trusted_obstruction_layer_advanced"
    ):
        return "H5_POPUP_CHAIN_REMAINS_BLOCKED"
    if (
        parameters.get("target_mode") == "startup_ad_dismissal"
        and isinstance(transition, Mapping)
        and transition.get("protocol_version") == "logical_surface_transition.v1"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "surface_state_changed"
        and transition.get("reason_code") == "trusted_startup_ad_layer_clearance"
    ):
        return None
    if (
        parameters.get("target_mode") == "page_tab_navigation"
        and isinstance(transition, Mapping)
        and transition.get("protocol_version") == "logical_surface_transition.v1"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "surface_state_changed"
        and transition.get("reason_code") == "trusted_page_tab_state_change"
        and "page_tab_activation"
        in set(transition.get("corroborating_signals") or ())
    ):
        return None
    if not (
        isinstance(transition, Mapping)
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "logical_page_changed"
    ):
        return "LOGICAL_TRANSITION_UNVERIFIED"
    return None


def _logical_navigation_postcondition_satisfied(
    *,
    parameters: Mapping[str, Any],
    state_diff: Mapping[str, Any],
) -> bool:
    """保留布尔后置条件接口，错误分类由 Runtime 上层消费。"""
    return (
        _logical_navigation_postcondition_error(
            parameters=parameters,
            state_diff=state_diff,
        )
        is None
    )


def _visual_navigation_proof_reports_no_effect(proof: Any) -> bool:
    return bool(
        isinstance(proof, Mapping)
        and proof.get("protocol_version") == "visual_navigation_transition_proof.v1"
        and proof.get("status") == "no_effect"
        and proof.get("after_frame_count") == 2
        and proof.get("after_frames_stable") is True
        and proof.get("main_structure_changed") is False
        and proof.get("after_identity_bound") is True
    )


def _trusted_visual_navigation_postcondition(
    *,
    transition: Any,
    proof: Any,
) -> bool:
    """视觉候选只接受与当前 after identity 同处一份 transition 的主证明。"""
    if not isinstance(transition, Mapping) or not isinstance(proof, Mapping):
        return False
    after_identity = str(transition.get("after_identity_fingerprint") or "")
    return bool(
        transition.get("logical_transition_proof") == "trusted"
        and transition.get("transition_kind") == "logical_page_changed"
        and len(after_identity) == 71
        and after_identity.startswith("sha256:")
        and proof.get("protocol_version") == "visual_navigation_transition_proof.v1"
        and proof.get("status") == "trusted"
        and proof.get("after_frame_count") == 2
        and proof.get("after_frames_stable") is True
        and proof.get("main_structure_changed") is True
        and proof.get("after_identity_bound") is True
    )


def _public_state_diff(result: PageCaptureResult) -> dict[str, Any]:
    entry = result.action_trace[-1] if result.action_trace else {}
    navigation = entry.get("navigation") if isinstance(entry, dict) else None
    navigation = navigation if isinstance(navigation, dict) else {}
    payload = {
        "transition_kind": entry.get("transition_kind") if isinstance(entry, dict) else None,
        "before_location": _public_location(navigation.get("before_url")),
        "after_location": _public_location(navigation.get("after_url")),
        "location_changed": navigation.get("current_url_changed"),
        "new_page_count": navigation.get("new_page_count"),
    }
    ui_state_diff = entry.get("state_diff") if isinstance(entry, dict) else None
    scroll_effect = ui_state_diff.get("scroll_effect") if isinstance(ui_state_diff, dict) else None
    if isinstance(scroll_effect, dict):
        payload["scroll_effect"] = dict(scroll_effect)
    logical_transition = (
        ui_state_diff.get("logical_surface_transition") if isinstance(ui_state_diff, dict) else None
    )
    if isinstance(logical_transition, dict):
        payload["logical_surface_transition"] = dict(logical_transition)
    effect = ui_state_diff.get("h5_probe_effect") if isinstance(ui_state_diff, dict) else None
    if isinstance(effect, dict) and effect.get("goal") == "payment_plan_selection":
        import hashlib

        # 只保存 Driver 观测合同，定位器和带查询参数的地址不进入公开动作回执。
        payload["h5_probe_effect"] = {
            key: effect.get(key) for key in (
                "protocol_version", "goal", "subject_key", "verified",
                "readonly_isolation", "readonly_denied_count", "document_time_origin_ms",
            )
        }
        payload["h5_probe_effect"]["verified"] = (
            effect.get("verified") is True and entry.get("result") == "probe_effect_observed"
        )
        url = effect.get("url")
        payload["h5_probe_effect"]["url_hash"] = (
            hashlib.sha256(url.encode("utf-8")).hexdigest() if isinstance(url, str) else None
        )
    return payload


def _public_location(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.hostname:
        return text[:2_000]
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("navigation_depth 必须为非负整数")
    return value


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("媒体跳转目标必须为数字")
    return float(value)


def _optional_positive_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("浏览器设备缩放比例必须为正数")
    return float(value)
