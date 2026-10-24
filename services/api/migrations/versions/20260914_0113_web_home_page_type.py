"""新增五类 Web 覆盖协议并约束新首页基线。

Revision ID: 20260914_0113
Revises: 20260912_0112
"""

from alembic import op

revision = "20260914_0113"
down_revision = "20260912_0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史快照和既有首页记录保留原值，新首页必须签发 home。"""
    op.execute("""
        ALTER TABLE qc_web_coverage_snapshot
        DROP CONSTRAINT ck_qc_web_coverage_snapshot_normalization,
        ADD CONSTRAINT ck_qc_web_coverage_snapshot_normalization CHECK (
            normalization_version IN (
                'web-page-type-normalization.v1',
                'web-page-type-assessment.v1',
                'web-page-type-assessment.v2',
                'web-page-type-assessment.v3'
            )
        )
    """)
    op.execute("""
        CREATE FUNCTION qc_web_require_formal_home_type() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status = 'home_baseline'
               AND NEW.status = OLD.status
               AND NEW.menu_page_type IS NOT DISTINCT FROM OLD.menu_page_type THEN
                RETURN NEW;
            END IF;
            IF NEW.status = 'home_baseline'
               AND NEW.menu_page_type IS DISTINCT FROM 'home'
               AND NOT EXISTS (
                   SELECT 1 FROM qc_web_run_policy_snapshot AS policy
                   WHERE policy.agent_run_id = NEW.agent_run_id
                     AND policy.task_id = NEW.task_id
                     AND NOT (policy.payload->'page_catalog'->'page_types'
                         @> '[{"code":"home"}]'::jsonb)
               ) THEN
                RAISE EXCEPTION 'Web home_baseline requires formal home page_type';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_qc_web_require_formal_home_type
        BEFORE INSERT OR UPDATE ON qc_web_menu_ledger_entry
        FOR EACH ROW EXECUTE FUNCTION qc_web_require_formal_home_type()
    """)


def downgrade() -> None:
    """五类快照不可降回四类，存在新协议数据时拒绝破坏性降级。"""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM qc_web_coverage_snapshot
                       WHERE normalization_version = 'web-page-type-assessment.v3')
               OR EXISTS (SELECT 1 FROM qc_h5_page_type_assessment
                          WHERE schema_version = 'web-page-type-assessment.v3') THEN
                RAISE EXCEPTION 'Cannot downgrade immutable Web v3 evidence';
            END IF;
        END $$
    """)
    op.execute(
        "DROP TRIGGER trg_qc_web_require_formal_home_type ON qc_web_menu_ledger_entry"
    )
    op.execute("DROP FUNCTION qc_web_require_formal_home_type()")
