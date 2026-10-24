from copy import deepcopy

import pytest

from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    assess_rule_application,
    build_task_rule_inventory,
    inventory_candidate_rules,
    inventory_execution_contract_hash,
    parse_task_rule_inventory,
    requires_task_rule_inventory,
)


def rule(rule_id="rule-1", **changes):
    return {
        "rule_id": rule_id,
        "rule_code": rule_id,
        "name": "页面检查",
        "executor_type": "semantic",
        "severity": "low",
        "input_contract": {"target_clients": ["H5"]},
        "applicable_page_types": ["home"],
        "judge_config": {},
        "sort_order": 1,
        **changes,
    }


def inventory(rules, **changes):
    return build_task_rule_inventory(
        ruleset_id="set-1",
        ruleset_version=1,
        ruleset_snapshot_hash="snapshot-1",
        target_platform="h5",
        channel="workbench",
        task_config={"workflow_version": "v2"},
        rules=rules,
        **changes,
    )


def test_h5_full_inspection_requires_inventory_but_exploration_does_not():
    assert requires_task_rule_inventory("h5", {"workflow_version": "v2"})
    assert not requires_task_rule_inventory(
        "h5", {"workflow_version": "v2", "execution_mode": "exploration_only"}
    )
    assert not requires_task_rule_inventory("web", {"workflow_version": "v2"})


def test_inventory_keeps_all_rules_before_platform_filtering():
    frozen = inventory([rule(), rule("app-only", input_contract={"target_clients": ["APP"]})])
    assert frozen["rule_count"] == 2
    assert {r["rule_id"] for r in frozen["rules"]} == {"rule-1", "app-only"}
    candidates = inventory_candidate_rules(
        frozen,
        page_type="home",
        page_url="https://sample.test/",
        blocked_auth_requirement=None,
    )
    assert [r["rule_id"] for r in candidates] == ["rule-1"]


def test_rule_contract_is_frozen_not_aliased_to_live_rule():
    live = rule()
    frozen = inventory([live])
    live["input_contract"]["target_clients"] = ["APP"]
    live["name"] = "已修改"
    assert frozen["rules"][0]["name"] == "页面检查"
    assert parse_task_rule_inventory({"rule_inventory": frozen, "ruleset_rule_total": 1}) == frozen


def test_corrupt_inventory_is_rejected():
    frozen = deepcopy(inventory([rule()]))
    frozen["rules"][0]["name"] = "已修改"
    with pytest.raises(TaskRuleInventoryError):
        parse_task_rule_inventory({"rule_inventory": frozen})


def test_missing_login_is_unproven_not_not_applicable():
    application = assess_rule_application(
        rule(input_contract={"target_clients": ["H5"], "auth_requirement": "authenticated"}),
        target_platform="h5",
        channel="workbench",
        inspection_scope=[],
        blocked_auth_requirement="authenticated",
    )
    assert application == {
        "applicability": "unproven",
        "reason_code": "auth_requirement_not_met",
    }


def test_empty_page_scope_does_not_restrict_product_exploration():
    frozen = inventory([rule("new-page", applicable_page_types=["content_detail"])])
    assert frozen["inspection_scope"] == []
    assert frozen["target_page_types"] == ["content_detail"]


def test_url_filter_remains_part_of_rule_matching_not_product_allowlist():
    frozen = inventory([rule(url_pattern="*/detail/*")])
    assert not inventory_candidate_rules(
        frozen,
        page_type="home",
        page_url="https://sample.test/",
        blocked_auth_requirement=None,
    )


def test_duplicate_rule_identity_is_rejected():
    with pytest.raises(TaskRuleInventoryError):
        inventory([rule(), rule()])


@pytest.mark.parametrize(
    "captures,changes,reason",
    [
        ([], {}, "required_page_not_observed"),
        (
            [{"page_type": "home", "url": "https://sample.test/"}],
            {"url_pattern": "*/detail/*"},
            "rule_url_scope_not_matched",
        ),
        (
            [{"page_type": "home", "url": "https://sample.test/", "page_state": "stable"}],
            {"judge_config": {"engine_config": {"required_page_states": ["popup_visible"]}}},
            "required_page_state_not_observed",
        ),
        (
            [{"page_type": "home", "url": "https://sample.test/", "page_state": "stable"}],
            {},
            "inspection_work_not_published",
        ),
    ],
)
def test_h5_inventory_explains_trigger_stage_without_fabricating_execution(
    captures, changes, reason
):
    from services.api.app.application.report_rule_inventory import project_report_rule_inventory

    frozen = inventory([rule(**changes)])
    insight = {"summary": {}, "rule_items": []}
    project_report_rule_inventory(
        insight,
        {
            "inventory_status": "frozen",
            "inventory": frozen,
            "terminal": True,
            "capture_facts": captures,
            "executions": [],
            "work_rules": [],
        },
    )
    assert insight["rule_items"][0]["reason_code"] == reason
    assert insight["rule_items"][0]["execution_id"] is None
    assert insight["summary"]["rule_inventory_pending_rules"] == 1
    assert not insight["summary"]["rule_inventory_complete"]


def test_page_contract_matches_real_publisher_execution_route():
    from services.api.app.agents.tools.rule_tools import _candidate_rule_to_dict
    from services.api.app.application.inspection_work_items import (
        build_inspection_rule_contract_hash,
    )
    from services.api.app.infrastructure.db.task_repository import _candidate_rule_from_inventory

    frozen = inventory(
        [rule(category="visual", check_point="检查页面", steps=[], manual_review_policy={})]
    )
    source = frozen["rules"][0]
    candidate = _candidate_rule_from_inventory({**source, "match_reason": "applicable_page_types"})
    assert inventory_execution_contract_hash(source) == build_inspection_rule_contract_hash(
        _candidate_rule_to_dict(candidate)
    )


def test_aggregate_contract_matches_existing_publication_path():
    from services.api.app.application.aggregate_inspection_publication import (
        build_aggregate_rule_source,
    )
    from services.api.app.application.inspection_work_items import (
        build_inspection_rule_contract_hash,
    )

    source = rule(
        category="visual",
        check_point="检查页面",
        steps=[],
        manual_review_policy={},
        judge_config={
            "capability_config": {
                "evidence_input_scope": "task",
                "evidence_scope_key": "toast_style",
            }
        },
    )
    aggregate = build_aggregate_rule_source(source)
    assert inventory_execution_contract_hash(source) == build_inspection_rule_contract_hash(
        aggregate.rule_payload
    )


@pytest.mark.parametrize("proof", [None, "wrong-contract"])
def test_unverified_result_cannot_fill_frozen_inventory(proof):
    from services.api.app.application.report_rule_inventory import project_report_rule_inventory

    frozen = inventory([rule()])
    item = {
        "execution_id": "execution-1",
        "rule_id": "rule-1",
        "is_bootstrap": False,
        "machine_display_status": "passed",
        "display_status": "passed",
        "confirm_status": None,
    }
    insight = {"summary": {}, "rule_items": [item]}
    project_report_rule_inventory(
        insight,
        {
            "inventory_status": "frozen",
            "inventory": frozen,
            "terminal": True,
            "executions": [{"execution_id": "execution-1", "proof_contract_hash": proof}],
        },
    )
    assert not insight["summary"]["rule_inventory_complete"]
    assert insight["rule_items"][-1]["reason_code"] == "execution_lineage_unverified"
    assert insight["summary"]["used_rule_count"] == 0


def test_valid_result_and_publication_proof_complete_inventory():
    from services.api.app.application.report_rule_inventory import project_report_rule_inventory

    frozen = inventory([rule()])
    insight = {
        "summary": {},
        "rule_items": [
            {
                "execution_id": "execution-1",
                "rule_id": "rule-1",
                "is_bootstrap": False,
                "machine_display_status": "passed",
                "execution_status": "pass",
                "display_status": "passed",
                "confirm_status": None,
            }
        ],
    }
    project_report_rule_inventory(
        insight,
        {
            "inventory_status": "frozen",
            "inventory": frozen,
            "executions": [
                {
                    "execution_id": "execution-1",
                    "proof_contract_hash": inventory_execution_contract_hash(frozen["rules"][0]),
                }
            ],
        },
    )
    assert insight["summary"]["rule_inventory_complete"]
    assert insight["summary"]["rule_inventory_pending_rules"] == 0
    assert insight["summary"]["used_rule_count"] == 1


class InventoryReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    async def load_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.snapshot


async def test_h5_task_api_returns_all_rules_without_fabricating_executions():
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.tests.test_task_insight_service import FakeInsightRepository

    repository = FakeInsightRepository()
    repository.context.update(
        target_platform="h5",
        task_config={"workflow_version": "v2", "ruleset_rule_total": 2},
    )
    reader = InventoryReader(
        {
            "inventory_status": "frozen",
            "inventory": inventory(
                [
                    rule(),
                    rule("app-only", input_contract={"target_clients": ["APP"]}),
                ]
            ),
            "terminal": False,
            "executions": [],
            "work_rules": [],
            "capture_facts": [],
        }
    )
    result = await TaskInsightService(
        repository=repository,
        rule_inventory_repository=reader,
    ).rule_results("task-1")
    assert len(reader.calls) == 1
    assert result["rule_inventory_total"] == 2
    assert result["rule_inventory_pending_rules"] == 1
    assert {item["rule_id"] for item in result["items"]} == {"rule-1", "app-only"}
    assert all(item["execution_id"] is None for item in result["items"])


@pytest.mark.parametrize("platform", ["app", "web"])
async def test_other_platform_task_api_keeps_own_inventory_contract(platform):
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.tests.test_task_insight_service import FakeInsightRepository

    repository = FakeInsightRepository()
    repository.context.update(target_platform=platform, task_config={"workflow_version": "v2"})
    reader = InventoryReader(None)
    result = await TaskInsightService(
        repository=repository,
        rule_inventory_repository=reader,
    ).rule_results("task-1")
    if platform == "app":
        assert reader.calls == [{"task_id": "task-1"}]
        assert result["rule_inventory_status"] == "unavailable"
    else:
        assert not reader.calls
        assert "rule_inventory_total" not in result


async def test_h5_report_lists_missing_rules_but_does_not_export_without_real_results():
    from services.api.app.application.report_insight import (
        ReportExportBlockedError,
        ReportInsightService,
    )
    from services.api.app.application.report_rule_inventory import append_report_rule_inventory
    from services.api.tests.test_report_insight import FakeEvidenceStore, FakeReportRepository

    repository = FakeReportRepository()
    repository.context.update(
        target_platform="h5",
        task_config={"workflow_version": "v2", "ruleset_rule_total": 1},
    )
    reader = InventoryReader(
        {
            "inventory_status": "frozen",
            "inventory": inventory([rule()]),
            "terminal": True,
            "executions": [],
            "work_rules": [],
            "capture_facts": [],
        }
    )
    service = ReportInsightService(
        repository=repository,
        evidence_store_factory=lambda: FakeEvidenceStore({}),
        rule_inventory_repository=reader,
    )
    result = await service.report_insight("task-1")
    assert result["summary"]["rule_inventory_total"] == 1
    assert result["summary"]["rule_inventory_pending_rules"] == 1
    assert result["rule_items"][0]["reason_code"] == "required_page_not_observed"
    assert result["rule_items"][0]["execution_id"] is None
    with pytest.raises(ReportExportBlockedError):
        await service.export_html("task-1")
    html = append_report_rule_inventory("<html><body></body></html>", result)
    assert "全规则应用清单" in html
    assert "required_page_not_observed" in html
