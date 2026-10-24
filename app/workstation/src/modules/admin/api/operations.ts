import { request } from './http'

/** 运营总览 / 使用分析（/operations/overview、/operations/analytics）。 */

export type OperationsRange = '7d' | '30d' | '90d' | 'quarter' | 'year'

export interface OperationsSummary {
  task_count: number
  report_count: number
  report_success_rate: number | null
  failure_rate: number | null
  done_count: number
  review_count: number
  failed_count: number
  canceled_count: number
  active_count: number
  active_tenant_count: number
  active_account_count: number
  avg_duration_seconds: number | null
}

export interface TrendPoint {
  date: string
  task_count: number
  report_count: number
  failed_count: number
}

export interface PlatformShare {
  platform: string
  task_count: number
}

export interface TenantUsageRow {
  tenant_id: string
  tenant_name: string
  status: string
  product_count: number
  active_account_count: number
  task_count: number
  report_count: number
  report_success_rate: number | null
  failure_rate: number | null
  last_used_at: string | null
  usage_status: 'active' | 'idle'
}

export interface OperationsOverview {
  range: OperationsRange
  start: string
  end: string
  tenant_id: string | null
  platform: string | null
  generated_at: string
  summary: OperationsSummary
  previous: OperationsSummary
  trend: TrendPoint[]
  platform_share: PlatformShare[]
  tenants: TenantUsageRow[]
}

export interface OperationsQuery {
  range?: OperationsRange
  tenant_id?: string
  platform?: string
}

function toQuery(params: OperationsQuery): string {
  const search = new URLSearchParams()
  if (params.range) search.set('range', params.range)
  if (params.tenant_id) search.set('tenant_id', params.tenant_id)
  if (params.platform) search.set('platform', params.platform)
  const text = search.toString()
  return text ? `?${text}` : ''
}

export function getOperationsOverview(params: OperationsQuery = {}) {
  return request<OperationsOverview>(`/operations/overview${toQuery(params)}`)
}

export function getOperationsAnalytics(params: OperationsQuery = {}) {
  return request<OperationsOverview>(`/operations/analytics${toQuery(params)}`)
}
