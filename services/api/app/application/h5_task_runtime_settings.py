"""H5 V2 Run 对锁定任务策略的运行时映射。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.agents.runtime.task_execution_policy import (
    InvalidTaskExecutionPolicyError,
    resolve_task_runtime_settings,
)
from services.api.app.core.config import AgentRuntimeSettings
from services.api.app.domain.page.registry import get_default_page_type_registry

_SUPPORTED_SCAN_MODES = frozenset({"quick", "full", "custom"})
_MAX_ACTIONS_BY_SCAN_MODE = {
    "quick": 40,
    "custom": 60,
    "full": 80,
}
_MAX_AI_CALLS_BY_SCAN_MODE = {
    "quick": 64,
    "custom": 72,
    "full": 200,
}
_MAX_DEPTH_BY_SCAN_MODE = {
    "quick": 2,
    "custom": 3,
    "full": 4,
}


@dataclass(frozen=True, slots=True)
class H5TaskRuntimeSettings:
    """已锁定 H5 Run 在组合根和执行器之间传递的唯一预算。"""

    scan_mode: str
    max_actions: int
    max_depth: int
    max_duration_seconds: float
    max_pages: int | None
    journey_steps: tuple[str, ...]
    include_page_types: tuple[str, ...]
    exclude_page_types: tuple[str, ...]
    max_ai_calls: int = 200
    max_total_tokens: int | None = None

    @property
    def sampling_policy_summary(self) -> dict[str, object]:
        """返回可随 Frontier 持久化的页型采样策略摘要。"""
        return {
            "max_pages": self.max_pages,
            "journey_steps": list(self.journey_steps),
            "include_page_types": list(self.include_page_types),
            "exclude_page_types": list(self.exclude_page_types),
        }


def build_h5_task_runtime_settings(
    *,
    task_config: Mapping[str, object],
    loop_guard: Mapping[str, object],
    page_sampling_policy: Mapping[str, object],
    defaults: AgentRuntimeSettings,
) -> H5TaskRuntimeSettings:
    """解析并校验 H5 Run 已锁定的护栏，任何歧义均拒绝执行。"""
    _require_mapping(task_config, "task_config")
    _require_mapping(loop_guard, "loop_guard")
    _require_mapping(page_sampling_policy, "page_sampling_policy")
    if not isinstance(defaults, AgentRuntimeSettings):
        raise TypeError("defaults 必须为 AgentRuntimeSettings")
    mode = _scan_mode(task_config.get("scan_mode"))
    resolved = resolve_task_runtime_settings(defaults, loop_guard, page_sampling_policy)
    max_actions, max_ai_calls = h5_scan_mode_runtime_limits(mode)
    configured_token_limit = _optional_positive_int(loop_guard, "max_total_tokens")
    max_depth = _effective_max_depth(
        page_sampling_policy,
        loop_guard,
        task_config,
        scan_mode=mode,
    )
    max_pages = _optional_positive_int(page_sampling_policy, "max_pages")
    journey_steps = _text_tuple(task_config.get("journey_steps"), "journey_steps")
    include_page_types = _page_type_tuple(
        page_sampling_policy.get("include_page_types"), "include_page_types"
    )
    exclude_page_types = _page_type_tuple(
        page_sampling_policy.get("exclude_page_types"), "exclude_page_types"
    )
    _validate_page_type_policy(include_page_types, exclude_page_types)
    return H5TaskRuntimeSettings(
        scan_mode=mode,
        max_actions=min(resolved.max_steps, max_actions),
        max_depth=max_depth,
        max_duration_seconds=resolved.max_duration_seconds,
        max_pages=max_pages,
        journey_steps=journey_steps,
        include_page_types=include_page_types,
        exclude_page_types=exclude_page_types,
        max_ai_calls=min(resolved.max_iterations, max_ai_calls),
        max_total_tokens=configured_token_limit,
    )


def h5_scan_mode_runtime_limits(scan_mode: str) -> tuple[int, int]:
    """返回 H5 扫描模式的动作和模型调用硬上限。"""
    mode = _scan_mode(scan_mode)
    return (
        _MAX_ACTIONS_BY_SCAN_MODE[mode],
        _MAX_AI_CALLS_BY_SCAN_MODE[mode],
    )


def _require_mapping(value: object, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise InvalidTaskExecutionPolicyError(f"{field_name} 必须为对象")


def _scan_mode(value: object) -> str:
    if value is None:
        return "full"
    if not isinstance(value, str) or value != value.strip() or value not in _SUPPORTED_SCAN_MODES:
        raise InvalidTaskExecutionPolicyError("scan_mode 必须为 quick、full 或 custom")
    return value


def _effective_max_depth(
    page_sampling_policy: Mapping[str, object],
    loop_guard: Mapping[str, object],
    task_config: Mapping[str, object],
    *,
    scan_mode: str,
) -> int:
    values = tuple(
        _non_negative_int(value, key)
        for key, value in (
            ("depth_limit", page_sampling_policy.get("depth_limit")),
            ("max_depth", loop_guard.get("max_depth")),
            ("max_depth", task_config.get("max_depth")),
        )
        if value is not None
    )
    return min(values) if values else _MAX_DEPTH_BY_SCAN_MODE[scan_mode]


def _optional_positive_int(mapping: Mapping[str, object], key: str) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    result = _non_negative_int(value, key)
    if result <= 0:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为正整数")
    return result


def _non_negative_int(value: object, key: str) -> int:
    if isinstance(value, bool):
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为非负整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为非负整数") from exc
    if (isinstance(value, float) and not value.is_integer()) or result < 0:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为非负整数")
    return result


def _text_tuple(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为字符串列表")
    values = tuple(value)
    if any(
        not isinstance(item, str) or not item.strip() or item != item.strip() for item in values
    ):
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为非空且无首尾空白的字符串列表")
    if len(set(values)) != len(values):
        raise InvalidTaskExecutionPolicyError(f"{key} 不能包含重复值")
    return values


def _validate_page_type_policy(
    included_values: tuple[str, ...],
    excluded_values: tuple[str, ...],
) -> None:
    lifecycle_codes = set(get_default_page_type_registry().lifecycle_codes)
    invalid = sorted(set(included_values).intersection(lifecycle_codes))
    if invalid:
        raise InvalidTaskExecutionPolicyError(
            "include_page_types 不能包含生命周期页面类型：" + ", ".join(invalid)
        )
    included = set(included_values)
    excluded = set(excluded_values)
    if included.intersection(excluded):
        raise InvalidTaskExecutionPolicyError("include_page_types 与 exclude_page_types 不能重叠")


def _page_type_tuple(value: object, key: str) -> tuple[str, ...]:
    """读取页面类型时统一大小写，且拒绝生命周期类型进入覆盖分母。"""
    return tuple(item.casefold() for item in _text_tuple(value, key))
