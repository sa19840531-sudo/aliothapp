export interface DashboardSummaryCard {
  key: string
  label: string
  value: number
}

export interface DashboardRecentTask {
  task_id: string
  task_name: string
  status: string
  created_at: string | null
}

export interface DashboardStatusCount {
  status: string
  count: number
}

export interface DashboardRiskCount {
  severity: string
  count: number
}

export interface DashboardExecutionStatusCount {
  execution_status: string
  count: number
}

export interface DashboardLatestExecution {
  execution_id: string
  rule_name: string
  execution_status: string
  severity: string | null
  finished_at: string | null
}

export interface DashboardTodayMetrics {
  executed_tasks_today: number
  executed_tasks_yesterday: number
  ignored_rules_today: number
  abnormal_rules_today: number
  qc_success_rate_today: number | null
  qc_success_rate_yesterday: number | null
}

export interface WorkstationDashboardOverview {
  summary_cards: DashboardSummaryCard[]
  today_metrics: DashboardTodayMetrics
  recent_tasks: DashboardRecentTask[]
  status_distribution: DashboardStatusCount[]
  risk_distribution: DashboardRiskCount[]
  execution_status_distribution: DashboardExecutionStatusCount[]
  latest_rule_executions: DashboardLatestExecution[]
  /** 本月本公司任务的模型用量；用量表不可用时为空 */
  token_usage?: DashboardTokenUsage | null
}

export interface DashboardTokenUsage {
  month: string
  total_tokens: number
  total_cost: number
  currency: string
  task_count: number
}
