"""补采证据挂上重新封存的输入集合：补采 → EvidenceInputSet → Attempt → RuleExecution 可追溯。

Revision ID: 20260911_0109
Revises: 20260911_0108
Create Date: 2026-09-11

09-11 审查 P1-10：补采成功后重判只换了 Attempt 输入哈希，没有形成新的不可变输入集合。
工作项被领取、准备重判时为已接上的补采封一个输入集合，并记在补采行上；上线前已接上的补采
在下一次领取时补封。
"""

from __future__ import annotations

from alembic import op

revision = "20260911_0109"
down_revision = "20260911_0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_supplement
        ADD COLUMN IF NOT EXISTS evidence_input_set_id varchar(96),
        ADD CONSTRAINT fk_qc_evidence_supplement_input_set
            FOREIGN KEY (task_id, evidence_input_set_id)
            REFERENCES qc_evidence_input_set(task_id, input_set_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_evidence_supplement.evidence_input_set_id IS
        '补采接上后按规则重新封存的输入集合；重判的 Attempt 与规则执行都指回它'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_supplement
        DROP CONSTRAINT IF EXISTS fk_qc_evidence_supplement_input_set,
        DROP COLUMN IF EXISTS evidence_input_set_id
        """
    )
