"""在不可变报告绑定中持久化 sealed 报告正文。

Revision ID: 20260807_0043
Revises: 20260807_0041, 20260807_0042
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "20260807_0043"
down_revision = ("20260807_0041", "20260807_0042")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_report_payload_snapshot (
            report_id varchar(128) NOT NULL,
            report_version integer NOT NULL,
            report_payload_hash varchar(128) NOT NULL,
            report_payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (report_id, report_version),
            CONSTRAINT fk_qc_report_payload_binding FOREIGN KEY (
                report_id, report_version
            ) REFERENCES qc_report_gate_binding (
                report_id, report_version
            ) ON DELETE RESTRICT,
            CONSTRAINT ck_qc_report_payload_object CHECK (
                jsonb_typeof(report_payload) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_report_payload_binding()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM qc_report_gate_binding AS binding
                WHERE binding.report_id = NEW.report_id
                  AND binding.report_version = NEW.report_version
                  AND binding.report_payload_hash = NEW.report_payload_hash
            ) THEN
                RAISE EXCEPTION '报告正文 hash 与 Gate 绑定不一致';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_report_payload_binding
        BEFORE INSERT ON qc_report_payload_snapshot
        FOR EACH ROW EXECUTE FUNCTION qc_guard_report_payload_binding()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_report_payload_snapshot_immutable
        BEFORE UPDATE OR DELETE ON qc_report_payload_snapshot
        FOR EACH ROW EXECUTE FUNCTION qc_guard_immutable_audit_record()
        """
    )
    op.execute(
        """
        UPDATE qc_task AS task
        SET finalization_agent_run_id = run.id
        FROM qc_report_gate_binding AS binding
        INNER JOIN qc_agent_run AS run
            ON binding.report_id = concat(
                'task:', run.task_id::text, ':run:', run.id::text
            )
        WHERE task.id = binding.task_id
          AND run.task_id = task.id
          AND task.finalization_state = 'sealed'
          AND task.completion_gate_snapshot_id = binding.gate_snapshot_id
          AND task.finalization_epoch = binding.finalization_epoch
          AND task.finalization_agent_run_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_report_payload_snapshot_immutable "
        "ON qc_report_payload_snapshot"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_report_payload_binding "
        "ON qc_report_payload_snapshot"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_report_payload_binding()")
    op.execute("DROP TABLE qc_report_payload_snapshot")