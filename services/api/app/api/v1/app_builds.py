"""Android APK 构建制品上传接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import BaseModel

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.application.platform_builds import (
    PlatformBuildService,
    PlatformBuildValidationError,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.storage.local_platform_build_store import (
    LocalPlatformBuildStore,
)

router = APIRouter()
_UPLOAD_CHUNK_SIZE = 1024 * 1024


class AppBuildUploadResponse(BaseModel):
    platform_build_ref: str
    status: str
    original_filename: str
    sha256: str
    size_bytes: int
    package_name: str | None
    version_name: str | None
    version_code: int | None


@lru_cache
def _get_platform_build_service() -> PlatformBuildService:
    from services.api.app.infrastructure.db.platform_build_repository import (
        PostgresPlatformBuildRepository,
    )

    settings = get_settings().platform_builds
    return PlatformBuildService(
        repository=PostgresPlatformBuildRepository(),
        store=LocalPlatformBuildStore(root=settings.local_root),
        max_size_bytes=settings.max_size_bytes,
    )


@router.post(
    "/app-builds",
    response_model=AppBuildUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传并校验 Android APK 构建制品",
)
async def upload_app_build(
    file: Annotated[UploadFile, File(description="Android APK 文件")],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    service: Annotated[PlatformBuildService, Depends(_get_platform_build_service)],
    source_url: Annotated[
        str | None,
        Form(description="APK 原始下载直链；云机可据此自行下载，绕开远端 ADB 慢传"),
    ] = None,
) -> AppBuildUploadResponse:
    try:
        build = await service.upload_apk(
            original_filename=file.filename or "",
            chunks=_read_upload_chunks(file),
            access_scope=access_scope,
            source_url=source_url,
        )
    except PlatformBuildValidationError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    finally:
        await file.close()
    return AppBuildUploadResponse(
        platform_build_ref=build.platform_build_ref,
        status=build.status.value,
        original_filename=build.original_filename,
        sha256=build.sha256,
        size_bytes=build.size_bytes,
        package_name=build.package_name,
        version_name=build.version_name,
        version_code=build.version_code,
    )


async def _read_upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
        yield chunk
