"""Journey 顺序步骤 V1 与检查点合同 V2 的严格解析入口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.domain.journey.checkpoints import (
    JourneyCheckpoint,
    JourneyEvidenceRequirement,
)
from services.api.app.domain.journey.definitions import (
    JOURNEY_CHECKPOINT_SCHEMA_VERSION,
    JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION,
    JOURNEY_RUN_PLAN_SCHEMA_VERSION,
    JourneyDefinition,
    JourneyExecutionPlan,
    JourneyVersion,
)
from services.api.app.domain.journey.steps import (
    JourneyRetryPolicy,
    JourneyStep,
    JourneyStepKind,
)
from services.api.app.domain.journey.validation import JourneyValidationError

_DEFINITION_FIELDS = frozenset(
    {
        "schema_version",
        "definition_id",
        "version",
        "name",
        "description",
        "surface_kinds",
        "steps",
        "extensions",
    }
)
_COMMON_STEP_FIELDS = frozenset(
    {"id", "type", "timeout_ms", "retry", "required", "extensions"}
)
_STEP_FIELDS = {
    JourneyStepKind.ACTION: frozenset(
        {"operation", "semantic_target", "arguments"}
    ),
    JourneyStepKind.WAIT: frozenset(
        {"condition", "semantic_target", "duration_ms", "poll_interval_ms"}
    ),
    JourneyStepKind.CAPTURE: frozenset({"role", "artifact_types", "reason"}),
    JourneyStepKind.ASSERT: frozenset(
        {"assertion", "semantic_target", "expected", "evidence_keys"}
    ),
}
_RETRY_FIELDS = frozenset({"max_attempts", "backoff_ms", "retry_on"})
_EXECUTION_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "execution_plan_id",
        "task_id",
        "journey_version",
        "steps",
        "rule_contract_hashes",
        "capture_plan_id",
    }
)
_JOURNEY_VERSION_FIELDS = frozenset(
    {"schema_version", "definition_id", "version", "content_hash"}
)


def parse_journey_definition(payload: Mapping[str, Any]) -> JourneyDefinition:
    """按版本拒绝未声明字段，不把旧步骤自动转换为检查点。"""
    source = _require_mapping(payload, "Journey Definition")
    checkpoint_mode = source.get("schema_version") == JOURNEY_CHECKPOINT_SCHEMA_VERSION
    fields = (
        (_DEFINITION_FIELDS - {"steps"}) | {"checkpoints"}
        if checkpoint_mode
        else _DEFINITION_FIELDS
    )
    _reject_unknown_fields(source, fields, "Journey Definition")
    steps = [] if checkpoint_mode else _require_list(source.get("steps"), "steps")
    return JourneyDefinition(
        definition_id=source.get("definition_id"),
        version=_require_int(source.get("version"), "version"),
        name=source.get("name"),
        description=source.get("description"),
        surface_kinds=_text_tuple(source.get("surface_kinds"), "surface_kinds"),
        steps=tuple(_parse_step(item, index=index) for index, item in enumerate(steps)),
        checkpoints=_parse_checkpoints(source.get("checkpoints")) if checkpoint_mode else (),
        extensions=_optional_mapping(source.get("extensions"), "extensions"),
        schema_version=source.get("schema_version"),
    )


def parse_journey_execution_plan(payload: Mapping[str, Any]) -> JourneyExecutionPlan:
    """解析任务启动时已经锁定的 Journey 执行快照。"""
    source = _require_mapping(payload, "Journey Execution Plan")
    checkpoint_mode = source.get("schema_version") == JOURNEY_RUN_PLAN_SCHEMA_VERSION
    fields = (
        (_EXECUTION_PLAN_FIELDS - {"steps"})
        | {"checkpoints", "agent_run_id", "scenario_key", "parameters"}
        if checkpoint_mode
        else _EXECUTION_PLAN_FIELDS
    )
    _reject_unknown_fields(source, fields, "Journey Execution Plan")
    if source.get("schema_version") not in (
        JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION,
        JOURNEY_RUN_PLAN_SCHEMA_VERSION,
    ):
        raise JourneyValidationError(
            f"schema_version 必须为 {JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION}"
        )
    version_payload = _require_mapping(
        source.get("journey_version"),
        "journey_version",
    )
    _reject_unknown_fields(
        version_payload,
        _JOURNEY_VERSION_FIELDS,
        "journey_version",
    )
    steps = [] if checkpoint_mode else _require_list(source.get("steps"), "steps")
    return JourneyExecutionPlan(
        execution_plan_id=source.get("execution_plan_id"),
        task_id=source.get("task_id"),
        journey_version=JourneyVersion(
            definition_id=version_payload.get("definition_id"),
            version=_require_int(version_payload.get("version"), "journey_version.version"),
            content_hash=version_payload.get("content_hash"),
            schema_version=version_payload.get("schema_version"),
        ),
        steps=tuple(_parse_step(item, index=index) for index, item in enumerate(steps)),
        checkpoints=_parse_checkpoints(source.get("checkpoints")) if checkpoint_mode else (),
        agent_run_id=source.get("agent_run_id"),
        scenario_key=source.get("scenario_key"),
        parameters=_optional_mapping(source.get("parameters"), "parameters"),
        rule_contract_hashes=_text_tuple(
            source.get("rule_contract_hashes", []),
            "rule_contract_hashes",
        ),
        capture_plan_id=source.get("capture_plan_id"),
        schema_version=source.get("schema_version"),
    )


def _parse_checkpoints(value: Any) -> tuple[JourneyCheckpoint, ...]:
    checkpoints = []
    for item in _require_list(value, "checkpoints"):
        source = _require_mapping(item, "checkpoint")
        _reject_unknown_fields(
            source,
            frozenset(
                {
                    "id",
                    "entry_condition",
                    "expected",
                    "evidence_requirements",
                    "depends_on",
                    "exit_condition",
                    "recovery_policy",
                    "side_effect_policy",
                }
            ),
            "checkpoint",
        )
        requirements = []
        for raw in _require_list(source.get("evidence_requirements"), "evidence_requirements"):
            obligation = _require_mapping(raw, "evidence_requirement")
            _reject_unknown_fields(
                obligation,
                frozenset(
                    {
                        "role",
                        "artifact_types",
                        "minimum_count",
                        "capture_phase",
                    }
                ),
                "evidence_requirement",
            )
            requirements.append(
                JourneyEvidenceRequirement(
                    role=obligation.get("role"),
                    artifact_types=_text_tuple(obligation.get("artifact_types"), "artifact_types"),
                    minimum_count=_require_int(obligation.get("minimum_count", 1), "minimum_count"),
                    capture_phase=obligation.get("capture_phase", "after_action"),
                )
            )
        checkpoints.append(
            JourneyCheckpoint(
                checkpoint_id=source.get("id"),
                entry_condition=_require_mapping(source.get("entry_condition"), "entry_condition"),
                expected=_require_mapping(source.get("expected"), "expected"),
                evidence_requirements=tuple(requirements),
                depends_on=_text_tuple(source.get("depends_on"), "depends_on"),
                exit_condition=_require_mapping(source.get("exit_condition"), "exit_condition"),
                recovery_policy=source.get("recovery_policy"),
                side_effect_policy=source.get("side_effect_policy"),
            )
        )
    return tuple(checkpoints)


def _parse_step(value: Any, *, index: int) -> JourneyStep:
    field_name = f"steps[{index}]"
    source = _require_mapping(value, field_name)
    raw_kind = source.get("type")
    try:
        kind = JourneyStepKind(raw_kind)
    except (TypeError, ValueError) as exc:
        raise JourneyValidationError(f"{field_name}.type 不受支持") from exc
    allowed_fields = _COMMON_STEP_FIELDS | _STEP_FIELDS[kind]
    _reject_unknown_fields(source, allowed_fields, field_name)
    retry = _parse_retry(source.get("retry"), field_name=field_name)
    payload = {key: source[key] for key in _STEP_FIELDS[kind] if key in source}
    return JourneyStep(
        step_id=source.get("id"),
        kind=kind,
        payload=payload,
        timeout_ms=_optional_int(source.get("timeout_ms"), 30_000, f"{field_name}.timeout_ms"),
        retry=retry,
        required=_optional_bool(source.get("required"), True, f"{field_name}.required"),
        extensions=_optional_mapping(source.get("extensions"), f"{field_name}.extensions"),
    )


def _parse_retry(value: Any, *, field_name: str) -> JourneyRetryPolicy:
    if value is None:
        return JourneyRetryPolicy()
    source = _require_mapping(value, f"{field_name}.retry")
    _reject_unknown_fields(source, _RETRY_FIELDS, f"{field_name}.retry")
    return JourneyRetryPolicy(
        max_attempts=_optional_int(
            source.get("max_attempts"),
            1,
            f"{field_name}.retry.max_attempts",
        ),
        backoff_ms=_optional_int(
            source.get("backoff_ms"),
            0,
            f"{field_name}.retry.backoff_ms",
        ),
        retry_on=_text_tuple(source.get("retry_on", []), f"{field_name}.retry.retry_on"),
    )


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise JourneyValidationError(f"{field_name} 必须为对象")
    if any(not isinstance(key, str) for key in value):
        raise JourneyValidationError(f"{field_name} 的字段名必须为字符串")
    return value


def _optional_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _require_mapping(value, field_name)


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise JourneyValidationError(f"{field_name} 必须为数组")
    return value


def _text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    return tuple(_require_list(value, field_name))


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JourneyValidationError(f"{field_name} 必须为整数")
    return value


def _optional_int(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    return _require_int(value, field_name)


def _optional_bool(value: Any, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise JourneyValidationError(f"{field_name} 必须为布尔值")
    return value


def _reject_unknown_fields(
    source: Mapping[str, Any], allowed: frozenset[str], field_name: str
) -> None:
    unknown = sorted(set(source).difference(allowed))
    if unknown:
        raise JourneyValidationError(
            f"{field_name} 包含未声明字段：{', '.join(str(item) for item in unknown)}"
        )
