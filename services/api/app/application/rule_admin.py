"""管理后台规则配置的业务口径。

对齐 2.1 原型「配置中心 → 规则配置」：业务人员新增 / 编辑规则只填
规则名称、规则详情、适用页面、优先级；规则编号、执行器、步骤、输入契约、
判定配置、人工审核策略等系统字段由系统自动补齐（新增）或原样保留（编辑）。
启停用独立开关，开启前校验这条规则在正式任务里真能跑。
"""

from __future__ import annotations

import re
from typing import Any

from services.api.app.application.aggregate_rule_config import (
    AggregateRuleConfigurationError,
    validate_enabled_aggregate_rule_configuration,
)
from services.api.app.application.rule_judge import resolve_rule_execution_route

# 业务新增规则的系统默认值：纯大模型判定，证据要求与现役大模型规则同口径
QUICK_RULE_CODE_PREFIX = "custom_"
QUICK_RULE_CATEGORY = "compliance"
QUICK_RULE_EXECUTOR = "llm"
_QUICK_RULE_CODE_PATTERN = re.compile(rf"^{QUICK_RULE_CODE_PREFIX}(\d+)$")

# RulePayload 的全部字段；编辑时从库里整行取出再覆盖业务字段，避免系统字段被清空
_RULE_PAYLOAD_FIELDS = (
    "ruleset_id",
    "rule_code",
    "name",
    "description",
    "category",
    "executor_type",
    "severity",
    "status",
    "scope",
    "channel",
    "page_type",
    "applicable_page_types",
    "url_pattern",
    "steps",
    "input_contract",
    "check_point",
    "judge_config",
    "manual_review_policy",
    "sort_order",
)

_RULESET_PAYLOAD_FIELDS = (
    "ruleset_code",
    "name",
    "version",
    "status",
    "description",
    "source_type",
    "source_ref",
    "page_sampling_policy",
    "loop_guard",
)


def rule_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """库里的规则行 → 完整写入载荷（空值按写入口径还原，channel 为空表示不限渠道）。"""
    payload = {field: row.get(field) for field in _RULE_PAYLOAD_FIELDS}
    for text_field in ("description", "scope", "page_type", "url_pattern", "check_point"):
        payload[text_field] = payload[text_field] or ""
    payload["applicable_page_types"] = list(payload["applicable_page_types"] or [])
    payload["steps"] = list(payload["steps"] or [])
    for mapping_field in ("input_contract", "judge_config", "manual_review_policy"):
        payload[mapping_field] = dict(payload[mapping_field] or {})
    payload["sort_order"] = int(payload["sort_order"] if payload["sort_order"] is not None else 100)
    return payload


def ruleset_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {field: row.get(field) for field in _RULESET_PAYLOAD_FIELDS}
    payload["description"] = payload["description"] or ""
    payload["source_ref"] = payload["source_ref"] or ""
    payload["page_sampling_policy"] = dict(payload["page_sampling_policy"] or {})
    payload["loop_guard"] = dict(payload["loop_guard"] or {})
    return payload


def merge_rule_business_fields(
    row: dict[str, Any],
    *,
    name: str | None = None,
    check_point: str | None = None,
    applicable_page_types: list[str] | None = None,
    severity: str | None = None,
    evidence_input_scope: str | None = None,
    journey_rule_contract: dict | None = None,
) -> dict[str, Any]:
    """只改业务字段，其余系统字段原样保留。"""
    payload = rule_payload_from_row(row)
    if name is not None:
        payload["name"] = name.strip()
    if check_point is not None:
        new_check_point = check_point.strip()
        judge_config = dict(payload["judge_config"])
        # 业务新增的规则判定标准就是规则详情，跟着一起改；精调过的规则不动判定标准
        if judge_config.get("llm_checks") == [payload["check_point"]]:
            judge_config["llm_checks"] = [new_check_point]
            payload["judge_config"] = judge_config
        payload["check_point"] = new_check_point
    if applicable_page_types is not None:
        pages = list(dict.fromkeys(item.strip() for item in applicable_page_types if item.strip()))
        payload["applicable_page_types"] = pages
        if payload["page_type"] not in pages:
            payload["page_type"] = pages[0] if pages else ""
        payload["steps"] = [
            step
            for step in payload["steps"]
            if not isinstance(step, dict)
            or not step.get("expected_page_type")
            or step.get("expected_page_type") in pages
        ]
    if severity is not None:
        payload["severity"] = severity
    if evidence_input_scope is not None:
        judge = dict(payload["judge_config"])
        capability = dict(judge.get("capability_config") or {})
        if (
            evidence_input_scope not in {"page", "journey"}
            or capability.get("evidence_input_scope", "page") not in {"page", "journey"}
        ):
            raise ValueError("其他汇总合同不能通过页面/短流程选项覆盖")
        clients = payload["input_contract"].get("target_clients", [])
        if evidence_input_scope == "journey" and clients != ["H5"]:
            raise ValueError("只有明确仅适用 H5 的规则能切换短流程合同，不能改变 App/Web 规则路由")
        capability.update(
            evidence_input_scope=evidence_input_scope,
            evidence_scope_key=(
                "journey_observation" if evidence_input_scope == "journey" else "page"
            ),
            requires_journey=evidence_input_scope == "journey",
        )
        if evidence_input_scope == "page":
            capability.pop("journey_rule_contract", None)
        judge["capability_config"] = capability
        payload["judge_config"] = judge
    if journey_rule_contract is not None:
        from services.api.app.application.h5_journey_rule_contract import parse_rule_contract

        judge = dict(payload["judge_config"])
        capability = dict(judge.get("capability_config") or {})
        if (capability.get("evidence_input_scope") != "journey"
                or payload["input_contract"].get("target_clients") != ["H5"]):
            raise ValueError("流程配置只能用于仅适用 H5 的流程规则")
        capability["journey_rule_contract"] = parse_rule_contract(journey_rule_contract)
        judge["capability_config"] = capability
        payload["judge_config"] = judge
    return payload


def rule_enable_problem(payload: dict[str, Any]) -> str | None:
    """开启前的可执行性检查；返回大白话原因，None 表示可以开启。"""
    if not payload.get("applicable_page_types"):
        return "开启前请先选择这条规则的适用页面"
    route = resolve_rule_execution_route(
        executor_type=str(payload.get("executor_type") or ""),
        judge_config=dict(payload.get("judge_config") or {}),
    )
    if not route.ready:
        reason = route.blocked_reason or {}
        detail = reason.get("message") or "配置不完整"
        return f"这条规则的判定配置跑不起来，开启后会拦住所有完整质检任务：{detail}"
    try:
        validate_enabled_aggregate_rule_configuration(payload.get("judge_config") or {})
    except AggregateRuleConfigurationError as exc:
        return f"这条规则的汇总判定配置不完整：{exc}"
    return None


def next_quick_rule_code(existing_codes: list[str]) -> str:
    """业务新增规则编号：custom_001 起按规则集内已有编号顺延。"""
    numbers = [
        int(match.group(1))
        for code in existing_codes
        if (match := _QUICK_RULE_CODE_PATTERN.match(code))
    ]
    return f"{QUICK_RULE_CODE_PREFIX}{(max(numbers) + 1 if numbers else 1):03d}"


def build_quick_rule_payload(
    *,
    ruleset_id: str,
    rule_code: str,
    name: str,
    check_point: str,
    applicable_page_types: list[str],
    severity: str,
    sort_order: int,
) -> dict[str, Any]:
    """业务新增规则：补齐系统字段，默认关闭，测完再由管理员开启。"""
    pages = list(dict.fromkeys(item.strip() for item in applicable_page_types if item.strip()))
    detail = check_point.strip()
    return {
        "ruleset_id": ruleset_id,
        "rule_code": rule_code,
        "name": name.strip(),
        "description": "",
        "category": QUICK_RULE_CATEGORY,
        "executor_type": QUICK_RULE_EXECUTOR,
        "severity": severity,
        "status": "disabled",
        "scope": "",
        # 不限渠道，与现役种子规则一致
        "channel": None,
        "page_type": pages[0] if pages else "",
        "applicable_page_types": pages,
        "url_pattern": "",
        "steps": [
            {"action": "observe", "target": name.strip(), "expected_page_type": pages[0]}
        ]
        if pages
        else [],
        "input_contract": {
            "schema_version": "1.1",
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": ["screenshot", "ui_snapshot", "dom"],
            "optional_artifacts": ["visible_text", "har", "console_log", "ocr_text"],
            "required_fields": ["page.url", "facts.ui_snapshot"],
            "quality_gate": {"min_screenshot_count": 1, "allow_empty_visible_text": True},
            "recapture_policy": {"kind": "equivalent_state", "fallback": "fresh_observation"},
            "reuse_policy": {"scope": "per_page"},
        },
        "check_point": detail,
        "judge_config": {
            "schema_version": "1.1",
            "prompt_ref": "",
            "thresholds": {},
            "deterministic_rules": [],
            "llm_checks": [detail],
            "tool_refs": ["page_capture_tool", "semantic_rule_judge_tool"],
            "capability_config": {
                "schema_version": "1.0",
                "batch_group": "custom-semantic",
                "required_artifact_types": ["screenshot", "ui_snapshot", "dom"],
                "deterministic_tools": [],
                "semantic_judge": "semantic_rule_judge_tool",
                "requires_journey": False,
                "evidence_source": "observation_packet",
            },
        },
        "manual_review_policy": {
            "schema_version": "1.0",
            "triggers": ["evidence_insufficient", "low_confidence", "model_uncertainty"],
            "min_confidence": 0.82,
            "reviewer_group": "qc_rule_owner",
        },
        "sort_order": sort_order,
    }
