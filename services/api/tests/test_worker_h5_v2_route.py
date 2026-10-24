"""Worker 对 H5 V2 的专用分流测试。"""

from datetime import UTC, datetime

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.models import ExecutionOutcome, RunResult, TerminationReason
from services.api.app.application.app_workflow_router import WorkflowRoute
from services.api.app.application.rule_judge import RuleEngineConfigurationError
from services.api.app.core.config import Settings
from services.api.app.domain.task import TaskExecutionMode
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


class RecordingRuleEngineConfigurationGuard:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[TaskExecutionMode] = []

    async def ensure_ready_for(self, execution_mode: TaskExecutionMode) -> None:
        self.calls.append(execution_mode)
        if self.fail:
            raise RuleEngineConfigurationError("规则配置不可执行")


def _h5_v2_record(
    mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION,
) -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-h5-v2",
        task_id="task-h5-v2",
        message="探索 H5 页面",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "execution_mode": mode.value,
            "_task_target": {
                "platform": "h5",
                "target_url": "https://m.example.test/home",
            },
        },
    )


@pytest.mark.parametrize(
    "summary",
    [
        {"overall_status": "manual_review"},
        {"overall_status": "pass", "business_completion_status": "review_required"},
    ],
)
def test_h5_v2_report_requiring_review_maps_to_review_terminal(summary) -> None:
    record = _h5_v2_record()
    record.status = AgentRunQueueStatus.COMPLETED
    record.execution_outcome = ExecutionOutcome.SUCCESS.value
    record.final_report = {"summary": summary}

    assert (
        worker_module._task_execution_status(record)
        is worker_module.TaskExecutionStatus.NEEDS_REVIEW
    )


def test_h5_v2_completed_report_keeps_completed_terminal() -> None:
    record = _h5_v2_record()
    record.status = AgentRunQueueStatus.COMPLETED
    record.execution_outcome = ExecutionOutcome.SUCCESS.value
    record.final_report = {
        "summary": {
            "overall_status": "pass",
            "business_completion_status": "complete",
        }
    }

    assert (
        worker_module._task_execution_status(record)
        is worker_module.TaskExecutionStatus.COMPLETED
    )


def test_web_report_requiring_review_is_not_changed_by_h5_mapping() -> None:
    record = _h5_v2_record()
    record.task_config["_task_target"]["platform"] = "web"
    record.status = AgentRunQueueStatus.COMPLETED
    record.execution_outcome = ExecutionOutcome.SUCCESS.value
    record.final_report = {
        "summary": {
            "overall_status": "manual_review",
            "business_completion_status": "review_required",
        }
    }

    assert (
        worker_module._task_execution_status(record)
        is worker_module.TaskExecutionStatus.COMPLETED
    )


def test_h5_review_gate_updates_report_terminal_projection() -> None:
    record = _h5_v2_record()
    record.status = AgentRunQueueStatus.COMPLETED
    record.final_report = {
        "summary": {
            "overall_status": "incomplete",
            "business_completion_status": "incomplete",
        },
        "page_exploration": {
            "complete": True,
            "quality_status": "complete",
            "requires_review": False,
            "terminal_reason": "completion_gate_pending",
            "h5_coverage": {},
        },
    }

    worker_module._attach_completion_gate(
        record,
        worker_module._WorkerFinalizationDecision(
            gate_passed=False,
            gate_snapshot_id="gate-review",
            finalization_epoch=2,
            block_reasons=("review_policy_unsatisfied",),
        ),
    )

    exploration = record.final_report["page_exploration"]
    assert exploration["complete"] is False
    assert exploration["quality_status"] == "needs_review"
    assert exploration["requires_review"] is True
    assert exploration["terminal_reason"] == "review_policy_unsatisfied"
    assert record.final_report["summary"]["overall_status"] == "manual_review"
    assert (
        record.final_report["summary"]["business_completion_status"]
        == "review_required"
    )
    assert record.final_report["report_stage"] == "final_review"
    assert record.final_report["report_availability"] == "preview"
    assert record.final_report["is_final"] is False
    assert record.final_report["export_allowed"] is False
    assert record.final_report["export_ready"] is False
    assert record.final_report["incomplete_reasons"] == ["H5_REPORT_NOT_FINALIZED"]


def test_h5_technical_gate_block_does_not_become_review_terminal() -> None:
    record = _h5_v2_record()
    record.status = AgentRunQueueStatus.FAILED
    record.final_report = {
        "summary": {
            "overall_status": "incomplete",
            "business_completion_status": "incomplete",
        },
        "page_exploration": {
            "complete": False,
            "quality_status": "technical_failed",
            "requires_review": False,
            "terminal_reason": "inspection_failed",
            "h5_coverage": {},
        },
    }

    worker_module._attach_completion_gate(
        record,
        worker_module._WorkerFinalizationDecision(
            gate_passed=False,
            gate_snapshot_id="gate-failed",
            finalization_epoch=2,
            block_reasons=("background_execution",),
        ),
    )

    exploration = record.final_report["page_exploration"]
    assert exploration["quality_status"] == "technical_failed"
    assert exploration["terminal_reason"] == "inspection_failed"
    assert record.final_report["summary"]["overall_status"] == "incomplete"
    assert record.final_report["report_stage"] == "final_review"
    assert record.final_report["is_final"] is False


def test_h5_review_gate_preserves_specific_page_terminal_reason() -> None:
    record = _h5_v2_record()
    record.status = AgentRunQueueStatus.COMPLETED
    record.final_report = {
        "summary": {
            "overall_status": "incomplete",
            "business_completion_status": "incomplete",
        },
        "page_exploration": {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "authentication_required",
            "h5_coverage": {},
        },
    }

    worker_module._attach_completion_gate(
        record,
        worker_module._WorkerFinalizationDecision(
            gate_passed=False,
            gate_snapshot_id="gate-coverage-review",
            finalization_epoch=2,
            block_reasons=("h5_coverage_scope_incomplete",),
        ),
    )

    exploration = record.final_report["page_exploration"]
    assert exploration["terminal_reason"] == "authentication_required"
    assert record.final_report["summary"]["overall_status"] == "manual_review"


def test_h5_failed_run_is_not_rewritten_as_review_by_gate_reason() -> None:
    record = _h5_v2_record()
    record.status = AgentRunQueueStatus.FAILED
    record.final_report = {
        "summary": {
            "overall_status": "incomplete",
            "business_completion_status": "incomplete",
        },
        "page_exploration": {
            "complete": False,
            "quality_status": "technical_failed",
            "requires_review": False,
            "terminal_reason": "work_failed",
            "h5_coverage": {},
        },
    }

    worker_module._attach_completion_gate(
        record,
        worker_module._WorkerFinalizationDecision(
            gate_passed=False,
            gate_snapshot_id="gate-review",
            finalization_epoch=2,
            block_reasons=("review_policy_unsatisfied",),
        ),
    )

    assert record.final_report["summary"]["overall_status"] == "incomplete"
    assert record.final_report["page_exploration"]["quality_status"] == "technical_failed"
    assert record.final_report["report_stage"] == "final_review"


def test_web_blocked_gate_keeps_preview_report() -> None:
    record = _h5_v2_record()
    record.task_config["_task_target"]["platform"] = "web"
    record.status = AgentRunQueueStatus.COMPLETED
    record.final_report = {"summary": {"overall_status": "incomplete"}}

    worker_module._attach_completion_gate(
        record,
        worker_module._WorkerFinalizationDecision(
            gate_passed=False,
            gate_snapshot_id="gate-web",
            finalization_epoch=2,
            block_reasons=("review_policy_unsatisfied",),
        ),
    )

    assert record.final_report["report_stage"] == "preview"
    assert record.final_report["is_final"] is False
    assert "report_availability" not in record.final_report


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution_mode",
    [TaskExecutionMode.FULL_INSPECTION, TaskExecutionMode.EXPLORATION_ONLY],
)
async def test_worker_routes_h5_v2_after_checking_exact_execution_mode(
    monkeypatch,
    execution_mode: TaskExecutionMode,
) -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    record = await queue.enqueue(
        "探索 H5 页面",
        task_id="task-h5-v2",
        task_config=_h5_v2_record(execution_mode).task_config,
    )
    state_store = InMemoryAgentRunStateStore()
    routed: list[tuple[WorkflowRoute, object]] = []
    evidence_handoff = object()
    guard = RecordingRuleEngineConfigurationGuard()

    def _unexpected_main_agent(*_args, **_kwargs):
        raise AssertionError("H5 V2 Run 不得构造通用 Main Agent")

    async def _capture_h5(*_args, dispatcher, h5_evidence_handoff, **_kwargs):
        assert guard.calls == [execution_mode]
        routed.append((dispatcher.route(record, settings), h5_evidence_handoff))

    monkeypatch.setattr(worker_module, "build_default_main_agent", _unexpected_main_agent)
    monkeypatch.setattr(
        worker_module,
        "_build_default_h5_evidence_handoff",
        lambda: evidence_handoff,
    )
    monkeypatch.setattr(worker_module, "_process_h5_v2_one", _capture_h5, raising=False)
    settings = Settings.model_validate({"workflow": {"h5_v2_mode": "on"}})

    await worker_module._process_one(
        queue,
        record.run_id,
        settings,
        state_store=state_store,
        h5_workflow_handler=object(),
        rule_engine_configuration_guard=guard,
    )

    assert guard.calls == [execution_mode]
    assert routed == [(WorkflowRoute.H5_V2, evidence_handoff)]


@pytest.mark.asyncio
async def test_worker_rejects_h5_before_runtime_when_rule_engine_guard_fails(
    monkeypatch,
) -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    queued = await queue.enqueue(
        "探索 H5 页面",
        task_id="task-h5-v2",
        task_config=_h5_v2_record().task_config,
    )
    guard = RecordingRuleEngineConfigurationGuard(fail=True)
    h5_called = False
    terminal_records: list[AgentRunRecord] = []

    async def _unexpected_h5(*_args, **_kwargs) -> None:
        nonlocal h5_called
        h5_called = True

    async def _capture_terminal_record(*, record, **_kwargs) -> None:
        terminal_records.append(record)

    monkeypatch.setattr(worker_module, "_process_h5_v2_one", _unexpected_h5)
    monkeypatch.setattr(
        worker_module,
        "_finish_standard_terminal_run",
        _capture_terminal_record,
    )

    await worker_module._process_one(
        queue,
        queued.run_id,
        Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        state_store=InMemoryAgentRunStateStore(),
        h5_workflow_handler=object(),
        h5_evidence_handoff=object(),
        rule_engine_configuration_guard=guard,
    )

    assert guard.calls == [TaskExecutionMode.FULL_INSPECTION]
    assert h5_called is False
    assert len(terminal_records) == 1
    assert terminal_records[0].status is AgentRunQueueStatus.FAILED


@pytest.mark.asyncio
async def test_worker_loops_share_one_rule_engine_configuration_guard(monkeypatch) -> None:
    guard = RecordingRuleEngineConfigurationGuard()
    injected_guards: list[object] = []

    async def _capture_worker_loop(
        *_args,
        rule_engine_configuration_guard=None,
        **_kwargs,
    ) -> None:
        injected_guards.append(rule_engine_configuration_guard)

    monkeypatch.setattr(worker_module, "_worker_loop", _capture_worker_loop)
    tasks = worker_module._start_worker_loops(
        concurrency=2,
        queue=object(),
        settings=Settings(),
        stop_event=worker_module.asyncio.Event(),
        state_store=InMemoryAgentRunStateStore(),
        worker_instance_id="worker-1",
        app_workflow_handler=object(),
        sealed_inspection_publisher=object(),
        rule_engine_configuration_guard=guard,
    )
    await worker_module.asyncio.gather(*tasks)

    assert injected_guards == [guard, guard]


@pytest.mark.asyncio
async def test_worker_rejects_invalid_h5_policy_before_dispatch(monkeypatch) -> None:
    """若策略解析在分派之后，非法 H5 Run 仍可能创建浏览器会话。"""
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    config = _h5_v2_record().task_config
    config["scan_mode"] = "unsupported"
    record = await queue.enqueue(
        "探索 H5 页面",
        task_id="task-h5-v2",
        task_config=config,
    )
    dispatched = False

    class Dispatcher:
        async def dispatch_h5_v2(self, **_kwargs):
            nonlocal dispatched
            dispatched = True
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=None,
                trace_sink=None,
                execution_outcome=ExecutionOutcome.SUCCESS,
            )

    async def finish_terminal_run(**_kwargs):
        return None

    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **_kwargs: None)
    monkeypatch.setattr(worker_module, "_finish_terminal_run", finish_terminal_run)
    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=Dispatcher(),
    )

    assert dispatched is False
    assert record.error_message is not None
    assert "scan_mode" in record.error_message
