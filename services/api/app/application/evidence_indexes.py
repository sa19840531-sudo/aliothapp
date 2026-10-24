"""探索级 Evidence Index 应用端口与盘点快照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.evidence.index import (
    EvidenceActionLogItem,
    EvidenceArtifactIndexItem,
    EvidenceCaptureLineage,
    EvidenceGapItem,
    EvidenceIndexReference,
)


@dataclass(frozen=True, kw_only=True)
class EvidenceInventory:
    """Persistence Gate 盘点快照；不含规则结论。"""

    complete: bool
    capture_ids: tuple[str, ...] = ()
    manifest_ids: tuple[str, ...] = ()
    gaps: tuple[EvidenceGapItem, ...] = ()
    page_timeline: tuple[str, ...] = ()
    capture_lineage: tuple[EvidenceCaptureLineage, ...] = ()
    artifacts: tuple[EvidenceArtifactIndexItem, ...] = ()
    action_logs: tuple[EvidenceActionLogItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise ValueError("complete 必须为布尔值")
        if self.complete and self.gaps:
            raise ValueError("齐全盘点不得携带缺口")
        if not self.complete and not self.gaps:
            raise ValueError("不齐全盘点必须说明缺口")
        timeline = tuple(str(item) for item in self.page_timeline)
        if any(not item.strip() for item in timeline):
            raise ValueError("page_timeline 不能包含空值")
        object.__setattr__(self, "page_timeline", timeline)
        object.__setattr__(self, "capture_lineage", tuple(self.capture_lineage))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "action_logs", tuple(self.action_logs))

    @property
    def has_only_retryable_gaps(self) -> bool:
        """仅在途原写入缺口时可暂不截止并重试；结构性缺口必须不足。"""
        from services.api.app.domain.evidence.index import RETRYABLE_EVIDENCE_GAP_CODES

        if self.complete or not self.gaps:
            return False
        return all(item.code in RETRYABLE_EVIDENCE_GAP_CODES for item in self.gaps)

    @property
    def has_inflight_work(self) -> bool:
        from services.api.app.domain.evidence.index import INFLIGHT_DRAIN_GAP_CODES

        return any(item.code in INFLIGHT_DRAIN_GAP_CODES for item in self.gaps)

    @property
    def has_timed_out_writes(self) -> bool:
        from services.api.app.domain.evidence.index import TIMEOUT_EVIDENCE_GAP_CODES

        return any(item.code in TIMEOUT_EVIDENCE_GAP_CODES for item in self.gaps)


class EvidenceIndexRepository(Protocol):
    """盘点会话证据并幂等封存 Index；禁止发布 Probe / Inspection。"""

    async def inventory(self, *, task_id: str, session_id: str) -> EvidenceInventory: ...

    async def seal(
        self,
        *,
        index: EvidenceIndexReference,
        agent_run_id: str | None = None,
    ) -> EvidenceIndexReference: ...

    async def get(
        self, *, task_id: str, session_id: str
    ) -> EvidenceIndexReference | None: ...
