"""接通检查点观测附件；保留既有类型，已有样本禁止破坏性降级。"""

from alembic import op

revision = "20261016_0138"
down_revision = "20261016_0137"
branch_labels = None
depends_on = None

LEGACY_TYPES = (
    "'screenshot', 'dom', 'visible_text', 'ocr_text', 'har', "
    "'console_log', 'media_state', 'ai_io', 'report', "
    "'action_trace', 'ui_snapshot', 'temporal_trace', 'page_coverage', "
    "'ui_tree', 'view_hierarchy', 'device_log', 'network_trace', "
    "'fault_artifact'"
)


def upgrade():
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_evidence_artifact_type")
    op.execute(f"""ALTER TABLE qc_evidence_artifact ADD CONSTRAINT ck_qc_evidence_artifact_type
        CHECK (artifact_type IN ({LEGACY_TYPES}, 'checkpoint_observation'))""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM qc_evidence_artifact
                  WHERE artifact_type='checkpoint_observation') THEN
          RAISE EXCEPTION '检查点样本已存在，禁止回滚抹去';
        END IF;
      END $$""")
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_evidence_artifact_type")
    op.execute(f"""ALTER TABLE qc_evidence_artifact ADD CONSTRAINT ck_qc_evidence_artifact_type
        CHECK (artifact_type IN ({LEGACY_TYPES}))""")
