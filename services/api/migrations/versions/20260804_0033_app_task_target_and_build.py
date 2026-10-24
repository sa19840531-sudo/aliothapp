"""增加 App 任务目标和 APK 构建制品。

Revision ID: 20260804_0033
Revises: 20260803_0032
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "20260804_0033"
down_revision = "20260803_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
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
    op.execute(
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
            CONSTRAINT ck_qc_platform_build_platform CHECK (
                platform = 'android'
            ),
            CONSTRAINT ck_qc_platform_build_status CHECK (
                status IN ('uploading', 'verified', 'rejected', 'deleted')
            ),
            CONSTRAINT ck_qc_platform_build_size CHECK (
                size_bytes > 0
            ),
            CONSTRAINT ck_qc_platform_build_sha256 CHECK (
                sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_platform_build_text CHECK (
                BTRIM(original_filename) <> ''
            ),
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
    op.execute(
        """
        CREATE INDEX idx_qc_platform_build_status
        ON qc_platform_build (platform, status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_task_target_platform
        ON qc_task (target_platform, status, created_at)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_task WHERE target_platform = 'app') THEN
                RAISE EXCEPTION '存在 App 任务，不能降级为 URL-only 任务结构';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_task_target_platform")
    op.execute("DROP INDEX IF EXISTS idx_qc_platform_build_status")
    op.execute(
        """
        ALTER TABLE qc_task
            DROP CONSTRAINT IF EXISTS ck_qc_task_target_shape,
            DROP CONSTRAINT IF EXISTS ck_qc_task_target_platform,
            ALTER COLUMN target_url SET NOT NULL,
            DROP COLUMN IF EXISTS target_platform
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_platform_build")
