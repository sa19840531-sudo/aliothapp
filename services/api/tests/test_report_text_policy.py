"""App 报告文案策略：禁止灰产/黑产字样。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.api.app.application.report_generation import ReportGenerationService
from services.api.app.application.report_text_policy import (
    is_app_report_target,
    maybe_scrub_app_report_payload,
    scrub_app_report_text,
)
from services.api.app.application.task_query import (
    RuleExecutionCompleteness,
    RuleExecutionSummary,
    TaskDetail,
)


def test_scrub_app_report_text_removes_simplified_and_traditional_labels() -> None:
    assert scrub_app_report_text("疑似灰产广告") == "疑似广告"
    assert scrub_app_report_text("涉及黑产推广") == "涉及推广"
    assert scrub_app_report_text("灰產與黑產風險") == "與風險"
    assert scrub_app_report_text(None) is None
    assert scrub_app_report_text("") == ""


def test_scrub_keeps_unrelated_text() -> None:
    text = "开屏广告关闭按钮不可点击"
    assert scrub_app_report_text(text) == text


def test_is_app_report_target_by_platform_and_product() -> None:
    assert is_app_report_target(target_platform="app") is True
    assert is_app_report_target(product_code="app-qc") is True
    assert is_app_report_target(
        task_config={"_task_target": {"platform": "app"}}
    ) is True
    assert is_app_report_target(target_platform="h5") is False


def test_maybe_scrub_only_applies_to_app() -> None:
    payload = {
        "rule_results": [{"result_summary": "判定为灰产", "problem_desc": "黑产引流"}]
    }
    h5 = maybe_scrub_app_report_payload(payload, target_platform="h5")
    assert h5["rule_results"][0]["result_summary"] == "判定为灰产"
    app = maybe_scrub_app_report_payload(payload, target_platform="app")
    assert "灰产" not in app["rule_results"][0]["result_summary"]
    assert "黑产" not in app["rule_results"][0]["problem_desc"]


class _Query:
    def __init__(self, task: TaskDetail, executions: list[RuleExecutionSummary]) -> None:
        self.task = task
        self.executions = executions

    async def get_task_detail(self, task_id: str) -> TaskDetail | None:
        return self.task

    async def list_rule_executions(
        self, task_id: str, *, agent_run_id: str | None = None
    ) -> list[RuleExecutionSummary]:
        return self.executions

    async def list_evidence_artifacts(
        self, task_id: str, *, agent_run_id: str | None = None
    ) -> list:
        return []

    async def get_rule_execution_completeness(
        self, *, task_id: str, agent_run_id: str
    ) -> RuleExecutionCompleteness:
        return RuleExecutionCompleteness(
            candidate_query_count=1,
            candidate_rule_count=1,
            completed_candidate_count=1,
            unresolved_rule_codes=(),
        )

    async def list_task_target_page_coverage(
        self, *, task_id: str, agent_run_id: str | None = None
    ) -> list:
        return []


@pytest.mark.asyncio
async def test_app_report_generation_scrubs_forbidden_labels() -> None:
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    task = TaskDetail(
        task_id="task-app-1",
        target_url=None,
        ruleset_id="rs-1",
        ruleset_code="app",
        ruleset_version=1,
        channel="api",
        status="done",
        priority=1,
        run_id="run-1",
        created_at=now,
        started_at=now,
        finished_at=now,
        last_error=None,
        target_platform="app",
    )
    executions = [
        RuleExecutionSummary(
            execution_id="ex-1",
            rule_id="rule-1",
            rule_code="alioth_1",
            rule_name="广告检查",
            page_capture_id="cap-1",
            execution_status="fail",
            diagnostic_codes=(),
            missing_evidence=(),
            judge_strategy="semantic",
            confidence=0.9,
            severity="high",
            result_summary="页面存在灰产推广痕迹",
            problem_desc="疑似黑产引流入口",
            review_status="open",
            source="agent",
            started_at=now,
            finished_at=now,
        )
    ]
    service = ReportGenerationService(query_service=_Query(task, executions), clock=lambda: now)
    report = await service.generate_task_report("task-app-1", agent_run_id="run-1")
    blob = str(report)
    assert "灰产" not in blob
    assert "黑产" not in blob
    assert "灰產" not in blob
    assert "黑產" not in blob
    assert report["rule_results"][0]["result_summary"] == "页面存在推广痕迹"
    assert report["rule_results"][0]["problem_desc"] == "疑似引流入口"
