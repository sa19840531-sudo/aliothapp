"""App 账号环境注入：只接受 account_environment_ref，不携带明文凭证。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    optional_text,
    require_text,
)

APP_ACCOUNT_PROTOCOL_VERSION = "app-account.v1"


@dataclass(frozen=True, kw_only=True)
class AppAccountInjectRequest:
    """按引用绑定账号环境到业务会话。"""

    task_id: str
    session_id: str
    account_environment_ref: str
    package_name: str | None = None
    protocol_version: str = APP_ACCOUNT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "account_environment_ref"):
            require_text(getattr(self, field_name), field_name)
        optional_text(self.package_name, "package_name")
        if self.protocol_version != APP_ACCOUNT_PROTOCOL_VERSION:
            raise InteractionValidationError("App Account protocol_version 不受支持")

    @property
    def idempotency_key(self) -> str:
        return f"{self.session_id}:{self.account_environment_ref}"


@dataclass(frozen=True, kw_only=True)
class AppAccountInjectResult:
    """账号环境绑定结果（可审计，不含凭证）。"""

    request_id: str
    session_id: str
    succeeded: bool
    retryable: bool
    account_environment_ref: str
    bound: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        require_text(self.request_id, "request_id")
        require_text(self.session_id, "session_id")
        require_text(self.account_environment_ref, "account_environment_ref")
        if not isinstance(self.succeeded, bool) or not isinstance(self.retryable, bool):
            raise InteractionValidationError("succeeded/retryable 必须为布尔值")
        if not isinstance(self.bound, bool):
            raise InteractionValidationError("bound 必须为布尔值")
        optional_text(self.error_code, "error_code")
        if self.succeeded == (self.error_code is not None):
            raise InteractionValidationError("成功状态与 error_code 不一致")
        if self.succeeded and not self.bound:
            raise InteractionValidationError("注入成功必须 bound=True")


class AppAccountPort(Protocol):
    """账号环境注入端口。"""

    async def inject_account(
        self, request: AppAccountInjectRequest
    ) -> AppAccountInjectResult: ...


def bind_account_ref_on_options(
    options: dict[str, Any],
    *,
    account_environment_ref: str,
) -> dict[str, Any]:
    """把账号引用写入 Session options（供等价格局使用）。"""
    updated = dict(options)
    updated["account_environment_ref"] = require_text(
        account_environment_ref, "account_environment_ref"
    )
    updated["account_bound"] = True
    return updated
