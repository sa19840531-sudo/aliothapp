"""任务详情沿用全规则清单，候选核对不膨胀真实执行统计。"""

from copy import deepcopy

from services.api.app.application.report_rule_inventory import project_task_rule_inventory
from services.api.app.application.task_insight import TaskInsightService
from services.api.tests.test_report_rule_inventory import _aggregate_na, _fixture
from services.api.tests.test_task_insight_service import FakeInsightRepository, _execution


async def test_task_rule_results_contains_all_73_rules_but_stats_keep_343_real_attempts():
    fixture, snapshot = _fixture()
    repository = FakeInsightRepository()
    repository.context.update(status="review", target_platform="app", ruleset_rule_total=73)
    repository.context["task_config"].update(
        workflow_version="v2", execution_mode="full_inspection"
    )
    repository.execution_rows = [
        _execution(
            item["rule_code"],
            {
                "passed": "pass",
                "evidence_insufficient": "evidence_insufficient",
                "not_applicable": "skipped",
            }[item["machine_display_status"]],
            execution_id=item["execution_id"],
            rule_id=item["rule_id"],
        )
        for item in fixture["rule_items"]
    ]

    class Reader:
        async def load_snapshot(self, **kwargs):
            assert kwargs == {"task_id": "task-1"}
            return deepcopy(snapshot)

    service = TaskInsightService(repository=repository, rule_inventory_repository=Reader())
    result = await service.rule_results("task-1")
    assert result["rule_inventory_total"] == 73
    assert len({item["rule_id"] for item in result["items"]}) == 73
    assert len([item for item in result["items"] if item["item_type"] == "candidate"]) == 53
    assert all(
        item["execution_id"] is None and not item["confirmable"]
        for item in result["items"]
        if item["item_type"] == "candidate"
    )
    assert result["rule_inventory_pending_rules"] == 54
    stats = await service.stats("task-1")
    assert stats["total"] == stats["triggered"] == 343
    assert stats["finished"] == 80
    assert sum(stats["status_counts"].values()) == 343
    assert stats["rule_inventory_total"] == 73


async def test_startup_dispositions_remain_visible_without_counting_as_triggered():
    _, snapshot = _fixture()
    snapshot.update(executions=[], auth_gate=None)
    repository = FakeInsightRepository()
    repository.context.update(status="review", target_platform="app", ruleset_rule_total=73)
    repository.context["task_config"].update(
        workflow_version="v2", execution_mode="full_inspection"
    )
    repository.execution_rows = [
        _execution(
            f"R{index:02}",
            "skipped",
            rule_id=f"rule-{index}",
            page_capture_id=None,
            execution_reason="exploration_not_started",
        )
        for index in range(73)
    ] + [
        _execution(
            f"R{index:02}",
            "evidence_insufficient",
            rule_id=f"rule-{index}",
            page_capture_id=None,
            error_code="AGGREGATE_APPLICABILITY_UNPROVEN",
            judge_input={"source": "aggregate_candidate_disposition"},
        )
        for index in range(70, 73)
    ]

    class Reader:
        async def load_snapshot(self, **kwargs):
            return deepcopy(snapshot)

    service = TaskInsightService(repository=repository, rule_inventory_repository=Reader())
    result = await service.rule_results("task-1")
    assert result["used_rule_count"] == 0
    assert len(result["items"]) == 76
    assert all(item["execution_id"] for item in result["items"])
    assert result["items"][-1]["execution_reason"] == "AGGREGATE_APPLICABILITY_UNPROVEN"
    stats = await service.stats("task-1")
    assert stats["total"] == 73
    assert stats["triggered"] == stats["finished"] == stats["used_rule_count"] == 0
    assert stats["status_counts"] == {"not_executed": 73, "evidence_insufficient": 3}


def test_existing_page_candidate_is_enriched_without_duplicate_rule_or_fake_execution():
    fixture, snapshot = _fixture()
    items = fixture["rule_items"]
    for item in items:
        item["item_type"] = "execution"
    existing = {
        "item_type": "candidate",
        "execution_id": None,
        "rule_id": "rule-39",
        "rule_code": "R39",
        "rule_name": "现有页面候选",
        "display_status": "coverage_gap",
        "machine_display_status": "coverage_gap",
        "page_capture_id": "capture-existing",
        "bound_page_type": "home",
        "confirmable": False,
    }
    items.append(existing)
    result, summary = project_task_rule_inventory(items, snapshot, ruleset_rule_total=73)
    assert len([item for item in result if item["rule_id"] == "rule-39"]) == 1
    assert existing["page_capture_id"] == "capture-existing"
    assert existing["execution_id"] is None
    assert existing["machine_display_status"] == "not_executed"
    assert existing["reason_code"] == "eligible"
    assert len({item["rule_id"] for item in result}) == 73
    assert summary["used_rule_count"] == 20


def test_existing_aggregate_candidate_gets_verified_na_and_basis_without_execution():
    fixture, snapshot = _fixture()
    snapshot["aggregate_dispositions"] = [_aggregate_na()]
    items = fixture["rule_items"]
    for item in items:
        item["item_type"] = "execution"
    existing = {
        "item_type": "candidate",
        "execution_id": None,
        "rule_id": "rule-70",
        "display_status": "pending",
        "machine_display_status": "pending",
        "confirmable": False,
    }
    items.append(existing)
    result, summary = project_task_rule_inventory(items, snapshot, ruleset_rule_total=73)
    assert len([item for item in result if item["rule_id"] == "rule-70"]) == 1
    assert existing["execution_id"] is None
    assert existing["display_status"] == existing["machine_display_status"] == "not_applicable"
    assert existing["execution_reason"] == "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE"
    assert existing["reason_code"] == "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE"
    assert "app-scope-result-1:negative-evidence-1" in existing["problem_desc"]
    assert existing["confirmable"] is False
    assert summary["rule_inventory_pending_rules"] == 53
    assert summary["used_rule_count"] == 20
    assert len([item for item in result if item["execution_id"]]) == 343


def test_unverifiable_or_wrong_owner_inventory_never_creates_task_candidates():
    fixture, _ = _fixture()
    for item in fixture["rule_items"]:
        item["item_type"] = "execution"
    items, summary = project_task_rule_inventory(
        fixture["rule_items"],
        {
            "inventory_status": "unavailable",
            "inventory": None,
            "unavailable_reason": "run_owner_mismatch",
        },
        ruleset_rule_total=73,
    )
    assert len(items) == 343
    assert not any(item["item_type"] == "candidate" for item in items)
    assert summary["rule_inventory_complete"] is False
    assert summary["rule_inventory_unavailable_reason"] == "run_owner_mismatch"


def test_old_machine_pass_keeps_result_and_exposes_current_round_coverage_reason():
    fixture, snapshot = _fixture()
    snapshot["executions"] = []
    items = fixture["rule_items"]
    for item in items:
        item["item_type"] = "execution"
    original_result = items[0]["machine_display_status"]
    result, summary = project_task_rule_inventory(items, snapshot, ruleset_rule_total=73)
    original = next(item for item in result if item["execution_id"] == items[0]["execution_id"])
    assert original["machine_display_status"] == original_result == "passed"
    assert "未通过当前主运行或收口轮次关联核验" in original["inventory_coverage_reason"]
    assert summary["rule_inventory_complete"] is False
    assert len([item for item in result if item["execution_id"]]) == 343
