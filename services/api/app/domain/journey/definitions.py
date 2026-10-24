"""Journey Definition、Version 与任务执行快照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from services.api.app.domain.journey.checkpoints import JourneyCheckpoint, validate_checkpoints
from services.api.app.domain.journey.steps import JourneyStep
from services.api.app.domain.journey.validation import (
    JourneyValidationError,
    as_tuple,
    freeze_mapping,
    reject_platform_locators,
    require_text,
    stable_hash,
    thaw_mapping,
    validate_unique_texts,
)

JOURNEY_DSL_SCHEMA_VERSION = "journey-dsl.v1"
JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION = "journey-execution-plan.v1"
JOURNEY_CHECKPOINT_SCHEMA_VERSION = "journey-dsl.v2"
JOURNEY_RUN_PLAN_SCHEMA_VERSION = "journey-execution-plan.v2"


@dataclass(frozen=True, kw_only=True)
class JourneyDefinition:
    """可发布且内容可寻址的 Journey Definition 版本。"""

    definition_id: str
    version: int
    name: str
    surface_kinds: tuple[str, ...]
    steps: tuple[JourneyStep, ...] = ()
    checkpoints: tuple[JourneyCheckpoint, ...] = ()
    description: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = JOURNEY_DSL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.definition_id, "definition_id")
        require_text(self.name, "name")
        if self.schema_version not in (
            JOURNEY_DSL_SCHEMA_VERSION,
            JOURNEY_CHECKPOINT_SCHEMA_VERSION,
        ):
            raise JourneyValidationError("Journey Definition 的 schema_version 不受支持")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise JourneyValidationError("version 必须为正整数")
        surface_kinds = as_tuple(self.surface_kinds, "surface_kinds")
        steps = as_tuple(self.steps, "steps")
        validate_unique_texts(surface_kinds, "surface_kinds")
        if not surface_kinds:
            raise JourneyValidationError("surface_kinds 不能为空")
        if self.schema_version == JOURNEY_DSL_SCHEMA_VERSION:
            if not steps or self.checkpoints:
                raise JourneyValidationError("V1 必须包含 steps，不能包含 checkpoints")
        else:
            if steps:
                raise JourneyValidationError("检查点合同不能混入顺序 steps")
            object.__setattr__(self, "checkpoints", validate_checkpoints(self.checkpoints))
        if any(not isinstance(step, JourneyStep) for step in steps):
            raise JourneyValidationError("steps 必须由 JourneyStep 组成")
        step_ids = tuple(step.step_id for step in steps)
        if len(set(step_ids)) != len(step_ids):
            raise JourneyValidationError("steps 不能包含重复 id")
        if self.description is not None:
            require_text(self.description, "description")
        frozen_extensions = freeze_mapping(self.extensions, "extensions")
        reject_platform_locators(frozen_extensions, "extensions")
        object.__setattr__(self, "extensions", frozen_extensions)
        object.__setattr__(self, "surface_kinds", surface_kinds)
        object.__setattr__(self, "steps", steps)

    @property
    def content_hash(self) -> str:
        """返回包含完整步骤快照的稳定内容哈希。"""
        return stable_hash(self.to_dict())

    @property
    def version_ref(self) -> JourneyVersion:
        return JourneyVersion(
            definition_id=self.definition_id,
            version=self.version,
            content_hash=self.content_hash,
            schema_version=self.schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "definition_id": self.definition_id,
            "version": self.version,
            "name": self.name,
            "surface_kinds": list(self.surface_kinds),
        }
        if self.schema_version == JOURNEY_DSL_SCHEMA_VERSION:
            payload["steps"] = [step.to_dict() for step in self.steps]
        else:
            payload["checkpoints"] = [item.to_dict() for item in self.checkpoints]
        if self.description is not None:
            payload["description"] = self.description
        if self.extensions:
            payload["extensions"] = thaw_mapping(self.extensions)
        return payload


@dataclass(frozen=True, kw_only=True)
class JourneyVersion:
    """跨模块引用 Journey 版本时使用的最小不可变身份。"""

    definition_id: str
    version: int
    content_hash: str
    schema_version: str = JOURNEY_DSL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.definition_id, "definition_id")
        require_text(self.content_hash, "content_hash")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise JourneyValidationError("version 必须为正整数")
        if self.schema_version not in (
            JOURNEY_DSL_SCHEMA_VERSION,
            JOURNEY_CHECKPOINT_SCHEMA_VERSION,
        ):
            raise JourneyValidationError("JourneyVersion 的 schema_version 不受支持")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "definition_id": self.definition_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, kw_only=True)
class JourneyExecutionPlan:
    """任务启动时锁定的 Journey 版本和步骤快照。"""

    execution_plan_id: str
    task_id: str
    journey_version: JourneyVersion
    steps: tuple[JourneyStep, ...]
    rule_contract_hashes: tuple[str, ...] = ()
    capture_plan_id: str | None = None
    schema_version: str = JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION
    checkpoints: tuple[JourneyCheckpoint, ...] = ()
    agent_run_id: str | None = None
    scenario_key: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.execution_plan_id, "execution_plan_id")
        require_text(self.task_id, "task_id")
        if not isinstance(self.journey_version, JourneyVersion):
            raise JourneyValidationError("journey_version 必须为 JourneyVersion")
        if self.schema_version not in (
            JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION,
            JOURNEY_RUN_PLAN_SCHEMA_VERSION,
        ):
            raise JourneyValidationError("JourneyExecutionPlan 的 schema_version 不受支持")
        steps = as_tuple(self.steps, "steps")
        rule_contract_hashes = as_tuple(
            self.rule_contract_hashes,
            "rule_contract_hashes",
        )
        if self.schema_version == JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION:
            if (
                not steps
                or self.checkpoints
                or self.agent_run_id is not None
                or self.scenario_key is not None
                or self.parameters
                or self.journey_version.schema_version != JOURNEY_DSL_SCHEMA_VERSION
            ):
                raise JourneyValidationError("V1 执行计划不允许检查点或 Run 范围")
        else:
            require_text(self.agent_run_id, "agent_run_id")
            require_text(self.scenario_key, "scenario_key")
            if len(self.scenario_key) > 128:
                raise JourneyValidationError("scenario_key 不能超过 128 字符")
            if steps or self.journey_version.schema_version != JOURNEY_CHECKPOINT_SCHEMA_VERSION:
                raise JourneyValidationError("Run 检查点计划不能混入顺序步骤")
            object.__setattr__(self, "checkpoints", validate_checkpoints(self.checkpoints))
        frozen_parameters = freeze_mapping(self.parameters, "parameters")
        reject_platform_locators(frozen_parameters, "parameters")
        object.__setattr__(self, "parameters", frozen_parameters)
        if any(not isinstance(step, JourneyStep) for step in steps):
            raise JourneyValidationError("执行计划 steps 必须由 JourneyStep 组成")
        validate_unique_texts(rule_contract_hashes, "rule_contract_hashes")
        if tuple(sorted(rule_contract_hashes)) != rule_contract_hashes:
            raise JourneyValidationError("rule_contract_hashes 必须按升序排列")
        if self.capture_plan_id is not None:
            require_text(self.capture_plan_id, "capture_plan_id")
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "rule_contract_hashes", rule_contract_hashes)
        if self.schema_version == JOURNEY_RUN_PLAN_SCHEMA_VERSION:
            identity = self.to_dict()
            del identity["execution_plan_id"]
            if self.execution_plan_id != f"journey-plan:{stable_hash(identity)}":
                raise JourneyValidationError("Run 执行计划内容身份不一致")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "execution_plan_id": self.execution_plan_id,
            "task_id": self.task_id,
            "journey_version": self.journey_version.to_dict(),
            "rule_contract_hashes": list(self.rule_contract_hashes),
            "capture_plan_id": self.capture_plan_id,
        }
        if self.schema_version == JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION:
            payload["steps"] = [step.to_dict() for step in self.steps]
        else:
            payload.update(
                agent_run_id=self.agent_run_id,
                scenario_key=self.scenario_key,
                parameters=thaw_mapping(self.parameters),
                checkpoints=[item.to_dict() for item in self.checkpoints],
            )
        return payload


def build_journey_execution_plan(
    definition: JourneyDefinition,
    *,
    task_id: str,
    rule_contract_hashes: Sequence[str] = (),
    capture_plan_id: str | None = None,
) -> JourneyExecutionPlan:
    """从已发布定义生成内容可寻址的不可变执行快照。"""
    require_text(task_id, "task_id")
    if definition.schema_version != JOURNEY_DSL_SCHEMA_VERSION:
        raise JourneyValidationError("检查点合同必须通过 Run 执行计划冻结")
    supplied_rule_hashes = as_tuple(rule_contract_hashes, "rule_contract_hashes")
    validate_unique_texts(supplied_rule_hashes, "rule_contract_hashes")
    locked_rule_hashes = tuple(sorted(supplied_rule_hashes))
    if capture_plan_id is not None:
        require_text(capture_plan_id, "capture_plan_id")
    identity = {
        "task_id": task_id,
        "journey_version": definition.version_ref.to_dict(),
        "rule_contract_hashes": list(locked_rule_hashes),
        "capture_plan_id": capture_plan_id,
    }
    return JourneyExecutionPlan(
        execution_plan_id=f"journey-plan:{stable_hash(identity)}",
        task_id=task_id,
        journey_version=definition.version_ref,
        steps=definition.steps,
        rule_contract_hashes=locked_rule_hashes,
        capture_plan_id=capture_plan_id,
    )


def build_run_journey_execution_plan(
    definition: JourneyDefinition,
    *,
    task_id: str,
    agent_run_id: str,
    scenario_key: str,
    parameters: Mapping[str, Any],
    rule_contract_hashes: Sequence[str] = (),
    capture_plan_id: str | None = None,
) -> JourneyExecutionPlan:
    """同一 Run 可冻结多个流程，各流程场景独立锁定版本和参数。"""
    hashes = as_tuple(rule_contract_hashes, "rule_contract_hashes")
    validate_unique_texts(hashes, "rule_contract_hashes")
    parameters = freeze_mapping(parameters, "parameters")
    identity = {
        "schema_version": JOURNEY_RUN_PLAN_SCHEMA_VERSION,
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "scenario_key": scenario_key,
        "parameters": thaw_mapping(parameters),
        "journey_version": definition.version_ref.to_dict(),
        "checkpoints": [item.to_dict() for item in definition.checkpoints],
        "rule_contract_hashes": sorted(hashes),
        "capture_plan_id": capture_plan_id,
    }
    return JourneyExecutionPlan(
        execution_plan_id=f"journey-plan:{stable_hash(identity)}",
        task_id=task_id,
        agent_run_id=agent_run_id,
        scenario_key=scenario_key,
        parameters=parameters,
        journey_version=definition.version_ref,
        steps=(),
        checkpoints=definition.checkpoints,
        rule_contract_hashes=tuple(sorted(hashes)),
        capture_plan_id=capture_plan_id,
        schema_version=JOURNEY_RUN_PLAN_SCHEMA_VERSION,
    )
