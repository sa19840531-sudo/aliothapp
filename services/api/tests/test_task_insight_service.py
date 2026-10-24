"""任务详情聚合服务的单元测试：时间线拼装、候选∪执行合并与截图去重。"""

import base64
import inspect
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application import task_insight
from services.api.app.application.h5_terminal_projection import h5_target_coverage_ready
from services.api.app.application.task_insight import (
    TaskInsightNotFoundError,
    TaskInsightService,
    _h5_assessment_scope_ready,
    _h5_model_contract_projection,
    _h5_terminal_projection,
    _legacy_h5_coverage_projection,
)
from services.api.app.application.task_progress import whole_task_progress_percent
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)

NOW = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)


async def test_legacy_provider_failure_does_not_become_passed_by_historical_human_verdict() -> None:
    repository = FakeInsightRepository()
    repository.execution_rows = [_execution(
        "legacy-provider-error", "manual_review", result_summary="模型 API 返回 HTTP 500，未生成结论",
        human_verdict="pass", human_verdict_note="保留历史人工记录",
    )]
    result = await _service(repository).rule_results("task-1")
    item = result["items"][0]
    assert item["machine_display_status"] == "detect_error"
    assert item["display_status"] == "detect_error"
    assert item["confirmable"] is False
    assert item["confirm_status"] is None
    assert item["human_verdict"] == "pass"
    assert item["human_verdict_note"] == "保留历史人工记录"


class FakeInsightRepository:
    async def read_web_capture_identities(self, **kwargs):
        from services.api.app.domain.page.web_url_identity import (
            DEFAULT_WEB_URL_IDENTITY_POLICY as policy,
        )
        return {row["page_capture_id"]: {"logical_page_id": policy.logical_id(row["url"]), "canonical_url": policy.canonical(row["url"]), "logical_page_identity_status": "verified"} for row in self.h5_captures if row.get("url")}

    def __init__(self) -> None:
        self.context: dict[str, Any] | None = {
            "task_id": "task-1",
            "status": "running",
            "target_platform": None,
            "target_url": "https://example.com",
            "channel": "workbench",
            "created_at": NOW,
            "started_at": NOW,
            "finished_at": None,
            "last_error": None,
            "task_config": {"journey_steps": ["首页", "支付"]},
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "run_id": "run-1",
            "run_status": "running",
            "stop_reason": None,
        }
        self.nodes: list[dict[str, Any]] = []
        self.execution_rows: list[dict[str, Any]] = []
        self.expected_counts: dict[str, int] = {}
        self.applicable_rules: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.page_rows: list[dict[str, Any]] = []
        self.exploration_actions: list[dict[str, Any]] = []
        self.h5_work_items: list[dict[str, Any]] = []
        self.h5_actions: list[dict[str, Any]] = []
        self.h5_captures: list[dict[str, Any]] = []
        self.h5_assessments: list[dict[str, Any]] = []
        self.h5_inventory: list[dict[str, Any]] = []
        self.h5_full_closure: dict[str, Any] = {}
        self.h5_visual_navigation_usage: dict[str, Any] = {}
        self.surface_session_summary: dict[str, Any] = {}
        self.calls: list[str] = []
        self.inspection_rows: dict[str, list[dict[str, Any]]] = {
            "work_items": [],
            "rules": [],
            "attempts": [],
        }
        self.inspection_control_mode: str | None = None
        self.app_stage_projection: dict[str, Any] = {
            "bootstrap": None,
            "exploration_work": {},
            "gate": None,
            "inspection_summary": {},
            "app_exploration": None,
        }

    async def fetch_task_context(self, task_id: str) -> dict[str, Any] | None:
        return self.context

    async def list_page_nodes(self, *, agent_run_id: str) -> list[dict[str, Any]]:
        self.calls.append("legacy_nodes")
        return self.nodes

    async def list_rule_execution_rows(
        self, *, task_id: str, agent_run_id: str | None
    ) -> list[dict[str, Any]]:
        self.calls.append("rule_executions")
        return self.execution_rows

    async def list_page_capture_inspection_rows(
        self, *, task_id: str, agent_run_id: str | None
    ) -> list[dict[str, Any]]:
        return self.page_rows

    async def list_exploration_actions(self, *, agent_run_id: str | None) -> list[dict[str, Any]]:
        self.calls.append("legacy_actions")
        return self.exploration_actions

    async def list_h5_v2_work_items(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append("h5_work")
        return self.h5_work_items

    async def list_h5_action_executions(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append("h5_actions")
        return self.h5_actions

    async def list_h5_page_captures(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append("h5_captures")
        return self.h5_captures

    async def list_h5_page_type_assessments(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append("h5_assessments")
        return self.h5_assessments

    async def list_h5_evidence_inventory(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append("h5_inventory")
        return self.h5_inventory

    async def fetch_h5_full_inspection_closure(
        self, *, task_id: str, agent_run_id: str
    ) -> dict[str, Any]:
        self.calls.append("h5_full_closure")
        return self.h5_full_closure

    async def fetch_h5_visual_navigation_usage(
        self, *, task_id: str, agent_run_id: str
    ) -> dict[str, Any]:
        del task_id, agent_run_id
        self.calls.append("h5_visual_navigation_usage")
        return self.h5_visual_navigation_usage

    async def fetch_surface_session_summary(
        self, *, task_id: str, agent_run_id: str
    ) -> dict[str, Any]:
        del task_id, agent_run_id
        self.calls.append("surface_session_summary")
        return self.surface_session_summary

    async def list_inspection_work_item_rows(
        self,
        *,
        task_id: str,
        control_mode: str | None,
    ) -> dict[str, list[dict[str, Any]]]:
        self.inspection_control_mode = control_mode
        return self.inspection_rows

    async def count_rules_by_page_type(self, *, task_id: str) -> dict[str, int]:
        return self.expected_counts

    async def list_applicable_rules(
        self, *, task_id: str, page_types: list[str] | None
    ) -> list[dict[str, Any]]:
        if page_types is None:
            return self.applicable_rules
        return [
            rule
            for rule in self.applicable_rules
            if set(rule["applicable_page_types"]) & set(page_types)
        ]

    async def list_logs(
        self, *, task_id: str, level: str | None, limit: int | None, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        items = [log for log in self.logs if level is None or log["level"] == level]
        sliced = items[offset : offset + limit] if limit is not None else items[offset:]
        return sliced, len(items)

    async def list_screenshot_artifacts(
        self, *, task_id: str, agent_run_id: str | None
    ) -> list[dict[str, Any]]:
        return self.screenshots

    async def fetch_evidence_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self.artifacts.get(artifact_id)

    async def list_task_page_types(self, *, task_id: str) -> list[str]:
        return sorted({node["page_type"] for node in self.nodes if node.get("page_type")})

    async def fetch_app_stage(self, task_id: str) -> dict[str, Any]:
        return self.app_stage_projection


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.payloads: dict[str, bytes] = {}

    async def read(self, storage_path: str) -> bytes:
        self.reads.append(storage_path)
        if storage_path in self.payloads:
            return self.payloads[storage_path]
        return b"png-bytes:" + storage_path.encode()


def _node(seq: int, **overrides: Any) -> dict[str, Any]:
    node = {
        "node_id": f"node-{seq}",
        "node_seq": seq,
        "source": "journey",
        "step_name": f"步骤{seq}",
        "page_type": None,
        "status": "pending",
        "page_capture_id": None,
        "candidate_rule_total": None,
        "current_rule_code": None,
        "first_visited_at": None,
        "finished_at": None,
        "url": None,
        "captured_at": None,
        "page_state": None,
        "detection_status": None,
        "detection_confidence": None,
        "detection_duration_ms": None,
        "element_count": None,
        "parent_page_capture_id": None,
        "inspection_lifecycle_status": None,
        "inspection_status": None,
        "inspection_failure_stage": None,
        "inspection_failure_code": None,
        "inspection_failure_message": None,
        "inspection_failure_retryable": None,
        "inspection_abandon_reason": None,
        "screenshot_artifact_id": None,
    }
    node.update(overrides)
    return node


def _execution(rule_code: str, status: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "execution_id": f"exec-{rule_code}-{status}",
        "rule_id": f"rule-{rule_code}",
        "rule_code": rule_code,
        "rule_name": f"规则{rule_code}",
        "category": "ad",
        "rule_severity": "high",
        "rule_package": None,
        "trigger_display": None,
        "applicable_page_types": ["home"],
        "sort_order": 1,
        "execution_status": status,
        "judge_strategy": "hybrid",
        "confidence": 0.9,
        "severity": "high",
        "result_summary": None,
        "problem_desc": None,
        "review_status": None,
        "error_message": None,
        "evidence_artifact_ids": [],
        "page_capture_id": "cap-1",
        "started_at": NOW,
        "finished_at": NOW,
        "capture_page_type": "home",
        "is_bootstrap": False,
        "execution_reason": None,
        "human_verdict": None,
        "human_verdict_note": None,
        "human_verdict_at": None,
    }
    row.update(overrides)
    return row


def _rule(rule_code: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "rule_id": f"rule-{rule_code}",
        "rule_code": rule_code,
        "rule_name": f"规则{rule_code}",
        "category": "ad",
        "severity": "high",
        "executor_type": "hybrid",
        "page_type": "home",
        "rule_package": None,
        "trigger_display": None,
        "applicable_page_types": ["home"],
        "check_point": "检查点",
        "sort_order": 1,
    }
    row.update(overrides)
    return row


def _service(repository: FakeInsightRepository) -> TaskInsightService:
    return TaskInsightService(repository=repository, evidence_store_factory=FakeEvidenceStore)


async def test_app_stage_projects_exploration_closure_without_probe() -> None:
    repository = FakeInsightRepository()
    repository.context["target_url"] = None
    repository.context["task_config"] = {
        "workflow_version": "v2",
        "execution_mode": "exploration_only",
        "_task_target": {"platform": "app"},
    }
    repository.app_stage_projection = {
        "bootstrap": {"stage": "app_ready", "ready_at": NOW, "updated_at": NOW},
        "exploration_work": {"succeeded": 3},
        "gate": None,
        "inspection_summary": {"work_item_count": 2},
        "app_exploration": {
            "surface_phase": "popup_chain",
            "surface_phase_status": "completed",
            "current_page_identity": "page:home",
            "completed_work_count": 3,
            "pending_work_count": 0,
            "action_count": 2,
            "reobservation_count": 1,
            "coverage_summary": {"unique_page_count": 2},
            "terminal_reason_code": "exploration_completed",
            "outcome": "exploration_completed",
            "evidence_index_id": "index-1",
            "evidence_index_hash": "sha256:abc",
            "evidence_index_status": "sealed",
            "evidence_gap_count": 0,
            "surface_session_status": "closed",
            "final_observation_count": 1,
            "started_at": NOW,
            "completed_at": NOW,
        },
    }

    result = await _service(repository).app_stage("task-1")

    assert "probe_work" not in result
    assert result["execution_mode"] == "exploration_only"
    exploration = result["app_exploration"]
    assert exploration["status"] == "exploration_completed"
    assert exploration["started_at"] == NOW.isoformat()
    assert exploration["completed_at"] == NOW.isoformat()
    assert exploration["evidence_index_hash"] == "sha256:abc"


@pytest.mark.parametrize(
    ("outcome", "session_status", "expected_status"),
    [
        ("evidence_insufficient", "closed", "evidence_insufficient"),
        ("exploration_completed", "lost", "close_failed"),
        (None, "lost", "technical_failed"),
    ],
)
async def test_app_stage_keeps_business_and_technical_outcomes_distinct(
    outcome: str | None,
    session_status: str,
    expected_status: str,
) -> None:
    repository = FakeInsightRepository()
    repository.context["target_url"] = None
    repository.context["task_config"] = {"_task_target": {"platform": "app"}}
    repository.app_stage_projection["app_exploration"] = {
        "outcome": outcome,
        "surface_session_status": session_status,
        "evidence_index_status": "sealed" if outcome else None,
        "completed_work_count": 1,
        "pending_work_count": 0,
    }

    result = await _service(repository).app_stage("task-1")

    assert result["app_exploration"]["status"] == expected_status


async def test_app_stage_marks_failure_fact_without_session_as_technical_failed() -> None:
    repository = FakeInsightRepository()
    repository.context["target_url"] = None
    repository.context["task_config"] = {"_task_target": {"platform": "app"}}
    repository.app_stage_projection["app_exploration"] = {
        "outcome": None,
        "surface_session_status": None,
        "technical_failure": {
            "phase": "runtime",
            "reason_code": "VISION_PROVIDER_UNAVAILABLE",
            "recovery_state": "pending",
        },
        "completed_work_count": 0,
        "pending_work_count": 0,
    }

    result = await _service(repository).app_stage("task-1")

    assert result["app_exploration"]["status"] == "technical_failed"
    assert result["app_exploration"]["technical_failure"]["phase"] == "runtime"


async def test_app_stage_keeps_legacy_app_exploration_null() -> None:
    repository = FakeInsightRepository()
    repository.context["target_url"] = None
    repository.context["task_config"] = {"_task_target": {"platform": "app"}}

    result = await _service(repository).app_stage("task-1")

    assert result["app_exploration"] is None


async def test_inspection_work_items_projects_protocol_state_without_judge_payload() -> None:
    repository = FakeInsightRepository()
    repository.inspection_rows = {
        "work_items": [
            {
                "work_item_id": "work-1",
                "control_mode": "enforced",
                "status": "failed",
                "session_dependency": "active_session",
                "last_error_code": "INSPECTION_ACTIVE_SESSION_LOST",
            },
            {
                "work_item_id": "work-2",
                "control_mode": "shadow",
                "status": "pending",
                "session_dependency": "snapshot",
                "last_error_code": None,
            },
        ],
        "rules": [
            {
                "work_item_id": "work-1",
                "rule_code": "alioth_1",
                "status": "error",
            }
        ],
        "attempts": [
            {
                "work_item_id": "work-1",
                "attempt_id": "attempt-1",
                "status": "abandoned",
                "has_result_payload": True,
            }
        ],
    }

    result = await _service(repository).inspection_work_items(
        "task-1",
        control_mode=None,
    )

    assert result["summary"] == {
        "work_item_count": 2,
        "enforced_count": 1,
        "shadow_count": 1,
        "nonterminal_count": 0,
        "failed_count": 1,
        "active_session_failed_count": 1,
        "status_counts": {"failed": 1, "pending": 1},
        "rule_status_counts": {"error": 1},
        "attempt_status_counts": {"abandoned": 1},
    }
    assert result["items"][0]["rules"][0]["rule_code"] == "alioth_1"
    assert result["items"][0]["attempts"][0]["has_result_payload"] is True
    assert "result_payload" not in result["items"][0]["attempts"][0]


async def test_platform_progress_export_reports_capability_progress_as_excel() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "done"
    repository.context["run_status"] = "done"
    repository.context["stop_reason"] = "max_steps_reached"
    repository.context["finished_at"] = NOW
    repository.context["task_config"]["task_name"] = "平台能力回归"
    repository.nodes = [
        _node(
            1,
            page_type="home",
            status="recognized",
            page_capture_id="cap-1",
            candidate_rule_total=3,
            inspection_lifecycle_status="active",
            inspection_status="completed",
            captured_at=NOW,
            url="https://example.com/home",
        )
    ]
    repository.page_rows = [
        {
            "page_capture_id": "cap-1",
            "parent_page_capture_id": None,
            "page_seq": 1,
            "page_type": "home",
            "url": "https://example.com/home",
            "captured_at": NOW,
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
            "inspection_failure_stage": None,
            "inspection_failure_code": None,
            "inspection_failure_message": None,
            "inspection_failure_retryable": None,
            "inspection_abandon_reason": None,
        }
    ]
    repository.applicable_rules = [
        _rule("alioth_1"),
        _rule("alioth_2", sort_order=2),
        _rule("alioth_3", sort_order=3),
        _rule(
            "alioth_4",
            sort_order=4,
            page_type="payment",
            applicable_page_types=["payment"],
        ),
        _rule("alioth_5", sort_order=5),
    ]
    repository.execution_rows = [
        _execution("alioth_1", "pass", evidence_artifact_ids=["evidence-1"]),
        _execution(
            "alioth_2",
            "evidence_insufficient",
            problem_desc="缺少弹窗关闭后的状态证据",
        ),
        _execution(
            "alioth_5",
            "skipped",
            execution_reason="run_budget_exhausted",
            capture_failure_code="MAX_STEPS_REACHED",
        ),
    ]

    filename, content = await _service(repository).platform_progress_export("task-1")

    assert filename.startswith("alioth_platform_progress_task-1_")
    assert filename.endswith(".xlsx")
    assert content.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        assert workbook.testzip() is None
        workbook_xml = workbook.read("xl/workbook.xml").decode()
        assert 'name="能力总览"' in workbook_xml
        assert 'name="规则进展"' in workbook_xml
        assert 'name="页面进展"' in workbook_xml
        assert 'name="待补强项"' in workbook_xml
        summary_xml = workbook.read("xl/worksheets/sheet1.xml").decode()
        rules_xml = workbook.read("xl/worksheets/sheet2.xml").decode()
        assert "不计算、也不代表受检产品通过率" in summary_xml
        assert "本次已应用规则（2 条）" in summary_xml
        assert "alioth_1" in summary_xml
        assert "规则alioth_1" in summary_xml
        assert "alioth_2" in summary_xml
        assert "规则alioth_2" in summary_xml
        assert "本次未触发规则（3 条）" in summary_xml
        assert "alioth_3" in summary_xml
        assert "alioth_4" in summary_xml
        assert "alioth_5" in summary_xml
        assert "未触发原因" in summary_xml
        assert "已访问适用页面（首页），但未记录该规则命中候选或进入执行链" in summary_xml
        assert "未发现或未访问适用页面：支付" in summary_xml
        assert "已匹配候选，但未进入规则判定" in summary_xml
        assert "运行步数达到上限（MAX_STEPS_REACHED）" in summary_xml
        assert "执行触发状态" in rules_xml
        assert "已完成验证" in rules_xml
        assert "已触达待补强" in rules_xml
        assert "缺少弹窗关闭后的状态证据" in rules_xml
        assert "alioth_3" in rules_xml


async def test_timeline_uses_journey_fallback_when_no_nodes() -> None:
    repository = FakeInsightRepository()
    repository.expected_counts = {"home": 8, "payment": 14}
    service = _service(repository)
    timeline = await service.page_timeline("task-1")
    assert [node["step_name"] for node in timeline["nodes"]] == ["首页", "支付"]
    assert timeline["nodes"][0]["page_type"] == "home"
    assert timeline["nodes"][0]["rule_progress"]["total"] == 8
    assert timeline["nodes"][1]["rule_progress"]["total"] == 14
    # 运行中且无访问记录时，第一个待执行节点为当前节点。
    assert timeline["current_node_seq"] == 1
    assert timeline["nodes"][0]["status"] == "running"


async def test_app_exploration_only_timeline_does_not_read_formal_rule_results() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "app",
        "status": "done",
        "task_config": {
            "workflow_version": "v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "app"},
        },
    }
    repository.nodes = [
        _node(
            1,
            source="discovered",
            page_type="home",
            status="recognized",
            page_capture_id="app-capture-1",
        )
    ]
    repository.execution_rows = [_execution("alioth_1", "pass")]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["execution_mode"] == "exploration_only"
    assert timeline["nodes"][0]["rule_progress"] == {"executed": 0, "total": None}
    assert "rule_executions" not in repository.calls


async def test_h5_timeline_enriches_logical_surface_report_from_assessment() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
    }
    identity = "sha256:" + "1" * 64
    state = "sha256:" + "2" * 64
    repository.h5_captures = [
        {
            "page_capture_id": "capture-logical",
            "page_seq": 1,
            "page_type": "profile",
            "page_state": "stable",
            "url": "https://example.test/app",
            "captured_at": NOW,
            "detection_status": "recognized",
            "detection_confidence": 0.92,
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
            "artifact_ids": ["artifact-logical"],
            "capture_role": "after_action",
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-logical",
            "page_capture_id": "capture-logical",
            "page_type": "profile",
            "status": "covered",
            "assessment_payload": {
                "page_capture_ids": ["capture-logical"],
                "evidence_artifact_ids": ["artifact-logical"],
                "detection_result": {
                    "confidence_source": "deterministic_structure",
                    "logical_surface": {
                        "protocol_version": "logical_surface.v1",
                        "render_profile": {
                            "kind": "flutter_web",
                            "status": "confirmed",
                            "confidence": 0.96,
                        },
                        "document_identity": "sha256:" + "9" * 64,
                        "identity_fingerprint": identity,
                        "state_fingerprint": state,
                        "evidence_sources": ["flutter_semantics", "document"],
                        "evidence_quality": "high",
                        "degraded_reasons": [],
                        "shadow": True,
                        "page_type_binding": {
                            "page_type": "profile",
                            "page_type_confidence": 0.92,
                            "confidence_source": "deterministic_structure",
                            "coverage_allowed": True,
                            "rule_matching_allowed": True,
                        },
                    },
                },
            },
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["nodes"][0]["capture_role"] == "after_action"
    logical = timeline["exploration_summary"]["logical_surface"]
    assert logical["unique_logical_page_count"] == 1
    assert logical["render_profile_counts"] == {"flutter_web": 1}
    assert logical["logical_pages"][0]["page_type"] == "profile"
    assert logical["logical_pages"][0]["confidence_sources"] == [
        "deterministic_structure"
    ]


async def test_app_exploration_only_rule_results_are_empty() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "app",
        "task_config": {
            "workflow_version": "v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "app"},
        },
    }
    repository.execution_rows = [_execution("alioth_1", "pass")]

    result = await _service(repository).rule_results("task-1")

    assert result["total"] == 0
    assert result["items"] == []
    assert "rule_executions" not in repository.calls


async def test_h5_v1_timeline_keeps_legacy_nodes_and_working_memory_path() -> None:
    """正式 H5 平台的 V1 任务不得被 V2 读模型吞掉旧事实。"""

    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "task_config": {
            "workflow_version": "v1",
            "journey_steps": ["首页"],
        },
    }
    repository.nodes = [
        {
            "node_seq": 1,
            "step_name": "首页",
            "page_type": "home",
            "status": "completed",
            "page_capture_id": "legacy-capture-1",
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["nodes"][0]["page_capture_id"] == "legacy-capture-1"
    assert "legacy_nodes" in repository.calls
    assert "legacy_actions" in repository.calls
    assert "h5_work" not in repository.calls


async def test_h5_timeline_reads_v2_facts_without_legacy_nodes_or_working_memory() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "task_id": "task-h5",
        "target_platform": "h5",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
        "working_memory": {
            "page_type_assessments": [{"page_type": "payment", "status": "covered"}]
        },
    }
    repository.h5_work_items = [
        {
            "work_id": "work-1",
            "status": "succeeded",
            "branch_id": "h5:capture-v2",
            "source_capture_id": "capture-before",
            "frontier": {
                "snapshot_id": "frontier-1",
                "pending_count": 0,
                "blocked_count": 0,
                "failed_count": 0,
                "covered_page_count": 1,
                "covered_page_types": ["home"],
                "sampling_policy_summary": {"include_page_types": ["home"]},
                "entries": [],
            },
            "browser_recovery": {
                "payload_hash": "recovery-2",
                "failed_work_id": "work-failed",
                "recovery_attempt_count": 2,
                "source_session_id": "h5:run-1",
                "target_session_id": "h5:run-1:recovery:0123456789abcdefabcd",
                "equivalent": True,
                "reason_code": None,
                "rebaseline_after_capture_id": "capture-v2",
            },
            "browser_recovery_attempt_history": [
                {
                    "payload_hash": "attempt-1",
                    "failed_work_id": "work-failed",
                    "failure_stage": "action",
                    "recovery_attempt_count": 1,
                    "status": "completed",
                    "error_code": "PAGE_CAPTURE_SESSION_CRASHED",
                    "reason_code": "reopen_failed",
                    "started_at": "2026-07-17T09:59:58+00:00",
                }
            ],
            "browser_recovery_parent_attempt_audit": [
                {
                    "payload_hash": "attempt-1",
                    "failed_work_id": "work-failed",
                    "failure_stage": "action",
                    "recovery_attempt_count": 1,
                    "status": "completed",
                    "error_code": "PAGE_CAPTURE_SESSION_CRASHED",
                    "reason_code": "reopen_failed",
                    "started_at": "2026-07-17T09:59:58+00:00",
                },
                {
                    "payload_hash": "attempt-2",
                    "failed_work_id": "work-failed",
                    "failure_stage": "action",
                    "recovery_attempt_count": 2,
                    "status": "completed",
                    "error_code": "PAGE_CAPTURE_SESSION_CRASHED",
                    "reason_code": None,
                    "started_at": "2026-07-17T09:59:59+00:00",
                },
            ],
        }
    ]
    repository.h5_actions = [
        {
            "action_execution_id": "action-1",
            "action": "navigate",
            "status": "explored",
            "execution_status": "completed",
            "source_page_capture_id": "capture-before",
            "result_page_capture_id": "capture-v2",
            "parameters": {
                "explorer_decision_fallback": {
                    "schema_version": "h5-explorer-decision-fallback.v1",
                    "reason_code": "H5_EXPLORER_OUTPUT_INVALID",
                }
            },
            "created_at": NOW,
        }
    ]
    repository.h5_captures = [
        {
            "page_capture_id": "capture-v2",
            "parent_page_capture_id": "capture-before",
            "page_seq": 1,
            "page_type": "home",
            "page_state": "stable",
            "url": "https://example.com/home",
            "captured_at": NOW,
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
            "artifact_ids": ["artifact-1"],
            "capture_role": "after_action",
            "navigation_retry": {
                "schema_version": "navigation_retry.v1",
                "max_attempts": 2,
                "attempt_count": 2,
                "retry_count": 1,
                "retry_reasons": ["err_empty_response"],
                "outcome": "succeeded_after_retry",
            },
            "page_settle_diagnostics": {
                "outcome": "blocked",
                "blocking_reasons": ["blocking_loader_visible"],
                "dynamic_reasons": ["dom_continues_changing"],
                "sample_count": 9,
                "stable_sample_count": 0,
            },
        },
        {
            "page_capture_id": "capture-before-audit",
            "page_seq": 2,
            "page_type": None,
            "page_state": "loaded",
            "url": "https://example.com/home",
            "captured_at": NOW,
            "inspection_lifecycle_status": "active",
            "inspection_status": "captured",
            "artifact_ids": ["artifact-before"],
            "capture_role": "before_action",
        },
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-1",
            "page_capture_id": "capture-v2",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {
                "catalog_sha256": "catalog-1",
                "page_capture_ids": ["capture-v2"],
                "evidence_artifact_ids": ["artifact-1"],
            },
        }
    ]
    repository.h5_inventory = [
        {
            "inventory_item_id": "inventory-1",
            "action_execution_id": "action-1",
            "status": "pass",
            "after_capture_id": "capture-v2",
        }
    ]
    repository.surface_session_summary = {
        "total_count": 1,
        "terminal_count": 1,
        "nonterminal_count": 0,
        "reference_count": 1,
        "nonterminal_reference_count": 0,
        "status_counts": {"closed": 1},
        "items": [{"session_id": "web:run-1", "status": "closed"}],
    }

    timeline = await _service(repository).page_timeline("task-h5")

    assert timeline["nodes"][0]["page_capture_id"] == "capture-v2"
    assert timeline["nodes"][0]["navigation_retry"]["retry_count"] == 1
    assert timeline["nodes"][0]["page_settle_diagnostics"]["outcome"] == "blocked"
    assert len(timeline["nodes"]) == 2
    assert timeline["exploration_summary"]["page_total"] == 1
    assert timeline["exploration_summary"]["evidence_page_results"] == [
        {
            "page_capture_id": "capture-before-audit",
            "parent_page_capture_id": None,
            "supersedes_page_capture_id": None,
            "page_seq": 2,
            "page_type": None,
            "page_url": "https://example.com/home",
            "page_title": None,
            "captured_at": NOW,
            "detection_status": None,
            "lifecycle_status": "active",
            "inspection_status": "captured",
            "failure_stage": None,
            "failure_code": None,
            "failure_message": None,
            "failure_retryable": None,
            "abandon_reason": None,
            "page_state": "loaded",
            "artifact_ids": ["artifact-before"],
        }
    ]
    assert timeline["exploration_actions"][0]["action"] == "navigate"
    assert timeline["h5_evidence_inventory"]["insufficient_count"] == 0
    assert timeline["h5_evidence_preflight"]["status"] == "ready"
    assert timeline["h5_evidence_preflight"]["publication_allowed"] is True
    assert timeline["h5_assessments"][0]["status"] == "covered"
    assert timeline["frontier"]["snapshot_id"] == "frontier-1"
    assert timeline["browser_recovery"]["status"] == "recovered"
    assert timeline["browser_recovery"]["attempt_count"] == 2
    assert timeline["browser_recovery"]["failed_attempt_count"] == 1
    assert timeline["browser_recovery"]["successful_attempt_count"] == 1
    assert timeline["browser_recovery"]["equivalent_recovery_count"] == 1
    assert timeline["h5_model_contract"]["status"] == "degraded"
    assert timeline["h5_model_contract"]["fallback_action_count"] == 1
    assert timeline["h5_model_contract"]["reason_codes"] == [
        "H5_EXPLORER_OUTPUT_INVALID"
    ]
    assert timeline["surface_session_summary"]["nonterminal_count"] == 0
    assert "surface_session_summary" in repository.calls
    assert "legacy_nodes" not in repository.calls
    assert "legacy_actions" not in repository.calls
    assert "rule_executions" not in repository.calls


def test_h5_model_contract_projection_preserves_unresolved_work_error() -> None:
    projection = _h5_model_contract_projection(
        work_items=[
            {
                "work_id": "work-1",
                "status": "retry_wait",
                "last_error_code": "H5_EXPLORER_OUTPUT_INVALID",
            }
        ],
        actions=[],
    )

    assert projection["status"] == "retrying"
    assert projection["fallback_action_count"] == 0
    assert projection["error_work_count"] == 1
    assert projection["reason_codes"] == ["H5_EXPLORER_OUTPUT_INVALID"]
    assert projection["error_work_items"] == [
        {
            "work_id": "work-1",
            "status": "retry_wait",
            "error_code": "H5_EXPLORER_OUTPUT_INVALID",
        }
    ]


async def test_web_v2_timeline_aggregates_capture_versions_by_logical_url() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "web",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "full_inspection"},
    }
    repository.h5_captures = [
        {
            "page_capture_id": "capture-ready",
            "page_seq": 1,
            "page_type": "content_list",
            "page_state": "stable",
            "url": "https://EXAMPLE.com/news?id=42&utm_source=feed",
            "captured_at": NOW,
            "capture_purpose": "web_page_ready_baseline",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
        },
        {
            "page_capture_id": "capture-content",
            "page_seq": 2,
            "page_type": "content_list",
            "page_state": "stable",
            "url": "https://example.com/news?id=42#content",
            "captured_at": NOW,
            "capture_purpose": "web_primary_content_evidence",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
        },
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-content",
            "page_capture_id": "capture-content",
            "page_type": "content_list",
            "page_state": "stable",
            "status": "covered",
            "reason_codes": [],
            "assessment_payload": {
                "classification_status": "classified",
                "final_page_type": "content_list",
                "raw_page_type": "Content_List",
                "proposed_page_type": "content_list",
                "observed_page_type": "content_list",
                "expected_page_type": "video_list",
                "conflict_reason_code": "URL_HINT_DOM_CONFLICT",
                "diagnostic_message": "目录提示与 DOM 列表主体冲突",
                "confidence": 0.94,
            },
        }
    ]
    repository.execution_rows = [
        _execution("web_1", "pass", page_capture_id="capture-ready"),
        _execution("web_2", "fail", page_capture_id="capture-content"),
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert len(timeline["nodes"]) == 1
    node = timeline["nodes"][0]
    assert node["canonical_url"] == "https://example.com/news"
    assert node["capture_count"] == 2
    assert node["capture_ids"] == ["capture-ready", "capture-content"]
    assert node["primary_capture_id"] == "capture-content"
    assert node["page_capture_id"] == "capture-content"
    assert node["raw_page_type"] == "Content_List"
    assert node["proposed_page_type"] == "content_list"
    assert node["observed_page_type"] == "content_list"
    assert node["expected_page_type"] == "video_list"
    assert node["conflict_reason_code"] == "URL_HINT_DOM_CONFLICT"
    assert node["diagnostic_message"] == "目录提示与 DOM 列表主体冲突"
    assert node["classification_confidence"] == pytest.approx(0.94)
    assert node["classification_review_reason"] == "URL_HINT_DOM_CONFLICT"
    assert node["rule_progress"]["executed"] == 2
    assert node["result_counts"] == {"passed": 1, "failed": 1}


@pytest.mark.parametrize(
    "url, expected_role",
    [
        ("https://example.com/landing", None),
        ("https://example.com/", "home"),
        ("https://example.com/#/detail/1", None),
    ],
)
async def test_web_v2_timeline_requires_trusted_home_role(url, expected_role) -> None:
    """缺少自然重定向的 /landing 与 SPA 详情不可仅靠 home 声明提升角色。"""

    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "web",
        "status": "review",
        "task_config": {"workflow_version": "v2", "execution_mode": "full_inspection"},
    }
    repository.h5_captures = [
        {
            "page_capture_id": "capture-home",
            "page_seq": 1,
            "page_type": "video_list",
            "page_state": "stable",
            "url": url,
            "captured_at": NOW,
            "capture_purpose": "web_page_ready_baseline",
            "detection_status": "recognized",
            "inspection_lifecycle_status": "active",
            "inspection_status": "completed",
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
            },
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["nodes"][0]["page_role"] == expected_role
    assert timeline["nodes"][0]["page_type"] == "video_list"


async def test_h5_timeline_projects_safety_interstitial_from_assessment() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "status": "review",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
    }
    repository.h5_captures = [
        {
            "page_capture_id": "capture-safety",
            "page_seq": 1,
            "page_type": None,
            "page_state": "loaded",
            "url": "https://rd.mhhyj29j.top/",
            "captured_at": NOW,
            "detection_status": None,
            "inspection_lifecycle_status": "active",
            "inspection_status": "captured",
            "artifact_ids": ["artifact-dom", "artifact-text", "artifact-shot"],
            "capture_role": "entry",
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-safety",
            "page_capture_id": "capture-safety",
            "detection_status": "ambiguous",
            "page_type": "unknown",
            "page_state": "safety_interstitial",
            "status": "evidence_insufficient",
            "reason_codes": ["PAGE_TYPE_ASSESSMENT_BLOCKED_BY_SAFETY"],
            "assessment_payload": {
                "detection_result": {
                    "detection_status": "ambiguous",
                    "page_type": None,
                    "page_state": "safety_interstitial",
                    "risk_flags": [
                        "safety_interstitial",
                        "automation_blocked",
                        "deterministic_safety_interstitial",
                    ],
                    "conflicts": ["target_blocked_by_safety_interstitial"],
                    "reason": "页面命中安全/反爬中间页特征。",
                    "rule_matching_allowed": False,
                }
            },
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    page_result = timeline["exploration_summary"]["page_results"][0]
    assert page_result["page_state"] == "safety_interstitial"
    assert page_result["detection_status"] == "ambiguous"
    assert page_result["target_access_status"] == "blocked_by_safety_interstitial"
    assert page_result["risk_flags"] == [
        "safety_interstitial",
        "automation_blocked",
        "deterministic_safety_interstitial",
    ]


async def test_h5_terminal_projection_requires_global_frontier_and_inventory_closure() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "task_id": "task-h5-needs-review",
        "target_platform": "h5",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
    }
    repository.h5_work_items = [
        {
            "work_id": "work-pending",
            "status": "pending",
            "frontier": {"snapshot_id": "frontier-2", "pending_count": 1},
        }
    ]
    repository.h5_inventory = [{"inventory_item_id": "inventory-1", "status": "insufficient"}]

    timeline = await _service(repository).page_timeline("task-h5-needs-review")

    assert timeline["exploration_summary"]["complete"] is False
    assert timeline["exploration_summary"]["quality_status"] == "in_progress"
    assert timeline["terminal_reason"] == "exploration_incomplete"


async def test_h5_full_inspection_terminal_waits_for_inspection_and_completion_gate() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "full_inspection"},
    }
    repository.h5_work_items = [
        {
            "work_id": "work-1",
            "status": "succeeded",
            "frontier": {
                "snapshot_id": "frontier-1",
                "pending_count": 0,
                "covered_page_types": ["home"],
                "sampling_policy_summary": {"include_page_types": ["home"]},
            },
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {"catalog_sha256": "catalog-1"},
        }
    ]
    repository.h5_inventory = []
    repository.h5_full_closure = {
        "nonterminal_inspection_count": 1,
        "failed_inspection_count": 0,
        "gate_passed": False,
        "gate_block_reasons": ["inspection_pending"],
    }

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["exploration_summary"]["complete"] is False
    assert timeline["terminal_reason"] == "inspection_pending"
    assert "h5_full_closure" in repository.calls
    assert "rule_executions" in repository.calls


def test_h5_full_inspection_projection_crosses_publication_delivery_and_consumer_runs() -> None:
    source = inspect.getsource(PostgresTaskInsightRepository.fetch_h5_full_inspection_closure)

    assert "publication.finalization_epoch = task.finalization_epoch" in source
    assert "publication.source_agent_run_id = $2::uuid" in source
    assert "qc_outbox_event" in source
    assert "qc_event_inbox" in source
    assert "qc_h5_inspection_publication_work_item" in source
    assert "qc_inspection_attempt" in source
    assert "work.consumer_agent_run_id = $2::uuid" not in source


def test_h5_and_app_task_context_prefer_finalization_owner_over_newer_consumer_run() -> None:
    source = inspect.getsource(PostgresTaskInsightRepository.fetch_task_context)

    assert "task.target_platform IN ('h5', 'app')" in source
    assert "id = task.finalization_agent_run_id" in source
    assert "WHEN agent_type = 'main' THEN 1" in source


def test_h5_insight_sql_projects_visual_navigation_leaf_fields_only() -> None:
    """恢复 whole-object JSONB 投影会让视觉签名、坐标和来源标识进入应用层。"""
    capture_source = inspect.getsource(PostgresTaskInsightRepository.list_h5_page_captures)
    action_source = inspect.getsource(PostgresTaskInsightRepository.list_h5_action_executions)

    assert "visual_navigation_audit,protocol_version" in capture_source
    assert "visual_navigation_audit,target_kind_counts" in capture_source
    assert "visual_navigation_audit,policy_rejection_counts" in capture_source
    assert "visual_navigation_audit,navigation_knowledge,match_type_counts" in capture_source
    assert "visual_navigation_audit,model_usage,total_tokens" in capture_source
    assert "#> '{page_detection,visual_navigation_audit}'" not in capture_source
    assert "h5_visual_coordinate_mode" in inspect.getsource(
        PostgresTaskInsightRepository.list_h5_v2_work_items
    )

    assert "visual_navigation_transition,protocol_version" in action_source
    assert "visual_navigation_transition,reason_codes" in action_source
    assert "state_diff,visual_action_committed" in action_source
    assert "#> '{parameters,visual_navigation_capability}'" not in action_source
    assert "action.intent_payload->'parameters' AS parameters" not in action_source
    assert "action.result_payload," not in action_source
    assert "THEN NULL" in action_source
    assert "visual_navigation_coordinate" in action_source
    assert "action.intent_payload->>'semantic_target'" in action_source
    assert "AS is_visual_navigation_provenance" in action_source
    assert "visual_navigation_capability" in action_source
    assert "visual_navigation_provenance" in action_source
    assert "visual_target_kind" in action_source
    assert "? 'visual_navigation_capability'" in action_source


def test_visual_navigation_usage_sql_reads_safe_numeric_ledger_columns_only() -> None:
    """视觉模型用量必须按固定 operation 从计量表取数，不能读取供应商原始载荷。"""
    source = inspect.getsource(
        PostgresTaskInsightRepository.fetch_h5_visual_navigation_usage
    )

    assert "qc_llm_usage_event" in source
    assert "h5_visual_navigation_detection" in source
    for safe_column in (
        "ai_calls",
        "input_tokens",
        "output_tokens",
        "cache_hit_tokens",
        "total_tokens",
    ):
        assert safe_column in source
    assert "vendor_usage_payload" not in source
    assert "calculation" not in source
    assert "metadata" not in source


async def test_h5_timeline_attributes_visual_mode_and_safe_model_usage() -> None:
    repository = FakeInsightRepository()
    assert repository.context is not None
    repository.context.update(
        {
            "target_platform": "h5",
            "status": "done",
            "task_config": {
                "workflow_version": "v2",
                "execution_mode": "exploration_only",
            },
        }
    )
    repository.h5_work_items = [
        {
            "work_id": "work-visual",
            "status": "succeeded",
            "h5_visual_coordinate_mode": "enforced",
            "frontier": {
                "snapshot_id": "frontier-visual",
                "pending_count": 0,
                "covered_page_types": ["home"],
            },
        }
    ]
    repository.h5_captures = [
        {
            "page_capture_id": "capture-visual",
            "page_seq": 1,
            "page_type": "home",
            "detection_status": "recognized",
            "capture_role": "after_action",
            "visual_navigation_audit": {
                "protocol_version": "visual_navigation_audit.v1",
                "detector_status": "recognized",
                "prediction_count": 1,
                "target_kind_counts": {"primary_navigation": 1},
                "model_usage": {"request_count": 2, "elapsed_ms": 42},
            },
        }
    ]
    repository.h5_visual_navigation_usage = {
        "request_count": 3,
        "input_tokens": 90,
        "output_tokens": 10,
        "cache_hit_tokens": 5,
        "total_tokens": 100,
    }

    timeline = await _service(repository).page_timeline("task-1")

    visual = timeline["exploration_summary"]["visual_navigation"]
    assert visual["mode"] == "enforced"
    assert visual["model_usage"] == {
        "request_count": 3,
        "elapsed_ms": 42,
        "input_tokens": 90,
        "output_tokens": 10,
        "cache_hit_tokens": 5,
        "total_tokens": 100,
    }
    assert "h5_visual_navigation_usage" in repository.calls


async def test_h5_timeline_keeps_failed_audit_request_when_usage_ledger_is_empty() -> None:
    """空 SQL 聚合不能把失败视觉调用的安全 Audit 请求数覆盖为零。"""
    repository = FakeInsightRepository()
    assert repository.context is not None
    repository.context.update(
        {
            "target_platform": "h5",
            "status": "done",
            "task_config": {
                "workflow_version": "v2",
                "execution_mode": "exploration_only",
            },
        }
    )
    repository.h5_work_items = [
        {
            "work_id": "work-visual-failed",
            "status": "succeeded",
            "h5_visual_coordinate_mode": "enforced",
            "frontier": {
                "snapshot_id": "frontier-visual-failed",
                "pending_count": 0,
                "covered_page_types": ["home"],
            },
        }
    ]
    repository.h5_captures = [
        {
            "page_capture_id": "capture-visual-failed",
            "page_seq": 1,
            "page_type": "home",
            "detection_status": "recognized",
            "capture_role": "after_action",
            "visual_navigation_audit": {
                "protocol_version": "visual_navigation_audit.v1",
                "detector_status": "blocked",
                "prediction_count": 0,
                "target_kind_counts": {},
                "conflict_codes": ["visual_detector_failed"],
                "model_usage": {
                    "request_count": 1,
                    "elapsed_ms": 42,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_hit_tokens": 1,
                    "total_tokens": 15,
                },
            },
        }
    ]
    repository.h5_visual_navigation_usage = {
        "request_count": 0,
        "elapsed_ms": 999,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_tokens": 0,
        "total_tokens": 0,
    }

    timeline = await _service(repository).page_timeline("task-1")

    visual = timeline["exploration_summary"]["visual_navigation"]
    assert visual["model_usage"] == {
        "request_count": 1,
        "elapsed_ms": 42,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_tokens": 0,
        "total_tokens": 0,
    }
    assert visual["error_reason_counts"] == {"visual_detector_failed": 1}


@pytest.mark.parametrize(
    ("status", "complete", "quality_status", "terminal_reason"),
    [
        ("pending", False, "in_progress", "exploration_incomplete"),
        ("leased", False, "in_progress", "exploration_incomplete"),
        ("running", False, "in_progress", "exploration_incomplete"),
        ("retry_wait", False, "in_progress", "exploration_incomplete"),
        ("succeeded", True, "complete", None),
        ("bounded", False, "needs_review", "work_bounded"),
        ("budget_exhausted", False, "needs_review", "work_budget_exhausted"),
        ("blocked", False, "needs_review", "work_blocked"),
        ("no_progress", False, "needs_review", "work_no_progress"),
        ("failed", False, "technical_failed", "work_failed"),
        ("cancelled", False, "needs_review", "work_cancelled"),
    ],
)
def test_h5_terminal_maps_every_non_neutral_work_status(
    status: str,
    complete: bool,
    quality_status: str,
    terminal_reason: str | None,
) -> None:
    result = _h5_terminal_projection(
        work_items=[{"work_id": "work-1", "status": status}],
        frontier={"pending_count": 0},
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=[{"action_execution_id": "action-1", "status": "pass"}],
        action_count=1,
        assessment_scope_ready=True,
    )

    assert result == {
        "complete": complete,
        "quality_status": quality_status,
        "requires_review": quality_status == "needs_review",
        "terminal_reason": terminal_reason,
    }


def test_h5_terminal_treats_superseded_as_neutral_recovery_history() -> None:
    result = _h5_terminal_projection(
        work_items=[
            {"work_id": "old", "status": "superseded"},
            {"work_id": "successor", "status": "succeeded"},
        ],
        frontier={"pending_count": 0},
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=[{"action_execution_id": "action-1", "status": "pass"}],
        action_count=1,
        assessment_scope_ready=True,
    )

    assert result["complete"] is True
    assert result["terminal_reason"] is None


def test_h5_terminal_requires_review_for_audited_high_risk_candidates() -> None:
    result = _h5_terminal_projection(
        work_items=[{"work_id": "work-1", "status": "succeeded"}],
        frontier={
            "pending_count": 0,
            "blocked_count": 2,
            "entries": [
                {
                    "status": "blocked",
                    "last_failure_reason": "risk_level_not_allowed",
                },
                {
                    "status": "blocked",
                    "last_failure_reason": "risk_level_not_allowed",
                },
            ],
        },
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=[{"action_execution_id": "action-1", "status": "pass"}],
        action_count=1,
        assessment_scope_ready=True,
    )

    assert result == {
        "complete": False,
        "quality_status": "needs_review",
        "requires_review": True,
        "terminal_reason": "frontier_blocked",
    }


def test_h5_coverage_ready_does_not_reconstruct_completion_from_partial_fields() -> None:
    assert (
        h5_target_coverage_ready(
            {
                "completion_ready": False,
                "assessment_scope_ready": True,
                "required_page_types": ["home"],
                "covered_page_types": ["home"],
                "missing_page_types": [],
            }
        )
        is False
    )


def test_h5_coverage_ready_rejects_empty_target_denominator() -> None:
    assert (
        h5_target_coverage_ready(
            {
                "completion_ready": True,
                "assessment_scope_ready": True,
                "required_page_types": [],
                "covered_page_types": ["home"],
                "missing_page_types": [],
            }
        )
        is False
    )


async def test_missing_current_h5_snapshot_does_not_rebuild_legacy_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application import task_insight

    class MissingCoverageRepository(FakeInsightRepository):
        async def fetch_h5_coverage_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-1", "run-1")
            return None

    def forbid_legacy(**kwargs):
        pytest.fail("权威快照缺失时不得重建历史覆盖")

    repository = MissingCoverageRepository()
    repository.context["target_platform"] = "h5"
    repository.context["task_config"] = {
        "workflow_version": "v2", "execution_mode": "full_inspection",
    }
    monkeypatch.setattr(task_insight, "_legacy_h5_coverage_projection", forbid_legacy)

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["h5_coverage"]["authority_source"] == "unavailable"
    assert timeline["h5_coverage"]["completion_ready"] is False
    assert timeline["exploration_summary"]["complete"] is False


def test_legacy_h5_coverage_excludes_lifecycle_pages_from_dynamic_denominator() -> None:
    coverage = _legacy_h5_coverage_projection(
        frontier={"covered_page_types": ["guide_page"]},
        assessments=[
            {
                "page_type": "guide_page",
                "status": "covered",
                "catalog_sha256": "catalog-1",
            }
        ],
    )

    assert coverage["required_page_types"] == []
    assert coverage["covered_page_types"] == []
    assert h5_target_coverage_ready(coverage) is False


def test_h5_terminal_does_not_hide_failed_frontier_behind_risk_blocks() -> None:
    result = _h5_terminal_projection(
        work_items=[{"work_id": "work-1", "status": "succeeded"}],
        frontier={
            "pending_count": 0,
            "blocked_count": 1,
            "failed_count": 1,
            "entries": [
                {
                    "status": "blocked",
                    "last_failure_reason": "risk_level_not_allowed",
                },
                {
                    "status": "failed",
                    "last_failure_reason": "line_loading_reobserve_limit_exceeded",
                },
            ],
        },
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=[{"action_execution_id": "action-1", "status": "pass"}],
        action_count=1,
        assessment_scope_ready=True,
    )

    assert result["complete"] is False
    assert result["terminal_reason"] == "frontier_blocked"


@pytest.mark.parametrize(
    ("error_code", "terminal_reason"),
    [
        ("AUTHENTICATION_REQUIRED", "authentication_required"),
        ("H5_BROWSER_RECOVERY_FAILED", "recovery_failed"),
    ],
)
def test_h5_terminal_preserves_precise_error_reason_before_status(
    error_code: str, terminal_reason: str
) -> None:
    result = _h5_terminal_projection(
        work_items=[{"work_id": "work-1", "status": "failed", "last_error_code": error_code}],
        frontier={"pending_count": 0},
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=[{"action_execution_id": "action-1", "status": "pass"}],
        action_count=1,
        assessment_scope_ready=True,
    )

    assert result["terminal_reason"] == terminal_reason


@pytest.mark.parametrize(
    ("assessment_scope_ready", "terminal_reason"),
    [
        (False, "assessment_scope_incomplete"),
        (True, None),
    ],
)
def test_h5_terminal_requires_formally_proven_assessment_scope(
    assessment_scope_ready: bool, terminal_reason: str | None
) -> None:
    result = _h5_terminal_projection(
        work_items=[{"work_id": "work-1", "status": "succeeded"}],
        frontier={"pending_count": 0},
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=[],
        action_count=0,
        assessment_scope_ready=assessment_scope_ready,
    )

    assert result["terminal_reason"] == terminal_reason
    assert result["complete"] is (terminal_reason is None)


@pytest.mark.parametrize(
    ("frontier", "assessments", "ready"),
    [
        (
            {
                "covered_page_types": [],
                "sampling_policy_summary": {"include_page_types": ["home"]},
            },
            [],
            False,
        ),
        (
            {
                "covered_page_types": ["home"],
                "sampling_policy_summary": {"include_page_types": ["home", "payment"]},
            },
            [{"page_type": "home", "catalog_sha256": "catalog-1"}],
            True,
        ),
        (
            {
                "covered_page_types": ["home", "payment"],
                "sampling_policy_summary": {"include_page_types": ["home", "payment"]},
            },
            [
                {"page_type": "home", "catalog_sha256": "catalog-1"},
                {"page_type": "payment", "catalog_sha256": "catalog-1"},
            ],
            True,
        ),
        (
            {
                "covered_page_types": ["home", "payment"],
                "sampling_policy_summary": {"include_page_types": ["home", "payment"]},
            },
            [
                {"page_type": "home", "catalog_sha256": "catalog-1"},
                {"page_type": "payment", "catalog_sha256": "catalog-2"},
            ],
            False,
        ),
        (
            {"covered_page_types": ["home", "payment"]},
            [
                {"page_type": "home", "catalog_sha256": "catalog-1"},
                {"page_type": "payment"},
            ],
            False,
        ),
    ],
)
def test_h5_assessment_scope_readiness_requires_one_formal_catalog_set(
    frontier: dict[str, Any], assessments: list[dict[str, Any]], ready: bool
) -> None:
    assert _h5_assessment_scope_ready(frontier=frontier, assessments=assessments) is ready


async def test_h5_timeline_does_not_treat_frontier_allowlist_as_required_scope() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
    }
    repository.h5_work_items = [
        {
            "work_id": "work-1",
            "status": "succeeded",
            "frontier": {
                "pending_count": 0,
                "covered_page_types": ["home"],
                "sampling_policy_summary": {"include_page_types": ["home", "payment"]},
            },
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {"catalog_sha256": "catalog-1"},
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["exploration_summary"]["complete"] is True
    assert timeline["exploration_summary"]["terminal_reason"] is None
    assert timeline["terminal_reason"] is None


async def test_h5_timeline_fails_closed_when_one_covered_assessment_lacks_catalog() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
    }
    repository.h5_work_items = [
        {
            "work_id": "work-1",
            "status": "succeeded",
            "frontier": {
                "pending_count": 0,
                "covered_page_types": ["home", "payment"],
            },
        }
    ]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {"catalog_sha256": "catalog-1"},
        },
        {
            "assessment_id": "assessment-payment",
            "page_type": "payment",
            "status": "covered",
            "assessment_payload": {},
        },
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["exploration_summary"]["complete"] is False
    assert (
        timeline["exploration_summary"]["terminal_reason"]
        == "assessment_scope_incomplete"
    )
    assert timeline["terminal_reason"] == "assessment_scope_incomplete"


@pytest.mark.parametrize(
    ("action_count", "inventory", "terminal_reason"),
    [
        (0, [], None),
        (1, [], "evidence_inventory_incomplete"),
        (
            2,
            [{"action_execution_id": "action-1", "status": "pass"}],
            "evidence_inventory_incomplete",
        ),
        (
            1,
            [{"action_execution_id": "action-1", "status": "pass"}],
            None,
        ),
    ],
)
def test_h5_terminal_distinguishes_legal_zero_inventory_from_missing_expected_actions(
    action_count: int,
    inventory: list[dict[str, Any]],
    terminal_reason: str | None,
) -> None:
    result = _h5_terminal_projection(
        work_items=[{"work_id": "work-1", "status": "succeeded"}],
        frontier={"pending_count": 0},
        assessments=[{"page_type": "home", "status": "covered"}],
        inventory=inventory,
        action_count=action_count,
        assessment_scope_ready=True,
    )

    assert result["terminal_reason"] == terminal_reason


async def test_h5_timeline_partial_assessment_and_missing_inventory_fail_closed() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "status": "done",
        "task_config": {"workflow_version": "v2", "execution_mode": "exploration_only"},
    }
    repository.h5_work_items = [
        {
            "work_id": "work-1",
            "status": "succeeded",
            "frontier": {
                "pending_count": 0,
                "covered_page_types": ["home", "payment"],
                "sampling_policy_summary": {"include_page_types": ["home", "payment"]},
            },
        }
    ]
    repository.h5_actions = [{"action_execution_id": "action-1", "status": "explored"}]
    repository.h5_assessments = [
        {
            "assessment_id": "assessment-home",
            "page_type": "home",
            "status": "covered",
            "assessment_payload": {"catalog_sha256": "catalog-1"},
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["exploration_summary"]["complete"] is False
    assert timeline["exploration_summary"]["quality_status"] == "needs_review"
    assert timeline["terminal_reason"] == "assessment_scope_incomplete"


async def test_app_stage_trusts_explicit_target_platform_before_legacy_fallback() -> None:
    repository = FakeInsightRepository()
    repository.context = {
        **repository.context,
        "target_platform": "h5",
        "target_url": None,
        "task_config": {"product": {"product_code": "app-qc"}},
    }

    result = await _service(repository).app_stage("task-1")

    assert result == {"applicable": False, "task_id": "task-1"}


async def test_timeline_rollup_and_current_node() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(
            1,
            page_type="home",
            status="recognized",
            page_capture_id="cap-1",
            detection_status="recognized",
            detection_confidence=0.987,
            detection_duration_ms=1200,
            element_count=47,
            candidate_rule_total=8,
            screenshot_artifact_id="art-1",
        ),
        _node(
            2,
            page_type="payment",
            status="recognized",
            page_capture_id="cap-2",
            current_rule_code="alioth_13",
        ),
        _node(3, page_type="video_detail", status="pending"),
    ]
    repository.execution_rows = [
        _execution("alioth_2", "pass"),
        _execution("alioth_4", "fail"),
        _execution("alioth_5", "skipped"),
    ]
    service = _service(repository)
    timeline = await service.page_timeline("task-1")
    first = timeline["nodes"][0]
    assert first["result_rollup"] == "failed"
    assert first["rule_progress"] == {"executed": 3, "total": 8}
    assert first["detection"]["confidence"] == pytest.approx(0.987)
    assert first["element_count"] == 47
    assert first["page_state"] is None
    # current_rule_code 所在节点是当前节点，展示态为执行中。
    assert timeline["current_node_seq"] == 2
    second = timeline["nodes"][1]
    assert second["is_current"] is True
    assert second["status"] == "running"
    third = timeline["nodes"][2]
    assert third["status"] == "pending"
    assert third["status_label"] == "待执行"


async def test_timeline_exposes_complete_attention_reasons_and_real_candidate_total() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(
            1,
            page_type="video_list",
            status="recognized",
            page_capture_id="cap-1",
            inspection_lifecycle_status="superseded",
            candidate_rule_total=0,
        )
    ]
    repository.execution_rows = [
        _execution(
            "alioth_16",
            "manual_review",
            diagnostic_code="semantic_provider_http_503",
            result_summary="模型 API 返回 HTTP 503，未生成自动结论。",
            problem_desc="同源请求已短时熔断并转人工复核。",
        ),
        _execution(
            "alioth_21",
            "evidence_insufficient",
            problem_desc="尚未执行弹窗或遮罩关闭动作",
        ),
    ]

    timeline = await _service(repository).page_timeline("task-1")
    node = timeline["nodes"][0]

    assert node["rule_progress"] == {"executed": 2, "total": 2}
    assert [item["rule_code"] for item in node["attention_items"]] == [
        "alioth_16",
        "alioth_21",
    ]
    assert node["attention_items"][0]["diagnostic_message"] == (
        "模型 API 返回 HTTP 503（服务暂时不可用），自动判定未完成。"
    )
    assert node["attention_items"][1]["diagnostic_message"] == ("尚未执行弹窗或遮罩关闭动作")


async def test_timeline_exposes_dynamic_page_and_frontier_failure_summary() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(
            1,
            source="discovered",
            page_type="home",
            status="failed",
            page_capture_id="cap-1",
            inspection_lifecycle_status="active",
            inspection_status="degraded",
            inspection_failure_code="RULE_EXECUTION_WRITE_FAILED",
            inspection_failure_message="database unavailable",
        )
    ]
    repository.page_rows = [
        {
            "page_capture_id": "cap-1",
            "inspection_lifecycle_status": "active",
            "inspection_status": "degraded",
        }
    ]
    repository.exploration_actions = [
        {
            "exploration_action_id": "exploration-1",
            "action": "click",
            "status": "failed",
            "failure_code": "PAGE_CAPTURE_FAILED",
        }
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert timeline["exploration_summary"]["page_total"] == 1
    assert timeline["exploration_summary"]["degraded_page_count"] == 1
    assert timeline["exploration_summary"]["failed_action_count"] == 1
    assert timeline["exploration_summary"]["complete"] is True
    assert timeline["nodes"][0]["inspection"] == {
        "lifecycle_status": "active",
        "status": "degraded",
        "status_label": "页面检查完成 · 需复核",
        "failure_stage": None,
        "failure_code": "RULE_EXECUTION_WRITE_FAILED",
        "failure_message": "database unavailable",
        "failure_retryable": None,
        "abandon_reason": None,
    }


async def test_timeline_labels_disabled_supplemental_evidence_as_insufficient() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(
            1,
            source="discovered",
            page_type="line_loading",
            status="failed",
            page_capture_id="cap-1",
            inspection_lifecycle_status="active",
            inspection_status="degraded",
            inspection_failure_code="SUPPLEMENTAL_EVIDENCE_DISABLED",
            inspection_failure_message="当前策略暂不执行补采。",
        )
    ]

    timeline = await _service(repository).page_timeline("task-1")

    inspection = timeline["nodes"][0]["inspection"]
    assert inspection["status"] == "degraded"
    assert inspection["status_label"] == "证据不足"
    assert inspection["failure_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"


async def test_rule_results_merge_candidates_and_running() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(
            1,
            page_type="home",
            status="recognized",
            page_capture_id="cap-1",
            current_rule_code="alioth_16",
        ),
    ]
    repository.execution_rows = [_execution("alioth_2", "pass")]
    repository.applicable_rules = [
        {
            "rule_id": "rule-alioth_2",
            "rule_code": "alioth_2",
            "rule_name": "规则alioth_2",
            "category": "text",
            "severity": "low",
            "executor_type": "llm",
            "page_type": "global",
            "rule_package": None,
            "trigger_display": None,
            "applicable_page_types": ["home"],
            "check_point": "检查点",
            "sort_order": 1,
        },
        {
            "rule_id": "rule-alioth_16",
            "rule_code": "alioth_16",
            "rule_name": "规则alioth_16",
            "category": "ad",
            "severity": "high",
            "executor_type": "hybrid",
            "page_type": "home",
            "rule_package": None,
            "trigger_display": None,
            "applicable_page_types": ["home"],
            "check_point": "检查点",
            "sort_order": 2,
        },
        {
            "rule_id": "rule-alioth_17",
            "rule_code": "alioth_17",
            "rule_name": "规则alioth_17",
            "category": "ad",
            "severity": "medium",
            "executor_type": "hybrid",
            "page_type": "home",
            "rule_package": None,
            "trigger_display": None,
            "applicable_page_types": ["home"],
            "check_point": "检查点",
            "sort_order": 3,
        },
    ]
    service = _service(repository)
    result = await service.rule_results("task-1")
    by_code = {item["rule_code"]: item for item in result["items"]}
    assert by_code["alioth_2"]["display_status"] == "passed"
    assert by_code["alioth_2"]["item_type"] == "execution"
    # 当前正在执行的规则展示为执行中，其余候选为待执行。
    assert by_code["alioth_16"]["display_status"] == "running"
    assert by_code["alioth_17"]["display_status"] == "pending"
    assert by_code["alioth_17"]["rule_package"] == "广告质检"
    assert "触发" in by_code["alioth_17"]["trigger_display"]
    assert result["status_counts"] == {"passed": 1, "running": 1, "pending": 1}
    # 状态筛选生效。
    filtered = await service.rule_results("task-1", display_status="pending")
    assert [item["rule_code"] for item in filtered["items"]] == ["alioth_17"]


async def test_rule_results_terminal_marks_unexecuted_as_coverage_gap() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "done"
    repository.nodes = [
        _node(1, page_type="home", status="recognized", page_capture_id="cap-1"),
        _node(2, page_type="payment", status="skipped"),
    ]
    repository.execution_rows = [_execution("alioth_2", "pass")]
    repository.applicable_rules = [
        {
            "rule_id": "rule-alioth_13",
            "rule_code": "alioth_13",
            "rule_name": "价格一致",
            "category": "payment",
            "severity": "high",
            "executor_type": "hybrid",
            "page_type": "payment",
            "rule_package": None,
            "trigger_display": None,
            "applicable_page_types": ["payment"],
            "check_point": "检查点",
            "sort_order": 1,
        },
    ]
    service = _service(repository)
    result = await service.rule_results("task-1")
    by_code = {item["rule_code"]: item for item in result["items"]}
    assert by_code["alioth_13"]["display_status"] == "coverage_gap"
    assert by_code["alioth_13"]["result_summary"] == "页面未访问，规则未执行"


async def test_rule_results_separates_budget_termination_from_evidence_gap() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "done"
    repository.execution_rows = [
        _execution(
            "alioth_17",
            "skipped",
            execution_reason="candidate_not_executed",
        ),
        _execution("alioth_21", "evidence_insufficient"),
    ]

    result = await _service(repository).rule_results("task-1")
    by_code = {item["rule_code"]: item for item in result["items"]}

    assert by_code["alioth_17"]["display_status"] == "not_executed"
    assert by_code["alioth_21"]["display_status"] == "evidence_insufficient"
    assert result["status_counts"] == {
        "not_executed": 1,
        "evidence_insufficient": 1,
    }


async def test_rule_results_show_machine_status_and_human_final_verdict_together() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "done"
    repository.execution_rows = [
        _execution(
            "alioth_30",
            "manual_review",
            category="input_risk",
            review_status="approved",
            human_verdict="pass",
            human_verdict_note="人工确认通过",
            human_verdict_at=NOW,
        )
    ]

    result = await _service(repository).rule_results("task-1")
    item = result["items"][0]
    assert item["machine_display_status"] == "manual_review"
    assert item["display_status"] == "passed"
    assert item["human_verdict_label"] == "通过"
    assert item["confirmable"] is True
    assert item["confirm_status"] == "confirmed"
    assert result["status_counts"] == {"passed": 1}


async def test_timeline_hides_nested_detail_return_revisit_audit_node() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(
            1,
            source="discovered",
            page_type="post_list",
            status="recognized",
            page_capture_id="cap-list",
            inspection_lifecycle_status="active",
            inspection_status="completed",
            candidate_rule_total=2,
        ),
        _node(
            2,
            source="discovered",
            page_type="post_list",
            status="recognized",
            page_capture_id="cap-return",
            inspection_lifecycle_status="abandoned",
            inspection_status="matching",
            inspection_abandon_reason="nested_detail_return_revisit",
        ),
    ]
    repository.execution_rows = [
        _execution("alioth_2", "pass", page_capture_id="cap-list"),
        _execution("alioth_4", "fail", page_capture_id="cap-list"),
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert [node["page_capture_id"] for node in timeline["nodes"]] == ["cap-list"]
    assert timeline["nodes"][0]["rule_progress"] == {"executed": 2, "total": 2}


async def test_abandoned_zero_rule_transition_is_hidden_from_timeline() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "done"
    repository.nodes = [
        _node(
            1,
            page_type="line_loading",
            status="recognized",
            page_capture_id="cap-1",
            inspection_lifecycle_status="abandoned",
            inspection_status="detecting",
        )
    ]
    repository.execution_rows = []
    repository.applicable_rules = [
        {
            "rule_id": "rule-alioth_2",
            "rule_code": "alioth_2",
            "rule_name": "文案检查",
            "category": "text",
            "severity": "low",
            "executor_type": "llm",
            "page_type": "global",
            "rule_package": None,
            "trigger_display": None,
            "applicable_page_types": ["line_loading"],
            "check_point": "检查点",
            "sort_order": 1,
        }
    ]

    service = _service(repository)
    result = await service.rule_results("task-1")
    timeline = await service.page_timeline("task-1")

    assert result["total"] == 0
    assert result["items"] == []
    assert timeline["nodes"] == []


async def test_failed_zero_rule_capture_remains_visible_for_diagnosis() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "review"
    repository.nodes = [
        _node(
            1,
            page_type="profile",
            status="recognized",
            page_capture_id="cap-1",
            inspection_lifecycle_status="abandoned",
            inspection_status="failed",
            inspection_failure_code="PAGE_CAPTURE_FAILED",
            inspection_failure_message="关闭弹窗后采集失败",
        )
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert len(timeline["nodes"]) == 1
    assert timeline["nodes"][0]["rule_progress"] == {"executed": 0, "total": 0}
    assert timeline["nodes"][0]["inspection"]["failure_code"] == "PAGE_CAPTURE_FAILED"


async def test_stats_reports_recent_anomalies() -> None:
    repository = FakeInsightRepository()
    repository.nodes = [
        _node(1, page_type="home", status="recognized", page_capture_id="cap-1"),
    ]
    early = NOW.replace(hour=9)
    repository.execution_rows = [
        _execution("alioth_2", "pass"),
        _execution("alioth_5", "skipped", finished_at=early),
        _execution("alioth_6", "error"),
    ]
    service = _service(repository)
    stats = await service.stats("task-1")
    assert stats["triggered"] == 3
    assert stats["status_counts"]["passed"] == 1
    assert stats["status_counts"]["not_applicable"] == 1
    assert stats["status_counts"]["detect_error"] == 1
    codes = [item["rule_code"] for item in stats["recent_anomalies"]]
    assert codes == ["alioth_6"]


async def test_stats_does_not_report_unstarted_exploration_as_not_applicable() -> None:
    """导航前失败没有业务判定，不得把收尾占位执行解释为确认不适用。"""
    repository = FakeInsightRepository()
    repository.context["status"] = "review"
    repository.nodes = []
    repository.execution_rows = [
        _execution(
            "alioth_2",
            "skipped",
            execution_reason="exploration_not_started",
        )
    ]

    stats = await _service(repository).stats("task-1")

    assert stats["status_counts"] == {"not_executed": 1}


async def test_stats_progress_percent_stable_across_page_discovery() -> None:
    """主进度不受规则完成数和探索中发现的新规则候选影响。"""
    repository = FakeInsightRepository()
    repository.context["started_at"] = None
    repository.context["ruleset_rule_total"] = 8
    repository.nodes = [
        _node(1, page_type="home", status="recognized", page_capture_id="cap-1"),
    ]
    repository.execution_rows = [_execution(f"alioth_{index}", "pass") for index in range(1, 5)]
    repository.applicable_rules = [_rule(f"alioth_{index}") for index in range(1, 5)]
    repository.exploration_actions = [
        {"exploration_action_id": "act-1", "status": "candidate"},
        {"exploration_action_id": "act-2", "status": "selected"},
        {"exploration_action_id": "act-3", "status": "explored"},
    ]
    service = _service(repository)
    stats_before = await service.stats("task-1")
    assert stats_before["frontier_pending"] == 2
    assert stats_before["total"] == 8
    assert stats_before["finished"] == 4
    assert stats_before["progress_percent"] == 5

    # 探索落地一个新页面：Frontier 少一个待探索入口，新页候选规则进入已知总量
    repository.nodes.append(
        _node(2, page_type="payment", status="recognized", page_capture_id="cap-2")
    )
    repository.applicable_rules.extend(
        _rule(f"alioth_pay_{index}", applicable_page_types=["payment"]) for index in range(1, 5)
    )
    repository.exploration_actions = [
        {"exploration_action_id": "act-1", "status": "explored"},
        {"exploration_action_id": "act-2", "status": "selected"},
        {"exploration_action_id": "act-3", "status": "explored"},
    ]
    stats_after = await service.stats("task-1")
    assert stats_after["frontier_pending"] == 1
    assert stats_after["progress_percent"] == stats_before["progress_percent"]


async def test_stats_progress_percent_anchors_terminal_status() -> None:
    repository = FakeInsightRepository()
    repository.context["status"] = "done"
    repository.execution_rows = [_execution("alioth_2", "pass")]
    service = _service(repository)
    stats = await service.stats("task-1")
    assert stats["progress_percent"] == 100


@pytest.mark.parametrize("status", ["review", "done", "failed", "canceled"])
async def test_task_loading_ends_without_completing_rule_placeholders(status: str) -> None:
    repository = FakeInsightRepository()
    repository.context.update(status=status, ruleset_rule_total=73, run_status="failed")
    repository.execution_rows = [
        _execution(f"alioth_{index}", "skipped", execution_reason=reason)
        for index, reason in enumerate([
            "exploration_not_started", "auth_requirement_not_met", "not_matched_by_run"
        ])
    ]
    stats = await _service(repository).stats("task-1")
    assert stats["total"] == 73
    assert stats["triggered"] == 0
    assert stats["finished"] == 0
    assert stats["progress_percent"] == 100
    assert stats["status_counts"] == {"not_executed": 3}


async def test_rule_completion_does_not_follow_elapsed_budget_or_raw_execution_count() -> None:
    repository = FakeInsightRepository()
    repository.context.update(ruleset_rule_total=73, loop_guard={"max_duration_seconds": 1})
    repository.execution_rows = [
        _execution("passed", "pass"),
        _execution("business-review", "manual_review"),
        _execution("not-applicable", "skipped", execution_reason="product_not_applicable"),
        _execution("bootstrap", "skipped", is_bootstrap=True),
        _execution("not-run", "skipped", execution_reason="exploration_not_started"),
        _execution("provider-error", "manual_review", execution_reason="semantic_provider_http_500"),
        _execution("missing-evidence", "evidence_insufficient"),
        _execution("execution-error", "error"),
    ]
    stats = await _service(repository).stats("task-1")
    assert stats["triggered"] == 6
    assert stats["finished"] == 3
    assert stats["total"] == 73
    assert stats["progress_percent"] == 95


async def test_legacy_supplemental_denial_does_not_inflate_task_completion() -> None:
    repository = FakeInsightRepository()
    repository.context.update(status="review", ruleset_rule_total=2)
    repository.execution_rows = [
        _execution("passed", "pass"),
        _execution("gap", "skipped", error_code="SUPPLEMENTAL_EVIDENCE_DISABLED"),
    ]
    stats = await _service(repository).stats("task-1")
    assert stats["finished"] == 1
    assert stats["progress_percent"] == 100
    assert stats["status_counts"] == {"passed": 1, "evidence_insufficient": 1}


async def test_fully_executed_business_review_can_reach_complete_progress() -> None:
    repository = FakeInsightRepository()
    repository.context.update(status="review", ruleset_rule_total=3)
    repository.execution_rows = [
        _execution("passed", "pass"),
        _execution("failed", "fail"),
        _execution("business-review", "manual_review"),
    ]
    stats = await _service(repository).stats("task-1")
    assert stats["finished"] == stats["total"] == 3
    assert stats["progress_percent"] == 100


async def test_exploration_only_completion_does_not_invent_formal_rule_counts() -> None:
    repository = FakeInsightRepository()
    repository.context.update(status="done", ruleset_rule_total=73)
    repository.context["task_config"]["execution_mode"] = "exploration_only"
    stats = await _service(repository).stats("task-1")
    assert stats["total"] == stats["finished"] == stats["triggered"] == 0
    assert stats["progress_percent"] == 100
    assert "rule_executions" not in repository.calls


@pytest.mark.parametrize("run_budget", [
    {"max_duration_seconds": 3600},
    {"timeout_minutes": 60},
    {"max_duration_seconds": "bad"},
    {},
])
async def test_stats_loading_uses_frozen_run_budget_without_changing_rule_completion(
    monkeypatch: pytest.MonkeyPatch,
    run_budget: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        task_insight,
        "whole_task_progress_percent",
        lambda status, **kwargs: whole_task_progress_percent(status, now=NOW, **kwargs),
    )
    repository = FakeInsightRepository()
    repository.context.update(
        started_at=NOW - timedelta(seconds=1800),
        loop_guard={"max_duration_seconds": 1800, "max_steps": 10},
        run_loop_guard=run_budget,
        ruleset_rule_total=208,
    )
    repository.execution_rows = [_execution(f"passed-{index}", "pass") for index in range(116)]
    service = _service(repository)
    stats = await service.stats("task-1")
    expected = whole_task_progress_percent(
        "running", started_at=repository.context["started_at"],
        loop_guard=run_budget or repository.context["loop_guard"],
        run_status="running", now=NOW,
    )
    assert stats["progress_percent"] == expected
    assert (stats["finished"], stats["total"]) == (116, 208)
    repository.context.update(run_status="failed", finished_at=NOW)
    stats = await service.stats("task-1")
    assert stats["progress_percent"] == 99
    assert (stats["finished"], stats["total"]) == (116, 208)
    repository.context["status"] = "review"
    stats = await service.stats("task-1")
    assert stats["progress_percent"] == 100
    assert (stats["finished"], stats["total"]) == (116, 208)


async def test_screenshots_group_and_dedupe_by_checksum() -> None:
    repository = FakeInsightRepository()
    repository.execution_rows = [
        _execution("alioth_2", "fail", evidence_artifact_ids=["art-1"]),
    ]
    base = {
        "content_type": "image/png",
        "size_bytes": 100,
        "storage_path": "p",
        "created_at": NOW,
        "captured_at": NOW,
        "url": "https://example.com",
    }
    repository.screenshots = [
        {
            **base,
            "artifact_id": "art-1",
            "page_capture_id": "cap-1",
            "checksum": "sha256:aaa",
            "page_type": "home",
            "page_title": "漫画首页",
            "page_seq": 1,
        },
        {
            **base,
            "artifact_id": "art-2",
            "page_capture_id": "cap-1",
            "checksum": "sha256:aaa",
            "page_type": "home",
            "page_title": "漫画首页",
            "page_seq": 1,
        },
        {
            **base,
            "artifact_id": "art-3",
            "page_capture_id": "cap-2",
            "checksum": "sha256:bbb",
            "page_type": "payment",
            "page_title": "会员充值",
            "page_seq": 2,
        },
        {
            **base,
            "artifact_id": "art-4",
            "page_capture_id": "cap-3",
            "checksum": "sha256:ccc",
            "page_type": "home",
            "page_title": "AI世界首页",
            "page_seq": 3,
        },
    ]
    service = _service(repository)
    payload = await service.screenshots("task-1")
    groups = {group["page_display_name"]: group for group in payload["groups"]}
    home = groups["漫画首页-home"]
    assert home["total"] == 2
    assert len(home["items"]) == 1
    assert home["items"][0]["duplicate_count"] == 2
    assert home["items"][0]["related_rules"][0]["rule_code"] == "alioth_2"
    assert groups["会员充值-payment"]["items"][0]["duplicate_count"] == 1
    assert groups["AI世界首页-home"]["items"][0]["duplicate_count"] == 1


async def test_web_ad_gallery_uses_landing_screenshot_only() -> None:
    repository = FakeInsightRepository()
    repository.context["target_platform"] = "web"
    base = {
        "page_capture_id": "capture-ad",
        "page_type": "ad_page",
        "page_title": "广告落地页",
        "capture_purpose": "web_ad_page",
        "url": "https://ads.example/landing",
        "content_type": "image/png",
        "size_bytes": 100,
        "created_at": NOW,
        "captured_at": NOW,
    }
    repository.screenshots = [
        {**base, "artifact_id": "source", "checksum": "sha256:source", "screenshot_purpose": None},
        {
            **base,
            "artifact_id": "pair",
            "checksum": "sha256:pair",
            "screenshot_purpose": "ad_landing_consistency",
        },
        {
            **base,
            "artifact_id": "landing",
            "checksum": "sha256:landing",
            "screenshot_purpose": "ad_landing",
        },
    ]

    payload = await _service(repository).screenshots("task-1")

    assert [item["artifact_id"] for group in payload["groups"] for item in group["items"]] == [
        "landing"
    ]
    repository.screenshots.pop()
    assert (await _service(repository).screenshots("task-1"))["groups"] == []


async def test_h5_screenshots_keep_rule_evidence_but_hide_gallery_duplicates() -> None:
    repository = FakeInsightRepository()
    repository.context["target_platform"] = "h5"
    base = {
        "content_type": "image/png",
        "size_bytes": 100,
        "storage_path": "p",
        "created_at": NOW,
        "captured_at": NOW,
        "url": "https://example.com/profile",
        "page_type": "profile",
        "page_title": "个人中心",
        "page_state": "stable",
        "logical_identity_fingerprint": "sha256:" + "1" * 64,
        "logical_state_fingerprint": "sha256:" + "2" * 64,
    }
    repository.screenshots = [
        {
            **base,
            "artifact_id": "before",
            "page_capture_id": "cap-before",
            "checksum": "sha256:before",
            "capture_role": "before_action",
        },
        {
            **base,
            "artifact_id": "after-1",
            "page_capture_id": "cap-after-1",
            "checksum": "sha256:after-1",
            "capture_role": "after_action",
        },
        {
            **base,
            "artifact_id": "after-2",
            "page_capture_id": "cap-after-2",
            "checksum": "sha256:after-2",
            "capture_role": "after_action",
            "url": "https://example.com/profile?refresh=2",
        },
    ]

    payload = await _service(repository).screenshots("task-1")

    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["total"] == 3
    assert [item["artifact_id"] for item in group["items"]] == [
        "before",
        "after-1",
        "after-2",
    ]
    assert [item["gallery_visible"] for item in group["items"]] == [
        False,
        True,
        False,
    ]
    assert group["items"][1]["duplicate_count"] == 2
    assert group["items"][2]["duplicate_of_artifact_id"] == "after-1"


async def test_h5_screenshots_ignore_volatile_state_but_keep_meaningful_tab_variants() -> None:
    repository = FakeInsightRepository()
    repository.context["target_platform"] = "h5"
    base = {
        "content_type": "image/png",
        "size_bytes": 100,
        "storage_path": "p",
        "created_at": NOW,
        "captured_at": NOW,
        "url": "https://example.com/home",
        "page_type": "home",
        "page_title": "首页",
        "page_state": "stable",
        "capture_role": "after_action",
        "logical_identity_fingerprint": "sha256:" + "1" * 64,
    }
    repository.screenshots = [
        {
            **base,
            "artifact_id": "recommended-1",
            "page_capture_id": "cap-1",
            "checksum": "sha256:one",
            "logical_state_fingerprint": "sha256:" + "2" * 64,
            "logical_state_signals": {"selected_tab": "推荐", "scroll_bucket": "top"},
        },
        {
            **base,
            "artifact_id": "recommended-ticker-changed",
            "page_capture_id": "cap-2",
            "checksum": "sha256:two",
            "logical_state_fingerprint": "sha256:" + "3" * 64,
            "logical_state_signals": {"selected_tab": "推荐", "scroll_bucket": "middle"},
        },
        {
            **base,
            "artifact_id": "domestic",
            "page_capture_id": "cap-3",
            "checksum": "sha256:three",
            "logical_state_fingerprint": "sha256:" + "4" * 64,
            "logical_state_signals": {"selected_tab": "国产", "scroll_bucket": "top"},
        },
    ]

    payload = await _service(repository).screenshots("task-1")
    items = payload["groups"][0]["items"]

    assert [item["gallery_visible"] for item in items] == [True, False, True]
    assert items[0]["duplicate_count"] == 2
    assert items[1]["duplicate_of_artifact_id"] == "recommended-1"


async def test_screenshots_carry_presentation_and_settle_quality() -> None:
    """探索过程展示：每张截图带展示层评估（最上层/底页可见/诊断）与页面稳定性判定。"""
    repository = FakeInsightRepository()
    base = {
        "content_type": "image/png",
        "size_bytes": 100,
        "storage_path": "p",
        "created_at": NOW,
        "captured_at": NOW,
        "url": "https://example.com",
        "page_type": "home",
        "page_title": "首页",
        "page_seq": 1,
    }
    repository.screenshots = [
        {
            **base,
            "artifact_id": "art-1",
            "page_capture_id": "cap-1",
            "checksum": "sha256:aaa",
            "page_state": "stable",
            "presentation": {
                "protocol_version": "presentation-stack.v1",
                "topmost_layer_kind": "page",
                "base_page_visibility": "visible",
                "blocking_scope": "none",
                "mutation_mode": "instrumented_diagnostic",
                "hit_test_status": "available",
                "evidence_sources": ["h5_ui_snapshot", "h5_action_log"],
                "diagnostic_operations": [
                    {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
                ],
                "uncertainty": [],
                "document_epoch_ms": 1788845247999.7,
            },
            "settle_diagnostics": json.dumps(
                {"outcome": "settled", "dynamic_reasons": [], "blocking_reasons": []}
            ),
        },
        {
            **base,
            "artifact_id": "art-2",
            "page_capture_id": "cap-2",
            "checksum": "sha256:bbb",
            "page_seq": 2,
            "page_state": "popup",
            "presentation": None,
            "settle_diagnostics": None,
        },
    ]
    service = _service(repository)
    payload = await service.screenshots("task-1")
    items = payload["groups"][0]["items"]
    assert items[0]["page_state"] == "stable"
    assert items[0]["presentation"] == {
        "protocol_version": "presentation-stack.v1",
        "topmost_layer_kind": "page",
        "base_page_visibility": "visible",
        "blocking_scope": "none",
        "mutation_mode": "instrumented_diagnostic",
        "hit_test_status": "available",
        "diagnostic_operations": [
            {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
        ],
        "uncertainty": [],
    }
    assert items[0]["settle"] == {
        "outcome": "settled",
        "blocking_reasons": [],
        "dynamic_reasons": [],
    }
    assert items[1]["page_state"] == "popup"
    assert items[1]["presentation"] is None
    assert items[1]["settle"] is None


async def test_screenshots_keeps_only_one_startup_and_one_popup_ad() -> None:
    """首屏广告、弹窗广告各只展示一张，轮播素材不得刷屏。"""

    repository = FakeInsightRepository()
    repository.context["target_platform"] = "app"
    base = {
        "content_type": "image/png",
        "size_bytes": 100,
        "storage_path": "p",
        "created_at": NOW,
        "captured_at": NOW,
        "url": "https://example.com",
        "package_name": "uk.example.app",
        "activity": ".Main",
        "width": 1080,
        "height": 1920,
    }
    repository.screenshots = [
        {
            **base,
            "artifact_id": "startup-1",
            "page_capture_id": "cap-s1",
            "checksum": "sha256:s1",
            "page_type": "startup_ad",
            "page_state": "startup_ad",
            "page_title": None,
            "page_seq": 1,
        },
        {
            **base,
            "artifact_id": "startup-2",
            "page_capture_id": "cap-s2",
            "checksum": "sha256:s2",
            "page_type": "startup_ad",
            "page_state": "countdown",
            "page_title": None,
            "page_seq": 2,
        },
        {
            **base,
            "artifact_id": "popup-1",
            "page_capture_id": "cap-p1",
            "checksum": "sha256:p1",
            "overlay_signature": "popup:ad-a",
            "page_type": "video_list",
            "page_state": "popup",
            "page_title": "长视频",
            "page_seq": 3,
        },
        {
            **base,
            "artifact_id": "popup-2",
            "page_capture_id": "cap-p2",
            "checksum": "sha256:p2",
            "overlay_signature": "popup:ad-b",
            "page_type": "video_list",
            "page_state": "popup",
            "page_title": "长视频",
            "page_seq": 4,
        },
        {
            **base,
            "artifact_id": "biz-1",
            "page_capture_id": "cap-b1",
            "checksum": "sha256:b1",
            "page_type": "video_list",
            "page_state": "stable",
            "page_title": "长视频",
            "page_seq": 5,
        },
        {
            **base,
            "artifact_id": "biz-2",
            "page_capture_id": "cap-b2",
            "checksum": "sha256:b2",
            "page_type": "video_list",
            "page_state": "stable",
            "page_title": "长视频",
            "page_seq": 6,
        },
    ]
    service = _service(repository)
    payload = await service.screenshots("task-1")
    items = {item["artifact_id"]: item for group in payload["groups"] for item in group["items"]}
    # 同组同 page_type 稳定页也只展一张。
    assert set(items) == {"startup-1", "popup-1", "biz-1"}
    assert items["startup-1"]["occlusion_kind"] == "startup"
    assert items["startup-1"]["related_frames"][0]["artifact_id"] == "startup-2"
    assert items["popup-1"]["occlusion_kind"] == "overlay"
    assert items["popup-1"]["related_frames"][0]["artifact_id"] == "popup-2"
    assert items["biz-1"]["occlusion_kind"] == "stable"
    assert items["biz-1"]["related_frames"][0]["artifact_id"] == "biz-2"


async def test_screenshots_merges_app_home_tab_titles_into_one_group() -> None:
    """上方分类页名不同但同屏网格：App 截图证据只展 1 组、最多 2 张代表图。"""

    repository = FakeInsightRepository()
    repository.context["target_platform"] = "app"
    base = {
        "content_type": "image/png",
        "size_bytes": 100,
        "storage_path": "p",
        "created_at": NOW,
        "captured_at": NOW,
        "url": "app://home",
        "package_name": "dark.wwcskrpojs.ebjhyebjbl",
        "activity": ".MainActivity",
        "width": 1080,
        "height": 1920,
        "page_type": "home",
        "page_state": "stable",
    }
    titles = ("最新", "推荐", "国产", "动漫", "漫画", "最新", "推荐")
    repository.screenshots = [
        {
            **base,
            "artifact_id": f"home-{idx}",
            "page_capture_id": f"cap-{idx}",
            "checksum": f"sha256:home-{idx}",
            "page_title": title,
            "page_seq": idx,
        }
        for idx, title in enumerate(titles, start=1)
    ]
    service = _service(repository)
    payload = await service.screenshots("task-1")
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert len(group["items"]) == 1
    head = group["items"][0]
    assert head["artifact_id"] == "home-1"
    assert len(head["related_frames"]) == 6
    assert {frame["artifact_id"] for frame in head["related_frames"]} == {
        f"home-{idx}" for idx in range(2, 8)
    }


async def test_screenshots_archive_dedupes_and_reads_store() -> None:
    repository = FakeInsightRepository()
    repository.screenshots = [
        {
            "artifact_id": "art-1",
            "page_capture_id": "cap-1",
            "checksum": "sha256:aaa",
            "content_type": "image/png",
            "size_bytes": 1,
            "storage_path": "task/cap-1/art-1.png",
            "created_at": NOW,
            "page_type": "home",
            "page_seq": 1,
            "captured_at": NOW,
            "url": "u",
        },
        {
            "artifact_id": "art-2",
            "page_capture_id": "cap-1",
            "checksum": "sha256:aaa",
            "content_type": "image/png",
            "size_bytes": 1,
            "storage_path": "task/cap-1/art-2.png",
            "created_at": NOW,
            "page_type": "home",
            "page_seq": 1,
            "captured_at": NOW,
            "url": "u",
        },
    ]
    store = FakeEvidenceStore()
    service = TaskInsightService(repository=repository, evidence_store_factory=lambda: store)
    filename, content = await service.screenshots_archive("task-1")
    assert filename == "task_task-1_screenshots.zip"
    assert store.reads == ["task/cap-1/art-1.png"]


async def test_web_ad_archive_names_source_and_landing_evidence_separately() -> None:
    repository = FakeInsightRepository()
    repository.screenshots = [
        {
            "artifact_id": artifact_id,
            "page_capture_id": "capture-ad",
            "checksum": f"sha256:{artifact_id}",
            "content_type": "image/png",
            "storage_path": f"task/capture-ad/{artifact_id}.png",
            "page_type": "ad_page",
            "capture_purpose": "web_ad_page",
            "screenshot_purpose": purpose,
            "page_seq": 2,
        }
        for artifact_id, purpose in (
            ("source123", None),
            ("landing123", "ad_landing"),
        )
    ]
    store = FakeEvidenceStore()
    service = TaskInsightService(repository=repository, evidence_store_factory=lambda: store)

    _, content = await service.screenshots_archive("task-1")

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == [
            "02_ad_page_source_source12.png",
            "02_ad_page_landing_landing1.png",
        ]
    assert content[:2] == b"PK"


async def test_web_ad_archive_keeps_each_role_when_images_have_same_checksum() -> None:
    repository = FakeInsightRepository()
    repository.screenshots = [
        {
            "artifact_id": artifact_id,
            "page_capture_id": capture_id,
            "checksum": "sha256:same-image",
            "content_type": "image/png",
            "storage_path": f"task/{capture_id}/{artifact_id}.png",
            "page_type": page_type,
            "capture_purpose": capture_purpose,
            "screenshot_purpose": purpose,
            "page_seq": page_seq,
        }
        for artifact_id, capture_id, page_type, capture_purpose, purpose, page_seq in (
            ("home1234", "capture-home", "home", None, None, 1),
            ("source12", "capture-ad", "ad_page", "web_ad_page", None, 2),
            ("landing1", "capture-ad", "ad_page", "web_ad_page", "ad_landing", 2),
        )
    ]
    store = FakeEvidenceStore()
    for artifact in repository.screenshots:
        store.payloads[artifact["storage_path"]] = b"\x89PNG\r\n\x1a\nsame-image"
    service = TaskInsightService(repository=repository, evidence_store_factory=lambda: store)

    _, content = await service.screenshots_archive("task-1")

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == [
            "01_home_home1234.png",
            "02_ad_page_source_source12.png",
            "02_ad_page_landing_landing1.png",
        ]


async def test_evidence_file_reads_store() -> None:
    repository = FakeInsightRepository()
    repository.artifacts["art-1"] = {
        "artifact_id": "art-1",
        "task_id": "task-1",
        "page_capture_id": "cap-1",
        "artifact_type": "screenshot",
        "source": "baseline",
        "storage_path": "task/cap-1/art-1.png",
        "content_type": "image/png",
        "size_bytes": 10,
        "created_at": NOW,
    }
    service = _service(repository)
    payload = await service.evidence_file("art-1")
    assert payload is not None
    assert payload["content"].startswith(b"png-bytes:")
    assert await service.evidence_file("missing") is None


async def test_evidence_file_renders_first_frame_of_screenshot_sequence() -> None:
    repository = FakeInsightRepository()
    storage_path = "task/cap-1/art-sequence.json"
    repository.artifacts["art-sequence"] = {
        "artifact_id": "art-sequence",
        "task_id": "task-1",
        "page_capture_id": "cap-1",
        "artifact_type": "screenshot",
        "source": "baseline",
        "storage_path": storage_path,
        "content_type": "application/json",
        "size_bytes": 10,
        "created_at": NOW,
    }
    store = FakeEvidenceStore()
    first_frame = b"\x89PNG\r\n\x1a\nfirst"
    second_frame = b"\x89PNG\r\n\x1a\nsecond"
    store.payloads[storage_path] = json.dumps(
        {
            "sequence": True,
            "frames": [
                {"index": 0, "encoding": "base64", "data": base64.b64encode(first_frame).decode()},
                {
                    "index": 1,
                    "encoding": "base64",
                    "data": base64.b64encode(second_frame).decode(),
                },
            ],
        }
    ).encode()
    service = TaskInsightService(
        repository=repository,
        evidence_store_factory=lambda: store,
    )

    payload = await service.evidence_file("art-sequence")

    assert payload is not None
    assert payload["content_type"] == "image/png"
    assert payload["content"] == first_frame


async def test_screenshots_archive_expands_sequence_frames() -> None:
    repository = FakeInsightRepository()
    storage_path = "task/cap-1/art-sequence.json"
    repository.screenshots = [
        {
            "artifact_id": "art-sequence",
            "page_capture_id": "cap-1",
            "checksum": "sha256:sequence",
            "content_type": "application/json",
            "size_bytes": 10,
            "storage_path": storage_path,
            "created_at": NOW,
            "page_type": "startup_ad",
            "page_seq": 1,
            "captured_at": NOW,
            "url": "u",
        }
    ]
    store = FakeEvidenceStore()
    frames = [b"frame-zero", b"frame-one"]
    store.payloads[storage_path] = json.dumps(
        {
            "sequence": True,
            "frames": [
                {
                    "index": index,
                    "encoding": "base64",
                    "data": base64.b64encode(frame).decode(),
                }
                for index, frame in enumerate(frames)
            ],
        }
    ).encode()
    service = TaskInsightService(
        repository=repository,
        evidence_store_factory=lambda: store,
    )

    _, archive_content = await service.screenshots_archive("task-1")

    with zipfile.ZipFile(io.BytesIO(archive_content)) as archive:
        names = archive.namelist()
        assert names == [
            "01_startup_ad_art-sequ_frame-00.png",
            "01_startup_ad_art-sequ_frame-01.png",
        ]
        assert [archive.read(name) for name in names] == frames


async def test_missing_task_raises_not_found() -> None:
    repository = FakeInsightRepository()
    repository.context = None
    service = _service(repository)
    try:
        await service.page_timeline("nope")
    except TaskInsightNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("缺失任务必须抛 TaskInsightNotFoundError")


async def test_logs_pagination_and_export() -> None:
    repository = FakeInsightRepository()
    repository.logs = [
        {
            "id": index,
            "ts": NOW,
            "level": "INFO" if index % 2 == 0 else "ERROR",
            "source": "observer",
            "message": f"消息{index}",
            "page_capture_id": None,
            "rule_execution_id": None,
            "context": {},
        }
        for index in range(5)
    ]
    service = _service(repository)
    page = await service.logs("task-1", page=2, page_size=2)
    assert page["total"] == 5
    assert [item["id"] for item in page["items"]] == [2, 3]
    errors = await service.logs("task-1", level="ERROR")
    assert {item["level"] for item in errors["items"]} == {"ERROR"}
    text = await service.logs_export_text("task-1")
    assert "消息0" in text and text.endswith("\n")
