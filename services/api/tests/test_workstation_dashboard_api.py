from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import dashboard as workstation_dashboard_module
from services.api.app.infrastructure.db.workstation_dashboard_repository import (
    _build_today_metrics,
)
from services.api.app.main import create_app


@dataclass
class FakeWorkstationDashboardService:
    async def get_overview(self, *, access_scope) -> dict:
        return {
            "summary_cards": [{"key": "total_tasks", "label": "任务总数", "value": 12}],
            "today_metrics": {
                "executed_tasks_today": 5,
                "executed_tasks_yesterday": 3,
                "ignored_rules_today": 4,
                "abnormal_rules_today": 1,
                "qc_success_rate_today": 87.5,
                "qc_success_rate_yesterday": 90.0,
            },
            "recent_tasks": [{"task_id": "task-1", "task_name": "首页改版回归测试"}],
            "status_distribution": [{"status": "running", "count": 2}],
            "risk_distribution": [{"severity": "high", "count": 1}],
            "execution_status_distribution": [
                {"execution_status": "pass", "count": 8},
                {"execution_status": "manual_review", "count": 2},
            ],
            "latest_rule_executions": [{"execution_id": "execution-1", "rule_name": "规则"}],
        }


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[workstation_dashboard_module._get_workstation_dashboard_service] = (
        lambda: FakeWorkstationDashboardService()
    )
    return TestClient(app)


def test_get_workstation_dashboard_overview_returns_aggregation(client: TestClient) -> None:
    response = client.get("/api/v1/workstation/dashboard/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["summary_cards"][0]["key"] == "total_tasks"
    assert body["recent_tasks"][0]["task_id"] == "task-1"
    assert body["status_distribution"][0]["status"] == "running"
    assert body["today_metrics"]["executed_tasks_today"] == 5
    assert body["today_metrics"]["qc_success_rate_today"] == 87.5
    assert body["execution_status_distribution"][0]["execution_status"] == "pass"


def test_build_today_metrics_maps_statuses_by_card_definition() -> None:
    metrics = _build_today_metrics(
        executed_tasks_today=6,
        executed_tasks_yesterday=4,
        daily_execution_rows=[
            {"is_today": True, "execution_status": "pass", "count": 10},
            {"is_today": True, "execution_status": "fail", "count": 2},
            {"is_today": True, "execution_status": "warning", "count": 2},
            {"is_today": True, "execution_status": "skipped", "count": 4},
            {"is_today": True, "execution_status": "error", "count": 3},
            {"is_today": True, "execution_status": "manual_review", "count": 1},
            {"is_today": True, "execution_status": "coverage_gap", "count": 1},
            {"is_today": True, "execution_status": "not_executed", "count": 1},
            {"is_today": False, "execution_status": "pass", "count": 9},
            {"is_today": False, "execution_status": "error", "count": 1},
        ],
    )

    assert metrics["executed_tasks_today"] == 6
    assert metrics["executed_tasks_yesterday"] == 4
    assert metrics["ignored_rules_today"] == 4
    # 异常 = 检测异常 3 + 转人工 1 + 计划外未覆盖 1 + 未执行 1；warning 属未通过不算异常
    assert metrics["abnormal_rules_today"] == 6
    # 今日：AI 已出结果 (10 + 2 + 2 + 4) / 24 = 75.0；未覆盖/未执行/异常不计入成功
    assert metrics["qc_success_rate_today"] == 75.0
    # 昨日：9 / 10 = 90.0
    assert metrics["qc_success_rate_yesterday"] == 90.0


def test_build_today_metrics_handles_empty_days() -> None:
    metrics = _build_today_metrics(
        executed_tasks_today=0,
        executed_tasks_yesterday=0,
        daily_execution_rows=[],
    )

    assert metrics["ignored_rules_today"] == 0
    assert metrics["abnormal_rules_today"] == 0
    assert metrics["qc_success_rate_today"] is None
    assert metrics["qc_success_rate_yesterday"] is None
