"""支持 Web 不限菜单预算与整数执行槽位。"""

from __future__ import annotations

from alembic import op

revision = "20260914_0117"
down_revision = "20260914_0116"
branch_labels = None
depends_on = None


def _replace_slot_guard(*, allow_unbounded: bool) -> None:
    invalid_limit = (
        "NOT FOUND OR (configured_menu_limit IS NOT NULL "
        "AND NEW.execution_slot > configured_menu_limit)"
        if allow_unbounded else
        "NOT FOUND OR configured_menu_limit IS NULL "
        "OR NEW.execution_slot > configured_menu_limit"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION qc_enforce_web_menu_execution_slot()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            configured_menu_limit integer;
        BEGIN
            IF NEW.execution_slot IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT budget.menu_limit INTO configured_menu_limit
            FROM qc_web_exploration_budget AS budget
            WHERE budget.agent_run_id = NEW.agent_run_id
              AND budget.site_id = NEW.site_id
            FOR SHARE;
            IF {invalid_limit} THEN
                RAISE EXCEPTION 'Web 菜单执行槽位 % 超出预算上限 %',
                    NEW.execution_slot, configured_menu_limit
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_menu_execution_slot_budget
        BEFORE INSERT OR UPDATE OF agent_run_id, site_id, execution_slot
        ON qc_web_menu_ledger_entry FOR EACH ROW
        EXECUTE FUNCTION qc_enforce_web_menu_execution_slot()
        """
    )


def upgrade() -> None:
    """新预算默认不限量，保留既有正整数预算及槽位唯一索引。"""
    op.execute(
        "DROP TRIGGER trg_qc_web_menu_execution_slot_budget ON qc_web_menu_ledger_entry"
    )
    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
            ALTER COLUMN menu_limit DROP NOT NULL,
            ALTER COLUMN menu_limit DROP DEFAULT,
            DROP CONSTRAINT ck_qc_web_budget_menu_limit,
            ADD CONSTRAINT ck_qc_web_budget_menu_limit
                CHECK (menu_limit IS NULL OR menu_limit > 0)
        """
    )
    op.execute(
        """
        ALTER TABLE qc_web_menu_ledger_entry
            ALTER COLUMN execution_slot TYPE integer,
            DROP CONSTRAINT ck_qc_web_menu_slot,
            ADD CONSTRAINT ck_qc_web_menu_slot
                CHECK (execution_slot IS NULL OR execution_slot >= 1)
        """
    )
    _replace_slot_guard(allow_unbounded=True)


def downgrade() -> None:
    """持锁预检；不能无损表示为旧预算时拒绝降级，禁止截断或回填。"""
    op.execute(
        "LOCK TABLE qc_web_exploration_budget, qc_web_menu_ledger_entry "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM qc_web_exploration_budget
                WHERE menu_limit IS NULL OR menu_limit NOT IN (5, 10)
            ) OR EXISTS (
                SELECT 1 FROM qc_web_menu_ledger_entry WHERE execution_slot > 10
            ) THEN
                RAISE EXCEPTION 'Web 不限菜单数据无法无损降级至五或十菜单预算'
                    USING ERRCODE = '23514';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
            DROP CONSTRAINT ck_qc_web_budget_menu_limit,
            ALTER COLUMN menu_limit SET NOT NULL,
            ALTER COLUMN menu_limit SET DEFAULT 10,
            ADD CONSTRAINT ck_qc_web_budget_menu_limit CHECK (menu_limit IN (5, 10))
        """
    )
    op.execute(
        "DROP TRIGGER trg_qc_web_menu_execution_slot_budget ON qc_web_menu_ledger_entry"
    )
    op.execute(
        """
        ALTER TABLE qc_web_menu_ledger_entry
            DROP CONSTRAINT ck_qc_web_menu_slot,
            ALTER COLUMN execution_slot TYPE smallint,
            ADD CONSTRAINT ck_qc_web_menu_slot
                CHECK (execution_slot IS NULL OR execution_slot BETWEEN 1 AND 10)
        """
    )
    _replace_slot_guard(allow_unbounded=False)
