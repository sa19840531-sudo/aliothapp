"""配置中心业务口径：写权限只归体验中心管理员、编辑只改四个业务字段、开关前校验可执行。"""

from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from services.api.app.api.v1 import rules as rules_module
from services.api.app.api.v1.auth import AuthUserResponse
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth
from services.api.tests.test_rules_api import FakeRuleRepository

RULESET_ID = "00000000-0000-0000-0000-00000000aa01"
RULE_ID = "00000000-0000-0000-0000-00000000bb01"


def _user(permissions: list[str]) -> AuthUserResponse:
    return AuthUserResponse(
        id="u-1",
        username="u1",
        full_name="测试账号",
        role="平台管理员" if "qcRules:write" in permissions else "规则配置员",
        status="enabled",
        permissions=permissions,
    )


def _client(repo: FakeRuleRepository, permissions: list[str]) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    user = _user(permissions)
    app.dependency_overrides[rules_module.require_auth_user] = lambda: user
    app.dependency_overrides[rules_module._get_rule_repository] = lambda: repo
    return TestClient(app)


def _admin(repo: FakeRuleRepository) -> TestClient:
    return _client(repo, ["qcRules", "qcRules:write"])


def _ruleset(**overrides) -> dict:
    row = {
        "id": RULESET_ID,
        "ruleset_code": "alioth_first_ruleset",
        "name": "首期规则集",
        "version": 1,
        "status": "enabled",
        "description": "首期",
        "source_type": "spreadsheet",
        "source_ref": "docs/data/seed.csv",
        "snapshot_hash": None,
        "rule_count": 73,
        "page_sampling_policy": {"max_pages": 100},
        "loop_guard": {"max_steps": 200, "max_ai_calls": 200},
        "created_by": None,
        "published_at": None,
        "archived_at": None,
        "created_at": "2026-09-01T10:00:00+00:00",
        "updated_at": "2026-09-01T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _rule(**overrides) -> dict:
    row = {
        "id": RULE_ID,
        "ruleset_id": RULESET_ID,
        "rule_code": "alioth_12",
        "name": "提示文案叹号结尾检查",
        "description": None,
        "category": "text",
        "executor_type": "hybrid",
        "severity": "medium",
        "status": "enabled",
        "scope": None,
        "channel": None,
        "page_type": "content_detail",
        "applicable_page_types": ["content_detail", "video_detail"],
        "url_pattern": None,
        "steps": [
            {"action": "navigate", "expected_page_type": "content_detail"},
            {"action": "navigate", "expected_page_type": "video_detail"},
        ],
        "input_contract": {"target_clients": ["H5"], "required_artifacts": ["screenshot"]},
        "check_point": "提示文字不应使用叹号结尾",
        "judge_config": {
            "llm_checks": ["精调过的判定标准"],
            "tool_refs": ["semantic_rule_judge_tool"],
            "deterministic_rules": ["标点检查"],
        },
        "manual_review_policy": {"min_confidence": 0.82},
        "sort_order": 120,
        "created_by": None,
        "created_at": "2026-09-01T10:00:00+00:00",
        "updated_at": "2026-09-01T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _last_call(repo: FakeRuleRepository, name: str) -> dict:
    return [payload for call, payload in repo.calls if call == name][-1]


def test_platform_rule_writes_are_admin_only_but_reading_stays_open() -> None:
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[_rule()])
    department = _client(repo, ["qcRules"])

    assert department.get("/api/v1/rulesets").status_code == 200
    assert department.get("/api/v1/rules").status_code == 200
    forbidden = [
        department.post("/api/v1/rulesets", json={"name": "新规则集"}),
        department.patch(f"/api/v1/rules/{RULE_ID}/status", json={"status": "disabled"}),
        department.patch(
            f"/api/v1/rules/{RULE_ID}",
            json={"name": "x", "check_point": "y", "applicable_page_types": ["home"]},
        ),
        department.delete(f"/api/v1/rules/{RULE_ID}"),
        department.post(f"/api/v1/rulesets/{RULESET_ID}/publish"),
    ]
    assert [item.status_code for item in forbidden] == [403] * len(forbidden)
    assert "体验中心管理员" in forbidden[0].json()["error"]["message"]
    writes = {"update_rule", "delete_rule", "create_ruleset"}
    assert not any(call in writes for call, _ in repo.calls)


def test_business_edit_keeps_system_fields_and_null_channel() -> None:
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[_rule()])
    client = _admin(repo)

    response = client.patch(
        f"/api/v1/rules/{RULE_ID}",
        json={
            "name": "提示文案结尾检查",
            "check_point": "提示文字结尾不要用叹号",
            "applicable_page_types": ["video_detail"],
            "severity": "high",
        },
    )

    assert response.status_code == 200, response.text
    written = _last_call(repo, "update_rule")
    assert written["name"] == "提示文案结尾检查"
    assert written["check_point"] == "提示文字结尾不要用叹号"
    assert written["severity"] == "high"
    assert written["applicable_page_types"] == ["video_detail"]
    # 系统字段原样保留：输入契约、渠道（空 = 不限）、精调过的判定标准
    assert written["input_contract"] == {
        "target_clients": ["H5"],
        "required_artifacts": ["screenshot"],
    }
    assert written["channel"] is None
    assert written["judge_config"]["llm_checks"] == ["精调过的判定标准"]
    # 页面收窄后，页面类型与步骤跟着收窄
    assert written["page_type"] == "video_detail"
    assert [step["expected_page_type"] for step in written["steps"]] == ["video_detail"]


def test_business_edit_resyncs_llm_checks_for_quick_created_rules() -> None:
    quick = _rule(
        executor_type="llm",
        check_point="首页不应出现错别字",
        judge_config={
            "llm_checks": ["首页不应出现错别字"],
            "tool_refs": ["semantic_rule_judge_tool"],
        },
    )
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[quick])
    client = _admin(repo)

    response = client.patch(
        f"/api/v1/rules/{RULE_ID}",
        json={
            "name": "错别字检查",
            "check_point": "首页和详情页不应出现错别字",
            "applicable_page_types": ["home"],
        },
    )

    assert response.status_code == 200, response.text
    written = _last_call(repo, "update_rule")
    assert written["judge_config"]["llm_checks"] == ["首页和详情页不应出现错别字"]


def test_enabling_an_unrunnable_rule_is_refused_in_plain_words() -> None:
    broken = _rule(status="disabled", judge_config={"strategy": "hybrid"})
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[broken])
    client = _admin(repo)

    refused = client.patch(f"/api/v1/rules/{RULE_ID}/status", json={"status": "enabled"})

    assert refused.status_code == 422
    assert "跑不起来" in refused.json()["error"]["message"]
    assert not any(call == "update_rule" for call, _ in repo.calls)


def test_switch_rule_off_and_on_keeps_every_other_field() -> None:
    original = _rule()
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[deepcopy(original)])
    client = _admin(repo)

    off = client.patch(f"/api/v1/rules/{RULE_ID}/status", json={"status": "disabled"})
    on = client.patch(f"/api/v1/rules/{RULE_ID}/status", json={"status": "enabled"})

    assert off.status_code == 200 and on.status_code == 200, (off.text, on.text)
    written = _last_call(repo, "update_rule")
    assert written["status"] == "enabled"
    for field in ("input_contract", "judge_config", "steps", "channel", "manual_review_policy"):
        assert written[field] == original[field]


def test_business_create_fills_system_fields_and_starts_disabled() -> None:
    repo = FakeRuleRepository(
        rulesets=[_ruleset()],
        rules=[_rule(rule_code="custom_002", sort_order=800)],
    )
    client = _admin(repo)

    response = client.post(
        f"/api/v1/rulesets/{RULESET_ID}/rules",
        json={
            "name": "首页错别字检查",
            "check_point": "首页文案不应出现错别字",
            "applicable_page_types": ["home"],
            "severity": "low",
        },
    )

    assert response.status_code == 201, response.text
    created = _last_call(repo, "create_rule")
    assert created["rule_code"] == "custom_003"
    assert created["status"] == "disabled"
    assert created["executor_type"] == "llm"
    assert created["channel"] is None
    assert created["sort_order"] == 810
    assert created["judge_config"]["llm_checks"] == ["首页文案不应出现错别字"]
    assert "semantic_rule_judge_tool" in created["judge_config"]["tool_refs"]
    assert created["page_type"] == "home"


def test_ruleset_business_edit_keeps_code_version_and_policies() -> None:
    repo = FakeRuleRepository(rulesets=[_ruleset(status="draft")], rules=[])
    client = _admin(repo)

    response = client.patch(
        f"/api/v1/rulesets/{RULESET_ID}",
        json={"name": "首期规则集（改名）", "description": "新说明", "status": "enabled"},
    )

    assert response.status_code == 200, response.text
    written = _last_call(repo, "update_ruleset")
    assert written["name"] == "首期规则集（改名）"
    assert written["status"] == "enabled"
    assert written["ruleset_code"] == "alioth_first_ruleset"
    assert written["version"] == 1
    assert written["source_ref"] == "docs/data/seed.csv"
    assert written["page_sampling_policy"] == {"max_pages": 100}
    assert written["loop_guard"] == {"max_steps": 200, "max_ai_calls": 200}


def test_new_ruleset_only_needs_a_name_and_inherits_live_policies() -> None:
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[])
    client = _admin(repo)

    response = client.post(
        "/api/v1/rulesets", json={"name": "运营二组规则集", "description": "二组"}
    )

    assert response.status_code == 201, response.text
    created = _last_call(repo, "create_ruleset")
    assert created["ruleset_code"].startswith("ruleset_")
    assert created["version"] == 1
    assert created["page_sampling_policy"] == {"max_pages": 100}
    assert created["loop_guard"] == {"max_steps": 200, "max_ai_calls": 200}


def test_listing_rules_without_ruleset_reads_across_rulesets() -> None:
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[_rule()])
    client = _admin(repo)

    response = client.get("/api/v1/rules?page=1&page_size=20")

    assert response.status_code == 200
    assert _last_call(repo, "list_rules")["ruleset_id"] is None
