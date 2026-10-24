"""APK 构建制品上传、校验与持久化编排。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from services.api.app.application.apk_identity import ApkIdentity, extract_apk_identity
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task.platform_build import PlatformBuild

logger = logging.getLogger(__name__)

_APK_MAGIC_VALUES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class PlatformBuildValidationError(ValueError):
    """APK 文件不满足平台制品要求。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class StagedPlatformBuild:
    """对象存储提交前的临时 APK 摘要。"""

    stage_id: str
    sha256: str
    size_bytes: int
    header: bytes


class PlatformBuildStore(Protocol):
    async def stage(
        self,
        chunks: AsyncIterable[bytes],
        *,
        max_size_bytes: int,
    ) -> StagedPlatformBuild: ...

    async def commit(self, staged: StagedPlatformBuild) -> str: ...

    async def discard(self, staged: StagedPlatformBuild) -> None: ...

    def resolve_path(self, storage_path: str) -> Path: ...


class PlatformBuildRepository(Protocol):
    async def get_verified_by_ref(
        self,
        platform_build_ref: str,
    ) -> PlatformBuild | None: ...

    async def find_verified_by_sha256(
        self,
        sha256: str,
        *,
        access_scope: AccessScope,
    ) -> PlatformBuild | None: ...

    async def create_upload(
        self,
        *,
        original_filename: str,
        sha256: str,
        size_bytes: int,
        created_by: str,
        tenant_id: str,
    ) -> PlatformBuild: ...

    async def mark_verified(
        self,
        *,
        build_id: str,
        storage_path: str,
        package_name: str | None = None,
        version_name: str | None = None,
        version_code: int | None = None,
    ) -> PlatformBuild: ...

    async def mark_rejected(self, *, build_id: str, reason: str) -> None: ...


class PlatformBuildService:
    """流式保存 APK，并且只发布校验成功的稳定引用。"""

    def __init__(
        self,
        *,
        repository: PlatformBuildRepository,
        store: PlatformBuildStore,
        max_size_bytes: int,
        identity_extractor: Callable[[Path], ApkIdentity] | None = None,
    ) -> None:
        if max_size_bytes <= 0:
            raise ValueError("max_size_bytes 必须大于 0")
        self._repository = repository
        self._store = store
        self._max_size_bytes = max_size_bytes
        self._identity_extractor = identity_extractor or extract_apk_identity

    async def upload_apk(
        self,
        *,
        original_filename: str,
        chunks: AsyncIterable[bytes],
        access_scope: AccessScope,
        source_url: str | None = None,
    ) -> PlatformBuild:
        filename = _validate_filename(original_filename)
        origin = _validated_source_url(source_url)
        if not isinstance(access_scope, AccessScope):
            raise PlatformBuildValidationError(
                "上传 App 构建必须提供明确的 AccessScope",
                code="ACCESS_SCOPE_MISSING",
            )
        created_by = access_scope.actor_user_id
        if not isinstance(created_by, str) or not created_by.strip():
            raise PlatformBuildValidationError("created_by 不能为空", code="CREATOR_MISSING")
        staged = await self._store.stage(chunks, max_size_bytes=self._max_size_bytes)
        existing = await self._repository.find_verified_by_sha256(
            staged.sha256,
            access_scope=access_scope,
        )
        if existing is not None:
            await self._store.discard(staged)
            if origin is not None and existing.storage_path:
                _write_source_url_sidecar(
                    self._store.resolve_path(existing.storage_path), origin
                )
            return existing

        pending = await self._repository.create_upload(
            original_filename=filename,
            sha256=staged.sha256,
            size_bytes=staged.size_bytes,
            created_by=created_by.strip(),
            tenant_id=access_scope.tenant_id,
        )
        try:
            _validate_apk_magic(staged.header)
            storage_path = await self._store.commit(staged)
            if origin is not None:
                _write_source_url_sidecar(self._store.resolve_path(storage_path), origin)
            identity = self._identity_extractor(self._store.resolve_path(storage_path))
            if identity.package_name is None:
                raise PlatformBuildValidationError(
                    "无法从 APK 解析 package_name（aapt/AndroidManifest）",
                    code="APK_PACKAGE_UNRESOLVED",
                )
            return await self._repository.mark_verified(
                build_id=pending.build_id,
                storage_path=storage_path,
                package_name=identity.package_name,
                version_name=identity.version_name,
                version_code=identity.version_code,
            )
        except PlatformBuildValidationError as exc:
            await self._store.discard(staged)
            await self._repository.mark_rejected(
                build_id=pending.build_id,
                reason=exc.code,
            )
            raise
        except Exception:
            await self._store.discard(staged)
            await self._repository.mark_rejected(
                build_id=pending.build_id,
                reason="STORAGE_COMMIT_FAILED",
            )
            raise


def _validate_filename(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformBuildValidationError("APK 文件名不能为空", code="FILENAME_MISSING")
    filename = value.strip()
    if "/" in filename or "\\" in filename or not filename.lower().endswith(".apk"):
        raise PlatformBuildValidationError(
            "只允许上传不含路径的 .apk 文件",
            code="APK_FILENAME_INVALID",
        )
    return filename


def _validate_apk_magic(header: bytes) -> None:
    if not any(header.startswith(magic) for magic in _APK_MAGIC_VALUES):
        raise PlatformBuildValidationError(
            "文件不是有效的 APK/ZIP 容器",
            code="APK_MAGIC_INVALID",
        )


def _validated_source_url(value: str | None) -> str | None:
    """校验 APK 原始下载直链；仅接受 http(s)。"""
    if value is None:
        return None
    url = str(value).strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        raise PlatformBuildValidationError(
            "source_url 必须是 HTTP(S) 直链",
            code="APK_SOURCE_URL_INVALID",
        )
    if len(url) > 4096 or any(ch in url for ch in "\r\n"):
        raise PlatformBuildValidationError(
            "source_url 长度超限或包含换行",
            code="APK_SOURCE_URL_INVALID",
        )
    return url


def _write_source_url_sidecar(apk_path: Path, source_url: str) -> None:
    """在制品旁写 ``<apk>.srcurl``，供云机安装时自行下载绕开远端 ADB 慢传。

    写失败不影响上传结果：安装侧读不到 sidecar 会回退原 ADB 路径。
    """
    try:
        Path(f"{apk_path}.srcurl").write_text(source_url, encoding="utf-8")
    except OSError:
        logger.warning("写入 APK 源链接 sidecar 失败：%s", apk_path)
