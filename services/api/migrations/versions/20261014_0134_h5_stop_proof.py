"""保留原旅程停止证明，不删除历史隔离原因和采集证据。"""

from alembic import op

revision = "20261014_0134"
down_revision = "20261014_0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE qc_h5_session_journey_guard ADD COLUMN physical_stop_proof jsonb,
        ADD CONSTRAINT ck_h5_physical_stop_proof CHECK (physical_stop_proof IS NULL OR
            (status='drained' AND jsonb_typeof(physical_stop_proof)='array'
             AND jsonb_array_length(physical_stop_proof)>0))""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_h5_session_journey_guard WHERE physical_stop_proof IS NOT NULL)
        THEN RAISE EXCEPTION 'H5 原停止证明已存在，禁止回滚抹去审计'; END IF;
    END $$""")
    op.execute("""ALTER TABLE qc_h5_session_journey_guard DROP CONSTRAINT ck_h5_physical_stop_proof,
        DROP COLUMN physical_stop_proof""")
