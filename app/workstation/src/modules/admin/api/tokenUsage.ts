import { downloadFile } from '@/services/http'
import { request } from './http'

/** 部门 Token 消耗与费用（/cost-reports/departments）。 */

export type BudgetLevel = 'none' | 'normal' | 'warning' | 'critical' | 'over'

export interface DepartmentCostRow {
  tenant_id: string
  tenant_name: string
  department_name: string
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  total_tokens: number
  ai_calls: number
  task_count: number
  total_cost: number
  main_provider: string | null
  main_model: string | null
  monthly_budget: number | null
  budget_used_percent: number | null
  budget_level: BudgetLevel
}

export interface PriceVersion {
  input_price: number
  output_price: number
  cache_hit_price: number
  pricing_source: string
  pricing_configured: boolean
  currency: string
  effective_from: string
  last_seen_at: string
  event_count: number
}

export interface DepartmentCostTotals {
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  total_tokens: number
  ai_calls: number
  task_count: number
  total_cost: number
  unpriced_calls: number
  currency: string
}

export interface DepartmentCostReport {
  month: string
  month_label: string
  start: string
  end: string
  generated_at: string
  provider: string | null
  providers: string[]
  months: string[]
  totals: DepartmentCostTotals
  previous_totals: DepartmentCostTotals
  token_change_percent: number | null
  cost_change_percent: number | null
  alert_department_count: number
  over_90_department_count: number
  rows: DepartmentCostRow[]
  current_price: PriceVersion | null
  price_history: PriceVersion[]
}

export interface DepartmentCostTaskRow {
  task_id: string
  task_name: string
  department_name: string
  provider: string | null
  model: string | null
  ai_calls: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  total_cost: number
  first_call_at: string | null
  last_call_at: string | null
}

export interface DepartmentCostQuery {
  month?: string
  provider?: string
  tenant_id?: string
  department_name?: string
}

function toQuery(params: DepartmentCostQuery): string {
  const search = new URLSearchParams()
  if (params.month) search.set('month', params.month)
  if (params.provider) search.set('provider', params.provider)
  if (params.tenant_id) search.set('tenant_id', params.tenant_id)
  if (params.department_name) search.set('department_name', params.department_name)
  const text = search.toString()
  return text ? `?${text}` : ''
}

export function getDepartmentCostReport(params: DepartmentCostQuery = {}) {
  return request<DepartmentCostReport>(`/cost-reports/departments${toQuery(params)}`)
}

export async function getDepartmentCostDetail(params: DepartmentCostQuery = {}) {
  const result = await request<{ items: DepartmentCostTaskRow[] }>(`/cost-reports/departments/detail${toQuery(params)}`)
  return result.items
}

export function exportDepartmentCostReport(params: DepartmentCostQuery = {}) {
  return downloadFile(
    `/cost-reports/departments/export${toQuery(params)}`,
    `department-token-cost-${params.month ?? 'current'}.csv`,
  )
}
