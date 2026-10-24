import { requestJson } from './http'

/**
 * 已发布规则目录（GET /workstation/rule-catalog）：部门规则中心三个页签、
 * 创建任务 / 快速质检的规则集与页面范围选择器共用这一份口径。
 */

/** 启用中的规则集；只有开启的规则集会进入任务选择范围。 */
export interface CatalogRuleset {
  ruleset_id: string
  ruleset_code: string
  version: number
  name: string
  description: string
  status: string
  enabled_rule_count: number
  rule_codes: string[]
}

/** 启用规则集里的启用规则。 */
export interface CatalogRule {
  rule_id: string
  /** 精确关联该期规则集；旧目录没有此字段时仍可按编码和版本核对。 */
  ruleset_id?: string | null
  rule_code: string
  name: string
  category: string
  severity: string
  executor_type: string
  check_point: string
  applicable_page_types: string[]
  /** 真实规则集导入来源，未记录时为空，不能按执行器猜测。 */
  source_type?: string | null
  ruleset_code: string
  ruleset_version: number
}

/** 页面识别目录里的页面类型，附关联的已发布规则。 */
export interface CatalogPageType {
  code: string
  name: string
  category: string
  rule_matchable: boolean
  coverage_required: boolean
  description: string
  related_rule_codes: string[]
}

/** 各平台提交任务时后端认可的页面类型编码（H5 / App 为业务页型，Web 为四类）。 */
export interface PageScopeOptions {
  h5: string[]
  web: string[]
  app: string[]
}

export interface RuleCatalog {
  rulesets: CatalogRuleset[]
  rules: CatalogRule[]
  page_types: CatalogPageType[]
  page_scope_options: PageScopeOptions
}

export function getRuleCatalog(): Promise<RuleCatalog> {
  return requestJson<RuleCatalog>('/workstation/rule-catalog')
}
