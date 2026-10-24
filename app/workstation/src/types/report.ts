import type { PageExplorationSummary } from './task-insight'
import type { WebStageDurations } from './task'

export interface WorkstationReportListItem {
  task_id: string
  task_name: string
  report_status: string
  report_availability: 'none' | 'preview' | 'final'
  report_unavailable_reason?: string | null
  overall_status: string
  attention_required: number | null
  generated_at: string | null
  pending_confirm_count?: number
  /** 以下为 2.0 报告列表页新增：产品、平台与通过率（旧后端可能缺省） */
  department_name?: string | null
  product_code?: string | null
  product_name?: string | null
  target_platform?: string | null
  issue_count?: number | null
  pass_rate?: number | null
}

export interface WorkstationReportListResponse {
  items: WorkstationReportListItem[]
  total: number
  page: number
  page_size: number
}

export interface ReportTaskInfo {
  task_id: string
  target_url: string
  ruleset_code: string
  ruleset_version: number
  status: string
  run_id: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface ReportSummary {
  total_rules: number
  pass: number
  fail: number
  warning: number
  manual_review: number
  evidence_insufficient: number
  skipped: number
  error: number
  attention_required: number
  evaluated_rules: number
  unresolved_candidate_rules?: number
  non_candidate_rules?: number
  confirmed_not_applicable_rules?: number
  coverage_gap_rules?: number
  full_rule_pass_rate?: number
  qc_success_rate?: number
  pass_rate?: number
  candidate_execution_coverage: number | null
  overall_status: string
}

export interface ReportRuleResult {
  execution_id: string
  rule_id: string
  rule_code: string
  rule_name: string
  execution_status: string
  outcome_class?: 'judged' | 'not_applicable' | 'coverage_gap' | 'execution_gap'
  judge_strategy: string | null
  confidence: number | null
  severity: string | null
  result_summary: string | null
  problem_desc: string | null
  evidence_artifact_ids?: string[]
  evidence_input_set_id?: string | null
  missing_evidence?: string[]
  review_status: string | null
  source: string
  started_at: string | null
  finished_at: string | null
}

export interface ReportEvidenceEntry {
  artifact_id?: string
  artifact_type?: string
  title?: string
  source?: string
  storage_path?: string | null
  [key: string]: unknown
}

export interface WorkstationReportDetail {
  report_version: string
  report_stage: 'final' | 'final_review' | 'preview'
  is_final: boolean
  report_availability?: 'none' | 'preview' | 'final'
  export_allowed?: boolean
  incomplete_reasons?: string[]
  termination_reason: string | null
  generated_at: string
  completion_gate?: {
    gate_passed: boolean
    block_reasons?: string[]
    gate_snapshot_id?: string
    finalization_epoch?: number
  }
  task: ReportTaskInfo
  summary: ReportSummary
  page_exploration: PageExplorationSummary | null
  attention_results: ReportRuleResult[]
  rule_results: ReportRuleResult[]
  evidence_entries: ReportEvidenceEntry[]
  stage_durations?: WebStageDurations | null
}
