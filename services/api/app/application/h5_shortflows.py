"""短流程显式启用并冻结发布版本，未知规则和能力不猜测接线。"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.api.app.agents.skills.loader import h5_shortflow_skill_hash
from services.api.app.application.h5_checkpoint_observation import supports_checkpoint
from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    parse_task_rule_inventory,
)
from services.api.app.domain.journey.definitions import JOURNEY_CHECKPOINT_SCHEMA_VERSION


class H5ShortflowError(ValueError):
    """请求不能冻结为可执行、可判定的短流程。"""


class H5ShortflowSelection(BaseModel):
    """调用方只能选择已发布的合同，不提交定位器、预算或伪造规则哈希。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    definition_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_key: str = Field(min_length=1, max_length=128)
    rule_ids: list[str] = Field(min_length=1, max_length=100)
    workflow_skill: Literal["h5-player-workflow", "h5-payment-workflow"]

    @field_validator("definition_id", "scenario_key")
    @classmethod
    def non_blank(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError("流程身份不能为空或包含首尾空白")
        return value

    @field_validator("rule_ids")
    @classmethod
    def unique_rules(cls, values):
        if any(
            not value.strip() or value != value.strip() or len(value) > 128 for value in values
        ) or len(set(values)) != len(values):
            raise ValueError("rule_ids 必须为非空且不重复的规则身份")
        return values


def validate_h5_shortflow_selections(values):
    if not isinstance(values, (list, tuple)) or len(values) > 10:
        raise H5ShortflowError("每任务最多启用十个短流程")
    try:
        selections = tuple(H5ShortflowSelection.model_validate(value) for value in values)
    except ValidationError as exc:
        raise H5ShortflowError(str(exc)) from exc
    scopes = [(item.definition_id, item.scenario_key) for item in selections]
    rules = [rule for item in selections for rule in item.rule_ids]
    if len(set(scopes)) != len(scopes) or len(set(rules)) != len(rules):
        raise H5ShortflowError("重复流程场景或规则跨流程绑定会造成输入歧义")
    return selections


async def freeze_h5_shortflows(*, task_config, repository):
    if not task_config.get("h5_shortflows"):
        return dict(task_config)
    selections = validate_h5_shortflow_selections(task_config["h5_shortflows"])
    if (
        task_config.get("workflow_version") != "v2"
        or task_config.get("execution_mode") != "full_inspection"
    ):
        raise H5ShortflowError("短流程目前只接入 H5 V2 正式质检")
    try:
        inventory = parse_task_rule_inventory(task_config)
    except TaskRuleInventoryError as exc:
        raise H5ShortflowError(str(exc)) from exc
    if inventory is None or inventory["target_platform"] != "h5":
        raise H5ShortflowError("短流程缺少服务端冻结的 H5 规则清单")
    rules = {rule["rule_id"]: rule for rule in inventory["rules"]}
    frozen = []
    for item in selections:
        definition = await repository.get(definition_id=item.definition_id, version=item.version)
        if (
            definition is None
            or definition.content_hash != item.content_hash
            or definition.schema_version != JOURNEY_CHECKPOINT_SCHEMA_VERSION
            or "web" not in definition.surface_kinds
        ):
            raise H5ShortflowError("短流程已发布版本不存在、哈希漂移或平台不匹配")
        if any(
            checkpoint.side_effect_policy != "read_only" or not supports_checkpoint(checkpoint)
            for checkpoint in definition.checkpoints
        ):
            raise H5ShortflowError("短流程含未实现的观测或非只读动作合同，禁止消耗预算")
        payment = any(
            {"payment_display_visible", "payment_plan_selected"}.intersection(
                set(checkpoint.entry_condition)
                | set(checkpoint.expected)
                | set(checkpoint.exit_condition)
            )
            for checkpoint in definition.checkpoints
        )
        if payment != (item.workflow_skill == "h5-payment-workflow"):
            raise H5ShortflowError("短流程业务事实与所选 Skill 不匹配")
        hashes = []
        for rule_id in item.rule_ids:
            rule = rules.get(rule_id)
            config = (rule or {}).get("judge_config")
            capability = config.get("capability_config") if isinstance(config, Mapping) else None
            capability = capability if isinstance(capability, Mapping) else {}
            if (
                rule is None
                or rule["applicability"] != "applicable"
                or capability.get("evidence_input_scope") != "journey"
                or capability.get("evidence_scope_key") != "journey_observation"
            ):
                raise H5ShortflowError("短流程规则不存在、不适用或不是流程证据合同")
            hashes.append(rule["contract_hash"])
            if capability.get("journey_rule_contract") is not None:
                from services.api.app.application.h5_journey_rule_contract import (
                    validate_rule_definition,
                )

                try:
                    validate_rule_definition(capability["journey_rule_contract"], definition,
                                             scenario_key=item.scenario_key,
                                             workflow_skill=item.workflow_skill)
                except ValueError as exc:
                    raise H5ShortflowError(str(exc)) from exc
        frozen.append(
            {
                **item.model_dump(),
                "definition": definition.to_dict(),
                "rule_contract_hashes": sorted(set(hashes)),
                "workflow_skill_hash": h5_shortflow_skill_hash(item.workflow_skill),
            }
        )
    return {**task_config, "h5_shortflows": frozen}
