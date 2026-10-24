"""部门 Token 消耗与费用接口：预算分级、环比、导出与明细。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.app.api.v1 import cost_reports as cost_module
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
HQ_TENANT = "00000000-0000-0000-0000-000000000001"


@dataclass
class FakeDepartmentCostRepository:
    calls: list[dict] = field(default_factory=list)

    async def month_totals(self, *, start, end, provider, tenant_id):
        self.calls.append({"method": "totals", "start": start, "end": end, "provider": provider})
        if start.month == 9:
            return {"input_tokens": 300, "output_tokens": 100, "total_tokens": 400, "ai_calls": 12, "total_cost": 300.0, "unpriced_calls": 0, "task_count": 3}
        return {"input_tokens": 150, "output_tokens": 50, "total_tokens": 200, "ai_calls": 6, "total_cost": 100.0, "unpriced_calls": 0, "task_count": 1}

    async def department_rows(self, *, start, end, provider, tenant_id):
        return [
            {"tenant_id": HQ_TENANT, "tenant_name": "体验中心", "department_name": "一部", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "ai_calls": 4, "total_cost": 95.0, "main_provider": "custom", "main_model": "grok-4.5", "task_count": 1},
            {"tenant_id": HQ_TENANT, "tenant_name": "体验中心", "department_name": "二部", "input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "ai_calls": 4, "total_cost": 120.0, "main_provider": "custom", "main_model": "grok-4.5", "task_count": 1},
            {"tenant_id": HQ_TENANT, "tenant_name": "体验中心", "department_name": "三部", "input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "ai_calls": 4, "total_cost": 85.0, "main_provider": "custom", "main_model": "grok-4.5", "task_count": 1},
            {"tenant_id": HQ_TENANT, "tenant_name": "体验中心", "department_name": "未填写部门", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "ai_calls": 0, "total_cost": 0.0, "main_provider": None, "main_model": None, "task_count": 0},
        ]

    async def task_rows(self, *, start, end, provider, tenant_id, department_name, limit=200):
        self.calls.append({"method": "tasks", "department_name": department_name, "provider": provider})
        return [
            {"task_id": "task-1", "task_name": "任务一", "department_name": department_name or "一部", "provider": "custom", "model": "grok-4.5", "ai_calls": 4, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "total_cost": 95.0, "first_call_at": NOW, "last_call_at": NOW}
        ]

    async def price_versions(self):
        return [
            {"input_price": 18.0, "output_price": 52.0, "cache_hit_price": 4.0, "pricing_source": "runtime_config_per_call", "pricing_configured": True, "currency": "CNY", "effective_from": datetime(2026, 9, 1, tzinfo=UTC), "last_seen_at": NOW, "event_count": 12},
            {"input_price": 0.0, "output_price": 0.0, "cache_hit_price": 0.0, "pricing_source": "runtime_config_per_call", "pricing_configured": False, "currency": "CNY", "effective_from": datetime(2026, 8, 1, tzinfo=UTC), "last_seen_at": datetime(2026, 8, 30, tzinfo=UTC), "event_count": 6},
        ]

    async def providers(self):
        return ["custom"]


@dataclass
class FakeDepartmentRepository:
    async def list_budgets(self):
        return [
            {"tenant_id": HQ_TENANT, "name": "一部", "monthly_token_budget": 100.0},
            {"tenant_id": HQ_TENANT, "name": "二部", "monthly_token_budget": 100.0},
            {"tenant_id": HQ_TENANT, "name": "三部", "monthly_token_budget": 100.0},
        ]


def _client(repository: FakeDepartmentCostRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[cost_module._get_department_cost_repository] = lambda: repository
    app.dependency_overrides[cost_module._get_department_repository] = FakeDepartmentRepository
    return TestClient(app)


def test_department_report_grades_budgets_and_compares_previous_month() -> None:
    repo = FakeDepartmentCostRepository()
    response = _client(repo).get("/api/v1/cost-reports/departments", params={"month": "2026-09", "provider": "custom"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == "2026-09" and body["month_label"] == "2026 年 9 月"
    assert body["totals"]["total_tokens"] == 400 and body["previous_totals"]["total_tokens"] == 200
    assert body["token_change_percent"] == 100.0
    assert body["cost_change_percent"] == 200.0
    levels = {row["department_name"]: row["budget_level"] for row in body["rows"]}
    assert levels == {"一部": "critical", "二部": "over", "三部": "warning", "未填写部门": "none"}
    assert body["alert_department_count"] == 3
    assert body["over_90_department_count"] == 2
    assert body["rows"][0]["budget_used_percent"] == 95.0
    assert body["current_price"]["input_price"] == 18.0 and body["current_price"]["pricing_configured"] is True
    assert len(body["price_history"]) == 2
    assert body["months"][0] >= "2026-09"
    assert all(call["provider"] == "custom" for call in repo.calls if call["method"] == "totals")


def test_department_report_export_and_detail() -> None:
    repo = FakeDepartmentCostRepository()
    client = _client(repo)
    export = client.get("/api/v1/cost-reports/departments/export", params={"month": "2026-09"})
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("text/csv")
    text = export.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("月份,公司,部门,输入 Token")
    assert "一部" in text and "95.00" in text

    detail = client.get("/api/v1/cost-reports/departments/detail", params={"month": "2026-09", "department_name": "一部"})
    assert detail.status_code == 200, detail.text
    assert detail.json()["items"][0]["task_name"] == "任务一"
    assert any(call["method"] == "tasks" and call["department_name"] == "一部" for call in repo.calls)

    assert client.get("/api/v1/cost-reports/departments", params={"month": "2026/09"}).status_code == 422
