"""首导航必须以真实落点验明任务边界，离域后态仍须留下诊断证据。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    ExecuteInteractionRequest,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedCommandScope,
)
from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    ResolvedInteractionTarget,
)
from services.api.app.application.web_evidence_exit_gate import NoopEvidenceExitGate
from services.api.app.domain.interaction import (
    ActionExecutionStatus,
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    CommandPolicy,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserCaptureWriter,
    BrowserInteractionExecutor,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureResult,
)
from services.api.tests.test_capture_coordinator import RecordingActionExecutionRepository

TARGET_URL = "https://dys18.com/"
OUTSIDE_URL = "https://example.com/"


def _request(destination_url=TARGET_URL):
    return ExecuteInteractionRequest(
        agent_run_id="run-1",
        source_capture_id="capture-before",
        finalization_epoch=1,
        intent=ActionIntent(
            task_id="task-1",
            session_id="h5:run-1",
            intent_id="h5-bootstrap:run-1",
            idempotency_key="h5-bootstrap:run-1",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="h5-entry:run-1",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="H5 任务锁定入口",
            purpose="打开任务锁定的 H5 目标 URL",
            parameters={"destination_url": destination_url, "initial_surface_action": True},
            risk_level=InteractionRiskLevel.LOW,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            capture_profile=CaptureProfile(
                profile_id="h5-landing-scope",
                before=CaptureDirective(role="before_action", artifact_types=("dom",)),
                after=CaptureDirective(role="after_action", artifact_types=("dom", "action_trace")),
            ),
        ),
    )


def _scope(allowed_hosts=("dys18.com",), *, epoch=1):
    return TrustedCommandScope(
        task_id="task-1",
        session_id="h5:run-1",
        surface_kind=SurfaceKind.WEB,
        finalization_epoch=epoch,
        policy=CommandPolicy(allowed_hosts=allowed_hosts),
    )


def _page_result(url, *, mode="goto", status_code=200, with_receipt=True):
    trace = (
        [
            {
                "action": "navigate",
                "destination_url": TARGET_URL,
                "command_succeeded": True,
                "arrival_verified": False,
                "argument_hash": "retained-cli-receipt",
            }
        ]
        if mode == "goto" and with_receipt
        else []
    )
    network_requests = (
        [
            {"event": "request", "url": TARGET_URL, "resource_type": "document"},
            {"event": "response", "url": url, "status": status_code},
        ]
        if mode == "goto"
        else []
    )
    return PageCaptureResult(
        execution_status="success",
        mode=mode,
        reachable=True,
        final_url=url,
        title="Example Domain" if url == OUTSIDE_URL else "首页",
        status_code=status_code,
        artifacts=[
            PageCaptureArtifact(artifact_type="dom", artifact_id=None, content={"url": url}),
            PageCaptureArtifact(artifact_type="action_trace", artifact_id=None, content=trace),
            PageCaptureArtifact(
                artifact_type="har",
                artifact_id=None,
                content={"format": "network_events", "entries": network_requests},
            ),
        ],
        action_trace=trace,
        network_requests=network_requests,
    )


def _executor(browser, scope_reader, buffer, *, destination_url=TARGET_URL):
    return BrowserInteractionExecutor(
        browser=browser,
        target_resolver=SimpleNamespace(
            resolve=AsyncMock(
                return_value=ResolvedInteractionTarget(destination_url=destination_url)
            )
        ),
        evidence_buffer=buffer,
        command_validator=InteractionCommandValidator(scope_reader=scope_reader),
    )


@pytest.mark.parametrize("status_code", [200, 304, None])
async def test_outside_landing_is_blocked_and_keeps_actual_action_evidence(status_code):
    async def capture(request):
        await request._pre_action_validation("about:blank")
        return _page_result(OUTSIDE_URL, status_code=status_code)

    browser = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope()))
    buffer = InMemoryActionEvidenceBuffer()
    executor = _executor(browser, scope_reader, buffer)

    result = await executor.execute(_request())

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert result.retryable is False
    assert result.state_diff["driver_invoked"] is True
    assert result.state_diff["command_scope_verified"] is False
    assert result.state_diff["arrival_verified"] is False
    assert result.state_summary["post_action_evidence_available"] is True
    assert result.state_summary["location"] == OUTSIDE_URL
    pending = await buffer.get(action_execution_id=result.action_execution_id)
    assert pending.browser_result.final_url == OUTSIDE_URL
    assert pending.browser_result.status_code == status_code
    assert pending.browser_result.artifacts[0].content["url"] == OUTSIDE_URL
    assert pending.browser_result.execution_status == "failed"
    assert pending.browser_result.metrics["diagnostic_capture"] is True
    assert pending.browser_result.metrics["postcondition_complete"] is False
    assert pending.browser_result.metrics["navigation_scope"]["requested_url"] == TARGET_URL
    assert pending.browser_result.metrics["navigation_scope"]["main_response_status"] == status_code
    # 同一动作只复用失败事实，不能再次导航或用备用地址掩盖实际落点。
    replay = await executor.execute(_request())
    assert replay == result
    browser.capture.assert_awaited_once()


@pytest.mark.parametrize(
    ("final_url", "allowed_hosts", "destination_url"),
    [
        ("https://dys18.com/home?tab=new", ("dys18.com",), TARGET_URL),
        ("https://DYS18.com/home", ("dys18.com",), TARGET_URL),
        ("https://entry.dys18.com/", ("dys18.com", "entry.dys18.com"), TARGET_URL),
        (TARGET_URL, ("dys18.com",), "http://dys18.com/"),
    ],
)
async def test_allowed_landing_keeps_normal_evidence_pending(
    final_url, allowed_hosts, destination_url
):
    async def capture(request):
        await request._pre_action_validation("about:blank")
        return _page_result(final_url)

    browser = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope(allowed_hosts)))
    buffer = InMemoryActionEvidenceBuffer()

    result = await _executor(
        browser, scope_reader, buffer, destination_url=destination_url
    ).execute(_request(destination_url))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code is None
    assert scope_reader.read_command_scope.await_count == 3
    pending = await buffer.get(action_execution_id=result.action_execution_id)
    assert pending.browser_result.final_url == final_url
    assert "diagnostic_capture" not in pending.browser_result.metrics
    assert pending.browser_result.metrics["navigation_scope"]["verified"] is True


@pytest.mark.parametrize("source_same_run", [True, False])
async def test_recovery_compatibility_requires_same_run_source_and_frozen_landing(source_same_run):
    base = _request()
    session_id = "h5:run-1:recovery:" + "a" * 20
    request = replace(
        base,
        intent=replace(
            base.intent,
            intent_id="h5-browser-recovery:work:action:1",
            idempotency_key="h5-browser-recovery:work:action:1",
            session_id=session_id,
            source_capture_id="capture-before",
            parameters={
                "destination_url": TARGET_URL,
                "h5_browser_recovery": True,
                "recovery_source_session_id": "h5:run-1" if source_same_run else "h5:other-run",
            },
        ),
    )

    async def capture(browser_request):
        validator = browser_request._h5_entry_landing_validation
        if source_same_run:
            assert callable(validator)
            assert await validator(TARGET_URL) is True
            assert await validator(OUTSIDE_URL) is False
        else:
            assert validator is None
        return _page_result(TARGET_URL)

    browser = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    scope_reader = SimpleNamespace(
        read_command_scope=AsyncMock(return_value=replace(_scope(), session_id=session_id))
    )
    result = await _executor(browser, scope_reader, InMemoryActionEvidenceBuffer()).execute(request)
    assert result.status is InteractionResultStatus.EVIDENCE_PENDING


async def test_forbidden_destination_is_still_rejected_before_navigation():
    browser = SimpleNamespace(capture=AsyncMock())
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope()))
    buffer = InMemoryActionEvidenceBuffer()

    result = await _executor(browser, scope_reader, buffer, destination_url=OUTSIDE_URL).execute(
        _request(OUTSIDE_URL)
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert result.state_diff["driver_invoked"] is False
    assert "post_action_evidence_available" not in result.state_summary
    browser.capture.assert_not_awaited()
    assert await buffer.get(action_execution_id=result.action_execution_id) is None


async def test_landing_validation_preserves_evidence_when_frozen_epoch_changes():
    async def capture(request):
        await request._pre_action_validation("about:blank")
        return _page_result(TARGET_URL)

    browser = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    scope_reader = SimpleNamespace(
        read_command_scope=AsyncMock(side_effect=[_scope(), _scope(), _scope(epoch=2)])
    )
    buffer = InMemoryActionEvidenceBuffer()

    result = await _executor(browser, scope_reader, buffer).execute(_request())

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "COMMAND_SCOPE_EPOCH_MISMATCH"
    assert result.state_diff["driver_invoked"] is True
    assert result.state_summary["post_action_evidence_available"] is True
    pending = await buffer.get(action_execution_id=result.action_execution_id)
    assert pending.browser_result.final_url == TARGET_URL


class _CapturePersistence:
    def __init__(self):
        self.saved = []
        self.references = {}

    async def persist(self, **kwargs):
        self.saved.append(kwargs)
        capture_id = f"capture-{len(self.saved)}"
        artifacts = [
            SimpleNamespace(artifact_id=f"{capture_id}-{index}", artifact_type=item.artifact_type)
            for index, item in enumerate(kwargs["result"].artifacts)
        ]
        self.references[capture_id] = CaptureRef(
            task_id=kwargs["task_id"],
            session_id=kwargs["surface_session_id"],
            capture_id=capture_id,
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role=kwargs["capture_role"],
            artifact_ids=tuple(item.artifact_id for item in artifacts),
            parent_capture_id=kwargs["parent_page_capture_id"],
            action_execution_id=kwargs["action_execution_id"],
        )
        return SimpleNamespace(page_capture_id=capture_id, artifacts=artifacts)

    async def get_verified_capture(self, *, capture_id):
        return SimpleNamespace(capture=self.references[capture_id])


@pytest.mark.parametrize("with_receipt", [False, True])
async def test_coordinator_seals_outside_after_capture_without_completing_or_replaying(
    with_receipt,
):
    modes = []

    async def capture(request):
        modes.append(request.mode)
        if request.mode is PageCaptureMode.GOTO:
            await request._pre_action_validation("about:blank")
            return _page_result(OUTSIDE_URL, status_code=304, with_receipt=with_receipt)
        return _page_result(
            "about:blank" if len(modes) == 1 else OUTSIDE_URL, mode="observe", status_code=None
        )

    browser = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    scope_reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope(epoch=0)))
    buffer = InMemoryActionEvidenceBuffer()
    executor = _executor(browser, scope_reader, buffer)
    persistence = _CapturePersistence()
    ledger = RecordingActionExecutionRepository([])
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser, persistence=persistence, evidence_buffer=buffer
        ),
        interaction_executor=executor,
        evidence_exit_gate=NoopEvidenceExitGate(),
        action_execution_repository=ledger,
        action_recovery_captures=persistence,
        command_validator=InteractionCommandValidator(scope_reader=scope_reader),
    )
    request = _request()
    command = CaptureCoordinationCommand(agent_run_id=request.agent_run_id, intent=request.intent)

    result = await coordinator.coordinate(command)

    assert result.interaction.status is InteractionResultStatus.BLOCKED
    assert result.interaction.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert result.interaction.result_capture_id == result.after_capture.capture_id
    assert result.interaction.state_diff["driver_invoked"] is True
    assert ledger.state.status is ActionExecutionStatus.BLOCKED
    assert ledger.state.result_capture_id == result.after_capture.capture_id
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id
    after_result = persistence.saved[-1]["result"]
    assert after_result.final_url == OUTSIDE_URL
    assert after_result.artifacts[0].content["url"] == OUTSIDE_URL
    assert after_result.metrics["diagnostic_capture"] is True
    assert after_result.metrics["diagnostic_role"] == "post_navigation_out_of_scope"
    assert after_result.metrics["navigation_scope"]["verified"] is False
    assert after_result.metrics["postcondition_complete"] is False
    assert after_result.metrics["navigation_scope"]["requested_url"] == TARGET_URL
    assert after_result.metrics["navigation_scope"]["final_url"] == OUTSIDE_URL
    assert after_result.metrics["navigation_scope"]["main_response_status"] == 304
    har = next(item for item in after_result.artifacts if item.artifact_type == "har")
    assert har.content["entries"] == [
        {"event": "request", "url": TARGET_URL, "resource_type": "document"},
        {"event": "response", "url": OUTSIDE_URL, "status": 304},
    ]
    if with_receipt:
        assert after_result.action_trace[0]["argument_hash"] == "retained-cli-receipt"
        assert after_result.action_trace[0]["arrival_verified"] is False
    assert await buffer.get(action_execution_id=result.interaction.action_execution_id) is None
    # 证据落库后的恢复只读账本和同一份前后 Capture，不再进入浏览器。
    replay = await coordinator.coordinate(command)
    assert replay.interaction == result.interaction
    assert replay.after_capture == result.after_capture
    assert modes == [PageCaptureMode.OBSERVE, PageCaptureMode.GOTO, PageCaptureMode.OBSERVE]
