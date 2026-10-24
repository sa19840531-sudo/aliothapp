"""qc_rule_execution 增加规则级 quality_outcome 五态生成列。

Revision ID: 20260804_0037
Revises: 20260803_0032
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "20260804_0037"
down_revision = "20260804_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 五态映射由生成列锁死：写入方只能提供 execution_status，
    # quality_outcome 不可直写，物理上杜绝两轴组合漂移。
    op.execute(
        """
        DO $$
        DECLARE
            existing_column record;
        BEGIN
            SELECT
                is_generated,
                generation_expression,
                is_nullable,
                data_type,
                character_maximum_length
            INTO existing_column
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'qc_rule_execution'
              AND column_name = 'quality_outcome';

            IF NOT FOUND THEN
                ALTER TABLE qc_rule_execution
                ADD COLUMN quality_outcome varchar(32) GENERATED ALWAYS AS (
                    CASE execution_status
                        WHEN 'pass' THEN 'passed'
                        WHEN 'fail' THEN 'failed'
                        WHEN 'warning' THEN 'failed'
                        WHEN 'manual_review' THEN 'inconclusive'
                        WHEN 'error' THEN 'inconclusive'
                        WHEN 'evidence_insufficient' THEN 'evidence_gap'
                        WHEN 'skipped' THEN 'skipped'
                    END
                ) STORED NOT NULL;
            ELSIF existing_column.is_generated <> 'ALWAYS'
                OR existing_column.is_nullable <> 'NO'
                OR existing_column.data_type <> 'character varying'
                OR existing_column.character_maximum_length <> 32
                OR existing_column.generation_expression NOT LIKE '%pass%passed%'
                OR existing_column.generation_expression NOT LIKE '%fail%failed%'
                OR existing_column.generation_expression NOT LIKE '%warning%failed%'
                OR existing_column.generation_expression NOT LIKE '%manual_review%inconclusive%'
                OR existing_column.generation_expression NOT LIKE '%error%inconclusive%'
                OR existing_column.generation_expression NOT LIKE '%evidence_insufficient%evidence_gap%'
                OR existing_column.generation_expression NOT LIKE '%skipped%skipped%'
            THEN
                RAISE EXCEPTION
                    'qc_rule_execution.quality_outcome 已存在，但定义与 20260804_0037 不一致';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule_execution.quality_outcome IS
        '规则级质检结论五态（passed/failed/skipped/inconclusive/evidence_gap），由 execution_status 派生，禁止直写'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE qc_rule_execution DROP COLUMN IF EXISTS quality_outcome")
