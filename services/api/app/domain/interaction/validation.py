"""交互合同共用的不可变 JSON 与边界校验。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

RUNTIME_HANDLE_KEYS = frozenset(
    {
        "browser",
        "browser_handle",
        "connection",
        "db_session",
        "driver",
        "lease_token",
        "page",
        "page_handle",
        "repository",
        "row_version",
        "session_handle",
        "session_lease",
        "webdriver",
    }
)
PLATFORM_LOCATOR_KEYS = frozenset(
    {
        "accessibility_id",
        "android_uiautomator",
        "appium",
        "appium_locator",
        "class_chain",
        "css",
        "css_selector",
        "element_id",
        "ios_predicate",
        "locator",
        "locators",
        "predicate",
        "resource_id",
        "selector",
        "target",
        "web_selector",
        "xpath",
    }
)
ACTION_PARAMETER_FORBIDDEN_KEYS = RUNTIME_HANDLE_KEYS | PLATFORM_LOCATOR_KEYS
_PLATFORM_LOCATOR_PREFIXES = (
    "#",
    ".",
    "//",
    "(//",
    "accessibility=",
    "accessibility:",
    "appium=",
    "appium:",
    "css=",
    "css:",
    "id=",
    "resource-id=",
    "xpath=",
    "xpath:",
)


class InteractionValidationError(ValueError):
    """交互意图、执行结果或采集引用不满足稳定合同。"""


def require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InteractionValidationError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return require_text(value, field_name)


def validate_semantic_target(value: Any, field_name: str) -> str:
    target = require_text(value, field_name)
    if target.casefold().startswith(_PLATFORM_LOCATOR_PREFIXES):
        raise InteractionValidationError(f"{field_name} 必须是语义目标，不能是平台定位器")
    return target


def as_tuple(value: Any, field_name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InteractionValidationError(f"{field_name} 必须为数组")
    return tuple(value)


def validate_unique_texts(values: Sequence[Any], field_name: str) -> None:
    normalized = tuple(require_text(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise InteractionValidationError(f"{field_name} 不能包含重复值")


def freeze_mapping(
    value: Mapping[str, Any],
    field_name: str,
    *,
    forbidden_keys: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InteractionValidationError(f"{field_name} 必须为对象")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise InteractionValidationError(f"{field_name} 包含非法字段名")
        if key.casefold() in forbidden_keys:
            raise InteractionValidationError(f"{field_name} 包含禁止字段：{key}")
        frozen[key] = _freeze_json(
            item,
            field_name,
            forbidden_keys=forbidden_keys,
        )
    return MappingProxyType(frozen)


def thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}


def _freeze_json(
    value: Any,
    field_name: str,
    *,
    forbidden_keys: frozenset[str],
) -> Any:
    if isinstance(value, Mapping):
        return freeze_mapping(
            value,
            field_name,
            forbidden_keys=forbidden_keys,
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, field_name, forbidden_keys=forbidden_keys) for item in value
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise InteractionValidationError(f"{field_name} 不能包含 NaN 或无穷大")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise InteractionValidationError(f"{field_name} 只能包含 JSON 兼容值")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
