"""将新 Web 探索任务的菜单硬上限收紧为五个。

Revision ID: 20260903_0088
Revises: 20260903_0087
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0088"
down_revision = "20260903_0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留历史十菜单预算，同时对新五菜单预算按行强制执行槽位上限。"""

    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
            ALTER COLUMN menu_limit SET DEFAULT 5,
            DROP CONSTRAINT ck_qc_web_budget_menu_limit,
            ADD CONSTRAINT ck_qc_web_budget_menu_limit
                CHECK (menu_limit IN (5, 10))
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_enforce_web_menu_execution_slot()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            configured_menu_limit smallint;
        BEGIN
            IF NEW.execution_slot IS NULL THEN
                RETURN NEW;
            END IF;

            SELECT budget.menu_limit
            INTO configured_menu_limit
            FROM qc_web_exploration_budget AS budget
            WHERE budget.agent_run_id = NEW.agent_run_id
              AND budget.site_id = NEW.site_id;

            IF configured_menu_limit IS NULL
               OR NEW.execution_slot > configured_menu_limit THEN
                RAISE EXCEPTION
                    'Web 菜单执行槽位 % 超出预算上限 %',
                    NEW.execution_slot,
                    configured_menu_limit
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
        ON qc_web_menu_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION qc_enforce_web_menu_execution_slot()
        """
    )


def downgrade() -> None:
    """移除动态槽位校验并恢复旧版十菜单固定预算。"""

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_web_menu_execution_slot_budget
        ON qc_web_menu_ledger_entry
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_enforce_web_menu_execution_slot()")
    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
            DROP CONSTRAINT ck_qc_web_budget_menu_limit
        """
    )
    op.execute(
        """
        UPDATE qc_web_exploration_budget
        SET menu_limit = 10
        WHERE menu_limit = 5
        """
    )
    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
            ALTER COLUMN menu_limit SET DEFAULT 10,
            ADD CONSTRAINT ck_qc_web_budget_menu_limit
                CHECK (menu_limit = 10)
        """
    )
