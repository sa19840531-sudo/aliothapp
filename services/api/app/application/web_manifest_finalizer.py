"""Web 收口清单裁决：隔离引导证据的失败不得阻断主线。"""

from __future__ import annotations

import logging

from services.api.app.domain.evidence import EvidenceQualityStatus

_LOGGER = logging.getLogger(__name__)


class WebPageManifestFinalizer:
    """普通 Web Capture 严格裁决，引导支线尽力裁决并独立留痕。"""

    def __init__(self, *, repository, finalizer):
        self._repository = repository
        self._finalizer = finalizer

    async def finalize_capture(self, *, task_id: str, capture_id: str) -> None:
        manifest = await self._repository.get_latest_for_capture(
            task_id=task_id, capture_id=capture_id,
        )
        if manifest is not None:
            await self._finalizer.finalize(manifest)

    async def finalize_session(
        self, *, task_id: str, session_id: str, agent_run_id: str,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at=None,
    ) -> int:
        captures = await self._repository.list_latest_for_session(
            task_id=task_id, session_id=session_id, agent_run_id=agent_run_id,
        )
        fence = {}
        if recovery_lease_token is not None:
            fence = {
                "recovery_lease_token": recovery_lease_token,
                "recovery_generation": recovery_generation,
                "recovery_lease_expires_at": recovery_lease_expires_at,
            }
        finalized = 0
        for item in captures:
            manifest = item.manifest
            if manifest is None or manifest.quality_status is not EvidenceQualityStatus.PENDING:
                continue
            await self._finalizer.finalize(manifest, **fence)
            finalized += 1
        try:
            guides = await self._repository.list_optional_guides_for_session(
                task_id=task_id, session_id=session_id, agent_run_id=agent_run_id,
            )
            for item in guides:
                manifest = item.manifest
                if manifest is None or manifest.quality_status is not EvidenceQualityStatus.PENDING:
                    continue
                try:
                    await self._finalizer.finalize(manifest, **fence)
                    finalized += 1
                except Exception:  # noqa: BLE001 - 引导清单故障只保留支线诊断
                    _LOGGER.warning(
                        "web_guide_manifest_finalize_failed capture_id=%s", item.capture_id,
                    )
        except Exception:  # noqa: BLE001 - 可选引导清单的读取故障不得中断主线
            _LOGGER.warning("web_guide_manifest_inventory_failed task_id=%s", task_id)
        return finalized
