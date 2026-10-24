"""持久化 Manifest Read 影子对账，作为 authoritative 切主凭证。

Revision ID: 20260825_0078
Revises: 20260824_0077
Create Date: 2026-08-25

E24：请求 on 必须基于跨进程可审计的零差异样本，禁止只依赖进程内存 sink。
"""

from __future__ import annotations

from alembic import op

revision = "20260825_0078"
down_revision = "20260824_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_manifest_shadow_comparison (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL,
            capture_id varchar(160) NOT NULL,
            outcome varchar(64) NOT NULL,
            compared_at timestamptz NOT NULL,
            legacy_artifact_count integer NOT NULL DEFAULT 0,
            manifest_artifact_count integer NOT NULL DEFAULT 0,
            difference_count integer NOT NULL DEFAULT 0,
            missing_in_manifest jsonb NOT NULL DEFAULT '[]'::jsonb,
            unexpected_in_manifest jsonb NOT NULL DEFAULT '[]'::jsonb,
            mismatched_size jsonb NOT NULL DEFAULT '[]'::jsonb,
            reason text,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_manifest_shadow_outcome CHECK (
                outcome IN (
                    'matched',
                    'mismatched',
                    'manifest_read_failed',
                    'skipped'
                )
            ),
            CONSTRAINT ck_qc_manifest_shadow_counts CHECK (
                legacy_artifact_count >= 0
                AND manifest_artifact_count >= 0
                AND difference_count >= 0
            ),
            CONSTRAINT ck_qc_manifest_shadow_json CHECK (
                jsonb_typeof(missing_in_manifest) = 'array'
                AND jsonb_typeof(unexpected_in_manifest) = 'array'
                AND jsonb_typeof(mismatched_size) = 'array'
                AND jsonb_typeof(payload) = 'object'
            ),
            CONSTRAINT uq_qc_manifest_shadow_capture
                UNIQUE (task_id, capture_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_manifest_shadow_compared
            ON qc_manifest_shadow_comparison (compared_at DESC, outcome)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_manifest_shadow_comparison")
