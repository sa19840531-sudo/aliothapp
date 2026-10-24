import { request } from './http'

/** AI 服务与供应商（/llm-providers）。 */

export type ProviderStatus = 'active' | 'standby' | 'restricted' | 'disabled'
export type ProviderHealth = 'healthy' | 'warning' | 'down' | 'unknown'

export interface ProviderMetrics {
  call_count: number
  failed_count: number
  error_rate: number | null
  total_tokens: number
  last_call_at: string | null
}

export interface ProviderItem {
  id: string
  code: string
  name: string
  base_url: string | null
  model: string | null
  priority: number
  weight: number
  breaker_rule: string | null
  status: ProviderStatus
  is_primary: boolean
  api_key_configured: boolean
  managed_by_env: boolean
  quota_remaining_percent: number | null
  health: ProviderHealth
  health_label: string
  last_check_status: string | null
  last_check_latency_ms: number | null
  last_check_message: string | null
  last_checked_at: string | null
  metrics: ProviderMetrics
  created_at: string
  updated_at: string
}

export interface ProviderEvent {
  id: string
  provider_id: string | null
  provider_name: string
  event_type: string
  description: string
  action: string | null
  result: string | null
  operator_name: string | null
  occurred_at: string
}

export interface RoutingSettings {
  auto_failover: boolean
}

export interface AlertPolicy {
  service_down: boolean
  quota_low: boolean
  error_rate: boolean
  latency: boolean
}

export interface ProviderSettings {
  routing: RoutingSettings
  alert_policy: AlertPolicy
  effective_provider_code: string | null
  note: string
}

export interface ProvidersOverview {
  monitor_status: 'normal' | 'warning' | 'down'
  monitor_label: string
  generated_at: string
  providers: ProviderItem[]
  settings: ProviderSettings
  warnings: string[]
  events: ProviderEvent[]
}

export interface ProviderPayload {
  name: string
  base_url?: string | null
  model?: string | null
  priority: number
  weight: number
  breaker_rule?: string | null
  status: ProviderStatus
  quota_remaining_percent?: number | null
}

export interface ConnectionTestResult {
  provider: ProviderItem
  status: string
  latency_ms: number | null
  message: string
}

export function getProvidersOverview() {
  return request<ProvidersOverview>('/llm-providers')
}

export function createProvider(payload: ProviderPayload) {
  return request<ProviderItem>('/llm-providers', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateProvider(providerId: string, payload: ProviderPayload) {
  return request<ProviderItem>(`/llm-providers/${encodeURIComponent(providerId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function setPrimaryProvider(providerId: string) {
  return request<ProviderItem>(`/llm-providers/${encodeURIComponent(providerId)}/primary`, { method: 'POST' })
}

export function testProviderConnection(providerId: string) {
  return request<ConnectionTestResult>(`/llm-providers/${encodeURIComponent(providerId)}/connection-test`, {
    method: 'POST',
  })
}

export function updateProviderSettings(payload: { routing: RoutingSettings; alert_policy: AlertPolicy }) {
  return request<ProviderSettings>('/llm-providers/settings', { method: 'PUT', body: JSON.stringify(payload) })
}

export async function listProviderEvents(limit = 50) {
  const result = await request<{ items: ProviderEvent[] }>(`/llm-providers/events?limit=${limit}`)
  return result.items
}
