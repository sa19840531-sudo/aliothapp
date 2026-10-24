"""App V2 全检报告沿用列表权威可用性，并在服务端阻止预览导出。"""

import base64
import json
from typing import Any

import pytest

from services.api.app.application.report_insight import (
    ReportExportBlockedError,
    ReportInsightService,
)


class AppReportRepository:
    """提供同一任务上下文和读取事实，避免依赖旧报告测试的默认状态。"""

    def __init__(self, availability: str | None, *, platform: str = "app") -> None:
        self.context: dict[str, Any] = {
            "task_id": "task-app",
            "target_platform": platform,
            "target_url": "app://com.example.inspection",
            "status": "review",
            "run_id": "run-current",
            "task_config": {
                "task_name": "Android 全检任务",
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
            },
            # 历史宽泛封存标记即使存在，也不能覆盖当前 Run 的权威结果。
            "finalization_state": "sealed",
            "sealed_report_bound": True,
            "completion_gate_passed": False,
        }
        if availability is not None:
            self.context["app_report_availability"] = availability
        self.rows = [{
            "execution_id": "execution-current",
            "rule_id": "rule-current",
            "rule_code": "APP-001",
            "rule_name": "页面规则",
            "execution_status": "pass",
            "execution_reason": None,
            "is_bootstrap": False,
        }]
        self.screenshots: list[dict[str, Any]] = []
        self.screenshot_queries: list[dict[str, Any]] = []

    async def fetch_task_context(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        assert task_id == "task-app"
        return self.context

    async def list_rule_execution_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.rows

    async def count_recognized_pages(self, **kwargs: Any) -> int:
        return 0

    async def list_page_capture_inspection_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def list_exploration_actions(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def list_screenshot_artifacts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.screenshot_queries.append(kwargs)
        return self.screenshots


def service(repository: AppReportRepository) -> ReportInsightService:
    return ReportInsightService(repository=repository, evidence_store_factory=object)


@pytest.mark.parametrize("platform", ["app", "android"])
async def test_current_app_business_results_remain_preview_without_formal_binding(
    platform: str,
) -> None:
    repository = AppReportRepository("preview", platform=platform)
    insight = await service(repository).report_insight("task-app")

    assert insight["report_availability"] == "preview"
    assert insight["summary"]["report_stage"] == "final_review"
    assert insight["summary"]["total_rules"] == 1
    assert insight["pass_rate"] == 100
    assert insight["is_final"] is False
    assert insight["export_ready"] is False
    assert insight["export_allowed"] is False
    assert insight["incomplete_reasons"] == ["APP_REPORT_NOT_FINALIZED"]


@pytest.mark.parametrize("availability", ["none", None, "unknown"])
async def test_no_current_app_report_facts_never_fall_back_to_old_sealed_flags(
    availability: str | None,
) -> None:
    repository = AppReportRepository(availability)
    insight = await service(repository).report_insight("task-app")

    assert insight["report_availability"] == "none"
    assert insight["summary"]["report_stage"] == "final_review"
    assert insight["is_final"] is False
    assert insight["export_ready"] is False
    assert insight["export_allowed"] is False
    assert insight["report_unavailable_reason"] == "APP_REPORT_NO_BUSINESS_RESULTS"
    assert insight["incomplete_reasons"] == ["APP_REPORT_NO_BUSINESS_RESULTS"]


@pytest.mark.parametrize("availability", ["preview", "none", None])
@pytest.mark.parametrize("default_execution_mode", ["explicit", "missing", "null"])
async def test_app_preview_export_is_blocked_on_server_even_without_pending_confirmations(
    availability: str | None,
    default_execution_mode: str,
) -> None:
    repository = AppReportRepository(availability)
    if default_execution_mode == "missing":
        repository.context["task_config"].pop("execution_mode")
    elif default_execution_mode == "null":
        repository.context["task_config"]["execution_mode"] = None
    with pytest.raises(ReportExportBlockedError, match="尚未通过门禁并封存"):
        await service(repository).export_html("task-app")


async def test_current_app_final_binding_allows_formal_report_and_export() -> None:
    repository = AppReportRepository("final")
    repository.context["completion_gate_passed"] = True
    report_service = service(repository)
    insight = await report_service.report_insight("task-app")

    assert insight["report_availability"] == "final"
    assert insight["summary"]["report_stage"] == "final"
    assert insight["is_final"] is True
    assert insight["export_ready"] is True
    assert insight["export_allowed"] is True
    assert insight["incomplete_reasons"] == []
    assert insight["report_unavailable_reason"] is None
    filename, html_text = await report_service.export_html("task-app")
    assert filename == "task_task-app_report.html"
    assert "Android 全检任务" in html_text


async def test_app_final_export_keeps_current_run_screenshot_evidence() -> None:
    repository = AppReportRepository("final")
    repository.context["completion_gate_passed"] = True
    repository.rows[0].update(
        execution_status="fail",
        evidence_artifact_ids=["screenshot-current"],
    )
    repository.screenshots = [{
        "artifact_id": "screenshot-current",
        "content_type": "image/png",
        "storage_path": "task-app/run-current/evidence.png",
    }]
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGN06TjDwMDAxAAG"
        "ABLCAZytuU1zAAAAAElFTkSuQmCC"
    )
    reads = []

    class ScreenshotStore:
        async def read(self, storage_path: str) -> bytes:
            reads.append(storage_path)
            return png

    report_service = ReportInsightService(
        repository=repository, evidence_store_factory=ScreenshotStore,
    )
    _, html_text = await report_service.export_html("task-app")
    start = html_text.index('id="__edit_data__"')
    encoded = html_text[html_text.index(">", start) + 1:html_text.index("</div>", start)]
    issue = json.loads(base64.b64decode(encoded).decode("utf-8"))["issues"][0]

    assert repository.screenshot_queries == [{
        "task_id": "task-app", "agent_run_id": "run-current",
    }]
    assert reads == ["task-app/run-current/evidence.png"]
    assert issue["images"] == [f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"]
    assert "已内嵌 1 张截图" in issue["content"]
    assert "冻结报告未提供该材料" not in issue["content"]


async def test_running_app_preview_stays_in_preview_stage() -> None:
    repository = AppReportRepository("preview")
    repository.context["status"] = "running"
    insight = await service(repository).report_insight("task-app")

    assert insight["summary"]["report_stage"] == "preview"
    assert insight["report_availability"] == "preview"
    assert insight["export_allowed"] is False


@pytest.mark.parametrize(
    "task_config",
    [
        {"workflow_version": "v1", "execution_mode": "full_inspection"},
        {"workflow_version": "v2", "execution_mode": "exploration"},
        {},
    ],
)
async def test_legacy_app_reports_keep_existing_state_and_export_behavior(
    task_config: dict[str, str],
) -> None:
    repository = AppReportRepository("none")
    repository.context["task_config"] = task_config
    report_service = service(repository)
    insight = await report_service.report_insight("task-app")

    assert "report_availability" not in insight
    assert "is_final" not in insight
    assert "export_allowed" not in insight
    assert insight["summary"]["report_stage"] == "final"
    assert insight["export_ready"] is True
    filename, _ = await report_service.export_html("task-app")
    assert filename == "task_task-app_report.html"
