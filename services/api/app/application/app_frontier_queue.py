"""App 任务级 Frontier 的应用层命令与持久化端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.exploration import (
    AppFrontierCounts,
    AppFrontierItem,
    AppFrontierLease,
    AppFrontierStatus,
)


@dataclass(frozen=True, kw_only=True)
class PublishAppFrontierCandidatesCommand:
    """幂等登记某张截图发现的全部安全候选。"""

    task_id: str
    agent_run_id: str
    session_id: str
    source_work_id: str
    candidates: tuple[AppFrontierItem, ...]

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "session_id", "source_work_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.candidates, tuple):
            raise TypeError("candidates 必须为 tuple")
        if any(not isinstance(item, AppFrontierItem) for item in self.candidates):
            raise TypeError("candidates 只能包含 AppFrontierItem")


@dataclass(frozen=True, kw_only=True)
class ClaimAppFrontierCommand:
    """串行领取当前 Run 下一条可执行 Frontier Item。"""

    task_id: str
    agent_run_id: str
    session_id: str
    lease_owner: str
    lease_seconds: int = 180
    frontier_item_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "session_id", "lease_owner"):
            _require_text(getattr(self, field_name), field_name)
        if self.frontier_item_id is not None:
            _require_text(self.frontier_item_id, "frontier_item_id")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须为正整数")


@dataclass(frozen=True, kw_only=True)
class TransitionAppFrontierCommand:
    """按租约和行版本写回 Frontier Item。"""

    frontier_item_id: str
    expected_status: AppFrontierStatus
    target_status: AppFrontierStatus
    lease_token: str
    row_version: int
    result_work_id: str | None = None
    result_capture_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_delay_seconds: int = 0

    def __post_init__(self) -> None:
        for field_name in ("frontier_item_id", "lease_token"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.expected_status, AppFrontierStatus):
            raise TypeError("expected_status 必须为 AppFrontierStatus")
        if not isinstance(self.target_status, AppFrontierStatus):
            raise TypeError("target_status 必须为 AppFrontierStatus")
        if (
            isinstance(self.row_version, bool)
            or not isinstance(self.row_version, int)
            or self.row_version < 0
        ):
            raise ValueError("row_version 必须为非负整数")
        for field_name in (
            "result_work_id",
            "result_capture_id",
            "error_code",
            "error_message",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if (
            isinstance(self.retry_delay_seconds, bool)
            or not isinstance(self.retry_delay_seconds, int)
            or self.retry_delay_seconds < 0
        ):
            raise ValueError("retry_delay_seconds 必须为非负整数")


@dataclass(frozen=True, kw_only=True)
class RenewAppFrontierLeaseCommand:
    frontier_item_id: str
    lease_owner: str
    lease_token: str
    lease_seconds: int

    def __post_init__(self) -> None:
        for field_name in ("frontier_item_id", "lease_owner", "lease_token"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须为正整数")


class AppFrontierQueue(Protocol):
    """PostgreSQL 为事实源的 App 任务级 Frontier 端口。"""

    async def publish_candidates(
        self,
        *,
        command: PublishAppFrontierCandidatesCommand,
    ) -> tuple[AppFrontierItem, ...]: ...

    async def claim(
        self,
        *,
        command: ClaimAppFrontierCommand,
        lease_token: str,
    ) -> AppFrontierLease | None: ...

    async def transition(self, *, command: TransitionAppFrontierCommand) -> int: ...

    async def renew(self, *, command: RenewAppFrontierLeaseCommand) -> None: ...

    async def counts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
    ) -> AppFrontierCounts: ...

    async def next_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
    ) -> AppFrontierItem | None: ...

    async def list_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        limit: int = 256,
    ) -> tuple[AppFrontierItem, ...]: ...

    async def get_item(self, *, frontier_item_id: str) -> AppFrontierItem | None: ...

    async def cancel_nonterminal(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        reason: str,
    ) -> int: ...

    async def expire_unclaimable(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
    ) -> int: ...


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
