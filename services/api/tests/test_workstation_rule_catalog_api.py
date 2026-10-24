"""已发布规则目录：规则中心与任务创建选择器共用的只读口径。"""

from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import rule_catalog as rule_catalog_module
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.page.web_page_catalog import WEB_PAGE_TYPE_CODES
from services.api.app.main import create_app


@dataclass
class FakeInspectionRepository:
    rulesets: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)

    async def list_published_rulesets(self) -> list[dict]:
        return self.rulesets

    async def list_published_rules(self) -> list[dict]:
        return self.rules


def _rule(code: str, pages: list[str]) -> dict:
    return {
        "rule_id": f"id-{code}",
        "rule_code": code,
        "name": f"规则 {code}",
        "category": "text",
        "severity": "high",
        "executor_type": "hybrid",
        "check_point": "检查点",
        "applicable_page_types": pages,
        "sort_order": 10,
        "ruleset_id": "rs-1",
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "source_type": "spreadsheet",
    }


def test_rule_catalog_lists_published_rulesets_rules_pages_and_scope_options() -> None:
    repo = FakeInspectionRepository(
        rulesets=[
            {
                "ruleset_id": "rs-1",
                "ruleset_code": "alioth_first_ruleset",
                "version": 1,
                "name": "首期规则集",
                "description": "",
                "status": "enabled",
            }
        ],
        rules=[_rule("alioth_6", ["home", "video_list"]), _rule("alioth_12", ["content_detail"])],
    )
    app = create_app()
    app.dependency_overrides[rule_catalog_module._get_inspection_repository] = lambda: repo

    response = TestClient(app).get("/api/v1/workstation/rule-catalog")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rulesets"][0]["enabled_rule_count"] == 2
    assert body["rulesets"][0]["rule_codes"] == ["alioth_6", "alioth_12"]
    assert body["rules"][0]["ruleset_id"] == "rs-1"
    assert body["rules"][0]["source_type"] == "spreadsheet"
    pages = {item["code"]: item for item in body["page_types"]}
    assert pages["home"]["related_rule_codes"] == ["alioth_6"]
    assert pages["content_detail"]["related_rule_codes"] == ["alioth_12"]
    registry = get_default_page_type_registry()
    # 页面范围只给后端认可的页型：H5 / App 为业务页型，Web 为四类
    assert body["page_scope_options"]["h5"] == list(registry.coverage_required_codes)
    assert body["page_scope_options"]["web"] == list(WEB_PAGE_TYPE_CODES)
    assert "line_loading" not in body["page_scope_options"]["app"]


def test_rule_catalog_keeps_exact_ruleset_version_and_unknown_source() -> None:
    original = _rule("same-code", ["home"])
    original.pop("source_type")
    newer = {
        **_rule("same-code", ["payment"]),
        "rule_id": "id-newer",
        "ruleset_id": "rs-2",
        "ruleset_version": 2,
        "source_type": "api",
    }
    repo = FakeInspectionRepository(
        rulesets=[
            {
                "ruleset_id": f"rs-{version}",
                "ruleset_code": "alioth_first_ruleset",
                "version": version,
                "name": f"第{version}版规则集",
                "description": "",
                "status": "enabled",
            }
            for version in (1, 2)
        ],
        rules=[original, newer],
    )
    app = create_app()
    app.dependency_overrides[rule_catalog_module._get_inspection_repository] = lambda: repo

    response = TestClient(app).get("/api/v1/workstation/rule-catalog")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["enabled_rule_count"] for item in body["rulesets"]] == [1, 1]
    assert [(item["ruleset_id"], item["ruleset_version"]) for item in body["rules"]] == [
        ("rs-1", 1), ("rs-2", 2),
    ]
    assert body["rules"][0]["source_type"] is None
    assert body["rules"][1]["source_type"] == "api"
