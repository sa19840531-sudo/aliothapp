"""部门管理接口：公司范围收口、重名校验、停用前校验。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import departments as departments_module
from services.api.app.infrastructure.db.organization_deletion import OrganizationDeletion
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
HQ_TENANT = "00000000-0000-0000-0000-000000000001"
BRANCH_TENANT = "00000000-0000-0000-0000-00000000000a"


@dataclass
class FakeDepartmentRepository:
    rows: list[dict] = field(default_factory=list)

    def _row(self, payload: dict, **extra) -> dict:
        return {
            "id": f"dept-{len(self.rows) + 1}",
            "tenant_name": "北京分公司",
            "product_count": 0,
            "user_count": 0,
            "created_at": NOW,
            "updated_at": NOW,
            **payload,
            **extra,
        }

    async def list_departments(self, *, tenant_id=None, keyword=None, status=None):
        items = [dict(row) for row in self.rows]
        if tenant_id:
            items = [row for row in items if row["tenant_id"] == tenant_id]
        if keyword:
            items = [row for row in items if keyword in row["name"]]
        if status:
            items = [row for row in items if row["status"] == status]
        return items

    async def find_by_id(self, department_id):
        return next((dict(row) for row in self.rows if row["id"] == department_id), None)

    async def find_by_name(self, *, tenant_id, name):
        return next(
            (dict(row) for row in self.rows if row["tenant_id"] == tenant_id and row["name"].lower() == name.lower()),
            None,
        )

    async def create_department(self, payload):
        row = self._row(payload)
        self.rows.append(row)
        return dict(row)

    async def update_department(self, department_id, payload):
        for row in self.rows:
            if row["id"] == department_id:
                row.update(payload)
                return dict(row)
        return None

    async def delete_department(self, department_id, *, tenant_id=None):
        row = await self.find_by_id(department_id)
        if row is None or (tenant_id and row['tenant_id'] != tenant_id):
            return OrganizationDeletion()
        associations = row.get('associations', {})
        if associations:
            return OrganizationDeletion(associations=associations)
        self.rows = [item for item in self.rows if item['id'] != department_id]
        return OrganizationDeletion(deleted=True)

    async def list_budgets(self):
        return [row for row in self.rows if row.get("monthly_token_budget") is not None]


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


HQ_ADMIN = _user("hq", "平台管理员", HQ_TENANT, ["dashboard", "users", "users:any", "tenants", "departments"])
COMPANY_ADMIN = _user("branch-admin", "公司管理员", BRANCH_TENANT, ["dashboard", "users", "departments"])
PLAIN_USER = _user("plain", "规则配置员", BRANCH_TENANT, ["dashboard", "qcRules"])


def _client(user, repository: FakeDepartmentRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[auth_module.require_auth_user] = lambda: user
    app.dependency_overrides[departments_module._get_department_repository] = lambda: repository
    return TestClient(app)


def _seed() -> FakeDepartmentRepository:
    repo = FakeDepartmentRepository()
    repo.rows.append(repo._row({"tenant_id": HQ_TENANT, "name": "体验部", "owner_name": None, "status": "enabled"}, tenant_name="体验中心"))
    repo.rows.append(repo._row({"tenant_id": BRANCH_TENANT, "name": "一部", "owner_name": "小明", "status": "enabled"}, product_count=2, user_count=1))
    repo.rows.append(repo._row({"tenant_id": BRANCH_TENANT, "name": "二部", "owner_name": None, "status": "enabled"}))
    return repo


def test_company_admin_only_sees_and_creates_own_company_departments() -> None:
    repo = _seed()
    client = _client(COMPANY_ADMIN, repo)

    listed = client.get("/api/v1/departments", params={"tenant_id": HQ_TENANT})
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["一部", "二部"]

    created = client.post("/api/v1/departments", json={"name": "三部", "tenant_id": HQ_TENANT})
    assert created.status_code == 201, created.text
    assert created.json()["tenant_id"] == BRANCH_TENANT

    forbidden = client.put("/api/v1/departments/dept-1", json={"name": "体验部", "status": "enabled"})
    assert forbidden.status_code == 403


def test_hq_can_pick_company_and_duplicate_name_is_rejected() -> None:
    repo = _seed()
    client = _client(HQ_ADMIN, repo)

    all_rows = client.get("/api/v1/departments").json()["items"]
    assert {item["tenant_id"] for item in all_rows} == {HQ_TENANT, BRANCH_TENANT}

    created = client.post(
        "/api/v1/departments",
        json={"name": "三部", "tenant_id": BRANCH_TENANT, "owner_name": "小红", "monthly_token_budget": 4000},
    )
    assert created.status_code == 201, created.text
    assert created.json()["tenant_id"] == BRANCH_TENANT
    assert created.json()["monthly_token_budget"] == 4000

    duplicate = client.post("/api/v1/departments", json={"name": "一部", "tenant_id": BRANCH_TENANT})
    assert duplicate.status_code == 409
    assert "同名部门" in duplicate.json()["error"]["message"]


def test_disable_department_is_blocked_while_products_or_accounts_remain() -> None:
    repo = _seed()
    client = _client(HQ_ADMIN, repo)

    blocked = client.put("/api/v1/departments/dept-2", json={"name": "一部", "status": "disabled"})
    assert blocked.status_code == 409
    assert "2 个启用产品" in blocked.json()["error"]["message"]
    assert "1 个启用账号" in blocked.json()["error"]["message"]

    disabled = client.put("/api/v1/departments/dept-3", json={"name": "二部", "status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"


def test_department_permission_is_required() -> None:
    client = _client(PLAIN_USER, _seed())
    assert client.get("/api/v1/departments").status_code == 403
    assert client.post("/api/v1/departments", json={"name": "四部"}).status_code == 403


def test_delete_department_scope_association_message_and_empty_success() -> None:
    repo = _seed()
    client = _client(COMPANY_ADMIN, repo)
    assert client.delete("/api/v1/departments/dept-1").status_code == 403
    repo.rows[1]['associations'] = {'产品': 2, '账号': 1, '任务': 3}
    blocked = client.delete("/api/v1/departments/dept-2")
    assert blocked.status_code == 409
    assert "3 条任务" in blocked.json()['error']['message']
    assert "含停用或历史数据" in blocked.json()['error']['message']
    deleted = client.delete("/api/v1/departments/dept-3")
    assert deleted.status_code == 204 and deleted.content == b''
    assert client.delete("/api/v1/departments/dept-3").status_code == 404
    assert _client(PLAIN_USER, _seed()).delete("/api/v1/departments/dept-3").status_code == 403
