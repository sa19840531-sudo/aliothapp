from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import page_config as page_config_module
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.main import create_app


@dataclass
class FakeInspectionRepository:
    rules: list[dict] = field(default_factory=list)

    async def list_enabled_rules(self, *, ruleset_code: str, version: int) -> list[dict]:
        return self.rules

    async def list_rules_in_scope(
        self, *, ruleset_code: str | None, version: int | None
    ) -> list[dict]:
        # 不指定规则集时按全部已发布规则统计（规则集拆分后的默认口径）
        return self.rules


@pytest.fixture
def client() -> TestClient:
    repo = FakeInspectionRepository(
        rules=[
            {
                "rule_code": "alioth_6",
                "name": "技术错误直出检查",
                "category": "visual",
                "severity": "high",
                "applicable_page_types": ["home", "content_detail"],
            },
            {
                "rule_code": "alioth_14",
                "name": "优惠价低于原价检查",
                "category": "payment",
                "severity": "high",
                "applicable_page_types": ["payment", "content_detail"],
            },
            {
                # 旧种子残留的伪类型：不匹配任何 Catalog 页面，仅计入规则总数
                "rule_code": "alioth_16",
                "name": "广告全局检查",
                "category": "ad",
                "severity": "critical",
                "applicable_page_types": ["global"],
            },
        ]
    )
    app = create_app()
    app.dependency_overrides[page_config_module._get_inspection_repository] = lambda: repo
    return TestClient(app)


def test_page_config_lists_catalog_pages_with_rule_bindings(client: TestClient) -> None:
    response = client.get("/api/v1/workstation/page-config")

    assert response.status_code == 200
    body = response.json()

    registry = get_default_page_type_registry()
    catalog = registry.catalog
    assert body["catalog_version"] == catalog.version
    assert body["catalog_sha256"] == registry.content_sha256
    # 页面清单与 Catalog 顺序、数量一致
    assert [page["code"] for page in body["pages"]] == [
        page_type.code for page_type in catalog.page_types
    ]

    by_code = {page["code"]: page for page in body["pages"]}
    assert by_code["home"]["bound_rule_codes"] == ["alioth_6"]
    assert by_code["content_detail"]["bound_rule_codes"] == ["alioth_6", "alioth_14"]
    assert by_code["payment"]["bound_rule_count"] == 1
    # global 伪类型不参与任何页面绑定
    assert all("alioth_16" not in page["bound_rule_codes"] for page in body["pages"])

    # 识别特征 = ocr/dom/layout 信号合计，说明文案来自 layout_hints
    payment_type = catalog.require_code("payment")
    expected_features = (
        len(payment_type.signals.ocr_keywords)
        + len(payment_type.signals.dom_hints)
        + len(payment_type.signals.layout_hints)
    )
    assert by_code["payment"]["feature_count"] == expected_features
    assert by_code["payment"]["description"] == "、".join(payment_type.signals.layout_hints)

    stats = body["stats"]
    assert stats["page_type_total"] == len(catalog.page_types)
    assert stats["enabled_rule_total"] == 3
    assert stats["rule_binding_total"] == 4
    assert stats["covered_page_total"] == 3
    assert stats["feature_total"] == sum(page["feature_count"] for page in body["pages"])
