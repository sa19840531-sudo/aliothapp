export type TaskStatus =
  | 'queued'
  | 'running'
  | 'canceling'
  | 'review'
  | 'done'
  | 'failed'
  | 'canceled'
export type TaskChannel = 'workbench' | 'telegram' | 'api'
export type ReportStatus = 'not_started' | 'generating' | 'ready' | 'generated' | 'failed' | 'not_applicable'
export type WorkstationTaskAction =
  | 'start'
  | 'edit'
  | 'cancel'
  | 'detail'
  | 'report'
  | 'exploration'
  | 'evidence'
  | 'rerun'
  | 'copy'
  | 'failure'
  | 'retry-finalization'
  | 'delete'

export interface SubmitTaskPayload {
  target_url: string
  ruleset_code: string
  ruleset_version: number
  channel: TaskChannel
  priority: number
  task_config: Record<string, unknown>
  page_sampling_policy: Record<string, unknown>
  loop_guard: Record<string, unknown>
}

export interface SubmitTaskResponse {
  task_id: string
  run_id: string | null
  ruleset_id?: string
  status: TaskStatus | string
  message?: string
}

export interface AppTaskDeviceConfig {
  backend: string
  profile_id: string
}

/** 空 required_page_types = 由系统自动识别页面；其余字段缺省时由后端按默认值或产品登记画像补齐。 */
export interface AppExplorationTargetConfig {
  required_page_types: string[]
  milestones?: string[]
  minimum_business_pages?: number
  device_capability_template?:
    | 'android_visual_readonly_v1'
    | 'android_visual_evidence_v1'
}

export interface H5ExplorationTargetConfig {
  required_page_types: string[]
}

/** Web 页面范围：空数组 = 按站点目录冻结（未知站点按四类）；非空 = 本次必须巡检到的页面类型。 */
export interface WebExplorationTargetConfig {
  required_page_types: string[]
}

export interface AppTaskCreatePayload {
  task_name: string
  platform_build_ref: string
  package_name?: string | null
  device_backend?: string
  device_profile_id?: string
  remark?: string | null
  ruleset_code?: string
  ruleset_version?: number
  channel?: TaskChannel
  priority?: number
}

export interface ProductSnapshot {
  department_name: string
  product_code: string
  product_name: string
}

export interface WorkstationTaskCreatePayload {
  task_name: string
  target_url: string
  ruleset_code: string
  ruleset_version: number
  channel: TaskChannel
  priority: number
  product: ProductSnapshot
  scan_mode: 'full' | 'quick' | 'custom'
  journey_steps: string[]
  remark: string | null
}

export type V2TaskPlatform = 'h5' | 'web' | 'app'
export type V2TaskExecutionMode = 'full_inspection' | 'exploration_only'

export type V2AppSource =
  | {
      type: 'build_ref'
      platform_build_ref: string
    }
  | {
      type: 'download_url'
      download_url: string
    }

export type V2TaskTarget =
  | {
      platform: 'h5'
      url: string
      exploration_target?: H5ExplorationTargetConfig
    }
  | {
      platform: 'web'
      url: string
      /** 仅探索入口锁定服务端公开的浏览器画像；正式入口由服务端填充。 */
      browser_profile_id?: string
      /** 默认由服务端站点目录冻结；strict_four 显式要求四类全部纳入 Gate。 */
      applicability_mode?: 'catalog_declared' | 'strict_four'
      required_page_types?: string[]
      /** 后端只从 exploration_target 读取 Web 页面范围 */
      exploration_target?: WebExplorationTargetConfig
    }
  | {
      platform: 'app'
      source: V2AppSource
      package_name?: string | null
      device: AppTaskDeviceConfig
      exploration_target: AppExplorationTargetConfig
    }

export interface H5ShortflowSelection {
  definition_id: string
  version: number
  content_hash: string
  scenario_key: string
  rule_ids: string[]
  workflow_skill: 'h5-player-workflow' | 'h5-payment-workflow'
}

export interface V2TaskCreatePayload {
  task_name: string
  target: V2TaskTarget
  /** 同时执行的规则集编码（选「全部」就是全部开启的规则集）；都不传时后端按全部开启的规则集执行 */
  ruleset_codes?: string[]
  /** 老调用方只锁单个规则集时用 */
  ruleset_code?: string
  ruleset_version?: number
  channel: TaskChannel
  priority: number
  product: ProductSnapshot
  scan_mode: 'full' | 'quick' | 'custom'
  /** 创建任务页已不再填写用户旅程，缺省即空 */
  journey_steps?: string[]
  h5_shortflows?: H5ShortflowSelection[]
  remark: string | null
}

/**
 * 仅页面探索允许 H5、Web 与 App 共用同一条 V2 入口。
 * 完整质检与探索留证的下游分流由 execution_mode 决定，而不是由前端再维护一套 App 合同。
 */
export interface V2ExplorationTaskCreatePayload
  extends Omit<V2TaskCreatePayload, 'target'> {
  target: V2TaskTarget
}

export interface WorkstationTaskListItem {
  task_id: string
  task_name: string
  product: ProductSnapshot
  /** H5/Web 为 URL；App 任务通常为空 */
  target_url: string | null
  target_platform: V2TaskPlatform
  execution_mode: V2TaskExecutionMode
  journey_steps: string[]
  status: TaskStatus | string
  progress: number
  report_status: ReportStatus | string
  report_availability?: 'none' | 'preview' | 'final'
  failure_reason: string | null
  available_actions: Array<WorkstationTaskAction | string>
  created_at: string
  /** 2.0 原型任务列表：所属公司 / 发起人 / 执行时长（旧后端可能缺省） */
  tenant_name?: string | null
  submitter_name?: string | null
  execution_started_at?: string | null
  execution_finished_at?: string | null
  duration_ms?: number | null
}

export interface WebStageDurations {
  /** null 表示对应阶段尚未完成或权威时间边界尚未落库。 */
  exploration_ms: number | null
  inspection_ms: number | null
  report_generation_ms: number | null
  total_ms: number | null
}

export interface WorkstationTaskDetail extends WorkstationTaskListItem {
  started_at: string | null
  finished_at: string | null
  ruleset_code: string
  ruleset_version: number
  channel: string
  priority: number
  run_id: string | null
  run_status?: string | null
  execution_phase?:
    | 'queued'
    | 'pending'
    | 'running'
    | 'configuration_error'
    | 'navigation_failed'
    | 'evidence_failed'
    | 'classification_review'
    | 'done'
    | 'canceled'
    | null
  queue_position?: number | null
  tasks_ahead?: number | null
  available_worker_count?: number | null
  start_condition?: string | null
  workflow_version?: string | null
  scan_mode: 'full' | 'quick' | 'custom' | string
  remark: string | null
  required_page_types: string[]
  optional_page_types?: string[]
  not_applicable_page_types?: string[]
  applicability_mode?: string | null
  applicability_catalog_version?: string | null
  applicability_catalog_hash?: string | null
  browser_profile_id: string | null
  viewport_width: number | null
  viewport_height: number | null
  /** Web V2 正式闭环的服务端权威执行终态。 */
  execution_status?: string | null
  finalization_state?: string | null
  completion_gate_passed?: boolean
  report_available?: boolean
  coverage_status?: string | null
  coverage_observed_page_types?: string[]
  coverage_missing_page_types?: string[]
  coverage_unrecognized_page_types?: string[]
  coverage_normalization_protocol_version?: string | null
  stage_durations?: WebStageDurations | null
}

export interface WorkstationTaskListResponse {
  items: WorkstationTaskListItem[]
  total: number
  page: number
  page_size: number
}

export interface WorkstationTaskOperationResponse {
  task_id: string
  status: TaskStatus | string
  run_id: string | null
  message: string
}

export interface AgentRunStep {
  step_seq: number
  phase: string | null
  thought_summary: string | null
  selected_capability: string | null
  capability_kind: string | null
  status: string
  latency_ms: number | null
  error_message: string | null
  capability_input: Record<string, unknown>
  capability_output: Record<string, unknown>
}

export interface LLMCostEstimate {
  provider: string
  model: string
  currency: string
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  input_cost: number
  output_cost: number
  cache_hit_cost: number
  total_cost: number
  pricing_unit: string
  pricing_source: string
  pricing_configured: boolean
  applied_pricing_tiers: string[]
}

export interface AgentRunDetail {
  run_id: string
  status: string
  stop_reason: string | null
  error_message: string | null
  total_steps: number
  total_ai_calls: number
  total_tokens: number | null
  input_tokens: number | null
  output_tokens: number | null
  cache_hit_tokens: number | null
  llm_cost_estimate: LLMCostEstimate | null
  final_text: string | null
  started_at: string | null
  finished_at: string | null
  steps: AgentRunStep[]
}

export interface AgentRunEnvelope {
  run: AgentRunDetail | null
}

export interface TaskDetail {
  task_id: string
  target_url: string
  ruleset_id: string
  ruleset_code: string
  ruleset_version: number
  channel: string
  status: TaskStatus | string
  priority: number
  run_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  last_error: string | null
}

export interface TaskListItem {
  task_id: string
  target_url: string
  ruleset_code: string
  ruleset_version: number
  channel: string
  status: TaskStatus | string
  priority: number
  run_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface TaskListResponse {
  items: TaskListItem[]
}

export interface ListTasksParams {
  department_name?: string
  submitter_name?: string
  target_platform?: string
  status?: TaskStatus
  limit?: number
  task_name?: string
  product_name?: string
  product_code?: string
  target_url?: string
  report_status?: ReportStatus
  created_from?: string
  created_to?: string
  page?: number
  page_size?: number
}
