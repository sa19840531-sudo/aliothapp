"""V1 Worker 内 Snapshot 工作项的 Judge 与 Result Writer 边界测试。"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.agents.runtime.inspection_work_item_consumer import (
    InspectionWorkItemConsumer,
)
from services.api.app.agents.runtime.run_context import get_agent_run_context
from services.api.app.agents.runtime.usage_meter import (
    bind_run_llm_usage_meter,
    record_current_llm_usage,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemConflictError,
    InspectionWorkItemExecutionLink,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemSyncResult,
)
from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RuleDecision,
    RuleJudgeOutcome,
    build_evidence_request,
)


class FakeService:
    def __init__(
        self,
        events: list[str],
        *,
        expected_session_dependency: str | None = "snapshot",
        recoverable_result_payload: dict | None = None,
        target_platform: str | None = None,
    ) -> None:
        self.events = events
        self.expected_session_dependency = expected_session_dependency
        self.recoverable_result_payload = recoverable_result_payload
        self.target_platform = target_platform
        self.claim_count = 0
        self.attempt_transitions = []
        self.rule_transitions = []
        self.transitions = []
        self.row_version = 1
        self.sync_count = 0
        self.raise_conflict_on_judged_attempt = False

    async def claim(self, command):
        self.claim_count += 1
        if self.claim_count > 1:
            return None
        assert command.session_dependency == self.expected_session_dependency
        lease_dependency = self.expected_session_dependency or "active_session"
        return InspectionWorkItemLease(
            work_item_id="work-1",
            task_id=command.task_id,
            page_capture_id="capture-1",
            batch_key="hybrid:visual:snapshot",
            strategy="hybrid",
            category="visual",
            session_dependency=lease_dependency,
            input_hash="input-1",
            producer_agent_run_id="producer-run-1",
            consumer_agent_run_id=command.consumer_agent_run_id,
            lease_token="lease-1",
            attempt_no=1,
            row_version=1,
            rules=(
                InspectionWorkItemRule(
                    rule_id="rule-1",
                    rule_code="alioth_1",
                    ordinal=0,
                    contract_hash="contract-1",
                ),
            ),
            recoverable_result_payload=self.recoverable_result_payload,
            target_platform=self.target_platform,
        )

    async def transition(self, command):
        self.transitions.append(command)
        self.row_version += 1
        return self.row_version

    async def start_attempt(self, command):
        return "attempt-1"

    async def transition_attempt(self, command):
        self.attempt_transitions.append(command)
        if (
            self.raise_conflict_on_judged_attempt
            and command.target_status.value == "judged"
        ):
            raise InspectionWorkItemConflictError("判定尝试状态已变化")
        if command.target_status.value == "judged":
            self.events.append("persist_judgement")

    async def transition_rule(self, command):
        self.rule_transitions.append(command)

    async def sync_formal_results(self, **kwargs):
        self.sync_count += 1
        if self.sync_count == 1:
            return InspectionWorkItemSyncResult(
                work_item_id="work-1",
                expected_rule_count=1,
                terminal_rule_count=0,
                pending_rule_count=1,
                written_rule_count=0,
                links=(),
            )
        return InspectionWorkItemSyncResult(
            work_item_id="work-1",
            expected_rule_count=1,
            terminal_rule_count=1,
            pending_rule_count=0,
            written_rule_count=1,
            links=(
                InspectionWorkItemExecutionLink(
                    rule_id="rule-1",
                    rule_code="alioth_1",
                    execution_status="pass",
                    observation_packet_id="packet-1",
                    rule_execution_id="execution-1",
                ),
            ),
        )

    async def renew_lease(self, command):
        self.row_version += 1
        return self.row_version


class FakeJudge:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.handoffs = []

    async def judge(self, handoff):
        self.events.append("judge")
        self.handoffs.append(handoff)
        return RuleJudgeOutcome(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            decisions=(
                RuleDecision(
                    work_item_id=handoff.work_item_id,
                    attempt_id=handoff.attempt_id,
                    rule_id=handoff.rules[0].rule_id,
                    rule_code=handoff.rules[0].rule_code,
                    execution_status="pass",
                    observation_packet_id="packet-1",
                ),
            ),
        )


class UsageRecordingJudge(FakeJudge):
    async def judge(self, handoff):
        record_current_llm_usage(
            {
                "input_token_count": 21,
                "output_token_count": 9,
                "total_token_count": 30,
            }
        )
        return await super().judge(handoff)


class FakeEvidenceRequestJudge:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def judge(self, handoff):
        self.events.append("judge")
        request = build_evidence_request(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            rule_id=handoff.rules[0].rule_id,
            rule_code=handoff.rules[0].rule_code,
            target_capture_id=handoff.evidence.page_capture_id,
            reason_code="OBSERVATION_PACKET_INCOMPLETE",
            evidence_goal="缺少动作后的截图",
            required_artifact_types=("screenshot",),
            session_dependency=EvidenceSessionDependency.SNAPSHOT,
            replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
            required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
            target_invariants={"business": {"route": "/home"}},
            minimum_source_grade=EvidenceSourceGrade.B,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return RuleJudgeOutcome(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            evidence_requests=(request,),
        )


class FakeResultWriter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = []

    async def write(self, *, handoff, outcome):
        self.events.append("write")
        self.calls.append((handoff, outcome))
        return {
            "execution_status": "pass",
            "rule_code": handoff.rules[0].rule_code,
            "execution_id": "execution-1",
        }


class ContextAssertingResultWriter(FakeResultWriter):
    async def write(self, *, handoff, outcome):
        context = get_agent_run_context()
        assert context is not None
        assert context.task_id == handoff.task_id
        assert context.agent_run_id == "run-1"
        assert context.work_item_id == handoff.work_item_id
        assert context.attempt_id == handoff.attempt_id
        assert context.lease_token == "lease-1"
        return await super().write(handoff=handoff, outcome=outcome)


async def test_snapshot_consumer_persists_judgement_before_result_write() -> None:
    events: list[str] = []
    service = FakeService(events)
    judge = FakeJudge(events)
    writer = FakeResultWriter(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=judge,
        result_writer=writer,
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.claimed_count == 1
    assert result.completed_count == 1
    assert events == ["judge", "persist_judgement", "write"]
    assert [command.target_status.value for command in service.attempt_transitions] == [
        "judged",
        "written",
    ]
    assert service.rule_transitions[0].target_status.value == "judged"
    assert service.transitions[-1].target_status.value == "succeeded"


@pytest.mark.parametrize("target_platform", ["h5", "web", "app"])
async def test_late_h5_judgement_settles_closed_session_before_waiting_evidence(
    target_platform: str,
) -> None:
    events: list[str] = []
    service = FakeService(events, target_platform=target_platform)

    class Settlement:
        async def settle_requests_after_session_close(self, *, task_id, agent_run_id):
            assert task_id == "task-1"
            assert agent_run_id == "producer-run-1"
            assert service.transitions[-1].target_status.value == "running"
            events.append("settle_closed_session")
            return 1

    async def reopen_waiting_evidence(*, task_id):
        assert task_id == "task-1"
        assert service.transitions[-1].target_status.value == "succeeded"
        events.append("reopen_waiting_evidence")
        return 0

    service.reopen_waiting_evidence = reopen_waiting_evidence
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=FakeResultWriter(events),
        h5_closed_session_settlement=Settlement(),
    )
    result = await consumer.drain_snapshot(
        task_id="task-1", consumer_agent_run_id="run-1",
    )
    assert result.completed_count == 1
    assert events == ["judge", "persist_judgement", "write"] + (
        ["settle_closed_session", "reopen_waiting_evidence"]
        if target_platform == "h5" else []
    )


async def test_late_h5_request_is_settled_even_when_attempt_returns_waiting_evidence() -> None:
    events: list[str] = []

    class Service(FakeService):
        async def sync_formal_results(self, **kwargs):
            return InspectionWorkItemSyncResult(
                work_item_id="work-1", expected_rule_count=1, terminal_rule_count=0,
                pending_rule_count=1, written_rule_count=0, links=(),
            )

        async def reopen_waiting_evidence(self, *, task_id):
            assert task_id == "task-1"
            assert self.transitions[-1].target_status.value == "waiting_evidence"
            events.append("reopen")
            return 0

    class Writer(FakeResultWriter):
        async def write(self, *, handoff, outcome):
            events.append("write")
            return {
                "execution_status": "evidence_requested",
                "evidence_requests": [item.to_dict() for item in outcome.evidence_requests],
                "admitted_evidence_request_ids": [
                    item.request_id for item in outcome.evidence_requests
                ],
            }

    class Settlement:
        async def settle_requests_after_session_close(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-1", "producer-run-1")
            events.append("settle")
            return 1

    consumer = InspectionWorkItemConsumer(
        service=Service(events, target_platform="h5"), lease_owner="worker-1",
        lease_seconds=180, retry_backoff_seconds=2,
        judge=FakeEvidenceRequestJudge(events), result_writer=Writer(events),
        h5_closed_session_settlement=Settlement(),
        supplemental_evidence_policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
    )
    result = await consumer.drain_snapshot(task_id="task-1", consumer_agent_run_id="run-1")
    assert result.completed_count == 0
    assert events == ["judge", "persist_judgement", "write", "settle", "reopen"]


@pytest.mark.parametrize("target_platform", ["h5", "web", "app"])
async def test_h5_uses_its_request_admitting_writer_only(target_platform: str) -> None:
    events: list[str] = []
    service = FakeService(events, target_platform=target_platform)
    default_writer = FakeResultWriter(events)
    h5_writer = FakeResultWriter(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=default_writer,
        h5_result_writer=h5_writer,
    )

    await consumer.drain_snapshot(task_id="task-1", consumer_agent_run_id="run-1")

    assert len(h5_writer.calls) == (1 if target_platform == "h5" else 0)
    assert len(default_writer.calls) == (0 if target_platform == "h5" else 1)


async def test_snapshot_consumer_finalizes_while_lease_heartbeat_is_active(
    monkeypatch,
) -> None:
    events: list[str] = []
    service = FakeService(events)
    heartbeat_active = False

    @asynccontextmanager
    async def tracked_heartbeat(_attempt):
        nonlocal heartbeat_active
        heartbeat_active = True
        try:
            yield
        finally:
            heartbeat_active = False

    original_sync = service.sync_formal_results

    async def sync_formal_results(**kwargs):
        if service.sync_count > 0:
            assert heartbeat_active is True
        return await original_sync(**kwargs)

    service.sync_formal_results = sync_formal_results
    monkeypatch.setattr(
        "services.api.app.agents.runtime.inspection_work_item_consumer."
        "InspectionWorkItemAttempt.lease_heartbeat",
        tracked_heartbeat,
    )
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=FakeResultWriter(events),
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.completed_count == 1
    assert heartbeat_active is False


async def test_snapshot_consumer_records_rule_judge_llm_usage() -> None:
    events: list[str] = []
    consumer = InspectionWorkItemConsumer(
        service=FakeService(events),
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=UsageRecordingJudge(events),
        result_writer=FakeResultWriter(events),
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.usage is not None
    assert result.usage["input_tokens"] == 21
    assert result.usage["output_tokens"] == 9
    assert result.usage["total_tokens"] == 30
    assert result.usage["ai_calls"] == 1


async def test_snapshot_consumer_reuses_outer_usage_meter() -> None:
    events: list[str] = []
    consumer = InspectionWorkItemConsumer(
        service=FakeService(events),
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=UsageRecordingJudge(events),
        result_writer=FakeResultWriter(events),
    )

    with bind_run_llm_usage_meter() as usage_meter:
        result = await consumer.drain_snapshot(
            task_id="task-1",
            consumer_agent_run_id="run-1",
        )
        snapshot = usage_meter.snapshot()

    assert result.usage is None
    assert snapshot is not None
    assert snapshot.input_tokens == 21
    assert snapshot.output_tokens == 9
    assert snapshot.total_tokens == 30
    assert snapshot.ai_calls == 1


async def test_snapshot_consumer_persists_effective_insufficient_before_writer(
    caplog,
) -> None:
    events: list[str] = []
    service = FakeService(events)
    writer = FakeResultWriter(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeEvidenceRequestJudge(events),
        result_writer=writer,
    )

    with caplog.at_level(logging.INFO):
        result = await consumer.drain_snapshot(
            task_id="task-1",
            consumer_agent_run_id="run-1",
        )

    assert result.completed_count == 1
    persisted = service.attempt_transitions[0].result_payload
    assert persisted["execution_status"] == "success"
    assert persisted["evidence_requests"] == []
    assert persisted["decisions"][0]["execution_status"] == "evidence_insufficient"
    assert (
        persisted["decisions"][0]["error_code"]
        == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert writer.calls[0][1].evidence_requests == ()
    assert writer.calls[0][1].decisions[0].execution_status == "evidence_insufficient"
    assert any(
        "补采请求已按服务端策略抑制" in record.message
        and "SUPPLEMENTAL_EVIDENCE_DISABLED" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("target_platform", "request_retained"),
    (("h5", True), ("app", False), ("web", False), (None, False)),
)
async def test_supplemental_evidence_policy_isolated_to_declared_platforms(
    target_platform: str | None,
    request_retained: bool,
) -> None:
    events: list[str] = []
    service = FakeService(events, target_platform=target_platform)
    writer = FakeResultWriter(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeEvidenceRequestJudge(events),
        result_writer=writer,
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
        supplemental_evidence_target_platforms=frozenset({"h5"}),
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.completed_count == 1
    assert bool(writer.calls[0][1].evidence_requests) is request_retained


async def test_snapshot_consumer_injects_attempt_scoped_handoff_into_judge() -> None:
    events: list[str] = []
    service = FakeService(events)
    judge = FakeJudge(events)
    writer = FakeResultWriter(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=judge,
        result_writer=writer,
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.completed_count == 1
    assert len(judge.handoffs) == 1
    handoff = judge.handoffs[0]
    assert handoff.protocol_version == "inspection-handoff.v2"
    assert handoff.agent_run_id == "run-1"
    assert handoff.evidence.producer_agent_run_id == "producer-run-1"
    assert handoff.attempt_id == "attempt-1"
    assert handoff.evidence.page_capture_id == "capture-1"
    assert handoff.rules[0].contract_hash == "contract-1"
    assert writer.calls[0][0] is handoff


async def test_available_consumer_can_claim_active_session_work_for_same_run() -> None:
    events: list[str] = []
    service = FakeService(events, expected_session_dependency="active_session")
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=ContextAssertingResultWriter(events),
    )

    result = await consumer.drain_available(
        task_id="task-1",
        consumer_agent_run_id="run-1",
        session_dependency="active_session",
    )

    assert result.claimed_count == 1
    assert result.completed_count == 1
    assert events == ["judge", "persist_judgement", "write"]


async def test_recovered_judgement_replay_keeps_attempt_fencing_context(monkeypatch) -> None:
    events: list[str] = []
    recoverable_payload = {
        "items": [
            {
                "rule_code": "alioth_1",
                "execution_status": "pass",
                "observation_packet_id": "packet-1",
            }
        ]
    }
    service = FakeService(
        events,
        recoverable_result_payload=recoverable_payload,
    )

    async def replay(*, lease, result_payload):
        context = get_agent_run_context()
        assert context is not None
        assert context.task_id == lease.task_id
        assert context.agent_run_id == "run-1"
        assert context.work_item_id == lease.work_item_id
        assert context.attempt_id == "attempt-1"
        assert context.lease_token == lease.lease_token
        assert result_payload == recoverable_payload
        events.append("replay")
        return {"execution_status": "pass"}

    monkeypatch.setattr(
        "services.api.app.agents.runtime.inspection_work_item_consumer."
        "replay_recoverable_judgement",
        replay,
    )
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=FakeResultWriter(events),
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.completed_count == 1
    assert result.recovered_count == 1
    assert events == ["persist_judgement", "replay"]


async def test_snapshot_consumer_skips_attempt_conflict_without_fail_incomplete(
    caplog,
) -> None:
    events: list[str] = []
    service = FakeService(events)
    service.raise_conflict_on_judged_attempt = True
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=FakeResultWriter(events),
    )

    with caplog.at_level(logging.INFO):
        result = await consumer.drain_snapshot(
            task_id="task-1",
            consumer_agent_run_id="run-1",
        )

    assert result.claimed_count == 1
    assert result.completed_count == 0
    assert result.deferred_count == 1
    assert events == ["judge"]
    assert [command.target_status.value for command in service.attempt_transitions] == [
        "judged"
    ]
    assert service.rule_transitions == []
    assert any("状态已被其他流程推进" in record.message for record in caplog.records)


async def test_snapshot_consumer_retries_rule_execution_lease_conflict() -> None:
    events: list[str] = []
    service = FakeService(events)

    class LeaseConflictWriter(FakeResultWriter):
        async def write(self, *, handoff, outcome):
            del handoff, outcome
            raise RuleExecutionLeaseConflictError("规则写回租约冲突")

    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=FakeJudge(events),
        result_writer=LeaseConflictWriter(events),
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.deferred_count == 1
    assert service.attempt_transitions[-1].target_status.value == "retryable_error"
    assert service.transitions[-1].target_status.value == "retry_wait"


async def test_snapshot_consumer_times_out_stalled_work_item() -> None:
    events: list[str] = []
    service = FakeService(events)

    class StalledJudge(FakeJudge):
        async def judge(self, handoff):
            del handoff
            events.append("judge")
            await asyncio.Event().wait()

    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        attempt_timeout_seconds=0.01,
        judge=StalledJudge(events),
        result_writer=FakeResultWriter(events),
    )

    result = await consumer.drain_snapshot(
        task_id="task-1",
        consumer_agent_run_id="run-1",
    )

    assert result.claimed_count == 1
    assert result.completed_count == 0
    assert result.deferred_count == 1
    assert events == ["judge"]
    assert service.attempt_transitions[-1].target_status.value == "retryable_error"
    assert service.attempt_transitions[-1].error_code == "INSPECTION_WORK_ITEM_TIMEOUT"
    assert service.transitions[-1].target_status.value == "retry_wait"
    assert service.transitions[-1].error_code == "INSPECTION_WORK_ITEM_TIMEOUT"


class ReusingPlanner:
    """假规划器：把租约里的第一条规则按「整个任务一次」沿用来源结论。"""

    def __init__(self, events: list[str], *, reuse: bool = True) -> None:
        self.events = events
        self.reuse = reuse
        self.handoffs = []

    async def plan(self, *, handoff):
        from services.api.app.application.rule_result_reuse import RuleReusePlan, no_reuse_plan
        from services.api.app.domain.inspection.rule_reuse import (
            RuleReuseRecord,
            RuleReuseScope,
            build_reused_decision,
        )

        self.events.append("plan_reuse")
        self.handoffs.append(handoff)
        if not self.reuse:
            return no_reuse_plan(handoff)
        rule = handoff.rules[0]
        record = RuleReuseRecord(
            scope=RuleReuseScope.PER_TASK,
            reuse_key=f"task:{rule.rule_id}",
            source_execution_id="execution-source",
            source_page_capture_id="capture-0",
            source_execution_status="pass",
            source_page_type="home",
            reason="整个任务只判一次，沿用首个合格结论",
        )
        decision = build_reused_decision(
            handoff=handoff,
            rule=rule,
            record=record,
            source_result_summary="域名可以正常访问",
        )
        return RuleReusePlan(
            reused_decisions=(decision,),
            records=(record,),
            judge_rules=tuple(handoff.rules[1:]),
        )


async def test_consumer_skips_judge_when_every_rule_reuses_a_prior_conclusion() -> None:
    events: list[str] = []
    service = FakeService(events)
    judge = FakeJudge(events)
    writer = FakeResultWriter(events)
    planner = ReusingPlanner(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=judge,
        result_writer=writer,
        reuse_planner=planner,
    )

    result = await consumer.drain_snapshot(task_id="task-1", consumer_agent_run_id="run-1")

    assert result.completed_count == 1
    # 全部规则沿用结论：不调用 Judge，复用判定照常持久化并正式写回。
    assert events == ["plan_reuse", "persist_judgement", "write"]
    assert judge.handoffs == []
    _handoff, outcome = writer.calls[0]
    assert len(outcome.decisions) == 1
    reused = outcome.decisions[0]
    assert reused.rule_code == "alioth_1"
    assert reused.execution_status == "pass"
    assert reused.payload["judge_output"]["reuse"]["source_execution_id"] == "execution-source"
    assert reused.payload["judge_output"]["reuse"]["scope"] == "per_task"
    judged_payload = next(
        command.result_payload
        for command in service.attempt_transitions
        if command.target_status.value == "judged"
    )
    assert judged_payload["items"][0]["judge_output"]["reuse"]["scope_label"] == "整个任务一次"


async def test_consumer_judges_everything_when_planner_finds_nothing_to_reuse() -> None:
    events: list[str] = []
    service = FakeService(events)
    judge = FakeJudge(events)
    writer = FakeResultWriter(events)
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=judge,
        result_writer=writer,
        reuse_planner=ReusingPlanner(events, reuse=False),
    )

    result = await consumer.drain_snapshot(task_id="task-1", consumer_agent_run_id="run-1")

    assert result.completed_count == 1
    assert events == ["plan_reuse", "judge", "persist_judgement", "write"]
    assert [rule.rule_code for rule in judge.handoffs[0].rules] == ["alioth_1"]
    _handoff, outcome = writer.calls[0]
    assert "reuse" not in outcome.decisions[0].payload.get("judge_output", {})
