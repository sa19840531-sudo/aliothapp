"""Web V2 正式规则的主动交互安全边界。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY

import pytest

from services.api.app.agents.runtime.inspection_result_writer import (
    CurrentRuleDecisionWriter,
)
from services.api.app.agents.runtime.inspection_work_item_consumer import (
    InspectionWorkItemConsumer,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemExecutionLink,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemSyncResult,
)
from services.api.app.application.report_generation import _rule_execution_to_dict
from services.api.app.application.task_query import RuleExecutionSummary
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


class _Service:
    def __init__(self, *, target_platform: str) -> None:
        self.target_platform = target_platform
        self.claim_count = 0
        self.row_version = 1
        self.attempt_transitions = []
        self.rule_transitions = []
        self.work_transitions = []
        self.sync_count = 0

    async def claim(self, command):
        self.claim_count += 1
        if self.claim_count > 1:
            return None
        lease = InspectionWorkItemLease(
            work_item_id="work-web-active",
            task_id=command.task_id,
            page_capture_id="capture-web-active",
            batch_key="hybrid:journey:active_session",
            strategy="hybrid",
            category="journey",
            session_dependency="active_session",
            input_hash="input-web-active",
            producer_agent_run_id="run-web-main",
            consumer_agent_run_id=command.consumer_agent_run_id,
            lease_token="lease-web-active",
            attempt_no=1,
            row_version=1,
            rules=(
                InspectionWorkItemRule(
                    rule_id="rule-active",
                    rule_code="alioth_active",
                    ordinal=0,
                    contract_hash="contract-active",
                ),
            ),
            target_platform=self.target_platform,
        )
        return lease

    async def transition(self, command):
        self.work_transitions.append(command)
        self.row_version += 1
        return self.row_version

    async def start_attempt(self, _command):
        return "attempt-web-active"

    async def transition_attempt(self, command):
        self.attempt_transitions.append(command)

    async def transition_rule(self, command):
        self.rule_transitions.append(command)

    async def sync_formal_results(self, **_kwargs):
        self.sync_count += 1
        if self.sync_count == 1:
            return InspectionWorkItemSyncResult(
                work_item_id="work-web-active",
                expected_rule_count=1,
                terminal_rule_count=0,
                pending_rule_count=1,
                written_rule_count=0,
                links=(),
            )
        return InspectionWorkItemSyncResult(
            work_item_id="work-web-active",
            expected_rule_count=1,
            terminal_rule_count=1,
            pending_rule_count=0,
            written_rule_count=0,
            links=(
                InspectionWorkItemExecutionLink(
                    rule_id="rule-active",
                    rule_code="alioth_active",
                    execution_status="evidence_insufficient",
                    observation_packet_id=None,
                    rule_execution_id="execution-web-active",
                ),
            ),
        )

    async def renew_lease(self, _command):
        self.row_version += 1
        return self.row_version


class _EvidenceRequestJudge:
    async def judge(self, handoff):
        request = build_evidence_request(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            rule_id=handoff.rules[0].rule_id,
            rule_code=handoff.rules[0].rule_code,
            target_capture_id=handoff.evidence.page_capture_id,
            reason_code="ACTIVE_INTERACTION_REQUIRED",
            evidence_goal="需要提交表单后观察结果",
            required_artifact_types=("screenshot", "dom"),
            session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
            replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
            required_equivalence=EvidenceRequiredEquivalence.ORIGINAL_ATOMIC,
            side_effect_policy=EvidenceSideEffectPolicy.APPROVAL_REQUIRED,
            target_invariants={"business": {"route": "/checkout"}},
            minimum_source_grade=EvidenceSourceGrade.A0,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return RuleJudgeOutcome(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            evidence_requests=(request,),
        )


class _ExistingJourneyJudge:
    async def judge(self, handoff):
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
                    observation_packet_id="packet-existing-journey",
                ),
            ),
        )


class _Writer:
    def __init__(self) -> None:
        self.outcomes = []
        self.handoffs = []

    async def write(self, *, handoff, outcome):
        self.handoffs.append(handoff)
        self.outcomes.append(outcome)
        decision = outcome.decisions[0]
        return {
            "execution_status": decision.execution_status,
            "rule_code": decision.rule_code,
            "execution_id": "execution-web-active",
        }


@asynccontextmanager
async def _without_heartbeat(_attempt):
    yield


def _consumer(monkeypatch, *, target_platform: str, judge):
    service = _Service(target_platform=target_platform)
    writer = _Writer()
    monkeypatch.setattr(
        "services.api.app.agents.runtime.inspection_work_item_consumer."
        "InspectionWorkItemAttempt.lease_heartbeat",
        _without_heartbeat,
    )
    return (
        InspectionWorkItemConsumer(
            service=service,
            lease_owner="worker-web",
            lease_seconds=180,
            retry_backoff_seconds=2,
            judge=judge,
            result_writer=writer,
        ),
        service,
        writer,
    )


async def test_web_active_interaction_is_persisted_as_specific_evidence_gap(
    monkeypatch,
) -> None:
    consumer, service, writer = _consumer(
        monkeypatch,
        target_platform="web",
        judge=_EvidenceRequestJudge(),
    )

    result = await consumer.drain_available(
        task_id="task-web",
        consumer_agent_run_id="run-web-main",
        session_dependency="active_session",
    )

    assert result.completed_count == 1
    persisted = service.attempt_transitions[0].result_payload
    assert persisted["evidence_requests"] == []
    assert persisted["decisions"][0]["execution_status"] == "evidence_insufficient"
    assert (
        persisted["decisions"][0]["error_code"]
        == "web_active_interaction_not_enabled"
    )
    audit = persisted["decisions"][0]["judge_output"]["web_active_interaction"]
    assert audit == {
        "request_id": ANY,
        "target_capture_id": "capture-web-active",
        "reason_code": "web_active_interaction_not_enabled",
        "disposition": "suppressed",
    }
    written = writer.outcomes[0]
    assert written.evidence_requests == ()
    assert (
        written.decisions[0].to_writer_payload()["error_code"]
        == "web_active_interaction_not_enabled"
    )

    formal_calls = []

    async def record_formal_result(**kwargs):
        formal_calls.append(kwargs)
        return {"execution_id": "execution-web-active"}

    monkeypatch.setattr(
        "services.api.app.agents.runtime.inspection_result_writer."
        "record_rule_execution_for_current_run",
        record_formal_result,
    )
    await CurrentRuleDecisionWriter().write(
        handoff=writer.handoffs[0],
        outcome=written,
    )
    assert formal_calls[0]["execution_status"] == "evidence_insufficient"
    assert formal_calls[0]["error_code"] == "web_active_interaction_not_enabled"
    assert formal_calls[0]["judge_input"]["web_active_interaction"] == {
        "source": "persisted_evidence_only",
        "original_reason_code": "ACTIVE_INTERACTION_REQUIRED",
        "session_dependency": "active_session",
        "side_effect_policy": "approval_required",
        "required_interaction": None,
    }
    assert formal_calls[0]["judge_output"]["web_active_interaction"] == audit


async def test_web_existing_journey_decision_is_not_downgraded(monkeypatch) -> None:
    consumer, service, writer = _consumer(
        monkeypatch,
        target_platform="web",
        judge=_ExistingJourneyJudge(),
    )

    result = await consumer.drain_available(
        task_id="task-web",
        consumer_agent_run_id="run-web-main",
        session_dependency="active_session",
    )

    assert result.completed_count == 1
    persisted = service.attempt_transitions[0].result_payload
    assert persisted["decisions"][0]["execution_status"] == "pass"
    assert writer.outcomes[0].decisions[0].execution_status == "pass"


def test_web_active_interaction_reason_is_visible_in_formal_report_audit() -> None:
    result = _rule_execution_to_dict(
        RuleExecutionSummary(
            execution_id="execution-web-active",
            rule_id="rule-active",
            rule_code="alioth_active",
            rule_name="需要主动交互的规则",
            page_capture_id="capture-web-active",
            execution_status="evidence_insufficient",
            judge_strategy="hybrid",
            confidence=None,
            severity="medium",
            result_summary="Web 正式质检不执行未授权主动交互，规则按证据不足收口。",
            problem_desc="需要提交表单后观察结果",
            review_status=None,
            started_at=None,
            finished_at=None,
            diagnostic_codes=("web_active_interaction_not_enabled",),
        )
    )

    assert result["execution_status"] == "evidence_insufficient"
    assert result["outcome_class"] == "evidence_gap"
    assert result["diagnostic_codes"] == ["web_active_interaction_not_enabled"]


@pytest.mark.parametrize("target_platform", ["h5", "app"])
async def test_non_web_active_interaction_keeps_shared_supplemental_policy(
    monkeypatch,
    target_platform: str,
) -> None:
    consumer, service, _writer = _consumer(
        monkeypatch,
        target_platform=target_platform,
        judge=_EvidenceRequestJudge(),
    )

    result = await consumer.drain_available(
        task_id=f"task-{target_platform}",
        consumer_agent_run_id=f"run-{target_platform}",
        session_dependency="active_session",
    )

    assert result.completed_count == 1
    persisted = service.attempt_transitions[0].result_payload
    assert persisted["decisions"][0]["error_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
