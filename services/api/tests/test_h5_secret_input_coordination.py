"""专用登录提交也经过统一账本；秘密未清空时绝不截图。"""

import json
from dataclasses import replace
from types import SimpleNamespace

from services.api.app.application.capture_coordinator import (
    CaptureCoordinator,
    InProcessSessionCommandGate,
)
from services.api.app.application.interaction_command_validation import InteractionCommandValidator
from services.api.app.application.interaction_runtime import InMemoryActionEvidenceBuffer
from services.api.app.application.web_evidence_exit_gate import NoopEvidenceExitGate
from services.api.app.domain.interaction import ActionExecutionStatus
from services.api.app.infrastructure.browser.h5_secret_input_coordination import (
    CoordinatedH5SecretInputRuntime,
)
from services.api.app.infrastructure.browser.h5_secret_input_runtime import (
    PlaywrightH5SecretInputRuntime,
)
from services.api.app.infrastructure.browser.page_capture import H5BrowserSecretFormResult
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.tests.test_capture_coordinator import (
    RecordingActionExecutionRepository,
    RecordingCaptureWriter,
)
from services.api.tests.test_h5_secret_input_runtime import (
    _PASSWORD_CANARY,
    _PHONE_CANARY,
    _command,
    _resolver,
    _SnapshotReader,
)


class _FreshReader:
    def __init__(self):
        self.capture_ids = []

    async def read_ui_snapshot(self, *, task_id, page_capture_id):
        self.capture_ids.append(page_capture_id)
        return await _SnapshotReader().read_ui_snapshot(
            task_id=task_id, page_capture_id="capture-auth"
        )


class _Browser:
    def __init__(self, events, *, submitted=True, scrubbed=True):
        self.events = events
        self.submitted = submitted
        self.scrubbed = scrubbed
        self.calls = 0

    async def submit_secret_form(self, **kwargs):
        self.calls += 1
        await kwargs["pre_action_validation"]("https://h5.example.com/login")
        assert kwargs["phone"] == _PHONE_CANARY
        assert kwargs["password"] == _PASSWORD_CANARY
        self.events.append("secret:submit")
        return H5BrowserSecretFormResult(
            submitted=self.submitted,
            fields_scrubbed=self.scrubbed,
            hostname="h5.example.com",
            reason_code="secret_form_submitted"
            if self.submitted
            else "secret_form_submit_no_effect",
        )


def _build(tmp_path, *, submitted=True, scrubbed=True):
    resolver, ref = _resolver(tmp_path)
    events = []
    writer = RecordingCaptureWriter(events)
    ledger = RecordingActionExecutionRepository(events)
    gate = InProcessSessionCommandGate()
    reader = _FreshReader()
    browser = _Browser(events, submitted=submitted, scrubbed=scrubbed)
    validator = InteractionCommandValidator()
    base = PlaywrightH5SecretInputRuntime(
        credential_resolver=resolver,
        snapshot_reader=reader,
        target_resolver=UiSnapshotSemanticTargetResolver(snapshot_reader=reader),
        browser=browser,
        session_command_gate=gate,
        command_validator=validator,
    )
    buffer = InMemoryActionEvidenceBuffer()

    def factory(capture_writer, executor):
        return CaptureCoordinator(
            capture_writer=capture_writer,
            interaction_executor=executor,
            session_command_gate=gate,
            command_validator=validator,
            action_execution_repository=ledger,
            evidence_exit_gate=NoopEvidenceExitGate(),
        )

    runtime = CoordinatedH5SecretInputRuntime(
        runtime=base,
        capture_writer=writer,
        evidence_buffer=buffer,
        coordinator_factory=factory,
    )
    return SimpleNamespace(
        runtime=runtime,
        base=base,
        command=replace(_command(ref), agent_run_id="run-auth"),
        events=events,
        ledger=ledger,
        writer=writer,
        reader=reader,
        browser=browser,
        buffer=buffer,
    )


async def test_secret_submission_uses_before_after_ledger_and_fresh_snapshot_without_secrets(
    tmp_path,
):
    case = _build(tmp_path)
    result = await case.runtime.execute(case.command)
    assert result.submitted is True
    assert result.after_capture.capture_id == "capture-after"
    assert result.action_execution_id == case.ledger.state.action_execution_id
    assert case.reader.capture_ids == ["capture-before"]
    assert case.events == [
        "action:planned",
        "capture:before_action",
        "action:executing",
        "secret:submit",
        "action:evidence_pending",
        "capture:after_action",
        "action:completed",
    ]
    assert case.ledger.state.status is ActionExecutionStatus.COMPLETED
    pending = await case.buffer.get(action_execution_id=result.action_execution_id)
    rendered = json.dumps(
        {
            "input": case.command.to_safe_dict(),
            "result": result.to_safe_dict(),
            "ledger": dict(case.ledger.state.result_payload),
            "action_artifacts": [artifact.content for artifact in pending.browser_result.artifacts],
        },
        ensure_ascii=False,
        default=str,
    )
    assert _PHONE_CANARY not in rendered
    assert _PASSWORD_CANARY not in rendered
    assert "#password" not in rendered
    assert case.ledger.state.result_payload["state_summary"]["authentication_verified"] is False
    from services.api.app.infrastructure.browser.capture_runtime import _merge_action_evidence
    from services.api.app.infrastructure.browser.page_capture import PageCaptureResult

    merged = _merge_action_evidence(
        capture_result=PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://h5.example.com/",
            title="页面",
            status_code=200,
        ),
        action_result=pending.browser_result,
        required_types={"action_trace"},
    )
    assert merged.action_trace[0]["action"] == "credential_submit"
    assert merged.artifacts[0].content == merged.action_trace


async def test_unclean_secret_window_records_failure_without_after_capture(tmp_path):
    case = _build(tmp_path, scrubbed=False)
    result = await case.runtime.execute(case.command)
    assert result.submitted is False
    assert result.fields_scrubbed is False
    assert "capture:after_action" not in case.events
    assert case.ledger.state.status is ActionExecutionStatus.FAILED
    assert case.ledger.state.result_capture_id is None


async def test_scrubbed_failed_submission_keeps_failure_and_after_evidence(tmp_path):
    case = _build(tmp_path, submitted=False)
    result = await case.runtime.execute(case.command)
    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.after_capture.capture_id == "capture-after"
    assert case.ledger.state.status is ActionExecutionStatus.FAILED
    assert case.ledger.state.result_capture_id == "capture-after"
    assert case.events[-1] == "action:terminal_evidence"


async def test_production_secret_base_cannot_bypass_coordinator(tmp_path):
    case = _build(tmp_path)
    result = await case.base.execute(case.command)
    assert result.reason_code == "secret_input_coordination_required"
    assert case.browser.calls == 0


async def test_secret_submission_requires_run_identity(tmp_path):
    case = _build(tmp_path)
    result = await case.runtime.execute(replace(case.command, agent_run_id=None))
    assert result.reason_code == "secret_input_run_required"
    assert case.events == []
    assert case.browser.calls == 0


async def test_real_submit_control_risk_is_rechecked_before_unlocking_credentials(tmp_path):
    case = _build(tmp_path)
    read_snapshot = case.reader.read_ui_snapshot

    async def payment_control(**kwargs):
        snapshot = await read_snapshot(**kwargs)
        snapshot["elements"][-1]["aria_label"] = "确认付款"
        return snapshot

    case.reader.read_ui_snapshot = payment_control
    result = await case.runtime.execute(case.command)
    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert case.browser.calls == 0
    assert case.ledger.state.status is ActionExecutionStatus.FAILED
    assert case.ledger.state.result_capture_id == "capture-after"


def test_production_secret_wrapper_shares_scope_lease_and_persistent_ledger(monkeypatch):
    from services.api.tests.test_browser_command_scope import _production_runtime

    web = _production_runtime(monkeypatch)
    base = SimpleNamespace()
    wrapped = web.coordinate_secret_input_runtime(base)
    assert base._command_validator is web._command_validator
    coordinator = wrapped._coordinator_factory(object(), object())
    assert coordinator._session_command_gate is web.session_command_gate
    assert coordinator._action_executions is web._action_executions
    assert coordinator._action_recovery_captures is web._capture_recovery
    assert coordinator._command_validator is web._command_validator
