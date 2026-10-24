"""保存检查点原尝试与精确动作证据，禁止历史补写和回滚丢失。"""

from alembic import op

revision = "20261016_0136"
down_revision = "20261016_0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE qc_journey_checkpoint_attempt (
        attempt_id varchar(96) PRIMARY KEY,
        execution_plan_id varchar(96) NOT NULL REFERENCES qc_journey_execution_plan,
        checkpoint_id varchar(128) NOT NULL,
        task_id uuid NOT NULL REFERENCES qc_task ON DELETE CASCADE,
        agent_run_id uuid NOT NULL REFERENCES qc_agent_run,
        session_id varchar(128) NOT NULL,
        finalization_epoch integer NOT NULL CHECK (finalization_epoch>=0),
        work_id varchar(96) NOT NULL UNIQUE REFERENCES qc_v2_work_item ON DELETE CASCADE,
        source_capture_id uuid NOT NULL REFERENCES qc_capture DEFERRABLE INITIALLY DEFERRED,
        intent_id varchar(128) NOT NULL,
        intent_payload jsonb NOT NULL,
        work_payload jsonb NOT NULL,
        status varchar(32) NOT NULL CHECK (status IN ('reserved','bound','gap')),
        observation_status varchar(32) NOT NULL DEFAULT 'not_evaluated'
            CHECK (observation_status IN ('not_evaluated','observed','needs_review')),
        turn_payload jsonb,
        reason text,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(session_id,intent_id),
        FOREIGN KEY (agent_run_id,task_id) REFERENCES qc_agent_run(id,task_id),
        FOREIGN KEY (task_id,session_id) REFERENCES qc_surface_session(task_id,session_id)
    )""")
    op.execute("""CREATE TABLE qc_journey_checkpoint_evidence (
        attempt_id varchar(96) NOT NULL REFERENCES qc_journey_checkpoint_attempt ON DELETE CASCADE,
        role varchar(32) NOT NULL CHECK(role IN ('before_action','after_action')),
        capture_id uuid NOT NULL REFERENCES qc_capture DEFERRABLE INITIALLY DEFERRED,
        action_execution_id varchar(96) NOT NULL REFERENCES qc_action_execution
            DEFERRABLE INITIALLY DEFERRED,
        manifest_id varchar(96) NOT NULL REFERENCES qc_evidence_manifest
            DEFERRABLE INITIALLY DEFERRED,
        manifest_version integer NOT NULL CHECK(manifest_version>0),
        binding_payload jsonb NOT NULL,
        PRIMARY KEY(attempt_id,role), UNIQUE(attempt_id,capture_id)
    )""")
    op.execute("""CREATE TABLE qc_journey_checkpoint_observation (
        observation_id varchar(96) PRIMARY KEY,
        attempt_id varchar(96) NOT NULL REFERENCES qc_journey_checkpoint_attempt ON DELETE CASCADE,
        task_id uuid NOT NULL REFERENCES qc_task ON DELETE CASCADE,
        finalization_epoch integer NOT NULL,
        observed boolean NOT NULL,
        bindings jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TRIGGER trg_checkpoint_observation_epoch BEFORE INSERT
        ON qc_journey_checkpoint_observation FOR EACH ROW
        EXECUTE FUNCTION qc_guard_v2_finalization_write()""")
    op.execute("""CREATE TRIGGER trg_checkpoint_attempt_epoch BEFORE INSERT OR UPDATE
        ON qc_journey_checkpoint_attempt FOR EACH ROW
        EXECUTE FUNCTION qc_guard_v2_finalization_write()""")
    op.execute("""CREATE FUNCTION qc_checkpoint_attempt_identity_guard() RETURNS trigger AS $$
        BEGIN
            IF ROW(NEW.attempt_id,NEW.execution_plan_id,NEW.checkpoint_id,NEW.task_id,
                NEW.agent_run_id,NEW.session_id,NEW.finalization_epoch,NEW.work_id,
                NEW.source_capture_id,NEW.intent_id,NEW.intent_payload,NEW.work_payload)
                IS DISTINCT FROM ROW(OLD.attempt_id,OLD.execution_plan_id,OLD.checkpoint_id,OLD.task_id,
                OLD.agent_run_id,OLD.session_id,OLD.finalization_epoch,OLD.work_id,
                OLD.source_capture_id,OLD.intent_id,OLD.intent_payload,OLD.work_payload)
                OR OLD.status<>'reserved'
            THEN RAISE EXCEPTION '检查点尝试已冻结，禁止覆盖身份或绑定'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_checkpoint_attempt_identity BEFORE UPDATE
        ON qc_journey_checkpoint_attempt FOR EACH ROW
        EXECUTE FUNCTION qc_checkpoint_attempt_identity_guard()""")
    op.execute("""CREATE FUNCTION qc_checkpoint_binding_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION '检查点证据和观察记录不可覆盖'; END;
        $$ LANGUAGE plpgsql""")
    for table in ("qc_journey_checkpoint_evidence", "qc_journey_checkpoint_observation"):
        op.execute(f"""CREATE TRIGGER trg_checkpoint_binding_immutable BEFORE UPDATE
            ON {table} FOR EACH ROW EXECUTE FUNCTION qc_checkpoint_binding_immutable()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM qc_journey_checkpoint_attempt)
        THEN RAISE EXCEPTION '检查点尝试证据已存在，禁止回滚抹去'; END IF;
    END $$""")
    op.execute("DROP TABLE qc_journey_checkpoint_observation")
    op.execute("DROP TABLE qc_journey_checkpoint_evidence")
    op.execute("DROP TABLE qc_journey_checkpoint_attempt")
    op.execute("DROP FUNCTION qc_checkpoint_attempt_identity_guard()")
    op.execute("DROP FUNCTION qc_checkpoint_binding_immutable()")
