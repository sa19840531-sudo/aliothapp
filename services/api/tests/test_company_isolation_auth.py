"""公司隔离在登录态上的表现：停用公司拒登、公司管理员权限、成本报表只对总部开放。"""

from dataclasses import dataclass

from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.auth import hash_password
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


def _user(username: str, role: str, *, tenant_status: str = "enabled") -> dict:
    return {
        "id": f"{username}-id",
        "username": username,
        "full_name": username,
        "password_hash": hash_password("secret123"),
        "role": role,
        "status": "enabled",
        "tenant_id": BRANCH_A,
        "tenant_name": "华东分公司",
        "tenant_status": tenant_status,
    }


def build_client(repo: FakeUserRepository) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[auth_module._get_user_repository] = lambda: repo
    return TestClient(app)


def _login(client: TestClient, username: str):
    return client.post(
        "/api/v1/auth/login", json={"username": username, "password": "secret123"}
    )


def test_login_and_me_carry_company_name() -> None:
    client = build_client(FakeUserRepository(users=[_user("a-admin", "公司管理员")]))

    response = _login(client, "a-admin")

    assert response.status_code == 200
    body = response.json()["user"]
    assert body["tenant_id"] == BRANCH_A
    assert body["tenant_name"] == "华东分公司"
    assert body["role"] == "公司管理员"
    # 公司管理员：管本公司账号 + 看本公司全部任务，不能跨公司也不能建公司。
    assert body["permissions"] == [
        "dashboard",
        "users",
        "departments",
        "roles",
        "qcRules",
        "configCenter",
        "taskObjects:read:tenant",
        "taskObjects:write:owner",
        "taskObjects:review:tenant",
        "evidenceExports:original",
        "evidenceExports:redacted",
        "evidenceRedactions:manage",
    ]

    me = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {response.json()['token']}"}
    )
    assert me.status_code == 200
    assert me.json()["tenant_name"] == "华东分公司"


def test_login_rejected_when_company_disabled() -> None:
    client = build_client(
        FakeUserRepository(users=[_user("a-qc", "规则配置员", tenant_status="disabled")])
    )

    response = _login(client, "a-qc")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "所属公司已停用，请联系总部管理员"


def test_existing_token_stops_working_after_company_disabled() -> None:
    repo = FakeUserRepository(users=[_user("a-qc", "规则配置员")])
    client = build_client(repo)
    token = _login(client, "a-qc").json()["token"]

    repo.users[0]["tenant_status"] = "disabled"
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 401
    assert me.json()["error"]["message"] == "所属公司已停用，请联系总部管理员"


def test_cost_reports_only_open_to_headquarters() -> None:
    client = build_client(FakeUserRepository(users=[_user("a-admin", "公司管理员")]))
    token = _login(client, "a-admin").json()["token"]

    response = client.get(
        "/api/v1/cost-reports/llm-runs", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "无权限查看成本报表"
