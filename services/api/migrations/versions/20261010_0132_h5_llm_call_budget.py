"""H5 跨消费者物理调用冻结准入账本。"""

from alembic import op

revision = "20261010_0132"
down_revision = "20261008_0131"
branch_labels = None
depends_on = None

CREATE_BUDGET_TABLE = """CREATE TABLE qc_h5_llm_call_budget (
    task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
    finalization_epoch integer NOT NULL,
    agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE CASCADE,
    max_calls integer NOT NULL CHECK (max_calls > 0),
    admitted_calls bigint NOT NULL DEFAULT 0 CHECK (admitted_calls >= 0),
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, finalization_epoch)
)"""


def upgrade() -> None:
    op.execute(CREATE_BUDGET_TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE qc_h5_llm_call_budget")
