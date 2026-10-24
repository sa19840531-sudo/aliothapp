"""Main Agent page_capture → ActionIntent → CaptureCoordinator 桥接单元测试。"""

from __future__ import annotations

import pytest

from services.api.app.application.capture_coordination import ExecuteInteractionRequest
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.page_capture_persistence import (
    PersistedEvidenceArtifact,
    PersistedPageCapture,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.application.web_agent_page_capture import (
    AgentPageCaptureIntentError,
    AgentPageCaptureSpec,
    FixedResolvedTargetResolver,
    build_action_intent_for_agent_page_capture,
    build_resolved_target_for_agent_page_capture,
    execute_agent_page_capture_with_browser,
)
from services.api.app.domain.interaction import (
    CommandPolicy,
    InteractionOperation,
    InteractionResultStatus,
)
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserInteractionExecutor,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
)


class RecordingBrowser:
    def __init__(self, *, artifacts: list[PageCaptureArtifact] | None = None) -> None:
        self.requests: list[PageCaptureRequest] = []
        self._artifacts = list(artifacts or [])

    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        self.requests.append(request)
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=request.url or "https://example.com/",
            title="ok",
            status_code=200,
            artifacts=list(self._artifacts),
            console_logs=[],
            network_requests=[],
            action_trace=[{"action": request.mode.value}],
            metrics={},
            error_message=None,
        )

    async def close_session(self, session_id: str) -> None:
        del session_id


class SnapshotReader:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str]] = []

    async def read_ui_snapshot(self, *, task_id: str, page_capture_id: str):
        self.calls.append((task_id, page_capture_id))
        return self.snapshot


async def _execute_snapshot_bound_spec(
    *,
    spec: AgentPageCaptureSpec,
    snapshot: dict[str, object],
) -> tuple[object, RecordingBrowser, SnapshotReader]:
    browser = RecordingBrowser()
    reader = SnapshotReader(snapshot)
    target = build_resolved_target_for_agent_page_capture(spec)
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=FixedResolvedTargetResolver(
            target,
            snapshot_reader=reader,
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        command_validator=InteractionCommandValidator(policy=CommandPolicy.default()),
    )
    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id=spec.agent_run_id,
            intent=build_action_intent_for_agent_page_capture(spec),
            source_capture_id="before-1",
            action_execution_id="action-1",
        )
    )
    return result, browser, reader


def test_goto_maps_to_navigate_intent_without_locator_parameters() -> None:
    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.GOTO,
        url="https://example.com/home",
    )
    intent = build_action_intent_for_agent_page_capture(spec)
    target = build_resolved_target_for_agent_page_capture(spec)

    assert intent.action_type is InteractionOperation.NAVIGATE
    assert intent.parameters["destination_url"] == "https://example.com/home"
    assert intent.parameters.get("coordinator_dual_capture") is True
    assert "selector" not in intent.parameters
    assert target.destination_url == "https://example.com/home"
    assert target.selector is None


def test_click_keeps_selector_only_in_resolved_target() -> None:
    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action="click",
        selector="text=关闭",
        clear_obstructions=True,
        source_capture_id="capture-parent",
    )
    intent = build_action_intent_for_agent_page_capture(spec)
    target = build_resolved_target_for_agent_page_capture(spec)

    assert intent.action_type is InteractionOperation.CLICK
    assert intent.source_capture_id == "capture-parent"
    assert "selector" not in intent.parameters
    assert intent.parameters.get("clear_obstructions") is True
    assert target.selector == "text=关闭"
    assert target.clear_obstructions is True


@pytest.mark.asyncio
async def test_fixed_target_binds_actual_semantics_from_before_snapshot() -> None:
    class SnapshotReader:
        async def read_ui_snapshot(self, **_kwargs):
            return {
                "elements": [
                    {
                        "selector": "#pay",
                        "tag": "button",
                        "role": "button",
                        "text": "立即支付",
                    },
                    {
                        "selector": "#password",
                        "tag": "input",
                        "placeholder": "支付密码",
                    },
                ]
            }

    target = await FixedResolvedTargetResolver(
        ResolvedInteractionTarget(selector="#pay"),
        snapshot_reader=SnapshotReader(),
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="agent-requested-interaction",
            parameters={},
        )
    )

    assert target.target_trust == "dom_snapshot"
    assert "立即支付" in target.trusted_target_texts
    assert target.trusted_target_role == "button"


@pytest.mark.asyncio
async def test_fixed_target_rejects_duplicate_snapshot_selector() -> None:
    class SnapshotReader:
        async def read_ui_snapshot(self, **_kwargs):
            return {
                "elements": [
                    {"selector": "#close", "tag": "button", "text": "关闭"},
                    {"selector": "#close", "tag": "button", "text": "关闭"},
                ]
            }

    with pytest.raises(SemanticTargetResolutionError) as error:
        await FixedResolvedTargetResolver(
            ResolvedInteractionTarget(selector="#close"),
            snapshot_reader=SnapshotReader(),
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="agent-requested-interaction",
                parameters={},
            )
        )

    assert error.value.error_code == "SEMANTIC_TARGET_AMBIGUOUS"


@pytest.mark.asyncio
async def test_agent_payment_selector_is_rejected_before_browser_call() -> None:
    result, browser, _reader = await _execute_snapshot_bound_spec(
        spec=AgentPageCaptureSpec(
            task_id="task-1",
            session_id="session-1",
            agent_run_id="run-1",
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="click",
            selector="#pay",
        ),
        snapshot={
            "elements": [
                {
                    "selector": "#pay",
                    "tag": "button",
                    "role": "button",
                    "text": "立即支付",
                }
            ]
        },
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert browser.requests == []


@pytest.mark.asyncio
async def test_agent_input_and_submit_selectors_are_both_bound_to_snapshot() -> None:
    result, browser, reader = await _execute_snapshot_bound_spec(
        spec=AgentPageCaptureSpec(
            task_id="task-1",
            session_id="session-1",
            agent_run_id="run-1",
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="submit_probe",
            selector="#phone",
            submit_selector="#pay",
            probe_text="ALIOTH_RATE_PROBE_run_001",
        ),
        snapshot={
            "elements": [
                {
                    "selector": "#phone",
                    "tag": "input",
                    "placeholder": "手机号",
                },
                {
                    "selector": "#pay",
                    "tag": "button",
                    "role": "button",
                    "text": "确认支付",
                },
            ]
        },
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert browser.requests == []
    assert reader.calls == [("task-1", "before-1")]


@pytest.mark.asyncio
async def test_agent_selector_missing_from_latest_snapshot_requires_reread() -> None:
    with pytest.raises(SemanticTargetResolutionError) as error:
        await FixedResolvedTargetResolver(
            ResolvedInteractionTarget(selector="#close"),
            snapshot_reader=SnapshotReader(
                {"elements": [{"selector": "#other", "tag": "button"}]}
            ),
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="before-2",
                action_type=InteractionOperation.CLICK,
                semantic_target="agent-requested-interaction",
                parameters={},
            )
        )

    assert error.value.error_code == "SEMANTIC_TARGET_NOT_FOUND"
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_agent_unique_visible_button_is_allowed_to_reach_browser() -> None:
    result, browser, _reader = await _execute_snapshot_bound_spec(
        spec=AgentPageCaptureSpec(
            task_id="task-1",
            session_id="session-1",
            agent_run_id="run-1",
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="click",
            selector="#close",
        ),
        snapshot={
            "elements": [
                {
                    "selector": "#close",
                    "tag": "button",
                    "role": "button",
                    "text": "关闭",
                }
            ]
        },
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert len(browser.requests) == 1
    assert browser.requests[0].selector == "#close"


@pytest.mark.asyncio
async def test_coordinate_agent_page_capture_uses_before_action_after() -> None:
    browser = RecordingBrowser()
    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.OBSERVE,
    )

    outcome = await execute_agent_page_capture_with_browser(browser=browser, spec=spec)

    assert outcome.action_browser_result.execution_status == "success"
    assert outcome.before_capture_id
    assert outcome.after_capture_id
    assert outcome.before_capture_id != outcome.after_capture_id
    assert outcome.persisted.page_capture_id == outcome.after_capture_id
    # 入口合成 before，浏览器只执行动作阶段一次
    assert len(browser.requests) == 1
    assert browser.requests[0].mode is PageCaptureMode.OBSERVE
    assert browser.requests[0].session_id == "session-1"


@pytest.mark.asyncio
async def test_coordinate_agent_page_capture_uses_server_scope_before_browser() -> None:
    browser = RecordingBrowser()

    class ScopeReader:
        async def read_command_scope(self, **_kwargs):
            return None

    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.OBSERVE,
    )

    with pytest.raises(AgentPageCaptureIntentError) as error:
        await execute_agent_page_capture_with_browser(
            browser=browser,
            spec=spec,
            command_scope_reader=ScopeReader(),
        )

    assert error.value.error_code == "COMMAND_SCOPE_UNAVAILABLE"
    assert browser.requests == []


@pytest.mark.asyncio
async def test_supplemental_page_capture_is_blocked_before_browser() -> None:
    browser = RecordingBrowser()
    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action="submit_probe",
        selector="textarea.comment",
        submit_selector="button.submit",
        probe_text="ALIOTH_RATE_PROBE_run_001",
    )

    with pytest.raises(AgentPageCaptureIntentError) as error:
        await execute_agent_page_capture_with_browser(browser=browser, spec=spec)

    assert error.value.error_code == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    assert browser.requests == []


@pytest.mark.asyncio
async def test_supplemental_page_capture_contract_can_be_enabled_explicitly() -> None:
    browser = RecordingBrowser()
    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action="network_fault",
        fault_url_pattern=r"ads\.example\.com/",
    )

    outcome = await execute_agent_page_capture_with_browser(
        browser=browser,
        spec=spec,
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    )

    assert outcome.action_browser_result.execution_status == "success"
    assert len(browser.requests) == 1
    assert browser.requests[0].trigger_action == "network_fault"


class TimeoutThenSuccessPersistence:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def persist(
        self,
        *,
        result: PageCaptureResult,
        **kwargs,
    ) -> PersistedPageCapture:
        del kwargs
        artifact_types = [item.artifact_type for item in result.artifacts]
        self.calls.append(artifact_types)
        if len(self.calls) == 1:
            raise TimeoutError("simulated persistence timeout")
        return PersistedPageCapture(
            page_capture_id=f"capture-{len(self.calls)}",
            page_seq=len(self.calls),
            artifacts=[
                PersistedEvidenceArtifact(
                    artifact_id=f"artifact-{index}",
                    artifact_type=artifact_type,
                    storage_path=f"mem://capture/{artifact_type}",
                    content_type="application/octet-stream",
                    checksum="checksum",
                    size_bytes=1,
                    metadata={},
                )
                for index, artifact_type in enumerate(artifact_types, start=1)
            ],
        )


@pytest.mark.asyncio
async def test_after_capture_retries_compacted_artifacts_on_persistence_timeout() -> None:
    browser = RecordingBrowser(
        artifacts=[
            PageCaptureArtifact(
                artifact_type="dom",
                artifact_id=None,
                content="<html></html>",
            ),
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={"encoding": "base64", "data": "x"},
            ),
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={"encoding": "base64", "data": "extra-frame"},
            ),
            PageCaptureArtifact(
                artifact_type="visible_text",
                artifact_id=None,
                content="可见文本",
            ),
            PageCaptureArtifact(
                artifact_type="ui_snapshot",
                artifact_id=None,
                content={"elements": []},
            ),
        ]
    )
    persistence = TimeoutThenSuccessPersistence()
    spec = AgentPageCaptureSpec(
        task_id="task-1",
        session_id="session-1",
        agent_run_id="run-1",
        mode=PageCaptureMode.OBSERVE,
        persistence_timeout_seconds=0.01,
    )

    outcome = await execute_agent_page_capture_with_browser(
        browser=browser,
        spec=spec,
        persistence_service=persistence,
    )

    assert outcome.after_capture_id == "capture-2"
    assert persistence.calls[0] == [
        "dom",
        "screenshot",
        "screenshot",
        "visible_text",
        "ui_snapshot",
        "action_trace",
    ]
    assert persistence.calls[1] == [
        "screenshot",
        "visible_text",
        "ui_snapshot",
        "action_trace",
    ]
