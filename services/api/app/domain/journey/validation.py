"""Journey 领域对象共用的规范化与稳定哈希能力。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

_PLATFORM_LOCATOR_KEYS = frozenset(
    {
        "selector",
        "target",
        "locator",
        "locators",
        "css",
        "css_selector",
        "xpath",
        "appium",
        "appium_locator",
        "web_selector",
        "ios_predicate",
        "android_uiautomator",
        "class_chain",
        "predicate",
        "accessibility_id",
        "resource_id",
        "element_id",
        "driver",
        "page_handle",
        "session_handle",
        "lease_token",
    }
)
_PLATFORM_LOCATOR_PREFIXES = (
    "#",
    ".",
    "//",
    "(//",
    "css=",
    "css:",
    "xpath=",
    "xpath:",
    "appium=",
    "appium:",
    "accessibility=",
    "accessibility:",
    "resource-id=",
    "id=",
)


class JourneyValidationError(ValueError):
    """Journey 定义不符合版本化 DSL 契约。"""


def require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JourneyValidationError(f"{field_name} 不能为空")
    if value != value.strip():
        raise JourneyValidationError(f"{field_name} 不能包含首尾空白")
    return value


def optional_text(value: Any, field_name: str) -> None:
    if value is not None:
        require_text(value, field_name)


def optional_positive_int(value: Any, field_name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise JourneyValidationError(f"{field_name} 必须为正整数")


def as_tuple(value: Any, field_name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise JourneyValidationError(f"{field_name} 必须为数组")
    return tuple(value)


def validate_unique_texts(values: Sequence[Any], field_name: str) -> None:
    normalized = tuple(require_text(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise JourneyValidationError(f"{field_name} 不能包含重复值")


def validate_semantic_target(value: Any, field_name: str) -> str:
    """拒绝把 CSS、XPath 或 Appium 定位器写入可复用 Journey。"""
    target = require_text(value, field_name)
    normalized = target.casefold()
    if normalized.startswith(_PLATFORM_LOCATOR_PREFIXES):
        raise JourneyValidationError(f"{field_name} 必须是语义目标，不能是平台定位器")
    return target


def reject_platform_locators(value: Any, field_name: str) -> None:
    """递归拒绝 Driver 句柄和平台定位字段。"""
    if isinstance(value, Mapping):
        forbidden = sorted(
            str(key)
            for key in value
            if isinstance(key, str) and key.casefold() in _PLATFORM_LOCATOR_KEYS
        )
        if forbidden:
            raise JourneyValidationError(
                f"{field_name} 不能包含平台定位或运行时字段：{', '.join(forbidden)}"
            )
        for item in value.values():
            reject_platform_locators(item, field_name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_platform_locators(item, field_name)


def freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise JourneyValidationError(f"{field_name} 必须为对象")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise JourneyValidationError(f"{field_name} 的键必须为无首尾空白的非空字符串")
        frozen[key] = _freeze_json(item, field_name)
    return MappingProxyType(frozen)


def thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}


def stable_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _freeze_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return freeze_mapping(value, field_name)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise JourneyValidationError(f"{field_name} 不能包含 NaN 或无穷大")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise JourneyValidationError(f"{field_name} 只能包含 JSON 兼容值")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
