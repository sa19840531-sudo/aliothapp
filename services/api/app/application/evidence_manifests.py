"""Evidence Manifest 的应用层持久化端口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.protocol import (
    freeze_json_mapping,
    require_aware_datetime,
    require_text,
)


@dataclass(frozen=True, kw_only=True)
class PersistedEvidenceManifest:
    """幂等写入结果。"""

    manifest: EvidenceManifestReference
    created: bool


@dataclass(frozen=True, kw_only=True)
class CaptureEvidenceManifest:
    """Capture 上下文与该 Capture 最新 Manifest 的只读投影。"""

    capture_id: str
    agent_run_id: str
    capture_role: str
    captured_at: datetime
    metadata: Mapping[str, object]
    manifest: EvidenceManifestReference | None = None
    action_execution_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.capture_id, "capture_id")
        if self.manifest is not None:
            if not isinstance(self.manifest, EvidenceManifestReference):
                raise TypeError("manifest 必须为 EvidenceManifestReference")
            if self.manifest.capture_id != self.capture_id:
                raise ValueError("manifest 与 capture_id 不一致")
        require_text(self.agent_run_id, "agent_run_id")
        require_text(self.capture_role, "capture_role")
        require_aware_datetime(self.captured_at, "captured_at")
        if self.action_execution_id is not None:
            require_text(self.action_execution_id, "action_execution_id")
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata, "metadata"))


class EvidenceManifestRepository(Protocol):
    """Capture Pipeline 原子写入清单和就绪事件的端口。"""

    async def add(
        self,
        *,
        manifest: EvidenceManifestReference,
        **kwargs: object,
    ) -> PersistedEvidenceManifest: ...

    async def get(self, *, manifest_id: str) -> EvidenceManifestReference | None: ...

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ) -> EvidenceManifestReference | None: ...

    async def list_latest_for_session(
        self,
        *,
        task_id: str,
        session_id: str,
        agent_run_id: str,
    ) -> tuple[CaptureEvidenceManifest, ...]: ...

    async def list_stale_pending(
        self,
        *,
        created_before: datetime,
        limit: int = 100,
    ) -> tuple[EvidenceManifestReference, ...]: ...
