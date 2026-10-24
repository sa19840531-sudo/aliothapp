"""W46/W47 H5 验收资产契约测试。"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

from services.api.app.domain.page.registry import get_default_page_type_registry

ROOT = Path(__file__).resolve().parents[3]
SCENARIO_PATH = ROOT / "docs/data/h5_acceptance_scenarios.csv"
VERIFY_SCRIPT_PATH = ROOT / "scripts/verify_h5_acceptance_run.py"
SCENARIO_VERIFY_SCRIPT_PATH = ROOT / "scripts/verify_h5_acceptance_scenarios.py"
QC_WORKFLOW_SKILL_PATH = ROOT / "services/api/app/agents/skills/qc-workflow/SKILL.md"
PAGE_DETECTION_SKILL_PATH = ROOT / "services/api/app/agents/skills/phase/page-detection/SKILL.md"
PAGE_EXPLORATION_SKILL_PATH = (
    ROOT / "services/api/app/agents/skills/phase/page-exploration/SKILL.md"
)


def _load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_h5_acceptance_run", VERIFY_SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_scenario_verify_module():
    spec = importlib.util.spec_from_file_location(
        "verify_h5_acceptance_scenarios",
        SCENARIO_VERIFY_SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scenario_rows() -> list[dict[str, str]]:
    with SCENARIO_PATH.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def test_h5_acceptance_scenarios_cover_mainline_and_recovery_cases() -> None:
    rows = _scenario_rows()
    scenario_ids = {row["scenario_id"] for row in rows}

    assert len(rows) >= 22
    assert {
        "W46-H5-003",
        "W46-H5-005",
        "W46-H5-011",
        "W46-H5-012",
        "W46-H5-013",
        "W46-H5-014",
        "W46-H5-015",
        "W46-H5-016",
        "W46-H5-017",
        "W46-H5-104",
        "W46-H5-105",
    }.issubset(scenario_ids)
    assert all(row["ruleset_code"] == "alioth_first_ruleset" for row in rows)
    assert all(row["ruleset_version"] == "1" for row in rows)


def test_h5_line_loading_popup_chain_contract_is_consistent_across_skills() -> None:
    skill_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            QC_WORKFLOW_SKILL_PATH,
            PAGE_DETECTION_SKILL_PATH,
            PAGE_EXPLORATION_SKILL_PATH,
        )
    )

    assert "same_session_reobserve" in skill_text
    assert "入口首屏" in skill_text
    assert "首页弹窗链" in skill_text
    stale_line_loading_gate = (
        "line_loading` 等瞬态页 | `false` | 不进入 rule-matching；"
        "当前版本由运行时按 `evidence_insufficient` 收口"
    )
    assert "line_loading/loading --补采暂缓，证据不足收口" not in skill_text
    assert stale_line_loading_gate not in skill_text


def test_h5_acceptance_scenarios_pass_contract_validator() -> None:
    validator = _load_scenario_verify_module()

    issues = validator.validate_scenarios(_scenario_rows())

    assert issues == []


def test_h5_acceptance_control_plane_checks_require_current_authority() -> None:
    verifier = _load_verify_module()
    task = {"status": "done"}
    timeline = {
        "h5_coverage": {
            "authority_source": "h5_page_type_assessment",
            "revision": 3,
            "payload_hash": "a" * 64,
            "completion_ready": True,
            "missing_page_types": [],
        },
        "h5_evidence_inventory": {
            "items": [
                {
                    "inventory_item_id": "inventory-1",
                    "status": "pass",
                    "authority_source": "pass_manifest",
                    "before_manifest_id": "manifest-before",
                    "after_manifest_id": "manifest-after",
                }
            ]
        },
        "full_inspection_closure": {
            "nonterminal_inspection_count": 0,
            "gate_passed": True,
            "publication_projection": {
                "outbox_pending_count": 0,
                "outbox_dead_letter_count": 0,
                "inbox_pending_count": 0,
                "inbox_dead_letter_count": 0,
            },
        },
    }

    assert verifier._check_h5_coverage(task, timeline).passed is True
    assert verifier._check_h5_inventory_authority(task, timeline).passed is True
    assert verifier._check_h5_delivery_closure(task, timeline).passed is True

    timeline["h5_coverage"]["missing_page_types"] = ["payment"]
    timeline["h5_evidence_inventory"]["items"][0]["authority_source"] = "legacy_object_verified"
    assert verifier._check_h5_coverage(task, timeline).passed is False
    assert verifier._check_h5_inventory_authority(task, timeline).passed is False

    timeline["full_inspection_closure"] = {}
    assert verifier._check_h5_delivery_closure(task, timeline).passed is False
    timeline["execution_mode"] = "exploration_only"
    assert verifier._check_h5_delivery_closure(task, timeline).passed is True


def test_h5_acceptance_requires_report_and_timeline_terminal_consistency() -> None:
    verifier = _load_verify_module()
    report = {
        "page_exploration": {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "work_bounded",
        }
    }
    timeline = {
        "exploration_summary": {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "work_bounded",
        },
        "terminal_reason": "work_bounded",
    }

    check = verifier._check_report_timeline_terminal_consistency(report, timeline)

    assert check.passed is True
    assert check.code == "H5_TERMINAL_CONSISTENCY"


def test_h5_acceptance_rejects_report_timeline_terminal_mismatch() -> None:
    verifier = _load_verify_module()
    report = {
        "page_exploration": {
            "complete": True,
            "quality_status": "complete",
            "requires_review": False,
            "terminal_reason": None,
        }
    }
    timeline = {
        "exploration_summary": {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "work_bounded",
        },
        "terminal_reason": "work_bounded",
    }

    check = verifier._check_report_timeline_terminal_consistency(report, timeline)

    assert check.passed is False
    assert "complete" in check.message
    assert "terminal_reason" in check.message


def test_h5_acceptance_rejects_timeline_summary_and_compatibility_mismatch() -> None:
    verifier = _load_verify_module()
    report = {
        "page_exploration": {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "work_bounded",
        }
    }
    timeline = {
        "exploration_summary": {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "work_bounded",
        },
        "terminal_reason": "work_blocked",
    }

    check = verifier._check_report_timeline_terminal_consistency(report, timeline)

    assert check.passed is False
    assert "timeline_terminal_reason" in check.message


def test_h5_acceptance_scenario_validator_rejects_unknown_page_type() -> None:
    validator = _load_scenario_verify_module()
    rows = _scenario_rows()
    rows[0] = {**rows[0], "expected_page_types": "home,unknown_page_type"}

    issues = validator.validate_scenarios(rows)

    assert any(issue.code == "UNKNOWN_LIST_VALUE" for issue in issues)


def test_h5_acceptance_scenario_validator_rejects_duplicate_product_code() -> None:
    validator = _load_scenario_verify_module()
    rows = _scenario_rows()
    rows[1] = {**rows[1], "product_code": rows[0]["product_code"]}

    issues = validator.validate_scenarios(rows)

    assert any(issue.code == "DUPLICATED_FIELD" for issue in issues)


def test_h5_acceptance_scenario_validator_rejects_unknown_artifact_kind() -> None:
    validator = _load_scenario_verify_module()
    rows = _scenario_rows()
    rows[0] = {**rows[0], "required_artifacts": "screenshot,unknown_artifact"}

    issues = validator.validate_scenarios(rows)

    assert any(issue.code == "UNKNOWN_LIST_VALUE" for issue in issues)


def test_h5_acceptance_scenarios_reference_catalog_page_types() -> None:
    registry = get_default_page_type_registry()
    allowed_types = set(registry.all_codes)
    allowed_phase_only = {
        "auth_gate",
        "popup_chain",
        "system_dialog",
    }

    for row in _scenario_rows():
        for field in ("expected_page_types", "expected_rule_scope"):
            values = [item.strip() for item in row[field].split(",") if item.strip()]
            assert values, (row["scenario_id"], field)
            for page_type in values:
                assert page_type in allowed_types, (row["scenario_id"], field, page_type)

        phases = [
            item.strip() for item in row["expected_surface_phases"].split(",") if item.strip()
        ]
        assert phases, row["scenario_id"]
        for phase in phases:
            assert phase in allowed_types | allowed_phase_only, (row["scenario_id"], phase)


def test_h5_acceptance_verifier_accepts_complete_payloads() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "done",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "total_cost": 0.00016,
                "cost_currency": "CNY",
            },
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-1",
                    "evidence_artifact_ids": ["artifact-1"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 1,
            },
        },
    )

    assert all(check.passed for check in checks)


def test_h5_acceptance_verifier_rejects_fake_done_without_evidence() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "done",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": None,
                    "evidence_artifact_ids": [],
                },
                {
                    "rule_code": "alioth_2",
                    "execution_status": "skipped",
                    "source": "bootstrap",
                },
            ]
        },
        report={
            "report_stage": "preview",
            "summary": {"overall_status": "incomplete"},
            "page_exploration": {
                "tracking_available": True,
                "complete": False,
                "page_total": 0,
            },
        },
    )

    failed_codes = {check.code for check in checks if not check.passed}
    assert {
        "REPORT_READY",
        "RULE_EVIDENCE",
        "PAGE_EXPLORATION",
    }.issubset(failed_codes)


def test_h5_acceptance_verifier_accepts_task_scope_evidence() -> None:
    verifier = _load_verify_module()

    check = verifier._check_rule_execution_evidence(
        [
            {
                "rule_code": "alioth_ops_7",
                "execution_status": "fail",
                "page_capture_id": None,
                "evidence_input_set_id": "input-set-1",
                "observation_packet_id": "packet-1",
                "evidence_artifact_ids": ["artifact-1"],
            }
        ]
    )

    assert check.passed is True


def test_h5_acceptance_verifier_rejects_false_complete_dynamic_coverage() -> None:
    verifier = _load_verify_module()

    check = verifier._check_page_exploration(
        {"status": "review"},
        {
            "summary": {
                "overall_status": "incomplete",
                "task_goal_completed": False,
            },
            "page_exploration": {
                "tracking_available": True,
                "complete": False,
                "quality_status": "needs_review",
                "page_total": 4,
                "pending_page_count": 4,
                "pending_work_count": 4,
                "coverage": {"acceptance_complete": True},
            },
        },
    )

    assert check.passed is False
    assert "动态覆盖" in check.message


def test_h5_acceptance_verifier_accepts_fail_closed_review_run() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "review",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "failed",
            "termination_reason": "incomplete",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_18",
                    "execution_status": "pass",
                    "source": "agent",
                    "page_capture_id": "capture-1",
                    "evidence_artifact_ids": ["artifact-1"],
                }
            ]
        },
        report={
            "report_stage": "final_review",
            "summary": {
                "overall_status": "incomplete",
                "candidate_execution_complete": False,
                "unresolved_candidate_rule_codes": ["alioth_16", "alioth_17"],
            },
            "page_exploration": {
                "tracking_available": True,
                "complete": False,
                "quality_status": "needs_review",
                "page_total": 2,
            },
        },
    )

    assert all(check.passed for check in checks)


def test_h5_business_acceptance_rejects_review_preview_without_gate() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "review",
            "report_status": "ready",
            "completion_gate_passed": False,
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_18",
                    "execution_status": "pass",
                    "source": "agent",
                    "page_capture_id": "capture-1",
                    "evidence_artifact_ids": ["artifact-1"],
                }
            ]
        },
        report={
            "report_stage": "final_review",
            "is_final": False,
            "summary": {
                "overall_status": "incomplete",
                "business_completion_status": "incomplete",
            },
            "page_exploration": {
                "tracking_available": True,
                "complete": False,
                "quality_status": "needs_review",
                "page_total": 1,
            },
        },
        require_business_completion=True,
    )

    business = next(check for check in checks if check.code == "BUSINESS_COMPLETION")
    assert business.passed is False
    assert "completion_gate_passed=false" in business.message
    assert "report.is_final=false" in business.message


def test_h5_acceptance_verifier_rejects_review_report_marked_as_final() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "review",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "failed",
            "termination_reason": "incomplete",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_18",
                    "execution_status": "pass",
                    "source": "agent",
                    "page_capture_id": "capture-1",
                    "evidence_artifact_ids": ["artifact-1"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "incomplete"},
            "page_exploration": {
                "tracking_available": True,
                "complete": False,
                "quality_status": "needs_review",
                "page_total": 2,
            },
        },
    )

    report_check = next(check for check in checks if check.code == "REPORT_READY")
    assert report_check.passed is False
    assert "expected=final_review" in report_check.message


def test_h5_acceptance_verifier_accepts_popup_chain_closure() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "done",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-home",
                    "evidence_artifact_ids": ["artifact-rule"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 2,
            },
            "surface_phases": [
                {
                    "phase": "popup_chain",
                    "status": "completed",
                    "stable_end": True,
                    "steps": [
                        {
                            "action_type": "click",
                            "target_role": "close",
                            "before_capture_id": "capture-popup-1",
                            "after_capture_id": "capture-popup-2",
                            "evidence_artifact_ids": ["artifact-popup-1"],
                        },
                        {
                            "action_type": "tap",
                            "target_role": "close",
                            "before_capture_id": "capture-popup-2",
                            "after_capture_id": "capture-home",
                            "evidence_artifact_ids": ["artifact-popup-2"],
                        },
                    ],
                }
            ],
        },
        expect_popup_chain=True,
    )

    assert all(check.passed for check in checks)


def test_h5_acceptance_verifier_accepts_popup_chain_runtime_trace() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "done",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
            "action_trace": [
                {
                    "tool_name": "page_capture_tool",
                    "status": "ok",
                    "arguments": verifier.json.dumps(
                        {
                            "mode": "single_trigger",
                            "trigger_action": "click",
                            "clear_obstructions": True,
                            "supersedes_page_capture_id": "capture-popup-1",
                        }
                    ),
                },
                {
                    "tool_name": "page_capture_tool",
                    "status": "ok",
                    "arguments": verifier.json.dumps(
                        {
                            "mode": "single_trigger",
                            "trigger_action": "click",
                            "clear_obstructions": True,
                            "supersedes_page_capture_id": "capture-popup-2",
                        }
                    ),
                },
            ],
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-home",
                    "evidence_artifact_ids": ["artifact-rule"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 3,
                "page_results": [
                    {
                        "page_seq": 1,
                        "page_capture_id": "capture-popup-1",
                        "page_type": "home",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-before-1"],
                    },
                    {
                        "page_seq": 2,
                        "page_capture_id": "capture-popup-2",
                        "supersedes_page_capture_id": "capture-popup-1",
                        "page_type": "home",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-after-1"],
                    },
                    {
                        "page_seq": 3,
                        "page_capture_id": "capture-home",
                        "supersedes_page_capture_id": "capture-popup-2",
                        "page_type": "home",
                        "page_state": "stable",
                        "failure_code": "SUPPLEMENTAL_EVIDENCE_DISABLED",
                        "artifact_ids": ["artifact-after-2"],
                    },
                ],
            },
        },
        expect_popup_chain=True,
    )

    assert all(check.passed for check in checks)


def test_h5_acceptance_verifier_accepts_persisted_h5_popup_actions() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={"task_id": "task-1", "status": "done", "report_status": "ready"},
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-home",
                    "evidence_artifact_ids": ["artifact-rule"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 3,
                "page_results": [
                    {
                        "page_seq": 1,
                        "page_capture_id": "capture-popup-1",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-popup-1"],
                    },
                    {
                        "page_seq": 4,
                        "page_capture_id": "capture-popup-2",
                        "parent_page_capture_id": "capture-before-1",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-popup-2"],
                    },
                    {
                        "page_seq": 6,
                        "page_capture_id": "capture-home",
                        "parent_page_capture_id": "capture-before-2",
                        "page_state": "stable",
                        "artifact_ids": ["artifact-home"],
                    },
                ],
                "evidence_page_results": [
                    {
                        "page_seq": 2,
                        "page_capture_id": "capture-before-1",
                        "parent_page_capture_id": "capture-popup-1",
                        "artifact_ids": ["artifact-before-1"],
                    },
                    {
                        "page_seq": 5,
                        "page_capture_id": "capture-before-2",
                        "parent_page_capture_id": "capture-popup-2",
                        "artifact_ids": ["artifact-before-2"],
                    },
                ],
                "action_results": [
                    {
                        "exploration_action_id": "action-close-1",
                        "command_sequence": 1,
                        "status": "explored",
                        "execution_status": "completed",
                        "requested_source_capture_id": "capture-popup-1",
                        "source_page_capture_id": "capture-before-1",
                        "result_page_capture_id": "capture-popup-2",
                        "parameters": {"target_mode": "popup_dismissal"},
                    },
                    {
                        "exploration_action_id": "action-close-2",
                        "command_sequence": 2,
                        "status": "explored",
                        "execution_status": "completed",
                        "requested_source_capture_id": "capture-popup-2",
                        "source_page_capture_id": "capture-before-2",
                        "result_page_capture_id": "capture-home",
                        "parameters": {"target_mode": "popup_dismissal"},
                    },
                ],
            },
        },
        expect_popup_chain=True,
    )

    assert all(check.passed for check in checks)


def test_h5_acceptance_verifier_splits_popup_chains_and_accepts_controlled_back() -> None:
    verifier = _load_verify_module()

    def page(capture_id: str, *, parent_id: str | None, state: str) -> dict:
        return {
            "page_capture_id": capture_id,
            "parent_page_capture_id": parent_id,
            "page_state": state,
            "artifact_ids": [f"artifact-{capture_id}"],
        }

    def action(
        sequence: int,
        *,
        requested_id: str,
        source_id: str,
        result_id: str,
        action_type: str = "click",
        target_mode: str | None = "popup_dismissal",
    ) -> dict:
        parameters = {"target_mode": target_mode} if target_mode is not None else {}
        return {
            "exploration_action_id": f"action-{sequence}",
            "command_sequence": sequence,
            "action": action_type,
            "status": "explored",
            "execution_status": "completed",
            "requested_source_capture_id": requested_id,
            "source_page_capture_id": source_id,
            "result_page_capture_id": result_id,
            "parameters": parameters,
        }

    report = {
        "page_exploration": {
            "page_results": [
                page("home-popup-1", parent_id=None, state="popup"),
                page("home-popup-2", parent_id="home-before-1", state="popup"),
                page("home-stable", parent_id="home-before-2", state="stable"),
                page("detail-popup-1", parent_id="entry-before", state="popup"),
                page("detail-popup-2", parent_id="detail-before-1", state="popup"),
                page("detail-popup-3", parent_id="detail-before-2", state="popup"),
                page("video-list", parent_id="back-before", state="stable"),
            ],
            "evidence_page_results": [
                page("home-before-1", parent_id="home-popup-1", state="loaded"),
                page("home-before-2", parent_id="home-popup-2", state="loaded"),
                page("entry-before", parent_id="home-stable", state="loaded"),
                page("detail-before-1", parent_id="detail-popup-1", state="loaded"),
                page("detail-before-2", parent_id="detail-popup-2", state="loaded"),
                page("back-before", parent_id="detail-popup-3", state="loaded"),
            ],
            "action_results": [
                action(
                    1,
                    requested_id="home-popup-1",
                    source_id="home-before-1",
                    result_id="home-popup-2",
                ),
                action(
                    2,
                    requested_id="home-popup-2",
                    source_id="home-before-2",
                    result_id="home-stable",
                ),
                action(
                    3,
                    requested_id="home-stable",
                    source_id="entry-before",
                    result_id="detail-popup-1",
                    target_mode="list_detail_navigation",
                ),
                action(
                    4,
                    requested_id="detail-popup-1",
                    source_id="detail-before-1",
                    result_id="detail-popup-2",
                ),
                action(
                    5,
                    requested_id="detail-popup-2",
                    source_id="detail-before-2",
                    result_id="detail-popup-3",
                ),
                action(
                    6,
                    requested_id="detail-popup-3",
                    source_id="back-before",
                    result_id="video-list",
                    action_type="back",
                    target_mode=None,
                ),
            ],
        }
    }

    check = verifier._check_popup_chain_from_persisted_actions(
        report,
        min_closed_layers=2,
    )

    assert check is not None
    assert check.passed is True
    assert "chain_count=2" in check.message


def test_h5_acceptance_verifier_rejects_persisted_popup_action_without_before_capture() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={"task_id": "task-1", "status": "done", "report_status": "ready"},
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-home",
                    "evidence_artifact_ids": ["artifact-rule"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 2,
                "page_results": [
                    {
                        "page_capture_id": "capture-popup",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-popup"],
                    },
                    {
                        "page_capture_id": "capture-home",
                        "parent_page_capture_id": "capture-before-missing",
                        "page_state": "stable",
                        "artifact_ids": ["artifact-home"],
                    },
                ],
                "action_results": [
                    {
                        "exploration_action_id": "action-close",
                        "command_sequence": 1,
                        "status": "explored",
                        "execution_status": "completed",
                        "requested_source_capture_id": "capture-popup",
                        "source_page_capture_id": "capture-before-missing",
                        "result_page_capture_id": "capture-home",
                        "parameters": {"target_mode": "popup_dismissal"},
                    }
                ],
            },
        },
        expect_popup_chain=True,
        min_popup_layers=1,
    )

    failed = {check.code: check.message for check in checks if not check.passed}
    assert "POPUP_CHAIN_CLOSURE" in failed
    assert "action-close" in failed["POPUP_CHAIN_CLOSURE"]


def test_h5_acceptance_verifier_counts_layers_inside_persisted_action_trace() -> None:
    verifier = _load_verify_module()
    report = {
        "page_exploration": {
            "page_results": [
                {
                    "page_capture_id": "capture-popup",
                    "page_state": "popup",
                    "artifact_ids": ["artifact-popup"],
                },
                {
                    "page_capture_id": "capture-home",
                    "parent_page_capture_id": "capture-before",
                    "page_state": "stable",
                    "artifact_ids": ["artifact-trace"],
                },
            ],
            "evidence_page_results": [
                {
                    "page_capture_id": "capture-before",
                    "parent_page_capture_id": "capture-popup",
                    "artifact_ids": ["artifact-before"],
                }
            ],
            "action_results": [
                {
                    "exploration_action_id": "action-close",
                    "command_sequence": 1,
                    "status": "explored",
                    "execution_status": "completed",
                    "requested_source_capture_id": "capture-popup",
                    "source_page_capture_id": "capture-before",
                    "result_page_capture_id": "capture-home",
                    "parameters": {"target_mode": "popup_dismissal"},
                }
            ],
        }
    }

    check = verifier._check_popup_chain_from_persisted_actions(
        report,
        popup_action_traces={
            "artifact-trace": [
                {"action": "click", "obstruction_chain_index": 1},
                {"action": "click", "obstruction_chain_index": 2},
                {"action": "click", "obstruction_chain_index": 3},
            ]
        },
        min_closed_layers=3,
    )

    assert check is not None
    assert check.passed is True
    assert "max_closed_layers=3" in check.message


def test_h5_acceptance_verifier_rejects_popup_chain_runtime_exhausted() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "review",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
            "action_trace": [
                {
                    "tool_name": "page_capture_tool",
                    "status": "ok",
                    "arguments": verifier.json.dumps(
                        {
                            "mode": "single_trigger",
                            "trigger_action": "click",
                            "clear_obstructions": True,
                            "supersedes_page_capture_id": "capture-popup-1",
                        }
                    ),
                },
                {
                    "tool_name": "page_capture_tool",
                    "status": "ok",
                    "arguments": verifier.json.dumps(
                        {
                            "mode": "single_trigger",
                            "trigger_action": "click",
                            "clear_obstructions": True,
                            "supersedes_page_capture_id": "capture-popup-2",
                        }
                    ),
                },
            ],
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-popup-2",
                    "evidence_artifact_ids": ["artifact-rule"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "manual_review"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 3,
                "page_results": [
                    {
                        "page_seq": 1,
                        "page_capture_id": "capture-popup-1",
                        "page_type": "home",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-before-1"],
                    },
                    {
                        "page_seq": 2,
                        "page_capture_id": "capture-popup-2",
                        "supersedes_page_capture_id": "capture-popup-1",
                        "page_type": "home",
                        "page_state": "popup",
                        "artifact_ids": ["artifact-after-1"],
                    },
                    {
                        "page_seq": 3,
                        "page_capture_id": "capture-popup-3",
                        "supersedes_page_capture_id": "capture-popup-2",
                        "page_type": "home",
                        "page_state": "popup",
                        "failure_code": "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
                        "artifact_ids": ["artifact-after-2"],
                    },
                ],
            },
        },
        expect_popup_chain=True,
    )

    failed = {check.code: check.message for check in checks if not check.passed}
    assert "POPUP_CHAIN_CLOSURE" in failed
    assert "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED" in failed["POPUP_CHAIN_CLOSURE"]


def test_h5_acceptance_verifier_rejects_popup_chain_fake_done() -> None:
    verifier = _load_verify_module()

    checks = verifier.verify_acceptance_payloads(
        task={
            "task_id": "task-1",
            "status": "done",
            "report_status": "ready",
        },
        run={
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.00016},
        },
        rule_executions={
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-popup-1",
                    "evidence_artifact_ids": ["artifact-rule"],
                }
            ]
        },
        report={
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "page_total": 2,
            },
            "surface_phases": [
                {
                    "phase": "popup_chain",
                    "status": "completed",
                    "steps": [
                        {
                            "action_type": "click",
                            "target_role": "close",
                            "before_capture_id": "capture-popup-1",
                            "evidence_artifact_ids": ["artifact-popup-1"],
                        }
                    ],
                }
            ],
        },
        expect_popup_chain=True,
    )

    failed_codes = {check.code for check in checks if not check.passed}
    assert "POPUP_CHAIN_CLOSURE" in failed_codes


def test_h5_acceptance_verifier_passes_bearer_token(monkeypatch) -> None:
    verifier = _load_verify_module()
    captured_headers: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured_headers.update(request.headers)
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)

    assert verifier.load_json_url(
        "http://127.0.0.1:8000/api/v1/health",
        access_token="token-1",
    ) == {"ok": True}
    assert captured_headers["Authorization"] == "Bearer token-1"


def test_h5_acceptance_verifier_login_uses_password_env(monkeypatch) -> None:
    verifier = _load_verify_module()
    captured_body: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b'{"token": "token-from-login"}'

    def fake_urlopen(request, timeout):
        captured_body.update(verifier.json.loads(request.data.decode("utf-8")))
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setenv("ALIOTH_TEST_PASSWORD", "secret")
    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)

    token = verifier._resolve_access_token(
        base_url="http://127.0.0.1:8000",
        access_token=None,
        username="admin",
        password_env="ALIOTH_TEST_PASSWORD",
    )

    assert token == "token-from-login"
    assert captured_body == {"username": "admin", "password": "secret"}
