"""全规则报告清单必须逐条对账，未执行处置不能冒充真实执行。"""

from collections import Counter
from copy import deepcopy

import pytest

from services.api.app.application.report_rule_inventory import (
    append_report_rule_inventory,
    project_report_rule_inventory,
)
from services.api.app.application.task_rule_inventory import build_task_rule_inventory


def _fixture():
    rules = []
    for index in range(73):
        rules.append(
            {
                "rule_id": f"rule-{index}",
                "sort_order": index,
                "rule_code": f"R{index:02}",
                "name": f"规则{index}",
                "applicable_page_types": ["wallet" if 22 <= index < 35 else "home"],
                "executor_type": "semantic",
                "severity": "high",
                "category": "ad",
                "scope": "task" if index >= 70 else "page",
                "channel": None,
                "input_contract": {
                    "target_clients": ["WEB"] if 20 <= index < 22 else ["APP"],
                    **({"auth_requirement": "authenticated"} if 35 <= index < 39 else {}),
                },
            }
        )
    inventory = build_task_rule_inventory(
        ruleset_id="ruleset-1",
        ruleset_version=1,
        ruleset_snapshot_hash="hash",
        target_platform="app",
        channel="workbench",
        rules=rules,
        task_config={"inspection_scope": {"page_types": ["home"]}},
    )
    items = []
    for index, count, machine, display in (
        [
            (index, 7 if index == 0 else 6 if index < 5 else 5, "passed", "passed")
            for index in range(11)
        ]
        + [
            (index, 23 if index == 3 else 16, "evidence_insufficient", "other")
            for index in range(3, 19)
        ]
        + [(19, 19, "not_applicable", "not_applicable")]
    ):
        for attempt in range(count):
            items.append(
                {
                    "execution_id": f"exec-{index}-{machine}-{attempt}",
                    "rule_id": f"rule-{index}",
                    "rule_code": f"R{index:02}",
                    "rule_name": f"规则{index}",
                    "machine_display_status": machine,
                    "execution_status": {"passed": "pass", "not_applicable": "skipped"}.get(
                        machine, machine
                    ),
                    "display_status": display,
                    "confirm_status": None,
                }
            )
    insight = {
        "summary": {"ruleset_rule_total": 73, "total_rules": 343, "qc_success_rate": 23.3},
        "qc_success_rate": 23.3,
        "rule_items": items,
        "confirm_items": [],
        "distribution": [],
    }
    snapshot = {
        "inventory_status": "frozen",
        "inventory": inventory,
        "auth_gate": "authenticated",
        "agent_run_id": "owner-1",
        "finalization_epoch": 2,
        "terminal": True,
        "executions": deepcopy(items),
        "work_rules": [],
    }
    return insight, snapshot


def _aggregate_na(rule_id="rule-70", **changes):
    return {
        "rule_id": rule_id,
        "candidate_id": "aggregate-candidate-1",
        "status": "not_applicable",
        "reason_code": "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE",
        "reason": "已验证产品无此规则声明的适用页型",
        "evidence_refs": ["app-scope-result-1:negative-evidence-1"],
        **changes,
    }


@pytest.mark.parametrize("rule_index", [20, 22])
@pytest.mark.parametrize("status,machine,display,pending", [
    ("evidence_insufficient", "evidence_insufficient", "other", 1),
    ("error", "detect_error", "detect_error", 1),
    ("pass", "passed", "passed", 0),
])
def test_scope_dispositions_do_not_hide_current_real_execution_gaps(
    rule_index, status, machine, display, pending,
):
    insight, snapshot = _fixture()
    selected = snapshot["inventory"]["rules"][rule_index]
    snapshot["inventory"] = build_task_rule_inventory(
        ruleset_id="ruleset-1", ruleset_version=1, ruleset_snapshot_hash="hash",
        target_platform="app", channel="workbench", rules=[selected],
        task_config={"inspection_scope": {"page_types": ["home"]}},
    )
    execution = {
        "execution_id": "current-real-judge", "rule_id": selected["rule_id"],
        "execution_status": status, "execution_reason": None,
        "machine_display_status": machine, "display_status": display,
        "confirm_status": None,
    }
    original = deepcopy(execution)
    insight["rule_items"] = [execution]
    snapshot["executions"] = [deepcopy(execution)]
    project_report_rule_inventory(insight, snapshot)
    assert insight["summary"]["used_rule_count"] == 1
    assert insight["summary"]["rule_inventory_pending_rules"] == pending
    assert insight["summary"]["rule_inventory_complete"] is (pending == 0)
    assert next(row["count"] for row in insight["distribution"] if row["status"] == display) == 1
    assert len(insight["rule_items"]) == 1
    for key, value in original.items():
        assert execution[key] == value


@pytest.mark.parametrize("with_real_judge_gap", [False, True])
def test_startup_audit_records_preserve_ids_without_claiming_rule_usage(with_real_judge_gap):
    insight, snapshot = _fixture()
    items = [
        {
            "execution_id": f"startup-{index}",
            "rule_id": f"rule-{index}",
            "rule_code": f"R{index:02}",
            "rule_name": f"规则{index}",
            "execution_status": "skipped",
            "execution_reason": "exploration_not_started",
            "machine_display_status": "not_executed",
            "display_status": "other",
            "result_summary": "本次启动失败，未采集页面",
            "confirm_status": None,
        }
        for index in range(73)
    ]
    items.extend(
        {
            "execution_id": f"preflight-{index}",
            "rule_id": f"rule-{index}",
            "rule_code": f"R{index:02}",
            "rule_name": f"规则{index}",
            "execution_status": "evidence_insufficient",
            "execution_reason": "AGGREGATE_APPLICABILITY_UNPROVEN",
            "machine_display_status": "evidence_insufficient",
            "display_status": "other",
            "confirm_status": None,
        }
        for index in range(70, 73)
    )
    snapshot.update(executions=[], auth_gate=None)
    if with_real_judge_gap:
        items.append(
            {
                **items[0],
                "execution_id": "real-judge-gap",
                "execution_status": "evidence_insufficient",
                "execution_reason": None,
                "machine_display_status": "evidence_insufficient",
            }
        )
        snapshot["executions"] = [deepcopy(items[-1])]
    original = deepcopy(items)
    insight["rule_items"] = items
    project_report_rule_inventory(insight, snapshot)
    assert insight["summary"]["used_rule_count"] == int(with_real_judge_gap)
    assert insight["summary"]["rule_inventory_pending_rules"] == 58
    assert {row["status"]: row["count"] for row in insight["distribution"]} == {
        "passed": 0,
        "failed": 0,
        "not_applicable": 2,
        "coverage_gap": 13,
        "detect_error": 0,
        "other": 58,
    }
    assert [row["execution_id"] for row in insight["rule_items"] if row["execution_id"]] == [
        row["execution_id"] for row in original
    ]
    for old, projected in zip(original, insight["rule_items"], strict=False):
        assert projected["execution_status"] == old["execution_status"]
        assert projected["execution_reason"] == old["execution_reason"]
        assert projected["machine_display_status"] == old["machine_display_status"]
    html = append_report_rule_inventory("<body></body>", insight)
    assert "<td>本产品不适用（适用性核对，未进行 AI 判定）</td><td>0</td>" in html


def test_projected_coverage_placeholder_keeps_raw_evidence_status_without_counting_usage():
    insight, snapshot = _fixture()
    placeholder = {
        "execution_id": "verified-web-gap", "rule_id": "rule-0",
        "execution_status": "evidence_insufficient", "execution_reason": None,
        "machine_display_status": "coverage_gap", "display_status": "coverage_gap",
        "is_bootstrap": False, "confirm_status": None,
    }
    insight["rule_items"] = [placeholder]
    snapshot["executions"] = [deepcopy(placeholder)]
    project_report_rule_inventory(insight, snapshot)
    assert insight["summary"]["used_rule_count"] == 0
    assert placeholder["execution_id"] == "verified-web-gap"
    assert placeholder["execution_status"] == "evidence_insufficient"
    assert placeholder["is_bootstrap"] is False


def test_verified_aggregate_na_finishes_scope_check_without_inventing_execution():
    insight, snapshot = _fixture()
    snapshot["aggregate_dispositions"] = [_aggregate_na()]
    project_report_rule_inventory(insight, snapshot)
    item = next(item for item in insight["rule_items"] if item["rule_id"] == "rule-70")
    assert item["item_type"] == "candidate"
    assert item["execution_id"] is item["execution_status"] is None
    assert item["machine_display_status"] == item["display_status"] == "not_applicable"
    assert item["applicability"] == "not_applicable"
    assert item["reason_code"] == "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE"
    assert "app-scope-result-1:negative-evidence-1" in item["problem_desc"]
    assert "未进行 AI 判定" in item["result_summary"]
    assert item["confirmable"] is False
    assert insight["summary"]["rule_inventory_pending_rules"] == 53
    assert insight["summary"]["used_rule_count"] == 20
    assert insight["summary"]["total_rules"] == 343
    assert insight["summary"]["qc_success_rate"] == insight["qc_success_rate"] == 23.3
    assert len([row for row in insight["rule_items"] if row["execution_id"]]) == 343
    assert {row["status"]: row["count"] for row in insight["distribution"]} == {
        "passed": 3,
        "failed": 0,
        "not_applicable": 4,
        "coverage_gap": 13,
        "detect_error": 0,
        "other": 53,
    }
    html = append_report_rule_inventory("<body></body>", insight)
    assert "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE" in html
    assert "app-scope-result-1:negative-evidence-1" in html


def test_full_inventory_can_finish_on_verified_aggregate_na_without_ai_success():
    _, snapshot = _fixture()
    snapshot["inventory"] = build_task_rule_inventory(
        ruleset_id="ruleset-1",
        ruleset_version=1,
        ruleset_snapshot_hash="hash",
        target_platform="app",
        channel="workbench",
        task_config={},
        rules=[rule for rule in snapshot["inventory"]["rules"] if rule["rule_id"] == "rule-70"],
    )
    snapshot["executions"] = []
    snapshot["aggregate_dispositions"] = [_aggregate_na()]
    insight = {
        "summary": {"total_rules": 0, "qc_success_rate": 0},
        "qc_success_rate": 0,
        "rule_items": [],
    }
    project_report_rule_inventory(insight, snapshot)
    assert insight["summary"]["rule_inventory_complete"] is True
    assert insight["summary"]["rule_inventory_pending_rules"] == 0
    assert insight["summary"]["used_rule_count"] == insight["summary"]["total_rules"] == 0
    assert insight["summary"]["qc_success_rate"] == insight["qc_success_rate"] == 0
    assert insight["rule_items"][0]["execution_id"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "needs_review"},
        {"status": "evidence_insufficient"},
        {"reason_code": "AGGREGATE_PRESENTATION_EVIDENCE_REJECTED"},
        {"evidence_refs": []},
        {"reason": ""},
        {"candidate_id": None},
    ],
)
def test_unresolved_or_incomplete_aggregate_disposition_never_completes_rule(changes):
    insight, snapshot = _fixture()
    snapshot["aggregate_dispositions"] = [_aggregate_na(**changes)]
    project_report_rule_inventory(insight, snapshot)
    item = next(item for item in insight["rule_items"] if item["rule_id"] == "rule-70")
    assert item["display_status"] == "other"
    assert item["machine_display_status"] == "not_executed"
    assert insight["summary"]["rule_inventory_pending_rules"] == 54


def test_aggregate_na_does_not_hide_current_execution_with_evidence_gap():
    insight, snapshot = _fixture()
    snapshot["aggregate_dispositions"] = [_aggregate_na("rule-3")]
    original = deepcopy([item for item in insight["rule_items"] if item["rule_id"] == "rule-3"])
    project_report_rule_inventory(insight, snapshot)
    actual = [item for item in insight["rule_items"] if item["rule_id"] == "rule-3"]
    assert len(actual) == len(original)
    assert all(item["execution_id"] for item in actual)
    assert all(
        all(after[key] == value for key, value in before.items())
        for before, after in zip(original, actual, strict=True)
    )
    assert insight["summary"]["rule_inventory_pending_rules"] == 54


def test_current_aggregate_na_is_reported_while_old_run_execution_remains_unchanged():
    insight, snapshot = _fixture()
    snapshot["executions"] = [
        item for item in snapshot["executions"] if item["rule_id"] != "rule-3"
    ]
    snapshot["aggregate_dispositions"] = [_aggregate_na("rule-3")]
    original = deepcopy([item for item in insight["rule_items"] if item["rule_id"] == "rule-3"])
    project_report_rule_inventory(insight, snapshot)
    actual = [item for item in insight["rule_items"] if item["rule_id"] == "rule-3"]
    assert len(actual) == len(original) + 1
    assert actual[-1]["execution_id"] is None
    assert actual[-1]["display_status"] == "not_applicable"
    assert all(
        all(after[key] == value for key, value in before.items())
        for before, after in zip(original, actual, strict=False)
    )
    assert insight["summary"]["rule_inventory_pending_rules"] == 53
    assert insight["summary"]["used_rule_count"] == 20
    assert insight["summary"]["total_rules"] == 343
    assert insight["qc_success_rate"] == 23.3
    counts = {row["status"]: row["count"] for row in insight["distribution"]}
    assert counts["not_applicable"] == 4 and counts["other"] == 53
    html = append_report_rule_inventory("<body></body>", insight)
    rule_row = next(row for row in html.split("<tr>") if "<td>R03</td>" in row)
    assert "<td>本产品不适用</td>" in rule_row


def test_all_73_rules_have_auditable_disposition_without_inflating_343_executions():
    insight, snapshot = _fixture()
    original = deepcopy(insight["rule_items"])
    project_report_rule_inventory(insight, snapshot)
    items = insight["rule_items"]
    candidates = [item for item in items if item["item_type"] == "candidate"]
    assert len({item["rule_id"] for item in items}) == 73
    assert len([item for item in items if item["execution_id"]]) == 343
    assert len(candidates) == 53
    assert insight["summary"]["used_rule_count"] == 20
    assert insight["summary"]["total_rules"] == 343
    assert insight["qc_success_rate"] == insight["summary"]["qc_success_rate"] == 23.3
    assert sum(item["count"] for item in insight["distribution"]) == 73
    assert {item["status"]: item["count"] for item in insight["distribution"]} == {
        "passed": 3,
        "failed": 0,
        "not_applicable": 3,
        "coverage_gap": 13,
        "detect_error": 0,
        "other": 54,
    }
    assert Counter(item["applicability"] for item in candidates) == {
        "not_applicable": 2,
        "out_of_scope": 13,
        "unproven": 4,
        "applicable": 34,
    }
    assert all(
        item["execution_id"] is None and item["execution_status"] is None for item in candidates
    )
    assert all(
        item["confirmable"] is False and item["human_verdict"] is None for item in candidates
    )
    assert all(
        item["inventory_hash"]
        and item["rule_contract_hash"]
        and item["source_agent_run_id"] == "owner-1"
        for item in candidates
    )
    assert insight["summary"]["rule_inventory_complete"] is False
    assert insight["summary"]["rule_inventory_pending_rules"] == 54
    assert insight["summary"]["scope_excluded_rules"] == 13
    for before, after in zip(original, items, strict=False):
        assert all(after[key] == value for key, value in before.items())


def test_old_run_results_cannot_complete_current_owner_inventory():
    insight, snapshot = _fixture()
    snapshot["executions"] = []
    project_report_rule_inventory(insight, snapshot)
    assert len({item["rule_id"] for item in insight["rule_items"]}) == 73
    assert len([item for item in insight["rule_items"] if item["execution_id"]]) == 343
    assert len([item for item in insight["rule_items"] if item["item_type"] == "candidate"]) == 73
    assert insight["summary"]["rule_inventory_complete"] is False


def test_invalid_historical_inventory_is_visible_and_never_invented():
    insight, _ = _fixture()
    before = deepcopy(insight["rule_items"])
    project_report_rule_inventory(
        insight,
        {
            "inventory_status": "unavailable",
            "inventory": None,
            "unavailable_reason": "historical_rules_changed",
            "aggregate_dispositions": [_aggregate_na()],
        },
    )
    assert insight["rule_items"] == before
    assert insight["summary"]["rule_inventory_complete"] is False
    assert insight["summary"]["rule_inventory_unavailable_reason"] == "historical_rules_changed"


def test_verified_platform_and_scope_exclusions_finish_application_check_without_ai_success():
    _, snapshot = _fixture()
    snapshot["inventory"] = build_task_rule_inventory(
        ruleset_id="ruleset-1",
        ruleset_version=1,
        ruleset_snapshot_hash="hash",
        target_platform="app",
        channel="workbench",
        task_config={"inspection_scope": {"page_types": ["home"]}},
        rules=[
            rule
            for rule in snapshot["inventory"]["rules"]
            if rule["rule_id"] in {"rule-20", "rule-21", "rule-22"}
        ],
    )
    snapshot["executions"] = []
    insight = {
        "summary": {"total_rules": 0, "qc_success_rate": 0},
        "qc_success_rate": 0,
        "rule_items": [],
        "distribution": [],
    }
    project_report_rule_inventory(insight, snapshot)
    assert insight["summary"]["rule_inventory_complete"] is True
    assert insight["summary"]["rule_inventory_pending_rules"] == 0
    assert insight["summary"]["scope_excluded_rules"] == 1
    assert insight["summary"]["used_rule_count"] == 0
    assert insight["qc_success_rate"] == 0
    assert all(item["execution_id"] is None for item in insight["rule_items"])


def test_valid_empty_inventory_is_checked_without_claiming_ai_success():
    _, snapshot = _fixture()
    snapshot["inventory"] = build_task_rule_inventory(
        ruleset_id="empty",
        ruleset_version=1,
        ruleset_snapshot_hash="hash",
        target_platform="app",
        channel="workbench",
        task_config={},
        rules=[],
    )
    snapshot["executions"] = []
    insight = {"summary": {"total_rules": 0}, "rule_items": [], "qc_success_rate": 0}
    project_report_rule_inventory(insight, snapshot)
    assert insight["summary"]["rule_inventory_complete"] is True
    assert insight["summary"]["rule_inventory_total"] == 0
    assert insight["qc_success_rate"] == 0


def test_frozen_rule_names_and_codes_win_over_later_catalog_edits():
    insight, snapshot = _fixture()
    insight["rule_items"][0].update(rule_name="提交后改名", rule_code="提交后改编号")
    project_report_rule_inventory(insight, snapshot)
    assert insight["rule_items"][0]["rule_name"] == "规则0"
    assert insight["rule_items"][0]["rule_code"] == "R00"
    assert insight["rule_items"][0]["display_status"] == "passed"


def test_candidate_work_failure_and_running_candidate_are_disclosed_without_ai_verdict():
    insight, snapshot = _fixture()
    snapshot["terminal"] = False
    snapshot["work_rules"] = [
        {
            "rule_id": "rule-40",
            "rule_status": "error",
            "work_status": "failed",
            "work_item_id": "failed-work",
            "error_code": "INPUT_FAILED",
        }
    ]
    project_report_rule_inventory(insight, snapshot)
    by_rule = {item["rule_id"]: item for item in insight["rule_items"] if not item["execution_id"]}
    assert by_rule["rule-39"]["machine_display_status"] == "pending"
    assert by_rule["rule-40"]["machine_display_status"] == "detect_error"
    assert by_rule["rule-40"]["work_rule_facts"][0]["work_item_id"] == "failed-work"
    assert by_rule["rule-40"]["execution_status"] is None


def test_export_discloses_every_rule_and_unexecuted_applicability_without_fake_judge():
    insight, snapshot = _fixture()
    project_report_rule_inventory(insight, snapshot)
    html = append_report_rule_inventory("<html><body></body></html>", insight)
    assert html.count("<tr>") == 74
    assert "R72" in html and "R20" in html
    assert "适用性核对，未进行 AI 判定" in html
    assert "inspection_scope_excluded" in html and "auth_requirement_not_met" in html
    assert "未进行规则判定" in html


async def test_report_service_attaches_full_inventory_but_does_not_export_unsealed_preview():
    from services.api.app.application.report_insight import (
        ReportExportBlockedError,
        ReportInsightService,
    )
    from services.api.tests.test_report_insight import FakeReportRepository, _row

    fixture, snapshot = _fixture()
    repository = FakeReportRepository()
    repository.context.update(target_platform="app", ruleset_rule_total=73)
    repository.context["task_config"].update(
        workflow_version="v2", execution_mode="full_inspection"
    )
    repository.rows = [
        _row(
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

    service = ReportInsightService(repository=repository, rule_inventory_repository=Reader())
    insight = await service.report_insight("task-1")
    assert len({item["rule_id"] for item in insight["rule_items"]}) == 73
    assert insight["summary"]["total_rules"] == 343
    assert insight["summary"]["used_rule_count"] == 20
    assert insight["summary"]["qc_success_rate"] == 23.3
    assert all(item["execution_id"] for item in insight["confirm_items"])
    with pytest.raises(ReportExportBlockedError, match="封存"):
        await service.export_html("task-1")
