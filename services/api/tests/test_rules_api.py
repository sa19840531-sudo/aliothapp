from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.app.api.v1 import rules as rules_module
from services.api.app.api.v1.auth import AuthUserResponse
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth


@dataclass
class FakeRuleRepository:
    rulesets: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def list_rulesets(self, *, keyword, status, page, page_size):
        self.calls.append(
            (
                "list_rulesets",
                {
                    "keyword": keyword,
                    "status": status,
                    "page": page,
                    "page_size": page_size,
                },
            )
        )
        return self.rulesets, len(self.rulesets)

    async def create_ruleset(self, payload):
        self.calls.append(("create_ruleset", payload))
        created = {
            "id": "ruleset-1",
            "ruleset_code": payload["ruleset_code"],
            "name": payload["name"],
            "version": payload["version"],
            "status": payload["status"],
            "description": payload.get("description") or None,
            "source_type": payload["source_type"],
            "source_ref": payload.get("source_ref") or None,
            "snapshot_hash": None,
            "rule_count": 0,
            "page_sampling_policy": payload.get("page_sampling_policy") or {},
            "loop_guard": payload.get("loop_guard") or {},
            "created_by": None,
            "published_at": None,
            "archived_at": None,
            "created_at": "2026-07-08T10:00:00+00:00",
            "updated_at": "2026-07-08T10:00:00+00:00",
        }
        self.rulesets.append(created)
        return created

    async def get_ruleset(self, ruleset_id):
        self.calls.append(("get_ruleset", {"ruleset_id": ruleset_id}))
        return next((item for item in self.rulesets if item["id"] == ruleset_id), None)

    async def update_ruleset(self, ruleset_id, payload):
        self.calls.append(("update_ruleset", {"ruleset_id": ruleset_id, **payload}))
        for item in self.rulesets:
            if item["id"] == ruleset_id:
                item.update({
                    "ruleset_code": payload["ruleset_code"],
                    "name": payload["name"],
                    "version": payload["version"],
                    "status": payload["status"],
                    "description": payload.get("description") or None,
                    "source_type": payload["source_type"],
                    "source_ref": payload.get("source_ref") or None,
                    "page_sampling_policy": payload.get("page_sampling_policy") or {},
                    "loop_guard": payload.get("loop_guard") or {},
                    "updated_at": "2026-07-08T10:05:00+00:00",
                })
                return item
        return None

    async def delete_ruleset(self, ruleset_id):
        self.calls.append(("delete_ruleset", {"ruleset_id": ruleset_id}))
        original = len(self.rulesets)
        self.rulesets = [item for item in self.rulesets if item["id"] != ruleset_id]
        return len(self.rulesets) != original

    async def publish_ruleset(self, ruleset_id):
        self.calls.append(("publish_ruleset", {"ruleset_id": ruleset_id}))
        for item in self.rulesets:
            if item["id"] == ruleset_id:
                item["status"] = "enabled"
                item["published_at"] = "2026-07-08T10:06:00+00:00"
                return item
        return None

    async def list_rules(
        self, *, ruleset_id, keyword, category, status, page, page_size,
        severity=None, source_type=None, page_type=None,
    ):
        self.calls.append(
            (
                "list_rules",
                {
                    "ruleset_id": ruleset_id,
                    "keyword": keyword,
                    "category": category,
                    "status": status,
                    "severity": severity,
                    "source_type": source_type,
                    "page_type": page_type,
                    "page": page,
                    "page_size": page_size,
                },
            )
        )
        return self.rules, len(self.rules)

    async def create_rule(self, payload):
        self.calls.append(("create_rule", payload))
        created = {
            "id": "rule-1",
            **payload,
            "description": payload.get("description") or None,
            "scope": payload.get("scope") or None,
            "channel": payload.get("channel") or None,
            "page_type": payload.get("page_type") or None,
            "url_pattern": payload.get("url_pattern") or None,
            "created_by": None,
            "created_at": "2026-07-08T10:00:00+00:00",
            "updated_at": "2026-07-08T10:00:00+00:00",
        }
        self.rules.append(created)
        return created

    async def get_rule(self, rule_id):
        self.calls.append(("get_rule", {"rule_id": rule_id}))
        return next((item for item in self.rules if item["id"] == rule_id), None)

    async def update_rule(self, rule_id, payload):
        self.calls.append(("update_rule", {"rule_id": rule_id, **payload}))
        for item in self.rules:
            if item["id"] == rule_id:
                item.update(payload)
                item["updated_at"] = "2026-07-08T10:05:00+00:00"
                return item
        return None

    async def delete_rule(self, rule_id):
        self.calls.append(("delete_rule", {"rule_id": rule_id}))
        original = len(self.rules)
        self.rules = [item for item in self.rules if item["id"] != rule_id]
        return len(self.rules) != original


def build_client(repo: FakeRuleRepository) -> TestClient:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[rules_module._get_rule_repository] = lambda: repo
    admin = AuthUserResponse(
        id="test-user",
        username="admin",
        full_name="系统管理员",
        role="平台管理员",
        status="enabled",
        permissions=[
            "dashboard", "users", "roles", "auditLogs", "qcRules", "qcRules:write", "configCenter",
        ],
    )
    app.dependency_overrides[rules_module._require_rules_permission] = lambda: admin
    app.dependency_overrides[rules_module._require_rules_write_permission] = lambda: admin
    return TestClient(app)


def test_list_rulesets_returns_paged_items() -> None:
    repo = FakeRuleRepository(
        rulesets=[
            {
                "id": "ruleset-1",
                "ruleset_code": "alioth_first_ruleset",
                "name": "首版规则集",
                "version": 1,
                "status": "draft",
                "description": None,
                "source_type": "manual",
                "source_ref": None,
                "snapshot_hash": None,
                "rule_count": 1,
                "page_sampling_policy": {},
                "loop_guard": {},
                "created_by": None,
                "published_at": None,
                "archived_at": None,
                "created_at": "2026-07-08T10:00:00+00:00",
                "updated_at": "2026-07-08T10:00:00+00:00",
            }
        ]
    )
    client = build_client(repo)

    response = client.get("/api/v1/rulesets?keyword=首版&page=1&page_size=10")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["ruleset_code"] == "alioth_first_ruleset"
    assert repo.calls[0][0] == "list_rulesets"


def test_list_rulesets_accepts_datetime_timestamps() -> None:
    ts = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    repo = FakeRuleRepository(
        rulesets=[
            {
                "id": "ruleset-1",
                "ruleset_code": "alioth_first_ruleset",
                "name": "首版规则集",
                "version": 1,
                "status": "draft",
                "description": None,
                "source_type": "manual",
                "source_ref": None,
                "snapshot_hash": None,
                "rule_count": 1,
                "page_sampling_policy": {},
                "loop_guard": {},
                "created_by": None,
                "published_at": None,
                "archived_at": None,
                "created_at": ts,
                "updated_at": ts,
            }
        ]
    )
    client = build_client(repo)

    response = client.get("/api/v1/rulesets?page=1&page_size=8")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["created_at"] == "2026-07-08T10:00:00Z"


def test_ruleset_crud_and_publish_flow() -> None:
    repo = FakeRuleRepository()
    client = build_client(repo)

    create_response = client.post(
        "/api/v1/rulesets",
        json={
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集",
            "version": 1,
            "status": "draft",
            "description": "首版",
            "source_type": "manual",
            "source_ref": "",
            "page_sampling_policy": {"mode": "first_n"},
            "loop_guard": {"max_steps": 30},
        },
    )
    assert create_response.status_code == 201
    ruleset_id = create_response.json()["id"]

    update_response = client.put(
        f"/api/v1/rulesets/{ruleset_id}",
        json={
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集 v2",
            "version": 2,
            "status": "draft",
            "description": "更新后",
            "source_type": "manual",
            "source_ref": "",
            "page_sampling_policy": {},
            "loop_guard": {},
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "首版规则集 v2"

    publish_response = client.post(f"/api/v1/rulesets/{ruleset_id}/publish")
    assert publish_response.status_code == 200
    assert publish_response.json()["status"] == "enabled"

    delete_response = client.delete(f"/api/v1/rulesets/{ruleset_id}")
    assert delete_response.status_code == 204
    assert delete_response.content == b""


def test_rules_crud_flow_uses_repository_override() -> None:
    repo = FakeRuleRepository(
        rulesets=[
            {
                "id": "ruleset-1",
                "ruleset_code": "alioth_first_ruleset",
                "name": "首版规则集",
                "version": 1,
                "status": "enabled",
                "description": None,
                "source_type": "manual",
                "source_ref": None,
                "snapshot_hash": None,
                "rule_count": 1,
                "page_sampling_policy": {},
                "loop_guard": {},
                "created_by": None,
                "published_at": None,
                "archived_at": None,
                "created_at": "2026-07-08T10:00:00+00:00",
                "updated_at": "2026-07-08T10:00:00+00:00",
            }
        ]
    )
    client = build_client(repo)

    create_response = client.post(
        "/api/v1/rules",
        json={
            "ruleset_id": "ruleset-1",
            "rule_code": "alioth_rule_001",
            "name": "规则 1",
            "description": "",
            "category": "text",
            "executor_type": "hybrid",
            "severity": "medium",
            "status": "draft",
            "scope": "",
            "channel": "workbench",
            "page_type": "",
            "applicable_page_types": ["home"],
            "url_pattern": "",
            "steps": [{"step": "检查"}],
            "input_contract": {},
            "check_point": "检查首页文案",
            "judge_config": {"strategy": "hybrid"},
            "manual_review_policy": {"mode": "auto"},
            "sort_order": 100,
        },
    )
    assert create_response.status_code == 201
    rule_id = create_response.json()["id"]

    list_response = client.get("/api/v1/rules?ruleset_id=ruleset-1&page=1&page_size=10")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1

    update_response = client.put(
        f"/api/v1/rules/{rule_id}",
        json={
            "ruleset_id": "ruleset-1",
            "rule_code": "alioth_rule_001",
            "name": "规则 1-更新",
            "description": "更新后",
            "category": "text",
            "executor_type": "hybrid",
            "severity": "high",
            "status": "enabled",
            "scope": "",
            "channel": "workbench",
            "page_type": "",
            "applicable_page_types": ["home"],
            "url_pattern": "",
            "steps": [{"step": "检查"}],
            "input_contract": {},
            "check_point": "检查首页文案",
            "judge_config": {
                "llm_checks": ["检查首页文案"],
                "tool_refs": ["semantic_rule_judge_tool"],
                "deterministic_rules": ["标点检查"],
            },
            "manual_review_policy": {"mode": "auto"},
            "sort_order": 90,
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "规则 1-更新"

    delete_response = client.delete(f"/api/v1/rules/{rule_id}")
    assert delete_response.status_code == 204
    assert delete_response.content == b""


def test_missing_ruleset_and_rule_return_404() -> None:
    repo = FakeRuleRepository()
    client = build_client(repo)

    ruleset_response = client.get("/api/v1/rulesets/missing")
    rule_response = client.get("/api/v1/rules/missing")

    assert ruleset_response.status_code == 404
    assert ruleset_response.json()["error"]["code"] == "AliothError"
    assert rule_response.status_code == 404
    assert rule_response.json()["error"]["code"] == "AliothError"


def test_enabled_aggregate_rule_requires_executable_scope_configuration() -> None:
    client = build_client(FakeRuleRepository())
    response = client.post(
        "/api/v1/rules",
        json={
            "ruleset_id": "ruleset-1",
            "rule_code": "aggregate-invalid",
            "name": "非法聚合规则",
            "category": "interaction",
            "executor_type": "hybrid",
            "status": "enabled",
            "applicable_page_types": ["profile"],
            "check_point": "检查任务内样式",
            "judge_config": {
                "capability_config": {
                    "evidence_input_scope": "task",
                    "evidence_scope_key": "unknown_strategy",
                }
            },
        },
    )

    assert response.status_code == 422


def test_ruleset_publish_rejects_unexecutable_aggregate_rule_before_mutation() -> None:
    repo = FakeRuleRepository(
        rulesets=[
            {
                "id": "ruleset-1",
                "ruleset_code": "ruleset",
                "name": "规则集",
                "version": 1,
                "status": "draft",
                "description": None,
                "source_type": "manual",
                "source_ref": None,
                "snapshot_hash": None,
                "rule_count": 1,
                "page_sampling_policy": {},
                "loop_guard": {},
                "created_by": None,
                "published_at": None,
                "archived_at": None,
                "created_at": "2026-08-18T10:00:00+00:00",
                "updated_at": "2026-08-18T10:00:00+00:00",
            }
        ],
        rules=[
            {
                "judge_config": {
                    "capability_config": {
                        "evidence_input_scope": "task",
                        "evidence_scope_key": "unknown_strategy",
                    }
                }
            }
        ],
    )
    client = build_client(repo)

    response = client.post("/api/v1/rulesets/ruleset-1/publish")

    assert response.status_code == 422
    assert not any(name == "publish_ruleset" for name, _ in repo.calls)


def test_rule_payload_rejects_non_catalog_page_types() -> None:
    client = build_client(FakeRuleRepository())
    payload = {
        "ruleset_id": "ruleset-1",
        "rule_code": "alioth_invalid",
        "name": "非法页面类型规则",
        "category": "text",
        "executor_type": "llm",
        "status": "enabled",
        "page_type": "ad_popup",
        "applicable_page_types": ["home"],
        "steps": [{"expected_page_type": "comment"}],
        "check_point": "检查页面",
    }

    response = client.post("/api/v1/rules", json=payload)

    assert response.status_code == 422
    assert "未知页面类型代码" in response.text


def test_rule_list_forwards_combined_filters_and_rejects_invalid_risk() -> None:
    repo = FakeRuleRepository()
    client = build_client(repo)
    response = client.get("/api/v1/rules", params={
        "ruleset_id": "rs2", "severity": "high", "source_type": "spreadsheet",
        "page_type": " home ", "page": 2, "page_size": 20,
    })
    assert response.status_code == 200, response.text
    assert repo.calls[-1][1] == {
        "ruleset_id": "rs2", "keyword": None, "category": None, "status": None,
        "severity": "high", "source_type": "spreadsheet", "page_type": "home",
        "page": 2, "page_size": 20,
    }
    invalid = client.get("/api/v1/rules", params={"severity": "very_high"})
    assert invalid.status_code == 422
