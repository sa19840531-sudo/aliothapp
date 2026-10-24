"""业务接口强制 Bearer 鉴权，长期登录令牌不得通过查询参数传递。"""

import time
from dataclasses import dataclass, field

import jwt
from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.workstation import tasks as workstation_tasks_module
from services.api.app.application.workstation_tasks import (
    WorkstationTaskListFilter,
    WorkstationTaskListResult,
)
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth


@dataclass
class FakeUserRepository:
    users: list[dict] = field(
        default_factory=lambda: [
            {
                "id": "user-1",
                "username": "admin",
                "full_name": "系统管理员",
                "role": "平台管理员",
                "status": "enabled",
                "tenant_id": "tenant-1",
            }
        ]
    )

    async def find_by_username(self, username: str):
        return next((item for item in self.users if item["username"] == username), None)

    async def touch_last_login(self, user_id: str):
        return None


class FakeWorkstationTaskService:
    async def list_tasks(
        self,
        filters: WorkstationTaskListFilter,
        *,
        access_scope,
    ) -> WorkstationTaskListResult:
        return WorkstationTaskListResult(
            items=[], total=0, page=filters.page, page_size=filters.page_size
        )


def _client_with_real_auth() -> TestClient:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[auth_module._get_user_repository] = lambda: FakeUserRepository()
    app.dependency_overrides[workstation_tasks_module._get_workstation_task_service] = (
        lambda: FakeWorkstationTaskService()
    )
    return TestClient(app)


def _valid_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "user-1",
            "username": "admin",
            "iat": now,
            "exp": now + 3600,
        },
        auth_module._auth_secret(),
        algorithm=auth_module._JWT_ALGORITHM,
    )


def test_business_endpoints_require_token() -> None:
    client = _client_with_real_auth()
    for path in (
        "/api/v1/workstation/tasks",
        "/api/v1/workstation/system/status",
        "/api/v1/workstation/reports",
        "/api/v1/tasks",
    ):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["message"] == "缺少登录令牌", path


def test_bearer_header_grants_access() -> None:
    client = _client_with_real_auth()
    response = client.get(
        "/api/v1/workstation/tasks",
        headers={"Authorization": f"Bearer {_valid_token()}"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_access_token_query_param_is_rejected() -> None:
    client = _client_with_real_auth()
    response = client.get(f"/api/v1/workstation/tasks?access_token={_valid_token()}")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "缺少登录令牌"


def test_evidence_ticket_issuance_requires_bearer() -> None:
    client = _client_with_real_auth()
    response = client.post(
        "/api/v1/evidence/00000000-0000-0000-0000-000000000000/ticket"
    )
    assert response.status_code == 401


def test_health_and_login_stay_open() -> None:
    client = _client_with_real_auth()
    assert client.get("/api/v1/health").status_code == 200
    login = client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "x"}
    )
    # 登录本身无需令牌（错误凭据是 401 业务错误，而不是"缺少登录令牌"）。
    assert login.status_code == 401
    assert login.json()["error"]["message"] == "账号或密码错误"
