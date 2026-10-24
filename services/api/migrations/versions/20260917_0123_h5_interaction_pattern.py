"""H5 自适应交互模式只在跨任务成功验证后晋升为可信加速知识。

Revision ID: 20260917_0123
Revises: 20260917_0122
Create Date: 2026-09-17
"""

from __future__ import annotations

from alembic import op

revision = "20260917_0123"
down_revision = "20260917_0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_h5_interaction_pattern (
            pattern_signature varchar(64) PRIMARY KEY,
            pattern_kind varchar(32) NOT NULL,
            pattern_payload jsonb NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'candidate',
            success_count integer NOT NULL DEFAULT 0,
            failure_count integer NOT NULL DEFAULT 0,
            distinct_success_task_count integer NOT NULL DEFAULT 0,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            last_verified_at timestamptz,
            last_failure_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_h5_interaction_pattern_kind
                CHECK (pattern_kind IN ('bottom_navigation')),
            CONSTRAINT ck_qc_h5_interaction_pattern_status
                CHECK (status IN ('candidate', 'shadow', 'trusted', 'quarantined', 'deprecated')),
            CONSTRAINT ck_qc_h5_interaction_pattern_counts
                CHECK (
                    success_count >= 0
                    AND failure_count >= 0
                    AND distinct_success_task_count >= 0
                )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_h5_interaction_pattern_observation (
            id bigserial PRIMARY KEY,
            pattern_signature varchar(64) NOT NULL
                REFERENCES qc_h5_interaction_pattern(pattern_signature) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            page_capture_id uuid NOT NULL,
            outcome varchar(16) NOT NULL,
            reason_code varchar(128),
            observed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_h5_interaction_pattern_observation_outcome
                CHECK (outcome IN ('success', 'failure')),
            CONSTRAINT fk_qc_h5_interaction_pattern_observation_capture
                FOREIGN KEY (task_id, page_capture_id)
                REFERENCES qc_page_capture(task_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_qc_h5_interaction_pattern_observation
                UNIQUE (pattern_signature, task_id, page_capture_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_qc_h5_interaction_pattern_status
        ON qc_h5_interaction_pattern(status, updated_at DESC)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_h5_interaction_pattern IS
        'H5 可迁移交互结构知识；不保存可直接执行的历史 CSS selector'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_h5_interaction_pattern_observation IS
        'H5 模式在具体任务和 Capture 上的成功或失败证明，用于晋升和隔离'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_h5_interaction_pattern_observation")
    op.execute("DROP TABLE IF EXISTS qc_h5_interaction_pattern")
