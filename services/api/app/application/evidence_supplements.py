"""重放补采证据挂接的应用层命令与端口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceSourceGrade,
    EvidenceSupplement,
    build_evidence_supplement,
)


@dataclass(frozen=True, kw_only=True)
class AttachEvidenceSupplementCommand:
    """Orchestrator 提交的完整补采证明引用。"""

    task_id: str
    request_id: str
    manifest_id: str
    probe_work_id: str
    restoration_attempt_id: str
    equivalence_assessment_id: str
    source_grade: EvidenceSourceGrade


@dataclass(frozen=True, kw_only=True)
class PersistedEvidenceSupplement:
    supplement: EvidenceSupplement
    created: bool


class EvidenceSupplementRepository(Protocol):
    async def attach(
        self,
        *,
        supplement: EvidenceSupplement,
    ) -> PersistedEvidenceSupplement: ...


class EvidenceSupplementAttacher:
    """构造稳定身份后，把所有可信校验委托给事实源仓储。"""

    def __init__(
        self,
        *,
        repository: EvidenceSupplementRepository,
        clock: Callable[[], datetime] | None = None,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )

    async def attach(
        self,
        command: AttachEvidenceSupplementCommand,
    ) -> PersistedEvidenceSupplement:
        self._supplemental_evidence_policy.require_enabled(
            SupplementalEvidenceOperation.ATTACH_SUPPLEMENT
        )
        supplement = build_evidence_supplement(
            task_id=command.task_id,
            request_id=command.request_id,
            manifest_id=command.manifest_id,
            probe_work_id=command.probe_work_id,
            restoration_attempt_id=command.restoration_attempt_id,
            equivalence_assessment_id=command.equivalence_assessment_id,
            source_grade=command.source_grade,
            created_at=self._clock(),
        )
        return await self._repository.attach(supplement=supplement)
