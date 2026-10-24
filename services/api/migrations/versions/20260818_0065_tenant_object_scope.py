"""建立租户与业务对象主体约束。

Revision ID: 20260818_0065
Revises: 20260817_0064
Create Date: 2026-08-18

历史任务和 App 构建不能伪装成现有管理员创建，因此统一回填到固定的默认租户，
并使用一个固定、禁用的历史数据主体承接原来没有 owner 的记录。新业务代码必须
从已认证主体写入 tenant/owner，不得把这两个字段暴露为客户端可控输入。
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0065"
down_revision = "20260817_0064"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
HISTORY_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    """把任务、账号和 App 构建纳入租户/主体边界。"""

    op.execute(
        """
        CREATE TABLE qc_tenant (
            id uuid PRIMARY KEY,
            tenant_key varchar(64) NOT NULL,
            display_name varchar(128) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'enabled',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_tenant_key UNIQUE (tenant_key),
            CONSTRAINT ck_qc_tenant_status CHECK (
                status IN ('enabled', 'disabled')
            )
        )
        """
    )
    op.execute(
        f"""
        INSERT INTO qc_tenant (
            id, tenant_key, display_name, status
        )
        VALUES (
            '{DEFAULT_TENANT_ID}'::uuid,
            'default',
            '默认租户（历史数据归属）',
            'enabled'
        )
        """
    )

    # 账号先补租户，再建立外键；固定历史主体明确表示迁移来源未知。
    op.execute("ALTER TABLE qc_user ADD COLUMN tenant_id uuid")
    op.execute(
        f"""
        UPDATE qc_user
        SET tenant_id = '{DEFAULT_TENANT_ID}'::uuid
        WHERE tenant_id IS NULL
        """
    )
    op.execute(
        f"""
        INSERT INTO qc_user (
            id,
            username,
            full_name,
            password_hash,
            role,
            status,
            tenant_id
        )
        VALUES (
            '{HISTORY_PRINCIPAL_ID}'::uuid,
            '__history_principal__',
            '历史数据主体（禁用）',
            'disabled-history-principal',
            '审计员',
            'disabled',
            '{DEFAULT_TENANT_ID}'::uuid
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_user
            ALTER COLUMN tenant_id SET NOT NULL,
            ADD CONSTRAINT fk_qc_user_tenant
                FOREIGN KEY (tenant_id)
                REFERENCES qc_tenant(id)
                ON DELETE RESTRICT
        """
    )

    # 任务 owner/tenant 不能由旧的 NULL 值推断为现有管理员。
    op.execute("ALTER TABLE qc_task ADD COLUMN tenant_id uuid")
    op.execute(
        f"""
        UPDATE qc_task
        SET tenant_id = '{DEFAULT_TENANT_ID}'::uuid
        WHERE tenant_id IS NULL
        """
    )
    op.execute(
        f"""
        UPDATE qc_task AS task
        SET submitter_id = '{HISTORY_PRINCIPAL_ID}'::uuid
        WHERE task.submitter_id IS NULL
           OR NOT EXISTS (
                SELECT 1
                FROM qc_user AS user_row
                WHERE user_row.id = task.submitter_id
           )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
            ALTER COLUMN tenant_id SET NOT NULL,
            ALTER COLUMN submitter_id SET NOT NULL,
            ADD CONSTRAINT fk_qc_task_tenant
                FOREIGN KEY (tenant_id)
                REFERENCES qc_tenant(id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT fk_qc_task_submitter
                FOREIGN KEY (submitter_id)
                REFERENCES qc_user(id)
                ON DELETE RESTRICT
        """
    )

    # App 构建同样属于创建者租户，历史空值或悬空用户统一转入禁用主体。
    op.execute("ALTER TABLE qc_platform_build ADD COLUMN tenant_id uuid")
    op.execute(
        f"""
        UPDATE qc_platform_build
        SET tenant_id = '{DEFAULT_TENANT_ID}'::uuid
        WHERE tenant_id IS NULL
        """
    )
    op.execute(
        f"""
        UPDATE qc_platform_build AS build
        SET created_by = '{HISTORY_PRINCIPAL_ID}'::uuid
        WHERE build.created_by IS NULL
           OR NOT EXISTS (
                SELECT 1
                FROM qc_user AS user_row
                WHERE user_row.id = build.created_by
           )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_platform_build
            ALTER COLUMN tenant_id SET NOT NULL,
            ALTER COLUMN created_by SET NOT NULL,
            ADD CONSTRAINT fk_qc_platform_build_tenant
                FOREIGN KEY (tenant_id)
                REFERENCES qc_tenant(id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT fk_qc_platform_build_created_by
                FOREIGN KEY (created_by)
                REFERENCES qc_user(id)
                ON DELETE RESTRICT
        """
    )
    # 旧版 sha256 全局唯一会让不同租户上传相同 APK 时互相阻塞；去重边界改为
    # owner + tenant，与应用层可见性一致。
    op.execute(
        """
        ALTER TABLE qc_platform_build
        DROP CONSTRAINT IF EXISTS qc_platform_build_sha256_key
        """
    )

    op.execute(
        """
        CREATE INDEX idx_qc_user_tenant_status_created
        ON qc_user (tenant_id, status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_task_tenant_submitter_created
        ON qc_task (tenant_id, submitter_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_platform_build_tenant_created_by_created
        ON qc_platform_build (tenant_id, created_by, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_platform_build_owner_sha256
        ON qc_platform_build (tenant_id, created_by, sha256)
        """
    )


def downgrade() -> None:
    """移除租户约束并恢复迁移前的可空 owner 字段。"""

    op.execute("DROP INDEX IF EXISTS uq_qc_platform_build_owner_sha256")
    op.execute("DROP INDEX IF EXISTS idx_qc_platform_build_tenant_created_by_created")
    op.execute("DROP INDEX IF EXISTS idx_qc_task_tenant_submitter_created")
    op.execute("DROP INDEX IF EXISTS idx_qc_user_tenant_status_created")
    op.execute(
        """
        ALTER TABLE qc_platform_build
            DROP CONSTRAINT IF EXISTS fk_qc_platform_build_created_by,
            DROP CONSTRAINT IF EXISTS fk_qc_platform_build_tenant,
            ALTER COLUMN created_by DROP NOT NULL,
            ALTER COLUMN tenant_id DROP NOT NULL,
            DROP COLUMN IF EXISTS tenant_id
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
            DROP CONSTRAINT IF EXISTS fk_qc_task_submitter,
            DROP CONSTRAINT IF EXISTS fk_qc_task_tenant,
            ALTER COLUMN submitter_id DROP NOT NULL,
            ALTER COLUMN tenant_id DROP NOT NULL,
            DROP COLUMN IF EXISTS tenant_id
        """
    )
    op.execute(
        f"""
        UPDATE qc_task
        SET submitter_id = NULL
        WHERE submitter_id = '{HISTORY_PRINCIPAL_ID}'::uuid
        """
    )
    op.execute(
        f"""
        UPDATE qc_platform_build
        SET created_by = NULL
        WHERE created_by = '{HISTORY_PRINCIPAL_ID}'::uuid
        """
    )
    op.execute(
        """
        ALTER TABLE qc_platform_build
        ADD CONSTRAINT qc_platform_build_sha256_key UNIQUE (sha256)
        """
    )
    op.execute(
        """
        ALTER TABLE qc_user
            DROP CONSTRAINT IF EXISTS fk_qc_user_tenant,
            ALTER COLUMN tenant_id DROP NOT NULL,
            DROP COLUMN IF EXISTS tenant_id
        """
    )
    op.execute(
        f"""
        DELETE FROM qc_user
        WHERE id = '{HISTORY_PRINCIPAL_ID}'::uuid
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_tenant")
