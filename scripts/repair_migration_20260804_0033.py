"""修复数据库版本已越过 20260804_0033，但该版本 DDL 完全未落库的历史漂移。

默认只审计；只有显式传入 ``--apply`` 才会在单一事务内补齐
``qc_task`` App 目标约束与 ``qc_platform_build`` 表。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv

_MINIMUM_REPAIR_VERSION = "20260804_0033"


def _database_url() -> str:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    value = (os.getenv("ALIOTH_DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError("缺少 ALIOTH_DATABASE_URL，拒绝连接数据库")
    return value


async def _audit(connection: asyncpg.Connection[Any]) -> dict[str, Any]:
    version = await connection.fetchval("SELECT version_num FROM alembic_version")
    target_platform_exists = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'qc_task'
              AND column_name = 'target_platform'
        )
        """
    )
    platform_build_exists = await connection.fetchval(
        "SELECT to_regclass(current_schema() || '.qc_platform_build') IS NOT NULL"
    )
    invalid_h5_targets = await connection.fetchval(
        """
        SELECT count(*)
        FROM qc_task
        WHERE target_url IS NULL OR target_url !~* '^https?://'
        """
    )
    return {
        "alembic_version": version,
        "target_platform_exists": target_platform_exists,
        "platform_build_exists": platform_build_exists,
        "invalid_legacy_target_count": invalid_h5_targets,
    }


def _validate_repairable(audit: dict[str, Any]) -> bool:
    version = str(audit["alembic_version"] or "")
    if version < _MINIMUM_REPAIR_VERSION:
        raise RuntimeError(f"当前 Alembic 版本 {version!r} 尚未到 20260804_0033，应使用正式迁移")
    target_exists = bool(audit["target_platform_exists"])
    build_exists = bool(audit["platform_build_exists"])
    if target_exists != build_exists:
        raise RuntimeError("20260804_0033 只有部分 DDL 存在，拒绝自动修复，需先人工审核")
    if int(audit["invalid_legacy_target_count"] or 0) > 0:
        raise RuntimeError("存在空值或非 HTTP(S) 的旧任务目标，无法安全转为默认 H5 任务")
    return target_exists and build_exists


async def _apply(connection: asyncpg.Connection[Any]) -> None:
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext('alioth:migration-repair:20260804_0033'))"
    )
    audit = await _audit(connection)
    if _validate_repairable(audit):
        return
    await connection.execute(
        """
        ALTER TABLE qc_task
            ADD COLUMN target_platform varchar(16) NOT NULL DEFAULT 'h5',
            ALTER COLUMN target_url DROP NOT NULL,
            ADD CONSTRAINT ck_qc_task_target_platform CHECK (
                target_platform IN ('h5', 'app', 'web')
            ),
            ADD CONSTRAINT ck_qc_task_target_shape CHECK (
                (
                    target_platform IN ('h5', 'web')
                    AND target_url IS NOT NULL
                    AND target_url ~* '^https?://'
                )
                OR (
                    target_platform = 'app'
                    AND target_url IS NULL
                    AND NULLIF(
                        BTRIM(task_config #>> '{app,platform_build_ref}'),
                        ''
                    ) IS NOT NULL
                )
            )
        """
    )
    await connection.execute(
        """
        CREATE TABLE qc_platform_build (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            platform varchar(16) NOT NULL DEFAULT 'android',
            original_filename varchar(512) NOT NULL,
            storage_path text,
            sha256 varchar(64) NOT NULL UNIQUE,
            size_bytes bigint NOT NULL,
            package_name varchar(512),
            version_name varchar(256),
            version_code bigint,
            status varchar(32) NOT NULL DEFAULT 'uploading',
            rejection_reason text,
            created_by uuid,
            verified_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_platform_build_platform CHECK (platform = 'android'),
            CONSTRAINT ck_qc_platform_build_status CHECK (
                status IN ('uploading', 'verified', 'rejected', 'deleted')
            ),
            CONSTRAINT ck_qc_platform_build_size CHECK (size_bytes > 0),
            CONSTRAINT ck_qc_platform_build_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_qc_platform_build_text CHECK (BTRIM(original_filename) <> ''),
            CONSTRAINT ck_qc_platform_build_verified CHECK (
                (
                    status = 'verified'
                    AND storage_path IS NOT NULL
                    AND verified_at IS NOT NULL
                    AND rejection_reason IS NULL
                )
                OR (
                    status = 'rejected'
                    AND verified_at IS NULL
                    AND rejection_reason IS NOT NULL
                )
                OR status IN ('uploading', 'deleted')
            )
        )
        """
    )
    await connection.execute(
        """
        CREATE INDEX idx_qc_platform_build_status
        ON qc_platform_build (platform, status, created_at DESC)
        """
    )
    await connection.execute(
        """
        CREATE INDEX idx_qc_task_target_platform
        ON qc_task (target_platform, status, created_at)
        """
    )


async def _run(*, apply: bool) -> None:
    connection = await asyncpg.connect(_database_url())
    try:
        before = await _audit(connection)
        already_complete = _validate_repairable(before)
        if apply and not already_complete:
            async with connection.transaction():
                await _apply(connection)
        after = await _audit(connection)
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "audit",
                    "before": before,
                    "after": after,
                    "repair_complete": _validate_repairable(after),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="显式执行单事务修复；不传时只审计",
    )
    args = parser.parse_args()
    asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    main()
