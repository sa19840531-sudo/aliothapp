"""创建 Alioth 核心表结构。

Revision ID: 20260706_0001
Revises:
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op

revision = "20260706_0001"
down_revision = None
branch_labels = None
depends_on = None


TABLES = [
    """
    CREATE TABLE qc_ruleset (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ruleset_code varchar(128) NOT NULL,
        name varchar(255) NOT NULL,
        version integer NOT NULL,
        status varchar(32) NOT NULL DEFAULT 'draft',
        description text,
        source_type varchar(32) NOT NULL DEFAULT 'manual',
        source_ref text,
        snapshot_hash varchar(128),
        rule_count integer NOT NULL DEFAULT 0,
        page_sampling_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
        loop_guard jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_by uuid,
        published_at timestamptz,
        archived_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_ruleset_code_version UNIQUE (ruleset_code, version),
        CONSTRAINT ck_qc_ruleset_status CHECK (
            status IN ('draft', 'enabled', 'disabled', 'archived')
        ),
        CONSTRAINT ck_qc_ruleset_source_type CHECK (
            source_type IN ('manual', 'spreadsheet', 'import', 'api')
        ),
        CONSTRAINT ck_qc_ruleset_rule_count CHECK (rule_count >= 0)
    )
    """,
    """
    CREATE TABLE qc_rule (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ruleset_id uuid NOT NULL REFERENCES qc_ruleset(id) ON DELETE RESTRICT,
        rule_code varchar(128) NOT NULL,
        name varchar(255) NOT NULL,
        description text,
        category varchar(32) NOT NULL,
        executor_type varchar(32) NOT NULL,
        severity varchar(32) NOT NULL DEFAULT 'medium',
        status varchar(32) NOT NULL DEFAULT 'enabled',
        scope varchar(128),
        channel varchar(32),
        page_type varchar(64),
        url_pattern text,
        steps jsonb NOT NULL DEFAULT '[]'::jsonb,
        input_contract jsonb NOT NULL DEFAULT '{}'::jsonb,
        check_point text NOT NULL,
        judge_config jsonb NOT NULL DEFAULT '{}'::jsonb,
        manual_review_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
        sort_order integer NOT NULL DEFAULT 100,
        created_by uuid,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_rule_ruleset_code UNIQUE (ruleset_id, rule_code),
        CONSTRAINT ck_qc_rule_category CHECK (
            category IN (
                'text', 'visual', 'interaction', 'ad', 'payment',
                'input_risk', 'player', 'compliance'
            )
        ),
        CONSTRAINT ck_qc_rule_executor_type CHECK (
            executor_type IN ('llm', 'rule_engine', 'hybrid')
        ),
        CONSTRAINT ck_qc_rule_severity CHECK (
            severity IN ('low', 'medium', 'high', 'critical')
        ),
        CONSTRAINT ck_qc_rule_status CHECK (
            status IN ('draft', 'enabled', 'disabled', 'archived')
        ),
        CONSTRAINT ck_qc_rule_channel CHECK (
            channel IS NULL OR channel IN ('workbench', 'telegram', 'api')
        )
    )
    """,
    """
    CREATE TABLE qc_task (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        target_url text NOT NULL,
        ruleset_id uuid NOT NULL REFERENCES qc_ruleset(id) ON DELETE RESTRICT,
        channel varchar(32) NOT NULL DEFAULT 'workbench',
        status varchar(32) NOT NULL DEFAULT 'queued',
        priority integer NOT NULL DEFAULT 100,
        submitter_id uuid,
        task_config jsonb NOT NULL DEFAULT '{}'::jsonb,
        page_sampling_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
        loop_guard jsonb NOT NULL DEFAULT '{}'::jsonb,
        locked_by varchar(128),
        locked_at timestamptz,
        attempt_count integer NOT NULL DEFAULT 0,
        max_attempts integer NOT NULL DEFAULT 3,
        next_run_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        finished_at timestamptz,
        last_error text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_qc_task_status CHECK (
            status IN ('queued', 'running', 'review', 'done', 'failed', 'canceled')
        ),
        CONSTRAINT ck_qc_task_channel CHECK (
            channel IN ('workbench', 'telegram', 'api')
        ),
        CONSTRAINT ck_qc_task_attempts CHECK (attempt_count >= 0 AND max_attempts > 0)
    )
    """,
    """
    CREATE TABLE qc_page_capture (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        parent_page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        page_seq integer NOT NULL,
        page_slug varchar(128) NOT NULL,
        url text NOT NULL,
        page_type varchar(64),
        page_state varchar(64),
        capture_status varchar(32) NOT NULL DEFAULT 'pending',
        screenshot_path text,
        dom_path text,
        visible_text_path text,
        console_log_path text,
        action_log jsonb NOT NULL DEFAULT '[]'::jsonb,
        metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
        captured_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_page_capture_seq UNIQUE (task_id, page_seq),
        CONSTRAINT ck_qc_page_capture_status CHECK (
            capture_status IN ('pending', 'captured', 'failed', 'skipped')
        ),
        CONSTRAINT ck_qc_page_capture_seq CHECK (page_seq > 0)
    )
    """,
    """
    CREATE TABLE qc_evidence_artifact (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        rule_execution_id uuid,
        artifact_type varchar(64) NOT NULL,
        source varchar(64) NOT NULL,
        storage_path text NOT NULL,
        content_type varchar(128),
        checksum varchar(128),
        size_bytes bigint,
        metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN (
                'screenshot', 'dom', 'visible_text', 'ocr_text', 'har',
                'console_log', 'media_state', 'ai_io', 'report'
            )
        ),
        CONSTRAINT ck_qc_evidence_artifact_source CHECK (
            source IN ('baseline', 'rule_supplement', 'ai_call', 'report')
        ),
        CONSTRAINT ck_qc_evidence_artifact_size CHECK (
            size_bytes IS NULL OR size_bytes >= 0
        )
    )
    """,
    """
    CREATE TABLE qc_observation_packet (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        rule_id uuid REFERENCES qc_rule(id) ON DELETE SET NULL,
        evidence_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        packet_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        quality_gate_status varchar(32) NOT NULL DEFAULT 'pending',
        quality_gate_reason text,
        created_by_agent_run_id uuid,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_qc_observation_packet_quality CHECK (
            quality_gate_status IN ('pending', 'pass', 'fail')
        )
    )
    """,
    """
    CREATE TABLE qc_rule_execution (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
        page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        observation_packet_id uuid REFERENCES qc_observation_packet(id) ON DELETE SET NULL,
        agent_run_id uuid,
        source_action_trace_id uuid,
        execution_status varchar(32) NOT NULL,
        judge_strategy varchar(32) NOT NULL,
        confidence numeric(5,4),
        severity varchar(32),
        result_summary text,
        problem_desc text,
        evidence_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        judge_input jsonb NOT NULL DEFAULT '{}'::jsonb,
        judge_output jsonb NOT NULL DEFAULT '{}'::jsonb,
        review_status varchar(32),
        review_reason text,
        error_code varchar(128),
        error_message text,
        started_at timestamptz,
        finished_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_rule_execution_once UNIQUE (task_id, rule_id, page_capture_id),
        CONSTRAINT ck_qc_rule_execution_status CHECK (
            execution_status IN (
                'pass', 'fail', 'warning', 'manual_review',
                'evidence_insufficient', 'skipped', 'error'
            )
        ),
        CONSTRAINT ck_qc_rule_execution_strategy CHECK (
            judge_strategy IN ('rule_engine', 'llm', 'hybrid')
        ),
        CONSTRAINT ck_qc_rule_execution_review_status CHECK (
            review_status IS NULL OR review_status IN (
                'pending', 'approved', 'rejected', 'needs_recheck'
            )
        ),
        CONSTRAINT ck_qc_rule_execution_confidence CHECK (
            confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
        )
    )
    """,
    """
    CREATE TABLE qc_review_item (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        rule_execution_id uuid NOT NULL REFERENCES qc_rule_execution(id) ON DELETE CASCADE,
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        trigger_reason varchar(64) NOT NULL,
        status varchar(32) NOT NULL DEFAULT 'pending',
        priority integer NOT NULL DEFAULT 100,
        reviewer uuid,
        review_reason text,
        reviewed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_review_item_execution UNIQUE (rule_execution_id),
        CONSTRAINT ck_qc_review_item_trigger CHECK (
            trigger_reason IN (
                'low_confidence', 'high_risk', 'evidence_insufficient',
                'loop_budget_exceeded', 'no_progress', 'manual_flag'
            )
        ),
        CONSTRAINT ck_qc_review_item_status CHECK (
            status IN ('pending', 'approved', 'rejected', 'needs_recheck')
        )
    )
    """,
    """
    CREATE TABLE qc_agent_run (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        parent_agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
        agent_type varchar(64) NOT NULL,
        agent_name varchar(128) NOT NULL,
        status varchar(32) NOT NULL DEFAULT 'running',
        input_context jsonb NOT NULL DEFAULT '{}'::jsonb,
        output_context jsonb NOT NULL DEFAULT '{}'::jsonb,
        working_memory jsonb NOT NULL DEFAULT '{}'::jsonb,
        loop_guard jsonb NOT NULL DEFAULT '{}'::jsonb,
        stop_reason varchar(128),
        error_message text,
        total_steps integer NOT NULL DEFAULT 0,
        total_ai_calls integer NOT NULL DEFAULT 0,
        total_tokens integer NOT NULL DEFAULT 0,
        total_cost numeric(12,6) NOT NULL DEFAULT 0,
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_qc_agent_run_type CHECK (
            agent_type IN (
                'main', 'planner', 'explorer', 'execution',
                'evidence', 'rule_judge', 'review', 'report'
            )
        ),
        CONSTRAINT ck_qc_agent_run_status CHECK (
            status IN ('running', 'done', 'failed', 'aborted', 'canceled')
        ),
        CONSTRAINT ck_qc_agent_run_totals CHECK (
            total_steps >= 0
            AND total_ai_calls >= 0
            AND total_tokens >= 0
            AND total_cost >= 0
        )
    )
    """,
    """
    CREATE TABLE qc_agent_action_trace (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE CASCADE,
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        step_seq integer NOT NULL,
        phase varchar(64),
        thought_summary text,
        selected_capability varchar(128),
        capability_kind varchar(32),
        capability_input jsonb NOT NULL DEFAULT '{}'::jsonb,
        capability_output jsonb NOT NULL DEFAULT '{}'::jsonb,
        memory_delta jsonb NOT NULL DEFAULT '{}'::jsonb,
        cost jsonb NOT NULL DEFAULT '{}'::jsonb,
        latency_ms integer,
        status varchar(32) NOT NULL,
        error_code varchar(128),
        error_message text,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_agent_action_trace_step UNIQUE (agent_run_id, step_seq),
        CONSTRAINT ck_qc_agent_action_trace_status CHECK (
            status IN ('ok', 'error', 'retried', 'skipped', 'terminated', 'denied')
        ),
        CONSTRAINT ck_qc_agent_action_trace_capability_kind CHECK (
            capability_kind IS NULL OR capability_kind IN ('tool', 'skill')
        ),
        CONSTRAINT ck_qc_agent_action_trace_latency CHECK (
            latency_ms IS NULL OR latency_ms >= 0
        )
    )
    """,
    """
    CREATE TABLE qc_agent_capability_registry (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        capability_name varchar(128) NOT NULL,
        capability_kind varchar(32) NOT NULL,
        display_name varchar(255) NOT NULL,
        description text,
        version varchar(64) NOT NULL DEFAULT '1.0.0',
        status varchar(32) NOT NULL DEFAULT 'enabled',
        input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
        output_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
        permission_level varchar(32) NOT NULL DEFAULT 'low',
        timeout_ms integer NOT NULL DEFAULT 30000,
        retry_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
        owner_team varchar(128),
        implementation_ref varchar(255),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_agent_capability_registry_name_version UNIQUE (
            capability_name, version
        ),
        CONSTRAINT ck_qc_agent_capability_registry_kind CHECK (
            capability_kind IN ('tool', 'skill')
        ),
        CONSTRAINT ck_qc_agent_capability_registry_status CHECK (
            status IN ('draft', 'enabled', 'disabled', 'archived')
        ),
        CONSTRAINT ck_qc_agent_capability_registry_permission CHECK (
            permission_level IN ('low', 'medium', 'high_risk')
        ),
        CONSTRAINT ck_qc_agent_capability_registry_timeout CHECK (
            timeout_ms > 0
        )
    )
    """,
]


INDEXES_AND_TRIGGERS = [
    "CREATE INDEX idx_qc_ruleset_status ON qc_ruleset (status, updated_at DESC)",
    "CREATE TRIGGER trg_qc_ruleset_updated_at BEFORE UPDATE ON qc_ruleset FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_rule_ruleset ON qc_rule (ruleset_id, sort_order)",
    "CREATE INDEX idx_qc_rule_match ON qc_rule (ruleset_id, status, category, channel, page_type)",
    "CREATE INDEX idx_qc_rule_code ON qc_rule (rule_code)",
    "CREATE INDEX idx_qc_rule_input_contract_gin ON qc_rule USING gin (input_contract)",
    "CREATE TRIGGER trg_qc_rule_updated_at BEFORE UPDATE ON qc_rule FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_task_queue ON qc_task (status, next_run_at, priority, created_at) WHERE status = 'queued'",
    "CREATE INDEX idx_qc_task_status_created ON qc_task (status, created_at DESC)",
    "CREATE INDEX idx_qc_task_ruleset ON qc_task (ruleset_id)",
    "CREATE TRIGGER trg_qc_task_updated_at BEFORE UPDATE ON qc_task FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_page_capture_task ON qc_page_capture (task_id, page_seq)",
    "CREATE INDEX idx_qc_page_capture_type ON qc_page_capture (task_id, page_type)",
    "CREATE TRIGGER trg_qc_page_capture_updated_at BEFORE UPDATE ON qc_page_capture FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_evidence_artifact_task ON qc_evidence_artifact (task_id, created_at DESC)",
    "CREATE INDEX idx_qc_evidence_artifact_page ON qc_evidence_artifact (page_capture_id)",
    "CREATE INDEX idx_qc_evidence_artifact_rule_execution ON qc_evidence_artifact (rule_execution_id)",
    "CREATE INDEX idx_qc_evidence_artifact_type ON qc_evidence_artifact (artifact_type, source)",
    "CREATE INDEX idx_qc_observation_packet_task ON qc_observation_packet (task_id, created_at DESC)",
    "CREATE INDEX idx_qc_observation_packet_rule ON qc_observation_packet (rule_id)",
    "CREATE INDEX idx_qc_observation_packet_quality ON qc_observation_packet (quality_gate_status)",
    "CREATE INDEX idx_qc_observation_packet_artifacts_gin ON qc_observation_packet USING gin (evidence_artifact_ids)",
    "CREATE TRIGGER trg_qc_observation_packet_updated_at BEFORE UPDATE ON qc_observation_packet FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_rule_execution_task ON qc_rule_execution (task_id, created_at DESC)",
    "CREATE INDEX idx_qc_rule_execution_rule ON qc_rule_execution (rule_id)",
    "CREATE INDEX idx_qc_rule_execution_status ON qc_rule_execution (execution_status, review_status)",
    "CREATE INDEX idx_qc_rule_execution_agent_run ON qc_rule_execution (agent_run_id)",
    "CREATE INDEX idx_qc_rule_execution_source_action ON qc_rule_execution (source_action_trace_id)",
    "CREATE INDEX idx_qc_rule_execution_artifacts_gin ON qc_rule_execution USING gin (evidence_artifact_ids)",
    "CREATE INDEX idx_qc_rule_execution_output_gin ON qc_rule_execution USING gin (judge_output)",
    "CREATE TRIGGER trg_qc_rule_execution_updated_at BEFORE UPDATE ON qc_rule_execution FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_review_item_queue ON qc_review_item (status, priority, created_at) WHERE status = 'pending'",
    "CREATE INDEX idx_qc_review_item_task ON qc_review_item (task_id, created_at DESC)",
    "CREATE TRIGGER trg_qc_review_item_updated_at BEFORE UPDATE ON qc_review_item FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_agent_run_task ON qc_agent_run (task_id, started_at DESC)",
    "CREATE INDEX idx_qc_agent_run_parent ON qc_agent_run (parent_agent_run_id)",
    "CREATE INDEX idx_qc_agent_run_status ON qc_agent_run (status, started_at DESC)",
    "CREATE TRIGGER trg_qc_agent_run_updated_at BEFORE UPDATE ON qc_agent_run FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    "CREATE INDEX idx_qc_agent_action_trace_run ON qc_agent_action_trace (agent_run_id, step_seq)",
    "CREATE INDEX idx_qc_agent_action_trace_task ON qc_agent_action_trace (task_id, created_at DESC)",
    "CREATE INDEX idx_qc_agent_action_trace_capability ON qc_agent_action_trace (selected_capability, status)",
    "CREATE INDEX idx_qc_agent_action_trace_output_gin ON qc_agent_action_trace USING gin (capability_output)",
    "CREATE INDEX idx_qc_agent_capability_registry_enabled ON qc_agent_capability_registry (status, capability_kind, permission_level)",
    "CREATE INDEX idx_qc_agent_capability_registry_name ON qc_agent_capability_registry (capability_name)",
    "CREATE INDEX idx_qc_agent_capability_registry_input_schema_gin ON qc_agent_capability_registry USING gin (input_schema)",
    "CREATE TRIGGER trg_qc_agent_capability_registry_updated_at BEFORE UPDATE ON qc_agent_capability_registry FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
]


FOREIGN_KEYS = [
    """
    ALTER TABLE qc_evidence_artifact
    ADD CONSTRAINT fk_qc_evidence_artifact_rule_execution
    FOREIGN KEY (rule_execution_id)
    REFERENCES qc_rule_execution(id)
    ON DELETE SET NULL
    """,
    """
    ALTER TABLE qc_rule_execution
    ADD CONSTRAINT fk_qc_rule_execution_agent_run
    FOREIGN KEY (agent_run_id)
    REFERENCES qc_agent_run(id)
    ON DELETE SET NULL
    """,
    """
    ALTER TABLE qc_rule_execution
    ADD CONSTRAINT fk_qc_rule_execution_source_action_trace
    FOREIGN KEY (source_action_trace_id)
    REFERENCES qc_agent_action_trace(id)
    ON DELETE SET NULL
    """,
]


TABLE_COMMENTS = {
    "qc_ruleset": "规则集版本快照表，任务启动时锁定一组规则版本",
    "qc_rule": "质检规则表，定义规则分类、适用范围、执行方式与判定契约",
    "qc_task": "质检任务表，作为长期任务状态源；Redis 负责短期分发队列与运行中结果缓存",
    "qc_page_capture": "页面基线采集记录表，组织页面级截图、DOM、文本和动作摘要",
    "qc_evidence_artifact": "证据产物表，统一记录截图、DOM、OCR、HAR、AI IO 和报告文件",
    "qc_observation_packet": "Observation Packet 表，沉淀规则判定前的标准化证据输入包",
    "qc_rule_execution": "规则执行结果主表，记录每条规则在任务和页面上的最终判定",
    "qc_review_item": "人工审核项表，由规则执行结果派生，用于人工复核队列和反馈闭环",
    "qc_agent_run": "Agent 运行实例表，记录 Main Agent 和后续专职 Agent 的执行上下文与成本",
    "qc_agent_action_trace": "Agent 单步动作留痕表，append-only 记录每次能力调用、能力读取和决策摘要",
    "qc_agent_capability_registry": "Agent 可见能力契约注册表，统一维护 Skill 与 Tool 的版本、Schema 与权限",
}


COLUMN_COMMENTS = {
    "qc_ruleset": {
        "id": "规则集主键",
        "ruleset_code": "规则集业务编号，同一规则集跨版本保持不变",
        "name": "规则集名称",
        "version": "规则集版本号，与 ruleset_code 组成唯一版本",
        "status": "规则集状态，见数据字典 lifecycle_status",
        "description": "规则集说明",
        "source_type": "规则集来源类型，见数据字典 ruleset_source_type",
        "source_ref": "规则集来源引用",
        "snapshot_hash": "规则集内容哈希，用于重复导入检测和审计追溯",
        "rule_count": "规则集中规则数量",
        "page_sampling_policy": "规则集默认页面抽样策略 JSON，见 JSONB 数据字典",
        "loop_guard": "规则集默认 Agent Loop 护栏 JSON，见 JSONB 数据字典",
        "created_by": "创建人用户 ID，IAM 未落地前作为外部用户占位",
        "published_at": "规则集发布时间",
        "archived_at": "规则集归档时间",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_rule": {
        "id": "规则主键",
        "ruleset_id": "所属规则集 ID",
        "rule_code": "规则业务编号，同一规则集内唯一",
        "name": "规则名称",
        "description": "规则说明",
        "category": "规则分类，见数据字典 rule_category",
        "executor_type": "规则执行器类型，见数据字典 judge_strategy",
        "severity": "规则默认严重级别，见数据字典 severity",
        "status": "规则状态，见数据字典 lifecycle_status",
        "scope": "规则适用业务范围，如站点、产品线、租户或页面域",
        "channel": "规则适用提交渠道，见数据字典 channel",
        "page_type": "规则适用页面类型",
        "url_pattern": "规则适用 URL 匹配表达式",
        "steps": "到达检查点所需操作步骤 JSON 数组，见 JSONB 数据字典",
        "input_contract": "规则所需证据输入契约 JSON，见 JSONB 数据字典",
        "check_point": "规则检查点描述",
        "judge_config": "规则判定配置 JSON，见 JSONB 数据字典",
        "manual_review_policy": "人工复核策略 JSON，见 JSONB 数据字典",
        "sort_order": "规则执行排序，数值越小越靠前",
        "created_by": "创建人用户 ID，IAM 未落地前作为外部用户占位",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_task": {
        "id": "任务主键",
        "target_url": "被质检目标 URL",
        "ruleset_id": "任务锁定使用的规则集 ID",
        "channel": "任务提交渠道，见数据字典 channel",
        "status": "任务状态，见数据字典 task_status",
        "priority": "队列优先级，数值越小越优先",
        "submitter_id": "提交人用户 ID，IAM 未落地前作为外部用户占位",
        "task_config": "任务级配置 JSON，见 JSONB 数据字典",
        "page_sampling_policy": "任务级页面抽样策略 JSON，可覆盖规则集默认值",
        "loop_guard": "任务级 Agent Loop 护栏 JSON，可覆盖规则集默认值",
        "locked_by": "当前抢占任务的 Worker 标识",
        "locked_at": "任务被 Worker 抢占的时间",
        "attempt_count": "已尝试执行次数",
        "max_attempts": "最大允许执行次数",
        "next_run_at": "下一次可被队列消费的时间",
        "started_at": "任务开始执行时间",
        "finished_at": "任务结束时间",
        "last_error": "最近一次失败错误摘要",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_page_capture": {
        "id": "页面采集主键",
        "task_id": "所属任务 ID",
        "parent_page_capture_id": "父页面采集 ID，用于表达页面跳转或弹窗来源",
        "page_seq": "任务内页面序号，从 1 开始",
        "page_slug": "业务可读页面标识，如 home、detail-1",
        "url": "采集时页面 URL",
        "page_type": "页面识别类型",
        "page_state": "页面状态，如 loaded、login_required、popup_open",
        "capture_status": "采集状态，见数据字典 page_capture_status",
        "screenshot_path": "页面截图对象存储路径",
        "dom_path": "页面 DOM 对象存储路径",
        "visible_text_path": "可见文本对象存储路径",
        "console_log_path": "浏览器控制台日志对象存储路径",
        "action_log": "到达该页面的动作摘要 JSON 数组",
        "metadata": "页面采集扩展元数据 JSON",
        "captured_at": "页面实际采集完成时间",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_evidence_artifact": {
        "id": "证据产物主键",
        "task_id": "所属任务 ID",
        "page_capture_id": "关联页面采集 ID，可为空",
        "rule_execution_id": "关联规则执行结果 ID，可在规则执行表创建后补外键",
        "artifact_type": "证据产物类型，见数据字典 artifact_type",
        "source": "证据来源，见数据字典 artifact_source",
        "storage_path": "对象存储路径或内部存储引用",
        "content_type": "文件 MIME 类型",
        "checksum": "文件校验值，如 sha256",
        "size_bytes": "文件大小，单位字节",
        "metadata": "证据扩展元数据 JSON",
        "created_at": "创建时间",
    },
    "qc_observation_packet": {
        "id": "Observation Packet 主键",
        "task_id": "所属任务 ID",
        "page_capture_id": "关联页面采集 ID",
        "rule_id": "目标规则 ID",
        "evidence_artifact_ids": "输入证据产物 ID 列表 JSON 数组",
        "packet_payload": "结构化证据摘要 JSON，供 Rule Judge 判定使用",
        "quality_gate_status": "证据质量门禁状态，见数据字典 quality_gate_status",
        "quality_gate_reason": "证据质量门禁说明",
        "created_by_agent_run_id": "创建该证据包的 Agent Run ID",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_rule_execution": {
        "id": "规则执行结果主键",
        "task_id": "所属任务 ID",
        "rule_id": "被执行规则 ID",
        "page_capture_id": "关联页面采集 ID",
        "observation_packet_id": "本次判定使用的 Observation Packet ID",
        "agent_run_id": "产出该规则结论的 Agent Run ID",
        "source_action_trace_id": "产出该规则结论的 Agent 单步动作留痕 ID",
        "execution_status": "规则执行状态，见数据字典 rule_execution_status",
        "judge_strategy": "实际判定策略，见数据字典 judge_strategy",
        "confidence": "判定置信度，取值 0 到 1",
        "severity": "本次结果严重级别，见数据字典 severity",
        "result_summary": "规则执行结果摘要",
        "problem_desc": "问题描述，失败或告警时必填",
        "evidence_artifact_ids": "支撑该结论的证据产物 ID 列表 JSON 数组",
        "judge_input": "判定输入摘要 JSON",
        "judge_output": "判定输出摘要 JSON",
        "review_status": "人工审核状态，见数据字典 review_status",
        "review_reason": "进入人工审核或审核结论说明",
        "error_code": "异常错误码",
        "error_message": "异常错误信息",
        "started_at": "规则执行开始时间",
        "finished_at": "规则执行结束时间",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_review_item": {
        "id": "人工审核项主键",
        "rule_execution_id": "关联规则执行结果 ID",
        "task_id": "所属任务 ID",
        "trigger_reason": "触发人工审核原因，见数据字典 review_trigger_reason",
        "status": "审核状态，见数据字典 review_status",
        "priority": "审核队列优先级，数值越小越优先",
        "reviewer": "审核人用户 ID，IAM 未落地前作为外部用户占位",
        "review_reason": "审核说明或驳回原因",
        "reviewed_at": "审核完成时间",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_agent_run": {
        "id": "Agent Run 主键",
        "task_id": "所属任务 ID",
        "parent_agent_run_id": "父 Agent Run ID，用于多 Agent 委派链路",
        "agent_type": "Agent 类型，见数据字典 agent_type",
        "agent_name": "Agent 实例名称",
        "status": "Agent Run 状态，见数据字典 agent_run_status",
        "input_context": "Agent 启动输入上下文 JSON",
        "output_context": "Agent 结束输出上下文 JSON",
        "working_memory": "Agent 工作记忆 JSON，见 JSONB 数据字典",
        "loop_guard": "Agent Loop 护栏 JSON，见 JSONB 数据字典",
        "stop_reason": "Agent 停止原因，如 completed、max_steps_reached、stuck_detected、timeout、canceled、error",
        "error_message": "Agent 失败错误信息",
        "total_steps": "累计执行步骤数",
        "total_ai_calls": "累计 AI 调用次数",
        "total_tokens": "累计 Token 数",
        "total_cost": "累计成本，币种由成本统计配置约定",
        "started_at": "Agent Run 开始时间",
        "finished_at": "Agent Run 结束时间",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
    "qc_agent_action_trace": {
        "id": "动作留痕主键",
        "agent_run_id": "所属 Agent Run ID",
        "task_id": "所属任务 ID",
        "step_seq": "Agent Run 内步骤序号，从 1 开始递增",
        "phase": "当前执行阶段，如 planning、exploring、judging、reporting",
        "thought_summary": "决策摘要，不保存完整模型思维链",
        "selected_capability": "本步骤选择的 Agent 能力名称",
        "capability_kind": "能力类型，见数据字典 capability_kind",
        "capability_input": "脱敏后的能力输入 JSON",
        "capability_output": "脱敏后的能力输出 JSON",
        "memory_delta": "本步骤对工作记忆的增量影响 JSON",
        "cost": "本步骤成本明细 JSON",
        "latency_ms": "本步骤耗时，单位毫秒",
        "status": "动作执行状态，见数据字典 action_trace_status",
        "error_code": "动作失败错误码",
        "error_message": "动作失败错误信息",
        "created_at": "创建时间",
    },
    "qc_agent_capability_registry": {
        "id": "能力注册记录主键",
        "capability_name": "能力唯一名称",
        "capability_kind": "能力类型，见数据字典 capability_kind",
        "display_name": "能力展示名称",
        "description": "能力功能说明",
        "version": "能力版本号",
        "status": "能力生命周期状态，见数据字典 lifecycle_status",
        "input_schema": "能力输入 JSON Schema",
        "output_schema": "能力输出 JSON Schema",
        "permission_level": "能力权限级别，见数据字典 permission_level",
        "timeout_ms": "能力单次调用超时时间，单位毫秒",
        "retry_policy": "能力重试策略 JSON，见 JSONB 数据字典",
        "owner_team": "能力责任团队",
        "implementation_ref": "能力实现引用：Skill 指向 SKILL.md，Tool 指向 Python handler",
        "created_at": "创建时间",
        "updated_at": "最后更新时间",
    },
}


CAPABILITY_SEEDS = [
    ("task-planning", "skill", "low", "services/api/app/agents/skills/phase/task-planning/SKILL.md"),
    ("rule-matching", "skill", "low", "services/api/app/agents/skills/rule/rule-matching/SKILL.md"),
    ("text-rule", "skill", "low", "services/api/app/agents/skills/rule/text-rule/SKILL.md"),
    ("visual-rule", "skill", "low", "services/api/app/agents/skills/rule/visual-rule/SKILL.md"),
    ("interaction-rule", "skill", "medium", "services/api/app/agents/skills/rule/interaction-rule/SKILL.md"),
    ("ad-rule", "skill", "medium", "services/api/app/agents/skills/rule/ad-rule/SKILL.md"),
    ("payment-rule", "skill", "high_risk", "services/api/app/agents/skills/rule/payment-rule/SKILL.md"),
    ("input-risk-rule", "skill", "medium", "services/api/app/agents/skills/rule/input-risk-rule/SKILL.md"),
    ("player-rule", "skill", "medium", "services/api/app/agents/skills/rule/player-rule/SKILL.md"),
    ("record-result", "tool", "medium", "services/api/app/agents/tools/registry.py"),
]


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _execute(statements: list[str]) -> None:
    for statement in statements:
        op.execute(statement.strip())


def _comment_on_table(table_name: str, comment: str) -> None:
    op.execute(f"COMMENT ON TABLE {table_name} IS {_sql_literal(comment)}")


def _comment_on_column(table_name: str, column_name: str, comment: str) -> None:
    op.execute(f"COMMENT ON COLUMN {table_name}.{column_name} IS {_sql_literal(comment)}")


def _seed_capabilities() -> None:
    for capability_name, capability_kind, permission_level, implementation_ref in CAPABILITY_SEEDS:
        display_name = capability_name.replace("-", " ").title()
        op.execute(
            f"""
            INSERT INTO qc_agent_capability_registry (
                capability_name,
                capability_kind,
                display_name,
                permission_level,
                owner_team,
                implementation_ref
            )
            VALUES (
                {_sql_literal(capability_name)},
                {_sql_literal(capability_kind)},
                {_sql_literal(display_name)},
                {_sql_literal(permission_level)},
                'ai-platform',
                {_sql_literal(implementation_ref)}
            )
            """
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    _execute(TABLES)
    _execute(INDEXES_AND_TRIGGERS)
    _execute(FOREIGN_KEYS)

    for table_name, comment in TABLE_COMMENTS.items():
        _comment_on_table(table_name, comment)
    for table_name, column_comments in COLUMN_COMMENTS.items():
        for column_name, comment in column_comments.items():
            _comment_on_column(table_name, column_name, comment)

    _seed_capabilities()


def downgrade() -> None:
    for table_name in (
        "qc_agent_capability_registry",
        "qc_agent_action_trace",
        "qc_agent_run",
        "qc_review_item",
        "qc_rule_execution",
        "qc_observation_packet",
        "qc_evidence_artifact",
        "qc_page_capture",
        "qc_task",
        "qc_rule",
        "qc_ruleset",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
