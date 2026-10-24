"""本地文件证据存储，作为对象存储接入前的可用实现。"""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactComputedIdentity,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.protocol import thaw_json_mapping

DEFAULT_EVIDENCE_STORAGE_ROOT = ".alioth/evidence"


class LocalEvidenceArtifactStore:
    """将页面证据写入本地目录，并返回数据库所需元数据。"""

    def __init__(self, *, root: str | Path | None = None) -> None:
        configured_root = root or os.getenv(
            "ALIOTH_EVIDENCE_STORAGE_ROOT", DEFAULT_EVIDENCE_STORAGE_ROOT
        )
        self._root = Path(configured_root).expanduser().resolve()

    async def store(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact:
        return await asyncio.to_thread(
            self._store_sync,
            task_id,
            page_capture_id,
            artifact_id,
            artifact_type,
            content,
        )

    async def delete(self, storage_path: str) -> None:
        await asyncio.to_thread(self._delete_sync, storage_path)

    async def read(self, storage_path: str) -> bytes:
        return await asyncio.to_thread(self._resolve_path(storage_path).read_bytes)

    async def inspect_identity(self, storage_path: str) -> EvidenceArtifactComputedIdentity:
        """以固定内存上限流式计算存量对象身份。"""
        return await asyncio.to_thread(self._inspect_identity_sync, storage_path)

    def _inspect_identity_sync(self, storage_path: str) -> EvidenceArtifactComputedIdentity:
        digest = hashlib.sha256()
        size_bytes = 0
        with self._resolve_path(storage_path).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        return EvidenceArtifactComputedIdentity(
            checksum=f"sha256:{digest.hexdigest()}",
            size_bytes=size_bytes,
        )

    def _store_sync(
        self,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact:
        data, extension, content_type, metadata = serialize_artifact(artifact_type, content)
        relative_path = Path(task_id) / page_capture_id / f"{artifact_id}.{extension}"
        target = self._root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256(data).hexdigest()
        if target.exists():
            return self._existing_artifact_or_raise(
                target=target,
                relative_path=relative_path,
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                content_type=content_type,
                metadata=metadata,
                data=data,
                checksum=checksum,
            )

        # 唯一临时文件先完整写入，再用硬链接的“仅当目标不存在时创建”语义抢占身份。
        # os.replace 会让并发写入互相覆盖，不能用于不可变证据对象。
        temporary = target.parent / f".{uuid4().hex}.tmp"
        try:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                return self._existing_artifact_or_raise(
                    target=target,
                    relative_path=relative_path,
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    content_type=content_type,
                    metadata=metadata,
                    data=data,
                    checksum=checksum,
                )
            return PersistedEvidenceArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                storage_path=relative_path.as_posix(),
                content_type=content_type,
                checksum=f"sha256:{checksum}",
                size_bytes=len(data),
                metadata=metadata,
                created=True,
            )
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _existing_artifact_or_raise(
        self,
        *,
        target: Path,
        relative_path: Path,
        artifact_id: str,
        artifact_type: str,
        content_type: str,
        metadata: dict[str, Any],
        data: bytes,
        checksum: str,
    ) -> PersistedEvidenceArtifact:
        if not target.is_file():
            raise ValueError("证据对象身份已被非文件占用")
        existing = target.read_bytes()
        if len(existing) != len(data) or hashlib.sha256(existing).hexdigest() != checksum:
            raise ValueError("证据对象不可覆盖：同一身份对应不同内容")
        return PersistedEvidenceArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            storage_path=relative_path.as_posix(),
            content_type=content_type,
            checksum=f"sha256:{checksum}",
            size_bytes=len(existing),
            metadata=metadata,
            created=False,
        )

    def _delete_sync(self, storage_path: str) -> None:
        target = self._resolve_path(storage_path)
        if target.is_file():
            target.unlink()

    def _resolve_path(self, storage_path: str) -> Path:
        target = (self._root / storage_path).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("证据存储路径越出本地证据根目录") from exc
        return target


def serialize_artifact(artifact_type: str, content: Any) -> tuple[bytes, str, str, dict[str, Any]]:
    # RawArtifactSet 为跨边界不可变对象，MappingProxyType/tuple 不能直接被
    # JSON 编码；通过受校验包装还原普通 JSON 值，避免把证据降级成 Python repr。
    if not isinstance(content, (bytes, bytearray)):
        content = thaw_json_mapping({"content": content})["content"]

    # 截圖優先接受原始 PNG 二進位（正式路徑）；Base64 dict 僅備援
    if artifact_type == "screenshot" and isinstance(content, (bytes, bytearray)):
        data = bytes(content)
        if len(data) < 8 or not data.startswith(b"\x89PNG"):
            raise ValueError("screenshot 二進位內容不是有效 PNG")
        return data, "png", "image/png", {"encoding": "binary"}

    if artifact_type == "screenshot" and isinstance(content, dict):
        encoded = content.get("data")
        if content.get("encoding") == "base64" and isinstance(encoded, str):
            metadata = {"encoding": "binary"}
            if content.get("purpose") in {
                "ad_landing",
                "ad_landing_consistency",
                "visual_navigation_viewport",
                "web_full_page_segment",
                "web_scrolled_viewport",
                "web_direct_full_page",
                "h5_video_primary",
                "video_publication_sample",
                "checkpoint_post_action_sample",
            }:
                metadata["purpose"] = content["purpose"]
            if (content.get("purpose") == "h5_video_primary"
                    and type(content.get("full_page")) is bool):
                metadata["full_page"] = content["full_page"]
            return base64.b64decode(encoded), "png", "image/png", metadata

    if artifact_type == "ui_tree" and isinstance(content, (bytes, bytearray)):
        data = bytes(content)
        return data, "xml", "application/xml; charset=utf-8", {"encoding": "utf-8"}

    if artifact_type == "visible_text" and isinstance(content, (bytes, bytearray)):
        data = bytes(content)
        data.decode("utf-8")
        return data, "txt", "text/plain; charset=utf-8", {"encoding": "utf-8"}

    if artifact_type == "visible_text" and isinstance(content, dict):
        text = content.get("text")
        source = content.get("source")
        if not isinstance(text, str) or not isinstance(source, str) or not source.strip():
            raise ValueError("visible_text 投影必须包含 text 与 source")
        metadata = {"encoding": "utf-8", "source": source}
        for key in ("char_count", "node_text_count"):
            value = content.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[key] = value
        if isinstance(content.get("truncated"), bool):
            metadata["truncated"] = content["truncated"]
        return text.encode("utf-8"), "txt", "text/plain; charset=utf-8", metadata

    if (
        artifact_type == "page_coverage"
        and isinstance(content, dict)
        and content.get("protocol_version")
        in {
            "web-full-page-segment-manifest.v1",
            "web-scrolled-viewport-manifest.v1",
        }
    ):
        metadata = {"encoding": "utf-8"}
        for key in (
            "protocol_version",
            "segment_count",
            "associated_page_capture_id",
            "associated_screenshot_artifact_ids",
        ):
            metadata[key] = content.get(key)
        if "capture_mode" in content:
            metadata["capture_mode"] = content.get("capture_mode")
        payload = json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return payload, "json", "application/json", metadata

    if isinstance(content, str):
        extension, content_type = {
            "dom": ("html", "text/html; charset=utf-8"),
            "visible_text": ("txt", "text/plain; charset=utf-8"),
            "ui_tree": ("xml", "application/xml; charset=utf-8"),
        }.get(artifact_type, ("txt", "text/plain; charset=utf-8"))
        return content.encode("utf-8"), extension, content_type, {"encoding": "utf-8"}

    payload = json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")
    metadata = {"encoding": "utf-8"}
    if isinstance(content, dict) and content.get("purpose") in {
        "ad_landing",
        "ad_landing_consistency",
    }:
        metadata["purpose"] = content["purpose"]
    return payload, "json", "application/json", metadata
