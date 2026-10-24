"""流程基建：控制／挂起／采集预约承诺／事件序／预算／观察事件落库。"""

from alembic import op

revision = "20261022_0143"
down_revision = "20261019_0142"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE qc_flow_control (
            phase_run_id text PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            trigger_key text NOT NULL UNIQUE,
            flow_code text NOT NULL,
            definition_hash text NOT NULL,
            control_status text NOT NULL,
            execution_result text NOT NULL,
            evidence_state text NOT NULL,
            resume_result text NOT NULL,
            budget_consumption_id text,
            parent_hold_id text,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE qc_flow_parent_work_hold (
            hold_id text PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            phase_run_id text NOT NULL,
            parent_work_kind text NOT NULL,
            parent_work_id text NOT NULL,
            parent_version integer NOT NULL,
            owner_execution_segment_id text NOT NULL,
            status text NOT NULL,
            frontier_ref text,
            resume_snapshot_ref text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_qc_flow_parent_work_hold_active
        ON qc_flow_parent_work_hold (parent_work_id)
        WHERE status = 'active'
    """)
    op.execute("""
        CREATE TABLE qc_capture_reservation (
            capture_request_id text PRIMARY KEY,
            capture_id text NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            status text NOT NULL,
            ownership jsonb NOT NULL,
            artifact_slots text[] NOT NULL DEFAULT '{}'::text[],
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE qc_artifact_commitment (
            capture_request_id text NOT NULL,
            artifact_slot text NOT NULL,
            capture_id text NOT NULL,
            content_digest text NOT NULL,
            content_length bigint NOT NULL,
            produced_under_fencing_token text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (capture_request_id, artifact_slot)
        )
    """)
    op.execute("""
        CREATE TABLE qc_flow_resource_event_seq (
            resource_id text NOT NULL,
            producer_epoch text NOT NULL,
            next_seq bigint NOT NULL,
            PRIMARY KEY (resource_id, producer_epoch)
        )
    """)
    op.execute("""
        CREATE TABLE qc_flow_budget_reservation (
            consumption_id text PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            phase_run_id text NOT NULL,
            units integer NOT NULL,
            status text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE qc_flow_observation_event (
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            phase_run_id text NOT NULL,
            resource_id text NOT NULL,
            producer_epoch text NOT NULL,
            resource_event_seq bigint NOT NULL,
            sample_index integer NOT NULL,
            capture_id text NOT NULL,
            clock_domain text NOT NULL,
            content_digest text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (resource_id, producer_epoch, resource_event_seq)
        )
    """)
    op.execute("""
        CREATE INDEX ix_qc_flow_control_task_id
        ON qc_flow_control (task_id)
    """)
    op.execute("""
        CREATE INDEX ix_qc_flow_observation_event_task_phase
        ON qc_flow_observation_event (task_id, phase_run_id)
    """)
    op.execute("""
        CREATE INDEX ix_qc_flow_observation_event_capture
        ON qc_flow_observation_event (capture_id)
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS qc_flow_observation_event")
    op.execute("DROP TABLE IF EXISTS qc_flow_budget_reservation")
    op.execute("DROP TABLE IF EXISTS qc_flow_resource_event_seq")
    op.execute("DROP TABLE IF EXISTS qc_artifact_commitment")
    op.execute("DROP TABLE IF EXISTS qc_capture_reservation")
    op.execute("DROP TABLE IF EXISTS qc_flow_parent_work_hold")
    op.execute("DROP TABLE IF EXISTS qc_flow_control")
