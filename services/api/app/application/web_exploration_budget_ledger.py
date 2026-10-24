"""Web V2 菜单动作与单详情预算的应用层合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.page import WebSiteMenuCatalog, WebSiteMenuContext, WebSiteMenuEntry

if TYPE_CHECKING:
    from services.api.app.application.web_menu_exploration import WebMenuExplorationSummary
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry


class WebExplorationBudgetLedgerConflictError(RuntimeError):
    """Web 预算身份、Session 归属或幂等事实发生漂移。"""


@dataclass(frozen=True, kw_only=True)
class WebMenuActionReservation:
    site_id: str
    menu_entry_id: str
    execution_slot: int
    intent_id: str
    already_reserved: bool


@dataclass(frozen=True, kw_only=True)
class WebMenuClaimRejection:
    """已认证并持久结算的预算拒绝；不代表领取成功或所有权冲突。"""

    error_code: str = "WEB_MENU_CLOSEOUT_RESERVED"


@dataclass(frozen=True, kw_only=True)
class WebDetailActionReservation:
    site_id: str
    menu_entry_id: str
    intent_id: str
    already_reserved: bool


@dataclass(frozen=True, kw_only=True)
class WebMenuLedgerTerminalRecord:
    """只携带现有 Capture/Action 身份与结构化终态，不复制证据正文。"""

    task_id: str
    agent_run_id: str
    session_id: str
    site_id: str
    menu_entry_id: str
    status: WebMenuTerminalStatus
    menu_action_execution_id: str | None = None
    menu_probe_capture_id: str | None = None
    menu_ready_capture_id: str | None = None
    menu_page_type: str | None = None
    detail_action_execution_id: str | None = None
    detail_probe_capture_id: str | None = None
    detail_ready_capture_id: str | None = None
    detail_page_type: str | None = None
    detail_selection: Mapping[str, object] = field(default_factory=dict)
    diagnostic: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "site_id",
            "menu_entry_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.status, WebMenuTerminalStatus):
            raise TypeError("status 必须为 WebMenuTerminalStatus")


class WebExplorationBudgetLedgerPort(Protocol):
    async def bootstrap(
        self,
        command: RunWebWorkflowCommand,
        site: WebSiteMenuCatalog | WebSiteMenuContext,
        *,
        catalog_version: str,
        catalog_sha256: str,
    ) -> None: ...

    async def claim_menu(
        self,
        *,
        command: RunWebWorkflowCommand,
        entry: WebSiteMenuEntry | WebRuntimeMenuEntry,
        source_capture_id: str,
    ) -> WebMenuActionReservation | WebMenuClaimRejection | None: ...

    async def reserve_detail(
        self,
        *,
        command: RunWebWorkflowCommand,
        entry: WebSiteMenuEntry | WebRuntimeMenuEntry,
        source_capture_id: str,
        candidate_id: str,
        destination_url: str,
    ) -> WebDetailActionReservation: ...

    async def finalize(self, record: WebMenuLedgerTerminalRecord) -> None: ...

    async def seal(
        self,
        *,
        command: RunWebWorkflowCommand,
        site_id: str,
    ) -> None: ...

    async def read_summary(
        self,
        *,
        agent_run_id: str,
        site_id: str,
    ) -> WebMenuExplorationSummary: ...

    async def closeout_unexecuted(
        self, *, command: RunWebWorkflowCommand, site_id: str
    ) -> None: ...


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


__all__ = [
    "WebDetailActionReservation",
    "WebExplorationBudgetLedgerConflictError",
    "WebExplorationBudgetLedgerPort",
    "WebMenuActionReservation",
    "WebMenuClaimRejection",
    "WebMenuLedgerTerminalRecord",
]
