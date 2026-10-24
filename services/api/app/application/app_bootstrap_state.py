"""App Workflow Bootstrap 状态仓储端口与乐观锁命令。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.interaction import (
    AppBootstrapSnapshot,
    AppBootstrapStage,
)
from services.api.app.domain.interaction.validation import optional_text, require_text

APP_READY_MESSAGE_TYPE = "alioth.v2.app-workflow.ready"

_ALLOWED_STAGE_TRANSITIONS = {
    AppBootstrapStage.VALIDATING: frozenset({AppBootstrapStage.ACQUIRING_SESSION}),
    AppBootstrapStage.ACQUIRING_SESSION: frozenset({AppBootstrapStage.RESOLVING_BUILD}),
    AppBootstrapStage.RESOLVING_BUILD: frozenset({AppBootstrapStage.INSTALLING}),
    AppBootstrapStage.INSTALLING: frozenset(
        {AppBootstrapStage.INSTALLING, AppBootstrapStage.INSTALLED}
    ),
    AppBootstrapStage.INSTALLED: frozenset({AppBootstrapStage.OPENING}),
    AppBootstrapStage.OPENING: frozenset(
        {AppBootstrapStage.OPENING, AppBootstrapStage.OPENED}
    ),
    AppBootstrapStage.OPENED: frozenset({AppBootstrapStage.BOOTSTRAP_CAPTURING}),
}


@dataclass(frozen=True, kw_only=True)
class TransitionAppBootstrapCommand:
    """使用阶段、版本和租约三重门禁推进一个 Bootstrap 检查点。"""

    task_id: str
    session_id: str
    platform_build_ref: str
    expected_stage: AppBootstrapStage
    target_stage: AppBootstrapStage
    expected_row_version: int
    lease_owner: str
    lease_token: str
    install_request_id: str | None = None
    install_attempt: int | None = None
    installed_package: str | None = None
    open_request_id: str | None = None
    open_attempt: int | None = None

    def __post_init__(self) -> None:
        _validate_identity(self)
        if not isinstance(self.expected_stage, AppBootstrapStage) or not isinstance(
            self.target_stage,
            AppBootstrapStage,
        ):
            raise ValueError("App Bootstrap stage 不受支持")
        if self.target_stage not in _ALLOWED_STAGE_TRANSITIONS.get(
            self.expected_stage,
            frozenset(),
        ):
            raise ValueError("非法 App Bootstrap 阶段跃迁")
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        for field_name in (
            "install_request_id",
            "installed_package",
            "open_request_id",
        ):
            optional_text(getattr(self, field_name), field_name)
        for field_name in ("install_attempt", "open_attempt"):
            value = getattr(self, field_name)
            if value is not None:
                _require_nonnegative(value, field_name)
        if self.target_stage is AppBootstrapStage.INSTALLING:
            if self.install_request_id is None:
                raise ValueError("进入 INSTALLING 必须提供 install_request_id")
            _require_positive(self.install_attempt, "install_attempt")
        if self.target_stage is AppBootstrapStage.INSTALLED:
            if self.install_request_id is None or self.installed_package is None:
                raise ValueError(
                    "进入 INSTALLED 必须提供 install_request_id 和 installed_package"
                )
        if self.target_stage is AppBootstrapStage.OPENING:
            if self.open_request_id is None:
                raise ValueError("进入 OPENING 必须提供 open_request_id")
            _require_positive(self.open_attempt, "open_attempt")
        if self.target_stage is AppBootstrapStage.OPENED and self.open_request_id is None:
            raise ValueError("进入 OPENED 必须提供 open_request_id")


@dataclass(frozen=True, kw_only=True)
class FailAppBootstrapCommand:
    """将当前租约持有者观察到的不可恢复失败写入权威检查点。"""

    task_id: str
    session_id: str
    platform_build_ref: str
    expected_stage: AppBootstrapStage
    expected_row_version: int
    lease_owner: str
    lease_token: str
    error_code: str
    error_message: str | None = None

    def __post_init__(self) -> None:
        _validate_identity(self)
        if not isinstance(self.expected_stage, AppBootstrapStage):
            raise ValueError("expected_stage 不受支持")
        if self.expected_stage.is_terminal:
            raise ValueError("终态 Bootstrap 不能再次标记失败")
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        require_text(self.error_code, "error_code")
        optional_text(self.error_message, "error_message")


@dataclass(frozen=True, kw_only=True)
class CancelAppBootstrapCommand:
    """由当前 Workflow 租约持有者封闭未完成的 App Bootstrap。"""

    task_id: str
    session_id: str
    platform_build_ref: str
    expected_stage: AppBootstrapStage
    expected_row_version: int
    lease_owner: str
    lease_token: str

    def __post_init__(self) -> None:
        _validate_identity(self)
        if not isinstance(self.expected_stage, AppBootstrapStage):
            raise ValueError("expected_stage 不受支持")
        if self.expected_stage.is_terminal:
            raise ValueError("终态 Bootstrap 不能再次取消")
        _require_nonnegative(self.expected_row_version, "expected_row_version")


@dataclass(frozen=True, kw_only=True)
class RenewAppBootstrapLeaseCommand:
    """由当前租约持有者续期 App Bootstrap 的处理租约。"""

    task_id: str
    session_id: str
    platform_build_ref: str
    lease_owner: str
    lease_token: str
    lease_seconds: int

    def __post_init__(self) -> None:
        _validate_identity(self)
        _require_positive(self.lease_seconds, "lease_seconds")


class AppBootstrapRepository(Protocol):
    """App Workflow 对 PostgreSQL Bootstrap 事实源的最小依赖。"""

    async def get_or_create(
        self,
        *,
        task_id: str,
        session_id: str,
        platform_build_ref: str,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int,
    ) -> AppBootstrapSnapshot: ...

    async def get(self, *, session_id: str) -> AppBootstrapSnapshot | None: ...

    async def renew(
        self,
        command: RenewAppBootstrapLeaseCommand,
    ) -> AppBootstrapSnapshot: ...

    async def transition(
        self,
        command: TransitionAppBootstrapCommand,
    ) -> AppBootstrapSnapshot: ...

    async def fail(
        self,
        command: FailAppBootstrapCommand,
    ) -> AppBootstrapSnapshot: ...

    async def cancel(
        self,
        command: CancelAppBootstrapCommand,
    ) -> AppBootstrapSnapshot: ...


def _validate_identity(command: object) -> None:
    for field_name in (
        "task_id",
        "session_id",
        "platform_build_ref",
        "lease_owner",
        "lease_token",
    ):
        require_text(getattr(command, field_name), field_name)


def _require_nonnegative(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")
    return value


def _require_positive(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return value
