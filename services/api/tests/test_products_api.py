from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import products as products_module
from services.api.app.api.v1.auth import hash_password
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth


@dataclass
class FakeUserRepository:
    users: list[dict] = field(default_factory=list)

    async def find_by_username(self, username: str):
        return next((item for item in self.users if item["username"] == username), None)

    async def touch_last_login(self, user_id: str):
        return None


@dataclass
class FakeProductRepository:
    products: list[dict] = field(default_factory=list)

    async def list_products(self, *, keyword, status, page, page_size, **_filters):
        filtered = self.products
        if keyword:
            filtered = [
                item
                for item in filtered
                if keyword in item["project_code"] or keyword in item["product_name"]
            ]
        if status:
            filtered = [item for item in filtered if item["status"] == status]
        start = (page - 1) * page_size
        end = start + page_size
        return [self._to_response(item) for item in filtered[start:end]], len(filtered)

    async def create_product(self, payload):
        if any(item["project_code"] == payload["project_code"] for item in self.products):
            import asyncpg

            raise asyncpg.UniqueViolationError("duplicate project_code")
        item = {
            "id": f"product-{len(self.products) + 1}",
            "project_code": payload["project_code"],
            "product_name": payload["product_name"],
            "h5_url": payload.get("h5_url"),
            "status": payload["status"],
            "tenant_id": payload.get("tenant_id"),
            "created_at": "2026-07-16T08:00:00+00:00",
            "updated_at": "2026-07-16T08:00:00+00:00",
        }
        self.products.append(item)
        return self._to_response(item)

    async def update_product(self, product_id: str, payload):
        for item in self.products:
            if item["id"] == product_id:
                item["project_code"] = payload["project_code"]
                item["product_name"] = payload["product_name"]
                item["h5_url"] = payload.get("h5_url")
                item["status"] = payload["status"]
                item["updated_at"] = "2026-07-16T09:00:00+00:00"
                return self._to_response(item)
        return None

    async def find_by_id(self, product_id: str):
        return next((self._to_response(item) for item in self.products if item["id"] == product_id), None)

    async def delete_product(self, product_id: str):
        original = len(self.products)
        self.products = [item for item in self.products if item["id"] != product_id]
        return len(self.products) != original

    @staticmethod
    def _to_response(item: dict) -> dict:
        return {
            "id": item["id"],
            "project_code": item["project_code"],
            "product_name": item["product_name"],
            "h5_url": item.get("h5_url"),
            "status": item["status"],
            "tenant_id": item.get("tenant_id"),
            "created_at": item.get("created_at", "2026-07-16T08:00:00+00:00"),
            "updated_at": item.get("updated_at", "2026-07-16T08:00:00+00:00"),
        }


def _build_client(user_repo: FakeUserRepository, product_repo: FakeProductRepository) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[auth_module._get_user_repository] = lambda: user_repo
    app.dependency_overrides[products_module._get_product_repository] = lambda: product_repo
    return TestClient(app)


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response.json()["token"]


def _config_user(username: str, password: str) -> dict:
    return {
        "id": f"user-{username}",
        "username": username,
        "full_name": "规则配置员",
        "password_hash": hash_password(password),
        "role": "规则配置员",
        "status": "enabled",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
    }


def test_products_crud_flow() -> None:
    user_repo = FakeUserRepository(users=[_config_user("editor", "secret")])
    product_repo = FakeProductRepository()
    client = _build_client(user_repo, product_repo)
    token = _login(client, "editor", "secret")
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post(
        "/api/v1/products",
        headers=headers,
        json={
            "project_code": "SF51",
            "product_name": "-ins成人版",
            "h5_url": "https://6dkwys8b.top/",
            "status": "enabled",
        },
    )
    assert create_response.status_code == 201
    product_id = create_response.json()["id"]
    assert create_response.json()["h5_url"] == "https://6dkwys8b.top/"

    dup_response = client.post(
        "/api/v1/products",
        headers=headers,
        json={"project_code": "SF51", "product_name": "重复编号"},
    )
    assert dup_response.status_code == 409

    list_response = client.get("/api/v1/products?page=1&page_size=10", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1

    update_response = client.put(
        f"/api/v1/products/{product_id}",
        headers=headers,
        json={
            "project_code": "SF51",
            "product_name": "-ins成人版",
            "h5_url": "",
            "status": "disabled",
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "disabled"
    # 空字符串 H5 归一化为 null
    assert update_response.json()["h5_url"] is None

    delete_response = client.delete(f"/api/v1/products/{product_id}", headers=headers)
    assert delete_response.status_code == 204
    assert delete_response.content == b""


def test_products_requires_authentication() -> None:
    # 与 rules.py 一致：产品接口按 qcRules 权限收口（三种角色均具备），
    # 未携带登录令牌时返回 401。
    client = _build_client(FakeUserRepository(users=[]), FakeProductRepository())

    response = client.get("/api/v1/products")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "缺少登录令牌"


def test_department_account_cannot_delete_other_company_or_shared_products() -> None:
    # 删除与修改同一套归属校验：共享产品只有运营中心能删，其它公司的产品不能删
    user_repo = FakeUserRepository(users=[_config_user("editor", "secret")])
    product_repo = FakeProductRepository(
        products=[
            {
                "id": "product-other",
                "project_code": "OTHER-1",
                "product_name": "别家公司的产品",
                "status": "enabled",
                "tenant_id": "00000000-0000-0000-0000-00000000000b",
            },
            {
                "id": "product-shared",
                "project_code": "SHARED-1",
                "product_name": "共享产品",
                "status": "enabled",
                "tenant_id": None,
            },
        ]
    )
    client = _build_client(user_repo, product_repo)
    headers = {"Authorization": f"Bearer {_login(client, 'editor', 'secret')}"}

    other = client.delete("/api/v1/products/product-other", headers=headers)
    shared = client.delete("/api/v1/products/product-shared", headers=headers)

    assert other.status_code == 403
    assert shared.status_code == 403
    assert len(product_repo.products) == 2
