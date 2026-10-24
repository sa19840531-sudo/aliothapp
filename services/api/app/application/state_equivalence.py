"""不依赖模型的恢复状态等价门禁。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.domain.inspection import (
    EvidenceRequest,
    InvariantComparison,
    RestorationPlan,
    RestorationValidationError,
    StateEquivalenceAssessment,
    StateInvariantDimension,
    StateInvariantSnapshot,
    build_state_equivalence_assessment_id,
    fingerprint_invariant,
    grade_meets_minimum,
    grade_supports,
)

STATE_EQUIVALENCE_EVALUATOR_VERSION = "alioth-state-equivalence.v1"

_ENVIRONMENT_KEYS = frozenset(
    {
        "surface_kind",
        "app_build",
        "device",
        "device_ref",
        "account",
        "account_environment_ref",
        "language",
        "region",
        "experiment",
        "environment",
        "process_identity",
        "app_data_container_ref",
        "deep_link",
        "lifecycle_phase",
        "session_event",
        "process_alive",
        "platform_build_ref",
    }
)
_BUSINESS_KEYS = frozenset(
    {
        "route",
        "url_pattern",
        "product_id",
        "order_id",
        "content_id",
        "page_type",
        "screen_type",
        "package_name",
        "activity",
    }
)
_STRUCTURE_KEYS = frozenset(
    {
        "dom_summary",
        "ui_tree_summary",
        "state_version",
        "server_state",
        "structure_version",
        "screen_id",
        "structure_signature",
        "image_signature",
        "has_ui_tree",
        "safe_area",
    }
)


class StateEquivalenceGate:
    """以目标不变量、原 Capture 和恢复 Capture 三方一致作为通过条件。"""

    def __init__(
        self,
        *,
        evaluator_version: str = STATE_EQUIVALENCE_EVALUATOR_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not evaluator_version.strip():
            raise ValueError("evaluator_version 不能为空")
        self._evaluator_version = evaluator_version
        self._clock = clock or (lambda: datetime.now(UTC))

    def assess(
        self,
        *,
        request: EvidenceRequest,
        plan: RestorationPlan,
        restoration_attempt_id: str,
        original: StateInvariantSnapshot,
        restored: StateInvariantSnapshot,
    ) -> StateEquivalenceAssessment:
        _validate_identity(
            request=request,
            plan=plan,
            original=original,
            restored=restored,
        )
        targets = _normalize_target_invariants(
            request.target_invariants,
            allow_empty_dimensions=(
                original.surface_kind.value == "web"
                and restored.surface_kind.value == "web"
                and original.session_id.startswith("h5:")
                and restored.session_id.startswith("h5:")
            ),
        )
        targets.setdefault(
            (StateInvariantDimension.ENVIRONMENT, "surface_kind"),
            original.surface_kind.value,
        )
        comparisons = tuple(
            _compare_invariant(
                dimension=dimension,
                key=key,
                expected=expected,
                fingerprint_scope=request.request_id,
                original=original,
                restored=restored,
            )
            for (dimension, key), expected in sorted(
                targets.items(),
                key=lambda item: (item[0][0].value, item[0][1]),
            )
        )
        assessment_id = build_state_equivalence_assessment_id(
            restoration_attempt_id=restoration_attempt_id,
            request_id=request.request_id,
            task_id=request.task_id,
            original_capture_id=original.capture_id,
            restored_capture_id=restored.capture_id,
            required_equivalence=request.required_equivalence,
            source_grade=plan.source_grade,
            minimum_source_grade=request.minimum_source_grade,
            comparisons=comparisons,
            evaluator_version=self._evaluator_version,
        )
        passed = all(item.matched for item in comparisons)
        passed = (
            passed
            and grade_supports(plan.source_grade, request.required_equivalence)
            and grade_meets_minimum(plan.source_grade, request.minimum_source_grade)
        )
        return StateEquivalenceAssessment(
            assessment_id=assessment_id,
            restoration_attempt_id=restoration_attempt_id,
            request_id=request.request_id,
            task_id=request.task_id,
            original_capture_id=original.capture_id,
            restored_capture_id=restored.capture_id,
            required_equivalence=request.required_equivalence,
            source_grade=plan.source_grade,
            minimum_source_grade=request.minimum_source_grade,
            comparisons=comparisons,
            passed=passed,
            evaluator_version=self._evaluator_version,
            evaluated_at=self._clock(),
        )


def _validate_identity(
    *,
    request: EvidenceRequest,
    plan: RestorationPlan,
    original: StateInvariantSnapshot,
    restored: StateInvariantSnapshot,
) -> None:
    if plan.request_id != request.request_id or plan.task_id != request.task_id:
        raise RestorationValidationError("恢复计划与 Evidence Request 身份不一致")
    if original.task_id != request.task_id or restored.task_id != request.task_id:
        raise RestorationValidationError("状态快照与 Evidence Request 任务不一致")
    if original.capture_id != request.target_capture_id:
        raise RestorationValidationError("原状态快照不是 Evidence Request 目标 Capture")
    if original.capture_id != plan.original_capture_id:
        raise RestorationValidationError("原状态快照与恢复计划不一致")
    if original.session_id != plan.source_session_id:
        raise RestorationValidationError("原状态快照 Session 与恢复计划不一致")
    if restored.session_id != plan.target_session_id:
        raise RestorationValidationError("恢复状态快照 Session 与恢复计划不一致")
    if restored.capture_id == original.capture_id:
        raise RestorationValidationError("恢复状态快照必须使用新的 Capture")
    if restored.captured_at < original.captured_at:
        raise RestorationValidationError("恢复状态快照不能早于原 Capture")
    if restored.captured_at > plan.deadline:
        raise RestorationValidationError("恢复状态快照已超过恢复计划截止时间")
    if original.surface_kind is not plan.surface_kind:
        raise RestorationValidationError("原状态快照 Surface 与恢复计划不一致")
    if restored.surface_kind is not plan.surface_kind:
        raise RestorationValidationError("恢复状态快照 Surface 与恢复计划不一致")


def _normalize_target_invariants(
    values: Mapping[str, Any],
    *,
    allow_empty_dimensions: bool = False,
) -> dict[tuple[StateInvariantDimension, str], Any]:
    result: dict[tuple[StateInvariantDimension, str], Any] = {}
    for key, value in values.items():
        try:
            dimension = StateInvariantDimension(key)
        except ValueError:
            dimension = _classify_flat_key(key)
            normalized_key = key
        else:
            # H5 请求保留无约束维度的空对象；仅忽略空维度，不改请求身份或已声明约束。
            if allow_empty_dimensions and isinstance(value, Mapping) and not value:
                continue
            if not isinstance(value, Mapping) or not value:
                raise RestorationValidationError(f"target_invariants.{key} 必须为非空对象")
            for nested_key, nested_value in value.items():
                result[(dimension, nested_key)] = nested_value
            continue
        result[(dimension, normalized_key)] = value
    if not result:
        raise RestorationValidationError("target_invariants 不能为空")
    return result


def _classify_flat_key(key: str) -> StateInvariantDimension:
    if key in _ENVIRONMENT_KEYS:
        return StateInvariantDimension.ENVIRONMENT
    if key in _BUSINESS_KEYS:
        return StateInvariantDimension.BUSINESS
    if key in _STRUCTURE_KEYS:
        return StateInvariantDimension.STRUCTURE
    return StateInvariantDimension.SEMANTIC


def _compare_invariant(
    *,
    dimension: StateInvariantDimension,
    key: str,
    expected: Any,
    fingerprint_scope: str,
    original: StateInvariantSnapshot,
    restored: StateInvariantSnapshot,
) -> InvariantComparison:
    expected_fingerprint = fingerprint_invariant(expected, scope=fingerprint_scope)
    original_value = _snapshot_value(original, dimension=dimension, key=key)
    restored_value = _snapshot_value(restored, dimension=dimension, key=key)
    original_fingerprint = (
        fingerprint_invariant(original_value, scope=fingerprint_scope)
        if original_value is not _MISSING
        else None
    )
    restored_fingerprint = (
        fingerprint_invariant(restored_value, scope=fingerprint_scope)
        if restored_value is not _MISSING
        else None
    )
    return InvariantComparison(
        dimension=dimension,
        key=key,
        expected_fingerprint=expected_fingerprint,
        original_fingerprint=original_fingerprint,
        restored_fingerprint=restored_fingerprint,
        matched=(expected_fingerprint == original_fingerprint == restored_fingerprint),
    )


_MISSING = object()


def _snapshot_value(
    snapshot: StateInvariantSnapshot,
    *,
    dimension: StateInvariantDimension,
    key: str,
) -> Any:
    if dimension is StateInvariantDimension.ENVIRONMENT and key == "surface_kind":
        return snapshot.surface_kind.value
    return snapshot.values_for(dimension).get(key, _MISSING)
