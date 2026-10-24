"""本地 APK 构建制品的临时写入和原子提交。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from services.api.app.application.platform_builds import (
    PlatformBuildValidationError,
    StagedPlatformBuild,
)

# 上传写盘使用独立线程池，避免占满默认 executor 拖死同进程其它请求。
_WRITE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="apk-store")


class LocalPlatformBuildStore:
    """按 SHA-256 保存 APK，所有调用路径都限制在配置根目录内。"""

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._staging_root = self._root / ".staging"

    async def stage(
        self,
        chunks: AsyncIterable[bytes],
        *,
        max_size_bytes: int,
    ) -> StagedPlatformBuild:
        stage_id = uuid4().hex
        temporary_path = self._stage_path(stage_id)
        await asyncio.to_thread(temporary_path.parent.mkdir, parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size_bytes = 0
        header = bytearray()
        loop = asyncio.get_running_loop()
        handle = await loop.run_in_executor(_WRITE_EXECUTOR, temporary_path.open, "xb")
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise PlatformBuildValidationError(
                        "APK 上传块必须为 bytes",
                        code="APK_CHUNK_INVALID",
                    )
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > max_size_bytes:
                    raise PlatformBuildValidationError(
                        "APK 超过配置的大小上限",
                        code="APK_TOO_LARGE",
                    )
                if len(header) < 4:
                    header.extend(chunk[: 4 - len(header)])
                digest.update(chunk)
                await loop.run_in_executor(_WRITE_EXECUTOR, handle.write, chunk)
            if size_bytes == 0:
                raise PlatformBuildValidationError(
                    "APK 文件不能为空",
                    code="APK_EMPTY",
                )
        except Exception:
            await loop.run_in_executor(_WRITE_EXECUTOR, _close_quietly, handle)
            await asyncio.to_thread(_unlink_if_exists, temporary_path)
            raise
        await loop.run_in_executor(_WRITE_EXECUTOR, handle.close)
        return StagedPlatformBuild(
            stage_id=stage_id,
            sha256=digest.hexdigest(),
            size_bytes=size_bytes,
            header=bytes(header),
        )

    async def commit(self, staged: StagedPlatformBuild) -> str:
        temporary_path = self._stage_path(staged.stage_id)
        relative_path = Path(staged.sha256[:2]) / f"{staged.sha256}.apk"
        target_path = self._resolve_relative(relative_path)
        await asyncio.to_thread(target_path.parent.mkdir, parents=True, exist_ok=True)
        if target_path.exists():
            await asyncio.to_thread(_unlink_if_exists, temporary_path)
        else:
            await asyncio.to_thread(os.replace, temporary_path, target_path)
        return relative_path.as_posix()

    async def discard(self, staged: StagedPlatformBuild) -> None:
        await asyncio.to_thread(_unlink_if_exists, self._stage_path(staged.stage_id))

    def resolve_path(self, storage_path: str) -> Path:
        """把已提交的相对存储路径解析为根目录内的绝对路径。"""
        if not isinstance(storage_path, str) or not storage_path.strip():
            raise ValueError("storage_path 不能为空")
        relative = Path(storage_path.strip().replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("storage_path 必须为根目录内相对路径")
        return self._resolve_relative(relative)

    def _stage_path(self, stage_id: str) -> Path:
        if not stage_id.isalnum():
            raise ValueError("stage_id 不合法")
        return self._resolve_relative(Path(".staging") / f"{stage_id}.part")

    def _resolve_relative(self, relative_path: Path) -> Path:
        target = (self._root / relative_path).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("APK 存储路径越出配置根目录") from exc
        return target


def _close_quietly(handle: object) -> None:
    close = getattr(handle, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _unlink_if_exists(path: Path) -> None:
    if path.is_file():
        path.unlink()
