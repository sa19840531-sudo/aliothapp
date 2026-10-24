import { requestJson } from './http'

export interface PageBoundRule {
  rule_code: string
  name: string
  severity: string
}

export interface PageConfigItem {
  code: string
  name: string
  status: string
  description: string
  feature_count: number
  bound_rule_count: number
  bound_rule_codes: string[]
  /** 页面识别配置详情（全部来自页面识别目录） */
  category: string
  coverage_required: boolean
  rule_matchable: boolean
  purpose: string
  aliases: string[]
  ocr_keywords: string[]
  dom_hints: string[]
  layout_hints: string[]
  entry_hints: string[]
  exit_hints: string[]
  next_page_types: string[]
  risk_actions: string[]
  route_prefixes: string[]
  navigation_aliases: string[]
  default_detail_page_type: string | null
  source: string
  remark: string
  bound_rules: PageBoundRule[]
}

export interface PageConfigStats {
  page_type_total: number
  supported_total: number
  feature_total: number
  enabled_rule_total: number
  rule_binding_total: number
  covered_page_total: number
}

export interface WorkstationPageConfig {
  catalog_version: string
  /** 为空 = 按全部已发布规则集统计 */
  ruleset_code: string | null
  ruleset_version: number | null
  stats: PageConfigStats
  pages: PageConfigItem[]
}

export function getPageConfig(): Promise<WorkstationPageConfig> {
  return requestJson<WorkstationPageConfig>('/workstation/page-config')
}
