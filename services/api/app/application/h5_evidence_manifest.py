"""H5 补证读取前及收口阶段将 Evidence Manifest 校验为可引用终态。"""

from __future__ import annotations

from datetime import datetime

from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.domain.evidence import EvidenceQualityStatus


class H5EvidenceManifestFinalizer:
    """按 Capture 校验已提交制品，收口时幂等结算 Session 的剩余清单。"""

    def __init__(
        self,
        *,
        repository: EvidenceManifestRepository,
        finalizer: AppEvidenceManifestFinalizer,
    ) -> None:
        self._repository = repository
        self._finalizer = finalizer

    async def finalize_capture(self, *, task_id: str, capture_id: str) -> None:
        manifest = await self._repository.get_latest_for_capture(
            task_id=task_id,
            capture_id=capture_id,
        )
        if manifest is not None:
            await self._finalizer.finalize(manifest)

    async def finalize_session(
        self,
        *,
        task_id: str,
        session_id: str,
        agent_run_id: str,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> int:
        """幂等裁决一次 Session 下每个 Capture 的最新 pending Manifest。"""

        captures = await self._repository.list_latest_for_session(
            task_id=task_id,
            session_id=session_id,
            agent_run_id=agent_run_id,
        )
        finalized = 0
        for item in captures:
            manifest = item.manifest
            if (
                manifest is None
                or manifest.quality_status is not EvidenceQualityStatus.PENDING
            ):
                continue
            await self._finalizer.finalize(
                manifest,
                **_recovery_fence_kwargs(
                    recovery_lease_token,
                    recovery_generation,
                    recovery_lease_expires_at,
                ),
            )
            finalized += 1
        return finalized


def _recovery_fence_kwargs(
    token: str | None,
    generation: int | None,
    expires_at: datetime | None,
) -> dict[str, object]:
    if token is None:
        return {}
    return {
        "recovery_lease_token": token,
        "recovery_generation": generation,
        "recovery_lease_expires_at": expires_at,
    }
