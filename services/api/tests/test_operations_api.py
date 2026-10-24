"""运营总览 / 使用分析接口：数据范围收口、权限、时间窗与比率口径。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import operations as operations_module
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
HQ_TENANT = "00000000-0000-0000-0000-000000000001"
BRANCH_TENANT = "00000000-0000-0000-0000-00000000000a"


@pytest.fixture(autouse=True)
def freeze_operations_clock(monkeypatch):
    """接口测试固定统计窗口，避免真实日期推进后将本期误当上期。"""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(operations_module, "datetime", FrozenDateTime)


@dataclass
class FakeOperationsRepository:
    calls: list[dict] = field(default_factory=list)

    async def summarize_window(self, *, start, end, tenant_id, platform):
        self.calls.append({"method": "summarize", "start": start, "end": end, "tenant_id": tenant_id, "platform": platform})
        # 上期窗口的终点就是本期起点，一定早于「现在」；本期窗口终点是当下。
        if end < NOW:
            return {"task_count": 4, "report_count": 3, "failed_count": 1, "active_account_count": 2}
        return {
            "task_count": 12,
            "report_count": 9,
            "done_count": 5,
            "review_count": 4,
            "failed_count": 1,
            "canceled_count": 1,
            "active_count": 1,
            "active_tenant_count": 2,
            "active_account_count": 5,
            "avg_duration_seconds": 231.7,
        }

    async def daily_trend(self, *, start, end, tenant_id, platform):
        second_day = (start + timedelta(days=1)).strftime("%Y-%m-%d")
        return [{"day": second_day, "task_count": 9, "report_count": 7, "failed_count": 1}]

    async def platform_share(self, *, start, end, tenant_id, platform):
        return [{"platform": "h5", "task_count": 12}]

    async def tenant_rows(self, *, start, end, tenant_id, platform):
        return [
            {
                "tenant_id": HQ_TENANT,
                "tenant_name": "体验中心",
                "status": "enabled",
                "product_count": 95,
                "task_count": 11,
                "report_count": 9,
                "failed_count": 1,
                "last_task_at": NOW,
                "active_account_count": 2,
                "last_login_at": NOW,
            },
            {
                "tenant_id": BRANCH_TENANT,
                "tenant_name": "北京分公司",
                "status": "enabled",
                "product_count": 3,
                "task_count": 0,
                "report_count": 0,
                "failed_count": 0,
                "last_task_at": None,
                "active_account_count": 0,
                "last_login_at": datetime(2026, 6, 1, tzinfo=UTC),
            },
        ]


def _user(user_id: str, role: str, tenant_id: str, permissions: list[str]):
    return auth_module.AuthUserResponse(
        id=user_id,
        username=user_id,
        full_name=f"用户 {user_id}",
        role=role,
        status="enabled",
        permissions=permissions,
        tenant_id=tenant_id,
        tenant_name="北京分公司",
    )


HQ_ADMIN = _user("hq", "平台管理员", HQ_TENANT, ["dashboard", "analytics", "taskObjects:read:any"])
HQ_WITHOUT_ANALYTICS = _user("hq2", "平台管理员", HQ_TENANT, ["dashboard", "taskObjects:read:any"])
COMPANY_ADMIN = _user("branch", "公司管理员", BRANCH_TENANT, ["dashboard", "taskObjects:read:tenant"])


def _client(user, repository: FakeOperationsRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[auth_module.require_auth_user] = lambda: user
    app.dependency_overrides[operations_module._get_operations_repository] = lambda: repository
    return TestClient(app)


def test_overview_forces_company_scope_for_company_admin() -> None:
    repo = FakeOperationsRepository()
    client = _client(COMPANY_ADMIN, repo)
    response = client.get("/api/v1/operations/overview", params={"range": "7d", "tenant_id": HQ_TENANT, "platform": "h5"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == BRANCH_TENANT
    assert all(call["tenant_id"] == BRANCH_TENANT for call in repo.calls)
    assert all(call["platform"] == "h5" for call in repo.calls)
    assert body["summary"]["task_count"] == 12
    assert body["summary"]["report_success_rate"] == 90.0
    assert body["summary"]["failure_rate"] == 8.3
    assert body["previous"]["task_count"] == 4
    # 7 天窗口铺满每一天，缺数据的日期补 0
    assert len(body["trend"]) == 8
    assert body["trend"][0]["task_count"] == 0
    assert body["trend"][1]["task_count"] == 9 and body["trend"][1]["report_count"] == 7
    assert body["platform_share"] == [{"platform": "h5", "task_count": 12}]


def test_overview_tenant_rows_mark_idle_companies() -> None:
    client = _client(HQ_ADMIN, FakeOperationsRepository())
    body = client.get("/api/v1/operations/overview", params={"range": "30d"}).json()
    assert body["tenant_id"] is None
    rows = {row["tenant_name"]: row for row in body["tenants"]}
    assert rows["体验中心"]["usage_status"] == "active"
    assert rows["体验中心"]["report_success_rate"] == 90.0
    assert rows["北京分公司"]["usage_status"] == "idle"
    assert rows["北京分公司"]["report_success_rate"] is None


def test_analytics_only_for_operations_center() -> None:
    assert _client(COMPANY_ADMIN, FakeOperationsRepository()).get("/api/v1/operations/analytics").status_code == 403
    assert _client(HQ_WITHOUT_ANALYTICS, FakeOperationsRepository()).get("/api/v1/operations/analytics").status_code == 403
    ok = _client(HQ_ADMIN, FakeOperationsRepository()).get("/api/v1/operations/analytics", params={"range": "90d"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["range"] == "90d"


def test_window_helper_covers_quarter_and_year() -> None:
    start, end, prev_start, prev_end = operations_module._window("quarter", now=NOW)
    assert start == datetime(2026, 7, 1, tzinfo=UTC)
    assert end == NOW
    assert prev_end == start
    assert (start - prev_start) == (end - start)

    year_start, _, _, _ = operations_module._window("year", now=NOW)
    assert year_start == datetime(2026, 1, 1, tzinfo=UTC)

    week_start, _, _, _ = operations_module._window("7d", now=NOW)
    assert week_start == datetime(2026, 9, 1, tzinfo=UTC)
