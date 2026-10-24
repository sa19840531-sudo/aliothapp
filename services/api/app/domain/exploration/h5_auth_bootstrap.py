"""H5 主动登录状态机使用的无秘密领域合同。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from services.api.app.domain.interaction import CaptureRef


class H5AuthStatus(StrEnum):
    """H5 认证 Bootstrap 的过程与终态。"""

    DISABLED = "disabled"
    CHECKING = "checking"
    ALREADY_AUTHENTICATED = "already_authenticated"
    AUTHENTICATED = "authenticated"
    GUEST_ONLY = "guest_only"
    FAILED = "failed"


class H5AuthPageKind(StrEnum):
    """当前页面可确定的认证语义。"""

    UNKNOWN = "unknown"
    TRANSIENT_OBSTRUCTION = "transient_obstruction"
    PROFILE_AUTHENTICATED = "profile_authenticated"
    PROFILE_GUEST = "profile_guest"
    LOGIN_PASSWORD = "login_password"
    LOGIN_SWITCHABLE = "login_switchable"
    LOGIN_CHALLENGE = "login_challenge"
    SUCCESS_NOTICE = "success_notice"


@dataclass(frozen=True, slots=True, kw_only=True)
class H5AuthPageObservation:
    """页面分析输出；只携带语义目标，不携带 DOM selector 或凭证。"""

    page_kind: H5AuthPageKind
    reason_code: str
    profile_target: str | None = None
    login_target: str | None = None
    registration_entry_target: str | None = None
    password_mode_target: str | None = None
    phone_target: str | None = None
    password_target: str | None = None
    submit_target: str | None = None
    registration_submit_target: str | None = None
    success_dismiss_target: str | None = None
    transient_target: str | None = None
    transition_parameters: tuple[tuple[str, str | int], ...] = ()

    def __post_init__(self) -> None:
        if not self.reason_code or self.reason_code != self.reason_code.strip():
            raise ValueError("reason_code 必须为无首尾空白的非空字符串")
        for field_name in (
            "profile_target",
            "login_target",
            "registration_entry_target",
            "password_mode_target",
            "phone_target",
            "password_target",
            "submit_target",
            "registration_submit_target",
            "success_dismiss_target",
            "transient_target",
        ):
            value = getattr(self, field_name)
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"{field_name} 必须为空或无首尾空白的非空字符串")
        if not isinstance(self.transition_parameters, tuple):
            raise TypeError("transition_parameters 必须为元组")
        parameter_names: set[str] = set()
        for item in self.transition_parameters:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("transition_parameters 只能包含键值对")
            key, value = item
            if not isinstance(key, str) or not key or key != key.strip():
                raise ValueError("transition_parameters 参数名必须为非空字符串")
            if isinstance(value, str):
                if not value or value != value.strip():
                    raise ValueError("transition_parameters 字符串值不得为空或带首尾空白")
            elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000:
                raise ValueError("transition_parameters 数值只能为 0..1000 的整数")
            if item[0] in parameter_names:
                raise ValueError("transition_parameters 参数名不得重复")
            parameter_names.add(item[0])

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "page_kind": self.page_kind.value,
            "reason_code": self.reason_code,
            "profile_target": self.profile_target,
            "login_target": self.login_target,
            "registration_entry_target": self.registration_entry_target,
            "password_mode_target": self.password_mode_target,
            "phone_target": self.phone_target,
            "password_target": self.password_target,
            "submit_target": self.submit_target,
            "registration_submit_target": self.registration_submit_target,
            "success_dismiss_target": self.success_dismiss_target,
            "transient_target": self.transient_target,
            "transition_parameters": dict(self.transition_parameters),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class H5AuthBootstrapResult:
    """认证阶段对正式根探索公开的安全摘要。"""

    status: H5AuthStatus
    final_capture: CaptureRef
    credential_scope: Literal["domain", "global"] | None
    reason_code: str
    attempted: bool
    needs_review: bool

    def __post_init__(self) -> None:
        if not isinstance(self.final_capture, CaptureRef):
            raise TypeError("final_capture 必须为 CaptureRef")
        if not self.reason_code or self.reason_code != self.reason_code.strip():
            raise ValueError("reason_code 必须为无首尾空白的非空字符串")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "credential_scope": self.credential_scope,
            "reason_code": self.reason_code,
            "attempted": self.attempted,
            "needs_review": self.needs_review,
        }


__all__ = [
    "H5AuthBootstrapResult",
    "H5AuthPageKind",
    "H5AuthPageObservation",
    "H5AuthStatus",
]
