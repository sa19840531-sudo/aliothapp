"""检查点执行计划按 Run、流程、场景冻结，保留旧任务级计划与所有证据。"""

from alembic import op

revision = "20261016_0135"
down_revision = "20261014_0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE qc_journey_execution_plan
            ADD COLUMN agent_run_id uuid,
            ADD COLUMN scenario_key varchar(128)
    """)
    op.execute("ALTER TABLE qc_agent_run ADD CONSTRAINT uq_journey_run_task UNIQUE (id, task_id)")
    op.execute("""
        ALTER TABLE qc_journey_execution_plan
            ADD CONSTRAINT fk_journey_plan_run_task FOREIGN KEY (agent_run_id, task_id)
                REFERENCES qc_agent_run(id, task_id) ON DELETE RESTRICT,
            ADD CONSTRAINT ck_journey_plan_scope CHECK (
                (schema_version='journey-execution-plan.v1'
                    AND agent_run_id IS NULL AND scenario_key IS NULL)
                OR (schema_version='journey-execution-plan.v2'
                    AND agent_run_id IS NOT NULL AND scenario_key IS NOT NULL
                    AND length(btrim(scenario_key)) > 0
                    AND scenario_key=btrim(scenario_key)
                    AND COALESCE(plan_payload->>'schema_version'=schema_version
                        AND plan_payload->>'agent_run_id'=agent_run_id::text
                        AND plan_payload->>'task_id'=task_id::text
                        AND plan_payload->>'scenario_key'=scenario_key
                        AND plan_payload->>'execution_plan_id'=execution_plan_id, false))
            )
    """)
    op.execute("""
        ALTER TABLE qc_journey_execution_plan
            DROP CONSTRAINT uq_qc_journey_execution_plan_task
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_journey_legacy_task ON qc_journey_execution_plan(task_id)
            WHERE agent_run_id IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_journey_run_scenario ON qc_journey_execution_plan
            (task_id, agent_run_id, definition_id, scenario_key) WHERE agent_run_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM qc_journey_execution_plan WHERE agent_run_id IS NOT NULL)
            THEN RAISE EXCEPTION 'Run 检查点计划已存在，禁止回滚丢失流程归属'; END IF;
        END $$
    """)
    op.execute("DROP INDEX uq_journey_run_scenario")
    op.execute("DROP INDEX uq_journey_legacy_task")
    op.execute("""
        ALTER TABLE qc_journey_execution_plan
            ADD CONSTRAINT uq_qc_journey_execution_plan_task UNIQUE (task_id),
            DROP CONSTRAINT ck_journey_plan_scope,
            DROP CONSTRAINT fk_journey_plan_run_task,
            DROP COLUMN scenario_key, DROP COLUMN agent_run_id
    """)
    op.execute("ALTER TABLE qc_agent_run DROP CONSTRAINT uq_journey_run_task")
