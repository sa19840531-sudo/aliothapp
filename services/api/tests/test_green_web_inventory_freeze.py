"""只有服务端真实新绿色合同扩展 Web 冻结；夹具不代表三端业务验收。"""

from copy import deepcopy

import pytest

from services.api.app.application.green_rule_catalog import (
    GreenRuleConfigurationError,
    build_green_rule_payload,
)
from services.api.app.application.task_rule_inventory import (
    parse_task_rule_inventory,
    requires_task_rule_inventory,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    freeze_task_rule_inventory_on_connection,
)
from services.api.tests.test_task_repository import FakePool
from services.api.tests.test_task_rule_inventory import _rule


class FrozenRulesConnection:
    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return {
            "ruleset_id": "green-ruleset-4",
            "ruleset_version": 4,
            "ruleset_snapshot_hash": "controlled-snapshot",
            "rules": self.rules,
        }


@pytest.mark.parametrize("rules", [[], [_rule(1, rule_code="alioth_green_32")]])
async def test_request_marker_cannot_make_ordinary_web_rules_use_new_inventory(rules):
    connection = FrozenRulesConnection(rules)
    request_config = {
        "workflow_version": "v2",
        "green_rules_contract": "green-rule-contract.v2",
        "rule_inventory": {"forged": True},
        "browser": "chromium",
    }
    before = deepcopy(request_config)

    result = await freeze_task_rule_inventory_on_connection(
        connection,
        target_platform="web",
        ruleset_code="ordinary-web",
        ruleset_version=4,
        channel="workbench",
        task_config=request_config,
    )

    assert result == {"workflow_version": "v2", "browser": "chromium"}
    assert not requires_task_rule_inventory("web", result)
    assert request_config == before
    assert connection.calls[0][1] == ("ordinary-web", 4)


@pytest.mark.parametrize(
    "config",
    [
        {"workflow_version": "v1"},
        {"workflow_version": "v2", "execution_mode": "exploration_only"},
    ],
)
async def test_ordinary_web_and_exploration_are_not_expanded_by_spoofed_marker(config):
    connection = FrozenRulesConnection([])
    result = await freeze_task_rule_inventory_on_connection(
        connection,
        target_platform="web",
        ruleset_code="ordinary-web",
        ruleset_version=1,
        channel="workbench",
        task_config={**config, "green_rules_contract": "green-rule-contract.v2"},
    )

    assert result == config
    assert connection.calls == []


async def test_new_green_web_freezes_real_rule_and_reads_it_after_live_edits():
    green_rule = {"rule_id": "green-rule-32", **build_green_rule_payload("32")}
    green_rule["status"] = "enabled"
    legacy_rule = _rule(1, rule_code="alioth_41")
    connection = FrozenRulesConnection([green_rule, legacy_rule])
    config = await freeze_task_rule_inventory_on_connection(
        connection,
        target_platform="web",
        ruleset_code="alioth_green_ruleset",
        ruleset_version=4,
        channel="workbench",
        task_config={"workflow_version": "v2"},
    )
    frozen_before = deepcopy(config)
    inventory = parse_task_rule_inventory(config)
    assert inventory is not None
    assert config["green_rules_contract"] == "green-rule-contract.v2"
    assert config["ruleset_rule_total"] == 2
    assert inventory["target_platform"] == "web"
    assert requires_task_rule_inventory("web", config)
    green_frozen = next(row for row in inventory["rules"] if row["rule_id"] == "green-rule-32")
    frozen_program = deepcopy(green_frozen["judge_config"]["green_skill_program"])
    green_rule.update(name="后来停用并改写", status="disabled", check_point="新义务")
    green_rule["judge_config"] = {}
    pool = FakePool(None)

    async def frozen_task_only(query, *args):
        assert "SELECT task.target_platform, task.channel, task.task_config" in query
        assert args == ("task-1",)
        return {
            "target_platform": "web",
            "channel": "workbench",
            "ruleset_id": "green-ruleset-4",
            "task_config": config,
        }

    pool.connection.fetchrow = frozen_task_only
    repository = PostgresTaskRepository(pool=pool)

    candidates = await repository.list_candidate_rules(task_id="task-1", page_type="home")
    definition = await repository.get_rule_judge_definition(
        task_id="task-1", rule_code="alioth_green_32"
    )

    assert {item.rule_code for item in candidates} == {"alioth_green_32", "alioth_41"}
    assert definition is not None
    assert definition.check_point == green_frozen["check_point"]
    assert definition.judge_config["green_skill_program"] == frozen_program
    assert config == frozen_before
    assert pool.connection.calls == []


async def test_web_freezer_rejects_later_corrupt_green_rule_before_persisting_task():
    """首条合同有效不能让后续损坏合同绕过创建时校验。"""
    valid = {"rule_id": "green-rule-32", **build_green_rule_payload("32")}
    corrupt = {"rule_id": "green-rule-s73", **build_green_rule_payload("s73")}
    corrupt["judge_config"]["green_rule_contract"]["source_row"] = 999
    connection = FrozenRulesConnection([valid, corrupt])

    with pytest.raises(GreenRuleConfigurationError):
        await freeze_task_rule_inventory_on_connection(
            connection,
            target_platform="web",
            ruleset_code="alioth_green_ruleset",
            ruleset_version=4,
            channel="workbench",
            task_config={"workflow_version": "v2"},
        )
