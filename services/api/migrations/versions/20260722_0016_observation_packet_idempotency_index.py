"""补齐 Observation Packet 幂等写入所需的唯一索引。

Revision ID: 20260722_0016
Revises: 20260721_0015
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op

revision = "20260722_0016"
down_revision = "20260721_0015"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_qc_observation_packet_run_page_rule"
_INDEX_PREDICATE = (
    "created_by_agent_run_id IS NOT NULL "
    "AND page_capture_id IS NOT NULL "
    "AND rule_id IS NOT NULL"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME}
        ON qc_observation_packet (
            created_by_agent_run_id,
            page_capture_id,
            rule_id
        )
        WHERE {_INDEX_PREDICATE}
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
