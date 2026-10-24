"""App sealed Index Publication 的应用层稳定端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

APP_PUBLICATION_SNAPSHOT_MISMATCH = "App Publication 来源、封存 Index 或任务快照不一致"
APP_PUBLICATION_SNAPSHOT_MISMATCH_CODE = "APP_PUBLICATION_SNAPSHOT_MISMATCH"


class AppPublicationSnapshotMismatch(RuntimeError):
    """封存来源和任务快照对不上。不得据此结束剩余流程或收口。"""

    error_code = APP_PUBLICATION_SNAPSHOT_MISMATCH_CODE
    retryable = True

    def __init__(self) -> None:
        super().__init__(APP_PUBLICATION_SNAPSHOT_MISMATCH)


class AppInspectionPublicationClaimDisposition(StrEnum):
    """唤醒相对 PostgreSQL 权威事实的处理结论。"""

    CLAIMED = "claimed"
    COMPLETED = "completed"
    BUSY = "busy"
    STALE = "stale"


@dataclass(frozen=True, kw_only=True)
class AppInspectionPublicationEntryInput:
    """创建 Publication 时冻结的一个 Index Entry。"""

    sequence_no: int
    capture_id: str
    evidence_manifest_id: str
    evidence_manifest_version: int
    semantic_role: str
    page_type: str | None
    page_state: str


@dataclass(frozen=True, kw_only=True)
class AppInspectionPublicationEntryLease:
    """Consumer 处理一个 Entry 所需的只读快照。"""

    entry_id: int
    sequence_no: int
    capture_id: str
    page_type: str | None
    page_state: str


@dataclass(frozen=True, kw_only=True)
class AppInspectionPublicationLease:
    """整份 Index Publication 的数据库租约与围栏。"""

    publication_id: str
    task_id: str
    source_agent_run_id: str
    evidence_index_id: str
    evidence_index_hash: str
    ruleset_id: str
    ruleset_version: int
    ruleset_snapshot_hash: str | None
    finalization_epoch: int
    lease_token: str
    row_version: int
    attempt_count: int
    max_attempts: int
    entries: tuple[AppInspectionPublicationEntryLease, ...]


@dataclass(frozen=True, kw_only=True)
class AppInspectionPublicationClaimResult:
    """claim 结果；只有 claimed 才允许携带租约。"""

    disposition: AppInspectionPublicationClaimDisposition
    lease: AppInspectionPublicationLease | None = None

    def __post_init__(self) -> None:
        if (
            self.disposition is AppInspectionPublicationClaimDisposition.CLAIMED
        ) != (self.lease is not None):
            raise ValueError("claim disposition 与 lease 不一致")


@dataclass(frozen=True, kw_only=True)
class AppInspectionPublicationWriteResult:
    publication_id: str
    created: bool
    entry_count: int


@dataclass(frozen=True, kw_only=True)
class AppUnpublishedSealedIndex:
    """主 Run 已终止但尚未交接正式质检的当前封存证据。"""

    task_id: str
    source_agent_run_id: str
    finalization_epoch: int


@dataclass(frozen=True, kw_only=True)
class AppInspectionPublicationTaskSummary:
    total_count: int
    nonterminal_count: int
    expected_entry_count: int
    terminal_entry_count: int
    linked_work_item_count: int
    explicit_exclusion_count: int
    needs_review_count: int
    failed_count: int


class AppInspectionPublicationRepository(Protocol):
    async def create_for_index(self, **kwargs: object) -> AppInspectionPublicationWriteResult: ...

    async def claim(self, **kwargs: object) -> AppInspectionPublicationClaimResult: ...

    async def complete(
        self, *, lease: AppInspectionPublicationLease, results: tuple[object, ...]
    ) -> None: ...

    async def retry(
        self, *, lease: AppInspectionPublicationLease, error: Exception
    ) -> None: ...
