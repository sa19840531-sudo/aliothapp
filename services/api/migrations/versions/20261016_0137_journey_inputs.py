"""封存检查点到规则 InputSet 的权威选择，不回填历史证据。"""

from alembic import op

revision = "20261016_0137"
down_revision = "20261016_0136"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE qc_journey_input_contract (
        input_set_id varchar(96) PRIMARY KEY REFERENCES qc_evidence_input_set ON DELETE CASCADE,
        task_id uuid NOT NULL REFERENCES qc_task ON DELETE CASCADE,
        agent_run_id uuid NOT NULL REFERENCES qc_agent_run,
        finalization_epoch integer NOT NULL CHECK(finalization_epoch>=0),
        execution_plan_id varchar(96) NOT NULL REFERENCES qc_journey_execution_plan,
        rule_contract_hash varchar(64) NOT NULL,
        selection_hash varchar(64) NOT NULL,
        contract_payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TRIGGER trg_journey_input_epoch BEFORE INSERT
        ON qc_journey_input_contract FOR EACH ROW EXECUTE FUNCTION qc_guard_v2_finalization_write()""")
    op.execute("""CREATE TRIGGER trg_journey_input_immutable BEFORE UPDATE
        ON qc_journey_input_contract FOR EACH ROW EXECUTE FUNCTION qc_checkpoint_binding_immutable()""")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM qc_journey_input_contract)
        THEN RAISE EXCEPTION '流程规则输入已存在，禁止回滚抹去'; END IF; END $$""")
    op.execute("DROP TABLE qc_journey_input_contract")
