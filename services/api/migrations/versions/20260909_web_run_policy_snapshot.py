"""Web Run 完整策略的首次冻结与数据库不可覆盖约束。"""

from alembic import op

revision = "20260909_0100"
down_revision = "20260908_0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE qc_web_run_policy_snapshot (
            agent_run_id uuid PRIMARY KEY,
            task_id uuid NOT NULL,
            payload jsonb NOT NULL,
            payload_sha256 varchar(64) NOT NULL,
            frozen_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_web_run_policy_owner FOREIGN KEY (task_id, agent_run_id)
                REFERENCES qc_agent_run(task_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_web_run_policy_body CHECK (jsonb_typeof(payload) = 'object'
                AND octet_length(payload::text) <= 1048576),
            CONSTRAINT ck_web_run_policy_digest CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE FUNCTION reject_web_run_policy_update() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Web Run policy is immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER web_run_policy_immutable BEFORE UPDATE ON qc_web_run_policy_snapshot
        FOR EACH ROW EXECUTE FUNCTION reject_web_run_policy_update()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE qc_web_run_policy_snapshot")
    op.execute("DROP FUNCTION reject_web_run_policy_update()")
