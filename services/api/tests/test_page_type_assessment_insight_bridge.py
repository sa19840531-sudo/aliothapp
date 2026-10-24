"""Run 工作内存中的页面类型评估必须贯通任务洞察与报告。"""

from datetime import UTC, datetime
from typing import Any

from services.api.app.application.report_insight import ReportInsightService
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.domain.page.catalog import load_default_page_type_catalog


class _Repository:
    def __init__(self) -> None:
        now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        assessments = _assessment_payloads()
        self.context: dict[str, Any] = {
            "task_id": "task-1",
            "status": "done",
            "target_url": "https://example.test",
            "channel": "workbench",
            "created_at": now,
            "started_at": now,
            "finished_at": now,
            "last_error": None,
            "task_config": {"task_name": "assessment-bridge", "journey_steps": []},
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "run_id": "run-1",
            "run_status": "completed",
            "stop_reason": "completed",
            "working_memory": {
                "page_type_assessments": assessments,
                "page_type_assessment_scope_complete": True,
                "page_type_assessment_scope_page_types": [
                    item["page_type"] for item in assessments
                ],
            },
        }
        self.pages = [
            {
                "page_capture_id": "cap-home",
                "artifact_ids": ["artifact-home"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ]

    async def fetch_task_context(self, task_id: str) -> dict[str, Any] | None:
        return self.context

    async def list_page_nodes(self, *, agent_run_id: str) -> list[dict[str, Any]]:
        return []

    async def list_page_capture_inspection_rows(
        self, *, task_id: str, agent_run_id: str | None
    ) -> list[dict[str, Any]]:
        return self.pages

    async def list_exploration_actions(self, *, agent_run_id: str | None) -> list[dict[str, Any]]:
        return [
            {
                "exploration_action_id": "home-to-activity",
                "origin": "discovered",
                "source_page_type": "home",
                "expected_next_page_type": "activity",
                "action": "click",
                "status": "explored",
                "risk_level": "low",
            }
        ]

    async def count_rules_by_page_type(self, *, task_id: str) -> dict[str, int]:
        return {}

    async def list_rule_execution_rows(
        self, *, task_id: str, agent_run_id: str | None
    ) -> list[dict[str, Any]]:
        return []

    async def count_recognized_pages(self, *, task_id: str, agent_run_id: str | None) -> int:
        return 1


async def test_task_timeline_uses_run_page_type_assessments() -> None:
    timeline = await TaskInsightService(repository=_Repository()).page_timeline("task-1")

    product = timeline["exploration_summary"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["assessment_scope_verified"] is True
    assert product["covered_page_types"] == ["home"]
    assert product["covered_count"] == product["applicable_count"] == 1
    assert product["coverage_rate"] == 1.0


async def test_report_insight_uses_run_page_type_assessments() -> None:
    insight = await ReportInsightService(repository=_Repository()).report_insight("task-1")

    product = insight["page_exploration"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["assessment_scope_verified"] is True
    assert product["covered_page_types"] == ["home"]
    assert product["covered_count"] == product["applicable_count"] == 1
    assert product["coverage_rate"] == 1.0


async def test_task_timeline_without_frozen_scope_keeps_all_inapplicable_rate_unknown() -> None:
    """无冻结范围时动态目标只有不适用的活动页，零分母不得显示全覆盖。"""
    repository = _Repository()
    repository.context["working_memory"].pop("page_type_assessment_scope_page_types")

    timeline = await TaskInsightService(repository=repository).page_timeline("task-1")

    product = timeline["exploration_summary"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["assessment_scope_verified"] is True
    assert product["not_applicable_page_types"] == ["activity"]
    assert product["covered_page_types"] == product["applicable_page_types"] == []
    assert product["covered_count"] == product["applicable_count"] == 0
    assert product["coverage_rate"] is None


async def test_report_without_frozen_scope_keeps_all_inapplicable_rate_unknown() -> None:
    """报告保持与任务洞察一致的零分母口径。"""
    repository = _Repository()
    repository.context["working_memory"].pop("page_type_assessment_scope_page_types")

    insight = await ReportInsightService(repository=repository).report_insight("task-1")

    product = insight["page_exploration"]["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["assessment_scope_verified"] is True
    assert product["not_applicable_page_types"] == ["activity"]
    assert product["covered_page_types"] == product["applicable_page_types"] == []
    assert product["covered_count"] == product["applicable_count"] == 0
    assert product["coverage_rate"] is None


def _assessment_payloads() -> list[dict[str, object]]:
    business_types = [
        page_type
        for page_type in load_default_page_type_catalog().codes
        if page_type not in {"line_loading", "startup_ad"}
    ]
    return [
        {
            "page_type": page_type,
            "status": "covered" if page_type == "home" else "not_applicable",
            "reason_code": (
                "recognized_with_evidence"
                if page_type == "home"
                else "no_product_entry_in_full_observation"
            ),
            "page_capture_ids": ["cap-home"],
            "evidence_artifact_ids": ["artifact-home"],
            "exploration_action_ids": [],
        }
        for page_type in business_types
    ]
