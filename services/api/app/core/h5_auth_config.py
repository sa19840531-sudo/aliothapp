"""H5 主动登录的域名配置与秘密读取边界。"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

_SYNTHETIC_CN_MOBILE_PREFIXES = (
    "138",
    "139",
    "150",
    "151",
    "152",
    "157",
    "158",
    "159",
    "182",
    "183",
    "187",
    "188",
)


class H5AuthConfigError(ValueError):
    """H5 认证配置无法安全解析或访问。"""


class H5AuthSettings(BaseModel):
    """H5 主动登录开关与配置文件位置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    config_path: Path | None = None

    @model_validator(mode="after")
    def validate_enabled_path(self) -> H5AuthSettings:
        if not self.enabled:
            return self
        if self.config_path is None:
            raise ValueError("启用 H5 主动登录时必须配置文件路径")
        if not self.config_path.is_file():
            raise ValueError("H5 主动登录配置文件不存在或不是普通文件")
        return self


@dataclass(frozen=True, slots=True)
class H5CredentialProfileRef:
    """可进入普通编排与审计链的无秘密账号引用。"""

    scope: Literal["domain", "global"]
    hostname: str
    submission_mode: Literal["login", "register_if_available"] = "login"
    allowed_request_hostnames: tuple[str, ...] = ()
    uses_synthetic_phone: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "hostname", normalize_h5_auth_hostname(self.hostname))
        if self.submission_mode not in {"login", "register_if_available"}:
            raise ValueError("submission_mode 必须为 login 或 register_if_available")
        normalized_allowed = tuple(
            normalize_h5_auth_hostname(hostname)
            for hostname in self.allowed_request_hostnames
        )
        if len(normalized_allowed) != len(set(normalized_allowed)):
            raise ValueError("allowed_request_hostnames 不得包含重复 hostname")
        object.__setattr__(self, "allowed_request_hostnames", normalized_allowed)

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "hostname": self.hostname,
            "submission_mode": self.submission_mode,
            "allowed_request_hostnames": list(self.allowed_request_hostnames),
        }


class H5CredentialSecret:
    """只允许专用浏览器 Runtime 短暂解开的秘密值。"""

    __slots__ = ("_password", "_phone")

    def __init__(self, *, phone: SecretStr, password: SecretStr) -> None:
        self._phone = SecretStr(phone.get_secret_value())
        self._password = SecretStr(password.get_secret_value())

    def __repr__(self) -> str:
        return "H5CredentialSecret(redacted)"

    __str__ = __repr__

    def to_safe_dict(self) -> dict[str, str]:
        return {"phone": "redacted", "password": "redacted"}

    def reveal_for_runtime(self) -> tuple[str, str]:
        """仅供专用 Secret Runtime 在持锁执行窗口内取值。"""
        return self._phone.get_secret_value(), self._password.get_secret_value()


class _CredentialEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    phone: SecretStr | None = None
    phone_strategy: Literal["synthetic_cn_mobile"] | None = None
    password: SecretStr
    submission_mode: Literal["login", "register_if_available"] = "login"
    allowed_request_hostnames: tuple[str, ...] = ()

    @field_validator("allowed_request_hostnames")
    @classmethod
    def validate_allowed_request_hostnames(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(normalize_h5_auth_hostname(hostname) for hostname in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_request_hostnames 不得包含重复 hostname")
        return normalized

    @field_validator("phone", "password")
    @classmethod
    def validate_non_empty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("手机号和密码不能为空")
        return value

    @model_validator(mode="after")
    def validate_phone_source(self) -> _CredentialEntry:
        if (self.phone is None) == (self.phone_strategy is None):
            raise ValueError("phone 与 phone_strategy 必须且只能配置一个")
        return self


class _H5AuthFileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["h5-auth-config.v1"]
    global_profile: _CredentialEntry | None = Field(default=None, alias="global")
    domains: Mapping[str, _CredentialEntry] = Field(default_factory=dict)

    @field_validator("domains", mode="before")
    @classmethod
    def validate_domain_keys(cls, value: object) -> dict[str, object]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("domains 必须为 hostname 到账号配置的映射")
        normalized: dict[str, object] = {}
        for raw_hostname, entry in value.items():
            if not isinstance(raw_hostname, str):
                raise ValueError("domains 的 hostname 必须为字符串")
            hostname = normalize_h5_auth_hostname(raw_hostname)
            if hostname in normalized:
                raise ValueError("domains 包含规范化后重复的 hostname")
            normalized[hostname] = entry
        return normalized

    @model_validator(mode="after")
    def validate_has_profile(self) -> _H5AuthFileConfig:
        if self.global_profile is None and not self.domains:
            raise ValueError("H5 主动登录配置至少需要 global 或一个 domains 账号")
        return self


class FileH5CredentialConfigResolver:
    """从受控文件按精确域名优先、global 兜底解析账号引用。"""

    def __init__(
        self,
        *,
        config: _H5AuthFileConfig,
        authorized_hostnames: Iterable[str],
    ) -> None:
        self._config = config
        self._authorized_hostnames = frozenset(
            normalize_h5_auth_hostname(hostname) for hostname in authorized_hostnames
        )

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        authorized_hostnames: Iterable[str],
    ) -> FileH5CredentialConfigResolver:
        config_path = Path(path)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise H5AuthConfigError("H5 主动登录配置文件无法读取或不是合法 JSON") from exc
        if isinstance(payload, Mapping):
            raw_domains = payload.get("domains")
            if isinstance(raw_domains, Mapping):
                for raw_hostname in raw_domains:
                    if not isinstance(raw_hostname, str):
                        raise H5AuthConfigError("domains 的 hostname 必须为字符串")
                    normalize_h5_auth_hostname(raw_hostname)
        try:
            config = _H5AuthFileConfig.model_validate(payload)
        except ValidationError:
            # Pydantic 的 ValidationError 可能携带原始输入；禁止把异常链带出秘密边界。
            raise H5AuthConfigError("H5 主动登录配置合同无效") from None
        return cls(config=config, authorized_hostnames=authorized_hostnames)

    def resolve_ref(self, hostname: str) -> H5CredentialProfileRef | None:
        normalized = normalize_h5_auth_hostname(hostname)
        if normalized not in self._authorized_hostnames:
            return None
        if normalized in self._config.domains:
            entry = self._config.domains[normalized]
            return H5CredentialProfileRef(
                scope="domain",
                hostname=normalized,
                submission_mode=entry.submission_mode,
                allowed_request_hostnames=entry.allowed_request_hostnames,
                uses_synthetic_phone=entry.phone_strategy == "synthetic_cn_mobile",
            )
        if self._config.global_profile is not None:
            entry = self._config.global_profile
            return H5CredentialProfileRef(
                scope="global",
                hostname=normalized,
                submission_mode=entry.submission_mode,
                allowed_request_hostnames=entry.allowed_request_hostnames,
                uses_synthetic_phone=entry.phone_strategy == "synthetic_cn_mobile",
            )
        return None

    def read_secret(self, ref: H5CredentialProfileRef) -> H5CredentialSecret:
        if not isinstance(ref, H5CredentialProfileRef):
            raise TypeError("ref 必须为 H5CredentialProfileRef")
        if ref.hostname not in self._authorized_hostnames:
            raise H5AuthConfigError("账号引用不属于本任务授权 hostname")
        if ref.scope == "domain":
            entry = self._config.domains.get(ref.hostname)
        else:
            entry = self._config.global_profile
        if entry is None:
            raise H5AuthConfigError("账号引用与当前配置不一致")
        phone = entry.phone
        if phone is None and entry.phone_strategy == "synthetic_cn_mobile":
            prefix = secrets.choice(_SYNTHETIC_CN_MOBILE_PREFIXES)
            phone = SecretStr(f"{prefix}{secrets.randbelow(100_000_000):08d}")
        if phone is None:
            raise H5AuthConfigError("账号配置未提供可用手机号")
        return H5CredentialSecret(phone=phone, password=entry.password)


def normalize_h5_auth_hostname(value: str) -> str:
    """规范化单个 hostname，并拒绝 URL、端口、路径和通配符。"""
    if not isinstance(value, str) or not value or value != value.strip():
        raise H5AuthConfigError("hostname 必须为无首尾空白的非空字符串")
    candidate = value[:-1] if value.endswith(".") else value
    if (
        not candidate
        or "*" in candidate
        or any(token in candidate for token in ("://", "/", "\\", ":", "@"))
        or any(character.isspace() for character in candidate)
    ):
        raise H5AuthConfigError("hostname 必须是精确主机名，不能包含通配符、URL、端口或路径")
    try:
        normalized = candidate.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise H5AuthConfigError("hostname 不是有效 IDNA 主机名") from exc
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise H5AuthConfigError("hostname 包含无效标签")
    if len(normalized) > 253:
        raise H5AuthConfigError("hostname 超过长度限制")
    return normalized


__all__ = [
    "FileH5CredentialConfigResolver",
    "H5AuthConfigError",
    "H5AuthSettings",
    "H5CredentialProfileRef",
    "H5CredentialSecret",
    "normalize_h5_auth_hostname",
]
