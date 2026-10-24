"""报告中心读模型 / 人工确认 / HTML 导出的单元测试。"""

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.report_insight import (
    ReportConfirmationError,
    ReportExportBlockedError,
    ReportInsightNotFoundError,
    ReportInsightService,
    _bound_final_snapshot_insight,
    _is_h5_v2_final_snapshot,
)
from services.api.app.domain.iam.access_scope import AccessScope

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def legacy_inventory_reader(monkeypatch):
    async def load_snapshot(self, **kwargs):
        return None

    monkeypatch.setattr(
        "services.api.app.infrastructure.db.task_rule_inventory_repository."
        "PostgresTaskRuleInventoryRepository.load_snapshot",
        load_snapshot,
    )


@pytest.mark.parametrize("status,reason", [
    ("skipped", "candidate_not_executed"),
    ("skipped", "run_budget_exhausted"),
    ("evidence_insufficient", None),
    ("error", None),
    ("manual_review", "semantic_provider_http_500"),
    ("manual_review", "semantic_provider_disconnect"),
])
async def test_execution_gaps_cannot_be_confirmed_as_business_results(status, reason) -> None:
    repository = FakeReportRepository()
    repository.rows = [_row("gap-rule", status, execution_reason=reason, human_verdict="fail")]
    service = _service(repository)
    insight = await service.report_insight("task-1")
    item = insight["rule_items"][0]
    assert item["confirmable"] is False
    assert item["confirm_status"] is None
    assert item["display_status"] not in {"passed", "failed", "not_applicable"}
    assert insight["qc_success_rate"] == 0
    assert item["human_verdict"] == "fail"
    with pytest.raises(ReportConfirmationError):
        await service.confirm_rule("task-1", "exec-gap-rule", verdict="pass")
    _, html_text = await service.export_html("task-1")
    assert "规则gap-rule" not in html_text
    assert "质检复核报告" in html_text
    assert "本报告不代表检测已完整完成" in html_text


@pytest.mark.parametrize(
    "code", ["SUPPLEMENTAL_EVIDENCE_DISABLED", "SUPPLEMENTAL_EVIDENCE_NOT_READY"]
)
async def test_legacy_supplemental_denial_cannot_become_product_not_applicable(code: str) -> None:
    repository = FakeReportRepository()
    repository.context["target_platform"] = "app"
    repository.rows = [
        _row("passed", "pass"),
        _row("gap", "skipped", error_code=code, human_verdict="ignored"),
    ]
    insight = await _service(repository).report_insight("task-1")
    gap = insight["rule_items"][1]
    assert gap["execution_status"] == "skipped"
    assert gap["human_verdict"] == "ignored"
    assert gap["machine_display_status"] == "evidence_insufficient"
    assert gap["display_status"] == "other"
    assert gap["confirmable"] is False
    assert insight["summary"]["confirmed_not_applicable_rules"] == 0
    assert insight["summary"]["completed_candidate_rules"] == 1
    assert insight["qc_success_rate"] == 50


@pytest.mark.parametrize(
    "reason", [None, "", "supplemental_evidence_disabled", "product_not_applicable"]
)
def test_frozen_supplemental_denial_retains_raw_status_but_does_not_count_success(reason) -> None:
    snapshot = {
        "task": {"task_id": "task-1", "status": "review"},
        "summary": {"qc_success_rate": 100},
        "rule_results": [{
            "execution_id": "gap", "execution_status": "skipped",
            "diagnostic_codes": ["SUPPLEMENTAL_EVIDENCE_DISABLED"],
            "execution_reason": reason,
        }],
    }
    insight = _bound_final_snapshot_insight(snapshot, target_platform="h5")
    assert insight["qc_success_rate"] == 0
    assert insight["rule_items"][0]["execution_status"] == "skipped"
    assert insight["rule_items"][0]["machine_display_status"] == "evidence_insufficient"
    assert snapshot["summary"]["qc_success_rate"] == 100


def test_frozen_skipped_results_keep_verified_reasons_and_fail_closed_without_them() -> None:
    snapshot = {
        "task": {"task_id": "task-1", "status": "done"},
        "summary": {"qc_success_rate": 100, "confirmed_not_applicable_rules": 1},
        "rule_results": [{"execution_id": "frozen-1", "execution_status": "skipped"}],
    }
    missing = _bound_final_snapshot_insight(snapshot, target_platform="h5")
    assert missing["rule_items"][0]["display_status"] == "other"
    assert missing["qc_success_rate"] == 0
    assert missing["summary"]["confirmed_not_applicable_rules"] == 0
    verified = _bound_final_snapshot_insight(
        snapshot, target_platform="h5", execution_reasons={"frozen-1": "candidate_not_executed"}
    )
    assert verified["rule_items"][0]["machine_display_status"] == "not_executed"
    assert verified["rule_items"][0]["display_status"] == "other"
    snapshot["rule_results"][0]["execution_reason"] = "run_budget_exhausted"
    direct = _bound_final_snapshot_insight(
        snapshot, target_platform="h5", execution_reasons={"frozen-1": "candidate_not_executed"}
    )
    assert direct["rule_items"][0]["execution_reason"] == "run_budget_exhausted"


class FakeReportRepository:
    async def read_web_capture_identities(self, **kwargs):
        from services.api.app.domain.page.web_url_identity import (
            DEFAULT_WEB_URL_IDENTITY_POLICY as policy,
        )
        return {row["page_capture_id"]: {"logical_page_id": policy.logical_id(row["url"]), "canonical_url": policy.canonical(row["url"]), "logical_page_identity_status": "verified"} for row in self.h5_page_rows if row.get("url")}

    def __init__(self) -> None:
        self.context: dict[str, Any] | None = {
            "task_id": "task-1",
            "status": "review",
            "target_url": "https://example.com",
            "channel": "workbench",
            "created_at": NOW,
            "started_at": NOW,
            "finished_at": NOW,
            "last_error": None,
            "task_config": {"task_name": "报告冒烟任务"},
            "finalization_state": "session_draining",
            "completion_gate_snapshot_id": "gate-1",
            "sealed_report_bound": False,
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "run_id": "run-1",
            "run_status": "failed",
            "stop_reason": "incomplete",
        }
        self.rows: list[dict[str, Any]] = []
        self.recognized_pages = 3
        self.updates: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.page_rows: list[dict[str, Any]] = []
        self.exploration_actions: list[dict[str, Any]] = []
        self.h5_page_rows: list[dict[str, Any]] = []
        self.h5_actions: list[dict[str, Any]] = []
        self.h5_work_items: list[dict[str, Any]] = []
        self.h5_work_reads: list[tuple[str, str]] = []
        self.h5_assessments: list[dict[str, Any]] = []
        self.h5_assessment_reads: list[tuple[str, str]] = []
        self.h5_inventory: list[dict[str, Any]] = []
        self.h5_full_closure: dict[str, Any] = {}
        self.h5_coverage: dict[str, Any] | None = None
        self.h5_coverage_reads: list[tuple[str, str]] = []
        self.web_coverage: dict[str, Any] | None = None
        self.web_coverage_reads: list[tuple[str, str]] = []
        self.scope_calls: list[tuple[str, AccessScope | None]] = []

    async def fetch_task_context(
        self, task_id: str, *, access_scope: AccessScope | None = None
    ) -> dict[str, Any] | None:
        self.scope_calls.append(("fetch_task_context", access_scope))
        return self.context

    async def list_rule_execution_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        self.scope_calls.append(("list_rule_execution_rows", access_scope))
        return self.rows

    async def count_recognized_pages(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> int:
        self.scope_calls.append(("count_recognized_pages", access_scope))
        return self.recognized_pages

    async def list_page_capture_inspection_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        self.scope_calls.append(("list_page_capture_inspection_rows", access_scope))
        return self.page_rows

    async def list_exploration_actions(
        self,
        *,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        self.scope_calls.append(("list_exploration_actions", access_scope))
        return self.exploration_actions

    async def list_h5_page_captures(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[dict[str, Any]]:
        del task_id, agent_run_id
        return self.h5_page_rows

    async def list_h5_v2_work_items(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[dict[str, Any]]:
        self.h5_work_reads.append((task_id, agent_run_id))
        return self.h5_work_items

    async def list_h5_action_executions(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[dict[str, Any]]:
        del task_id, agent_run_id
        return self.h5_actions

    async def list_h5_page_type_assessments(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[dict[str, Any]]:
        self.h5_assessment_reads.append((task_id, agent_run_id))
        return self.h5_assessments

    async def fetch_h5_coverage_snapshot(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any] | None:
        self.h5_coverage_reads.append((task_id, agent_run_id))
        return self.h5_coverage

    async def list_h5_evidence_inventory(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[dict[str, Any]]:
        del task_id, agent_run_id
        return self.h5_inventory

    async def fetch_h5_full_inspection_closure(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        del task_id, agent_run_id
        return self.h5_full_closure

    async def fetch_web_coverage_snapshot(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any] | None:
        self.web_coverage_reads.append((task_id, agent_run_id))
        return self.web_coverage

    async def fetch_rule_execution(
        self,
        execution_id: str,
        *,
        task_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        self.scope_calls.append(("fetch_rule_execution", access_scope))
        for row in self.rows:
            if row["execution_id"] == execution_id:
                return {
                    "execution_id": execution_id,
                    "task_id": "task-1",
                    "execution_status": row["execution_status"],
                    "execution_reason": row.get("execution_reason"),
                    "review_status": row.get("review_status"),
                    "is_bootstrap": row.get("is_bootstrap", False),
                    "human_verdict": row.get("human_verdict"),
                    "rule_code": row["rule_code"],
                    "rule_name": row["rule_name"],
                    "category": row.get("category"),
                }
        return None

    async def update_human_verdict(
        self,
        *,
        execution_id: str,
        verdict: str,
        note: str | None,
        operator: str | None,
        task_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        update = {"execution_id": execution_id, "verdict": verdict, "note": note}
        if access_scope is not None:
            update.update(
                {
                    "task_id": task_id,
                    "operator": operator,
                    "access_scope": access_scope,
                }
            )
        self.updates.append(update)
        return {
            "execution_id": execution_id,
            "human_verdict": verdict,
            "human_verdict_note": note,
            "human_verdict_by": operator,
            "human_verdict_at": NOW,
            "review_status": "approved" if verdict != "fail" else "rejected",
        }

    async def list_screenshot_artifacts(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        self.scope_calls.append(("list_screenshot_artifacts", access_scope))
        return self.screenshots


class FakeAppReportRepository(FakeReportRepository):
    def __init__(self) -> None:
        super().__init__()
        assert self.context is not None
        self.context["target_platform"] = "app"

    async def fetch_app_stage(self, task_id: str) -> dict[str, Any]:
        assert task_id == "task-1"
        return {
            "app_exploration": {
                "outcome": "evidence_insufficient",
                "coverage_summary": {
                    "coverage_complete": True,
                    "visual_quality_satisfied": False,
                    "page_type_applicability": {
                        "authority_source": "run_observation_and_frontier_exhaustion",
                        "assessment_scope_complete": True,
                        "assessment_scope_verified": True,
                        "covered_page_types": ["home"],
                        "known_uncovered_page_types": ["profile"],
                        "not_applicable_page_types": ["payment"],
                        "unresolved_applicability_page_types": [],
                        "applicable_page_types": ["home", "profile"],
                        "covered_count": 1,
                        "applicable_count": 2,
                        "coverage_rate": 0.5,
                    },
                    "app_target_snapshot": {"target_hash": "sha256:target"},
                    "degraded_provider_facts": [
                        {
                            "status": "degraded",
                            "provider_id": "llm-app-vision",
                        }
                    ],
                },
            }
        }


class FakeEvidenceStore:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    async def read(self, storage_path: str) -> bytes:
        return self.blobs[storage_path]


def _row(code: str, status: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "execution_id": f"exec-{code}",
        "rule_id": f"rule-{code}",
        "rule_code": code,
        "rule_name": f"规则{code}",
        "category": "ad",
        "rule_severity": "high",
        "rule_package": None,
        "trigger_display": None,
        "applicable_page_types": ["home"],
        "sort_order": 1,
        "execution_status": status,
        "judge_strategy": "hybrid",
        "confidence": 0.8,
        "severity": "high",
        "result_summary": None,
        "problem_desc": f"{code} 的问题描述",
        "review_status": None,
        "error_message": None,
        "evidence_artifact_ids": [],
        "page_capture_id": "cap-1",
        "started_at": NOW,
        "finished_at": NOW,
        "human_verdict": None,
        "human_verdict_note": None,
        "human_verdict_at": None,
        "capture_page_type": "home",
        "is_bootstrap": False,
        "execution_reason": None,
    }
    row.update(overrides)
    return row


def _service(repository: FakeReportRepository, store: FakeEvidenceStore | None = None):
    resolved_store = store if store is not None else FakeEvidenceStore({})
    return ReportInsightService(
        repository=repository,
        evidence_store_factory=lambda: resolved_store,
    )


@pytest.mark.parametrize("with_real_judge_gap", [False, True])
async def test_startup_and_aggregate_preflight_audits_do_not_inflate_qc_denominator(
    with_real_judge_gap,
):
    repository = FakeReportRepository()
    repository.context["target_platform"] = "app"
    repository.rows = [
        _row(
            "startup", "skipped", execution_reason="exploration_not_started", page_capture_id=None
        ),
        _row("auth", "skipped", execution_reason="auth_requirement_not_met", page_capture_id=None),
        _row("bootstrap", "skipped", is_bootstrap=True, page_capture_id=None),
        _row(
            "preflight",
            "evidence_insufficient",
            page_capture_id=None,
            error_code="AGGREGATE_APPLICABILITY_UNPROVEN",
            judge_input={"source": "aggregate_candidate_disposition"},
        ),
    ]
    if with_real_judge_gap:
        repository.rows.extend([_row("passed", "pass"), _row("judge-gap", "evidence_insufficient")])
    service = _service(repository)
    insight = await service.report_insight("task-1")
    assert insight["summary"]["total_rules"] == (2 if with_real_judge_gap else 0)
    assert insight["qc_success_rate"] == (50 if with_real_judge_gap else 0)
    assert len(insight["rule_items"]) == len(repository.rows)
    assert [item["execution_id"] for item in insight["rule_items"]] == [
        row["execution_id"] for row in repository.rows
    ]
    assert insight["rule_items"][3]["execution_status"] == "evidence_insufficient"
    assert insight["rule_items"][3]["execution_reason"] == "AGGREGATE_APPLICABILITY_UNPROVEN"
    assert repository.rows[3]["execution_reason"] is None
    _, html = await service.export_html("task-1")
    assert "质检复核报告" in html
    assert "4 条规则未实际判定" in html


@pytest.mark.parametrize("rule_index", [20, 22])
@pytest.mark.parametrize("real_judge_gap", [False, True])
async def test_export_scope_disposition_and_current_execution_gap_share_completion_semantics(
    rule_index, real_judge_gap,
):
    from copy import deepcopy

    from services.api.app.application.task_rule_inventory import build_task_rule_inventory
    from services.api.tests.test_report_rule_inventory import _fixture

    _, snapshot = _fixture()
    selected = snapshot["inventory"]["rules"][rule_index]
    snapshot["inventory"] = build_task_rule_inventory(
        ruleset_id="ruleset-1", ruleset_version=1, ruleset_snapshot_hash="hash",
        target_platform="app", channel="workbench", rules=[selected],
        task_config={"inspection_scope": {"page_types": ["home"]}},
    )
    row = _row(
        selected["rule_code"], "evidence_insufficient" if real_judge_gap else "skipped",
        rule_id=selected["rule_id"], execution_id="original-audit-or-judge",
        execution_reason=None if real_judge_gap else "exploration_not_started",
    )
    snapshot["executions"] = [deepcopy(row)] if real_judge_gap else []
    repository = FakeReportRepository()
    repository.context.update(
        target_platform="app",
        app_report_availability="preview" if real_judge_gap else "none",
    )
    repository.context["task_config"].update(workflow_version="v2", execution_mode="full_inspection")
    repository.rows = [row]

    class Reader:
        async def load_snapshot(self, **kwargs):
            return deepcopy(snapshot)

    service = ReportInsightService(
        repository=repository, evidence_store_factory=lambda: FakeEvidenceStore({}),
        rule_inventory_repository=Reader(),
    )
    insight = await service.report_insight("task-1")
    assert insight["summary"]["used_rule_count"] == int(real_judge_gap)
    assert insight["summary"]["rule_inventory_pending_rules"] == int(real_judge_gap)
    assert insight["summary"]["rule_inventory_complete"] is (not real_judge_gap)
    assert insight["report_availability"] == ("preview" if real_judge_gap else "none")
    assert insight["export_ready"] is False
    assert insight["export_allowed"] is False
    with pytest.raises(ReportExportBlockedError, match="尚未通过门禁并封存"):
        await service.export_html("task-1")
    assert row["execution_id"] == "original-audit-or-judge"


async def test_insight_assembles_summary_and_confirm_scope() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_4", "fail"),
        _row("alioth_5", "evidence_insufficient"),
        _row("alioth_6", "error"),
        _row(
            "alioth_7",
            "manual_review",
            category="input_risk",
            human_verdict="pass",
            human_verdict_note="人工看过",
        ),
        _row("alioth_8", "manual_review", category="input_risk"),
    ]
    repository.page_rows = [
        {
            "page_capture_id": "cap-1",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
        },
        {
            "page_capture_id": "cap-2",
            "inspection_lifecycle_status": "active",
            "inspection_status": "failed",
            "inspection_failure_code": "RULE_WRITE_FAILED",
            "inspection_failure_message": "规则结果写入失败",
        },
    ]
    repository.exploration_actions = [
        {"exploration_action_id": "exploration-1", "status": "explored"}
    ]
    insight = await _service(repository).report_insight("task-1")
    assert insight["summary"]["task_name"] == "报告冒烟任务"
    assert insight["summary"]["page_recognized_count"] == 3
    assert insight["summary"]["total_rules"] == 6
    assert insight["summary"]["report_stage"] == "final_review"
    assert insight["page_exploration"]["page_total"] == 2
    assert insight["page_exploration"]["failed_page_count"] == 1
    assert insight["page_exploration"]["page_results"][1]["failure_code"] == ("RULE_WRITE_FAILED")
    counts = {item["status"]: item["count"] for item in insight["distribution"]}
    # 2026-09-01 六态口径：执行故障=检测异常；证据不足/转人工没出确定结论归其他。
    assert counts == {
        "passed": 2,
        "failed": 1,
        "not_applicable": 0,
        "coverage_gap": 0,
        "detect_error": 1,
        "other": 2,
    }
    assert insight["qc_success_rate"] == 50.0
    assert insight["summary"]["qc_success_rate"] == 50.0
    assert insight["summary"]["full_rule_pass_rate"] == 33.3
    # 仅业务转人工需要确认；缺证和检测故障不通过人工结论补成检测完成。
    assert insight["confirm_progress"] == {
        "total": 2,
        "confirmed": 1,
        "pending": 1,
        "completed": False,
    }
    assert insight["export_ready"] is False
    confirm_codes = {item["rule_code"] for item in insight["confirm_items"]}
    assert confirm_codes == {"alioth_7", "alioth_8"}
    by_code = {item["rule_code"]: item for item in insight["rule_items"]}
    assert by_code["alioth_2"]["confirm_status"] is None
    assert by_code["alioth_4"]["confirm_status"] is None
    assert by_code["alioth_5"]["confirm_status"] is None
    assert by_code["alioth_6"]["confirm_status"] is None
    assert by_code["alioth_8"]["confirm_status"] == "pending"
    assert by_code["alioth_7"]["confirm_status"] == "confirmed"
    assert by_code["alioth_7"]["human_verdict_label"] == "通过"
    assert by_code["alioth_7"]["machine_display_status"] == "manual_review"
    assert by_code["alioth_7"]["display_status"] == "passed"
    assert by_code["alioth_4"]["rule_package"] == "广告质检"


async def test_report_insight_exposes_optional_logical_surface_audit() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context["target_platform"] = "h5"
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-spa-home",
            "page_type": "home",
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "render_profile": {
                    "kind": "dom_spa",
                    "status": "confirmed",
                    "confidence": 0.94,
                },
                "document_identity": "sha256:" + "9" * 64,
                "identity_fingerprint": "sha256:" + "1" * 64,
                "state_fingerprint": "sha256:" + "2" * 64,
                "evidence_sources": ["dom", "document"],
                "evidence_quality": "high",
                "degraded_reasons": [],
                "shadow": True,
                "page_type_binding": {
                    "page_type": "home",
                    "page_type_confidence": 0.9,
                    "confidence_source": "deterministic_structure",
                    "coverage_allowed": True,
                    "rule_matching_allowed": True,
                },
            },
            "capture_role": "after_action",
        }
    ]
    repository.h5_actions = [
        {
            "action_execution_id": "action-spa-profile",
            "exploration_action_id": "action-spa-profile",
            "status": "explored",
            "expected_next_page_type": "home",
            "result_page_capture_id": "capture-spa-home",
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "transition_kind": "logical_page_changed",
                "logical_transition_proof": "trusted",
            },
            "result_payload": {
                "state_diff": {
                    "before_location": "https://example.test/app",
                    "after_location": "https://example.test/app",
                    "location_changed": False,
                }
            },
        }
    ]

    insight = await _service(repository).report_insight("task-1")

    logical = insight["page_exploration"]["logical_surface"]
    assert logical["unique_logical_page_count"] == 1
    assert logical["render_profile_counts"] == {"dom_spa": 1}
    assert logical["logical_pages"][0]["page_type"] == "home"
    assert logical["transition_counts"] == {"logical_page_changed": 1}
    assert logical["same_url_logical_page_change_count"] == 1


async def test_h5_v2_report_uses_formal_assessments_when_working_memory_is_empty() -> None:
    """正式 Assessment 存在时，最终报告必须与 H5 V2 时间线使用同一事实源。"""
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={"task_name": "H5 V2 报告", "workflow_version": "v2"},
        working_memory={},
    )
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-home",
            "page_type": "home",
            "page_state": "stable",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
            "artifact_ids": ["artifact-home"],
            "capture_role": "after_action",
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_capture_id": "capture-home",
            "page_type": "home",
            "page_state": "stable",
            "status": "covered",
            "reason_codes": ["recognized_with_evidence"],
            "assessment_payload": {
                "catalog_sha256": "catalog-1",
                "page_capture_ids": ["capture-home"],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            },
        }
    ]

    insight = await _service(repository).report_insight("task-1")

    product = insight["page_exploration"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["assessment_scope_complete"] is True
    assert product["assessment_scope_verified"] is True
    assert product["covered_page_types"] == ["home"]
    assert product["coverage_rate"] == 1.0
    assert repository.h5_assessment_reads == [("task-1", "run-1")]


async def test_h5_v2_report_exposes_evidence_preflight_failure() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={"task_name": "H5 缺证报告", "workflow_version": "v2"},
        working_memory={},
    )
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-missing",
            "page_type": "home",
            "page_state": "stable",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "evidence_insufficient",
            "artifact_ids": ["artifact-screenshot"],
            "capture_role": "after_action",
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_capture_id": "capture-missing",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {
                "catalog_sha256": "catalog-1",
                "page_capture_ids": ["capture-missing"],
                "evidence_artifact_ids": ["artifact-screenshot"],
            },
        }
    ]
    repository.h5_inventory = [
        {
            "inventory_item_id": "inventory-missing",
            "status": "insufficient",
            "after_capture_id": "capture-missing",
            "reason_codes": ["after:dom_missing"],
            "published_inspection_count": 0,
        }
    ]
    repository.h5_work_items = [
        {
            "work_id": "work-home",
            "status": "succeeded",
            "frontier": {
                "snapshot_id": "frontier-home",
                "pending_count": 0,
                "covered_page_types": ["home"],
            },
        }
    ]
    repository.h5_coverage = {
        "required_page_types": ["home"],
        "covered_page_types": ["home"],
        "missing_page_types": [],
        "assessment_scope_ready": True,
        "completion_ready": True,
    }
    repository.h5_full_closure = {
        "gate_passed": False,
        "gate_block_reasons": ["evidence_insufficient"],
    }

    insight = await _service(repository).report_insight("task-1")

    exploration = insight["page_exploration"]
    assert exploration["complete"] is False
    assert exploration["quality_status"] == "needs_review"
    assert exploration["terminal_reason"] == "evidence_insufficient"
    assert (
        exploration["evidence_preflight"]["invalid_evidence_publication_blocked"]
        is True
    )
    assert exploration["evidence_preflight"]["reason_codes"] == [
        "after:dom_missing"
    ]


async def test_h5_v2_report_uses_same_bounded_terminal_projection_as_timeline() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={
            "task_name": "H5 持续不稳定页面",
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
        },
        working_memory={},
    )
    repository.h5_work_items = [
        {
            "work_id": "work-unstable",
            "status": "bounded",
            "frontier": {
                "snapshot_id": "frontier-unstable",
                "pending_count": 0,
                "covered_page_types": ["home"],
            },
        }
    ]
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-unstable",
            "page_type": "home",
            "page_state": "transitional",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "failed",
            "artifact_ids": ["artifact-shot"],
            "capture_role": "after_action",
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_capture_id": "capture-unstable",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {
                "catalog_sha256": "catalog-1",
                "page_capture_ids": ["capture-unstable"],
                "evidence_artifact_ids": ["artifact-shot"],
            },
        }
    ]
    repository.h5_coverage = {
        "required_page_types": ["home"],
        "covered_page_types": ["home"],
        "missing_page_types": [],
        "assessment_scope_ready": True,
        "completion_ready": True,
    }
    repository.h5_full_closure = {
        "nonterminal_inspection_count": 0,
        "failed_inspection_count": 0,
        "gate_passed": True,
        "gate_block_reasons": [],
    }

    insight = await _service(repository).report_insight("task-1")

    exploration = insight["page_exploration"]
    assert exploration["complete"] is False
    assert exploration["quality_status"] == "needs_review"
    assert exploration["requires_review"] is True
    assert exploration["terminal_reason"] == "work_bounded"
    assert repository.h5_work_reads == [("task-1", "run-1")]


@pytest.mark.parametrize("with_real_judge_gap", [False, True])
async def test_web_v2_report_uses_locked_web_coverage_snapshot(with_real_judge_gap) -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="web",
        task_config={"task_name": "Web V2 报告", "workflow_version": "v2"},
        working_memory={},
    )
    repository.page_rows = [
        {
            "page_capture_id": "capture-home",
            "page_type": "home",
            "page_state": "stable",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
            "artifact_ids": ["artifact-home"],
            "capture_role": "after_action",
        }
    ]
    repository.rows = [
        _row(
            "alioth_web_gap",
            "evidence_insufficient",
            page_capture_id=None,
            source_idempotency_key=(
                "web-coverage-gap:web-snapshot-1:content_detail:alioth_web_gap"
            ),
            judge_input={
                "schema_version": "web-coverage-gap.v1",
                "source": "web_coverage_gap",
                "coverage_snapshot_id": "web-snapshot-1",
                "missing_page_type": "content_detail",
                "reason": "required_page_type_not_discovered",
            },
        ),
        _row("actual-pass", "pass"),
    ]
    if with_real_judge_gap:
        repository.rows.append(_row("actual-judge-gap", "evidence_insufficient"))
    repository.web_coverage = {
        "coverage_snapshot_id": "web-snapshot-1",
        "finalization_epoch": 4,
        "required_page_types": ["content_detail", "home"],
        "observed_page_types": ["home"],
        "missing_page_types": ["content_detail"],
        "unrecognized_page_types": ["web_unknown"],
        "normalization_version": "web-page-type-normalization.v1",
        "normalization_records": [
            {
                "raw_page_type": "home",
                "canonical_page_type": "home",
                "capture_count": 1,
            }
        ],
        "coverage_status": "completed_with_gaps",
        "snapshot_hash": "a" * 64,
    }

    insight = await _service(repository).report_insight("task-1")

    coverage = insight["page_exploration"]["web_coverage"]
    assert coverage["coverage_snapshot_id"] == "web-snapshot-1"
    assert coverage["observed_page_types"] == ["home"]
    assert coverage["missing_page_types"] == ["content_detail"]
    assert coverage["unrecognized_page_types"] == ["web_unknown"]
    assert coverage["normalization_version"] == "web-page-type-normalization.v1"
    assert coverage["normalization_records"] == [
        {
            "raw_page_type": "home",
            "canonical_page_type": "home",
            "capture_count": 1,
        }
    ]
    counts = {item["status"]: item["count"] for item in insight["distribution"]}
    assert counts["coverage_gap"] == 1
    assert insight["summary"]["coverage_gap_rules"] == 1
    assert insight["summary"]["total_rules"] == (2 if with_real_judge_gap else 1)
    assert insight["qc_success_rate"] == (50.0 if with_real_judge_gap else 100.0)
    assert len(insight["rule_items"]) == len(repository.rows)
    assert insight["rule_items"][0]["execution_id"] == "exec-alioth_web_gap"
    assert insight["rule_items"][0]["execution_status"] == "evidence_insufficient"
    assert insight["rule_items"][0]["is_bootstrap"] is False
    assert repository.web_coverage_reads == [("task-1", "run-1")]
    assert repository.h5_coverage_reads == []
    assert repository.h5_assessment_reads == [("task-1", "run-1")]


@pytest.mark.parametrize(
    ("gate_passed", "report_bound", "expected_stage"),
    [
        (False, True, "final_review"),
        (True, False, "final_review"),
        (True, True, "final"),
    ],
)
async def test_web_report_visibility_requires_passed_bound_gate(
    gate_passed: bool,
    report_bound: bool,
    expected_stage: str,
) -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="web",
        task_config={"task_name": "Web V2 报告门禁", "workflow_version": "v2"},
        status="done",
        finalization_state="sealed",
        completion_gate_passed=gate_passed,
        sealed_report_bound=report_bound,
    )
    repository.web_coverage = {
        "coverage_snapshot_id": "web-snapshot-1",
        "finalization_epoch": 4,
        "required_page_types": ["home"],
        "observed_page_types": ["home"],
        "missing_page_types": [],
        "unrecognized_page_types": [],
        "normalization_version": "web-page-type-normalization.v1",
        "coverage_status": "complete",
        "snapshot_hash": "b" * 64,
    }

    insight = await _service(repository).report_insight("task-1")

    assert insight["summary"]["report_stage"] == expected_stage


class FakeFinalReportSnapshotRepository:
    def __init__(self, snapshot: dict[str, Any] | None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str | None]] = []

    async def get_final_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        del access_scope
        self.calls.append((task_id, agent_run_id))
        return self.snapshot


async def test_web_v2_final_insight_reads_bound_immutable_snapshot() -> None:
    repository = FakeReportRepository()
    repository.context = None
    repository.rows = [_row("live-mutated-rule", "fail")]
    snapshot_repository = FakeFinalReportSnapshotRepository(
        {
            "report_id": "task:task-1:run:run-1",
            "report_binding_version": 2,
            "report_contract": {
                "target_platform": "web",
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "notification_mode": "enforced",
            },
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": "run-1",
            "generated_at": "2026-09-04T00:02:00+00:00",
            "task": {
                "task_id": "task-1",
                "target_url": "https://example.com",
                "status": "done",
                "created_at": "2026-09-04T00:00:00+00:00",
                "started_at": "2026-09-04T00:00:10+00:00",
                "finished_at": "2026-09-04T00:02:00+00:00",
                "ruleset_code": "alioth_first_ruleset",
                "ruleset_version": 1,
            },
            "summary": {
                "total_rules": 1,
                "pass": 1,
                "fail": 0,
                "warning": 0,
                "skipped": 0,
                "evidence_insufficient": 0,
                "coverage_gap_rules": 0,
                "qc_success_rate": 100.0,
                "pass_rate": 100.0,
                "candidate_execution_coverage": 100.0,
            },
            "completion_gate": {
                "gate_passed": True,
                "gate_snapshot_id": "gate-1",
                "finalization_epoch": 4,
            },
            "page_exploration": {
                "recognized_pages": 1,
                "web_coverage": {
                    "coverage_snapshot_id": "coverage-1",
                    "snapshot_hash": "coverage-hash-1",
                    "finalization_epoch": 4,
                },
            },
            "rule_results": [
                {
                    "execution_id": "snapshot-execution",
                    "rule_id": "snapshot-rule",
                    "rule_code": "snapshot-rule",
                    "rule_name": "快照规则",
                    "page_capture_id": "capture-home",
                    "execution_status": "pass",
                    "confidence": 0.98,
                    "severity": "low",
                    "result_summary": "快照结论",
                    "problem_desc": None,
                    "review_status": None,
                    "source": "agent",
                    "finished_at": "2026-09-04T00:01:00+00:00",
                }
            ],
            "evidence_entries": [
                {
                    "artifact_id": "snapshot-shot",
                    "rule_execution_id": "snapshot-execution",
                }
            ],
        }
    )
    service = ReportInsightService(
        repository=repository,
        final_report_snapshot_repository=snapshot_repository,
        evidence_store_factory=lambda: FakeEvidenceStore({}),
    )

    insight = await service.report_insight("task-1")

    assert snapshot_repository.calls == [("task-1", None)]
    assert insight["report_availability"] == "final"
    assert insight["summary"]["task_name"] == "https://example.com"
    assert insight["summary"]["generated_at"] == "2026-09-04T00:02:00+00:00"
    assert [item["execution_id"] for item in insight["rule_items"]] == [
        "snapshot-execution"
    ]
    assert insight["rule_items"][0]["evidence_artifact_ids"] == ["snapshot-shot"]
    assert not any(name == "list_rule_execution_rows" for name, _ in repository.scope_calls)


async def test_h5_v2_final_insight_reads_bound_immutable_snapshot() -> None:
    repository = FakeReportRepository()
    repository.context = None
    repository.rows = [_row("live-mutated-rule", "fail")]
    snapshot_repository = FakeFinalReportSnapshotRepository(
        {
            "report_id": "task:task-1:run:run-1",
            "report_binding_version": 1,
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": "run-1",
            "generated_at": "2026-09-04T00:02:00+00:00",
            "task": {
                "task_id": "task-1",
                "task_name": "H5 正式报告",
                "target_platform": "h5",
                "target_url": "https://example.com/h5",
                "status": "done",
                "task_config": {
                    "workflow_version": "v2",
                    "execution_mode": "full_inspection",
                },
            },
            "summary": {
                "total_rules": 1,
                "pass": 1,
                "qc_success_rate": 100.0,
                "pass_rate": 100.0,
            },
            "completion_gate": {
                "gate_passed": True,
                "gate_snapshot_id": "gate-1",
                "finalization_epoch": 4,
            },
            "page_exploration": {"recognized_pages": 1},
            "rule_results": [
                {
                    "execution_id": "snapshot-execution",
                    "rule_id": "snapshot-rule",
                    "rule_code": "snapshot-rule",
                    "rule_name": "H5 快照规则",
                    "execution_status": "pass",
                    "source": "agent",
                    "evidence_artifact_ids": ["snapshot-h5-shot"],
                    "evidence_input_set_id": "evidence-input-set:h5",
                    "missing_evidence": ["after_action_screenshot"],
                }
            ],
            "evidence_entries": [
                {
                    "artifact_id": "snapshot-h5-shot",
                    "page_capture_id": "capture-h5",
                }
            ],
        }
    )
    service = ReportInsightService(
        repository=repository,
        final_report_snapshot_repository=snapshot_repository,
    )

    insight = await service.report_insight("task-1")

    assert insight["summary"]["target_platform"] == "h5"
    assert insight["summary"]["task_name"] == "H5 正式报告"
    assert insight["report_availability"] == "final"
    assert [item["execution_id"] for item in insight["rule_items"]] == [
        "snapshot-execution"
    ]
    assert insight["rule_items"][0]["evidence_artifact_ids"] == [
        "snapshot-h5-shot"
    ]
    assert insight["rule_items"][0]["evidence_input_set_id"] == (
        "evidence-input-set:h5"
    )
    assert insight["rule_items"][0]["missing_evidence"] == [
        "after_action_screenshot"
    ]
    assert not any(name == "list_rule_execution_rows" for name, _ in repository.scope_calls)


class FakeRulesetReportRepository(FakeReportRepository):
    """带「规则 → 所属规则集」查询的仓库。"""

    def __init__(self, rulesets: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.rulesets = rulesets
        self.ruleset_reads: list[list[str]] = []

    async def list_rule_rulesets(self, *, rule_ids: list[str]) -> dict[str, dict[str, Any]]:
        self.ruleset_reads.append(rule_ids)
        return {rule_id: self.rulesets[rule_id] for rule_id in rule_ids if rule_id in self.rulesets}


async def test_rule_items_carry_their_own_phase_ruleset() -> None:
    """报告页按「几期规则」分组：每条结果带上规则自己那一期的规则集，查不到的留空。"""
    repository = FakeRulesetReportRepository(
        {
            "rule-alioth_2": {"ruleset_code": "alioth_first_ruleset", "ruleset_name": "1期质检规则集"},
            "rule-alioth_ops_1": {
                "ruleset_code": "alioth_ruisheng_ruleset",
                "ruleset_name": "3期瑞升质检规则集",
            },
        }
    )
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_2", "fail", execution_id="exec-alioth_2-list", capture_page_type="video_list"),
        _row("alioth_ops_1", "manual_review"),
        _row("alioth_gone", "pass"),
    ]

    insight = await _service(repository).report_insight("task-1")

    assert repository.ruleset_reads == [["rule-alioth_2", "rule-alioth_gone", "rule-alioth_ops_1"]]
    assert [
        (item["execution_id"], item["ruleset_code"], item["ruleset_name"])
        for item in insight["rule_items"]
    ] == [
        ("exec-alioth_2", "alioth_first_ruleset", "1期质检规则集"),
        ("exec-alioth_2-list", "alioth_first_ruleset", "1期质检规则集"),
        ("exec-alioth_ops_1", "alioth_ruisheng_ruleset", "3期瑞升质检规则集"),
        ("exec-alioth_gone", None, None),
    ]
    # 人工确认清单和结果列表是同一批结果，归属一样
    assert [item["ruleset_name"] for item in insight["confirm_items"]] == ["3期瑞升质检规则集"]


async def test_sealed_report_rule_items_also_carry_phase_ruleset() -> None:
    """封存的正式报告正文不变，展示时同样补上规则所属的那一期。"""
    repository = FakeRulesetReportRepository(
        {"snapshot-rule": {"ruleset_code": "alioth_phase2_ruleset", "ruleset_name": "2期质检规则集"}}
    )
    repository.context = None
    snapshot_repository = FakeFinalReportSnapshotRepository(
        {
            "report_id": "task:task-1:run:run-1",
            "report_binding_version": 1,
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": "run-1",
            "generated_at": "2026-09-04T00:02:00+00:00",
            "task": {
                "task_id": "task-1",
                "task_name": "H5 正式报告",
                "target_platform": "h5",
                "target_url": "https://example.com/h5",
                "status": "done",
                "task_config": {
                    "workflow_version": "v2",
                    "execution_mode": "full_inspection",
                },
            },
            "summary": {"total_rules": 1, "pass": 1, "qc_success_rate": 100.0, "pass_rate": 100.0},
            "completion_gate": {
                "gate_passed": True,
                "gate_snapshot_id": "gate-1",
                "finalization_epoch": 4,
            },
            "page_exploration": {"recognized_pages": 1},
            "rule_results": [
                {
                    "execution_id": "snapshot-execution",
                    "rule_id": "snapshot-rule",
                    "rule_code": "snapshot-rule",
                    "rule_name": "H5 快照规则",
                    "execution_status": "pass",
                    "source": "agent",
                }
            ],
            "evidence_entries": [],
        }
    )
    service = ReportInsightService(
        repository=repository,
        final_report_snapshot_repository=snapshot_repository,
    )

    insight = await service.report_insight("task-1")

    assert insight["report_availability"] == "final"
    assert insight["rule_items"][0]["ruleset_code"] == "alioth_phase2_ruleset"
    assert insight["rule_items"][0]["ruleset_name"] == "2期质检规则集"


async def test_report_summary_carries_ruleset_rule_total() -> None:
    """规则结果汇总的「规则总数」取建任务时记下的启用规则条数（老任务由仓库查询按现在启用的兜底）。"""
    repository = FakeReportRepository()
    repository.context = {**repository.context, "ruleset_rule_total": 73}
    repository.rows = [_row("alioth_2", "pass")]

    insight = await _service(repository).report_insight("task-1")

    assert insight["summary"]["ruleset_rule_total"] == 73


@pytest.mark.parametrize(("context", "expected"), [({"ruleset_rule_total": 61}, 61), (None, None)])
async def test_sealed_report_takes_ruleset_rule_total_from_task(
    context: dict[str, Any] | None, expected: int | None
) -> None:
    """封存报告正文里没有规则总数，从任务上补；任务查不到时不给，页面按实际使用的条数显示。"""
    repository = FakeRulesetReportRepository({})
    repository.context = context
    snapshot_repository = FakeFinalReportSnapshotRepository(
        {
            "report_id": "task:task-1:run:run-1",
            "report_binding_version": 1,
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": "run-1",
            "generated_at": "2026-09-04T00:02:00+00:00",
            "task": {
                "task_id": "task-1",
                "task_name": "H5 正式报告",
                "target_platform": "h5",
                "target_url": "https://example.com/h5",
                "status": "done",
                "task_config": {"workflow_version": "v2", "execution_mode": "full_inspection"},
            },
            "summary": {"total_rules": 1, "pass": 1, "qc_success_rate": 100.0, "pass_rate": 100.0},
            "completion_gate": {"gate_passed": True, "gate_snapshot_id": "gate-1", "finalization_epoch": 4},
            "page_exploration": {"recognized_pages": 1},
            "rule_results": [
                {
                    "execution_id": "snapshot-execution",
                    "rule_id": "snapshot-rule",
                    "rule_code": "snapshot-rule",
                    "rule_name": "H5 快照规则",
                    "execution_status": "pass",
                    "source": "agent",
                }
            ],
            "evidence_entries": [],
        }
    )
    service = ReportInsightService(
        repository=repository,
        final_report_snapshot_repository=snapshot_repository,
    )

    insight = await service.report_insight("task-1")

    assert insight["report_availability"] == "final"
    assert insight["summary"]["ruleset_rule_total"] == expected


async def test_h5_preview_exposes_missing_evidence_and_input_set() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={
            "task_name": "H5 证据展示",
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
        },
    )
    repository.rows = [
        _row(
            "alioth_missing",
            "evidence_insufficient",
            missing_evidence=["after_action_screenshot", "dom_snapshot"],
            evidence_input_set_id="evidence-input-set:preview",
        )
    ]

    insight = await _service(repository).report_insight("task-1")

    item = insight["rule_items"][0]
    assert item["missing_evidence"] == ["after_action_screenshot", "dom_snapshot"]
    assert item["evidence_input_set_id"] == "evidence-input-set:preview"


def test_h5_v2_final_snapshot_requires_canonical_binding_identity() -> None:
    snapshot = {
        "report_id": "task:task-1:run:run-1",
        "report_binding_version": 1,
        "report_stage": "final",
        "is_final": True,
        "agent_run_id": "run-1",
        "task": {
            "task_id": "task-1",
            "target_platform": "h5",
            "task_config": {
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
            },
        },
        "completion_gate": {
            "gate_passed": True,
            "gate_snapshot_id": "gate-1",
            "finalization_epoch": 4,
        },
    }

    assert _is_h5_v2_final_snapshot(snapshot, task_id="task-1") is True
    assert (
        _is_h5_v2_final_snapshot(
            {**snapshot, "report_id": "task:task-1:run:stale-run"},
            task_id="task-1",
        )
        is False
    )
    assert (
        _is_h5_v2_final_snapshot(
            {**snapshot, "report_binding_version": 0},
            task_id="task-1",
        )
        is False
    )


async def test_web_final_snapshot_rejects_gate_coverage_epoch_mismatch() -> None:
    repository = FakeReportRepository()
    repository.context = None
    snapshot_repository = FakeFinalReportSnapshotRepository(
        {
            "report_id": "task:task-1:run:run-1",
            "report_binding_version": 2,
            "report_contract": {
                "target_platform": "web",
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "notification_mode": "enforced",
            },
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": "run-1",
            "task": {"task_id": "task-1"},
            "completion_gate": {
                "gate_passed": True,
                "gate_snapshot_id": "gate-1",
                "finalization_epoch": 4,
            },
            "page_exploration": {
                "web_coverage": {
                    "coverage_snapshot_id": "coverage-1",
                    "snapshot_hash": "coverage-hash-1",
                    "finalization_epoch": 3,
                }
            },
            "rule_results": [],
            "evidence_entries": [],
        }
    )
    service = ReportInsightService(
        repository=repository,
        final_report_snapshot_repository=snapshot_repository,
    )

    with pytest.raises(ReportInsightNotFoundError, match="绑定校验"):
        await service.report_insight("task-1")


async def test_web_final_export_uses_only_frozen_checksum_verified_evidence() -> None:
    repository = FakeReportRepository()
    repository.screenshots = [
        {
            "artifact_id": "live-replacement",
            "storage_path": "live.png",
            "content_type": "image/png",
        }
    ]
    frozen = b"frozen-image"
    service = ReportInsightService(
        repository=repository,
        evidence_store_factory=lambda: FakeEvidenceStore(
            {"frozen.png": frozen, "live.png": b"live"}
        ),
    )
    insight = {
        "task_id": "task-1",
        "summary": {"run_id": "run-1", "target_platform": "web"},
        "report_availability": "final",
        "evidence_entries": [
            {
                "artifact_id": "frozen-1",
                "storage_path": "frozen.png",
                "content_type": "image/png",
                "checksum": f"sha256:{hashlib.sha256(frozen).hexdigest()}",
            }
        ],
    }

    images = await service._collect_export_images(  # noqa: SLF001
        insight,
        [{"evidence_artifact_ids": ["frozen-1", "live-replacement"]}],
    )

    assert set(images) == {"frozen-1"}
    assert not any(name == "list_screenshot_artifacts" for name, _ in repository.scope_calls)


async def test_h5_v2_report_does_not_fallback_to_stale_working_memory() -> None:
    """正式表为空时必须失败关闭，不能恢复过期 WorkingMemory Assessment。"""
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={"task_name": "H5 V2 报告", "workflow_version": "v2"},
        working_memory={
            "page_type_assessments": [
                {
                    "page_type": "settings",
                    "status": "evidence_insufficient",
                    "reason_code": "stale_working_memory",
                }
            ],
            "page_type_assessment_scope_complete": True,
        },
    )

    insight = await _service(repository).report_insight("task-1")

    product = insight["page_exploration"]["coverage"]["product_business"]
    assert product["assessment_available"] is False
    assert product["assessment_scope_complete"] is False
    assert product["applicable_page_types"] is None
    assert product["covered_page_types"] == []
    assert product["coverage_rate"] is None


async def test_h5_v2_report_does_not_count_unbound_formal_coverage() -> None:
    """正式 covered 缺少匹配 Artifact 时仍须降级，不能因换事实源虚增覆盖。"""
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={"task_name": "H5 V2 报告", "workflow_version": "v2"},
        working_memory={},
    )
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-home",
            "page_type": "home",
            "page_state": "stable",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
            "artifact_ids": ["artifact-actual"],
            "capture_role": "after_action",
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_capture_id": "capture-home",
            "page_type": "home",
            "page_state": "stable",
            "status": "covered",
            "reason_codes": ["recognized_with_evidence"],
            "assessment_payload": {
                "catalog_sha256": "catalog-1",
                "page_capture_ids": ["capture-home"],
                "evidence_artifact_ids": ["artifact-missing"],
                "exploration_action_ids": [],
            },
        }
    ]

    insight = await _service(repository).report_insight("task-1")

    product = insight["page_exploration"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["covered_page_types"] == []
    assert product["coverage_rate"] is None
    assert insight["page_exploration"]["coverage"]["gaps"][0]["status"] == ("evidence_insufficient")


@pytest.mark.parametrize(
    ("target_platform", "task_config"),
    [
        ("h5", {"task_name": "H5 V1 报告", "workflow_version": "v1"}),
        ("web", {"task_name": "非 H5 报告"}),
    ],
)
async def test_legacy_reports_keep_working_memory_assessments(
    target_platform: str,
    task_config: dict[str, Any],
) -> None:
    """H5 V1 与非 H5 历史任务继续读取旧 WorkingMemory 合同。"""
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform=target_platform,
        task_config=task_config,
        working_memory={
            "page_type_assessments": [
                {
                    "page_type": "settings",
                    "status": "evidence_insufficient",
                    "reason_code": "legacy_assessment",
                }
            ],
            "page_type_assessment_scope_complete": True,
        },
    )

    insight = await _service(repository).report_insight("task-1")

    product = insight["page_exploration"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["applicable_page_types"] == ["settings"]
    assert repository.h5_assessment_reads == []


async def test_report_insight_exposes_only_safe_visual_navigation_aggregates() -> None:
    """报告中心复用统一摘要，不能把 Capture 内的视觉私密材料向外透传。"""
    repository = FakeReportRepository()
    repository.page_rows = [
        {
            "page_capture_id": "capture-visual",
            "visual_navigation_audit": {
                "protocol_version": "visual_navigation_audit.v1",
                "mode": "shadow",
                "detector_status": "recognized",
                "prediction_count": 1,
                "target_kind_counts": {"primary_navigation": 1},
                "signed_capability_count": 1,
                "policy_rejection_counts": {},
                "target_signature": "sha256:private-signature",
                "recognized_label": "立即支付",
            },
        }
    ]

    insight = await _service(repository).report_insight("task-1")

    visual = insight["page_exploration"]["visual_navigation"]
    assert visual["mode"] == "shadow"
    assert visual["eligible_capture_count"] == 1
    serialized = json.dumps(visual, ensure_ascii=False)
    assert "target_signature" not in serialized
    assert "sha256:private-signature" not in serialized
    assert "立即支付" not in serialized


async def test_popup_wall_termination_surfaces_as_report_finding() -> None:
    """弹窗墙终止串应转成报告可见的结构化发现，而不是只躺在 run 报错里。"""
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context["run_error"] = "popup_layer_limit_exceeded:20/20"
    repository.rows = [_row("alioth_2", "pass")]

    insight = await _service(repository).report_insight("task-1")

    popup_wall = insight["page_exploration"]["popup_wall"]
    assert popup_wall["kind"] == "popup_layer_limit_exceeded"
    assert popup_wall["closed_count"] == 20
    assert popup_wall["limit"] == 20
    assert "广告弹窗" in popup_wall["note"]

    repository.context["run_error"] = "popup_dismissal_stalled:3/3"
    insight = await _service(repository).report_insight("task-1")
    stalled = insight["page_exploration"]["popup_wall"]
    assert stalled["kind"] == "popup_dismissal_stalled"
    assert stalled["closed_count"] == 3
    assert "关闭未见效" in stalled["note"]

    repository.context["run_error"] = None
    insight = await _service(repository).report_insight("task-1")
    assert "popup_wall" not in insight["page_exploration"]


async def test_app_report_uses_persisted_target_coverage_and_visual_quality() -> None:
    repository = FakeAppReportRepository()

    insight = await _service(repository).report_insight("task-1")

    page_exploration = insight["page_exploration"]
    assert page_exploration["tracking_available"] is True
    assert page_exploration["complete"] is False
    assert page_exploration["quality_status"] == "needs_review"
    assert page_exploration["requires_review"] is True
    assert page_exploration["app_coverage"]["app_target_snapshot"]["target_hash"] == (
        "sha256:target"
    )
    assert (
        page_exploration["app_coverage"]["degraded_provider_facts"][0]["provider_id"]
        == "llm-app-vision"
    )
    product = page_exploration["coverage"]["product_business"]
    assert product["authority_source"] == "run_observation_and_frontier_exhaustion"
    assert product["applicable_page_types"] == ["home", "profile"]
    assert product["not_applicable_page_types"] == ["payment"]
    assert product["coverage_rate"] == 0.5


async def test_report_counts_budget_termination_outside_evidence_insufficient() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row(
            "alioth_17",
            "skipped",
            execution_reason="run_budget_exhausted",
        ),
        _row("alioth_21", "evidence_insufficient"),
    ]

    insight = await _service(repository).report_insight("task-1")
    counts = {item["status"]: item["count"] for item in insight["distribution"]}

    # 预算终止与证据不足没有业务结论，计入其他但不能人工补成通过或未通过。
    assert counts["other"] == 2
    assert counts["not_applicable"] == 0
    confirm_codes = {item["rule_code"] for item in insight["confirm_items"]}
    assert confirm_codes == set()


async def test_report_splits_confirmed_not_applicable_from_coverage_gap() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_16", "skipped"),
        _row("alioth_17", "skipped", is_bootstrap=True),
    ]

    insight = await _service(repository).report_insight("task-1")
    counts = {item["status"]: item["count"] for item in insight["distribution"]}
    by_code = {item["rule_code"]: item for item in insight["rule_items"]}

    assert counts["not_applicable"] == 1
    assert counts["coverage_gap"] == 1
    assert by_code["alioth_16"]["display_status"] == "not_applicable"
    assert by_code["alioth_16"]["display_status_label"] == "本产品不适用"
    assert by_code["alioth_17"]["display_status"] == "coverage_gap"
    assert by_code["alioth_17"]["display_status_label"] == "计划外未覆盖"
    assert insight["summary"]["confirmed_not_applicable_rules"] == 1
    assert insight["summary"]["coverage_gap_rules"] == 1
    assert insight["summary"]["full_rule_pass_rate"] == 33.3
    assert insight["qc_success_rate"] == 100.0
    assert insight["summary"]["total_rules"] == 2
    assert insight["summary"]["candidate_execution_coverage"] == 100.0
    assert insight["summary"]["ruleset_candidate_coverage"] == 66.7


async def test_report_counts_warning_as_completed_without_marking_it_passed() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "warning"),
        _row("alioth_17", "skipped", is_bootstrap=True),
    ]

    insight = await _service(repository).report_insight("task-1")

    assert insight["qc_success_rate"] == 100.0
    assert insight["summary"]["total_rules"] == 1
    assert insight["pass_rate"] == 0.0


async def test_report_distribution_uses_human_verdict_as_final_result() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "fail", human_verdict="pass"),
        _row("alioth_3", "pass", human_verdict="fail"),
        _row("alioth_4", "pass", human_verdict="ignored"),
        _row("alioth_5", "error"),
    ]

    insight = await _service(repository).report_insight("task-1")
    counts = {item["status"]: item["count"] for item in insight["distribution"]}

    assert counts == {
        "passed": 1,
        "failed": 1,
        "not_applicable": 1,
        "coverage_gap": 0,
        "detect_error": 1,
        "other": 0,
    }
    assert insight["qc_success_rate"] == 75.0
    assert insight["summary"]["full_rule_pass_rate"] == 25.0


async def test_report_keeps_candidate_completion_separate_from_ruleset_coverage() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_5", "evidence_insufficient", is_bootstrap=True),
        _row("alioth_17", "skipped", is_bootstrap=True),
    ]

    insight = await _service(repository).report_insight("task-1")

    assert insight["summary"]["candidate_execution_coverage"] == 50.0
    assert insight["summary"]["ruleset_candidate_coverage"] == 66.7


async def test_confirm_rule_scope_and_validation() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_5", "manual_review", category="input_risk"),
    ]
    service = _service(repository)

    result = await service.confirm_rule(
        "task-1", "exec-alioth_5", verdict="ignored", note="产品不含该功能"
    )
    assert result["human_verdict"] == "ignored"
    assert result["human_verdict_label"] == "本产品不适用"
    assert repository.updates == [
        {"execution_id": "exec-alioth_5", "verdict": "ignored", "note": "产品不含该功能"}
    ]

    with pytest.raises(ReportConfirmationError):
        await service.confirm_rule("task-1", "exec-alioth_2", verdict="pass")
    with pytest.raises(ReportConfirmationError):
        await service.confirm_rule("task-1", "exec-alioth_5", verdict="maybe")
    with pytest.raises(ReportInsightNotFoundError):
        await service.confirm_rule("task-1", "exec-none", verdict="pass")


async def test_report_insight_propagates_authenticated_scope_to_every_read() -> None:
    repository = FakeReportRepository()
    scope = AccessScope(
        actor_user_id="owner-1",
        tenant_id="tenant-a",
        permissions=frozenset({"taskObjects:read:owner"}),
    )

    await _service(repository).report_insight("task-1", access_scope=scope)

    assert {name for name, _scope in repository.scope_calls} == {
        "fetch_task_context",
        "list_rule_execution_rows",
        "count_recognized_pages",
        "list_page_capture_inspection_rows",
        "list_exploration_actions",
    }
    assert all(call_scope == scope for _name, call_scope in repository.scope_calls)


async def test_confirm_rule_uses_path_task_and_authenticated_actor_for_audit() -> None:
    repository = FakeReportRepository()
    repository.rows = [_row("alioth_5", "manual_review", category="input_risk")]
    scope = AccessScope(
        actor_user_id="reviewer-1",
        tenant_id="tenant-a",
        permissions=frozenset({"taskObjects:read:tenant", "taskObjects:review:tenant"}),
    )

    result = await _service(repository).confirm_rule(
        "task-1",
        "exec-alioth_5",
        verdict="pass",
        note="人工确认",
        access_scope=scope,
    )

    assert result["human_verdict"] == "pass"
    assert repository.scope_calls[-1] == ("fetch_rule_execution", scope)
    assert repository.updates[-1] == {
        "execution_id": "exec-alioth_5",
        "verdict": "pass",
        "note": "人工确认",
        "task_id": "task-1",
        "operator": "reviewer-1",
        "access_scope": scope,
    }


async def test_machine_pass_and_fail_take_effect_without_confirmation() -> None:
    # 2.1 口径：通过 / 未通过直接生效；审核队列的 pending 标记不再单独触发人工确认。
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass", review_status="pending", category="input_risk"),
        _row("alioth_4", "fail"),
    ]
    service = _service(repository)

    insight = await service.report_insight("task-1")
    by_code = {item["rule_code"]: item for item in insight["rule_items"]}
    assert by_code["alioth_2"]["confirm_status"] is None
    assert by_code["alioth_4"]["confirm_status"] is None
    assert insight["export_ready"] is True

    with pytest.raises(ReportConfirmationError):
        await service.confirm_rule("task-1", "exec-alioth_4", verdict="ignored")


async def test_export_blocked_until_confirmations_complete() -> None:
    repository = FakeReportRepository()
    repository.rows = [_row("alioth_5", "manual_review", category="input_risk")]
    service = _service(repository)
    with pytest.raises(ReportExportBlockedError):
        await service.export_html("task-1")


async def test_web_preview_report_is_visible_but_not_exportable() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="web",
        task_config={"task_name": "Web V2 运行中预览", "workflow_version": "v2"},
        status="running",
        finalization_state="session_draining",
        completion_gate_passed=False,
        sealed_report_bound=False,
    )
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_5", "evidence_insufficient"),
        _row("alioth_6", "error"),
    ]
    service = _service(repository)

    insight = await service.report_insight("task-1")

    assert insight["summary"]["report_stage"] == "preview"
    assert insight["report_availability"] == "preview"
    assert insight["web_preview"]["inspection_progress"] == {
        "total": 3,
        "completed": 3,
        "pending": 0,
    }
    assert insight["web_preview"]["completion_gate"]["gate_passed"] is False
    assert "completion_gate_not_passed" in insight["web_preview"]["completion_gate"][
        "gaps"
    ]
    assert insight["export_ready"] is False
    with pytest.raises(ReportExportBlockedError, match="预览报告"):
        await service.export_html("task-1")


async def test_web_report_without_persisted_facts_uses_none_state_with_reason() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="web",
        task_config={"task_name": "Web 尚无报告事实", "workflow_version": "v2"},
        status="queued",
        finalization_state=None,
        completion_gate_passed=False,
        sealed_report_bound=False,
    )
    repository.recognized_pages = 0

    insight = await _service(repository).report_insight("task-1")

    assert insight["report_availability"] == "none"
    assert insight["report_unavailable_reason"] == "WEB_REPORT_NO_PERSISTED_FACTS"
    assert insight["web_preview"]["trusted_capture_count"] == 0
    assert insight["web_preview"]["inspection_progress"] == {
        "total": 0,
        "completed": 0,
        "pending": 0,
    }


async def test_h5_review_report_is_visible_and_exportable_after_confirmation() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="h5",
        task_config={"task_name": "H5 复核报告", "workflow_version": "v2"},
        status="review",
        finalization_state="session_draining",
        completion_gate_passed=False,
        sealed_report_bound=False,
    )
    repository.rows = [_row("alioth_2", "pass")]
    service = _service(repository)

    insight = await service.report_insight("task-1")

    assert insight["summary"]["report_stage"] == "final_review"
    assert insight["report_availability"] == "preview"
    assert insight["is_final"] is False
    assert insight["export_allowed"] is True
    assert insight["export_ready"] is True
    assert insight["incomplete_reasons"] == ["H5_REPORT_NOT_FINALIZED"]
    filename, html_text = await service.export_html("task-1")
    assert filename == "task_task-1_report.html"
    assert "H5 复核报告 质检复核报告" in html_text


@pytest.mark.parametrize(
    "reason", ["exploration_not_started", "auth_requirement_not_met", "not_matched_by_run"]
)
async def test_h5_failed_run_placeholders_are_not_business_report_results(reason: str) -> None:
    repository = FakeReportRepository()
    repository.context.update(
        target_platform="h5", status="review", run_status="failed",
        target_url="https://dys18.com/",
        task_config={"workflow_version": "v2", "ruleset_rule_total": 73},
    )
    repository.rows = [
        _row(f"rule-{index}", "skipped", execution_reason=reason)
        for index in range(73)
    ] + [_row("missing-evidence", "evidence_insufficient")]
    service = _service(repository)
    insight = await service.report_insight("task-1")
    assert insight["qc_success_rate"] == 0
    assert insight["summary"]["candidate_execution_coverage"] == 0
    assert insight["summary"]["confirmed_not_applicable_rules"] == 0
    assert insight["report_availability"] == "none"
    assert insight["report_unavailable_reason"] == "H5_REPORT_NO_BUSINESS_RESULTS"
    assert insight["export_ready"] is False
    assert insight["export_allowed"] is False
    assert len(insight["rule_items"]) == 74
    with pytest.raises(ReportExportBlockedError, match="尚无真实规则质检结果"):
        await service.export_html("task-1")


async def test_h5_partial_business_results_remain_review_report_with_zero_gap_completion() -> None:
    repository = FakeReportRepository()
    repository.context.update(target_platform="h5", task_config={"workflow_version": "v2"})
    repository.rows = [
        _row("real-result", "pass"),
        _row("not-run", "skipped", execution_reason="exploration_not_started"),
        _row("missing-evidence", "evidence_insufficient"),
    ]
    service = _service(repository)
    insight = await service.report_insight("task-1")
    assert insight["report_availability"] == "preview"
    assert insight["summary"]["candidate_execution_coverage"] == 33.3
    assert insight["export_allowed"] is True
    _, html = await service.export_html("task-1")
    assert "本报告不代表检测已完整完成" in html


async def test_h5_outside_capture_is_diagnostic_failure_without_rewriting_real_url() -> None:
    repository = FakeReportRepository()
    repository.context.update(
        target_platform="h5", target_url="https://dys18.com/",
        task_config={"workflow_version": "v2"}, run_status="failed",
    )
    repository.h5_page_rows = [{
        "page_capture_id": "outside-capture", "agent_run_id": "run-1",
        "url": "https://example.com/", "capture_role": "after_action",
        "detection_status": "unrecognized", "capture_status": "captured",
    }]
    repository.rows = [_row("not-run", "skipped", execution_reason="exploration_not_started")]
    insight = await _service(repository).report_insight("task-1")
    assert insight["summary"]["target_url"] == "https://dys18.com/"
    assert insight["page_exploration"]["quality_status"] == "technical_failed"
    assert insight["page_exploration"]["requires_review"] is False
    assert insight["page_exploration"]["terminal_reason"] == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert insight["report_availability"] == "none"
    assert repository.h5_page_rows[0]["url"] == "https://example.com/"


async def test_web_report_groups_baseline_and_primary_content_as_one_logical_page() -> None:
    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="web",
        task_config={"task_name": "Web 逻辑页报告", "workflow_version": "v2"},
        status="running",
    )
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-ready",
            "page_seq": 1,
            "page_type": "content_detail",
            "url": "https://EXAMPLE.com/news?id=42&utm_source=feed",
            "capture_purpose": "web_page_ready_baseline",
            "capture_role": "after_action",
            "captured_at": NOW,
            "inspection_status": "completed",
            "screenshot_artifact_id": "shot-ready",
            "artifact_ids": ["shot-ready", "dom-ready"],
        },
        {
            "page_capture_id": "capture-content",
            "page_seq": 2,
            "page_type": None,
            "url": "https://example.com/news?id=42#body",
            "capture_purpose": "web_primary_content_evidence",
            "capture_role": "after_action",
            "captured_at": NOW,
            "inspection_status": None,
            "screenshot_artifact_id": "shot-content",
            "artifact_ids": ["shot-content", "dom-content"],
        },
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-ready",
            "page_capture_id": "capture-ready",
            "page_type": "content_detail",
            "status": "covered",
            "assessment_payload": {
                "classification_status": "classified",
                "final_page_type": "content_detail",
                "raw_page_type": "Content_Detail",
                "proposed_page_type": "content_detail",
                "observed_page_type": "content_detail",
                "expected_page_type": "video_list",
                "conflict_reason_code": "URL_HINT_DOM_CONFLICT",
                "diagnostic_message": "目录提示与 DOM 主体冲突",
                "confidence": 0.96,
            },
        }
    ]

    insight = await _service(repository).report_insight("task-1")

    assert insight["summary"]["page_recognized_count"] == 1
    assert insight["page_exploration"]["logical_page_total"] == 1
    page = insight["web_logical_pages"][0]
    assert page["canonical_url"] == "https://example.com/news"
    assert page["primary_capture_id"] == "capture-content"
    assert page["page_type"] == "content_detail"
    assert page["raw_page_type"] == "Content_Detail"
    assert page["proposed_page_type"] == "content_detail"
    assert page["observed_page_type"] == "content_detail"
    assert page["expected_page_type"] == "video_list"
    assert page["conflict_reason_code"] == "URL_HINT_DOM_CONFLICT"
    assert page["diagnostic_message"] == "目录提示与 DOM 主体冲突"
    assert page["confidence"] == pytest.approx(0.96)
    assert page["review_reason"] == "URL_HINT_DOM_CONFLICT"
    assert [item["capture_purpose"] for item in page["evidence_gallery"]] == [
        "web_page_ready_baseline",
        "web_primary_content_evidence",
    ]


def test_web_v2_logical_pages_project_supplemental_states_and_keep_age_timeline() -> None:
    from services.api.app.application.report_insight import _web_report_logical_pages

    rows = [
        {
            "page_capture_id": "capture-gate",
            "page_seq": 1,
            "url": "https://example.test/",
            "page_type": "age_gate",
            "capture_role": "after_action",
            "capture_purpose": "web_page_probe",
            "screenshot_artifact_id": "shot-gate",
            "artifact_ids": ["shot-gate", "dom-gate"],
            "supplemental_full_page": {
                "status": "failed",
                "capture_id": None,
                "screenshot_artifact_id": None,
                "reason_code": "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
            },
        },
        {
            "page_capture_id": "capture-home",
            "page_seq": 2,
            "url": "https://example.test/",
            "page_type": "home",
            "capture_role": "after_action",
            "capture_purpose": "web_page_ready_baseline",
            "screenshot_artifact_id": "shot-home",
            "artifact_ids": ["shot-home", "dom-home"],
            "supplemental_full_page": {
                "status": "captured",
                "capture_id": "capture-home-full",
                "screenshot_artifact_id": "shot-home-full",
                "reason_code": "WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            },
        },
        {
            "page_capture_id": "capture-unknown",
            "page_seq": 3,
            "url": "https://example.test/opaque",
            "page_type": "unknown",
            "capture_role": "after_action",
            "capture_purpose": "web_page_ready_baseline",
            "screenshot_artifact_id": "shot-unknown",
            "artifact_ids": ["shot-unknown", "dom-unknown"],
        },
    ]
    assessments = [
        {
            "page_capture_id": "capture-gate",
            "final_page_type": "age_gate",
            "classification_status": "classified",
            "coverage_group": "lifecycle",
        },
        {
            "page_capture_id": "capture-home",
            "final_page_type": "home",
            "classification_status": "classified",
            "coverage_group": "business",
        },
        {
            "page_capture_id": "capture-unknown",
            "final_page_type": "unknown",
            "classification_status": "needs_review",
            "coverage_group": "neither",
        },
    ]

    _, pages = _web_report_logical_pages(rows, assessments)

    assert [item["primary_capture_id"] for item in pages] == [
        "capture-gate",
        "capture-home",
        "capture-unknown",
    ]
    assert pages[0]["coverage_group"] == "lifecycle"
    assert pages[1]["coverage_group"] == "business"
    assert pages[2]["coverage_group"] == "neither"
    assert pages[0]["supplemental_full_page"] == rows[0]["supplemental_full_page"]
    assert pages[1]["primary_screenshot_artifact_id"] == "shot-home"
    assert pages[1]["supplemental_full_page"] == rows[1]["supplemental_full_page"]
    assert pages[2]["supplemental_full_page"] == {
        "status": "not_attempted",
        "capture_id": None,
        "screenshot_artifact_id": None,
        "reason_code": None,
    }


def test_web_v2_lifecycle_page_requires_verified_identity_when_identity_map_present() -> None:
    from services.api.app.application.report_insight import _web_report_logical_pages

    rows = [
        {
            "page_capture_id": "capture-gate",
            "page_seq": 1,
            "url": "https://untrusted.example/",
            "page_type": "age_gate",
            "capture_role": "after_action",
        }
    ]
    assessments = [
        {
            "page_capture_id": "capture-gate",
            "final_page_type": "age_gate",
            "coverage_group": "lifecycle",
        }
    ]

    primary, pages = _web_report_logical_pages(
        rows,
        assessments,
        trusted_identities={
            "capture-gate": {
                "logical_page_identity_status": "invalid",
            }
        },
    )

    assert primary == []
    assert pages == []


@pytest.mark.parametrize(
    "url, expected_role",
    [
        ("https://example.com/landing", None),
        ("https://example.com/", "home"),
        ("https://example.com/#/detail/1", None),
    ],
)
async def test_web_report_requires_url_or_trusted_redirect_for_home_role(url, expected_role) -> None:
    """没有真实重定向事实的子路径不得仅凭评估 home 声明变成首页。"""

    repository = FakeReportRepository()
    assert repository.context is not None
    repository.context.update(
        target_platform="web",
        target_url="https://example.com",
        task_config={"task_name": "Web 首页角色", "workflow_version": "v2"},
        status="review",
    )
    repository.h5_page_rows = [
        {
            "page_capture_id": "capture-home",
            "page_seq": 1,
            "page_type": "video_list",
            "url": url,
            "capture_purpose": "web_page_ready_baseline",
            "capture_role": "after_action",
            "captured_at": NOW,
            "inspection_status": "completed",
            "screenshot_artifact_id": "shot-home",
            "artifact_ids": ["shot-home", "dom-home"],
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_capture_id": "capture-home",
            "page_type": "video_list",
            "status": "covered",
            "assessment_payload": {
                "page_role": "home",
                "classification_status": "classified",
                "final_page_type": "video_list",
                "proposed_page_type": "video_list",
                "confidence": 0.95,
            },
        }
    ]

    insight = await _service(repository).report_insight("task-1")

    page = insight["web_logical_pages"][0]
    assert page["page_role"] == expected_role
    assert page["page_type"] == "video_list"


async def test_uncertainty_requires_confirmation_and_blocks_export() -> None:
    """2.1 口径主场景：计划外未覆盖 / 检测异常 / 其他三类确认完才可导出，未通过直接生效。"""
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_3", "fail"),
        _row("alioth_5", "evidence_insufficient"),
        _row("alioth_9", "manual_review", category="visual"),
        _row("alioth_10", "evidence_insufficient", category="input_risk"),
    ]
    service = _service(repository)

    insight = await service.report_insight("task-1")
    counts = {item["status"]: item["count"] for item in insight["distribution"]}
    assert counts == {
        "passed": 1,
        "failed": 1,
        "not_applicable": 0,
        "coverage_gap": 0,
        "detect_error": 0,
        "other": 3,
    }
    assert insight["confirm_progress"]["pending"] == 1
    assert insight["export_ready"] is False
    with pytest.raises(ReportExportBlockedError):
        await service.export_html("task-1")
    # 非红线转人工同样进入人工终审，可直接确认。
    result = await service.confirm_rule("task-1", "exec-alioth_9", verdict="pass")
    assert result["human_verdict"] == "pass"


async def test_export_unblocks_after_all_confirmations() -> None:
    repository = FakeReportRepository()
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_3", "fail", human_verdict="fail"),
        _row("alioth_5", "evidence_insufficient", human_verdict="ignored"),
        _row("alioth_9", "manual_review", category="visual", human_verdict="pass"),
    ]
    service = _service(repository)

    insight = await service.report_insight("task-1")
    assert insight["confirm_progress"]["pending"] == 0
    assert insight["export_ready"] is True

    filename, html_text = await service.export_html("task-1")
    assert filename == "task_task-1_report.html"
    assert "alioth_3 的问题描述" in html_text


async def test_export_renders_badge_rows_and_embedded_screenshot() -> None:
    repository = FakeReportRepository()
    png = b"\x89PNG-fake-bytes"
    repository.rows = [
        _row("alioth_2", "pass"),
        _row("alioth_7", "fail", human_verdict="fail"),
        _row(
            "alioth_9",
            "evidence_insufficient",
            human_verdict="ignored",
            human_verdict_note="无该功能",
        ),
        # 未执行/预算终止的异常残留：人工确认忽略后按对方口径不进导出件
        _row(
            "alioth_11",
            "skipped",
            execution_reason="candidate_not_executed",
            human_verdict="ignored",
        ),
        _row(
            "alioth_5",
            "manual_review",
            human_verdict="fail",
            human_verdict_note="确认存在问题",
            evidence_artifact_ids=["art-frames", "art-1"],
        ),
    ]
    repository.screenshots = [
        {
            # 连续截图的帧序列 JSON 也挂在 screenshot 类型下，导出必须跳过它
            "artifact_id": "art-frames",
            "page_capture_id": "cap-1",
            "checksum": "sha256:j",
            "content_type": "application/json",
            "size_bytes": 10,
            "storage_path": "t/c/frames.json",
            "created_at": NOW,
            "page_type": "home",
            "page_seq": 1,
            "captured_at": NOW,
            "url": "u",
        },
        {
            "artifact_id": "art-1",
            "page_capture_id": "cap-1",
            "checksum": "sha256:a",
            "content_type": "image/png",
            "size_bytes": len(png),
            "storage_path": "t/c/a.png",
            "created_at": NOW,
            "page_type": "home",
            "page_seq": 1,
            "captured_at": NOW,
            "url": "u",
        },
    ]
    repository.page_rows = [
        {
            "page_capture_id": "cap-1",
            "page_type": "home",
            "url": "https://example.com/home",
            "inspection_lifecycle_status": "active",
            "inspection_status": "degraded",
            "inspection_failure_code": "RULE_TOOL_FAILED",
            "inspection_failure_message": "局部规则工具失败",
        }
    ]
    store = FakeEvidenceStore({"t/c/a.png": png, "t/c/frames.json": b'{"sequence":[]}'})
    filename, html_text = await _service(repository, store).export_html("task-1")
    assert filename == "task_task-1_report.html"

    # 体验平台导入契约：__edit_data__ 内嵌 base64(UTF-8 JSON)，导入只读这个 div。
    marker = 'id="__edit_data__"'
    assert marker in html_text
    start = html_text.index(">", html_text.index(marker)) + 1
    end = html_text.index("</div>", start)
    state = json.loads(base64.b64decode(html_text[start:end].strip()).decode("utf-8"))

    assert state["title"] == "报告冒烟任务 质检复核报告"
    assert state["radar"] is None
    # 顶层分类必须带平台完整预置（导入会整体替换编辑器预置）。
    category_names = {category["name"] for category in state["categories"]}
    assert {"广告通用规范", "播放器规范", "内容展示规范与规避山寨感"} <= category_names

    titles = [issue["title"] for issue in state["issues"]]
    # 导出只收最终不通过：AI 直判不通过与人工二次确认不通过进清单；
    # 通过、人工忽略、未执行/未覆盖异常残留都不进。
    assert "规则alioth_7" in titles
    assert "规则alioth_5" in titles
    assert "规则alioth_2" not in titles
    assert "规则alioth_9" not in titles
    assert "规则alioth_11" not in titles
    # 平台容错要求：sec 必须显式 qc（缺省会被当成体验条目）。
    assert all(issue["sec"] == "qc" and issue["type"] == "text" for issue in state["issues"])
    # severity high → P1；category ad → 广告通用规范。
    assert {issue["priority"] for issue in state["issues"]} == {"P1"}
    assert all(issue["categories"] == ["广告通用规范"] for issue in state["issues"])

    fail_issue = next(issue for issue in state["issues"] if issue["title"] == "规则alioth_5")
    b64png = base64.b64encode(png).decode("ascii")
    assert fail_issue["images"] == [f"data:image/png;base64,{b64png}"]
    # 对方 2026-08-06 模板：规范 / 问题（AI 判定+原因+人工二次确认+所属页面）/ 优化建议
    assert "规范：" in fail_issue["content"]
    assert "AI 判定为待人工复核（原因：alioth_5 的问题描述）" in fail_issue["content"]
    assert "人工二次确认为：不通过" in fail_issue["content"]
    assert "所属页面：" in fail_issue["content"]
    assert "人工判定备注：确认存在问题" in fail_issue["content"]
    assert "优化建议：需人工填写" in fail_issue["content"]
    assert "研发回复：待研发填写" in fail_issue["content"]
    assert "研发回复</p>" in html_text
    # 2026-08-31 口径：未通过也必须人工终审后才可导出，导出件带二次确认字样。
    direct_fail = next(issue for issue in state["issues"] if issue["title"] == "规则alioth_7")
    assert "AI 判定为未通过（原因：alioth_7 的问题描述）" in direct_fail["content"]
    assert "人工二次确认为：不通过" in direct_fail["content"]
    assert "优化建议：需人工填写" in direct_fail["content"]

    # 正文为平台样式骨架，截图同时内嵌在幻灯片里。
    assert "cover-title" in html_text
    assert 'class="nav-list"' in html_text
    assert b64png in html_text
    assert "规则alioth_2" not in html_text


async def test_export_skips_oversized_screenshot() -> None:
    repository = FakeReportRepository()
    big = b"x" * 500_000
    repository.rows = [
        _row(
            "alioth_5",
            "fail",
            human_verdict="fail",
            evidence_artifact_ids=["art-big"],
        )
    ]
    repository.screenshots = [
        {
            "artifact_id": "art-big",
            "page_capture_id": "cap-1",
            "checksum": "sha256:b",
            "content_type": "image/png",
            "size_bytes": len(big),
            "storage_path": "t/c/big.png",
            "created_at": NOW,
            "page_type": "home",
            "page_seq": 1,
            "captured_at": NOW,
            "url": "u",
        }
    ]
    store = FakeEvidenceStore({"t/c/big.png": big})
    _, html_text = await _service(repository, store).export_html("task-1")
    assert "data:image/png;base64" not in html_text
    assert "art-big（超过单张 400 KB 内嵌上限）" in html_text


async def test_missing_task_raises_not_found() -> None:
    repository = FakeReportRepository()
    repository.context = None
    with pytest.raises(ReportInsightNotFoundError):
        await _service(repository).report_insight("nope")


async def test_export_keeps_multiple_images_and_existing_human_audit_with_missing_material_ids():
    repository = FakeReportRepository()
    repository.rows = [_row(
        "multi", "fail", human_verdict="fail", human_verdict_by="original-reviewer",
        human_verdict_at=NOW, evidence_artifact_ids=["a", "missing", "a", "b"],
    )]
    repository.screenshots = [
        {"artifact_id": key, "storage_path": key, "content_type": "image/png"}
        for key in ("a", "b")
    ]
    service = _service(repository, FakeEvidenceStore({"a": b"a", "b": b"b"}))
    insight = await service.report_insight("task-1")
    assert insight["rule_items"][0]["human_verdict_by"] == "original-reviewer"
    assert insight["rule_items"][0]["human_verdict_at"] == NOW
    _, html_text = await service.export_html("task-1")
    start = html_text.index('id="__edit_data__"')
    encoded = html_text[html_text.index(">", start) + 1:html_text.index("</div>", start)]
    issue = json.loads(base64.b64decode(encoded).decode())["issues"][0]
    assert issue["images"] == ["data:image/png;base64,YQ==", "data:image/png;base64,Yg=="]
    assert "确认人：original-reviewer" in issue["content"]
    assert "确认时间：2026-07-17 12:00:00+00:00" in issue["content"]
    assert "原材料标识：a、missing、b" in issue["content"]
    assert "missing（未找到该材料）" in issue["content"]
    assert "研发回复：待研发填写" in issue["content"]
