"""Evidence Request 生命周期的应用层端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from services.api.app.domain.inspection import EvidenceRequest, EvidenceSessionDependency


class EvidenceRequestStatus(StrEnum):
    """证据补采请求状态。"""

    PENDING = "pending"
    ROUTED = "routed"
    RUNNING = "running"
    SATISFIED = "satisfied"
    UNFULFILLABLE = "unfulfillable"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.SATISFIED,
            self.UNFULFILLABLE,
            self.EXPIRED,
            self.CANCELLED,
        }


@dataclass(frozen=True, kw_only=True)
class StoredEvidenceRequest:
    """请求正文与可并发更新的生命周期状态。"""

    request: EvidenceRequest
    status: EvidenceRequestStatus
    row_version: int
    resolution_manifest_id: str | None = None
    resolution_reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class TransitionEvidenceRequestCommand:
    """以乐观锁推进证据请求。"""

    request_id: str
    expected_status: EvidenceRequestStatus
    target_status: EvidenceRequestStatus
    row_version: int
    resolution_manifest_id: str | None = None
    resolution_reason: str | None = None


class EvidenceRequestClaimDisposition(StrEnum):
    """补采请求原子领取结果。"""

    CLAIMED = "claimed"
    EMPTY = "empty"


@dataclass(frozen=True, kw_only=True)
class EvidenceRequestLeaseFence:
    """跨应用层传递但不进入 ProbeWork 业务载荷的租约凭证。"""

    request_id: str
    task_id: str
    finalization_epoch: int
    lease_owner: str
    lease_token: str
    row_version: int

    def __post_init__(self) -> None:
        _validate_lease_fence(
            request_id=self.request_id,
            task_id=self.task_id,
            finalization_epoch=self.finalization_epoch,
            lease_owner=self.lease_owner,
            lease_token=self.lease_token,
            row_version=self.row_version,
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceRequestProcessingLease:
    """一个绑定 Task/Run/Epoch 的短期补采处理许可。"""

    request: EvidenceRequest
    lease_owner: str
    lease_token: str
    finalization_epoch: int
    row_version: int
    attempt_count: int
    max_attempts: int

    def __post_init__(self) -> None:
        if not isinstance(self.request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")
        _validate_lease_fence(
            request_id=self.request.request_id,
            task_id=self.request.task_id,
            finalization_epoch=self.finalization_epoch,
            lease_owner=self.lease_owner,
            lease_token=self.lease_token,
            row_version=self.row_version,
        )
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.attempt_count <= 0
            or self.max_attempts <= 0
            or self.attempt_count > self.max_attempts
        ):
            raise ValueError("Evidence Request 尝试次数无效")

    @property
    def fence(self) -> EvidenceRequestLeaseFence:
        return EvidenceRequestLeaseFence(
            request_id=self.request.request_id,
            task_id=self.request.task_id,
            finalization_epoch=self.finalization_epoch,
            lease_owner=self.lease_owner,
            lease_token=self.lease_token,
            row_version=self.row_version,
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceRequestClaimResult:
    """无任务或成功领取的确定性结果。"""

    disposition: EvidenceRequestClaimDisposition
    lease: EvidenceRequestProcessingLease | None = None

    def __post_init__(self) -> None:
        if (self.disposition is EvidenceRequestClaimDisposition.CLAIMED) != (
            self.lease is not None
        ):
            raise ValueError("Evidence Request 领取结果与 lease 不一致")


class EvidenceRequestLeaseReleaseDisposition(StrEnum):
    """租约释放后的请求去向。"""

    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_EXHAUSTED = "retry_exhausted"
    REQUEST_EXPIRED = "request_expired"


@dataclass(frozen=True, kw_only=True)
class ReleaseEvidenceRequestLeaseCommand:
    """在有效租约下安排重试；仓储负责在耗尽时转终态。"""

    request_id: str
    task_id: str
    finalization_epoch: int
    lease_owner: str
    lease_token: str
    row_version: int
    next_attempt_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        _validate_lease_fence(
            request_id=self.request_id,
            task_id=self.task_id,
            finalization_epoch=self.finalization_epoch,
            lease_owner=self.lease_owner,
            lease_token=self.lease_token,
            row_version=self.row_version,
        )
        if not isinstance(self.next_attempt_at, datetime) or (
            self.next_attempt_at.tzinfo is None
            or self.next_attempt_at.utcoffset() is None
        ):
            raise ValueError("next_attempt_at 必须包含时区")
        _require_text(self.reason_code, "reason_code")


@dataclass(frozen=True, kw_only=True)
class EvidenceRequestLeaseReleaseResult:
    """重试释放的持久化结果。"""

    disposition: EvidenceRequestLeaseReleaseDisposition
    row_version: int


@dataclass(frozen=True, kw_only=True)
class SettleEvidenceRequestLeaseCommand:
    """在有效租约下把不可继续处理的请求写入终态。"""

    request_id: str
    task_id: str
    finalization_epoch: int
    lease_owner: str
    lease_token: str
    row_version: int
    target_status: EvidenceRequestStatus
    resolution_reason: str

    def __post_init__(self) -> None:
        _validate_lease_fence(
            request_id=self.request_id,
            task_id=self.task_id,
            finalization_epoch=self.finalization_epoch,
            lease_owner=self.lease_owner,
            lease_token=self.lease_token,
            row_version=self.row_version,
        )
        if self.target_status not in {
            EvidenceRequestStatus.UNFULFILLABLE,
            EvidenceRequestStatus.EXPIRED,
            EvidenceRequestStatus.CANCELLED,
        }:
            raise ValueError("租约终态结算只支持 unfulfillable、expired 或 cancelled")
        _require_text(self.resolution_reason, "resolution_reason")


class EvidenceRequestRepository(Protocol):
    """供 Orchestrator、Probe 和完成门禁共享的请求仓储。"""

    async def get(self, *, request_id: str) -> StoredEvidenceRequest | None: ...

    async def claim_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        lease_owner: str,
        lease_seconds: int,
        session_dependencies: tuple[EvidenceSessionDependency, ...] = (
            EvidenceSessionDependency.ACTIVE_SESSION,
        ),
        deferred_h5_snapshot_request_ids: tuple[str, ...] = (),
    ) -> EvidenceRequestClaimResult: ...

    async def release_lease(
        self,
        *,
        command: ReleaseEvidenceRequestLeaseCommand,
    ) -> EvidenceRequestLeaseReleaseResult: ...

    async def settle_lease(
        self,
        *,
        command: SettleEvidenceRequestLeaseCommand,
    ) -> int: ...

    async def transition(self, *, command: TransitionEvidenceRequestCommand) -> int: ...

    async def expire_due(self, *, limit: int = 100) -> int: ...

    async def has_nonterminal_active_session_request(self, *, task_id: str) -> bool: ...


def _validate_lease_fence(
    *,
    request_id: str,
    task_id: str,
    finalization_epoch: int,
    lease_owner: str,
    lease_token: str,
    row_version: int,
) -> None:
    for value, field_name in (
        (request_id, "request_id"),
        (task_id, "task_id"),
        (lease_owner, "lease_owner"),
        (lease_token, "lease_token"),
    ):
        _require_text(value, field_name)
    for value, field_name in (
        (finalization_epoch, "finalization_epoch"),
        (row_version, "row_version"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} 必须为非负整数")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
