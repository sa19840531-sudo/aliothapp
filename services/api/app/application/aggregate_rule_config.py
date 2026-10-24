"""规则证据输入范围的显式配置与发布前校验。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.evidence import EvidenceInputScope

_SCOPE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SUPPORTED_AGGREGATE_SCOPE_KEYS = {
    EvidenceInputScope.TASK: frozenset(
        {"toast_style", "home_popup_sequence", "official_group_entry",
         "green_interaction_stability.v1", "green_temporal_rules.v1"}
    ),
    EvidenceInputScope.JOURNEY: frozenset({"journey_observation"}),
    EvidenceInputScope.ENVIRONMENT_MATRIX: frozenset({"environment_consistency"}),
}


class AggregateRuleConfigurationError(ValueError):
    """规则声明了不可执行或不完整的聚合证据范围。"""


@dataclass(frozen=True, kw_only=True)
class RuleEvidenceInputConfiguration:
    """从 judge_config 中解析出的稳定证据输入合同。"""

    scope: EvidenceInputScope
    scope_key: str

    @property
    def is_aggregate(self) -> bool:
        return self.scope is not EvidenceInputScope.PAGE


def resolve_rule_evidence_input_configuration(
    judge_config: Mapping[str, Any] | object,
    *,
    require_executable: bool = False,
) -> RuleEvidenceInputConfiguration:
    """缺省规则仍是 page；只有显式字段才能进入聚合链。"""

    config = judge_config if isinstance(judge_config, Mapping) else {}
    capability = config.get("capability_config")
    capability = capability if isinstance(capability, Mapping) else {}
    raw_scope = capability.get("evidence_input_scope", EvidenceInputScope.PAGE.value)
    try:
        scope = EvidenceInputScope(str(raw_scope))
    except ValueError as exc:
        raise AggregateRuleConfigurationError(
            "capability_config.evidence_input_scope 不受支持"
        ) from exc
    if scope is EvidenceInputScope.WEB_SEQUENCE:
        raise AggregateRuleConfigurationError("web_sequence 仅允许 Web 专属封存，不属于聚合规则配置")
    raw_key = capability.get("evidence_scope_key")
    if scope is EvidenceInputScope.PAGE:
        if raw_key not in (None, "", "page"):
            raise AggregateRuleConfigurationError("page 规则不得声明聚合 evidence_scope_key")
        return RuleEvidenceInputConfiguration(scope=scope, scope_key="page")
    scope_key = str(raw_key or "").strip()
    if not _SCOPE_KEY_PATTERN.fullmatch(scope_key):
        raise AggregateRuleConfigurationError(
            "聚合规则必须声明稳定的 capability_config.evidence_scope_key"
        )
    if require_executable and scope_key not in _SUPPORTED_AGGREGATE_SCOPE_KEYS[scope]:
        raise AggregateRuleConfigurationError(
            f"{scope.value} 范围尚无可执行的聚合策略：{scope_key}"
        )
    if scope_key in {"green_interaction_stability.v1", "green_temporal_rules.v1"}:
        from services.api.app.application.green_rule_catalog import (
            validate_green_rule_configuration,
        )

        try:
            contract = validate_green_rule_configuration({"judge_config": config})
        except ValueError as exc:
            raise AggregateRuleConfigurationError(str(exc)) from exc
        if contract is None:
            raise AggregateRuleConfigurationError("绿色过程范围必须绑定显式第4版合同及冻结程序")
    return RuleEvidenceInputConfiguration(scope=scope, scope_key=scope_key)


def validate_enabled_aggregate_rule_configuration(
    judge_config: Mapping[str, Any] | object,
) -> RuleEvidenceInputConfiguration:
    """启用/发布规则时 fail-closed，避免生产期才发现错误分流。"""

    return resolve_rule_evidence_input_configuration(
        judge_config,
        require_executable=True,
    )
