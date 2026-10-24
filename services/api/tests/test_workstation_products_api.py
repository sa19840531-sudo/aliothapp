from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import products as workstation_products_module
from services.api.app.main import create_app


@dataclass
class FakeProductRepository:
    products: list[dict] = field(default_factory=list)

    async def list_enabled_products(self, *, visible_tenant_id=None, visible_department=None) -> list[dict]:
        return [
            {
                "project_code": item["project_code"],
                "product_name": item["product_name"],
                "h5_url": item["h5_url"],
            }
            for item in self.products
        ]


@pytest.fixture
def client() -> TestClient:
    repo = FakeProductRepository(
        products=[
            {"project_code": "SF51", "product_name": "-ins成人版", "h5_url": "https://6dkwys8b.top/"},
            {"project_code": "SF69", "product_name": "麻豆免费版", "h5_url": "https://35.185.152.172:2469"},
        ]
    )
    app = create_app()
    app.dependency_overrides[workstation_products_module._get_product_repository] = lambda: repo
    return TestClient(app)


def test_list_workstation_products_returns_options(client: TestClient) -> None:
    response = client.get("/api/v1/workstation/products")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    first = body["items"][0]
    assert first["project_code"] == "SF51"
    assert first["product_name"] == "-ins成人版"
    assert first["h5_url"] == "https://6dkwys8b.top/"
    # 2.0 产品管理页新增的资料列：旧仓储没给时按缺省返回
    assert first["department_name"] is None
    assert first["owner_name"] is None
    assert first["platforms"] == []
    assert first["status"] == "enabled"
