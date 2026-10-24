"""将 Observation Packet 幂等身份扩展为证据绑定身份。

Revision ID: 20260831_0081
Revises: 20260826_0080
Create Date: 2026-08-31
"""

from __future__ import annotations

from alembic import op

revision = "20260831_0081"
down_revision = "20260826_0080"
branch_labels = None
depends_on = None

_OLD_INDEX = "uq_qc_observation_packet_run_page_rule"
_NEW_INDEX = "uq_qc_observation_packet_run_page_rule_input"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE qc_observation_packet "
        "ADD COLUMN input_fingerprint varchar(64)"
    )
    op.execute(
        "UPDATE qc_observation_packet "
        "SET input_fingerprint = 'legacy:' || md5(id::text || ':' || created_at::text) "
        "WHERE input_fingerprint IS NULL"
    )
    op.execute(f"DROP INDEX IF EXISTS {_OLD_INDEX}")
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_NEW_INDEX}
        ON qc_observation_packet (
            created_by_agent_run_id,
            page_capture_id,
            rule_id,
            input_fingerprint
        )
        WHERE created_by_agent_run_id IS NOT NULL
          AND page_capture_id IS NOT NULL
          AND rule_id IS NOT NULL
          AND input_fingerprint IS NOT NULL
        """
    )
    op.execute(
        "COMMENT ON COLUMN qc_observation_packet.input_fingerprint IS "
        "'观察包证据绑定指纹，用于区分同一运行、页面和规则下的不同证据输入集合'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_NEW_INDEX}")
    op.execute(
        """
        DELETE FROM qc_observation_packet AS packet
        USING qc_observation_packet AS duplicate
        WHERE packet.created_by_agent_run_id = duplicate.created_by_agent_run_id
          AND packet.page_capture_id = duplicate.page_capture_id
          AND packet.rule_id = duplicate.rule_id
          AND packet.created_at > duplicate.created_at
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_OLD_INDEX}
        ON qc_observation_packet (
            created_by_agent_run_id,
            page_capture_id,
            rule_id
        )
        WHERE created_by_agent_run_id IS NOT NULL
          AND page_capture_id IS NOT NULL
          AND rule_id IS NOT NULL
        """
    )
    op.execute(
        "ALTER TABLE qc_observation_packet DROP COLUMN input_fingerprint"
    )
