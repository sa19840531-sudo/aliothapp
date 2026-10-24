from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import users as users_module
from services.api.app.api.v1.auth import hash_password
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth

HQ_TENANT = "00000000-0000-0000-0000-000000000001"
BRANCH_A = "00000000-0000-0000-0000-00000000000a"
BRANCH_B = "00000000-0000-0000-0000-00000000000b"
TENANT_NAMES = {HQ_TENANT: "体验中心", BRANCH_A: "华东分公司", BRANCH_B: "华南分公司"}


@dataclass
class FakeTenantRepository:
    tenants: dict[str, dict] = field(default_factory=dict)

    async def find_by_id(self, tenant_id: str):
        return self.tenants.get(tenant_id)


@dataclass
class FakeDepartmentRepository:
    departments: list[dict] = field(default_factory=list)

    async def find_by_name(self, *, tenant_id: str, name: str):
        return next(
            (
                item
                for item in self.departments
                if item["tenant_id"] == tenant_id and item["name"].lower() == name.lower()
            ),
            None,
        )


def _department(tenant_id: str, name: str, status: str = "enabled") -> dict:
    return {"id": f"dept-{name}", "tenant_id": tenant_id, "name": name, "status": status}


def _default_departments() -> FakeDepartmentRepository:
    return FakeDepartmentRepository(
        departments=[
            _department(HQ_TENANT, "质检一部"),
            _department(BRANCH_A, "华东一部"),
            _department(BRANCH_A, "华东旧部", status="disabled"),
            _department(BRANCH_B, "华南一部"),
        ]
    )


@dataclass
class FakeUserRepository:
    users: list[dict] = field(default_factory=list)

    async def find_by_username(self, username: str):
        return next((item for item in self.users if item["username"] == username), None)

    async def find_by_id(self, user_id: str):
        return next((item for item in self.users if item["id"] == user_id), None)

    async def touch_last_login(self, user_id: str):
        return None

    async def list_users(
        self, *, keyword, status, page, page_size, tenant_id=None, role=None, department_name=None
    ):
        filtered = self.users
        if role:
            filtered = [item for item in filtered if item["role"] == role]
        if department_name:
            filtered = [item for item in filtered if item.get("department_name") == department_name]
        if keyword:
            filtered = [
                item
                for item in filtered
                if keyword in item["username"] or keyword in item["full_name"]
            ]
        if status:
            filtered = [item for item in filtered if item["status"] == status]
        if tenant_id:
            filtered = [item for item in filtered if item.get("tenant_id") == tenant_id]
        start = (page - 1) * page_size
        end = start + page_size
        page_items = filtered[start:end]
        return [self._to_response(item) for item in page_items], len(filtered)

    async def create_user(self, payload):
        if any(item["username"] == payload["username"] for item in self.users):
            raise ValueError("duplicate")
        item = {
            "id": f"user-{len(self.users) + 1}",
            "username": payload["username"],
            "full_name": payload["full_name"],
            "password_hash": payload["password_hash"],
            "role": payload["role"],
            "status": payload["status"],
            "tenant_id": payload.get("tenant_id") or HQ_TENANT,
            "department_name": payload.get("department_name"),
            "created_at": "2026-07-09T08:00:00+00:00",
            "updated_at": "2026-07-09T08:00:00+00:00",
        }
        self.users.append(item)
        return self._to_response(item)

    async def update_user(self, user_id: str, payload):
        for item in self.users:
            if item["id"] == user_id:
                item["full_name"] = payload["full_name"]
                item["role"] = payload["role"]
                item["status"] = payload["status"]
                if payload.get("password_hash"):
                    item["password_hash"] = payload["password_hash"]
                if payload.get("tenant_id"):
                    item["tenant_id"] = payload["tenant_id"]
                item["department_name"] = payload.get("department_name")
                item["updated_at"] = "2026-07-09T09:00:00+00:00"
                return self._to_response(item)
        return None

    async def delete_user(self, user_id: str):
        original = len(self.users)
        self.users = [item for item in self.users if item["id"] != user_id]
        return len(self.users) != original

    @staticmethod
    def _to_response(item: dict) -> dict:
        tenant_id = item.get("tenant_id") or HQ_TENANT
        return {
            "id": item["id"],
            "username": item["username"],
            "full_name": item["full_name"],
            "role": item["role"],
            "status": item["status"],
            "tenant_id": tenant_id,
            "tenant_name": TENANT_NAMES.get(tenant_id, ""),
            "department_name": item.get("department_name"),
            "created_at": item.get("created_at", "2026-07-09T08:00:00+00:00"),
            "updated_at": item.get("updated_at", "2026-07-09T08:00:00+00:00"),
        }


def build_client(
    repo: FakeUserRepository,
    tenant_repo: FakeTenantRepository | None = None,
    department_repo: FakeDepartmentRepository | None = None,
) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    tenants = tenant_repo or FakeTenantRepository(
        tenants={
            tenant_id: {"id": tenant_id, "display_name": name, "status": "enabled"}
            for tenant_id, name in TENANT_NAMES.items()
        }
    )
    departments = department_repo or _default_departments()
    app.dependency_overrides[auth_module._get_user_repository] = lambda: repo
    app.dependency_overrides[users_module._get_user_repository] = lambda: repo
    app.dependency_overrides[users_module._get_tenant_repository] = lambda: tenants
    app.dependency_overrides[users_module._get_department_repository] = lambda: departments
    return TestClient(app)


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response.json()["token"]


def _user(
    user_id: str,
    username: str,
    role: str,
    tenant_id: str,
    *,
    password: str = "secret123",
    full_name: str | None = None,
) -> dict:
    return {
        "id": user_id,
        "username": username,
        "full_name": full_name or username,
        "password_hash": hash_password(password),
        "role": role,
        "status": "enabled",
        "tenant_id": tenant_id,
        "created_at": "2026-07-09T08:00:00+00:00",
        "updated_at": "2026-07-09T08:00:00+00:00",
    }


def _multi_company_repo() -> FakeUserRepository:
    return FakeUserRepository(
        users=[
            _user("hq-admin", "hq", "平台管理员", HQ_TENANT, full_name="总部管理员"),
            _user("a-admin", "a-admin", "公司管理员", BRANCH_A, full_name="华东管理员"),
            _user("a-platform", "a-platform", "平台管理员", BRANCH_A, full_name="驻华东总部"),
            _user("a-qc", "a-qc", "规则配置员", BRANCH_A, full_name="华东质检"),
            _user("b-qc", "b-qc", "规则配置员", BRANCH_B, full_name="华南质检"),
        ]
    )


def test_users_crud_flow() -> None:
    repo = FakeUserRepository(
        users=[
            {
                "id": "admin-1",
                "username": "admin",
                "full_name": "系统管理员",
                "password_hash": hash_password("alioth123"),
                "role": "平台管理员",
                "status": "enabled",
                "tenant_id": HQ_TENANT,
                "created_at": "2026-07-09T08:00:00+00:00",
                "updated_at": "2026-07-09T08:00:00+00:00",
            }
        ]
    )
    client = build_client(repo)
    token = _login(client, "admin", "alioth123")

    create_response = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "alice",
            "full_name": "Alice Chen",
            "password": "alice123",
            "role": "规则配置员",
            "status": "enabled",
            "department_name": "质检一部",
        },
    )
    assert create_response.status_code == 201
    user_id = create_response.json()["id"]
    # 不指定公司时落在操作者自己的公司。
    assert create_response.json()["tenant_id"] == HQ_TENANT
    assert create_response.json()["tenant_name"] == "体验中心"

    list_response = client.get(
        "/api/v1/users?page=1&page_size=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 2

    update_response = client.put(
        f"/api/v1/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "full_name": "Alice Zhang",
            "role": "审计员",
            "status": "disabled",
            "password": "alice456",
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "disabled"

    delete_response = client.delete(
        f"/api/v1/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert delete_response.status_code == 204
    assert delete_response.content == b""


def test_list_users_accepts_datetime_timestamps() -> None:
    ts = datetime(2026, 7, 9, 8, 0, tzinfo=UTC)
    repo = FakeUserRepository(
        users=[
            {
                "id": "admin-1",
                "username": "admin",
                "full_name": "系统管理员",
                "password_hash": hash_password("alioth123"),
                "role": "平台管理员",
                "status": "enabled",
                "created_at": ts,
                "updated_at": ts,
            }
        ]
    )
    client = build_client(repo)
    token = _login(client, "admin", "alioth123")

    response = client.get(
        "/api/v1/users?page=1&page_size=10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["created_at"] == "2026-07-09T08:00:00Z"


def test_users_requires_permission() -> None:
    repo = FakeUserRepository(
        users=[
            {
                "id": "user-1",
                "username": "editor",
                "full_name": "规则编辑",
                "password_hash": hash_password("secret"),
                "role": "规则配置员",
                "status": "enabled",
            }
        ]
    )
    client = build_client(repo)
    token = _login(client, "editor", "secret")

    response = client.get(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "无权限访问该资源"


def test_company_admin_only_sees_own_company_users() -> None:
    client = build_client(_multi_company_repo())
    token = _login(client, "a-admin", "secret123")

    response = client.get(
        "/api/v1/users?page=1&page_size=10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert {item["username"] for item in body["items"]} == {"a-admin", "a-platform", "a-qc"}
    assert {item["tenant_name"] for item in body["items"]} == {"华东分公司"}

    # 公司管理员即使显式传别的公司，也只能看到本公司。
    forced = client.get(
        f"/api/v1/users?tenant_id={BRANCH_B}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert forced.status_code == 200
    assert {item["username"] for item in forced.json()["items"]} == {
        "a-admin",
        "a-platform",
        "a-qc",
    }


def test_hq_admin_sees_all_companies_and_can_filter_by_company() -> None:
    client = build_client(_multi_company_repo())
    token = _login(client, "hq", "secret123")

    everyone = client.get(
        "/api/v1/users?page=1&page_size=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert everyone.status_code == 200
    assert everyone.json()["total"] == 5

    branch_b = client.get(
        f"/api/v1/users?tenant_id={BRANCH_B}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert branch_b.status_code == 200
    assert [item["username"] for item in branch_b.json()["items"]] == ["b-qc"]


def test_company_admin_creates_users_only_in_own_company() -> None:
    repo = _multi_company_repo()
    client = build_client(repo)
    token = _login(client, "a-admin", "secret123")
    headers = {"Authorization": f"Bearer {token}"}
    base_payload = {
        "username": "a-new",
        "full_name": "华东新同事",
        "password": "newpass123",
        "role": "规则配置员",
        "status": "enabled",
        "department_name": "华东一部",
    }

    other_company = client.post(
        "/api/v1/users", headers=headers, json={**base_payload, "tenant_id": BRANCH_B}
    )
    assert other_company.status_code == 403
    assert other_company.json()["error"]["message"] == "只能维护本公司的用户"

    platform_admin = client.post(
        "/api/v1/users", headers=headers, json={**base_payload, "role": "平台管理员"}
    )
    assert platform_admin.status_code == 403
    assert platform_admin.json()["error"]["message"] == "只有总部可以设置平台管理员"

    own_company = client.post("/api/v1/users", headers=headers, json=base_payload)
    assert own_company.status_code == 201
    assert own_company.json()["tenant_id"] == BRANCH_A
    assert own_company.json()["tenant_name"] == "华东分公司"

    explicit_own = client.post(
        "/api/v1/users",
        headers=headers,
        json={**base_payload, "username": "a-new-2", "tenant_id": BRANCH_A},
    )
    assert explicit_own.status_code == 201
    assert explicit_own.json()["tenant_id"] == BRANCH_A


def test_company_admin_cannot_touch_other_company_or_platform_admin_accounts() -> None:
    client = build_client(_multi_company_repo())
    token = _login(client, "a-admin", "secret123")
    headers = {"Authorization": f"Bearer {token}"}
    update_payload = {
        "full_name": "改名",
        "role": "规则配置员",
        "status": "enabled",
        "department_name": "华东一部",
    }

    # 别的公司的账号对公司管理员来说等于不存在。
    other = client.put("/api/v1/users/b-qc", headers=headers, json=update_payload)
    assert other.status_code == 404
    other_delete = client.delete("/api/v1/users/b-qc", headers=headers)
    assert other_delete.status_code == 404

    # 总部账号在别的公司，同样视作不存在。
    hq = client.put("/api/v1/users/hq-admin", headers=headers, json=update_payload)
    assert hq.status_code == 404

    # 落在本公司的平台管理员账号也不能被公司管理员改动或删除。
    local_admin = client.put("/api/v1/users/a-platform", headers=headers, json=update_payload)
    assert local_admin.status_code == 403
    assert local_admin.json()["error"]["message"] == "只有总部可以维护平台管理员"
    local_admin_delete = client.delete("/api/v1/users/a-platform", headers=headers)
    assert local_admin_delete.status_code == 403

    # 本公司账号不能被挪到别的公司，也不能升为平台管理员。
    moved = client.put(
        "/api/v1/users/a-qc", headers=headers, json={**update_payload, "tenant_id": BRANCH_B}
    )
    assert moved.status_code == 403
    assert moved.json()["error"]["message"] == "只能维护本公司的用户"
    promoted = client.put(
        "/api/v1/users/a-qc", headers=headers, json={**update_payload, "role": "平台管理员"}
    )
    assert promoted.status_code == 403

    own = client.put("/api/v1/users/a-qc", headers=headers, json=update_payload)
    assert own.status_code == 200
    assert own.json()["full_name"] == "改名"
    assert own.json()["tenant_id"] == BRANCH_A


def test_hq_admin_assigns_company_and_rejects_unknown_or_disabled_company() -> None:
    tenants = FakeTenantRepository(
        tenants={
            HQ_TENANT: {"id": HQ_TENANT, "display_name": "体验中心", "status": "enabled"},
            BRANCH_A: {"id": BRANCH_A, "display_name": "华东分公司", "status": "enabled"},
            BRANCH_B: {"id": BRANCH_B, "display_name": "华南分公司", "status": "disabled"},
        }
    )
    client = build_client(_multi_company_repo(), tenants)
    token = _login(client, "hq", "secret123")
    headers = {"Authorization": f"Bearer {token}"}
    base_payload = {
        "username": "fresh",
        "full_name": "新人",
        "password": "newpass123",
        "role": "公司管理员",
        "status": "enabled",
    }

    created = client.post(
        "/api/v1/users", headers=headers, json={**base_payload, "tenant_id": BRANCH_A}
    )
    assert created.status_code == 201
    assert created.json()["tenant_id"] == BRANCH_A
    assert created.json()["role"] == "公司管理员"

    unknown = client.post(
        "/api/v1/users",
        headers=headers,
        json={**base_payload, "username": "fresh-2", "tenant_id": "no-such-company"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["message"] == "公司不存在"

    disabled = client.post(
        "/api/v1/users",
        headers=headers,
        json={**base_payload, "username": "fresh-3", "tenant_id": BRANCH_B},
    )
    assert disabled.status_code == 409
    assert disabled.json()["error"]["message"] == "该公司已停用，不能再添加用户"

    # 总部可以把账号挪到别的公司。
    moved = client.put(
        "/api/v1/users/a-qc",
        headers=headers,
        json={
            "full_name": "华东质检",
            "role": "规则配置员",
            "status": "enabled",
            "tenant_id": HQ_TENANT,
            "department_name": "质检一部",
        },
    )
    assert moved.status_code == 200
    assert moved.json()["tenant_id"] == HQ_TENANT
    assert moved.json()["tenant_name"] == "体验中心"


def test_department_account_must_pick_an_enabled_department_of_its_company() -> None:
    client = build_client(_multi_company_repo())
    token = _login(client, "a-admin", "secret123")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "username": "a-dept",
        "full_name": "华东部门账号",
        "password": "newpass123",
        "role": "规则配置员",
        "status": "enabled",
    }

    missing = client.post("/api/v1/users", headers=headers, json=payload)
    assert missing.status_code == 400
    assert missing.json()["error"]["message"] == "部门账号必须选择所属部门"

    unknown = client.post(
        "/api/v1/users", headers=headers, json={**payload, "department_name": "不存在的部门"}
    )
    assert unknown.status_code == 400
    assert "部门管理" in unknown.json()["error"]["message"]

    # 别的公司的部门不算本公司的部门
    other_company = client.post(
        "/api/v1/users", headers=headers, json={**payload, "department_name": "华南一部"}
    )
    assert other_company.status_code == 400

    disabled = client.post(
        "/api/v1/users", headers=headers, json={**payload, "department_name": "华东旧部"}
    )
    assert disabled.status_code == 409
    assert disabled.json()["error"]["message"] == "该部门已停用，不能再添加账号"

    created = client.post(
        "/api/v1/users", headers=headers, json={**payload, "department_name": "华东一部"}
    )
    assert created.status_code == 201
    assert created.json()["department_name"] == "华东一部"
    assert created.json()["data_scope"] == "仅华东一部数据"


def test_company_level_roles_do_not_keep_a_department() -> None:
    client = build_client(_multi_company_repo())
    token = _login(client, "a-admin", "secret123")

    created = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "a-auditor",
            "full_name": "华东审计",
            "password": "newpass123",
            "role": "审计员",
            "status": "enabled",
            "department_name": "华东一部",
        },
    )

    assert created.status_code == 201
    assert created.json()["department_name"] is None
    assert created.json()["data_scope"] == "本公司（只读）"


def test_department_account_keeps_its_disabled_department_but_cannot_move_into_one() -> None:
    repo = _multi_company_repo()
    a_qc = next(item for item in repo.users if item["id"] == "a-qc")
    a_qc["department_name"] = "华东旧部"
    client = build_client(repo)
    token = _login(client, "a-admin", "secret123")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"full_name": "华东质检", "role": "规则配置员", "status": "enabled"}

    # 原本就在已停用部门里的账号，改别的字段不受影响
    kept = client.put(
        "/api/v1/users/a-qc", headers=headers, json={**payload, "department_name": "华东旧部"}
    )
    assert kept.status_code == 200
    assert kept.json()["department_name"] == "华东旧部"

    a_qc["department_name"] = "华东一部"
    moved_in = client.put(
        "/api/v1/users/a-qc", headers=headers, json={**payload, "department_name": "华东旧部"}
    )
    assert moved_in.status_code == 409
    assert moved_in.json()["error"]["message"] == "该部门已停用，不能再添加账号"
