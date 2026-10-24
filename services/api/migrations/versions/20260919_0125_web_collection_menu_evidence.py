"""允许正式 Web v4 集合菜单结算，保留原有成功形状与不可变证据。"""

from importlib import import_module

from alembic import op

revision = "20260919_0125"
down_revision = "20260918_0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 保留历史约束的全部分支，仅增加集合菜单的完整行内形状。
    op.execute("""
        DO $$ DECLARE prior_guard text; BEGIN
          SELECT pg_get_expr(conbin, conrelid) INTO STRICT prior_guard
          FROM pg_constraint WHERE conrelid='qc_web_menu_ledger_entry'::regclass
            AND conname='ck_qc_web_menu_success_evidence';
          ALTER TABLE qc_web_menu_ledger_entry DROP CONSTRAINT ck_qc_web_menu_success_evidence;
          EXECUTE 'ALTER TABLE qc_web_menu_ledger_entry ADD CONSTRAINT '
            || 'ck_qc_web_menu_success_evidence CHECK ((' || prior_guard || ') OR (' || $guard$
              status='menu_page_captured'
              AND normalization_version='web-page-type-assessment.v4'
              AND menu_page_type='collection_page'
              AND decision <> 'home_baseline'
              AND execution_slot IS NOT NULL AND execution_slot > 0
              AND menu_action_count=1 AND detail_action_count=0
              AND menu_intent_id IS NOT NULL AND menu_source_capture_id IS NOT NULL
              AND menu_action_execution_id IS NOT NULL
              AND menu_probe_capture_id IS NOT NULL AND menu_ready_capture_id IS NOT NULL
              AND menu_probe_capture_id <> menu_ready_capture_id
              AND diagnostic->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'
              AND detail_intent_id IS NULL AND detail_source_capture_id IS NULL
              AND detail_candidate_id IS NULL AND detail_destination_sha256 IS NULL
              AND detail_action_execution_id IS NULL AND detail_probe_capture_id IS NULL
              AND detail_ready_capture_id IS NULL AND detail_page_type IS NULL
              AND detail_selection='{}'::jsonb
            $guard$ || ') IS TRUE)';
        END $$
    """)


def downgrade() -> None:
    # 不删除或伪造已生成的集合证据；有此类终态时必须先处理版本回退方案。
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM qc_web_menu_ledger_entry
                     WHERE status='menu_page_captured' AND menu_page_type='collection_page') THEN
            RAISE EXCEPTION 'Cannot downgrade immutable Web collection evidence';
          END IF;
        END $$
    """)
    previous = import_module(
        "services.api.migrations.versions.20260914_0119_web_v4_success_evidence"
    )
    previous._replace_success_guard(allow_v4=True)
