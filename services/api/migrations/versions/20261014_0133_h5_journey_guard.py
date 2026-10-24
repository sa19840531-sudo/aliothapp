"""H5 旅程物理排空记录不随控制连接断开或租期到期消失。"""

from alembic import op

revision = "20261014_0133"
down_revision = "20261010_0132"
branch_labels = None
depends_on = None

CREATE_H5_JOURNEY_AVAILABLE = """CREATE FUNCTION alioth_h5_journey_available(value text)
RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF value NOT LIKE 'h5:%' THEN RETURN true; END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='40001',
            MESSAGE='H5 关闭快照不是read committed，资源排空未确认';
    END IF;
    IF NOT pg_try_advisory_xact_lock(hashtextextended('h5-session-journey:' || value,0)) THEN
        RETURN false;
    END IF;
    RETURN NOT EXISTS (SELECT 1 FROM qc_h5_session_journey_guard
                      WHERE session_id=value AND status IN ('active','quarantined'));
END $$"""


def upgrade() -> None:
    # 物理资源可能先于业务会话注册，且未知在途不能随业务删除级联抹去。
    op.execute("""CREATE TABLE qc_h5_session_journey_guard (
        token uuid PRIMARY KEY,
        task_id text NOT NULL CHECK (length(task_id) > 0),
        session_id varchar(128) NOT NULL CHECK (session_id LIKE 'h5:%'),
        status varchar(32) NOT NULL CHECK (status IN ('active','quarantined','drained')),
        reason_code text,
        started_at timestamptz NOT NULL DEFAULT now(),
        drained_at timestamptz,
        CHECK ((status = 'drained') = (drained_at IS NOT NULL))
    )""")
    op.execute("""CREATE UNIQUE INDEX uq_h5_journey_undrained
        ON qc_h5_session_journey_guard(session_id)
        WHERE status IN ('active','quarantined')""")
    op.execute("""CREATE INDEX ix_h5_journey_task_pending
        ON qc_h5_session_journey_guard(task_id)
        WHERE status IN ('active','quarantined')""")
    # VOLATILE内部查询在取得锁后读新快照，不复用外层语句开始时的可见性。
    op.execute(CREATE_H5_JOURNEY_AVAILABLE)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_h5_session_journey_guard
                   WHERE status IN ('active','quarantined')) THEN
            RAISE EXCEPTION 'H5 物理旅程未排空，禁止回滚删除隔离事实';
        END IF;
    END $$""")
    op.execute("DROP FUNCTION alioth_h5_journey_available(text)")
    op.execute("DROP TABLE qc_h5_session_journey_guard")
