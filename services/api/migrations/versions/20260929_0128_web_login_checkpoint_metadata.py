"""同步 Web 登录游标的有界字段与数据库检查约束。"""

from alembic import op

revision = "20260929_0128"
down_revision = "20260924_0127"
branch_labels = None
depends_on = None

_REFERENCES = """'parent_capture_id','capture_id','menu_ledger_id','session_id',
    'idempotency_key','obstruction_fingerprint','surface_phase_run_id',
    'candidate_href_index','finalization_epoch','current_url','requested_url',
    'final_url','menu_navigation_capture_id'"""
_LOGIN = """'login_source_capture_id','login_branch_status','login_branch_reason',
    'login_cross_origin_status','login_blocked_requests','login_account_status',
    'login_account_artifact_ids','login_account_storage_paths',
    'login_account_avatar_opened','login_account_avatar_dom_artifact_id',
    'login_account_page_type','login_account_page_url','login_account_page_visits'"""
_AUDIT = "'raw_error_code','coordination_stage','cause_type','retryable'"


def _replace_constraint(*, login_metadata: bool) -> None:
    fields = _REFERENCES + ("," + _LOGIN if login_metadata else "")
    op.execute("""ALTER TABLE qc_web_run_stage_checkpoint
        DROP CONSTRAINT ck_qc_web_checkpoint_reference_only""")
    op.execute(f"""ALTER TABLE qc_web_run_stage_checkpoint
        ADD CONSTRAINT ck_qc_web_checkpoint_reference_only CHECK (
            cursor_payload - ARRAY[{fields}]::text[] = '{{}}'::jsonb
            AND last_error_detail - ARRAY[{fields},{_AUDIT}]::text[] = '{{}}'::jsonb)""")


def upgrade() -> None:
    _replace_constraint(login_metadata=True)


def downgrade() -> None:
    # 现存登录结果不能在降级时从检查约束中隐式丢失。
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_web_run_stage_checkpoint
            WHERE cursor_payload ?| ARRAY[{_LOGIN}]::text[]
               OR last_error_detail ?| ARRAY[{_LOGIN}]::text[]) THEN
            RAISE EXCEPTION 'WEB_LOGIN_CHECKPOINT_DOWNGRADE_BLOCKED';
        END IF;
    END $$""")
    _replace_constraint(login_metadata=False)
