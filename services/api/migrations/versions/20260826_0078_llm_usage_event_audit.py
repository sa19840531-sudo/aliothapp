"""新增 LLM 调用成本流水审计表。

Revision ID: 20260826_0078
Revises: 20260824_0077
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op

revision = "20260826_0078"
down_revision = "20260824_0077"
branch_labels = None
depends_on = None

_COMMENT_STATEMENTS = [
    (
        "COMMENT ON TABLE qc_llm_usage_event IS "
        "'LLM 调用成本流水表，记录每一次模型请求的归属、用量、计价和成本'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.scope_type IS "
        "'调用归属类型，用于区分任务运行、调试调用和未绑定调用'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.task_id IS "
        "'调用关联的质检任务标识'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.agent_run_id IS "
        "'调用关联的 Agent Run 标识'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.source IS "
        "'发起调用的系统组件标识'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.operation IS "
        "'发起调用的业务操作标识'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.provider IS "
        "'模型供应商标识'"
    ),
    "COMMENT ON COLUMN qc_llm_usage_event.model IS '模型名称'",
    (
        "COMMENT ON COLUMN qc_llm_usage_event.api_style IS "
        "'模型 API 协议类型'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.usage_available IS "
        "'供应商响应是否包含 token 用量明细'"
    ),
    "COMMENT ON COLUMN qc_llm_usage_event.ai_calls IS '模型请求次数'",
    "COMMENT ON COLUMN qc_llm_usage_event.input_tokens IS '输入 token 数'",
    "COMMENT ON COLUMN qc_llm_usage_event.output_tokens IS '输出 token 数'",
    (
        "COMMENT ON COLUMN qc_llm_usage_event.cache_hit_tokens IS "
        "'缓存命中输入 token 数'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.billable_input_tokens IS "
        "'扣除缓存命中后的计费输入 token 数'"
    ),
    "COMMENT ON COLUMN qc_llm_usage_event.total_tokens IS '总 token 数'",
    "COMMENT ON COLUMN qc_llm_usage_event.input_cost IS '输入 token 成本'",
    "COMMENT ON COLUMN qc_llm_usage_event.output_cost IS '输出 token 成本'",
    (
        "COMMENT ON COLUMN qc_llm_usage_event.cache_hit_cost IS "
        "'缓存命中 token 成本'"
    ),
    "COMMENT ON COLUMN qc_llm_usage_event.total_cost IS '本次调用总成本'",
    "COMMENT ON COLUMN qc_llm_usage_event.cost_currency IS '成本币种'",
    "COMMENT ON COLUMN qc_llm_usage_event.pricing_source IS '计价来源'",
    "COMMENT ON COLUMN qc_llm_usage_event.pricing_unit IS '计价单位'",
    (
        "COMMENT ON COLUMN qc_llm_usage_event.pricing_configured IS "
        "'调用时是否已配置有效单价'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.pricing_tier IS "
        "'调用命中的上下文价格档'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.input_price_per_million_tokens IS "
        "'每百万输入 token 单价'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.output_price_per_million_tokens IS "
        "'每百万输出 token 单价'"
    ),
    (
        "COMMENT ON COLUMN qc_llm_usage_event.cache_hit_price_per_million_tokens IS "
        "'每百万缓存命中 token 单价'"
    ),
    "COMMENT ON COLUMN qc_llm_usage_event.calculation IS '成本计算明细'",
    (
        "COMMENT ON COLUMN qc_llm_usage_event.vendor_usage_payload IS "
        "'供应商返回的用量载荷'"
    ),
    "COMMENT ON COLUMN qc_llm_usage_event.metadata IS '调用流水扩展信息'",
    "COMMENT ON COLUMN qc_llm_usage_event.created_at IS '创建时间'",
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_llm_usage_event (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scope_type varchar(32) NOT NULL DEFAULT 'unbound',
            task_id uuid REFERENCES qc_task(id) ON DELETE SET NULL,
            agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            source varchar(128) NOT NULL,
            operation varchar(128),
            provider varchar(64) NOT NULL,
            model varchar(128) NOT NULL,
            api_style varchar(32) NOT NULL,
            usage_available boolean NOT NULL DEFAULT true,
            ai_calls integer NOT NULL DEFAULT 1,
            input_tokens integer NOT NULL DEFAULT 0,
            output_tokens integer NOT NULL DEFAULT 0,
            cache_hit_tokens integer NOT NULL DEFAULT 0,
            billable_input_tokens integer NOT NULL DEFAULT 0,
            total_tokens integer NOT NULL DEFAULT 0,
            input_cost numeric(18,8) NOT NULL DEFAULT 0,
            output_cost numeric(18,8) NOT NULL DEFAULT 0,
            cache_hit_cost numeric(18,8) NOT NULL DEFAULT 0,
            total_cost numeric(18,8) NOT NULL DEFAULT 0,
            cost_currency varchar(16) NOT NULL DEFAULT 'CNY',
            pricing_source varchar(64) NOT NULL,
            pricing_unit varchar(32) NOT NULL DEFAULT '1m_tokens',
            pricing_configured boolean NOT NULL DEFAULT false,
            pricing_tier varchar(64),
            input_price_per_million_tokens numeric(18,8) NOT NULL DEFAULT 0,
            output_price_per_million_tokens numeric(18,8) NOT NULL DEFAULT 0,
            cache_hit_price_per_million_tokens numeric(18,8) NOT NULL DEFAULT 0,
            calculation jsonb NOT NULL DEFAULT '{}'::jsonb,
            vendor_usage_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_llm_usage_event_scope CHECK (
                scope_type IN ('task_run', 'agent_run', 'debug', 'unbound')
            ),
            CONSTRAINT ck_qc_llm_usage_event_usage_totals CHECK (
                ai_calls >= 0
                AND input_tokens >= 0
                AND output_tokens >= 0
                AND cache_hit_tokens >= 0
                AND billable_input_tokens >= 0
                AND total_tokens >= 0
                AND input_cost >= 0
                AND output_cost >= 0
                AND cache_hit_cost >= 0
                AND total_cost >= 0
                AND input_price_per_million_tokens >= 0
                AND output_price_per_million_tokens >= 0
                AND cache_hit_price_per_million_tokens >= 0
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_qc_llm_usage_event_created "
        "ON qc_llm_usage_event (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_qc_llm_usage_event_task "
        "ON qc_llm_usage_event (task_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_qc_llm_usage_event_run "
        "ON qc_llm_usage_event (agent_run_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_qc_llm_usage_event_scope_source "
        "ON qc_llm_usage_event (scope_type, source, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_qc_llm_usage_event_model "
        "ON qc_llm_usage_event (provider, model, created_at DESC)"
    )
    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_llm_usage_event")
