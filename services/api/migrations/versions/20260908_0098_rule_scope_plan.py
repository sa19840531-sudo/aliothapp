"""为聚合规则候选持久化冻结适用范围计划。

Revision ID: 20260908_0098
Revises: 20260907_0097
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import op

revision = "20260908_0098"
down_revision = "20260907_0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可审计的规则范围计划，不改写历史候选。"""

    op.execute(
        """
        ALTER TABLE qc_aggregate_inspection_candidate
        ADD COLUMN rule_scope_plan_hash varchar(128),
        ADD COLUMN rule_scope_plan jsonb,
        ADD CONSTRAINT ck_qc_aggregate_candidate_rule_scope_plan
        CHECK (
            (rule_scope_plan_hash IS NULL AND rule_scope_plan IS NULL)
            OR (
                rule_scope_plan_hash ~ '^sha256:[0-9a-f]{64}$'
                AND jsonb_typeof(rule_scope_plan) = 'object'
                AND rule_scope_plan->>'protocol_version' = 'rule-scope-plan.v1'
                AND rule_scope_plan->>'plan_hash' = rule_scope_plan_hash
                AND rule_scope_plan->>'task_id' = task_id::text
                AND rule_scope_plan->>'source_agent_run_id'
                    = source_agent_run_id::text
                AND (rule_scope_plan->>'finalization_epoch')::integer
                    = finalization_epoch
                AND rule_scope_plan->>'rule_id' = rule_id::text
                AND rule_scope_plan->>'status' IN (
                    'applicable', 'not_applicable', 'unproven'
                )
            )
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_aggregate_inspection_candidate.rule_scope_plan_hash IS
        '同一 Task/Run/Epoch 下规则适用范围计划的稳定 SHA-256'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_aggregate_inspection_candidate.rule_scope_plan IS
        '规则适用、不适用或未证明结论及其不可变权威来源'
        """
    )


def downgrade() -> None:
    """移除规则范围计划字段，不删除聚合候选。"""

    op.execute(
        """
        ALTER TABLE qc_aggregate_inspection_candidate
        DROP CONSTRAINT IF EXISTS ck_qc_aggregate_candidate_rule_scope_plan,
        DROP COLUMN IF EXISTS rule_scope_plan,
        DROP COLUMN IF EXISTS rule_scope_plan_hash
        """
    )
