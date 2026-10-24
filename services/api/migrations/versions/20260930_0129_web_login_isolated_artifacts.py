"""Web 隔离登录附件获得可审计索引，保持独立于主线 Capture 清单。"""

from alembic import op

revision = "20260930_0129"
down_revision = "20260929_0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_evidence_artifact_source")
    op.execute("""ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_source CHECK (
          source IN ('baseline', 'rule_supplement', 'ai_call', 'report',
                     'app_runtime', 'web_login_isolated'))""")
    op.execute("""ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_web_login_isolated_artifact CHECK (
          source <> 'web_login_isolated' OR ((
            page_capture_id IS NULL
            AND artifact_type IN ('screenshot', 'dom')
            AND metadata->>'branch' = 'isolated_login'
            AND metadata->>'source_capture_id' ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND metadata->>'agent_run_id' ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND metadata->>'main_session_id' = 'web:' || (metadata->>'agent_run_id')
            AND metadata->>'isolated_session_id' =
              (metadata->>'main_session_id') || chr(58) || 'login'
            AND metadata->>'step' ~ '^[a-z][a-z0-9_]{0,63}$') IS TRUE))""")
    op.execute("""CREATE UNIQUE INDEX uq_qc_web_login_isolated_artifact_step
        ON qc_evidence_artifact (
          task_id, (metadata->>'source_capture_id'), (metadata->>'step'), artifact_type)
        WHERE source = 'web_login_isolated'""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_evidence_artifact
                   WHERE source = 'web_login_isolated') THEN
          RAISE EXCEPTION 'WEB_LOGIN_ARTIFACT_DOWNGRADE_BLOCKED';
        END IF;
      END $$""")
    op.execute("DROP INDEX uq_qc_web_login_isolated_artifact_step")
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_web_login_isolated_artifact")
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_evidence_artifact_source")
    op.execute("""ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_source CHECK (
          source IN ('baseline', 'rule_supplement', 'ai_call', 'report', 'app_runtime'))""")
