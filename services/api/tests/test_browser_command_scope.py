"""Web 正式执行器必须在目标解析后再次验证服务端范围与可信控件语义。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.capture_coordination import ExecuteInteractionRequest
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedCommandScope,
)
from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    ResolvedInteractionTarget,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.core.config import WorkflowSettings
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    CommandPolicy,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.browser import capture_runtime
from services.api.app.infrastructure.browser.page_capture import PageCaptureResult
from services.api.app.infrastructure.db.h5_probe_action_execution_repository import (
    PostgresH5ProbeActionExecutionRepository,
)


def _request():
    return ExecuteInteractionRequest(
        agent_run_id="run-1",
        source_capture_id="before-1",
        finalization_epoch=1,
        intent=ActionIntent(
            task_id="task-1",
            session_id="session-1",
            intent_id="intent-1",
            idempotency_key="scope-check",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="parent-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="普通入口",
            purpose="探索普通页面",
            parameters={},
            risk_level=InteractionRiskLevel.LOW,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            capture_profile=CaptureProfile(
                profile_id="scope-check",
                before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
                after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
            ),
        ),
    )


@pytest.mark.parametrize(
    "scope_epoch,target_text,expected_code",
    [
        (None, "普通入口", "COMMAND_SCOPE_UNAVAILABLE"),
        (2, "普通入口", "COMMAND_SCOPE_EPOCH_MISMATCH"),
        (1, "确认付款", "COMMAND_TARGET_RISK_UNDERRATED"),
    ],
)
async def test_server_scope_and_resolved_risk_are_checked_before_browser(
    scope_epoch,
    target_text,
    expected_code,
):
    scope = (
        None
        if scope_epoch is None
        else TrustedCommandScope(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.WEB,
            finalization_epoch=scope_epoch,
            policy=CommandPolicy(),
        )
    )
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=scope))
    browser = SimpleNamespace(capture=AsyncMock())
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            return_value=ResolvedInteractionTarget(
                selector="#target",
                trusted_target_texts=(target_text,),
                trusted_target_role="button",
            )
        )
    )
    executor = capture_runtime.BrowserInteractionExecutor(
        browser=browser,
        target_resolver=resolver,
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        command_validator=InteractionCommandValidator(scope_reader=scope_reader),
    )
    result = await executor.execute(_request())
    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == expected_code
    assert result.state_diff["driver_invoked"] is False
    resolver.resolve.assert_awaited_once()
    scope_reader.read_command_scope.assert_awaited_once()
    browser.capture.assert_not_awaited()


async def test_server_scoped_web_navigation_does_not_require_dom_target_semantics() -> None:
    """服务端锁定 URL 的初始导航没有页面控件，仍须在主机边界内允许执行。"""

    scope = TrustedCommandScope(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        finalization_epoch=1,
        policy=CommandPolicy(allowed_hosts=("example.test",)),
    )
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=scope))
    browser_requests = []

    async def capture(browser_request):
        browser_requests.append(browser_request)
        await browser_request._pre_action_validation("about:blank")
        return PageCaptureResult(
            execution_status="success",
            mode="goto",
            reachable=True,
            final_url="https://example.test/",
            title="首页",
            status_code=200,
        )

    browser = SimpleNamespace(capture=capture)
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            return_value=ResolvedInteractionTarget(destination_url="https://example.test/")
        )
    )
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        source_capture_id="before-1",
        finalization_epoch=1,
        intent=ActionIntent(
            task_id="task-1",
            session_id="session-1",
            intent_id="web-bootstrap:run-1",
            idempotency_key="web-bootstrap:run-1",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="web-entry:run-1",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="Web 任务锁定入口",
            purpose="打开任务提交时锁定的桌面 Web 目标 URL",
            parameters={
                "destination_url": "https://example.test/",
                "initial_surface_action": True,
                "web_capture_stage": "web_entry_probe",
            },
            risk_level=InteractionRiskLevel.LOW,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            capture_profile=CaptureProfile(
                profile_id="web-bootstrap",
                before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
                after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
            ),
        ),
    )
    executor = capture_runtime.BrowserInteractionExecutor(
        browser=browser,
        target_resolver=resolver,
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        command_validator=InteractionCommandValidator(scope_reader=scope_reader),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code is None
    assert scope_reader.read_command_scope.await_count == 3
    assert len(browser_requests) == 1


def _production_runtime(monkeypatch):
    monkeypatch.setattr(
        capture_runtime,
        "get_settings",
        lambda: SimpleNamespace(
            workflow=WorkflowSettings(),
            quality_optimization=SimpleNamespace(web_interaction_timeout_seconds=20),
            evidence_storage=object(),
        ),
    )
    monkeypatch.setattr(
        capture_runtime,
        "build_evidence_artifact_store",
        lambda _settings: object(),
    )
    return capture_runtime.build_default_web_capture_runtime(
        browser=SimpleNamespace(_h5_visual_coordinate_mode="off"),
    )


def test_production_web_composition_shares_validator_with_h5_and_executor(monkeypatch):
    runtime = _production_runtime(monkeypatch)
    validator = runtime.coordinator._command_validator
    assert validator.scope_reader is not None
    assert runtime._executor._command_validator is validator
    assert runtime._h5_exploration_coordinator._command_validator is validator
    assert runtime._driver._executor._command_validator is validator
    assert runtime._command_validator is validator


def test_h5_probe_coordinator_reuses_runtime_components_and_enables_probe_gate(monkeypatch):
    runtime = _production_runtime(monkeypatch)

    coordinator = runtime.build_h5_probe_coordinator(
        supplemental_evidence_policy=build_lab_supplemental_evidence_policy(),
        probe_action_roles=True,
    )

    assert coordinator._command_validator is runtime._command_validator
    assert coordinator._interaction_executor._delegate is runtime._executor
    assert isinstance(
        coordinator._action_executions,
        PostgresH5ProbeActionExecutionRepository,
    )
    assert coordinator._action_executions is not runtime._action_executions
    assert coordinator._action_recovery_captures is runtime._capture_recovery
    assert coordinator._supplemental_evidence_policy.enabled is True
    assert coordinator._supplemental_evidence_policy.runtime_ready is True


def test_h5_restoration_coordinator_keeps_standard_action_roles(monkeypatch):
    runtime = _production_runtime(monkeypatch)

    coordinator = runtime.build_h5_probe_coordinator(
        supplemental_evidence_policy=build_lab_supplemental_evidence_policy(),
    )

    assert coordinator._interaction_executor is runtime._executor
    assert coordinator._action_executions is runtime._action_executions


async def test_legacy_tool_keeps_persisted_before_after_and_scope_when_exit_gate_is_off(
    monkeypatch,
):
    from services.api.app.application import web_agent_page_capture, web_evidence_exit_gate

    runtime = _production_runtime(monkeypatch)
    coordinate = AsyncMock(return_value="captured")
    monkeypatch.setattr(web_agent_page_capture, "coordinate_agent_page_capture", coordinate)
    monkeypatch.setattr(
        web_evidence_exit_gate,
        "build_web_evidence_exit_gate",
        lambda: web_evidence_exit_gate.NoopEvidenceExitGate(),
    )
    spec = object()
    assert await runtime.run_agent_page_capture(spec) == "captured"
    args = coordinate.await_args.kwargs
    assert args["spec"] is spec
    assert args["persistence_service"] is runtime._page_capture_persistence
    assert args["persist_before_capture"] is True
    assert args["session_command_gate"] is runtime.session_command_gate
    assert args["action_execution_repository"] is runtime._action_executions
    assert args["action_recovery_captures"] is runtime._capture_recovery
    assert args["command_validator"] is runtime.coordinator._command_validator
    assert args["snapshot_reader"] is runtime._trusted_ui_snapshot_reader


async def test_actual_browser_source_is_rechecked_inside_session_before_any_action(monkeypatch):
    from services.api.app.infrastructure.browser.page_capture import PlaywrightPageCaptureBrowser

    browser = PlaywrightPageCaptureBrowser()
    browser._sessions["session-1"] = SimpleNamespace(
        page=SimpleNamespace(url="https://outside.test/redirect"),
        context=object(),
        cli_session=None,
        console_logs=[],
        network_requests=[],
        network_inflight={},
    )
    implementation = AsyncMock()
    monkeypatch.setattr(browser, "_capture_on_page_impl", implementation)
    scope = TrustedCommandScope(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        finalization_epoch=1,
        policy=CommandPolicy(allowed_hosts=("example.test",)),
    )
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=scope))
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            return_value=ResolvedInteractionTarget(
                selector="#ordinary",
                trusted_target_texts=("普通入口",),
                trusted_target_role="button",
            )
        )
    )
    executor = capture_runtime.BrowserInteractionExecutor(
        browser=browser,
        target_resolver=resolver,
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        command_validator=InteractionCommandValidator(scope_reader=scope_reader),
    )
    result = await executor.execute(_request())
    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert result.state_diff["driver_invoked"] is False
    assert scope_reader.read_command_scope.await_count == 2
    implementation.assert_not_awaited()


@pytest.mark.parametrize("uncertain,outer_timeout", [(False, False), (True, False), (True, True)])
async def test_cli_unknown_response_retains_after_evidence_without_repeating_action(
    uncertain, outer_timeout
):
    from services.api.app.application.capture_coordinator import (
        CaptureCoordinationCommand,
        CaptureCoordinationError,
        CaptureCoordinator,
    )
    from services.api.app.application.web_evidence_exit_gate import NoopEvidenceExitGate
    from services.api.app.domain.interaction import ActionExecutionStatus
    from services.api.app.infrastructure.browser.playwright_cli_process import PlaywrightCliError
    from services.api.tests.test_capture_coordinator import (
        RecordingActionExecutionRepository,
        RecordingCaptureWriter,
    )

    browser = SimpleNamespace(
        capture=AsyncMock(
            side_effect=PlaywrightCliError(
                "CLI_RESPONSE_UNAVAILABLE" if uncertain else "CLI_TARGET_RISK_REJECTED",
                action_uncertain=uncertain,
            )
        )
    )
    if outer_timeout:

        async def timeout_after_cli_dispatch(request):
            request._cli_action_state["outcome"] = "unknown"
            raise TimeoutError()

        browser.capture.side_effect = timeout_after_cli_dispatch
    buffer = InMemoryActionEvidenceBuffer()
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            return_value=ResolvedInteractionTarget(
                selector="#ordinary",
                trusted_target_texts=("普通入口",),
                trusted_target_role="button",
            )
        )
    )
    executor = capture_runtime.BrowserInteractionExecutor(
        browser=browser,
        target_resolver=resolver,
        evidence_buffer=buffer,
        command_validator=InteractionCommandValidator(),
    )
    events = []
    ledger = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=executor,
        evidence_exit_gate=NoopEvidenceExitGate(),
        action_execution_repository=ledger,
    )
    command = CaptureCoordinationCommand(agent_run_id="run-1", intent=_request().intent)
    if uncertain:
        result = await coordinator.coordinate(command)
        expected = (
            InteractionResultStatus.TIMED_OUT if outer_timeout else InteractionResultStatus.FAILED
        )
        assert result.interaction.status is expected
        assert result.after_capture.capture_id == "capture-after"
        assert ledger.state.status is (
            ActionExecutionStatus.TIMED_OUT if outer_timeout else ActionExecutionStatus.FAILED
        )
        assert ledger.state.result_capture_id == "capture-after"
        assert result.interaction.retryable is False
        pending = await buffer.get(action_execution_id=result.interaction.action_execution_id)
        assert pending.browser_result.metrics["provider_outcome"] == "unknown"
        # 同一动作再次进入执行器只读已有回执，不重新调用浏览器。
        request = ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=command.intent,
            source_capture_id="capture-before",
            action_execution_id=result.interaction.action_execution_id,
        )
        replay = await executor.execute(request)
        assert replay.status is expected
    else:
        with pytest.raises(CaptureCoordinationError, match="动作执行未完成"):
            await coordinator.coordinate(command)
        assert ledger.state.status is ActionExecutionStatus.BLOCKED
        assert "capture:after_action" not in events
    assert browser.capture.await_count == 1


async def test_cli_cancellation_marks_original_request_through_browser_clones(monkeypatch):
    import asyncio

    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        PlaywrightPageCaptureBrowser,
        _clone_page_capture_request,
    )

    process = SimpleNamespace(
        is_quarantined=False,
        quarantine_reason=None,
        require_action_session=lambda: None,
    )
    original = PageCaptureRequest(mode=PageCaptureMode.SINGLE_TRIGGER)
    cloned = _clone_page_capture_request(original, _cli_session=SimpleNamespace(process=process))
    browser = PlaywrightPageCaptureBrowser()

    async def canceled_action(**kwargs):
        process.is_quarantined = True
        process.quarantine_reason = "action_outcome_uncertain"
        raise asyncio.CancelledError()

    monkeypatch.setattr(browser, "_capture_on_page_impl", canceled_action)
    with pytest.raises(asyncio.CancelledError):
        await browser._capture_on_page(
            request=cloned,
            context=object(),
            page=SimpleNamespace(url="https://example.test"),
            console_logs=[],
            network_requests=[],
            timeout_error_type=TimeoutError,
        )
    assert original._cli_action_state == {"outcome": "unknown"}
    assert original._cli_action_state is cloned._cli_action_state
