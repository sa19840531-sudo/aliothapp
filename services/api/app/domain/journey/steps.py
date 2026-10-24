"""Journey DSL 的确定性步骤与重试策略。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.journey.validation import (
    JourneyValidationError,
    as_tuple,
    freeze_mapping,
    optional_positive_int,
    optional_text,
    reject_platform_locators,
    require_text,
    thaw_mapping,
    validate_semantic_target,
    validate_unique_texts,
)

JOURNEY_ACTION_OPERATIONS = frozenset(
    item.value
    for item in InteractionOperation
    if item
    not in {
        InteractionOperation.TIMED_CAPTURE,
        InteractionOperation.NETWORK_FAULT,
    }
)
JOURNEY_WAIT_CONDITIONS = frozenset(
    {
        "surface_stable",
        "element_visible",
        "element_hidden",
        "url_changed",
        "duration",
    }
)


class JourneyStepKind(StrEnum):
    """V1 支持的确定性步骤类型。"""

    ACTION = "action"
    WAIT = "wait"
    CAPTURE = "capture"
    ASSERT = "assert"


@dataclass(frozen=True, kw_only=True)
class JourneyRetryPolicy:
    """步骤级重试策略；不承担工作项级租约重试。"""

    max_attempts: int = 1
    backoff_ms: int = 0
    retry_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts <= 0
        ):
            raise JourneyValidationError("retry.max_attempts 必须为正整数")
        if (
            isinstance(self.backoff_ms, bool)
            or not isinstance(self.backoff_ms, int)
            or self.backoff_ms < 0
        ):
            raise JourneyValidationError("retry.backoff_ms 不能小于 0")
        retry_on = as_tuple(self.retry_on, "retry.retry_on")
        validate_unique_texts(retry_on, "retry.retry_on")
        object.__setattr__(self, "retry_on", retry_on)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_ms": self.backoff_ms,
            "retry_on": list(self.retry_on),
        }


@dataclass(frozen=True, kw_only=True)
class JourneyStep:
    """一个不持有 Driver 的声明式 Journey 步骤。"""

    step_id: str
    kind: JourneyStepKind
    payload: Mapping[str, Any]
    timeout_ms: int = 30_000
    retry: JourneyRetryPolicy = field(default_factory=JourneyRetryPolicy)
    required: bool = True
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.step_id, "step_id")
        if not isinstance(self.kind, JourneyStepKind):
            raise JourneyValidationError("kind 必须是 JourneyStepKind")
        if (
            isinstance(self.timeout_ms, bool)
            or not isinstance(self.timeout_ms, int)
            or self.timeout_ms <= 0
        ):
            raise JourneyValidationError("timeout_ms 必须为正整数")
        if not isinstance(self.required, bool):
            raise JourneyValidationError("required 必须为布尔值")
        if not isinstance(self.retry, JourneyRetryPolicy):
            raise JourneyValidationError("retry 必须为 JourneyRetryPolicy")
        frozen_payload = freeze_mapping(self.payload, "payload")
        frozen_extensions = freeze_mapping(self.extensions, "extensions")
        reject_platform_locators(frozen_payload, "payload")
        reject_platform_locators(frozen_extensions, "extensions")
        _validate_step_payload(self.kind, frozen_payload)
        object.__setattr__(self, "payload", frozen_payload)
        object.__setattr__(self, "extensions", frozen_extensions)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.step_id,
            "type": self.kind.value,
            **thaw_mapping(self.payload),
            "timeout_ms": self.timeout_ms,
            "retry": self.retry.to_dict(),
            "required": self.required,
        }
        if self.extensions:
            payload["extensions"] = thaw_mapping(self.extensions)
        return payload


def _validate_step_payload(kind: JourneyStepKind, payload: Mapping[str, Any]) -> None:
    allowed_fields = {
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
    }[kind]
    unknown = sorted(set(payload).difference(allowed_fields))
    if unknown:
        raise JourneyValidationError(
            f"{kind.value} 包含未声明字段：{', '.join(unknown)}"
        )
    if kind == JourneyStepKind.ACTION:
        operation = require_text(payload.get("operation"), "action.operation")
        if operation not in JOURNEY_ACTION_OPERATIONS:
            raise JourneyValidationError("action.operation 不受支持")
        semantic_target = payload.get("semantic_target")
        if operation in {"navigate", "click", "tap", "input", "hover"}:
            validate_semantic_target(semantic_target, "action.semantic_target")
        elif semantic_target is not None:
            validate_semantic_target(semantic_target, "action.semantic_target")
        arguments = payload.get("arguments")
        if arguments is not None and not isinstance(arguments, Mapping):
            raise JourneyValidationError("action.arguments 必须为对象")
        return
    if kind == JourneyStepKind.WAIT:
        condition = payload.get("condition")
        duration_ms = payload.get("duration_ms")
        if condition is None and duration_ms is None:
            raise JourneyValidationError("wait 必须提供 condition 或 duration_ms")
        optional_text(condition, "wait.condition")
        if condition is not None and condition not in JOURNEY_WAIT_CONDITIONS:
            raise JourneyValidationError("wait.condition 不受支持")
        if condition == "duration" and duration_ms is None:
            raise JourneyValidationError("duration 等待必须提供 duration_ms")
        semantic_target = payload.get("semantic_target")
        if condition in {"element_visible", "element_hidden"}:
            validate_semantic_target(semantic_target, "wait.semantic_target")
        elif semantic_target is not None:
            validate_semantic_target(semantic_target, "wait.semantic_target")
        optional_positive_int(duration_ms, "wait.duration_ms")
        optional_positive_int(payload.get("poll_interval_ms"), "wait.poll_interval_ms")
        return
    if kind == JourneyStepKind.CAPTURE:
        require_text(payload.get("role"), "capture.role")
        artifact_types = payload.get("artifact_types")
        if not isinstance(artifact_types, tuple) or not artifact_types:
            raise JourneyValidationError("capture.artifact_types 必须为非空数组")
        validate_unique_texts(artifact_types, "capture.artifact_types")
        optional_text(payload.get("reason"), "capture.reason")
        return
    if kind == JourneyStepKind.ASSERT:
        assertion = require_text(payload.get("assertion"), "assert.assertion")
        semantic_target = payload.get("semantic_target")
        if assertion.startswith("element_"):
            validate_semantic_target(semantic_target, "assert.semantic_target")
        elif semantic_target is not None:
            validate_semantic_target(semantic_target, "assert.semantic_target")
        evidence_keys = payload.get("evidence_keys")
        if evidence_keys is not None:
            if not isinstance(evidence_keys, tuple):
                raise JourneyValidationError("assert.evidence_keys 必须为数组")
            validate_unique_texts(evidence_keys, "assert.evidence_keys")
