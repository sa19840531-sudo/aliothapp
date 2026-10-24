"""PostgreSQL APK 构建制品仓储。"""

from __future__ import annotations

from typing import Any

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task.platform_build import PlatformBuild, PlatformBuildStatus
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresPlatformBuildRepository:
    """以数据库状态保护 APK 从 uploading 到最终状态的单向迁移。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get_verified_by_ref(
        self,
        platform_build_ref: str,
    ) -> PlatformBuild | None:
        prefix = "app-build:"
        if (
            not isinstance(platform_build_ref, str)
            or not platform_build_ref.startswith(prefix)
            or not platform_build_ref.removeprefix(prefix).strip()
        ):
            raise ValueError("platform_build_ref 必须使用 app-build:<id>")
        build_id = platform_build_ref.removeprefix(prefix)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM qc_platform_build
                WHERE id = $1::uuid
                  AND platform = 'android'
                  AND status = 'verified'
                """,
                build_id,
            )
        return _build_from_row(row)

    async def find_verified_by_sha256(
        self,
        sha256: str,
        *,
        access_scope: AccessScope,
    ) -> PlatformBuild | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM qc_platform_build
                WHERE sha256 = $1
                  AND status = 'verified'
                  AND tenant_id = $2::uuid
                  AND created_by = $3::uuid
                LIMIT 1
                """,
                sha256,
                access_scope.tenant_id,
                access_scope.actor_user_id,
            )
        return _build_from_row(row)

    async def create_upload(
        self,
        *,
        original_filename: str,
        sha256: str,
        size_bytes: int,
        created_by: str,
        tenant_id: str,
    ) -> PlatformBuild:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_platform_build (
                    platform,
                    original_filename,
                    sha256,
                    size_bytes,
                    status,
                    created_by,
                    tenant_id
                )
                VALUES ('android', $1, $2, $3, 'uploading', $4::uuid, $5::uuid)
                ON CONFLICT (tenant_id, created_by, sha256)
                DO UPDATE SET
                    original_filename = EXCLUDED.original_filename,
                    size_bytes = EXCLUDED.size_bytes,
                    status = 'uploading',
                    rejection_reason = NULL,
                    storage_path = NULL,
                    updated_at = now()
                WHERE qc_platform_build.status <> 'verified'
                RETURNING *
                """,
                original_filename,
                sha256,
                size_bytes,
                created_by,
                tenant_id,
            )
        build = _build_from_row(row)
        if build is None:
            # 同 (租户, 上传者, sha256) 已存在 verified 记录：并发下由上游幂等分支接管
            raise RuntimeError("创建 APK 构建制品记录失败：同校验和已存在已验证制品")
        return build

    async def mark_verified(
        self,
        *,
        build_id: str,
        storage_path: str,
        package_name: str | None = None,
        version_name: str | None = None,
        version_code: int | None = None,
    ) -> PlatformBuild:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_platform_build
                SET
                    storage_path = $2,
                    package_name = $3,
                    version_name = $4,
                    version_code = $5,
                    status = 'verified',
                    rejection_reason = NULL,
                    verified_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid AND status = 'uploading'
                RETURNING *
                """,
                build_id,
                storage_path,
                package_name,
                version_name,
                version_code,
            )
        build = _build_from_row(row)
        if build is None:
            raise RuntimeError("APK 构建制品状态已变化，不能标记 verified")
        return build

    async def mark_rejected(self, *, build_id: str, reason: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_platform_build
                SET
                    status = 'rejected',
                    rejection_reason = $2,
                    verified_at = NULL,
                    updated_at = now()
                WHERE id = $1::uuid AND status = 'uploading'
                """,
                build_id,
                reason,
            )

    async def _get_pool(self):
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _build_from_row(row: Any | None) -> PlatformBuild | None:
    if row is None:
        return None
    return PlatformBuild(
        build_id=str(row["id"]),
        platform=str(row["platform"]),
        original_filename=str(row["original_filename"]),
        storage_path=row.get("storage_path"),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        status=PlatformBuildStatus(str(row["status"])),
        created_by=str(row["created_by"]),
        package_name=row.get("package_name"),
        version_name=row.get("version_name"),
        version_code=row.get("version_code"),
    )
