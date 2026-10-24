import { request } from './http'

export interface CostBucket {
  key: string
  run_count: number
  ai_calls: number
  usage_missing_count: number
  total_tokens: number
  total_cost: number
}

export interface CostRunItem {
  run_id: string
  task_id: string | null
  task_name: string | null
  task_status: string | null
  agent_type: string
  agent_name: string
  status: string
  provider: string
  model: string
  currency: string
  ai_calls: number
  usage_missing_count: number
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  billable_input_tokens: number
  total_tokens: number
  input_cost: number
  output_cost: number
  cache_hit_cost: number
  total_cost: number
  pricing_source: string
  pricing_configured: boolean
  applied_pricing_tiers: string[]
  pricing_breakdown: Record<string, unknown>[]
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface CostTaskItem {
  task_id: string | null
  task_name: string | null
  status: string
  currency: string
  run_count: number
  latest_run_id: string | null
  latest_run_status: string
  latest_run_created_at: string
  provider: string
  model: string
  ai_calls: number
  usage_missing_count: number
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  billable_input_tokens: number
  total_tokens: number
  input_cost: number
  output_cost: number
  cache_hit_cost: number
  total_cost: number
  pricing_source: string
  applied_pricing_tiers: string[]
  pricing_breakdown: Record<string, unknown>[]
}

export interface CostReport {
  currency: string
  days: number
  generated_at: string
  run_count: number
  completed_run_count: number
  failed_run_count: number
  ai_calls: number
  usage_missing_count: number
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  billable_input_tokens: number
  total_tokens: number
  input_cost: number
  output_cost: number
  cache_hit_cost: number
  total_cost: number
  average_run_cost: number
  average_call_cost: number
  by_day: CostBucket[]
  by_model: CostBucket[]
  by_status: CostBucket[]
  task_items: CostTaskItem[]
  recent_runs: CostRunItem[]
}

export interface CostUsageEventItem {
  event_id: string
  scope_type: string
  task_id: string | null
  task_name: string | null
  agent_run_id: string | null
  agent_type: string | null
  agent_name: string | null
  run_status: string | null
  source: string
  operation: string | null
  provider: string
  model: string
  api_style: string
  usage_available: boolean
  ai_calls: number
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  billable_input_tokens: number
  total_tokens: number
  input_cost: number
  output_cost: number
  cache_hit_cost: number
  total_cost: number
  currency: string
  pricing_source: string
  pricing_unit: string
  pricing_configured: boolean
  pricing_tier: string | null
  input_price_per_million_tokens: number
  output_price_per_million_tokens: number
  cache_hit_price_per_million_tokens: number
  calculation_formula: string
  created_at: string
}

export interface CostUsageEventReport {
  currency: string
  days: number
  generated_at: string
  event_count: number
  task_event_count: number
  debug_event_count: number
  unbound_event_count: number
  ai_calls: number
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  billable_input_tokens: number
  total_tokens: number
  input_cost: number
  output_cost: number
  cache_hit_cost: number
  total_cost: number
  usage_missing_count: number
  by_scope: CostBucket[]
  by_source: CostBucket[]
  by_model: CostBucket[]
  recent_events: CostUsageEventItem[]
}

export async function getLlmCostReport(params: { days?: number; limit?: number } = {}) {
  const searchParams = new URLSearchParams()
  searchParams.set('days', String(params.days ?? 30))
  searchParams.set('limit', String(params.limit ?? 50))
  return request<CostReport>(`/cost-reports/llm-runs?${searchParams.toString()}`)
}

export async function getLlmUsageEventReport(params: { days?: number; limit?: number } = {}) {
  const searchParams = new URLSearchParams()
  searchParams.set('days', String(params.days ?? 30))
  searchParams.set('limit', String(params.limit ?? 100))
  return request<CostUsageEventReport>(`/cost-reports/llm-events?${searchParams.toString()}`)
}
