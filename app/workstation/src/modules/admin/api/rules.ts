import { request } from './http'
import type { JourneyRuleContract } from '@/services/h5-shortflows.api'

export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface RulesetSummary {
  id: string
  ruleset_code: string
  name: string
  version: number
  status: 'draft' | 'enabled' | 'disabled' | 'archived'
  description: string | null
  source_type: 'manual' | 'spreadsheet' | 'import' | 'api'
  rule_count: number
  /** 实时统计：规则集里的全部规则数 / 已开启规则数 */
  total_rule_count?: number | null
  enabled_rule_count?: number | null
  page_sampling_policy?: Record<string, unknown>
  loop_guard?: Record<string, unknown>
  published_at: string | null
  created_at: string
  updated_at: string
}

export interface RuleItem {
  id: string
  ruleset_id: string
  rule_code: string
  name: string
  description: string | null
  category: 'text' | 'visual' | 'interaction' | 'ad' | 'payment' | 'input_risk' | 'player' | 'compliance'
  executor_type: 'llm' | 'rule_engine' | 'hybrid'
  severity: 'low' | 'medium' | 'high' | 'critical'
  status: 'draft' | 'enabled' | 'disabled' | 'archived'
  scope: string | null
  channel: 'workbench' | 'telegram' | 'api' | null
  page_type: string | null
  applicable_page_types: string[]
  url_pattern: string | null
  check_point: string
  steps: Record<string, unknown>[]
  input_contract: Record<string, unknown>
  judge_config: Record<string, unknown>
  manual_review_policy: Record<string, unknown>
  sort_order: number
  created_at: string
  updated_at: string
  /** 跨规则集列表时带上所属规则集 */
  ruleset_code?: string | null
  ruleset_name?: string | null
  ruleset_version?: number | null
  source_type?: RulesetSummary['source_type'] | null
}

/** 业务人员能填的四个字段（2.1 原型）：其余系统字段由后端补齐或原样保留。 */
export interface RuleBusinessPayload {
  name: string
  check_point: string
  applicable_page_types: string[]
  severity: RuleItem['severity']
  evidence_input_scope?: 'page' | 'journey'
  journey_rule_contract?: JourneyRuleContract
}

/** 规则集业务字段：名称、状态、说明。 */
export interface RulesetBusinessPayload {
  name?: string
  description?: string
  status?: RulesetSummary['status']
}

export interface RulesetPayload {
  ruleset_code: string
  name: string
  version: number
  status: RulesetSummary['status']
  description: string
  source_type: RulesetSummary['source_type']
  source_ref: string
  page_sampling_policy: Record<string, unknown>
  loop_guard: Record<string, unknown>
}

export interface RulePayload {
  ruleset_id: string
  rule_code: string
  name: string
  description: string
  category: RuleItem['category']
  executor_type: RuleItem['executor_type']
  severity: RuleItem['severity']
  status: RuleItem['status']
  scope: string
  channel: NonNullable<RuleItem['channel']>
  page_type: string
  applicable_page_types: string[]
  url_pattern: string
  check_point: string
  steps: Record<string, unknown>[]
  input_contract: Record<string, unknown>
  judge_config: Record<string, unknown>
  manual_review_policy: Record<string, unknown>
  sort_order: number
}

export interface RulesetQuery {
  keyword?: string
  status?: RulesetSummary['status'] | 'all'
  page?: number
  page_size?: number
}

export interface RuleQuery {
  ruleset_id?: string
  keyword?: string
  category?: RuleItem['category'] | 'all'
  status?: RuleItem['status'] | 'all'
  severity?: RuleItem['severity']
  source_type?: RulesetSummary['source_type']
  page_type?: string
  page?: number
  page_size?: number
}

function toSearchParams(params: Record<string, string | number | undefined>) {
  const searchParams = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') {
      searchParams.set(key, String(value))
    }
  })
  return searchParams.toString()
}

export async function listRulesets(query: RulesetQuery = {}) {
  const queryString = toSearchParams({
    keyword: query.keyword,
    status: query.status === 'all' ? undefined : query.status,
    page: query.page ?? 1,
    page_size: query.page_size ?? 10,
  })
  return request<PageResult<RulesetSummary>>(`/rulesets?${queryString}`)
}

export async function createRuleset(payload: RulesetPayload) {
  return request<RulesetSummary>('/rulesets', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function updateRuleset(rulesetId: string, payload: Partial<RulesetPayload>) {
  return request<RulesetSummary>(`/rulesets/${rulesetId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export async function deleteRuleset(rulesetId: string) {
  return request<void>(`/rulesets/${rulesetId}`, {
    method: 'DELETE',
  })
}

export async function publishRuleset(rulesetId: string) {
  return request<RulesetSummary>(`/rulesets/${rulesetId}/publish`, {
    method: 'POST',
  })
}

export async function listRules(query: RuleQuery = {}) {
  const queryString = toSearchParams({
    ruleset_id: query.ruleset_id,
    keyword: query.keyword,
    category: query.category === 'all' ? undefined : query.category,
    status: query.status === 'all' ? undefined : query.status,
    severity: query.severity,
    source_type: query.source_type,
    page_type: query.page_type,
    page: query.page ?? 1,
    page_size: query.page_size ?? 10,
  })
  return request<PageResult<RuleItem>>(`/rules?${queryString}`)
}

export async function createRule(payload: RulePayload) {
  return request<RuleItem>('/rules', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function updateRule(ruleId: string, payload: Partial<RulePayload>) {
  return request<RuleItem>(`/rules/${ruleId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

/** 只改名称、详情、适用页面、优先级，系统字段整行保留。 */
export async function patchRule(ruleId: string, payload: RuleBusinessPayload) {
  return request<RuleItem>(`/rules/${ruleId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

/** 开启 / 关闭规则；开启前后端会校验正式任务能不能执行。 */
export async function setRuleStatus(ruleId: string, status: 'enabled' | 'disabled') {
  return request<RuleItem>(`/rules/${ruleId}/status`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })
}

/** 业务新增规则：系统字段自动补齐，默认关闭。 */
export async function createBusinessRule(rulesetId: string, payload: RuleBusinessPayload) {
  return request<RuleItem>(`/rulesets/${rulesetId}/rules`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 改规则集名称、状态、说明；编码、版本、抽样与防死循环策略原样保留。 */
export async function patchRuleset(rulesetId: string, payload: RulesetBusinessPayload) {
  return request<RulesetSummary>(`/rulesets/${rulesetId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

/** 业务新增规则集：只填名称、状态、说明，编码与策略由后端生成。 */
export async function createBusinessRuleset(payload: Required<Pick<RulesetBusinessPayload, 'name'>> & RulesetBusinessPayload) {
  return request<RulesetSummary>('/rulesets', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function deleteRule(ruleId: string) {
  return request<void>(`/rules/${ruleId}`, {
    method: 'DELETE',
  })
}
