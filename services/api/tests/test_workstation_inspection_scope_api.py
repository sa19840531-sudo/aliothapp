from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import inspection_scope as inspection_scope_module
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
        ]
    )
    app = create_app()
    app.dependency_overrides[inspection_scope_module._get_inspection_repository] = lambda: repo
    return TestClient(app)


def test_inspection_scope_aggregates_pages_rules_and_stats(client: TestClient) -> None:
    response = client.get("/api/v1/workstation/inspection-scope")

    assert response.status_code == 200
    body = response.json()

    # 页面访问路径按 Catalog 顺序（home 在 content_detail、payment 之前），去重
    codes = [step["code"] for step in body["page_visit_path"]]
    assert codes == ["home", "content_detail", "payment"]

    assert body["stats"]["rule_count"] == 2
    assert body["stats"]["page_count"] == 3
    assert body["stats"]["estimated_minutes_low"] >= 10
    assert body["stats"]["estimated_minutes_high"] >= body["stats"]["estimated_minutes_low"]
    assert body["rules"][0]["rule_code"] == "alioth_6"
