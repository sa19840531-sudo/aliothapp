"""任务收口结论：四个维度分别判读、门禁原因说人话、路由透传访问范围。

排期「完善任务结束和资源清理流程」的读侧：探索走完不等于证据够，证据够不等于规则判完，
规则判完也不等于完成检查放行；每一维不达标都要说清楚是哪一条挡着。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from services.api.app.api.v1 import evidence as evidence_module
from services.api.app.api.v1.workstation import task_insight as task_insight_module
from services.api.app.application.task_insight import (
    TaskInsightNotFoundError,
    TaskInsightService,
)
from services.api.app.domain.task.closure_outcome import (
    ClosureDimensionState,
    completion_gate_dimension,
    evidence_dimension,
    explain_block_reason,
    exploration_dimension,
    overall_summary,
    rule_dimension,
)
from services.api.app.main import create_app

NOW = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)


# ---------- 单维判读 ----------


def test_exploration_dimension_reports_what_is_still_running() -> None:
    running = exploration_dimension(
        exploration_terminal=False,
        pending_action_count=2,
        pending_capture_commit_count=1,
        background_execution_count=0,
        visited_page_type_count=3,
    )
    assert running.state is ClosureDimensionState.INCOMPLETE
    assert "探索本身还没结束" in running.summary
    assert "2 个动作没做完" in running.summary
    assert "1 张截图没存完" in running.summary

    done = exploration_dimension(
        exploration_terminal=True,
        pending_action_count=0,
        pending_capture_commit_count=0,
        background_execution_count=0,
        visited_page_type_count=7,
    )
    assert done.state is ClosureDimensionState.SATISFIED
    assert "7 类页面" in done.summary

    unknown = exploration_dimension(
        exploration_terminal=None,
        pending_action_count=0,
        pending_capture_commit_count=0,
        background_execution_count=0,
        visited_page_type_count=0,
    )
    assert unknown.state is ClosureDimensionState.UNKNOWN


def test_evidence_dimension_separates_open_requests_violations_and_gaps() -> None:
    waiting = evidence_dimension(
        gap_count=4,
        presentation_violation_count=0,
        open_request_count=2,
        settled_request_count=1,
    )
    assert waiting.state is ClosureDimensionState.INCOMPLETE
    assert "2 个补采请求没有结清" in waiting.summary

    violated = evidence_dimension(
        gap_count=4,
        presentation_violation_count=3,
        open_request_count=0,
        settled_request_count=3,
    )
    # 用了被遮挡或诊断材料是「被挡住」，不是「还没走完」
    assert violated.state is ClosureDimensionState.BLOCKED
    assert "不算正式证据" in violated.summary

    gapped = evidence_dimension(
        gap_count=4,
        presentation_violation_count=0,
        open_request_count=0,
        settled_request_count=0,
    )
    assert gapped.state is ClosureDimensionState.INCOMPLETE
    assert "4 条规则证据不足" in gapped.summary

    clean = evidence_dimension(
        gap_count=0,
        presentation_violation_count=0,
        open_request_count=0,
        settled_request_count=5,
    )
    assert clean.state is ClosureDimensionState.SATISFIED


def test_rule_dimension_uses_six_state_distribution() -> None:
    labels = {"passed": "通过", "failed": "未通过", "other": "其他"}
    mixed = rule_dimension(
        total=10,
        group_counts={"passed": 6, "failed": 2, "other": 2},
        group_labels=labels,
    )
    assert mixed.state is ClosureDimensionState.INCOMPLETE
    assert "共 10 条：通过 6，未通过 2，没有确定结论 2" in mixed.summary

    settled = rule_dimension(
        total=8,
        group_counts={"passed": 6, "failed": 2},
        group_labels=labels,
    )
    assert settled.state is ClosureDimensionState.SATISFIED

    empty = rule_dimension(total=0, group_counts={}, group_labels=labels)
    assert empty.state is ClosureDimensionState.UNKNOWN
    assert "没有命中任何规则" in empty.summary


def test_rule_dimension_summary_numbers_add_up_to_the_total() -> None:
    """真库实况：84 条里有 20 条本产品不适用，句子只报 2+0+62 会让运营对不上账。"""

    full = rule_dimension(
        total=84,
        group_counts={"passed": 2, "failed": 0, "not_applicable": 20, "other": 62},
        group_labels={
            "passed": "通过",
            "failed": "未通过",
            "not_applicable": "本产品不适用",
            "other": "其他",
        },
    )
    assert full.summary == (
        "共 84 条：通过 2，未通过 0，本产品不适用 20，没有确定结论 62。"
    )

    covered = rule_dimension(
        total=9,
        group_counts={"passed": 4, "failed": 1, "coverage_gap": 3, "detect_error": 1},
        group_labels={},
    )
    assert covered.summary == (
        "共 9 条：通过 4，未通过 1，计划外未覆盖 3，检测异常 1。"
    )


def test_completion_gate_dimension_separates_waiting_for_people_from_stuck() -> None:
    waiting = completion_gate_dimension(
        gate_passed=False,
        block_reason_codes=["review_policy_unsatisfied", "h5_coverage_scope_incomplete"],
        evaluated_at=NOW,
        open_review_count=12,
    )
    # 只等人工：任务能进复核中心，不算被系统卡住
    assert waiting.state is ClosureDimensionState.INCOMPLETE
    assert [item["label"] for item in waiting.blockers] == ["还有人工确认没做", "H5 必检页面没走全"]
    assert all(item["waiting_for_people"] for item in waiting.blockers)

    stuck = completion_gate_dimension(
        gate_passed=False,
        block_reason_codes=["nonterminal_enforced_work_item"],
        evaluated_at=NOW,
        nonterminal_work_item_count=3,
    )
    assert stuck.state is ClosureDimensionState.BLOCKED
    assert stuck.blockers[0]["label"] == "还有规则检查没有结果"
    assert stuck.blockers[0]["waiting_for_people"] is False
    assert stuck.metrics["nonterminal_work_item_count"] == 3

    passed = completion_gate_dimension(gate_passed=True, block_reason_codes=[], evaluated_at=NOW)
    assert passed.state is ClosureDimensionState.SATISFIED

    unknown = completion_gate_dimension(gate_passed=None, block_reason_codes=[])
    assert unknown.state is ClosureDimensionState.UNKNOWN


def test_unknown_block_reason_still_explains_itself() -> None:
    explained = explain_block_reason("some_future_reason")
    assert explained["code"] == "some_future_reason"
    assert "找研发确认" in explained["advice"]
    assert explained["waiting_for_people"] is False


def test_overall_summary_prefers_blocked_then_incomplete() -> None:
    blocked = evidence_dimension(
        gap_count=1, presentation_violation_count=1, open_request_count=0, settled_request_count=0
    )
    incomplete = exploration_dimension(
        exploration_terminal=False,
        pending_action_count=1,
        pending_capture_commit_count=0,
        background_execution_count=0,
        visited_page_type_count=1,
    )
    satisfied = rule_dimension(
        total=2, group_counts={"passed": 2}, group_labels={"passed": "通过"}
    )
    assert "被挡住" in overall_summary([blocked, incomplete, satisfied])
    assert "还没走完" in overall_summary([incomplete, satisfied])
    assert overall_summary([satisfied]) == "四项都达标，任务已正常收口。"


# ---------- 服务与路由 ----------


class FakeInsightRepository:
    def __init__(
        self,
        facts: dict[str, Any],
        rule_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.facts = facts
        self.rule_rows = rule_rows or []
        self.calls: list[tuple[str, Any]] = []

    async def fetch_task_context(self, task_id: str, *, access_scope=None) -> dict | None:
        self.calls.append(("fetch_task_context", (task_id, access_scope)))
        if task_id == "missing":
            return None
        return {
            "task_id": task_id,
            "status": "review",
            "run_id": "run-1",
            "target_url": "https://demo.example",
            "task_config": {},
        }

    async def fetch_closure_outcome_facts(self, *, task_id, agent_run_id):
        self.calls.append(("fetch_closure_outcome_facts", (task_id, agent_run_id)))
        return self.facts


async def test_service_assembles_four_dimensions_from_gate_snapshot(monkeypatch) -> None:
    repository = FakeInsightRepository(
        {
            "gate": {
                "gate_passed": False,
                # 快照里 block_reasons 是 jsonb 文本
                "block_reasons": json.dumps(
                    ["nonterminal_enforced_work_item", "review_policy_unsatisfied"]
                ),
                "exploration_terminal": True,
                "pending_action_count": 0,
                "pending_capture_commit_count": 0,
                "background_execution_count": 0,
                "nonterminal_enforced_work_item_count": 2,
                "open_review_count": 5,
                "evaluated_at": NOW,
            },
            "evidence": {"gap_count": 3, "presentation_violation_count": 0},
            "supplement_request_counts": {"pending": 1, "unfulfillable": 2, "satisfied": 1},
            "visited_page_type_count": 6,
        }
    )
    service = TaskInsightService(repository=repository)

    async def _items(self, task_id, context, access_scope):
        return [
            {"display_status": "passed"},
            {"display_status": "passed"},
            {"display_status": "failed"},
            {"display_status": "evidence_insufficient"},
        ], None

    monkeypatch.setattr(TaskInsightService, "_assemble_rule_projection", _items)

    result = await service.closure_outcome("task-1")

    assert result["protocol_version"] == "task-closure-outcome.v1"
    assert result["task_status"] == "review"
    assert result["run_id"] == "run-1"
    assert result["supplement_request_counts"] == {
        "pending": 1,
        "unfulfillable": 2,
        "satisfied": 1,
    }
    dimensions = {item["key"]: item for item in result["dimensions"]}
    assert list(dimensions) == ["exploration", "evidence", "rules", "completion_gate"]
    assert dimensions["exploration"]["state"] == "satisfied"
    assert "6 类页面" in dimensions["exploration"]["summary"]
    # 有一个补采请求还在途 → 证据维度先说没结清
    assert dimensions["evidence"]["state"] == "incomplete"
    assert dimensions["evidence"]["metrics"]["open_supplement_request_count"] == 1
    assert dimensions["evidence"]["metrics"]["settled_supplement_request_count"] == 3
    assert dimensions["rules"]["metrics"]["total"] == 4
    assert dimensions["rules"]["metrics"]["group_counts"]["passed"] == 2
    # 证据不足归六态里的「其他」
    assert dimensions["rules"]["metrics"]["group_counts"]["other"] == 1
    assert dimensions["completion_gate"]["state"] == "blocked"
    assert [item["code"] for item in dimensions["completion_gate"]["blockers"]] == [
        "nonterminal_enforced_work_item",
        "review_policy_unsatisfied",
    ]
    assert result["summary"].startswith("最终完成检查被挡住")
    assert ("fetch_closure_outcome_facts", ("task-1", "run-1")) in repository.calls


async def test_service_handles_task_without_any_gate_snapshot(monkeypatch) -> None:
    repository = FakeInsightRepository(
        {
            "gate": None,
            "evidence": {},
            "supplement_request_counts": {},
            "visited_page_type_count": 0,
        }
    )
    service = TaskInsightService(repository=repository)

    async def _items(self, task_id, context, access_scope):
        return [], None

    monkeypatch.setattr(TaskInsightService, "_assemble_rule_projection", _items)

    result = await service.closure_outcome("task-1")

    dimensions = {item["key"]: item for item in result["dimensions"]}
    assert dimensions["exploration"]["state"] == "unknown"
    assert dimensions["completion_gate"]["state"] == "unknown"
    # 没有缺口也没有在途请求时证据维度算达标
    assert dimensions["evidence"]["state"] == "satisfied"
    assert dimensions["rules"]["state"] == "unknown"


async def test_service_raises_not_found_for_unknown_task() -> None:
    service = TaskInsightService(repository=FakeInsightRepository({}))

    try:
        await service.closure_outcome("missing")
    except TaskInsightNotFoundError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("未知任务应抛出 TaskInsightNotFoundError")


class FakeInsightService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.missing = False

    async def closure_outcome(self, task_id: str, *, access_scope=None) -> dict:
        if self.missing:
            raise TaskInsightNotFoundError(f"未找到任务：{task_id}")
        self.calls.append(("closure_outcome", (task_id, access_scope)))
        return {
            "protocol_version": "task-closure-outcome.v1",
            "task_id": task_id,
            "summary": "四项都达标，任务已正常收口。",
            "dimensions": [{"key": "exploration", "state": "satisfied"}],
        }


def _client() -> tuple[TestClient, FakeInsightService]:
    service = FakeInsightService()
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = lambda: service
    app.dependency_overrides[evidence_module._get_task_insight_service] = lambda: service
    return TestClient(app), service


def test_closure_outcome_route_passes_access_scope() -> None:
    test_client, service = _client()

    response = test_client.get("/api/v1/workstation/tasks/task-1/closure-outcome")

    assert response.status_code == 200
    body = response.json()
    assert body["dimensions"][0]["key"] == "exploration"
    name, (task_id, access_scope) = service.calls[0]
    assert name == "closure_outcome"
    assert task_id == "task-1"
    assert access_scope is not None
    assert access_scope.actor_user_id == "test-user"


def test_closure_outcome_route_maps_unknown_task_to_404() -> None:
    test_client, service = _client()
    service.missing = True

    response = test_client.get("/api/v1/workstation/tasks/task-9/closure-outcome")

    assert response.status_code == 404
    assert "task-9" in response.json()["error"]["message"]
