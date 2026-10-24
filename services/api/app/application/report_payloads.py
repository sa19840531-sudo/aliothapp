"""最终报告载荷的 JSON 协议归一化。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Any

from services.api.app.domain.protocol import ProtocolContractError, stable_protocol_hash


def normalize_report_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """把最终报告对象归一化为可写入 JSONB、可稳定哈希的结构。"""
    if not isinstance(value, Mapping):
        raise ProtocolContractError("final_report 必须为对象")
    normalized = normalize_report_json_value(value)
    if not isinstance(normalized, dict):
        raise ProtocolContractError("final_report 必须为对象")
    return normalized


def stable_report_payload_hash(value: Mapping[str, Any]) -> str:
    """基于归一化后的最终报告生成稳定哈希。"""
    return stable_protocol_hash(normalize_report_payload(value))


def normalize_report_json_value(value: Any) -> Any:
    """递归处理报告内允许出现的 Python 值。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[_require_report_object_key(key)] = normalize_report_json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [normalize_report_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProtocolContractError(
        f"report_payload 包含不支持的 JSON 类型: {type(value).__name__}"
    )


def _require_report_object_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise ProtocolContractError("report_payload 只能包含字符串键")
