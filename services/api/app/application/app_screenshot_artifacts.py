"""App 截圖 Artifact：本體 PNG 二進位落庫；跨模組回傳 artifact_id + URL。

尚未綁定正式 qc_task 時，用本地索引承接元數據；讀取走 mobile-demo artifacts 端點。
正式任務接入後可改掛 /api/v1/evidence/{id}/file。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)

APP_ARTIFACT_FILE_URL_PREFIX = "/api/v1/mobile-demo/artifacts"


class EvidenceArtifactStorePort(Protocol):
    async def store(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact: ...

    async def read(self, storage_path: str) -> bytes: ...


@dataclass(frozen=True, kw_only=True)
class AppScreenshotArtifactRef:
    """跨模組可傳遞的截圖引用（不含像素本體）。"""

    artifact_id: str
    page_capture_id: str
    task_id: str
    content_type: str
    checksum: str
    size_bytes: int
    storage_path: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        """公開回應不含 storage_path（僅內部索引保留）。"""
        return {
            "artifact_id": self.artifact_id,
            "page_capture_id": self.page_capture_id,
            "task_id": self.task_id,
            "content_type": self.content_type,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "url": self.url,
        }

    def to_index_dict(self) -> dict[str, Any]:
        """本機索引完整欄位（含 storage_path）。"""
        data = self.to_dict()
        data["storage_path"] = self.storage_path
        return data


def build_app_screenshot_artifact_url(artifact_id: str) -> str:
    return f"{APP_ARTIFACT_FILE_URL_PREFIX}/{artifact_id}/file"


class AppScreenshotArtifactService:
    """把 PNG bytes 寫入 Evidence Store，並維護可回讀索引。"""

    def __init__(
        self,
        *,
        store: EvidenceArtifactStorePort | None = None,
        index_root: Path | None = None,
    ) -> None:
        if store is not None:
            self._store = store
        else:
            try:
                from services.api.app.core.config import get_settings
                from services.api.app.infrastructure.storage.factory import (
                    build_evidence_artifact_store,
                )

                self._store = build_evidence_artifact_store(get_settings().evidence_storage)
            except Exception:  # noqa: BLE001 - Demo/測試環境缺配置時回退本地
                self._store = LocalEvidenceArtifactStore()
        if index_root is not None:
            self._index_root = index_root
        elif isinstance(self._store, LocalEvidenceArtifactStore):
            self._index_root = Path(self._store._root) / "_app_artifact_index"  # noqa: SLF001
        else:
            # MinIO 等後端：索引仍落本機 evidence root，避免 Demo 無法回讀
            from services.api.app.infrastructure.storage.local_evidence_store import (
                DEFAULT_EVIDENCE_STORAGE_ROOT,
            )
            root = os.getenv("ALIOTH_EVIDENCE_STORAGE_ROOT", DEFAULT_EVIDENCE_STORAGE_ROOT)
            self._index_root = Path(root).expanduser().resolve() / "_app_artifact_index"
        self._index_root.mkdir(parents=True, exist_ok=True)

    async def persist_png(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        png: bytes,
        artifact_id: str | None = None,
    ) -> AppScreenshotArtifactRef:
        aid = (artifact_id or str(uuid4())).strip()
        persisted = await self._store.store(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=aid,
            artifact_type="screenshot",
            content=bytes(png),
        )
        ref = AppScreenshotArtifactRef(
            artifact_id=persisted.artifact_id,
            page_capture_id=page_capture_id,
            task_id=task_id,
            content_type=persisted.content_type or "image/png",
            checksum=persisted.checksum,
            size_bytes=persisted.size_bytes,
            storage_path=persisted.storage_path,
            url=build_app_screenshot_artifact_url(persisted.artifact_id),
        )
        self._write_index(ref)
        return ref

    async def read_png(self, artifact_id: str) -> tuple[AppScreenshotArtifactRef, bytes]:
        ref = self.get_ref(artifact_id)
        if ref is None:
            raise FileNotFoundError(f"未找到 App 截圖 artifact：{artifact_id}")
        content = await self._store.read(ref.storage_path)
        return ref, content

    def get_ref(self, artifact_id: str) -> AppScreenshotArtifactRef | None:
        path = self._index_path(artifact_id)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AppScreenshotArtifactRef(
            artifact_id=str(raw["artifact_id"]),
            page_capture_id=str(raw["page_capture_id"]),
            task_id=str(raw["task_id"]),
            content_type=str(raw.get("content_type") or "image/png"),
            checksum=str(raw["checksum"]),
            size_bytes=int(raw["size_bytes"]),
            storage_path=str(raw["storage_path"]),
            url=str(raw.get("url") or build_app_screenshot_artifact_url(str(raw["artifact_id"]))),
        )

    def _index_path(self, artifact_id: str) -> Path:
        safe = "".join(ch for ch in artifact_id if ch.isalnum() or ch in "-_")
        if not safe or safe != artifact_id:
            raise ValueError("非法 artifact_id")
        return self._index_root / f"{safe}.json"

    def _write_index(self, ref: AppScreenshotArtifactRef) -> None:
        path = self._index_path(ref.artifact_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(ref.to_index_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


_default_service: AppScreenshotArtifactService | None = None


def get_app_screenshot_artifact_service() -> AppScreenshotArtifactService:
    global _default_service
    if _default_service is None:
        _default_service = AppScreenshotArtifactService()
    return _default_service
