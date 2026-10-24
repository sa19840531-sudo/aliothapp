"""结果读侧受控载荷回归；不把伪造JSON当正式采集或正式PG写入。"""

import copy
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.green_rule_result_projection import (
    apply_green_results,
    green_result_identity,
    project_green_subitems,
    selected_green_rule_ids,
    selected_green_rule_keys,
    validate_green_execution,
)
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.application.task_rule_inventory import inventory_execution_contract_hash
from services.api.tests.test_green_page_writeback import _actual_navigation_result
from services.api.tests.test_green_process_judge import judge, process_case
from services.api.tests.test_green_shared_rule_judge import definition, service


async def page_case(key="32"):
    if key == "32":
        rule, result = await _actual_navigation_result()
    else:
        configured = definition(key)
        judge, request, _reader = service(configured)
        result = await judge.judge(request)
        rule = {"rule_id": configured.rule_id, **build_green_rule_payload(key)}
    payload = json.loads(json.dumps(asdict(result)))
    contract_hash = inventory_execution_contract_hash(rule)
    task = {
        "task_id": "task-1",
        "source_run_id": "source-run",
        "read_epoch": 0,
        "target_platform": "app",
    }
    execution = {
        "id": "execution-1",
        "task_id": "task-1",
        "rule_id": rule["rule_id"],
        "agent_run_id": "consumer-run",
        "page_capture_id": result.page_capture_id,
        "observation_packet_id": result.observation_packet_id,
        "execution_status": result.execution_status,
        "judge_output": copy.deepcopy(payload["judge_output"]),
        "judge_input": {"expected_contract_hash": contract_hash},
        "evidence_artifact_ids": payload["evidence_artifact_ids"],
    }
    packet = {
        "id": result.observation_packet_id,
        "task_id": "task-1",
        "rule_id": rule["rule_id"],
        "created_by_agent_run_id": "consumer-run",
        "page_capture_id": result.page_capture_id,
        "evidence_artifact_ids": payload["evidence_artifact_ids"],
        "packet_payload": {
            "identity": {
                "source_agent_run_id": "source-run",
                "consumer_agent_run_id": "consumer-run",
            },
            "page": {"page_capture_id": result.page_capture_id},
        },
    }
    row = {
        "execution_payload": execution,
        "packet_payload": packet,
        "consumer_task_id": "task-1",
        "page_work_payload": {
            "id": "work-1",
            "task_id": "task-1",
            "producer_agent_run_id": "source-run",
            "finalization_epoch": 0,
            "control_mode": "enforced",
            "status": "succeeded",
            "page_capture_id": result.page_capture_id,
            "attempt_count": 1,
            "input_hash": "input-hash",
        },
        "page_member_payload": {
            "work_item_id": "work-1",
            "rule_id": rule["rule_id"],
            "rule_code": rule["rule_code"],
            "contract_hash": contract_hash,
            "status": "written",
            "rule_execution_id": "execution-1",
            "observation_packet_id": result.observation_packet_id,
        },
        "page_attempt_payload": {
            "work_item_id": "work-1",
            "attempt_no": 1,
            "consumer_agent_run_id": "consumer-run",
            "status": "written",
            "input_hash": "input-hash",
            "result_payload": {"items": [payload]},
        },
        "review_payload": {
            "task_id": "task-1",
            "rule_execution_id": "execution-1",
            "status": "pending",
            "review_reason": "其他材料待补充",
        },
    }
    materials = [
        {
            "artifact_id": artifact,
            "artifact_type": kind,
            "capture_id": result.page_capture_id,
            "png_verified": False,
        }
        for artifact, kind in (
            (
                artifact,
                {"ui-original": "ui_snapshot", "dom-original": "dom"}.get(artifact, "visible_text"),
            )
            for artifact in result.evidence_artifact_ids
        )
    ]
    return task, rule, row, materials


@pytest.mark.parametrize("key", ["102", "41"])
async def test_actual_page_preflight_missing_subitems_are_linked_without_business_promotion(key):
    task, rule, row, materials = await page_case(key)
    validated = validate_green_execution(task=task, rule=rule, row=row)
    subitems = project_green_subitems(validated, materials)
    assert [item["key"] for item in subitems] == (
        rule["judge_config"]["green_rule_contract"]["subchecks"]
    )
    assert validated["execution"]["execution_status"] == "evidence_insufficient"
    assert validated["review_status"] == "pending" and validated["missing_evidence"]
    assert all(
        item["status"] == "evidence_insufficient"
        and item["evaluated"] is False
        and item["confidence"] is None
        and item["missing_evidence"]
        and item["evidence_artifact_ids"] == []
        and item["screenshot_artifact_ids"] == []
        for item in subitems
    )


@pytest.mark.parametrize("key", ["102", "41"])
async def test_missing_page_subitem_with_input_capture_and_png_has_no_criterion_image(key):
    task, rule, row, materials = await page_case(key)
    validated = validate_green_execution(task=task, rule=rule, row=row)
    for item in validated["raw_subitems"]:
        item["capture_ids"] = list(validated["capture_ids"])
    for material in materials:
        material.update(artifact_type="screenshot", png_verified=True)
    subitems = project_green_subitems(validated, materials)
    assert all(item["capture_ids"] for item in subitems)
    assert all(
        item["evaluated"] is False
        and item["evidence_artifact_ids"] == []
        and item["screenshot_artifact_ids"] == []
        for item in subitems
    )


def test_s35_pending_layout_and_stability_cannot_borrow_correct_response_images():
    fixture, evidence = process_case()
    subitems = [item.to_dict() for item in judge(fixture, evidence)]
    materials = [
        {
            "artifact_id": original.artifact_id,
            "artifact_type": original.artifact_type,
            "capture_id": capture.capture_id,
            "png_verified": False,
        }
        for capture in evidence.captures
        for original in capture.originals
    ]
    materials.extend(
        {
            "artifact_id": "png-" + capture.capture_id,
            "artifact_type": "screenshot",
            "capture_id": capture.capture_id,
            "png_verified": True,
        }
        for capture in evidence.captures
    )
    validated = {
        "contract": fixture.rule["judge_config"]["green_rule_contract"],
        "allowed_artifact_ids": [material["artifact_id"] for material in materials],
        "capture_ids": [capture.capture_id for capture in evidence.captures],
        "raw_subitems": subitems,
    }
    result = project_green_subitems(validated, materials)
    assert result[0]["status"] == "pass" and result[0]["evaluated"] is True
    assert result[0]["screenshot_artifact_ids"] == [
        "png-" + capture.capture_id for capture in evidence.captures
    ]
    assert all(item["capture_ids"] for item in result[1:])
    assert all(
        item["evaluated"] is False
        and item["evidence_artifact_ids"] == []
        and item["screenshot_artifact_ids"] == []
        for item in result[1:]
    )
    # 即使输入范围另带只有PNG的Capture，也不能越过判据引用补同组图片。
    extra = {
        "artifact_id": "input-only-png",
        "artifact_type": "screenshot",
        "capture_id": "input-only-capture",
        "png_verified": True,
    }
    materials.append(extra)
    validated["allowed_artifact_ids"].append(extra["artifact_id"])
    validated["capture_ids"].append(extra["capture_id"])
    validated["raw_subitems"][0]["capture_ids"].append(extra["capture_id"])
    assert project_green_subitems(validated, materials)[0]["screenshot_artifact_ids"] == (
        result[0]["screenshot_artifact_ids"]
    )


@pytest.mark.parametrize("platform", ["web", "h5"])
async def test_app_external_handoff_never_borrows_another_platform_execution(platform):
    task, rule, row, _materials = await page_case("41")
    task["target_platform"] = platform
    with pytest.raises(ValueError, match="App"):
        validate_green_execution(task=task, rule=rule, row=row)


@pytest.mark.parametrize("key", ["102", "s72", "41", "62", "s35", "s49", "s73", "32"])
def test_all_eight_explicit_contracts_are_selected_but_unexecuted_candidates_are_untagged(key):
    rule = {"rule_id": "green-rule-" + key, **build_green_rule_payload(key)}
    config = {"rule_inventory": {"rules": [rule]}}
    assert selected_green_rule_keys(config) == {rule["rule_id"]: key}
    assert green_result_identity(rule)["rule_key"] == key
    candidate = {"rule_id": rule["rule_id"], "execution_id": None}
    apply_green_results([candidate], {}, expected_rules={rule["rule_id"]: key})
    assert "green_result" not in candidate


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_ruleset_versions_are_never_selected_as_new_green_results(version):
    rule = {"rule_id": "old-rule", **build_green_rule_payload("102")}
    rule["judge_config"]["green_rule_contract"]["schema_version"] = "green-rule-contract.v1"
    rule["judge_config"]["green_rule_contract"]["ruleset_version"] = version
    assert selected_green_rule_keys({"rule_inventory": {"rules": [rule]}}) == {}


async def test_real_32_subresults_are_allowlisted_without_inventing_a_png():
    task, rule, row, materials = await page_case()
    validated = validate_green_execution(task=task, rule=rule, row=row)
    subitems = project_green_subitems(validated, materials)
    assert subitems[0]["status"] == "fail" and subitems[0]["evaluated"] is True
    assert subitems[0]["evidence_artifact_ids"] == ["ui-original", "dom-original"]
    assert subitems[0]["screenshot_artifact_ids"] == [] and subitems[0]["evidence_notice"]
    assert subitems[0]["capture_ids"] == ["capture-1"]
    assert "storage_path" not in json.dumps(subitems)
    assert all("observations" not in item for item in subitems)


@pytest.mark.parametrize(
    "fault",
    [
        "task",
        "run",
        "consumer",
        "epoch",
        "contract",
        "packet",
        "attempt",
        "attempt_subitem",
        "attempt_evidence",
        "subitem_capture",
        "subitem_evidence",
    ],
)
async def test_wrong_lineage_or_changed_attempt_does_not_expose_a_subitem(fault):
    task, rule, row, materials = await page_case()
    if fault == "task":
        row["execution_payload"]["task_id"] = "foreign-task"
    elif fault == "run":
        row["page_work_payload"]["producer_agent_run_id"] = "old-run"
    elif fault == "consumer":
        row["consumer_task_id"] = "foreign-task"
    elif fault == "epoch":
        row["page_work_payload"]["finalization_epoch"] = 1
    elif fault == "contract":
        row["page_member_payload"]["contract_hash"] = "old-contract"
    elif fault == "packet":
        row["packet_payload"]["created_by_agent_run_id"] = "foreign-run"
    elif fault == "attempt":
        row["page_attempt_payload"]["attempt_no"] = 2
    elif fault == "attempt_subitem":
        row["page_attempt_payload"]["result_payload"]["items"][0]["judge_output"]["subresults"] = []
    elif fault == "attempt_evidence":
        row["page_attempt_payload"]["result_payload"]["items"][0]["evidence_artifact_ids"] = []
    elif fault == "subitem_capture":
        row["execution_payload"]["judge_output"]["subresults"][0]["capture_ids"] = [
            "foreign-capture"
        ]
    else:
        row["execution_payload"]["judge_output"]["subresults"][0]["evidence_refs"] = [
            "foreign-artifact"
        ]
    with pytest.raises((ValueError, KeyError)):
        validated = validate_green_execution(task=task, rule=rule, row=row)
        project_green_subitems(validated, materials)


@pytest.mark.parametrize("review", ["pending", "approved", "rejected", "needs_recheck"])
async def test_current_review_status_never_rewrites_machine_subitems(review):
    task, rule, row, _materials = await page_case()
    row["execution_payload"]["review_status"] = "pending"
    row["review_payload"]["status"] = review
    validated = validate_green_execution(task=task, rule=rule, row=row)
    assert validated["review_status"] == review
    assert validated["execution"]["execution_status"] == "fail"
    assert validated["raw_subitems"][0]["status"] == "fail"


@pytest.mark.parametrize("linkage", ["input_mismatch", "evidence_changed", "missing_result"])
def test_unlinked_green_public_summary_is_unfinished_and_keeps_machine_audit(linkage):
    item = {
        "execution_id": "e",
        "execution_status": "pass",
        "display_status": "passed",
        "machine_display_status": "passed",
        "confirmable": True,
        "confirm_status": "pending",
    }
    apply_green_results([item], {"e": {"linkage_status": linkage}})
    assert (
        item["display_status"] == "manual_review" and item["display_status_label"] == "关联待核对"
    )
    assert item["execution_status"] == "pass" and item["machine_display_status"] == "passed"
    assert item["confirmable"] is False and item["confirm_status"] is None


@pytest.mark.parametrize(
    "machine,review,expected",
    [
        ("fail", "pending", "failed"),
        ("pass", "pending", "manual_review"),
        ("fail", "approved", "failed"),
        ("evidence_insufficient", "approved", "evidence_insufficient"),
    ],
)
def test_linked_review_uses_existing_display_rules_without_business_promotion(
    machine, review, expected
):
    item = {"execution_id": "e", "execution_status": machine, "execution_reason": None}
    green = {"linkage_status": "linked", "review_status": review, "review_reason": "当前复核原因"}
    apply_green_results([item], {"e": green})
    assert item["display_status"] == expected and item["review_status"] == review
    assert item["execution_status"] == machine


@pytest.mark.parametrize("gap", ["parent_missing", "child_missing", "child_unevaluated"])
@pytest.mark.parametrize(
    "state,display", [("fail", "failed"), ("manual_review", "evidence_insufficient")]
)
def test_approved_human_pass_cannot_override_machine_failure_or_unproven_materials(
    gap, state, display
):
    item = {
        "execution_id": "execution-1", "execution_status": state,
        "human_verdict": "pass", "confirmable": True, "confirm_status": "confirmed",
    }
    green = {
        "linkage_status": "linked", "execution_status": state,
        "review_status": "approved", "review_reason": "已核对但业务材料仍不完整",
        "missing_evidence": [],
        "subitems": [{"status": "fail", "evaluated": True, "missing_evidence": []}],
    }
    if gap == "parent_missing":
        green["missing_evidence"] = ["controlled_missing_fact"]
    elif gap == "child_missing":
        green["subitems"][0]["missing_evidence"] = ["controlled_missing_fact"]
    else:
        green["subitems"].append({"status": "skipped", "evaluated": False})
    apply_green_results([item], {"execution-1": green})
    assert item["display_status"] == display
    assert item["execution_status"] == state and item["human_verdict"] == "pass"
    assert item["green_result"]["review_status"] == "approved"
    assert item["confirmable"] is False and item["confirm_status"] is None


def test_genuine_approved_business_review_without_material_gaps_keeps_human_final_verdict():
    item = {
        "execution_id": "execution-1", "execution_status": "manual_review",
        "human_verdict": "pass", "confirmable": True,
    }
    green = {
        "linkage_status": "linked", "execution_status": "manual_review",
        "review_status": "approved", "review_reason": "人工解决真实业务语义歧义",
        "missing_evidence": [],
        "subitems": [{"status": "manual_review", "evaluated": True, "missing_evidence": []}],
    }
    apply_green_results([item], {"execution-1": green})
    assert item["display_status"] == "passed"
    assert item["execution_status"] == "manual_review" and item["human_verdict"] == "pass"


async def test_frozen_old_rule_with_green_looking_output_is_not_tagged():
    _, rule, _, _ = await page_case()
    old = copy.deepcopy(rule)
    old.update(rule_code="alioth_3", judge_config={})
    assert green_result_identity(old) is None


@pytest.mark.parametrize(
    "linkage", ["input_mismatch", "evidence_changed", "missing_result", "missing_projection"]
)
async def test_service_public_filter_and_counts_do_not_count_unlinked_green_as_passed(linkage):
    _, rule, _, _ = await page_case()
    projection = {**green_result_identity(rule), "linkage_status": linkage}
    repository = SimpleNamespace(
        read_green_rule_results=AsyncMock(
            return_value={} if linkage == "missing_projection" else {"execution-1": projection}
        )
    )
    service = TaskInsightService(repository=repository)
    service._require_context = AsyncMock(
        return_value={
            "status": "running",
            "target_platform": "web",
            "task_config": {
                "rule_inventory": {"rules": [rule]},
            },
        }
    )
    service._assemble_base_rule_items = AsyncMock(
        return_value=[
            {
                "execution_id": "execution-1",
                "rule_id": rule["rule_id"],
                "rule_code": "live-rule-renamed-after-submit",
                "execution_status": "pass",
                "display_status": "passed",
                "machine_display_status": "passed",
                "bound_page_type": "home",
            }
        ]
    )
    result = await service.rule_results("task-1")
    assert result["status_counts"] == {"manual_review": 1}
    assert result["items"][0]["display_status_label"] == "关联待核对"
    assert result["items"][0]["execution_status"] == "pass"
    repository.read_green_rule_results.assert_awaited_once_with(
        task_id="task-1",
        execution_ids=["execution-1"],
        access_scope=None,
    )


@pytest.mark.parametrize(
    "config",
    [
        {"rule_inventory": [1]},
        {"rule_inventory": {"rules": "invalid"}},
        {"rule_inventory": {"rules": [{"judge_config": [1]}]}},
        {"rule_inventory": {"rules": [{"judge_config": {"green_rule_contract": [1]}}]}},
        {
            "rule_inventory": {
                "rules": [
                    {
                        "judge_config": {
                            "green_rule_contract": {
                                "schema_version": "green-rule-contract.v2",
                                "rule_key": [],
                            }
                        }
                    }
                ]
            }
        },
        {
            "rule_inventory": {
                "rules": [
                    {
                        "judge_config": {
                            "green_rule_contract": {
                                "schema_version": "green-rule-contract.v2",
                                "rule_key": {},
                            }
                        }
                    }
                ]
            }
        },
    ],
)
def test_malformed_old_web_configuration_never_breaks_extra_green_selection(config):
    assert selected_green_rule_ids(config) == set()


@pytest.mark.parametrize("payload", [None, {}])
async def test_page_declaring_an_input_set_cannot_fallback_when_payload_is_missing(payload):
    task, rule, row, _ = await page_case()
    row["execution_payload"]["evidence_input_set_id"] = "declared-formal-input"
    row["input_set_payload"] = payload
    with pytest.raises(ValueError):
        validate_green_execution(task=task, rule=rule, row=row)


async def test_orphaned_pending_execution_does_not_look_as_if_review_was_unnecessary():
    task, rule, row, _ = await page_case()
    row["execution_payload"]["review_status"] = "pending"
    row["review_payload"] = None
    with pytest.raises(ValueError, match="复核"):
        validate_green_execution(task=task, rule=rule, row=row)


async def test_duplicate_formal_execution_clears_previously_linked_verdict_and_review(monkeypatch):
    from services.api.app.infrastructure.db import green_rule_results as reader

    task, rule, row, materials = await page_case()
    task_row = {
        **task,
        "task_config": {},
        "ruleset_id": "ruleset-1",
        "target_platform": "web",
        "channel": "workbench",
        "finalization_epoch": 0,
    }
    inventory = {key: task_row[key] for key in ("ruleset_id", "target_platform", "channel")}
    inventory["rules"] = [rule]
    monkeypatch.setattr(reader, "parse_task_rule_inventory", lambda _: inventory)
    monkeypatch.setattr(reader, "read_green_result_materials", AsyncMock(return_value=materials))
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value=task_row),
        fetch=AsyncMock(return_value=[copy.deepcopy(row), copy.deepcopy(row)]),
    )
    result = (
        await reader.read_green_rule_results(
            connection,
            task_id=task["task_id"],
            execution_ids=["execution-1"],
            artifact_store=None,
        )
    )["execution-1"]
    assert result["linkage_status"] == "input_mismatch"
    assert result["execution_status"] is None
    assert result["review_status"] is None and result["review_reason"] is None
    assert result["missing_evidence"] == [] and result["subitems"] == []
