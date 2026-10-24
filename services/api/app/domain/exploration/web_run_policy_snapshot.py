"""Web Run 策略正文：规范摘要、深层不可变与支持版本边界。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from services.api.app.domain.exploration.web_full_page_policy import WebFullPageStabilizationPolicy

MAX_WEB_POLICY_BYTES = 1024 * 1024
WEB_POLICY_VERSIONS = {
    "schema_version": "web-run-policy.v1",
    "validator_version": "web-classification-validator.v1",
    "identity_policy_version": "web-url-identity.v2",
    "readiness_profile_version": "web-structure.v1",
    "workflow_skill_version": "1.0.0",
}

WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION = "web-full-page-supplemental.v1"
WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_BYTES = 16 * 1024 * 1024
WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION = 32768
WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_PIXELS = 100_000_000


class WebRunPolicyError(ValueError):
    """缺少可信历史或正文验证失败时阻止当前 Run 继续。"""


@dataclass(frozen=True, kw_only=True)
class WebFullPageSupplementalPolicy:
    """仅属于 Web V2 Run 的冻结全页补图策略。"""

    enabled: bool
    protocol_version: str = WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION
    capture_role: str = "supplemental"
    maximum_bytes: int = WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_BYTES
    maximum_dimension: int = WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION
    maximum_pixels: int = WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_PIXELS
    stabilization: WebFullPageStabilizationPolicy = field(
        default_factory=WebFullPageStabilizationPolicy
    )

    def __post_init__(self) -> None:
        expected = {
            "protocol_version": WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION,
            "capture_role": "supplemental",
            "maximum_bytes": WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_BYTES,
            "maximum_dimension": WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION,
            "maximum_pixels": WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_PIXELS,
        }
        if type(self.enabled) is not bool:
            raise ValueError("full_page_supplemental_policy.enabled 必须为布尔值")
        for field_name, expected_value in expected.items():
            if getattr(self, field_name) != expected_value:
                raise ValueError(f"full_page_supplemental_policy.{field_name} 不受支持")

    def to_payload(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "enabled": self.enabled,
            "capture_role": self.capture_role,
            "maximum_bytes": self.maximum_bytes,
            "maximum_dimension": self.maximum_dimension,
            "maximum_pixels": self.maximum_pixels,
            "stabilization": self.stabilization.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> WebFullPageSupplementalPolicy:
        expected_fields = {
            "protocol_version",
            "enabled",
            "capture_role",
            "maximum_bytes",
            "maximum_dimension",
            "maximum_pixels",
        }
        if set(payload) not in (expected_fields, expected_fields | {"stabilization"}):
            raise ValueError("full_page_supplemental_policy 必须包含完整冻结配置")
        values = dict(payload)
        if "stabilization" in values:
            values["stabilization"] = WebFullPageStabilizationPolicy.from_payload(
                values["stabilization"]
            )
        return cls(**values)


def mutable_json(value: object) -> object:
    """仅接受 JSON 值并隔离所有嵌套引用，禁止隐式字符串转换。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise WebRunPolicyError("WEB_POLICY_JSON_INVALID: 对象键必须为字符串")
        return {key: mutable_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [mutable_json(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        # JSONB 往返可能把整数形式浮点数编码为整数；统一规范数字。
        return int(value) if value.is_integer() else value
    raise WebRunPolicyError("WEB_POLICY_JSON_INVALID: 非 JSON 或非有限值")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_policy_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        mutable_json(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, kw_only=True)
class WebRunPolicySnapshot:
    task_id: str
    agent_run_id: str
    payload: Mapping[str, object]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.task_id or not self.agent_run_id:
            raise WebRunPolicyError("WEB_POLICY_OWNERSHIP_INVALID: 缺少 Task/Run")
        canonical = canonical_policy_json(self.payload)
        if len(canonical.encode("utf-8")) > MAX_WEB_POLICY_BYTES:
            raise WebRunPolicyError("WEB_POLICY_TOO_LARGE: 正文超过 1 MiB")
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != self.payload_sha256:
            raise WebRunPolicyError("WEB_POLICY_SHA256_MISMATCH: 冻结正文摘要不符")
        for key, version in WEB_POLICY_VERSIONS.items():
            if self.payload.get(key) != version:
                raise WebRunPolicyError(f"WEB_POLICY_VERSION_UNSUPPORTED: {key}")
        for key in (
            "page_catalog",
            "menu_priors",
            "readiness_profile",
            "discovery_policy",
            "budget",
        ):
            if not isinstance(self.payload.get(key), Mapping) or not self.payload[key]:
                raise WebRunPolicyError(f"WEB_POLICY_BODY_MISSING: {key}")
        for key in ("guide_version", "workflow_skill"):
            if not isinstance(self.payload.get(key), str) or not self.payload[key].strip():
                raise WebRunPolicyError(f"WEB_POLICY_BODY_MISSING: {key}")
        object.__setattr__(self, "payload", _freeze(mutable_json(self.payload)))

    @classmethod
    def create(cls, *, task_id: str, agent_run_id: str, payload: Mapping[str, object]):
        digest = hashlib.sha256(canonical_policy_json(payload).encode("utf-8")).hexdigest()
        return cls(
            task_id=task_id, agent_run_id=agent_run_id, payload=payload, payload_sha256=digest
        )

    def to_payload(self) -> dict[str, object]:
        return mutable_json(self.payload)
