"""规则结论复用：声明解析、判定前规划、复用判定载荷与 Judge 输出合并。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from services.api.app.application.rule_result_reuse import (
    CurrentCaptureFacts,
    ReusableExecutionCandidate,
    RuleReusePlanner,
    merge_reused_decisions,
    no_reuse_plan,
    normalize_object_key,
    rule_result_reuse_enabled,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    InspectionHandoff,
    InspectionRuleTarget,
    RuleDecision,
    RuleJudgeOutcome,
)
from services.api.app.domain.inspection.rule_reuse import (
    RuleReuseRecord,
    RuleReuseScope,
    build_reused_decision,
    invalidation_conditions,
    parse_reuse_declaration,
    reuse_scope_label,
)


def _handoff(*rules: tuple[str, str]) -> InspectionHandoff:
    targets = tuple(
        InspectionRuleTarget(
            rule_id=rule_id,
            rule_code=rule_code,
            ordinal=index,
            contract_hash=f"contract-{rule_id}",
        )
        for index, (rule_id, rule_code) in enumerate(rules or (("rule-1", "alioth_1"),))
    )
    return InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="input-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-2",
            producer_agent_run_id="producer-run-1",
        ),
        rules=targets,
    )


def _candidate(**overrides: Any) -> ReusableExecutionCandidate:
    payload: dict[str, Any] = {
        "execution_id": "execution-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "pass",
        "page_capture_id": "capture-1",
        "page_url": "https://demo.example/home",
        "page_type": "home",
        "surface_kind": "web",
        "agent_run_id": "consumer-run-1",
        "contract_hash": "contract-rule-1",
        "result_summary": "域名可以正常访问",
        "problem_desc": None,
        "confidence": 0.9,
        "judge_output": {"conclusion": "pass", "reasoning_summary": "证书有效"},
    }
    payload.update(overrides)
    return ReusableExecutionCandidate(**payload)


class FakeReuseRepository:
    def __init__(
        self,
        *,
        contracts: Mapping[str, Mapping[str, Any]],
        candidates: Sequence[ReusableExecutionCandidate] = (),
        facts: CurrentCaptureFacts | None = None,
        fail: bool = False,
    ) -> None:
        self.contracts = dict(contracts)
        self.candidates = tuple(candidates)
        self.facts = facts
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []

    async def load_capture_facts(self, *, page_capture_id: str) -> CurrentCaptureFacts | None:
        self.calls.append(("load_capture_facts", page_capture_id))
        return self.facts

    async def load_rule_contracts(
        self, *, rule_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, Any]]:
        self.calls.append(("load_rule_contracts", tuple(rule_ids)))
        if self.fail:
            raise RuntimeError("数据库不可用")
        return {rule_id: self.contracts.get(rule_id, {}) for rule_id in rule_ids}

    async def list_reusable_executions(
        self, *, task_id: str, rule_ids: Sequence[str]
    ) -> Sequence[ReusableExecutionCandidate]:
        self.calls.append(("list_reusable_executions", (task_id, tuple(rule_ids))))
        return [item for item in self.candidates if item.rule_id in set(rule_ids)]


# ---------- 声明解析 ----------


def test_declaration_defaults_to_per_page_when_missing_or_invalid() -> None:
    assert parse_reuse_declaration(None) is RuleReuseScope.PER_PAGE
    assert parse_reuse_declaration({}) is RuleReuseScope.PER_PAGE
    assert parse_reuse_declaration({"reuse_policy": "everywhere"}) is RuleReuseScope.PER_PAGE
    assert parse_reuse_declaration({"reuse_policy": {"scope": "nope"}}) is RuleReuseScope.PER_PAGE


def test_declaration_accepts_text_and_mapping_forms() -> None:
    assert parse_reuse_declaration({"reuse_policy": "per_task"}) is RuleReuseScope.PER_TASK
    assert (
        parse_reuse_declaration({"reuse_policy": {"scope": "same_object"}})
        is RuleReuseScope.SAME_OBJECT
    )
    assert reuse_scope_label("per_task") == "整个任务一次"
    assert reuse_scope_label("unknown") == "每页各自执行"
    assert invalidation_conditions(RuleReuseScope.PER_PAGE) == ()
    assert "account_or_login_changed" in invalidation_conditions("per_task")
    assert "object_changed" in invalidation_conditions("same_object")


def test_reuse_record_rejects_unusable_source_or_scope() -> None:
    with pytest.raises(ValueError):
        RuleReuseRecord(
            scope=RuleReuseScope.PER_PAGE,
            reuse_key="k",
            source_execution_id="e",
            source_page_capture_id="c",
            source_execution_status="pass",
            reason="r",
        )
    with pytest.raises(ValueError):
        RuleReuseRecord(
            scope=RuleReuseScope.PER_TASK,
            reuse_key="k",
            source_execution_id="e",
            source_page_capture_id="c",
            source_execution_status="manual_review",
            reason="r",
        )


def test_object_key_ignores_tracking_params_fragment_and_trailing_slash() -> None:
    assert normalize_object_key("https://Demo.example/video/1/?utm_source=x&b=2&a=1#top") == (
        "https://demo.example/video/1?a=1&b=2"
    )
    assert normalize_object_key("https://demo.example/video/1?a=1&b=2") == normalize_object_key(
        "https://demo.example/video/1/?b=2&a=1&spm=abc"
    )
    assert normalize_object_key("") == ""
    assert normalize_object_key("com.demo.app/MainActivity/") == "com.demo.app/MainActivity"


# ---------- 判定前规划 ----------


async def test_per_task_rule_reuses_first_qualified_conclusion_and_skips_judge_for_it() -> None:
    handoff = _handoff(("rule-1", "alioth_ops_1"), ("rule-2", "alioth_3"))
    repository = FakeReuseRepository(
        contracts={
            "rule-1": {"reuse_policy": {"scope": "per_task"}},
            "rule-2": {"reuse_policy": {"scope": "per_page"}},
        },
        candidates=(
            _candidate(execution_id="execution-old", execution_status="evidence_insufficient"),
            _candidate(execution_id="execution-1", rule_code="alioth_ops_1"),
            _candidate(execution_id="execution-later", rule_code="alioth_ops_1"),
        ),
        facts=CurrentCaptureFacts(
            page_capture_id="capture-2",
            url="https://demo.example/list",
            page_type="video_list",
            surface_kind="web",
        ),
    )

    plan = await RuleReusePlanner(repository=repository).plan(handoff=handoff)

    assert plan.reused is True
    assert [rule.rule_code for rule in plan.judge_rules] == ["alioth_3"]
    assert ("alioth_3", "scope_per_page") in plan.skipped
    decision = plan.reused_decisions[0]
    assert decision.rule_code == "alioth_ops_1"
    assert decision.execution_status == "pass"
    assert decision.observation_packet_id is None
    reuse = decision.payload["judge_output"]["reuse"]
    assert reuse["scope"] == "per_task"
    assert reuse["scope_label"] == "整个任务一次"
    assert reuse["source_execution_id"] == "execution-1"
    assert reuse["source_page_capture_id"] == "capture-1"
    assert reuse["source_execution_status"] == "pass"
    assert "account_or_login_changed" in reuse["invalidate_on"]
    assert reuse["basis"]["rule_contract_hash"] == "contract-rule-1"
    assert decision.payload["judge_input"]["schema_version"] == "rule-reuse.v1"
    assert decision.payload["result_summary"] == "沿用home上的结论：域名可以正常访问"
    assert decision.payload["judge_output"]["conclusion"] == "pass"
    assert decision.payload["judge_output"]["schema_version"] == "1.0"
    assert decision.payload["judge_output"]["reasoning_summary"] == "证书有效"
    tool_results = decision.payload["judge_output"]["tool_results"]
    assert len(tool_results) == 1
    assert dict(tool_results[0]) == {
        "tool_name": "rule_result_reuse",
        "status": "reused",
        "source_execution_id": "execution-1",
    }
    assert decision.payload["confidence"] == 0.9
    assert decision.payload["evidence_artifact_ids"] == ()
    # 缩减后的 Handoff 只剩要判的规则，且沿用工作项身份。
    judge_handoff = plan.judge_handoff(handoff)
    assert judge_handoff is not None
    assert [rule.rule_code for rule in judge_handoff.rules] == ["alioth_3"]
    assert judge_handoff.work_item_id == handoff.work_item_id
    assert judge_handoff.input_hash == handoff.input_hash


async def test_reused_hybrid_decision_passes_write_back_contract() -> None:
    """沿用记录要能过 llm/hybrid 写回契约；09-22 之前每条沿用都被打成执行异常。"""
    from services.api.app.application.rule_execution import (
        RecordedRuleExecution,
        RecordRuleExecutionCommand,
        RuleExecutionDefinition,
        RuleExecutionService,
    )

    handoff = _handoff(("rule-1", "alioth_ops_1"))
    repository = FakeReuseRepository(
        contracts={"rule-1": {"reuse_policy": {"scope": "per_task"}}},
        candidates=(
            _candidate(
                rule_code="alioth_ops_1",
                judge_output={"conclusion": "pass"},
            ),
        ),
        facts=CurrentCaptureFacts(page_capture_id="capture-2", surface_kind="web"),
    )
    plan = await RuleReusePlanner(repository=repository).plan(handoff=handoff)
    payload = {
        key: value
        for key, value in plan.reused_decisions[0].to_writer_payload().items()
        if value is not None
    }

    class _Recording:
        def __init__(self) -> None:
            self.commands: list[RecordRuleExecutionCommand] = []

        async def get_rule_execution_definition(self, *, task_id, rule_id, rule_code):
            return RuleExecutionDefinition(
                executor_type="hybrid",
                manual_review_policy={"min_confidence": 0.75},
                severity="critical",
            )

        async def record_rule_execution(self, command):
            self.commands.append(command)
            return RecordedRuleExecution(
                execution_id="execution-2",
                rule_id="rule-1",
                rule_code="alioth_ops_1",
                execution_status=command.execution_status,
                page_capture_id=command.page_capture_id,
            )

    recording = _Recording()
    await RuleExecutionService(repository=recording).record_rule_execution(
        RecordRuleExecutionCommand(task_id="task-1", page_capture_id="capture-2", **payload)
    )

    command = recording.commands[0]
    assert command.execution_status == "pass"
    assert command.judge_strategy == "hybrid"
    assert command.judge_output["reuse"]["source_execution_id"] == "execution-1"
    assert command.judge_output["reasoning_summary"] == "沿用home上的结论，本页未重新判定"
    assert command.evidence_artifact_ids == []


async def test_rule_version_or_environment_change_blocks_per_task_reuse() -> None:
    handoff = _handoff(("rule-1", "alioth_ops_1"))
    contracts = {"rule-1": {"reuse_policy": "per_task"}}
    facts = CurrentCaptureFacts(page_capture_id="capture-2", surface_kind="web")

    stale = FakeReuseRepository(
        contracts=contracts,
        candidates=(_candidate(contract_hash="contract-old"),),
        facts=facts,
    )
    plan = await RuleReusePlanner(repository=stale).plan(handoff=handoff)
    assert plan.reused is False
    assert plan.skipped == (("alioth_ops_1", "rule_version_changed"),)
    assert plan.judge_handoff(handoff) is handoff

    other_surface = FakeReuseRepository(
        contracts=contracts,
        candidates=(_candidate(surface_kind="android"),),
        facts=facts,
    )
    plan = await RuleReusePlanner(repository=other_surface).plan(handoff=handoff)
    assert plan.skipped == (("alioth_ops_1", "environment_changed"),)

    unknown_version = FakeReuseRepository(
        contracts=contracts,
        candidates=(_candidate(contract_hash=None),),
        facts=facts,
    )
    plan = await RuleReusePlanner(repository=unknown_version).plan(handoff=handoff)
    assert plan.skipped == (("alioth_ops_1", "rule_version_unknown"),)


async def test_same_object_reuses_only_the_same_normalized_page() -> None:
    handoff = _handoff(("rule-1", "alioth_41"))
    contracts = {"rule-1": {"reuse_policy": {"scope": "same_object"}}}
    same_page = FakeReuseRepository(
        contracts=contracts,
        candidates=(
            _candidate(page_url="https://demo.example/profile/?utm_source=a", page_type="profile"),
        ),
        facts=CurrentCaptureFacts(
            page_capture_id="capture-2",
            url="https://demo.example/profile?spm=xx",
            page_type="profile",
            surface_kind="web",
        ),
    )
    plan = await RuleReusePlanner(repository=same_page).plan(handoff=handoff)
    assert plan.reused is True
    reuse = plan.reused_decisions[0].payload["judge_output"]["reuse"]
    assert reuse["scope"] == "same_object"
    assert reuse["reuse_key"] == "https://demo.example/profile"
    assert plan.judge_handoff(handoff) is None  # 全部沿用，Judge 整体跳过

    other_page = FakeReuseRepository(
        contracts=contracts,
        candidates=(_candidate(page_url="https://demo.example/profile", page_type="profile"),),
        facts=CurrentCaptureFacts(
            page_capture_id="capture-2",
            url="https://demo.example/settings",
            page_type="settings",
            surface_kind="web",
        ),
    )
    plan = await RuleReusePlanner(repository=other_page).plan(handoff=handoff)
    assert plan.skipped == (("alioth_41", "object_changed"),)

    no_identity = FakeReuseRepository(
        contracts=contracts,
        candidates=(_candidate(),),
        facts=CurrentCaptureFacts(page_capture_id="capture-2"),
    )
    plan = await RuleReusePlanner(repository=no_identity).plan(handoff=handoff)
    assert plan.skipped == (("alioth_41", "object_identity_unavailable"),)


async def test_same_material_is_not_reused_until_material_identity_exists() -> None:
    handoff = _handoff(("rule-1", "alioth_16"))
    repository = FakeReuseRepository(
        contracts={"rule-1": {"reuse_policy": "same_material"}},
        candidates=(_candidate(),),
        facts=CurrentCaptureFacts(page_capture_id="capture-2", surface_kind="web"),
    )

    plan = await RuleReusePlanner(repository=repository).plan(handoff=handoff)

    assert plan.reused is False
    assert plan.skipped == (("alioth_16", "material_identity_unavailable"),)


async def test_lookup_failure_or_disabled_planner_falls_back_to_full_judge() -> None:
    handoff = _handoff(("rule-1", "alioth_ops_1"))
    failing = FakeReuseRepository(contracts={}, fail=True)
    plan = await RuleReusePlanner(repository=failing).plan(handoff=handoff)
    assert plan.reused is False
    assert plan.skipped == (("alioth_ops_1", "reuse_lookup_failed"),)

    disabled = RuleReusePlanner(repository=failing, enabled=False)
    plan = await disabled.plan(handoff=handoff)
    assert plan.skipped == (("alioth_ops_1", "reuse_disabled"),)
    assert failing.calls == [("load_rule_contracts", ("rule-1",))]


async def test_source_from_the_same_capture_is_never_reused() -> None:
    handoff = _handoff(("rule-1", "alioth_ops_1"))
    repository = FakeReuseRepository(
        contracts={"rule-1": {"reuse_policy": "per_task"}},
        candidates=(_candidate(page_capture_id="capture-2"),),
        facts=CurrentCaptureFacts(page_capture_id="capture-2", surface_kind="web"),
    )

    plan = await RuleReusePlanner(repository=repository).plan(handoff=handoff)

    assert plan.reused is False
    assert plan.skipped == (("alioth_ops_1", "no_prior_conclusion"),)


# ---------- 合并 Judge 输出 ----------


def test_merge_keeps_judged_decisions_requests_and_deferred() -> None:
    handoff = _handoff(("rule-1", "alioth_ops_1"), ("rule-2", "alioth_3"))
    record = RuleReuseRecord(
        scope=RuleReuseScope.PER_TASK,
        reuse_key="task:rule-1",
        source_execution_id="execution-1",
        source_page_capture_id="capture-1",
        source_execution_status="fail",
        reason="整个任务只判一次",
    )
    reused = build_reused_decision(handoff=handoff, rule=handoff.rules[0], record=record)
    judged = RuleJudgeOutcome(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        decisions=(
            RuleDecision(
                work_item_id="work-1",
                attempt_id="attempt-1",
                rule_id="rule-2",
                rule_code="alioth_3",
                execution_status="pass",
            ),
        ),
    )

    merged = merge_reused_decisions(handoff=handoff, outcome=judged, decisions=(reused,))

    assert [item.rule_code for item in merged.decisions] == ["alioth_3", "alioth_ops_1"]
    assert merged.decisions[1].execution_status == "fail"
    assert merged.to_dict()["execution_status"] == "success"

    alone = merge_reused_decisions(handoff=handoff, outcome=None, decisions=(reused,))
    assert alone.decisions == (reused,)
    with pytest.raises(ValueError):
        merge_reused_decisions(handoff=handoff, outcome=None, decisions=())
    assert merge_reused_decisions(handoff=handoff, outcome=judged, decisions=()) is judged


def test_no_reuse_plan_and_settings_helper() -> None:
    handoff = _handoff()
    plan = no_reuse_plan(handoff)
    assert plan.reused is False
    assert plan.judge_rules == handoff.rules
    assert plan.skipped == ()

    class _Settings:
        class rule_result_reuse:
            enabled = True

    assert rule_result_reuse_enabled(_Settings()) is True
    assert rule_result_reuse_enabled(object()) is False
