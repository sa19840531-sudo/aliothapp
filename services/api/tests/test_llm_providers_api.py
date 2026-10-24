"""AI 服务与供应商接口：环境供应商同步、健康判定、主备切换记录、连接测试、设置。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import llm_providers as providers_module
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)


@dataclass
class FakeProviderRepository:
    providers: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def _row(self, payload: dict) -> dict:
        return {
            "id": f"prov-{len(self.providers) + 1}",
            "base_url": None,
            "model": None,
            "priority": 1,
            "weight": 0,
            "breaker_rule": None,
            "status": "standby",
            "is_primary": False,
            "api_key_configured": False,
            "managed_by_env": False,
            "quota_remaining_percent": None,
            "last_check_status": None,
            "last_check_latency_ms": None,
            "last_check_message": None,
            "last_checked_at": None,
            "created_at": NOW,
            "updated_at": NOW,
            **payload,
        }

    async def list_providers(self):
        return [dict(row) for row in self.providers]

    async def find_by_id(self, provider_id):
        return next((dict(row) for row in self.providers if row["id"] == provider_id), None)

    async def find_by_code(self, code):
        return next((dict(row) for row in self.providers if row["code"] == code), None)

    async def create_provider(self, payload):
        row = self._row(payload)
        self.providers.append(row)
        return dict(row)

    async def update_provider(self, provider_id, payload):
        for row in self.providers:
            if row["id"] == provider_id:
                row.update(payload)
                return dict(row)
        return None

    async def sync_env_provider(self, payload):
        existing = await self.find_by_code(payload["code"])
        if existing is None:
            row = self._row({**payload, "status": "active", "is_primary": True, "managed_by_env": True, "weight": 100})
            self.providers.append(row)
            return dict(row)
        for row in self.providers:
            if row["code"] == payload["code"]:
                row.update(base_url=payload.get("base_url"), model=payload.get("model"), api_key_configured=payload.get("api_key_configured"))
                return dict(row)
        return existing

    async def set_primary(self, provider_id):
        target = None
        for row in self.providers:
            if row["id"] == provider_id:
                row.update(is_primary=True, status="active")
                target = dict(row)
            elif row["is_primary"]:
                row.update(is_primary=False, status="standby")
        return target

    async def record_check(self, provider_id, *, status, latency_ms, message):
        self.checks.append({"provider_id": provider_id, "status": status, "latency_ms": latency_ms, "message": message})
        for row in self.providers:
            if row["id"] == provider_id:
                row.update(last_check_status=status, last_check_latency_ms=latency_ms, last_check_message=message, last_checked_at=NOW)
                return dict(row)
        return None

    async def record_event(self, payload):
        row = {"id": f"evt-{len(self.events) + 1}", "occurred_at": NOW, **payload}
        self.events.append(row)
        return dict(row)

    async def list_events(self, *, limit=20):
        return [dict(row) for row in reversed(self.events)][:limit]

    async def usage_metrics(self, *, hours=24):
        return dict(self.usage)

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value, *, updated_by):
        self.settings[key] = dict(value)


def _user(permissions: list[str]):
    return auth_module.AuthUserResponse(
        id="hq",
        username="hq",
        full_name="运营中心",
        role="平台管理员",
        status="enabled",
        permissions=permissions,
        tenant_id="00000000-0000-0000-0000-000000000001",
        tenant_name="体验中心",
    )


OPS_ADMIN = _user(["dashboard", "systemOperations"])
NO_PERMISSION = _user(["dashboard"])


def _client(user, repository: FakeProviderRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[auth_module.require_auth_user] = lambda: user
    app.dependency_overrides[providers_module._get_provider_repository] = lambda: repository
    return TestClient(app)


@pytest.fixture
def env_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIOTH_LLM_PROVIDER", "custom")
    monkeypatch.setenv("ALIOTH_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("ALIOTH_LLM_MODEL", "grok-4.5")
    monkeypatch.setenv("ALIOTH_LLM_API_KEY", "test-key")


def test_overview_syncs_env_provider_and_flags_error_rate(env_provider) -> None:
    repo = FakeProviderRepository(usage={"custom": {"call_count": 100, "failed_count": 10, "total_tokens": 5000, "last_call_at": NOW}})
    response = _client(OPS_ADMIN, repo).get("/api/v1/llm-providers")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["providers"]) == 1
    provider = body["providers"][0]
    assert provider["code"] == "env:custom"
    assert provider["name"] == "custom · api.example.com"
    assert provider["managed_by_env"] is True and provider["api_key_configured"] is True
    assert provider["metrics"]["error_rate"] == 10.0
    assert provider["health"] == "warning"
    assert body["monitor_status"] == "warning"
    assert body["warnings"] and "错误率" in body["warnings"][0]
    assert body["settings"]["effective_provider_code"] == "env:custom"
    assert "环境变量" in body["settings"]["note"]


def test_set_primary_records_audit_and_rejects_disabled(env_provider) -> None:
    repo = FakeProviderRepository()
    client = _client(OPS_ADMIN, repo)
    client.get("/api/v1/llm-providers")
    created = client.post(
        "/api/v1/llm-providers",
        json={"name": "供应商 B", "base_url": "https://b.example.com/v1", "model": "gpt", "priority": 2, "weight": 10},
    )
    assert created.status_code == 201, created.text
    backup_id = created.json()["id"]

    switched = client.post(f"/api/v1/llm-providers/{backup_id}/primary")
    assert switched.status_code == 200, switched.text
    assert switched.json()["is_primary"] is True
    env_row = next(row for row in repo.providers if row["code"] == "env:custom")
    assert env_row["is_primary"] is False
    switch_event = next(event for event in repo.events if event["event_type"] == "switch")
    assert "custom · api.example.com 切换为 供应商 B" in switch_event["action"]
    assert "接入路由后生效" in switch_event["result"]

    client.put(
        f"/api/v1/llm-providers/{backup_id}",
        json={"name": "供应商 B", "priority": 2, "weight": 10, "status": "disabled"},
    )
    assert client.post(f"/api/v1/llm-providers/{backup_id}/primary").status_code == 409


def test_connection_test_persists_result(env_provider, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeProviderRepository()
    client = _client(OPS_ADMIN, repo)
    provider_id = client.get("/api/v1/llm-providers").json()["providers"][0]["id"]

    async def fake_test(provider):
        assert provider["id"] == provider_id
        return "ok", 321, "连接正常（HTTP 200）"

    monkeypatch.setattr(providers_module, "perform_connection_test", fake_test)
    response = client.post(f"/api/v1/llm-providers/{provider_id}/connection-test")
    assert response.status_code == 200, response.text
    assert response.json()["latency_ms"] == 321
    assert response.json()["provider"]["last_check_status"] == "ok"
    assert repo.checks == [{"provider_id": provider_id, "status": "ok", "latency_ms": 321, "message": "连接正常（HTTP 200）"}]


def test_settings_roundtrip_and_permission(env_provider) -> None:
    repo = FakeProviderRepository()
    client = _client(OPS_ADMIN, repo)
    saved = client.put(
        "/api/v1/llm-providers/settings",
        json={"routing": {"auto_failover": True}, "alert_policy": {"service_down": True, "quota_low": False, "error_rate": True, "latency": False}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["routing"]["auto_failover"] is True
    assert repo.settings["llm_alert_policy"]["quota_low"] is False
    overview = client.get("/api/v1/llm-providers").json()
    assert overview["settings"]["alert_policy"]["latency"] is False

    assert _client(NO_PERMISSION, repo).get("/api/v1/llm-providers").status_code == 403
