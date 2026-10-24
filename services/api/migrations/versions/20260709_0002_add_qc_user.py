"""新增后台用户表。

Revision ID: 20260709_0002
Revises: 20260706_0001
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op

revision = "20260709_0002"
down_revision = "20260706_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_user (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            username varchar(64) NOT NULL,
            full_name varchar(128) NOT NULL,
            password_hash text NOT NULL,
            role varchar(32) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'enabled',
            last_login_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_user_username UNIQUE (username),
            CONSTRAINT ck_qc_user_role CHECK (
                role IN ('平台管理员', '审计员', '规则配置员')
            ),
            CONSTRAINT ck_qc_user_status CHECK (
                status IN ('enabled', 'disabled')
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_user_status_created
        ON qc_user (status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_user_updated_at
        BEFORE UPDATE ON qc_user
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        INSERT INTO qc_user (username, full_name, password_hash, role, status)
        VALUES ('admin', '系统管理员', 'alioth123', '平台管理员', 'enabled')
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_qc_user_updated_at ON qc_user")
    op.execute("DROP INDEX IF EXISTS idx_qc_user_status_created")
    op.execute("DROP TABLE IF EXISTS qc_user")
