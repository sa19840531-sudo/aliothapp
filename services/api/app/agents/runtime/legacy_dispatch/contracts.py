"""V1 WorkingMemory Frontier 同步派发的兼容合同。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

LEGACY_EXPLORATION_DECISION_VERSION = "legacy-exploration-decision.v1"
_SUPPORTED_ACTIONS = frozenset(
    {
        "goto",
        "click",
        "tap",
        "hover",
        "scroll",
        "back",
        "observe",
        "timed_capture",
        "network_fault",
    }
)
_RUNTIME_HANDLE_KEYS = frozenset(
    {
        "browser",
        "connection",
        "driver",
        "lease_token",
        "page_handle",
        "session_handle",
        "session_lease",
    }
)


class LegacyDispatchContractError(ValueError):
    """V1 Frontier 兼容数据缺少稳定动作身份。"""


@dataclass(frozen=True, kw_only=True)
class LegacyExplorationRequest:
    """V1 Main Agent 向同步 Frontier 选择器发起的请求。"""

    blocking_page_capture_id: str | None = None

    def __post_init__(self) -> None:
        if self.blocking_page_capture_id is not None:
            _require_text(self.blocking_page_capture_id, "blocking_page_capture_id")


class LegacyExplorationFrontier(Protocol):
    """WorkingMemory 暴露给迁移期同步选择器的最小端口。"""

    def prepare_next_exploration_action(
        self,
        *,
        blocking_page_capture_id: str | None = None,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, kw_only=True)
class LegacyExplorationDecision:
    """V1 Frontier 已经选定的动作快照，不属于 V2 Explorer Agent 输出。"""

    action_payload: Mapping[str, Any]
    version: str = LEGACY_EXPLORATION_DECISION_VERSION

    def __post_init__(self) -> None:
        if self.version != LEGACY_EXPLORATION_DECISION_VERSION:
            raise LegacyDispatchContractError("LegacyExplorationDecision version 不受支持")
        frozen_payload = _freeze_mapping(self.action_payload, "action_payload")
        _reject_runtime_handles(frozen_payload)
        _require_text(
            frozen_payload.get("exploration_action_id"),
            "exploration_action_id",
        )
        action = _require_text(frozen_payload.get("action"), "action").casefold()
        if action not in _SUPPORTED_ACTIONS:
            raise LegacyDispatchContractError(f"不支持的 V1 探索动作：{action}")
        object.__setattr__(self, "action_payload", frozen_payload)

    @property
    def exploration_action_id(self) -> str:
        return str(self.action_payload["exploration_action_id"])

    def to_action_dict(self) -> dict[str, Any]:
        return _thaw_mapping(self.action_payload)

    @classmethod
    def from_action_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> LegacyExplorationDecision:
        return cls(action_payload=payload)


class LegacyFrontierSelector(Protocol):
    """V1 同步 Frontier 选择器；不能注册为 V2 Agent。"""

    def decide_next(
        self,
        *,
        frontier: LegacyExplorationFrontier,
        request: LegacyExplorationRequest,
    ) -> LegacyExplorationDecision | None: ...


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise LegacyDispatchContractError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _reject_runtime_handles(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(key for key in value if key.casefold() in _RUNTIME_HANDLE_KEYS)
        if forbidden:
            raise LegacyDispatchContractError(
                f"V1 派发快照不能包含运行时句柄：{', '.join(forbidden)}"
            )
        for item in value.values():
            _reject_runtime_handles(item)
    elif isinstance(value, tuple):
        for item in value:
            _reject_runtime_handles(item)


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LegacyDispatchContractError(f"{field_name} 必须为对象")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise LegacyDispatchContractError(f"{field_name} 包含非法字段名")
        frozen[key] = _freeze_json(item, field_name)
    return MappingProxyType(frozen)


def _freeze_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, field_name)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise LegacyDispatchContractError(f"{field_name} 不能包含 NaN 或无穷大")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise LegacyDispatchContractError(f"{field_name} 只能包含 JSON 兼容值")


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
