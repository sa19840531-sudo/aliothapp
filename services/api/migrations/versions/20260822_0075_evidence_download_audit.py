"""增加证据下载的不可变访问审计。

Revision ID: 20260822_0075
Revises: 20260820_0074
Create Date: 2026-08-22

下载票据只在签发时校验 owner/tenant，兑现时的放行与拒绝此前都没有留痕。
本表按次记录下载主体、租户、证据、票据与结果，供撤权核查与外发追溯。
故意不对 artifact_id 和 actor_user_id 建外键：审计必须比证据与账号活得久，
且账号已被删除时的拒绝记录恰恰是最需要留下的那一条。
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0075"
down_revision = "20260820_0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存每次证据文件读取的主体、票据与放行结果。"""

    op.execute(
        """
        CREATE TABLE qc_evidence_download_audit (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            artifact_id varchar(64) NOT NULL,
            actor_user_id varchar(64) NOT NULL,
            tenant_id varchar(64) NOT NULL,
            ticket_id varchar(64) NOT NULL,
            result varchar(16) NOT NULL,
            denied_reason varchar(64),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_evidence_download_result CHECK (
                result IN ('allowed', 'denied')
            ),
            CONSTRAINT ck_qc_evidence_download_denied_reason CHECK (
                (result = 'denied' AND denied_reason IS NOT NULL)
                OR (result = 'allowed' AND denied_reason IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_evidence_download_audit IS
        '证据下载访问审计表，按次记录票据兑现的主体、证据与放行结果'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_evidence_download_audit.ticket_id IS
        '下载票据 jti，用于把一次外发追溯到具体票据'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_evidence_download_audit.denied_reason IS
        '拒绝原因代码：subject_revoked 主体已停用或换租户，object_access_revoked 对象权限已收回'
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_download_artifact
        ON qc_evidence_download_audit (artifact_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_download_actor
        ON qc_evidence_download_audit (actor_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_reject_evidence_download_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'qc_evidence_download_audit is immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_evidence_download_audit_immutable
        BEFORE UPDATE OR DELETE ON qc_evidence_download_audit
        FOR EACH ROW EXECUTE FUNCTION qc_reject_evidence_download_audit_mutation()
        """
    )


def downgrade() -> None:
    """存在下载审计时拒绝破坏性降级。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_evidence_download_audit LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot downgrade evidence download audit while audit rows exist';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_evidence_download_audit_immutable "
        "ON qc_evidence_download_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_reject_evidence_download_audit_mutation()")
    op.execute("DROP INDEX IF EXISTS idx_qc_evidence_download_artifact")
    op.execute("DROP INDEX IF EXISTS idx_qc_evidence_download_actor")
    op.execute("DROP TABLE qc_evidence_download_audit")
