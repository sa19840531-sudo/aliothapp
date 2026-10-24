"""任务提交时逐条冻结规则应用契约；应用判断不冒充业务判定。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url
from services.api.app.domain.task.inspection_scope import inspection_scope_page_types

RULE_INVENTORY_SCHEMA = "task-rule-inventory.v1"


class TaskRuleInventoryError(ValueError):
    """冻结清单缺失、损坏或与任务锁定事实冲突。"""


def requires_task_rule_inventory(target_platform: object, task_config: Mapping[str, Any]) -> bool:
    """App 与 H5 正式质检使用同一清单合同，探索任务不发布规则清单。"""

    return (
        (str(target_platform).casefold() in {"app", "h5"}
         or (str(target_platform).casefold() == "web"
             and task_config.get("green_rules_contract") == "green-rule-contract.v2"))
        and str(task_config.get("workflow_version") or "").casefold() == "v2"
        and task_config.get("execution_mode", "full_inspection") == "full_inspection"
    )


def assess_rule_application(
    rule: Mapping[str, Any],
    *,
    target_platform: str,
    channel: str,
    inspection_scope: Iterable[str],
    blocked_auth_requirement: str | None = None,
) -> dict[str, str]:
    """每条规则先判断端与任务范围；缺登录证据始终保留为未证明。"""

    contract = rule.get("input_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    clients = contract.get("target_clients")
    platform = {"app": "APP", "web": "WEB", "h5": "H5"}.get(
        target_platform.casefold(), target_platform.upper()
    )
    if isinstance(clients, list) and clients and platform not in clients:
        return {"applicability": "not_applicable", "reason_code": "platform_not_supported"}
    if rule.get("channel") is not None and rule.get("channel") != channel:
        return {"applicability": "not_applicable", "reason_code": "channel_not_supported"}
    scope = frozenset(inspection_scope)
    pages = rule.get("applicable_page_types") or []
    if scope and not scope.intersection(pages):
        return {"applicability": "out_of_scope", "reason_code": "inspection_scope_excluded"}
    if blocked_auth_requirement and contract.get("auth_requirement") == blocked_auth_requirement:
        return {"applicability": "unproven", "reason_code": "auth_requirement_not_met"}
    return {"applicability": "applicable", "reason_code": "eligible"}


def build_task_rule_inventory(
    *,
    ruleset_id: str,
    ruleset_version: int,
    ruleset_snapshot_hash: str | None,
    target_platform: str,
    channel: str,
    task_config: Mapping[str, Any],
    rules: Iterable[Mapping[str, Any]],
    source: str = "task_submission",
) -> dict[str, Any]:
    """在过滤之前冻结全部启用规则及逐条确定性应用判断。"""

    scope = sorted(inspection_scope_page_types(task_config))
    frozen_rules = []
    for source_rule in rules:
        rule = _copy_json(dict(source_rule))
        from services.api.app.application.green_rule_catalog import (
            validate_green_rule_configuration,
        )

        validate_green_rule_configuration(rule)
        for field in ("ruleset_id", "ruleset_version", "ruleset_snapshot_hash"):
            rule.pop(field, None)
        if not isinstance(rule.get("rule_id"), str) or not rule["rule_id"]:
            raise TaskRuleInventoryError("冻结规则必须有 rule_id")
        rule["contract_hash"] = build_inspection_rule_contract_hash(rule)
        rule.update(
            assess_rule_application(
                rule,
                target_platform=target_platform,
                channel=channel,
                inspection_scope=scope,
            )
        )
        frozen_rules.append(rule)
    frozen_rules.sort(key=lambda rule: (rule.get("sort_order") or 0, rule["rule_id"]))
    if len({rule["rule_id"] for rule in frozen_rules}) != len(frozen_rules):
        raise TaskRuleInventoryError("冻结规则 rule_id 不得重复")
    inventory = {
        "schema_version": RULE_INVENTORY_SCHEMA,
        "source": source,
        "ruleset_id": ruleset_id,
        "ruleset_version": ruleset_version,
        "ruleset_snapshot_hash": ruleset_snapshot_hash,
        "target_platform": target_platform,
        "channel": channel,
        "inspection_scope": scope,
        "rule_count": len(frozen_rules),
        "target_page_types": sorted(
            {
                page
                for rule in frozen_rules
                if rule["applicability"] == "applicable"
                for page in rule.get("applicable_page_types") or []
                if not scope or page in scope
            }
        ),
        "rules": frozen_rules,
    }
    inventory["inventory_hash"] = _inventory_hash(inventory)
    return inventory


def parse_task_rule_inventory(task_config: Mapping[str, Any]) -> dict[str, Any] | None:
    """缺失返回 None；存在但不可信时拒绝回退到可变规则。"""

    value = task_config.get("rule_inventory")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("schema_version") != RULE_INVENTORY_SCHEMA:
        raise TaskRuleInventoryError("规则清单版本无效")
    inventory = _copy_json(dict(value))
    if (
        inventory.get("source") not in {"task_submission", "legacy_verified"}
        or not isinstance(inventory.get("ruleset_id"), str)
        or not isinstance(inventory.get("ruleset_version"), int)
        or isinstance(inventory.get("ruleset_version"), bool)
        or inventory["ruleset_version"] < 1
        or inventory.get("target_platform") not in {"app", "h5", "web"}
        or not isinstance(inventory.get("channel"), str)
        or not isinstance(inventory.get("inspection_scope"), list)
    ):
        raise TaskRuleInventoryError("规则清单头信息无效")
    rules = inventory.get("rules")
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise TaskRuleInventoryError("规则清单条目无效")
    ids = [rule.get("rule_id") for rule in rules]
    if any(not isinstance(rule_id, str) or not rule_id for rule_id in ids):
        raise TaskRuleInventoryError("规则清单缺少 rule_id")
    count = inventory.get("rule_count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or len(set(ids)) != len(ids)
        or count != len(rules)
    ):
        raise TaskRuleInventoryError("规则清单数量或唯一性不一致")
    total = task_config.get("ruleset_rule_total")
    if not isinstance(total, int) or isinstance(total, bool) or total != len(rules):
        raise TaskRuleInventoryError("规则清单与创建时规则总数不一致")
    if inventory.get("inventory_hash") != _inventory_hash(inventory):
        raise TaskRuleInventoryError("规则清单哈希不一致")
    if inventory.get("inspection_scope") != sorted(inspection_scope_page_types(task_config)):
        raise TaskRuleInventoryError("规则清单与任务质检范围不一致")
    expected_pages = sorted(
        {
            page
            for rule in rules
            if rule.get("applicability") == "applicable"
            for page in rule.get("applicable_page_types") or []
            if not inventory["inspection_scope"] or page in inventory["inspection_scope"]
        }
    )
    if inventory.get("target_page_types") != expected_pages:
        raise TaskRuleInventoryError("规则清单与冻结目标页型不一致")
    for rule in rules:
        from services.api.app.application.green_rule_catalog import (
            validate_green_rule_configuration,
        )

        validate_green_rule_configuration(rule)
        expected = assess_rule_application(
            rule,
            target_platform=inventory["target_platform"],
            channel=inventory["channel"],
            inspection_scope=inventory["inspection_scope"],
        )
        if any(rule.get(key) != value for key, value in expected.items()):
            raise TaskRuleInventoryError("规则清单应用判断不一致")
        if rule.get("contract_hash") != build_inspection_rule_contract_hash(rule):
            raise TaskRuleInventoryError("规则清单契约哈希不一致")
    return inventory


def inventory_candidate_rules(
    inventory: Mapping[str, Any],
    *,
    page_type: str,
    page_url: str | None,
    blocked_auth_requirement: str | None,
) -> list[dict[str, Any]]:
    """候选只消费冻结契约；排除项仍留在完整清单中供报告和门禁读取。"""

    scope = inventory["inspection_scope"]
    if scope and page_type not in scope:
        return []
    candidates = []
    for rule in inventory["rules"]:
        from services.api.app.application.web_rule_evidence_contract import (
            read_web_rule_evidence_contract,
        )

        web_contract = read_web_rule_evidence_contract(rule.get("input_contract") or {})
        if web_contract is not None and "stable_page" not in web_contract.accepted_roles:
            continue
        application = assess_rule_application(
            rule,
            target_platform=inventory["target_platform"],
            channel=inventory["channel"],
            inspection_scope=scope,
            blocked_auth_requirement=blocked_auth_requirement,
        )
        config = rule.get("judge_config") or {}
        capability = config.get("capability_config") or {}
        if application["applicability"] != "applicable":
            continue
        if capability.get("evidence_input_scope", "page") != "page":
            continue
        if page_type not in (rule.get("applicable_page_types") or []):
            continue
        matches, match_reason = candidate_rule_matches_page_url(
            page_url=page_url, url_pattern=rule.get("url_pattern")
        )
        if matches:
            candidates.append({**rule, "match_reason": match_reason or "applicable_page_types"})
    return candidates


def _inventory_hash(inventory: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in inventory.items() if key != "inventory_hash"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def inventory_execution_contract_hash(rule: Mapping[str, Any]) -> str:
    """核对实际发布合同；页面批次带执行路线，聚合批次使用原始规则合同。"""
    from services.api.app.application.rule_judge import rule_execution_route_payload

    payload = dict(rule)
    for field in ("input_contract", "judge_config", "manual_review_policy"):
        payload[field] = payload.get(field) or {}
    payload["steps"] = payload.get("steps") or []
    capability = payload["judge_config"].get("capability_config") or {}
    if capability.get("evidence_input_scope", "page") == "page":
        payload["execution_route"] = rule_execution_route_payload(
            executor_type=payload["executor_type"],
            judge_config=payload["judge_config"],
            input_contract=payload["input_contract"],
        )
    return build_inspection_rule_contract_hash(payload)


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))
