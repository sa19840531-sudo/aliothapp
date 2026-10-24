"""Evidence Request 独立持久化受理，不发布 Probe Work。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import EvidenceRequest


class EvidenceRequestAdmissionRepository(Protocol):
    """独立保存 Evidence Request 的最小仓储端口。"""

    async def upsert(self, *, request: EvidenceRequest) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceRequestAdmissionResult:
    """本次已受理的请求身份。"""

    admitted_request_ids: tuple[str, ...]


class EvidenceRequestAdmissionService:
    """在补采门禁允许时只持久化请求，不执行 Surface 操作。"""

    def __init__(
        self,
        *,
        repository: EvidenceRequestAdmissionRepository,
        policy: SupplementalEvidencePolicy,
    ) -> None:
        self._repository = repository
        self._policy = policy

    async def admit(
        self,
        *,
        requests: tuple[EvidenceRequest, ...],
    ) -> EvidenceRequestAdmissionResult:
        if not isinstance(requests, tuple) or any(
            not isinstance(request, EvidenceRequest) for request in requests
        ):
            raise TypeError("requests 必须为 EvidenceRequest 元组")
        if not requests:
            return EvidenceRequestAdmissionResult(admitted_request_ids=())
        self._policy.require_enabled(SupplementalEvidenceOperation.PRODUCE_REQUEST)
        admitted: list[str] = []
        for request in requests:
            await self._repository.upsert(request=request)
            admitted.append(request.request_id)
        return EvidenceRequestAdmissionResult(
            admitted_request_ids=tuple(admitted),
        )
