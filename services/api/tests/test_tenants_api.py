from dataclasses import dataclass, field

import asyncpg
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import tenants as tenants_module
from services.api.app.api.v1.auth import hash_password
from services.api.app.infrastructure.db.organization_deletion import OrganizationDeletion
from services.api.app.infrastructure.db.tenant_repository import (
    DEFAULT_TENANT_ID,
    TENANT_KEY_UNIQUE_CONSTRAINT,
    TENANT_NAME_UNIQUE_INDEX,
)
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth

BRANCH_A = "00000000-0000-0000-0000-00000000000a"


@dataclass
class FakeUserRepository:
    users: list[dict]

    async def find_by_username(self, username: str):
        return next((item for item in self.users if item["username"] == username), None)

    async def touch_last_login(self, user_id: str):
        return None


@dataclass
class FakeTenantRepository:
    tenants: list[dict] = field(default_factory=list)

    async def list_tenants(self):
        return [dict(item) for item in self.tenants]

    async def find_by_id(self, tenant_id: str):
        return next((dict(item) for item in self.tenants if item["id"] == tenant_id), None)

    async def find_by_display_name(self, display_name: str):
        return next(
            (
                dict(item)
                for item in self.tenants
                if item["display_name"].strip().lower() == display_name.strip().lower()
            ),
            None,
        )

    async def create_tenant(self, payload):
        if any(item["tenant_key"] == payload["tenant_key"] for item in self.tenants):
            raise _unique_violation(TENANT_KEY_UNIQUE_CONSTRAINT)
        if await self.find_by_display_name(payload["display_name"]) is not None:
            raise _unique_violation(TENANT_NAME_UNIQUE_INDEX)
        item = {
            "id": f"tenant-{len(self.tenants) + 1}",
            "tenant_key": payload["tenant_key"],
            "display_name": payload["display_name"],
            "status": "enabled",
            "user_count": 0,
            "created_at": "2026-09-07T08:00:00+00:00",
            "updated_at": "2026-09-07T08:00:00+00:00",
        }
        self.tenants.append(item)
        return dict(item)

    async def delete_tenant(self, tenant_id: str):
        existing = await self.find_by_id(tenant_id)
        if existing is None:
            return OrganizationDeletion()
        associations = existing.get('associations', {})
        if associations:
            return OrganizationDeletion(associations=associations)
        self.tenants = [item for item in self.tenants if item['id'] != tenant_id]
        return OrganizationDeletion(deleted=True)

    async def update_tenant(self, tenant_id: str, payload):
        for item in self.tenants:
            if item["id"] == tenant_id:
                item["display_name"] = payload["display_name"]
                item["status"] = payload["status"]
                item["updated_at"] = "2026-09-07T09:00:00+00:00"
                return dict(item)
        return None


def _unique_violation(constraint_name: str) -> asyncpg.UniqueViolationError:
    error = asyncpg.UniqueViolationError("重复记录")
    error.constraint_name = constraint_name
    return error


def _tenant(tenant_id: str, key: str, name: str, *, user_count: int = 0) -> dict:
    return {
        "id": tenant_id,
        "tenant_key": key,
        "display_name": name,
        "status": "enabled",
        "user_count": user_count,
        "created_at": "2026-08-18T08:00:00+00:00",
        "updated_at": "2026-08-18T08:00:00+00:00",
    }


def _users() -> FakeUserRepository:
    return FakeUserRepository(
        users=[
            {
                "id": "hq-admin",
                "username": "hq",
                "full_name": "总部管理员",
                "password_hash": hash_password("secret123"),
                "role": "平台管理员",
                "status": "enabled",
                "tenant_id": DEFAULT_TENANT_ID,
            },
            {
                "id": "a-admin",
                "username": "a-admin",
                "full_name": "华东管理员",
                "password_hash": hash_password("secret123"),
                "role": "公司管理员",
                "status": "enabled",
                "tenant_id": BRANCH_A,
            },
        ]
    )


def build_client(tenant_repo: FakeTenantRepository) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    users = _users()
    app.dependency_overrides[auth_module._get_user_repository] = lambda: users
    app.dependency_overrides[tenants_module._get_tenant_repository] = lambda: tenant_repo
    return TestClient(app)


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "secret123"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_hq_admin_lists_creates_and_updates_companies() -> None:
    repo = FakeTenantRepository(
        tenants=[_tenant(DEFAULT_TENANT_ID, "default", "体验中心", user_count=3)]
    )
    client = build_client(repo)
    headers = _login(client, "hq")

    listed = client.get("/api/v1/tenants", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["display_name"] == "体验中心"
    assert listed.json()["items"][0]["is_headquarters"] is True
    assert listed.json()["items"][0]["user_count"] == 3

    created = client.post(
        "/api/v1/tenants", headers=headers, json={"display_name": "  华东分公司 "}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["display_name"] == "华东分公司"
    assert body["status"] == "enabled"
    assert body["is_headquarters"] is False
    # 没填编码时自动生成，运营不用关心。
    assert body["tenant_key"].startswith("co-")

    with_key = client.post(
        "/api/v1/tenants",
        headers=headers,
        json={"display_name": "华南分公司", "tenant_key": "south-china"},
    )
    assert with_key.status_code == 201
    assert with_key.json()["tenant_key"] == "south-china"

    renamed = client.put(
        f"/api/v1/tenants/{body['id']}",
        headers=headers,
        json={"display_name": "华东体验中心", "status": "disabled"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "华东体验中心"
    assert renamed.json()["status"] == "disabled"


def test_company_creation_rejects_duplicate_name_and_bad_key() -> None:
    repo = FakeTenantRepository(
        tenants=[
            _tenant(DEFAULT_TENANT_ID, "default", "体验中心"),
            _tenant(BRANCH_A, "east", "华东分公司"),
        ]
    )
    client = build_client(repo)
    headers = _login(client, "hq")

    duplicate = client.post(
        "/api/v1/tenants", headers=headers, json={"display_name": "华东分公司"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["message"] == "公司名称已存在"

    bad_key = client.post(
        "/api/v1/tenants",
        headers=headers,
        json={"display_name": "华北分公司", "tenant_key": "华北"},
    )
    assert bad_key.status_code == 422

    rename_clash = client.put(
        f"/api/v1/tenants/{BRANCH_A}",
        headers=headers,
        json={"display_name": "体验中心", "status": "enabled"},
    )
    assert rename_clash.status_code == 409

    missing = client.put(
        "/api/v1/tenants/no-such-company",
        headers=headers,
        json={"display_name": "随便", "status": "enabled"},
    )
    assert missing.status_code == 404


def test_headquarters_cannot_be_disabled() -> None:
    repo = FakeTenantRepository(tenants=[_tenant(DEFAULT_TENANT_ID, "default", "体验中心")])
    client = build_client(repo)
    headers = _login(client, "hq")

    response = client.put(
        f"/api/v1/tenants/{DEFAULT_TENANT_ID}",
        headers=headers,
        json={"display_name": "体验中心", "status": "disabled"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "体验中心是总部，不能停用"


def test_company_admin_cannot_manage_companies() -> None:
    repo = FakeTenantRepository(tenants=[_tenant(DEFAULT_TENANT_ID, "default", "体验中心")])
    client = build_client(repo)
    headers = _login(client, "a-admin")

    assert client.get("/api/v1/tenants", headers=headers).status_code == 403
    created = client.post(
        "/api/v1/tenants", headers=headers, json={"display_name": "我的新公司"}
    )
    assert created.status_code == 403
    assert created.json()["error"]["message"] == "无权限访问该资源"


def test_automatic_company_key_retries_collision(monkeypatch) -> None:
    repo = FakeTenantRepository(tenants=[_tenant(BRANCH_A, "co-collision", "已有公司")])
    generated = iter(["collision", "available"])
    monkeypatch.setattr(tenants_module, "token_hex", lambda _: next(generated))
    client = build_client(repo)

    response = client.post(
        "/api/v1/tenants", headers=_login(client, "hq"), json={"display_name": "新公司"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["tenant_key"] == "co-available"
    assert len(repo.tenants) == 2


def test_automatic_company_key_collision_has_bounded_retry(monkeypatch) -> None:
    repo = FakeTenantRepository(tenants=[_tenant(BRANCH_A, "co-collision", "已有公司")])
    calls = []
    monkeypatch.setattr(
        tenants_module, "token_hex", lambda _: calls.append(1) or "collision"
    )
    client = build_client(repo)

    response = client.post(
        "/api/v1/tenants", headers=_login(client, "hq"), json={"display_name": "新公司"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "公司编码生成失败，请稍后重试"
    assert len(calls) == 5
    assert len(repo.tenants) == 1


def test_explicit_company_key_collision_does_not_replace_requested_key() -> None:
    repo = FakeTenantRepository(tenants=[_tenant(BRANCH_A, "east", "已有公司")])
    client = build_client(repo)

    response = client.post(
        "/api/v1/tenants",
        headers=_login(client, "hq"),
        json={"display_name": "新公司", "tenant_key": "east"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "公司编码已存在"
    assert len(repo.tenants) == 1


@pytest.mark.parametrize("operation", ["create", "update"])
def test_company_name_database_conflict_has_consistent_error(operation) -> None:
    class ConcurrentTenantRepository(FakeTenantRepository):
        async def create_tenant(self, payload):
            raise _unique_violation(TENANT_NAME_UNIQUE_INDEX)

        async def update_tenant(self, tenant_id, payload):
            raise _unique_violation(TENANT_NAME_UNIQUE_INDEX)

    repo = ConcurrentTenantRepository(tenants=[_tenant(BRANCH_A, "east", "华东分公司")])
    client = build_client(repo)
    headers = _login(client, "hq")
    # 名称预检查结束后，另一请求抢先占用该名称，仍须返回可读的冲突提示。
    response = (
        client.post("/api/v1/tenants", headers=headers, json={"display_name": "新公司"})
        if operation == "create"
        else client.put(
            f"/api/v1/tenants/{BRANCH_A}",
            headers=headers,
            json={"display_name": "新公司", "status": "enabled"},
        )
    )

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "公司名称已存在"


def test_company_name_uniqueness_ignores_case_and_surrounding_spaces() -> None:
    repo = FakeTenantRepository(tenants=[_tenant(BRANCH_A, "east", "  East Branch  ")])
    client = build_client(repo)

    response = client.post(
        "/api/v1/tenants", headers=_login(client, "hq"), json={"display_name": " east branch "}
    )

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "公司名称已存在"


def test_delete_company_preserves_headquarters_and_explains_associations() -> None:
    branch = _tenant(BRANCH_A, "east", "华东分公司")
    branch['associations'] = {'账号': 2, '任务': 3, '证据下载审计': 1}
    repo = FakeTenantRepository(tenants=[_tenant(DEFAULT_TENANT_ID, "default", "体验中心"), branch])
    client = build_client(repo)
    headers = _login(client, "hq")
    protected = client.delete(f"/api/v1/tenants/{DEFAULT_TENANT_ID}", headers=headers)
    assert protected.status_code == 409
    assert "不能删除" in protected.json()['error']['message']
    blocked = client.delete(f"/api/v1/tenants/{BRANCH_A}", headers=headers)
    assert blocked.status_code == 409
    assert "3 条任务" in blocked.json()['error']['message']
    assert "1 条证据下载审计" in blocked.json()['error']['message']
    branch['associations'] = {}
    deleted = client.delete(f"/api/v1/tenants/{BRANCH_A}", headers=headers)
    assert deleted.status_code == 204 and deleted.content == b''
    assert client.delete(f"/api/v1/tenants/{BRANCH_A}", headers=headers).status_code == 404
