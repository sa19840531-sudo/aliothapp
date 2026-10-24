"""安装器只走正式 API；验证新版本幂等、严格冲突和旧行不被更改。"""

import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from scripts.install_green_ruleset import GreenRulesetInstallationError, install_green_ruleset
from services.api.app.application.green_rule_catalog import (
    GreenRuleConfigurationError,
    build_green_ruleset_manifest,
)


class ControlledManagementApi:
    """受控管理接口，记录每次读写；不连接数据库或主环境。"""

    def __init__(self):
        old_id = str(uuid4())
        self.old_id = old_id
        self.rulesets = {
            old_id: {
                "id": old_id,
                "ruleset_code": "alioth_green_ruleset",
                "version": 3,
                "name": "历史规则集",
                "status": "enabled",
                "snapshot_hash": "old-snapshot",
            }
        }
        self.rules = {
            "old-rule": {
                "id": "old-rule",
                "ruleset_id": old_id,
                "rule_code": "alioth_green_s35",
                "check_point": "历史正文",
            }
        }
        self.calls = []
        self.reject_reads = False
        self.race_code = None

    def transport(self, request):
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append((request.method, path))
        data = json.loads(request.content) if request.content else None
        if request.method == "GET" and self.reject_reads:
            return httpx.Response(403, json={"detail": "无权限"})
        if request.method == "GET" and path == "/rulesets":
            rows = list(self.rulesets.values())
            return httpx.Response(200, json={"items": rows, "total": len(rows)})
        if request.method == "GET" and path == "/rules":
            rows = [
                row
                for row in self.rules.values()
                if row["ruleset_id"] == request.url.params["ruleset_id"]
            ]
            return httpx.Response(200, json={"items": rows, "total": len(rows)})
        if request.method == "GET" and path.startswith("/rulesets/"):
            return httpx.Response(200, json=self.rulesets[path.split("/")[2]])
        if request.method == "POST" and path == "/rulesets":
            identifier = str(uuid4())
            row = {**data, "id": identifier, "snapshot_hash": None}
            self.rulesets[identifier] = row
            return httpx.Response(201, json=row)
        if request.method == "POST" and path == "/rules":
            identifier = str(uuid4())
            row = {**data, "id": identifier}
            self.rules[identifier] = row
            if data["rule_code"] == self.race_code:
                self.race_code = None
                return httpx.Response(409, json={"detail": "并发同值创建"})
            return httpx.Response(201, json=row)
        if request.method == "POST" and path.endswith("/publish"):
            identifier = path.split("/")[2]
            row = self.rulesets[identifier]
            row.update(status="enabled", snapshot_hash="a" * 64)
            return httpx.Response(200, json=row)
        raise AssertionError(f"安装器调用了未授权路径：{request.method} {path}")

    def client(self):
        return httpx.AsyncClient(
            base_url="http://controlled.test/api/v1", transport=httpx.MockTransport(self.transport)
        )


async def test_default_preview_performs_no_network_or_publication():
    api = ControlledManagementApi()
    async with api.client() as client:
        result = await install_green_ruleset(client)
    assert result["action"] == "dry_run" and len(result["manifest"]["rules"]) == 8
    assert api.calls == []


async def test_two_installs_are_idempotent_and_do_not_change_old_rows():
    api = ControlledManagementApi()
    old_rulesets, old_rules = deepcopy(api.rulesets), deepcopy(api.rules)
    async with api.client() as client:
        first = await install_green_ruleset(client, apply=True)
        assert first["status"] == "draft" and len(first["created_rule_codes"]) == 8
        api.calls.clear()
        second = await install_green_ruleset(client, apply=True)
    assert second["ruleset_id"] == first["ruleset_id"]
    assert second["created_ruleset"] is False and second["created_rule_codes"] == []
    assert all(method == "GET" for method, _ in api.calls)
    assert api.rulesets[api.old_id] == old_rulesets[api.old_id]
    assert api.rules["old-rule"] == old_rules["old-rule"]
    assert all(
        row["status"] == "disabled"
        for row in api.rules.values()
        if row["ruleset_id"] == first["ruleset_id"]
    )


@pytest.mark.parametrize("fault", ["edited_rule", "enabled_rule", "extra_rule", "edited_ruleset"])
async def test_existing_same_version_conflict_blocks_before_any_mutation(fault):
    api = ControlledManagementApi()
    async with api.client() as client:
        first = await install_green_ruleset(client, apply=True)
        identifier = first["ruleset_id"]
        row = next(row for row in api.rules.values() if row["ruleset_id"] == identifier)
        if fault == "edited_rule":
            row["check_point"] = "人工更改后的业务正文"
        elif fault == "enabled_rule":
            row["status"] = "enabled"
        elif fault == "extra_rule":
            api.rules["unrelated"] = {"ruleset_id": identifier, "rule_code": "custom_new"}
        else:
            api.rulesets[identifier]["name"] = "人工改名"
        before = deepcopy((api.rulesets, api.rules))
        api.calls.clear()
        with pytest.raises(GreenRulesetInstallationError):
            await install_green_ruleset(client, apply=True, publish=True)
    assert (api.rulesets, api.rules) == before
    assert all(method == "GET" for method, _ in api.calls)


async def test_partial_same_value_install_only_creates_missing_rule():
    api = ControlledManagementApi()
    async with api.client() as client:
        first = await install_green_ruleset(client, apply=True)
        identifier = next(
            key for key, row in api.rules.items() if row["ruleset_id"] == first["ruleset_id"]
        )
        missing_code = api.rules.pop(identifier)["rule_code"]
        api.calls.clear()
        result = await install_green_ruleset(client, apply=True)
    assert result["created_rule_codes"] == [missing_code]
    assert [(method, path) for method, path in api.calls if method != "GET"] == [("POST", "/rules")]


async def test_same_value_create_race_is_read_back_not_overwritten():
    api = ControlledManagementApi()
    api.race_code = "alioth_green_s35"
    async with api.client() as client:
        result = await install_green_ruleset(client, apply=True)
    assert result["rule_count"] == 8
    assert all(method in ("GET", "POST") for method, _ in api.calls)


async def test_publication_requires_explicit_flag_and_never_enables_individual_rules():
    api = ControlledManagementApi()
    async with api.client() as client:
        result = await install_green_ruleset(client, apply=True, publish=True)
        assert result["status"] == "enabled" and result["snapshot_hash"]
        api.calls.clear()
        again = await install_green_ruleset(client, apply=True, publish=True)
    assert again["created_rule_codes"] == []
    assert all(method == "GET" for method, _ in api.calls)
    assert all(
        row["status"] == "disabled"
        for row in api.rules.values()
        if row["ruleset_id"] == result["ruleset_id"]
    )


async def test_api_denial_or_old_version_stops_without_writes():
    api = ControlledManagementApi()
    api.reject_reads = True
    async with api.client() as client:
        with pytest.raises(GreenRulesetInstallationError, match="403"):
            await install_green_ruleset(client, apply=True)
        assert all(method == "GET" for method, _ in api.calls)
        api.calls.clear()
        with pytest.raises(GreenRuleConfigurationError):
            await install_green_ruleset(client, version=3, apply=True)
    assert api.calls == []
    with pytest.raises(GreenRulesetInstallationError):
        await install_green_ruleset(publish=True)
    assert build_green_ruleset_manifest()["ruleset"]["version"] == 4
