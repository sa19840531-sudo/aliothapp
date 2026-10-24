"""增加 App 设备资源与跨 Worker 租约事实。

Revision ID: 20260814_0058
Revises: 20260814_0057
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0058"
down_revision = "20260814_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立设备资源、设备租约和 Surface Session 的设备绑定。"""
    op.execute(
        """
        CREATE TABLE qc_device_resource (
            device_ref varchar(255) PRIMARY KEY,
            backend varchar(64) NOT NULL,
            profile_id varchar(255) NOT NULL,
            capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
            enabled boolean NOT NULL DEFAULT true,
            health_state varchar(32) NOT NULL DEFAULT 'available',
            fencing_epoch bigint NOT NULL DEFAULT 0,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_device_resource_ref CHECK (BTRIM(device_ref) <> ''),
            CONSTRAINT ck_qc_device_resource_backend CHECK (BTRIM(backend) <> ''),
            CONSTRAINT ck_qc_device_resource_profile CHECK (BTRIM(profile_id) <> ''),
            CONSTRAINT ck_qc_device_resource_health CHECK (
                health_state IN ('available', 'claimed', 'recovering', 'quarantined', 'disabled')
            ),
            CONSTRAINT ck_qc_device_resource_json CHECK (
                jsonb_typeof(capabilities) = 'object'
            ),
            CONSTRAINT ck_qc_device_resource_counters CHECK (
                fencing_epoch >= 0 AND row_version >= 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_device_lease (
            lease_id uuid PRIMARY KEY,
            device_ref varchar(255) NOT NULL
                REFERENCES qc_device_resource(device_ref) ON DELETE RESTRICT,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            lease_owner varchar(255) NOT NULL,
            lease_token uuid NOT NULL,
            fencing_token bigint NOT NULL,
            state varchar(32) NOT NULL DEFAULT 'claimed',
            claimed_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            last_renewed_at timestamptz,
            released_at timestamptz,
            recovery_started_at timestamptz,
            quarantined_at timestamptz,
            quarantine_reason text,
            row_version bigint NOT NULL DEFAULT 0,
            attempt_count integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_device_lease_owner CHECK (BTRIM(lease_owner) <> ''),
            CONSTRAINT ck_qc_device_lease_session CHECK (BTRIM(session_id) <> ''),
            CONSTRAINT ck_qc_device_lease_state CHECK (
                state IN ('claimed', 'active', 'releasing', 'recovering', 'released', 'quarantined')
            ),
            CONSTRAINT ck_qc_device_lease_token CHECK (fencing_token >= 1),
            CONSTRAINT ck_qc_device_lease_counters CHECK (
                row_version >= 0 AND attempt_count >= 0
            ),
            CONSTRAINT ck_qc_device_lease_expiry CHECK (expires_at > claimed_at),
            CONSTRAINT ck_qc_device_lease_quarantine CHECK (
                (state = 'quarantined' AND quarantined_at IS NOT NULL
                    AND quarantine_reason IS NOT NULL AND BTRIM(quarantine_reason) <> '')
                OR (state <> 'quarantined' AND quarantined_at IS NULL AND quarantine_reason IS NULL)
            )
            -- 设备先于 Starting Session 领取；身份由 claim 与 Session 绑定事务分别校验。
            -- 不建立反向外键，避免“先占设备还是先建 Session”的初始化死锁。
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_device_lease_active_device
        ON qc_device_lease (device_ref)
        WHERE state IN ('claimed', 'active', 'releasing', 'recovering')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_device_lease_session
        ON qc_device_lease (task_id, session_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_device_lease_active_session
        ON qc_device_lease (task_id, session_id)
        WHERE state IN ('claimed', 'active', 'releasing', 'recovering')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_device_lease_recovery
        ON qc_device_lease (state, expires_at, updated_at)
        WHERE state IN ('claimed', 'active', 'releasing', 'recovering')
        """
    )
    op.execute(
        """
        ALTER TABLE qc_surface_session
        ADD COLUMN device_lease_id uuid,
        ADD COLUMN device_lease_fencing_token bigint,
        ADD CONSTRAINT fk_qc_surface_session_device_lease
            FOREIGN KEY (device_lease_id)
            REFERENCES qc_device_lease(lease_id)
            ON DELETE SET NULL,
        ADD CONSTRAINT ck_qc_surface_session_device_lease_fencing
            CHECK (
                (device_lease_id IS NULL AND device_lease_fencing_token IS NULL)
                OR (device_lease_id IS NOT NULL AND device_lease_fencing_token >= 1)
            )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_session_device_lease
        ON qc_surface_session (device_lease_id)
        WHERE device_lease_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_device_resource_updated_at
        BEFORE UPDATE ON qc_device_resource
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_device_lease_updated_at
        BEFORE UPDATE ON qc_device_lease
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_device_resource IS
        'App 设备资源权威目录；health_state=quarantined 时不得自动领取'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_device_lease IS
        'App 设备跨 Worker 租约；fencing_token 用于拒绝旧 Worker 写入'
        """
    )


def downgrade() -> None:
    """存在设备事实时拒绝破坏性回滚。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_device_lease) THEN
                RAISE EXCEPTION
                    '存在 App 设备租约事实，禁止静默清理后降级 0057';
            END IF;
            IF EXISTS (SELECT 1 FROM qc_device_resource) THEN
                RAISE EXCEPTION
                    '存在 App 设备资源事实，禁止静默清理后降级 0057';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DROP INDEX IF EXISTS idx_qc_surface_session_device_lease
        """
    )
    op.execute(
        """
        ALTER TABLE qc_surface_session
        DROP CONSTRAINT IF EXISTS ck_qc_surface_session_device_lease_fencing,
        DROP CONSTRAINT IF EXISTS fk_qc_surface_session_device_lease,
        DROP COLUMN IF EXISTS device_lease_fencing_token,
        DROP COLUMN IF EXISTS device_lease_id
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_device_lease_updated_at ON qc_device_lease
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_device_resource_updated_at ON qc_device_resource
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_device_lease_recovery")
    op.execute("DROP INDEX IF EXISTS uq_qc_device_lease_active_session")
    op.execute("DROP INDEX IF EXISTS idx_qc_device_lease_session")
    op.execute("DROP INDEX IF EXISTS uq_qc_device_lease_active_device")
    op.execute("DROP TABLE IF EXISTS qc_device_lease")
    op.execute("DROP TABLE IF EXISTS qc_device_resource")
