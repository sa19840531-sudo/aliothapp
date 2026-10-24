"""报告中心新端点（insight / confirmations / export / 列表增强）的路由测试。"""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import reports as reports_module
from services.api.app.application.report_insight import (
    ReportConfirmationError,
    ReportExportBlockedError,
    ReportInsightNotFoundError,
)
from services.api.app.main import create_app

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


class FakeReportInsightService:
    def __init__(self) -> None:
        self.missing = False
        self.export_blocked = False
        self.confirm_calls: list[tuple[str, str, str, str | None]] = []

    async def report_insight(self, task_id: str, **_kwargs) -> dict:
        if self.missing:
            raise ReportInsightNotFoundError(f"未找到任务：{task_id}")
        return {
            "task_id": task_id,
            "summary": {"task_name": "示例", "total_rules": 3},
            "pass_rate": 66.7,
            "distribution": [],
            "confirm_progress": {"total": 1, "confirmed": 0, "pending": 1, "completed": False},
            "export_ready": False,
            "confirm_items": [],
            "rule_items": [],
        }

    async def confirm_rule(
        self,
        task_id: str,
        execution_id: str,
        *,
        verdict: str,
        note: str | None = None,
        operator: str | None = None,
        **_kwargs,
    ) -> dict:
        if self.missing:
            raise ReportInsightNotFoundError("未找到")
        if execution_id == "exec-pass":
            raise ReportConfirmationError("该规则无需人工确认")
        self.confirm_calls.append((task_id, execution_id, verdict, note))
        return {
            "execution_id": execution_id,
            "human_verdict": verdict,
            "human_verdict_label": "通过" if verdict == "pass" else verdict,
            "human_verdict_note": note,
            "human_verdict_at": NOW,
            "review_status": "approved",
            "rule_code": "alioth_5",
        }

    async def export_html(self, task_id: str, **_kwargs) -> tuple[str, str]:
        if self.missing:
            raise ReportInsightNotFoundError("未找到")
        if self.export_blocked:
            raise ReportExportBlockedError("仍有 2 条规则待人工确认，暂不能导出报告")
        return (
            f"task_{task_id}_report.html",
            "<!DOCTYPE html><html><body>质检完成</body></html>",
        )


class FakeInsightRepositoryForReports:
    async def report_list_meta(
        self, task_ids: list[str], **_kwargs
    ) -> dict[str, dict[str, Any]]:
        return {
            task_id: {
                "generated_at": NOW,
                "pending_confirm_count": 2,
            }
            for task_id in task_ids
        }


class FakeWorkstationReportService:
    async def list_reports(self, *, page: int, page_size: int, **_kwargs) -> dict:
        return {
            "items": [
                {
                    "task_id": "task-1",
                    "task_name": "示例任务",
                    "report_status": "ready",
                    "overall_status": "generated",
                    "attention_required": None,
                    "generated_at": "2026-07-01T00:00:00+00:00",
                }
            ],
            "total": 1,
            "page": page,
            "page_size": page_size,
        }

    async def get_report(
        self, task_id: str, *, agent_run_id: str | None = None, **_kwargs
    ) -> dict:
        return {"task": {"task_id": task_id}}


@pytest.fixture()
def client() -> tuple[TestClient, FakeReportInsightService]:
    insight_service = FakeReportInsightService()
    app = create_app()
    app.dependency_overrides[reports_module._get_report_insight_service] = (
        lambda: insight_service
    )
    app.dependency_overrides[reports_module._get_task_insight_repository] = (
        lambda: FakeInsightRepositoryForReports()
    )
    app.dependency_overrides[reports_module._get_workstation_report_service] = (
        lambda: FakeWorkstationReportService()
    )
    return TestClient(app), insight_service


def test_list_reports_enriched_with_confirm_meta(
    client: tuple[TestClient, FakeReportInsightService],
) -> None:
    test_client, _ = client
    response = test_client.get("/api/v1/workstation/reports")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["pending_confirm_count"] == 2
    assert item["generated_at"] == NOW.isoformat()


def test_report_insight_route(client: tuple[TestClient, FakeReportInsightService]) -> None:
    test_client, service = client
    response = test_client.get("/api/v1/workstation/reports/task-1/insight")
    assert response.status_code == 200
    assert response.json()["confirm_progress"]["pending"] == 1
    service.missing = True
    assert test_client.get("/api/v1/workstation/reports/nope/insight").status_code == 404


def test_confirm_route_status_mapping(
    client: tuple[TestClient, FakeReportInsightService],
) -> None:
    test_client, service = client
    ok = test_client.post(
        "/api/v1/workstation/reports/task-1/confirmations/exec-1",
        json={"verdict": "pass", "note": "人工复核通过"},
    )
    assert ok.status_code == 200
    assert ok.json()["human_verdict"] == "pass"
    assert service.confirm_calls == [("task-1", "exec-1", "pass", "人工复核通过")]

    out_of_scope = test_client.post(
        "/api/v1/workstation/reports/task-1/confirmations/exec-pass",
        json={"verdict": "pass"},
    )
    assert out_of_scope.status_code == 409

    invalid = test_client.post(
        "/api/v1/workstation/reports/task-1/confirmations/exec-1",
        json={"verdict": "maybe"},
    )
    assert invalid.status_code == 422

    service.missing = True
    missing = test_client.post(
        "/api/v1/workstation/reports/nope/confirmations/exec-1",
        json={"verdict": "pass"},
    )
    assert missing.status_code == 404


def test_export_route_headers_and_gate(
    client: tuple[TestClient, FakeReportInsightService],
) -> None:
    test_client, service = client
    response = test_client.get("/api/v1/workstation/reports/task-1/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" in response.headers["content-disposition"]
    assert "质检完成" in response.text

    service.export_blocked = True
    blocked = test_client.get("/api/v1/workstation/reports/task-1/export")
    assert blocked.status_code == 409
    assert "待人工确认" in blocked.json()["error"]["message"]
