"""Explorer 工作队列的应用层状态与持久化端口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from services.api.app.domain.exploration import (
    AppFrontierStatus,
    ExplorationTerminal,
    ExplorationWork,
    ExplorerWork,
)
from services.api.app.domain.interaction import SurfaceKind


class ExplorerWorkPersistenceError(RuntimeError):
    """持久化身份漂移、归属不一致或状态竞争导致写回失败。

    ``reason_code`` 是稳定原因码：发布被拒时由仓储诊断出到底是任务、Capture、
    Evidence Request、恢复尝试还是等价证明不满足；没有诊断结果时为 ``None``。
    """

    def __init__(self, message: str = "", *, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ExplorerWorkDeadlineExceededError(ExplorerWorkPersistenceError):
    """后继已到截止时间，只允许在有效租约内结算当前 Work。"""


class ExplorerWorkType(StrEnum):
    """持久化 Explorer 工作的两种业务来源。"""

    EXPLORATION = "exploration"
    PROBE = "probe"


class ExplorerWorkStatus(StrEnum):
    """Explorer 工作项状态机。"""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    BOUNDED = "bounded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"
    NO_PROGRESS = "no_progress"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.BOUNDED,
            self.BUDGET_EXHAUSTED,
            self.BLOCKED,
            self.NO_PROGRESS,
            self.FAILED,
            self.CANCELLED,
            self.SUPERSEDED,
        }


@dataclass(frozen=True, kw_only=True)
class PublishedExplorerWork:
    """幂等发布结果。"""

    work_id: str
    status: ExplorerWorkStatus
    created: bool


@dataclass(frozen=True, kw_only=True)
class ExplorerWorkLease:
    """Explorer Worker 可消费的工作快照与并发写回凭证。"""

    work: ExplorerWork
    status: ExplorerWorkStatus
    lease_token: str
    attempt_count: int
    row_version: int


@dataclass(frozen=True, kw_only=True)
class ClaimExplorerWorkCommand:
    """领取指定类型 Explorer 工作的租约条件。

    ``surface_kinds`` 声明本执行器能跑哪些界面类型，队列只把匹配的工作交给它。
    补采 Probe 由 ``evidence_request_routing`` 按目标 Surface 路由产出，各平台的
    执行器共用同一张工作表，不声明就会互相领到跑不了的活（Web 执行器领到 android
    的恢复路线）。留空表示不按界面类型过滤，保持既有单端行为不变。
    """

    work_type: ExplorerWorkType
    lease_owner: str
    lease_seconds: int = 180
    task_id: str | None = None
    surface_kinds: tuple[SurfaceKind, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.surface_kinds, tuple) or any(
            not isinstance(item, SurfaceKind) for item in self.surface_kinds
        ):
            raise TypeError("surface_kinds 必须是 SurfaceKind 元组")
        if len(set(self.surface_kinds)) != len(self.surface_kinds):
            raise ValueError("surface_kinds 不允许重复")


@dataclass(frozen=True, kw_only=True)
class TransitionExplorerWorkCommand:
    """按状态、版本和租约令牌推进 Explorer 工作。"""

    work_id: str
    expected_status: ExplorerWorkStatus
    target_status: ExplorerWorkStatus
    lease_token: str
    row_version: int
    terminal: ExplorationTerminal | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_delay_seconds: int = 0
    app_frontier_settlement: AppFrontierSettlement | None = None
    # 投递预算是给「同一份工作」的毒丸护栏；当载荷已改选到另一条 Frontier
    # sibling 时，重投的是实质不同的工作，必须给新 sibling 独立预算。
    # 终止性由 Frontier 条目有限保证：每次重置都以一条条目被判死为代价。
    reset_attempt_count: bool = False
    # 只用于终态结算的已观察结果，不发布为可领取的后继 Work。
    settlement_work: ExplorationWork | None = None


@dataclass(frozen=True, kw_only=True)
class RenewExplorerWorkLeaseCommand:
    """续期当前 RUNNING Explorer Work，续期不得改变行版本。"""

    work_id: str
    lease_owner: str
    lease_token: str
    lease_seconds: int

    def __post_init__(self) -> None:
        for field_name in ("work_id", "lease_owner", "lease_token"):
            _require_text(getattr(self, field_name), field_name)
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须为正整数")


@dataclass(frozen=True, kw_only=True)
class UpdateExplorerWorkPayloadCommand:
    """在活动租约内持久化同一 Work 的不可变 Frontier 快照演进。"""

    work: ExplorationWork
    lease_token: str
    row_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.work, ExplorationWork):
            raise TypeError("work 必须为 ExplorationWork")
        _require_text(self.lease_token, "lease_token")
        if isinstance(self.row_version, bool) or not isinstance(self.row_version, int):
            raise ValueError("row_version 必须为整数")


@dataclass(frozen=True, kw_only=True)
class AppFrontierSettlement:
    """随父 Work/后继 Work 在同一事务写回的 App Frontier 事实。"""

    task_id: str
    agent_run_id: str
    session_id: str
    candidate_fingerprint: str
    target_status: AppFrontierStatus
    frontier_item_id: str | None = None
    lease_token: str | None = None
    row_version: int | None = None
    result_capture_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_delay_seconds: int = 0

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "session_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_text(self.candidate_fingerprint, "candidate_fingerprint")
        if self.target_status not in {
            AppFrontierStatus.VISITED,
            AppFrontierStatus.BLOCKED,
            AppFrontierStatus.FAILED,
            AppFrontierStatus.RETRY_WAIT,
            AppFrontierStatus.CANCELLED,
            AppFrontierStatus.SUPERSEDED,
        }:
            raise ValueError("App Frontier 事务写回状态不受支持")
        leased = self.frontier_item_id is not None
        if leased != (self.lease_token is not None) or leased != (self.row_version is not None):
            raise ValueError("指派 Frontier 必须同时携带 item、lease token 与 row version")
        if self.frontier_item_id is not None:
            _require_text(self.frontier_item_id, "frontier_item_id")
            _require_text(self.lease_token, "lease_token")
            if (
                isinstance(self.row_version, bool)
                or not isinstance(self.row_version, int)
                or self.row_version < 0
            ):
                raise ValueError("row_version 必须为非负整数")
        elif self.target_status not in {
            AppFrontierStatus.VISITED,
            AppFrontierStatus.BLOCKED,
            AppFrontierStatus.FAILED,
            AppFrontierStatus.SUPERSEDED,
        }:
            raise ValueError("未领取的发现态 Frontier 只能直接收口为终态")
        if (
            isinstance(self.retry_delay_seconds, bool)
            or not isinstance(self.retry_delay_seconds, int)
            or self.retry_delay_seconds < 0
        ):
            raise ValueError("retry_delay_seconds 必须为非负整数")
        if self.target_status is AppFrontierStatus.RETRY_WAIT and self.retry_delay_seconds <= 0:
            raise ValueError("App Frontier 重试必须设置正数延迟")


@dataclass(frozen=True, kw_only=True)
class CommitExplorerWorkSuccessorCommand:
    """以父 Work 活租约为门禁，原子完成父 Work 并发布唯一后继。"""

    parent_work_id: str
    task_id: str
    session_id: str
    lease_owner: str
    lease_token: str
    row_version: int
    successor: ExplorationWork
    parent_target_status: ExplorerWorkStatus = ExplorerWorkStatus.SUCCEEDED
    app_frontier_settlement: AppFrontierSettlement | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "parent_work_id",
            "task_id",
            "session_id",
            "lease_owner",
            "lease_token",
        ):
            _require_text(getattr(self, field_name), field_name)
        if (
            isinstance(self.row_version, bool)
            or not isinstance(self.row_version, int)
            or self.row_version < 0
        ):
            raise ValueError("row_version 必须为非负整数")
        if not isinstance(self.successor, ExplorationWork):
            raise TypeError("successor 必须为 ExplorationWork")
        if self.parent_target_status not in {
            ExplorerWorkStatus.SUCCEEDED,
            ExplorerWorkStatus.SUPERSEDED,
        }:
            raise ValueError("parent_target_status 只允许 succeeded 或 superseded")
        if self.app_frontier_settlement is not None and not isinstance(
            self.app_frontier_settlement,
            AppFrontierSettlement,
        ):
            raise TypeError("app_frontier_settlement 必须为 AppFrontierSettlement")
        if self.app_frontier_settlement is not None and (
            self.app_frontier_settlement.task_id != self.task_id
            or self.app_frontier_settlement.session_id != self.session_id
        ):
            raise ValueError("App Frontier 写回与父 Work 归属不一致")
        metadata = self.successor.frontier.observation.metadata
        if (
            self.successor.work_id == self.parent_work_id
            or self.successor.task_id != self.task_id
            or metadata.get("parent_work_id") != self.parent_work_id
        ):
            raise ValueError("后继 Work 与父 Work 身份或谱系不一致")
        if (
            self.parent_target_status is ExplorerWorkStatus.SUCCEEDED
            and self.successor.session_id != self.session_id
        ):
            raise ValueError("普通后继 Work 必须沿用父 Session")
        if self.parent_target_status is ExplorerWorkStatus.SUPERSEDED:
            if self.successor.session_id == self.session_id:
                reobservation = metadata.get("h5_action_reobservation")
                if not isinstance(reobservation, Mapping):
                    raise ValueError("同 Session supersede 缺少有效 H5 Action 重观察事实")
                reobservation_values = dict(reobservation)
                # v1 历史事实没有 retryable，按原有可重试语义兼容；新增字段只接受布尔值。
                retryable = reobservation_values.pop("retryable", True)
                if not isinstance(retryable, bool) or reobservation_values != {
                    "schema_version": "h5-action-reobservation.v1",
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "failed_work_id": self.parent_work_id,
                    "before_capture_id": self.successor.source_capture_id,
                    "error_code": reobservation_values.get("error_code"),
                }:
                    raise ValueError("同 Session supersede 缺少有效 H5 Action 重观察事实")
                error_code = reobservation_values.get("error_code")
                if (
                    not isinstance(error_code, str)
                    or not error_code.strip()
                    or error_code != error_code.strip()
                ):
                    raise ValueError("H5 Action 重观察 error_code 无效")
                return
            from services.api.app.application.h5_exploration_recovery import (
                H5BrowserRecoveryFacts,
            )

            if self.successor.surface_kind.value != "web":
                raise ValueError("跨 Session supersede 只允许 H5 浏览器恢复 Work")
            try:
                facts = H5BrowserRecoveryFacts.from_dict(metadata.get("h5_browser_recovery"))
            except (TypeError, ValueError) as exc:
                raise ValueError("跨 Session supersede 缺少有效 H5 恢复事实") from exc
            if (
                not facts.equivalent
                or facts.reason_code is not None
                or facts.failed_work_id != self.parent_work_id
                or facts.source_session_id != self.session_id
                or facts.target_session_id != self.successor.session_id
                or facts.task_id != self.task_id
                or facts.rebaseline_after_capture_id != self.successor.source_capture_id
            ):
                raise ValueError("跨 Session supersede 的 H5 恢复事实身份不一致")


@dataclass(frozen=True, kw_only=True)
class CommittedExplorerWorkSuccessor:
    """父 Work 与后继 Work 已在同一数据库事务提交。"""

    parent_row_version: int
    successor_work_id: str
    successor_created: bool


class ExplorerWorkQueue(Protocol):
    """Explorer Worker 与完成门禁共享的持久化工作端口。"""

    async def claim(
        self,
        *,
        command: ClaimExplorerWorkCommand,
        lease_token: str,
    ) -> ExplorerWorkLease | None: ...

    async def transition(self, *, command: TransitionExplorerWorkCommand) -> int: ...

    async def renew(self, *, command: RenewExplorerWorkLeaseCommand) -> None: ...

    async def update_payload(self, *, command: UpdateExplorerWorkPayloadCommand) -> int: ...

    async def commit_successor(
        self,
        *,
        command: CommitExplorerWorkSuccessorCommand,
        emit_wakeup: bool = True,
    ) -> CommittedExplorerWorkSuccessor: ...

    async def expire_unclaimable(
        self,
        *,
        limit: int = 100,
        task_id: str | None = None,
    ) -> int: ...

    async def next_claim_at(
        self,
        *,
        task_id: str,
        work_type: ExplorerWorkType,
    ) -> datetime | None: ...

    async def cancel_nonterminal(
        self,
        *,
        task_id: str,
        reason: str,
        work_types: tuple[ExplorerWorkType, ...] = (ExplorerWorkType.EXPLORATION,),
        h5_only: bool = False,
    ) -> int: ...

    async def reconcile_h5_terminal_frontier(self, *, task_id: str) -> bool: ...

    async def has_nonterminal_work(self, *, task_id: str) -> bool: ...


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
