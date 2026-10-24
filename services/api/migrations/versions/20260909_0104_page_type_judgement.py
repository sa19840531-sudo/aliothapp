"""页面类型判断记录：多份判断各留依据、冲突标未知、改判只追加。

Revision ID: 20260909_0104
Revises: 20260909_0103
Create Date: 2026-09-09

排期「实现页面类型冲突与改判处理」（04 专项 §5.6、§17.3）。识别写库仍只保留一个正式页型，
这张表按页面追加每一份判断（结构规则、模型、网页评估等各自一条），后来的改判追加为
revision 并指回被改的那条；裁定在读侧算（一致→确认，不一致→未知并保留冲突），不改页面
身份，也不动已冻结的必检范围。
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0104"
down_revision = "20260909_0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建判断记录表与查询索引。"""

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_page_type_judgement (
            judgement_id varchar(160) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid,
            page_capture_id uuid NOT NULL REFERENCES qc_capture(id) ON DELETE CASCADE,
            sequence_no integer NOT NULL,
            source varchar(64) NOT NULL,
            page_type varchar(128),
            page_state varchar(256),
            confidence double precision,
            catalog_version varchar(64),
            kind varchar(16) NOT NULL DEFAULT 'judgement',
            revises_judgement_id varchar(160)
                REFERENCES qc_page_type_judgement(judgement_id) ON DELETE SET NULL,
            basis jsonb NOT NULL DEFAULT '{}'::jsonb,
            basis_hash varchar(128) NOT NULL,
            task_finalization_state varchar(32),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_page_type_judgement_sequence CHECK (sequence_no > 0),
            CONSTRAINT ck_qc_page_type_judgement_kind CHECK (kind IN ('judgement', 'revision')),
            CONSTRAINT ck_qc_page_type_judgement_revision CHECK (
                (kind = 'revision') = (revises_judgement_id IS NOT NULL)
            ),
            CONSTRAINT ck_qc_page_type_judgement_basis CHECK (jsonb_typeof(basis) = 'object'),
            CONSTRAINT uq_qc_page_type_judgement_basis UNIQUE (page_capture_id, source, basis_hash),
            CONSTRAINT uq_qc_page_type_judgement_sequence UNIQUE (page_capture_id, sequence_no)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_qc_page_type_judgement_task_capture
            ON qc_page_type_judgement (task_id, page_capture_id, sequence_no)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_page_type_judgement IS
            '页面类型判断记录（只追加）：每份判断各留来源与依据，改判指回被改的判断；裁定在读侧算'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_page_type_judgement")
