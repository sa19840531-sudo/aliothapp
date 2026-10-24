"""产品 Excel 导入（2.1 原型 product-import.html）：xlsx 解析、所属公司编码、模板与归属校验。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import products as products_module
from services.api.app.application.spreadsheet import build_xlsx, read_sheet_rows
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
HQ_TENANT = "00000000-0000-0000-0000-000000000001"
BRANCH_TENANT = "00000000-0000-0000-0000-00000000000a"
OTHER_TENANT = "00000000-0000-0000-0000-00000000000b"


@dataclass
class FakeProductRepository:
    products: dict = field(default_factory=dict)
    records: list = field(default_factory=list)

    async def find_by_code(self, project_code: str):
        item = self.products.get(project_code)
        return dict(item) if item is not None else None

    async def upsert_product(self, payload: dict):
        inserted = payload["project_code"] not in self.products
        self.products[payload["project_code"]] = dict(payload)
        return dict(payload), inserted

    async def record_import(self, payload: dict):
        item = {"id": f"record-{len(self.records) + 1}", "batch_no": "IMP-20260908-001", **payload}
        self.records.append(item)
        return item

    async def list_import_records(self, *, tenant_id, limit=50):
        return [{**item, "tenant_name": "北京分公司", "created_at": NOW} for item in self.records]


@dataclass
class FakeTenantRepository:
    async def list_tenants(self):
        return [
            {"id": HQ_TENANT, "tenant_key": "default", "display_name": "体验中心"},
            {"id": BRANCH_TENANT, "tenant_key": "c-1001", "display_name": "北京分公司"},
        ]


def _user(user_id: str, tenant_id: str, permissions: list[str]):
    return auth_module.AuthUserResponse(
        id=user_id,
        username=user_id,
        full_name=f"用户 {user_id}",
        role="平台管理员" if "taskObjects:read:any" in permissions else "公司管理员",
        status="enabled",
        permissions=permissions,
        tenant_id=tenant_id,
        tenant_name="北京分公司" if tenant_id == BRANCH_TENANT else "体验中心",
    )


HQ_ADMIN = _user("hq", HQ_TENANT, ["qcRules", "taskObjects:read:any"])
COMPANY_ADMIN = _user("branch", BRANCH_TENANT, ["qcRules", "taskObjects:read:tenant"])


def _client(user, repo: FakeProductRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[auth_module.require_auth_user] = lambda: user
    app.dependency_overrides[products_module._get_product_repository] = lambda: repo
    app.dependency_overrides[products_module._get_tenant_repository] = FakeTenantRepository
    return TestClient(app)


def _xlsx(rows: list[list[object]]) -> bytes:
    return build_xlsx([["产品编码", "产品名称", "所属公司编码", "所属部门", "平台类型", "负责人", "H5地址", "状态"], *rows])


def test_hq_xlsx_import_resolves_company_codes_and_reports_unknown_ones() -> None:
    repo = FakeProductRepository(products={"OLD-1": {"project_code": "OLD-1", "tenant_id": OTHER_TENANT}})
    client = _client(HQ_ADMIN, repo)
    payload = _xlsx(
        [
            ["MB-001", "手机银行", "c-1001", "一部", "Android / iOS", "小明", "", "启用"],
            ["H5-001", "H5 活动页", "北京分公司", "二部", "H5", "", "https://example.com/h5", ""],
            ["SHARED-1", "共享产品", "", "体验部", "Web", "", "", "启用"],
            ["BAD-1", "公司不存在", "c-9999", "一部", "H5", "", "", ""],
            ["OLD-1", "别家的编码", "c-1001", "一部", "H5", "", "", ""],
        ]
    )
    response = client.post(
        "/api/v1/products/import",
        files={"file": ("产品导入.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 3
    assert body["failed_count"] == 2
    assert body["batch_no"] == "IMP-20260908-001"
    messages = {item["row"]: item["message"] for item in body["errors"]}
    assert "c-9999" in messages[5]
    assert "其它公司" in messages[6]
    assert repo.products["MB-001"]["tenant_id"] == BRANCH_TENANT
    assert repo.products["MB-001"]["platforms"] == ["app", "ios"]
    assert repo.products["H5-001"]["tenant_id"] == BRANCH_TENANT
    assert repo.products["SHARED-1"]["tenant_id"] is None
    assert repo.products["SHARED-1"]["platforms"] == ["web"]


def test_company_import_is_pinned_to_own_company() -> None:
    repo = FakeProductRepository(products={"SHARED-1": {"project_code": "SHARED-1", "tenant_id": None}})
    client = _client(COMPANY_ADMIN, repo)
    payload = _xlsx(
        [
            ["MB-002", "本公司产品", "", "一部", "Android", "", "", "启用"],
            ["MB-003", "写了别家公司", "c-9999", "一部", "Android", "", "", "启用"],
            ["SHARED-1", "想覆盖共享产品", "", "一部", "H5", "", "", "启用"],
        ]
    )
    response = client.post("/api/v1/products/import", files={"file": ("p.xlsx", payload, "application/octet-stream")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 1 and body["failed_count"] == 2
    assert repo.products["MB-002"]["tenant_id"] == BRANCH_TENANT
    assert "MB-003" not in repo.products
    assert repo.products["SHARED-1"]["tenant_id"] is None
    assert any("不是本公司" in item["message"] for item in body["errors"])
    assert any("共享产品" in item["message"] for item in body["errors"])


def test_import_template_is_a_real_xlsx_and_xls_is_rejected() -> None:
    client = _client(HQ_ADMIN, FakeProductRepository())
    template = client.get("/api/v1/products/import-template")
    assert template.status_code == 200
    assert template.headers["content-type"].startswith("application/vnd.openxmlformats")
    rows = read_sheet_rows(template.content)
    assert rows[0] == ["产品编码", "产品名称", "所属公司编码", "所属部门", "平台类型", "负责人", "H5地址", "状态"]
    assert any("iOS" in cell for row in rows[1:] for cell in row)

    legacy = client.post("/api/v1/products/import", files={"file": ("p.xls", b"\xd0\xcf\x11\xe0", "application/octet-stream")})
    assert legacy.status_code == 400
    assert ".xlsx" in legacy.json()["error"]["message"]
