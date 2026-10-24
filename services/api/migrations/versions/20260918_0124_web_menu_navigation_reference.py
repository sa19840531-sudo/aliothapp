"""允许 Web 游标保留原始菜单导航引用，不放宽页面内容白名单。"""

from alembic import op

revision = "20260918_0124"
down_revision = "20260917_0123"
branch_labels = None
depends_on = None

_REFERENCES = """'parent_capture_id','capture_id','menu_ledger_id','session_id',
    'idempotency_key','obstruction_fingerprint','surface_phase_run_id',
    'candidate_href_index','finalization_epoch','current_url','requested_url','final_url'"""
_AUDIT = "'raw_error_code','coordination_stage','cause_type','retryable'"


def _replace_constraint(*, navigation_reference: bool) -> None:
    references = _REFERENCES + (",'menu_navigation_capture_id'" if navigation_reference else "")
    op.execute("""ALTER TABLE qc_web_run_stage_checkpoint
        DROP CONSTRAINT ck_qc_web_checkpoint_reference_only""")
    op.execute(f"""ALTER TABLE qc_web_run_stage_checkpoint
        ADD CONSTRAINT ck_qc_web_checkpoint_reference_only CHECK (
            cursor_payload - ARRAY[{references}]::text[] = '{{}}'::jsonb
            AND last_error_detail - ARRAY[{references},{_AUDIT}]::text[] = '{{}}'::jsonb)""")


def upgrade() -> None:
    _replace_constraint(navigation_reference=True)


def downgrade() -> None:
    # 存在新引用时拒绝降级，不静默丢失导航血缘证据。
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_web_run_stage_checkpoint
            WHERE cursor_payload ? 'menu_navigation_capture_id'
               OR last_error_detail ? 'menu_navigation_capture_id') THEN
            RAISE EXCEPTION 'WEB_MENU_NAVIGATION_REFERENCE_DOWNGRADE_BLOCKED';
        END IF;
    END $$""")
    _replace_constraint(navigation_reference=False)
