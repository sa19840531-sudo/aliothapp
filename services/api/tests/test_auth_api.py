from dataclasses import dataclass

import jwt
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.auth import hash_password
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth


@dataclass
class FakeUserRepository:
    users: list[dict]

    async def find_by_username(self, username: str):
        return next((item for item in self.users if item["username"] == username), None)

    async def touch_last_login(self, user_id: str):
        return None


def build_client(repo: FakeUserRepository) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[auth_module._get_user_repository] = lambda: repo
    return TestClient(app)


def test_login_returns_token_and_user_profile() -> None:
    client = build_client(
        FakeUserRepository(
            users=[
                {
                    "id": "user-1",
                    "username": "admin",
                    "full_name": "系统管理员",
                    "password_hash": hash_password("alioth123"),
                    "role": "平台管理员",
                    "status": "enabled",
                    "tenant_id": "tenant-1",
                }
            ]
        )
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "alioth123"},
    )

    assert response.status_code == 200
    body = response.json()
    # 标准 JWT：三段式且能用服务端密钥验签，声明携带身份与到期时间。
    token = body["token"]
    assert token.count(".") == 2
    claims = jwt.decode(
        token, auth_module._auth_secret(), algorithms=[auth_module._JWT_ALGORITHM]
    )
    assert claims["username"] == "admin"
    assert claims["sub"] == "user-1"
    assert claims["tenant_id"] == "tenant-1"
    assert claims["exp"] > claims["iat"]
    assert body["user"] == {
        "id": "user-1",
        "username": "admin",
        "full_name": "系统管理员",
        "role": "平台管理员",
        "status": "enabled",
        "permissions": [
            "dashboard",
            "users",
            "users:any",
            "tenants",
            "departments",
            "roles",
            "auditLogs",
            "qcRules",
            "qcRules:write",
            "configCenter",
            "costReports",
            "analytics",
            "system",
            "agentRuns",
            "systemOperations",
            "taskObjects:read:any",
            "taskObjects:write:any",
            "taskObjects:review:any",
            "evidenceExports:original",
            "evidenceExports:redacted",
            "evidenceRedactions:manage",
        ],
        "tenant_id": "tenant-1",
        "tenant_name": "",
        "department_name": "",
    }
    assert body["expires_at"]


def test_me_requires_bearer_token() -> None:
    client = build_client(FakeUserRepository(users=[]))

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "缺少登录令牌"


def test_me_returns_current_user_from_token() -> None:
    client = build_client(
        FakeUserRepository(
            users=[
                {
                    "id": "user-2",
                    "username": "rule.editor",
                    "full_name": "规则编辑",
                    "password_hash": hash_password("secret"),
                    "role": "规则配置员",
                    "status": "enabled",
                    "tenant_id": "tenant-1",
                }
            ]
        )
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"username": "rule.editor", "password": "secret"},
    )
    token = login_response.json()["token"]

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["username"] == "rule.editor"
    assert response.json()["role"] == "规则配置员"


def test_login_fails_when_user_disabled() -> None:
    client = build_client(
        FakeUserRepository(
            users=[
                {
                    "id": "user-3",
                    "username": "disabled.user",
                    "full_name": "停用用户",
                    "password_hash": hash_password("secret"),
                    "role": "规则配置员",
                    "status": "disabled",
                    "tenant_id": "tenant-1",
                }
            ]
        )
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "disabled.user", "password": "secret"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "账号已禁用，请联系管理员"

def _admin_repo() -> FakeUserRepository:
    return FakeUserRepository(
        users=[
            {
                "id": "user-1",
                "username": "admin",
                "full_name": "系统管理员",
                "password_hash": hash_password("alioth123"),
                "role": "平台管理员",
                "status": "enabled",
                "tenant_id": "tenant-1",
            }
        ]
    )


def test_me_rejects_expired_jwt() -> None:
    import time

    client = build_client(_admin_repo())
    expired_token = jwt.encode(
        {
            "sub": "user-1",
            "username": "admin",
            "iat": int(time.time()) - 3600,
            "exp": int(time.time()) - 60,
        },
        auth_module._auth_secret(),
        algorithm=auth_module._JWT_ALGORITHM,
    )
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"}
    )
    assert response.status_code == 401
    assert "过期" in response.json()["error"]["message"]


def test_me_rejects_forged_signature() -> None:
    import time

    client = build_client(_admin_repo())
    forged_token = jwt.encode(
        {
            "sub": "user-1",
            "username": "admin",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "wrong-secret",
        algorithm=auth_module._JWT_ALGORITHM,
    )
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {forged_token}"}
    )
    assert response.status_code == 401
    assert "无效" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "secret",
    [
        None,
        "too-short-secret",
        auth_module._DEV_FALLBACK_SECRET,
        auth_module._PRODUCTION_PLACEHOLDER_SECRET,
    ],
    ids=["missing", "too-short", "development-fallback", "template-placeholder"],
)
def test_production_rejects_missing_weak_or_development_auth_secret(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    monkeypatch.setenv("ALIOTH_ENVIRONMENT", "production")
    if secret is None:
        monkeypatch.delenv("ALIOTH_AUTH_SECRET", raising=False)
    else:
        monkeypatch.setenv("ALIOTH_AUTH_SECRET", secret)
    auth_module._auth_secret.cache_clear()

    with pytest.raises(ValueError, match="生产环境"):
        auth_module.validate_auth_secret_at_startup()

    auth_module._auth_secret.cache_clear()


def test_development_keeps_compatible_auth_secret_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_ENVIRONMENT", "development")
    monkeypatch.delenv("ALIOTH_AUTH_SECRET", raising=False)
    auth_module._auth_secret.cache_clear()

    assert auth_module._auth_secret() == auth_module._DEV_FALLBACK_SECRET

    auth_module._auth_secret.cache_clear()


def test_production_accepts_explicit_strong_auth_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_ENVIRONMENT", "production")
    monkeypatch.setenv("ALIOTH_AUTH_SECRET", "production-random-secret-at-least-32-characters")
    auth_module._auth_secret.cache_clear()

    auth_module.validate_auth_secret_at_startup()
    assert auth_module._auth_secret() == "production-random-secret-at-least-32-characters"

    auth_module._auth_secret.cache_clear()
