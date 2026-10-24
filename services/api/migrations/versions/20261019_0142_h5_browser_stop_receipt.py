"""独立保存原浏览器停止回执，不回填历史lost或修改证据。"""

from alembic import op

revision = "20261019_0142"
down_revision = "20261017_0141"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE qc_h5_browser_stop_receipt (
            session_id text PRIMARY KEY REFERENCES qc_surface_session(session_id) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            owner_agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id),
            finalization_epoch integer NOT NULL,
            command_sequence bigint NOT NULL,
            resource_id uuid NOT NULL UNIQUE,
            proof jsonb NOT NULL,
            persisted_at timestamptz NOT NULL DEFAULT now(),
            CHECK (COALESCE(jsonb_typeof(proof)='object'
                AND proof->>'session_id'=session_id
                AND proof->>'resource_id'=resource_id::text
                AND proof->>'method'='owned_browser_close_ack_and_pid_absent'
                AND (proof->>'browser_pid') ~ '^[1-9][0-9]*$'
                AND proof->'driver_stop_ack'='true'::jsonb, false))
        );
    """)
    op.execute("""
        CREATE FUNCTION qc_h5_stop_receipt_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'H5_STOP_RECEIPT_IMMUTABLE' USING ERRCODE='23514'; END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_h5_stop_receipt_immutable BEFORE UPDATE
        ON qc_h5_browser_stop_receipt FOR EACH ROW EXECUTE FUNCTION qc_h5_stop_receipt_immutable();
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM qc_h5_browser_stop_receipt) THEN
                RAISE EXCEPTION 'H5_STOP_RECEIPT_DOWNGRADE_BLOCKED' USING ERRCODE='23514';
            END IF;
        END $$;
    """)
    op.execute("DROP TABLE qc_h5_browser_stop_receipt")
    op.execute("DROP FUNCTION qc_h5_stop_receipt_immutable()")
