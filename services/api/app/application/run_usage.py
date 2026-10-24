"""Agent Run 用量载荷的序列化与合并工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

USAGE_INT_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_hit_tokens",
    "billable_input_tokens",
    "total_tokens",
    "ai_calls",
)
USAGE_FLOAT_KEYS = (
    "input_cost",
    "output_cost",
    "cache_hit_cost",
    "total_cost",
)
USAGE_LABEL_KEYS = (
    "cost_currency",
    "pricing_source",
    "provider",
    "model",
)
USAGE_BOOL_KEYS = ("pricing_configured",)
USAGE_SET_KEYS = ("applied_pricing_tiers",)
USAGE_BREAKDOWN_KEYS = ("pricing_breakdown",)
USAGE_KNOWN_KEYS = frozenset(
    (
        *USAGE_INT_KEYS,
        *USAGE_FLOAT_KEYS,
        *USAGE_LABEL_KEYS,
        *USAGE_BOOL_KEYS,
        *USAGE_SET_KEYS,
        *USAGE_BREAKDOWN_KEYS,
    )
)


def usage_snapshot_to_payload(snapshot: Any | None) -> dict[str, Any] | None:
    """把 UsageSnapshot 或同形对象转为可入库的 JSON 载荷。"""

    if snapshot is None:
        return None
    payload = {
        "input_tokens": getattr(snapshot, "input_tokens", None),
        "output_tokens": getattr(snapshot, "output_tokens", None),
        "cache_hit_tokens": getattr(snapshot, "cache_hit_tokens", None),
        "billable_input_tokens": getattr(snapshot, "billable_input_tokens", None),
        "total_tokens": getattr(snapshot, "total_tokens", None),
        "ai_calls": getattr(snapshot, "ai_calls", None),
        "input_cost": getattr(snapshot, "input_cost", None),
        "output_cost": getattr(snapshot, "output_cost", None),
        "cache_hit_cost": getattr(snapshot, "cache_hit_cost", None),
        "total_cost": getattr(snapshot, "total_cost", None),
        "cost_currency": getattr(snapshot, "cost_currency", None),
        "pricing_source": getattr(snapshot, "pricing_source", None),
        "pricing_configured": getattr(snapshot, "pricing_configured", None),
        "applied_pricing_tiers": list(getattr(snapshot, "applied_pricing_tiers", ()) or ()),
        "pricing_breakdown": list(getattr(snapshot, "pricing_breakdown", ()) or ()),
        "provider": getattr(snapshot, "provider", None),
        "model": getattr(snapshot, "model", None),
    }
    return {key: value for key, value in payload.items() if value is not None}


def merge_usage_payloads(
    current: Mapping[str, Any] | None,
    additional: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """合并 Run 级 usage，并保留调用方写入的非计费辅助字段。"""

    merged = dict(current or {})
    if not additional:
        return merged
    additional_payload = dict(additional)
    for key in USAGE_INT_KEYS:
        value = _sum_optional_int(merged.get(key), additional_payload.get(key))
        if value is not None:
            merged[key] = value
    for key in USAGE_FLOAT_KEYS:
        value = _sum_optional_float(merged.get(key), additional_payload.get(key))
        if value is not None:
            merged[key] = value
    for key in USAGE_LABEL_KEYS:
        value = _text_or_none(additional_payload.get(key))
        if value is not None:
            merged[key] = _merge_label(_text_or_none(merged.get(key)), value)
    for key in USAGE_BOOL_KEYS:
        value = _bool_or_none(additional_payload.get(key))
        if value is not None:
            merged[key] = _merge_bool(_bool_or_none(merged.get(key)), value)
    for key in USAGE_SET_KEYS:
        values = sorted(
            {
                item
                for item in (
                    *_list_text_values(merged.get(key)),
                    *_list_text_values(additional_payload.get(key)),
                )
                if item
            }
        )
        if values:
            merged[key] = values
    merged_breakdown = _merge_pricing_breakdown_payloads(
        merged.get("pricing_breakdown"),
        additional_payload.get("pricing_breakdown"),
    )
    if merged_breakdown:
        merged["pricing_breakdown"] = merged_breakdown
    for key, value in additional_payload.items():
        if key not in USAGE_KNOWN_KEYS and key not in merged:
            merged[key] = value
    return merged


def total_ai_calls_from_usage(usage: Mapping[str, Any] | None) -> int:
    """返回用量载荷中的模型调用次数。"""

    return _int_or_none((usage or {}).get("ai_calls")) or 0


def total_tokens_from_usage(usage: Mapping[str, Any] | None) -> int:
    """返回用量载荷中的总 token 数。"""

    return _int_or_none((usage or {}).get("total_tokens")) or 0


def total_cost_from_usage(usage: Mapping[str, Any] | None) -> float:
    """返回用量载荷中的总成本。"""

    return _float_or_none((usage or {}).get("total_cost")) or 0.0


def _sum_optional_int(left: Any, right: Any) -> int | None:
    left_value = _int_or_none(left)
    right_value = _int_or_none(right)
    if left_value is None and right_value is None:
        return None
    return (left_value or 0) + (right_value or 0)


def _sum_optional_float(left: Any, right: Any) -> float | None:
    left_value = _float_or_none(left)
    right_value = _float_or_none(right)
    if left_value is None and right_value is None:
        return None
    return round((left_value or 0.0) + (right_value or 0.0), 8)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _merge_label(current: str | None, additional: str) -> str:
    if current is None or current == additional:
        return additional
    if current == "mixed":
        return current
    return "mixed"


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _merge_bool(current: bool | None, additional: bool) -> bool:
    if current is None:
        return additional
    return current and additional


def _list_text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _merge_pricing_breakdown_payloads(left: Any, right: Any) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in (*_list_dict_values(left), *_list_dict_values(right)):
        key = "|".join(
            _text_or_none(item.get(field)) or ""
            for field in (
                "pricing_tier",
                "pricing_source",
                "input_price_per_million_tokens",
                "output_price_per_million_tokens",
                "cache_hit_price_per_million_tokens",
            )
        )
        current = grouped.get(key)
        if current is None:
            current = dict(item)
            grouped[key] = current
            continue
        for field in (
            "ai_calls",
            "context_tokens",
            "input_tokens",
            "billable_input_tokens",
            "output_tokens",
            "cache_hit_tokens",
        ):
            current[field] = (_int_or_none(current.get(field)) or 0) + (
                _int_or_none(item.get(field)) or 0
            )
        for field in (
            "input_cost",
            "output_cost",
            "cache_hit_cost",
            "total_cost",
        ):
            current[field] = round(
                (_float_or_none(current.get(field)) or 0.0)
                + (_float_or_none(item.get(field)) or 0.0),
                8,
            )
    return [grouped[key] for key in sorted(grouped)]


def _list_dict_values(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))
