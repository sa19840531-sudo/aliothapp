"""部门级数据范围落到接口：产品可见范围 / 归属固定、任务提交时部门以登录身份为准。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import products as products_module
from services.api.app.application.spreadsheet import build_xlsx
from services.api.app.application.task_submission import TaskSubmissionService
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
from services.api.app.main import create_app
from services.api.tests.test_task_submission import FakeTaskRepository

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
BRANCH_TENANT = "00000000-0000-0000-0000-00000000000a"
DEPARTMENT_PERMISSIONS = [
    "dashboard",
    "qcRules",
    "taskObjects:read:owner",
    "taskObjects:write:owner",
    "taskObjects:read:department",
    "taskObjects:review:department",
]


@dataclass
class FakeProductRepository:
    products: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    records: list = field(default_factory=list)

    def _row(self, payload: dict, product_id: str) -> dict:
        return {
            "id": product_id,
            "h5_url": None,
            "department_name": None,
            "owner_name": None,
            "platforms": [],
            "status": "enabled",
            "tenant_id": BRANCH_TENANT,
            "tenant_name": "北京分公司",
            "created_at": NOW,
            "updated_at": NOW,
            **payload,
        }

    async def list_products(self, **kwargs):
        self.calls.append(("list", kwargs))
        return [], 0

    async def list_enabled_products(self, **kwargs):
        self.calls.append(("enabled", kwargs))
        return []

    async def find_by_id(self, product_id):
        return dict(self.products[product_id]) if product_id in self.products else None

    async def find_by_code(self, project_code):
        return next((dict(item) for item in self.products.values() if item["project_code"] == project_code), None)

    async def create_product(self, payload):
        row = self._row(payload, f"product-{len(self.products) + 1}")
        self.products[row["id"]] = row
        return dict(row)

    async def update_product(self, product_id, payload):
        self.products[product_id].update(payload)
        return dict(self.products[product_id])

    async def upsert_product(self, payload):
        existing = await self.find_by_code(payload["project_code"])
        if existing:
            existing.update(payload)
            return dict(existing), False
        row = self._row(payload, f"product-{len(self.products) + 1}")
        self.products[row["id"]] = row
        return dict(row), True

    async def record_import(self, payload):
        item = {"id": "record-1", "batch_no": "IMP-20260909-001", **payload}
        self.records.append(item)
        return item


def _department_user():
    return auth_module.AuthUserResponse(
        id="dept-user",
        username="dept-user",
        full_name="一部小明",
        role="规则配置员",
        status="enabled",
        permissions=DEPARTMENT_PERMISSIONS,
        tenant_id=BRANCH_TENANT,
        tenant_name="北京分公司",
        department_name="一部",
    )


def _client(repo: FakeProductRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[auth_module.require_auth_user] = _department_user
    app.dependency_overrides[products_module._get_product_repository] = lambda: repo
    return TestClient(app)


def test_department_account_product_visibility_and_pinned_department() -> None:
    repo = FakeProductRepository()
    client = _client(repo)

    assert client.get("/api/v1/products").status_code == 200
    assert repo.calls[-1][1]["visible_tenant_id"] == BRANCH_TENANT
    assert repo.calls[-1][1]["visible_department"] == "一部"

    created = client.post(
        "/api/v1/products",
        json={"project_code": "MB-001", "product_name": "手机银行", "department_name": "二部", "platforms": ["app"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["department_name"] == "一部"
    assert created.json()["tenant_id"] == BRANCH_TENANT

    # 其它部门的产品不能改；本部门的能改，且部门不能被改走
    repo.products["product-9"] = repo._row({"project_code": "OTHER-1", "product_name": "二部产品", "department_name": "二部"}, "product-9")
    forbidden = client.put(
        "/api/v1/products/product-9",
        json={"project_code": "OTHER-1", "product_name": "二部产品", "department_name": "二部"},
    )
    assert forbidden.status_code == 403
    assert "本部门" in forbidden.json()["error"]["message"]

    updated = client.put(
        "/api/v1/products/product-1",
        json={"project_code": "MB-001", "product_name": "手机银行改名", "department_name": "三部"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["department_name"] == "一部"


def test_department_account_import_is_pinned_to_own_department() -> None:
    repo = FakeProductRepository()
    client = _client(repo)
    payload = build_xlsx(
        [
            ["产品编码", "产品名称", "所属部门", "平台类型"],
            ["A-1", "本部门产品", "", "H5"],
            ["A-2", "写了本部门", "一部", "H5"],
            ["A-3", "写了别的部门", "二部", "H5"],
        ]
    )
    response = client.post("/api/v1/products/import", files={"file": ("p.xlsx", payload, "application/octet-stream")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 2 and body["failed_count"] == 1
    assert any("本部门" in item["message"] for item in body["errors"])
    departments = {item["project_code"]: item["department_name"] for item in repo.products.values()}
    assert departments == {"A-1": "一部", "A-2": "一部"}
    assert all(item["tenant_id"] == BRANCH_TENANT for item in repo.products.values())


@pytest.mark.asyncio
async def test_task_submission_pins_department_for_department_account() -> None:
    repository = FakeTaskRepository()
    service = TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=RedisAgentRunQueue(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_seconds=3600),
        state_store=InMemoryAgentRunStateStore(),
    )
    department_scope = AccessScope(
        actor_user_id="dept-user",
        tenant_id=BRANCH_TENANT,
        permissions=frozenset(DEPARTMENT_PERMISSIONS),
        department_name="一部",
    )
    await service.submit_task(
        access_scope=department_scope,
        target_url="https://example.com/h5",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=80,
        task_config={"product": {"department_name": "鼎丰", "product_code": "P-1", "product_name": "产品"}},
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
        loop_guard={"max_steps": 20},
    )
    assert repository.created[0]["task_config"]["product"]["department_name"] == "一部"

    # 公司级账号沿用产品快照里的部门
    company_scope = AccessScope(
        actor_user_id="company-admin",
        tenant_id=BRANCH_TENANT,
        permissions=frozenset({"taskObjects:read:tenant", "taskObjects:write:owner"}),
        department_name="一部",
    )
    await service.submit_task(
        access_scope=company_scope,
        target_url="https://example.com/h5",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=80,
        task_config={"product": {"department_name": "鼎丰", "product_code": "P-1", "product_name": "产品"}},
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
        loop_guard={"max_steps": 20},
    )
    assert repository.created[1]["task_config"]["product"]["department_name"] == "鼎丰"
