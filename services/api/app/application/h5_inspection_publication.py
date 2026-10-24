"""H5 Publication 消费协议；Redis 仅唤醒，PostgreSQL 决定是否可执行。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class H5InspectionPublicationClaimDisposition(StrEnum):
    """Publication 唤醒相对数据库事实的处理结论。"""

    CLAIMED = "claimed"
    COMPLETED = "completed"
    BUSY = "busy"
    STALE = "stale"


@dataclass(frozen=True, kw_only=True)
class H5InspectionPublicationLease:
    """Consumer 处理 Publication 所需的最小稳定快照。"""

    publication_id: str
    task_id: str
    source_agent_run_id: str
    consumer_agent_run_id: str
    page_capture_id: str
    page_type_assessment_id: str | None
    assessment_hash: str | None
    ruleset_id: str
    ruleset_version: int
    ruleset_snapshot_hash: str | None
    finalization_epoch: int
    page_type: str | None
    page_state: str
    lease_token: str
    row_version: int
    attempt_count: int
    max_attempts: int
    web_evidence_sequence_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class H5InspectionPublicationClaimResult:
    """claim 结果；只有 CLAIMED 才允许携带租约。"""

    disposition: H5InspectionPublicationClaimDisposition
    lease: H5InspectionPublicationLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, H5InspectionPublicationClaimDisposition):
            raise TypeError("disposition 类型无效")
        if (self.disposition is H5InspectionPublicationClaimDisposition.CLAIMED) != (
            self.lease is not None
        ):
            raise ValueError("claim disposition 与 lease 不一致")


@dataclass(frozen=True, kw_only=True)
class H5InspectionPublicationWriteResult:
    """创建持久化 Publication 后返回的稳定身份。"""

    publication_id: str
    created: bool
    row_version: int | None = None
    outbox_message_id: str | None = None
    outbox_status: str | None = None


@dataclass(frozen=True, kw_only=True)
class H5InspectionPublicationTaskSummary:
    """任务当前 epoch 的 Publication 完成屏障摘要。"""

    total_count: int
    nonterminal_count: int
    published_work_item_count: int
    excluded_active_session_rule_count: int
    needs_review_count: int
    failed_count: int


class H5InspectionPublicationRepository(Protocol):
    """Publication 数据库权威端口。"""

    async def claim(self, **kwargs: object) -> H5InspectionPublicationClaimResult: ...

    async def complete(self, *, lease: H5InspectionPublicationLease, result: object) -> None: ...

    async def process_web_sequence(self, *, lease: H5InspectionPublicationLease) -> tuple[str, ...]: ...

    async def retry(self, *, lease: H5InspectionPublicationLease, error: Exception) -> None: ...
