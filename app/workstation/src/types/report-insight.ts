/** 报告中心读模型（/workstation/reports/{id}/insight）。 */

import type { GreenRuleResultProjection, PageExplorationSummary, RuleDisplayStatus } from './task-insight'

export type HumanVerdict = 'pass' | 'fail' | 'ignored'
/** 报告读侧六类展示态：通过 / 未通过 / 本产品不适用 / 计划外未覆盖 / 检测异常 / 其他。 */
export type ReportDistributionStatus =
  | 'passed'
  | 'failed'
  | 'not_applicable'
  | 'coverage_gap'
  | 'detect_error'
  | 'other'

export interface ReportInsightSummary {
  task_name: string
  task_id: string
  target_url: string
  task_status: string
  target_platform: string
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  generated_at: string | null
  report_stage: 'final' | 'final_review' | 'preview'
  page_recognized_count: number
  /** 真实执行记录次数；与全规则清单条数分开计算。 */
  total_rules: number
  full_rule_pass_rate: number
  qc_success_rate: number
  pass_rate: number
  candidate_execution_coverage: number
  confirmed_not_applicable_rules: number
  coverage_gap_rules: number
  run_id: string | null
  ruleset_code: string | null
  ruleset_version: number | null
  /** 规则集名称；同时跑多个规则集时是「1期质检规则集 + 2期质检规则集」这样的组合名 */
  ruleset_name?: string | null
  /** 规则总数：新建任务时所选规则集里启用的规则条数（较早的任务没记，后端按现在启用的条数给） */
  ruleset_rule_total?: number | null
  rule_inventory_status?: string
  rule_inventory_total?: number
  rule_inventory_pending_rules?: number
  /** 已核对排除项不阻塞所选范围完成；适用性未证实、未执行与缺证不能算完成。 */
  rule_inventory_complete?: boolean
  scope_excluded_rules?: number
  scope_not_applicable_rules?: number
}

export interface ReportDistributionItem {
  status: ReportDistributionStatus
  label: string
  count: number
  percent: number
}

export interface ReportConfirmProgress {
  total: number
  confirmed: number
  pending: number
  completed: boolean
}

export interface ReportRuleItem {
  journey_result?: JourneyRuleResult | null
  /** 仅未封存报告提供当前绿色读投影；封存报告保留原快照。 */
  green_result?: GreenRuleResultProjection | null
  /** 清单候选只有适用性核对或缺口处置，没有规则执行记录。旧接口省略类型时按执行 ID 识别。 */
  item_type?: 'execution' | 'candidate'
  execution_id: string | null
  applicability?: 'applicable' | 'not_applicable' | 'out_of_scope' | 'unproven'
  reason_code?: string | null
  inventory_source?: string | null
  rule_id: string
  rule_code: string
  rule_name: string
  category: string | null
  rule_package: string
  trigger_display: string
  /** 规则检查点：这条规则查什么。 */
  check_point?: string | null
  /** 规则自己所属的规则集（1期 / 2期 / 3期瑞升…），报告按期分组；旧接口没有时为空。 */
  ruleset_code?: string | null
  ruleset_name?: string | null
  bound_page_type: string | null
  bound_page_type_name: string | null
  /** 六类展示态（人工终审优先）；机器细粒度态见 machine_display_status。 */
  display_status: ReportDistributionStatus
  display_status_label: string
  machine_display_status: RuleDisplayStatus
  machine_display_status_label: string
  execution_status: string | null
  /** 原执行原因，用于区分业务复核与模型线路、预算等检测缺口。 */
  execution_reason?: string | null
  severity: string | null
  severity_display: string | null
  confidence: number | null
  result_summary: string | null
  problem_desc: string | null
  evidence_artifact_ids: string[]
  evidence_input_set_id?: string | null
  missing_evidence?: string[]
  page_capture_id: string | null
  is_bootstrap: boolean
  finished_at: string | null
  confirmable: boolean
  human_verdict: HumanVerdict | null
  human_verdict_label: string | null
  human_verdict_note: string | null
  /** 原人工确认账号标识；历史记录未保存时明确显示未记录。 */
  human_verdict_by?: string | null
  human_verdict_at: string | null
  confirm_status: 'pending' | 'confirmed' | null
}

export interface JourneyRuleResult {
  execution_id: string
  input_set_id: string
  execution_plan_id: string
  scenario_key: string
  execution_status: string
  checkpoints: Array<{
    checkpoint_id: string
    status: string
    checks: Record<string, boolean>
    missing: string[]
    integrity: string
    bindings: Array<{
      role: string
      capture_id: string
      action_execution_id: string
      manifest_id: string
      manifest_version: number
      artifact_ids: string[]
    }>
  }>
}

export type ReportExecutionItem = ReportRuleItem & { execution_id: string }

export interface ReportInsight {
  /** 仅未封存绿色报告提供；复核、缺项或关联未完整时禁止导出。 */
  green_results_incomplete?: boolean
  green_results_notice?: string | null
  report_availability?: 'none' | 'preview' | 'final'
  report_unavailable_reason?: string | null
  is_final?: boolean
  export_allowed?: boolean
  incomplete_reasons?: string[]
  web_preview?: {
    trusted_capture_count: number
    inspection_progress: { total: number; completed: number; pending: number }
    page_type_review: { total: number; completed: number; pending: number }
    obstructions: Array<Record<string, unknown>>
    completion_gate: {
      gate_passed: boolean
      report_bound: boolean
      missing_page_types: string[]
      unrecognized_page_types: string[]
      pending_confirmations: number
      gaps: string[]
    }
  }
  task_id: string
  summary: ReportInsightSummary
  qc_success_rate: number
  pass_rate: number
  page_exploration: PageExplorationSummary
  distribution: ReportDistributionItem[]
  confirm_progress: ReportConfirmProgress
  export_ready: boolean
  confirm_items: ReportRuleItem[]
  rule_items: ReportRuleItem[]
  web_logical_pages?: WebLogicalReportPage[]
  web_report_explanation?: import('./web-report-explanation').WebReportExplanation | null
  web_identity_diagnostics?: Array<{ capture_id: string; reason_code: string }>
}

export interface WebLogicalReportPage {
  logical_page_id: string
  canonical_url: string
  menu_entry_ids: string[]
  page_role?: 'home' | 'menu' | 'detail' | null
  capture_count: number
  primary_capture_id: string
  page_type: string | null
  page_type_name: string | null
  classification_status: 'classified' | 'needs_review'
  raw_page_type: string | null
  proposed_page_type: string | null
  observed_page_type: string | null
  expected_page_type: string | null
  conflict_reason_code: string | null
  diagnostic_message: string | null
  confidence: number | null
  review_reason: string | null
  inspection_status: string | null
  evidence_gallery: Array<{
    capture_id: string
    capture_purpose: string
    captured_at: string | null
    screenshot_artifact_id: string | null
    artifact_ids: string[]
    is_primary: boolean
  }>
}

export interface ReportConfirmResult {
  execution_id: string
  human_verdict: HumanVerdict
  human_verdict_label: string
  human_verdict_note: string | null
  human_verdict_by?: string | null
  human_verdict_at: string | null
  review_status: string | null
  task_status: string | null
  rule_code: string
}
