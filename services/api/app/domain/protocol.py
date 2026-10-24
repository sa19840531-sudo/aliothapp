"""跨模块协议共用的消息信封、边界校验与兼容解析。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

LEGACY_MESSAGE_ENVELOPE_PROTOCOL_VERSION = "message-envelope.v1"
MESSAGE_ENVELOPE_PROTOCOL_VERSION = "message-envelope.v2"
SUPPORTED_MESSAGE_ENVELOPE_PROTOCOL_VERSIONS = frozenset(
    {
        LEGACY_MESSAGE_ENVELOPE_PROTOCOL_VERSION,
        MESSAGE_ENVELOPE_PROTOCOL_VERSION,
    }
)
MAX_PROTOCOL_PAYLOAD_BYTES = 256_000

_RUNTIME_HANDLE_KEYS = frozenset(
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


class ProtocolContractError(ValueError):
    """跨模块消息不是有界、可持久化且可兼容解析的纯数据。"""


def require_text(value: Any, field_name: str) -> str:
    """校验无首尾空白的非空文本。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProtocolContractError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def optional_text(value: Any, field_name: str) -> str | None:
    """校验可选文本。"""
    if value is None:
        return None
    return require_text(value, field_name)


def require_aware_datetime(value: Any, field_name: str) -> datetime:
    """校验带时区时间。"""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ProtocolContractError(f"{field_name} 必须是带时区的 datetime")
    return value


def parse_aware_datetime(value: Any, field_name: str) -> datetime:
    """从 ISO 8601 文本解析带时区时间。"""
    if isinstance(value, datetime):
        return require_aware_datetime(value, field_name)
    text = require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolContractError(f"{field_name} 不是有效 ISO 8601 时间") from exc
    return require_aware_datetime(parsed, field_name)


def require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    """校验字符串键对象。"""
    if not isinstance(value, Mapping):
        raise ProtocolContractError(f"{field_name} 必须为对象")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise ProtocolContractError(f"{field_name} 包含非法字段名")
    return value


def require_sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    """校验非文本数组。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProtocolContractError(f"{field_name} 必须为数组")
    return tuple(value)


def validate_protocol_payload(
    payload: Mapping[str, Any],
    *,
    field_name: str = "payload",
    max_bytes: int = MAX_PROTOCOL_PAYLOAD_BYTES,
) -> None:
    """验证消息大小、JSON 兼容性并拒绝运行时句柄。"""
    source = require_mapping(payload, field_name)
    _validate_runtime_handles(source, field_name)
    try:
        serialized = json.dumps(
            _thaw_json(source),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolContractError(f"{field_name} 只能包含 JSON 兼容值") from exc
    if len(serialized) > max_bytes:
        raise ProtocolContractError(f"{field_name} 超出 {max_bytes} 字节上限")


def read_known_fields(
    payload: Mapping[str, Any],
    *,
    known_fields: frozenset[str],
    required_fields: frozenset[str] = frozenset(),
    field_name: str = "payload",
    max_bytes: int = MAX_PROTOCOL_PAYLOAD_BYTES,
) -> dict[str, Any]:
    """读取当前版本字段并容忍未来生产者增加的未知字段。"""
    source = require_mapping(payload, field_name)
    validate_protocol_payload(source, field_name=field_name, max_bytes=max_bytes)
    missing = sorted(field for field in required_fields if field not in source)
    if missing:
        raise ProtocolContractError(f"{field_name} 缺少必填字段：{', '.join(missing)}")
    return {key: source[key] for key in known_fields if key in source}


def freeze_json_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    """冻结一份经过协议边界校验的 JSON 对象。"""
    source = require_mapping(value, field_name)
    validate_protocol_payload(source, field_name=field_name)
    return _freeze_mapping(source, field_name)


def thaw_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """把冻结对象还原为可序列化字典。"""
    return {key: _thaw_json(item) for key, item in value.items()}


def stable_protocol_hash(value: Any) -> str:
    """对 JSON 兼容值生成稳定 SHA-256。"""
    try:
        serialized = json.dumps(
            _thaw_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolContractError("哈希输入只能包含 JSON 兼容值") from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ProtocolEnvelope:
    """跨 Worker 持久化消息的统一外层信封。"""

    message_id: str
    message_type: str
    payload_protocol_version: str
    idempotency_key: str
    task_id: str
    occurred_at: datetime
    payload: Mapping[str, Any]
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    aggregate_version: int | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    protocol_version: str = MESSAGE_ENVELOPE_PROTOCOL_VERSION
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "message_id",
            "message_type",
            "payload_protocol_version",
            "idempotency_key",
            "task_id",
        ):
            require_text(getattr(self, field_name), field_name)
        optional_text(self.correlation_id, "correlation_id")
        optional_text(self.causation_id, "causation_id")
        require_aware_datetime(self.occurred_at, "occurred_at")
        if self.protocol_version not in SUPPORTED_MESSAGE_ENVELOPE_PROTOCOL_VERSIONS:
            raise ProtocolContractError("ProtocolEnvelope protocol_version 不受支持")
        if self.protocol_version == MESSAGE_ENVELOPE_PROTOCOL_VERSION:
            require_text(self.aggregate_type, "aggregate_type")
            require_text(self.aggregate_id, "aggregate_id")
            _require_non_negative_int(self.aggregate_version, "aggregate_version")
            expected_message_id = build_event_message_id(
                message_type=self.message_type,
                task_id=self.task_id,
                aggregate_type=self.aggregate_type,
                aggregate_id=self.aggregate_id,
                aggregate_version=self.aggregate_version,
            )
            if self.message_id != expected_message_id:
                raise ProtocolContractError("message_id 与事件聚合身份不一致")
        elif any(
            value is not None
            for value in (
                self.aggregate_type,
                self.aggregate_id,
                self.aggregate_version,
            )
        ):
            raise ProtocolContractError("V1 ProtocolEnvelope 不能携带 V2 聚合身份")
        payload = freeze_json_mapping(self.payload, "payload")
        extensions = freeze_json_mapping(self.extensions, "extensions")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "extensions", extensions)

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 PostgreSQL JSONB 或消息队列的纯数据。"""
        result = {
            "protocol_version": self.protocol_version,
            "message_id": self.message_id,
            "message_type": self.message_type,
            "payload_protocol_version": self.payload_protocol_version,
            "idempotency_key": self.idempotency_key,
            "task_id": self.task_id,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": thaw_json_mapping(self.payload),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }
        if self.protocol_version == MESSAGE_ENVELOPE_PROTOCOL_VERSION:
            result.update(
                {
                    "aggregate_type": self.aggregate_type,
                    "aggregate_id": self.aggregate_id,
                    "aggregate_version": self.aggregate_version,
                }
            )
        if self.extensions:
            result["extensions"] = thaw_json_mapping(self.extensions)
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProtocolEnvelope:
        """兼容解析当前信封并忽略未来版本新增的顶层字段。"""
        values = read_known_fields(
            payload,
            known_fields=frozenset(
                {
                    "protocol_version",
                    "message_id",
                    "message_type",
                    "payload_protocol_version",
                    "idempotency_key",
                    "task_id",
                    "aggregate_type",
                    "aggregate_id",
                    "aggregate_version",
                    "occurred_at",
                    "payload",
                    "correlation_id",
                    "causation_id",
                    "extensions",
                }
            ),
            required_fields=frozenset(
                {
                    "protocol_version",
                    "message_id",
                    "message_type",
                    "payload_protocol_version",
                    "idempotency_key",
                    "task_id",
                    "occurred_at",
                    "payload",
                }
            ),
            field_name="ProtocolEnvelope",
        )
        protocol_version = values["protocol_version"]
        is_v2 = protocol_version == MESSAGE_ENVELOPE_PROTOCOL_VERSION
        return cls(
            message_id=values["message_id"],
            message_type=values["message_type"],
            payload_protocol_version=values["payload_protocol_version"],
            idempotency_key=values["idempotency_key"],
            task_id=values["task_id"],
            occurred_at=parse_aware_datetime(values["occurred_at"], "occurred_at"),
            payload=require_mapping(values["payload"], "payload"),
            aggregate_type=values.get("aggregate_type") if is_v2 else None,
            aggregate_id=values.get("aggregate_id") if is_v2 else None,
            aggregate_version=values.get("aggregate_version") if is_v2 else None,
            correlation_id=values.get("correlation_id"),
            causation_id=values.get("causation_id"),
            protocol_version=protocol_version,
            extensions=require_mapping(values.get("extensions", {}), "extensions"),
        )


def build_protocol_envelope(
    *,
    message_type: str,
    payload_protocol_version: str,
    idempotency_key: str,
    task_id: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    occurred_at: datetime,
    payload: Mapping[str, Any],
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> ProtocolEnvelope:
    """基于聚合事实版本生成与时间、载荷无关的事件身份。"""
    return ProtocolEnvelope(
        message_id=build_event_message_id(
            message_type=message_type,
            task_id=task_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
        ),
        message_type=message_type,
        payload_protocol_version=payload_protocol_version,
        idempotency_key=idempotency_key,
        task_id=task_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        occurred_at=occurred_at,
        payload=payload,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def build_event_message_id(
    *,
    message_type: str,
    task_id: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
) -> str:
    """生成 V2 事件事实 ID；字段名继续兼容现有 `message_id`。"""
    identity = {
        "protocol_version": MESSAGE_ENVELOPE_PROTOCOL_VERSION,
        "event_type": require_text(message_type, "message_type"),
        "task_id": require_text(task_id, "task_id"),
        "aggregate_type": require_text(aggregate_type, "aggregate_type"),
        "aggregate_id": require_text(aggregate_id, "aggregate_id"),
        "aggregate_version": _require_non_negative_int(
            aggregate_version,
            "aggregate_version",
        ),
    }
    return f"event:{stable_protocol_hash(identity)}"


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolContractError(f"{field_name} 必须为非负整数")
    return value


def _validate_runtime_handles(value: Any, field_name: str) -> None:
    if isinstance(value, Mapping):
        invalid_keys = sorted(
            str(key)
            for key in value
            if not isinstance(key, str) or not key.strip() or key != key.strip()
        )
        if invalid_keys:
            raise ProtocolContractError(f"{field_name} 包含非法字段名")
        forbidden = sorted(
            key
            for key in value
            if isinstance(key, str) and key.casefold() in _RUNTIME_HANDLE_KEYS
        )
        if forbidden:
            raise ProtocolContractError(
                f"{field_name} 包含运行时句柄字段：{', '.join(forbidden)}"
            )
        for item in value.values():
            _validate_runtime_handles(item, field_name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_runtime_handles(item, field_name)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ProtocolContractError(f"{field_name} 不能包含 NaN 或无穷大")


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ProtocolContractError(f"{field_name} 包含非法字段名")
        frozen[key] = _freeze_json(item, field_name)
    return MappingProxyType(frozen)


def _freeze_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, field_name)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolContractError(f"{field_name} 不能包含 NaN 或无穷大")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProtocolContractError(f"{field_name} 只能包含 JSON 兼容值")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return thaw_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value
