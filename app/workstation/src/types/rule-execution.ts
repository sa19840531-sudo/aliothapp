export type RuleExecutionStatus =
  | 'pass'
  | 'fail'
  | 'warning'
  | 'manual_review'
  | 'evidence_insufficient'
  | 'skipped'
  | 'error'

export type RuleSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info'

export interface RuleExecutionSummary {
  execution_id: string
  rule_id: string
  rule_code: string
  rule_name: string
  page_capture_id: string | null
  execution_status: RuleExecutionStatus | string
  judge_strategy: string
  confidence: number | null
  severity: RuleSeverity | string | null
  result_summary: string | null
  problem_desc: string | null
  review_status: string | null
  started_at: string | null
  finished_at: string | null
  /** 判定来源：agent=Agent 真实写回；bootstrap=任务收尾兜底补记 */
  source: 'agent' | 'bootstrap' | string
}

export interface RuleExecutionListResponse {
  items: RuleExecutionSummary[]
}

export interface CandidateRule {
  rule_id: string
  rule_code: string
  name: string
  category: string
  severity: RuleSeverity | string
  executor_type: string
  page_type: string | null
  applicable_page_types: string[]
  check_point: string
  judge_config: Record<string, unknown>
  sort_order: number
  match_reason: string
}

export interface CandidateRuleListResponse {
  items: CandidateRule[]
}
