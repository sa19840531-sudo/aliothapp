"""管理后台对齐 2.1 原型：部门表、公司联系人、账号部门、产品归属公司、导入批次号、AI 供应商登记与平台设置。

Revision ID: 20260908_0102
Revises: 20260908_0101
Create Date: 2026-09-08

2.1 原型的体验中心管理后台有「分公司管理 / 部门管理 / 账号与权限 / Token 与费用 / AI 服务管理 /
Excel 导入」，这些页面的列在库里都没有对应的数据，补表补列：

- qc_tenant 增加 contact_name / remark（分公司联系人、备注）。
- qc_user 增加 department_name（账号所属部门，部门平台页脚与账号列表展示）。
- 新表 qc_department：分公司 → 部门 → 产品 的中间层，带负责人、状态与月度 Token 预算（元）。
  存量产品填过的部门名回填到体验中心名下，部门页不至于空着。
- qc_product 增加 tenant_id（NULL = 运营中心维护、全公司共享；有值 = 该公司自己的产品）。
- qc_product_import_record 增加 batch_no（IMP-YYYYMMDD-NNN，原型导入记录的「导入批次」）。
- 新表 qc_llm_provider / qc_llm_provider_event：AI 供应商登记、主备优先级、连接测试结果与服务事件。
  只存名称、地址、模型和运行指标，不存密钥；当前生效的供应商由环境变量决定并同步进表。
- 新表 qc_platform_setting：平台级键值设置（自动故障转移、预警策略等）。
"""

from __future__ import annotations

from alembic import op

revision = "20260908_0102"
down_revision = "20260908_0101"
branch_labels = None
depends_on = None

_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    """补公司联系人、账号部门、部门表、产品归属、导入批次号、供应商登记与平台设置。"""

    op.execute("ALTER TABLE qc_tenant ADD COLUMN IF NOT EXISTS contact_name varchar(128)")
    op.execute("ALTER TABLE qc_tenant ADD COLUMN IF NOT EXISTS remark text")

    op.execute("ALTER TABLE qc_user ADD COLUMN IF NOT EXISTS department_name varchar(128)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_department (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES qc_tenant(id),
            name varchar(128) NOT NULL,
            owner_name varchar(128),
            status varchar(16) NOT NULL DEFAULT 'enabled',
            monthly_token_budget numeric(14, 2),
            remark text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_department_tenant_name UNIQUE (tenant_id, name)
        )
        """
    )

    op.execute("ALTER TABLE qc_product ADD COLUMN IF NOT EXISTS tenant_id uuid REFERENCES qc_tenant(id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_qc_product_tenant ON qc_product (tenant_id)")
    op.execute(
        f"""
        INSERT INTO qc_department (tenant_id, name)
        SELECT '{_DEFAULT_TENANT_ID}'::uuid, department_name
        FROM qc_product
        WHERE department_name IS NOT NULL AND btrim(department_name) <> ''
        GROUP BY department_name
        ON CONFLICT DO NOTHING
        """
    )

    op.execute("ALTER TABLE qc_product_import_record ADD COLUMN IF NOT EXISTS batch_no varchar(32)")
    op.execute(
        """
        UPDATE qc_product_import_record AS record
        SET batch_no = 'IMP-' || to_char(numbered.created_at, 'YYYYMMDD') || '-' || lpad(numbered.rn::text, 3, '0')
        FROM (
            SELECT id, created_at,
                   row_number() OVER (PARTITION BY date(created_at) ORDER BY created_at, id) AS rn
            FROM qc_product_import_record
        ) AS numbered
        WHERE numbered.id = record.id AND record.batch_no IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_llm_provider (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code varchar(64) NOT NULL UNIQUE,
            name varchar(128) NOT NULL,
            base_url text,
            model varchar(128),
            priority integer NOT NULL DEFAULT 1,
            weight integer NOT NULL DEFAULT 0,
            breaker_rule varchar(255),
            status varchar(16) NOT NULL DEFAULT 'standby',
            is_primary boolean NOT NULL DEFAULT false,
            api_key_configured boolean NOT NULL DEFAULT false,
            managed_by_env boolean NOT NULL DEFAULT false,
            quota_remaining_percent numeric(5, 2),
            last_check_status varchar(16),
            last_check_latency_ms integer,
            last_check_message text,
            last_checked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_llm_provider_event (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            provider_id uuid REFERENCES qc_llm_provider(id) ON DELETE SET NULL,
            provider_name varchar(128) NOT NULL,
            event_type varchar(32) NOT NULL,
            description text NOT NULL,
            action text,
            result varchar(64),
            operator_name varchar(128),
            occurred_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_qc_llm_provider_event_occurred
            ON qc_llm_provider_event (occurred_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_platform_setting (
            key varchar(64) PRIMARY KEY,
            value jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_by varchar(128),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """删表删列。"""

    op.execute("DROP TABLE IF EXISTS qc_platform_setting")
    op.execute("DROP TABLE IF EXISTS qc_llm_provider_event")
    op.execute("DROP TABLE IF EXISTS qc_llm_provider")
    op.execute("ALTER TABLE qc_product_import_record DROP COLUMN IF EXISTS batch_no")
    op.execute("DROP INDEX IF EXISTS ix_qc_product_tenant")
    op.execute("ALTER TABLE qc_product DROP COLUMN IF EXISTS tenant_id")
    op.execute("DROP TABLE IF EXISTS qc_department")
    op.execute("ALTER TABLE qc_user DROP COLUMN IF EXISTS department_name")
    op.execute("ALTER TABLE qc_tenant DROP COLUMN IF EXISTS remark")
    op.execute("ALTER TABLE qc_tenant DROP COLUMN IF EXISTS contact_name")
