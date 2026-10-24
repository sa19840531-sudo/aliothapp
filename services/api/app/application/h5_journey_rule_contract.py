"""规则选择已有流程版本及其证据义务，不改写流程、Gate 或采集预算。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: str = Field(min_length=1, max_length=128)
    artifact_types: list[Literal["screenshot", "dom", "media_state", "action_receipt"]] = Field(
        min_length=1
    )
    minimum_count: int = Field(default=1, ge=1, le=2)
    capture_phase: Literal["before_action", "after_action"] = "after_action"

    @field_validator("artifact_types")
    @classmethod
    def unique_types(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("证据类型不能重复")
        return values


class CheckpointCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    requirements: list[EvidenceRequirement] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_roles(self):
        if len({item.role for item in self.requirements}) != len(self.requirements):
            raise ValueError("检查点证据角色不能重复")
        return self


class JourneyRuleContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    definition_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_skill: Literal["h5-player-workflow", "h5-payment-workflow"]
    scenario_key: str = Field(min_length=1, max_length=128)
    evidence_conditions: list[CheckpointCondition] = Field(min_length=1, max_length=100)

    @field_validator("definition_id", "scenario_key")
    @classmethod
    def clean_text(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError("流程与场景身份不能为空或含首尾空白")
        return value

    @model_validator(mode="after")
    def unique_checkpoints(self):
        if len({item.checkpoint_id for item in self.evidence_conditions}) != len(
            self.evidence_conditions
        ):
            raise ValueError("检查点不能重复")
        return self


def parse_rule_contract(value):
    return JourneyRuleContract.model_validate(value).model_dump(exclude_unset=True)


def validate_rule_definition(value, definition, *, scenario_key=None, workflow_skill=None):
    """证据编辑选择发布合同已保证的材料，依赖闭包由领域选择器统一计算。"""
    from services.api.app.application.h5_shortflow_read import definition_summary

    contract = JourneyRuleContract.model_validate(value)
    summary = definition_summary(definition) if definition is not None else None
    if summary is None or any(
        getattr(contract, key) != summary[key]
        for key in ("definition_id", "version", "content_hash", "workflow_skill")
    ):
        raise ValueError("流程类型或已发布版本不匹配，不允许猜测替代")
    if (scenario_key is not None and contract.scenario_key != scenario_key) or (
        workflow_skill is not None and contract.workflow_skill != workflow_skill
    ):
        raise ValueError("规则流程类型或场景与任务选择不匹配")
    from services.api.app.domain.journey.rule_selection import select_rule_checkpoints

    select_rule_checkpoints(
        definition, [item.model_dump() for item in contract.evidence_conditions]
    )
