import type { CatalogPageType, CatalogRule, CatalogRuleset } from '@/services/ruleCatalog.api'

/**
 * 规则集 / 页面范围多选的共用口径（创建任务、快速质检共用）。
 * 选中值是字符串数组，其中 PICKER_ALL 代表「全部」：默认就是全部，与选项有没有加载完无关。
 */
export const PICKER_ALL = '__all__'

export const RULESET_REQUIRED_MESSAGE = '请选择规则集'
export const NO_RULESET_MESSAGE = '暂无启用的规则集，请联系运营中心'

export interface PickerOption {
  value: string
  label: string
}

/** 默认选中「全部」；每次返回新数组，避免多处共用同一个引用。 */
export function allSelection(): string[] {
  return [PICKER_ALL]
}

export function isAllSelected(selection: readonly string[]): boolean {
  return selection.includes(PICKER_ALL)
}

/** 实际勾选的选项（按选项顺序）：「全部」展开成全部选项，已经不存在的旧值忽略。 */
export function selectedValues(
  selection: readonly string[],
  optionValues: readonly string[],
): string[] {
  if (isAllSelected(selection)) return [...optionValues]
  return optionValues.filter((value) => selection.includes(value))
}

/** 勾选「全部」即全选，取消「全部」即全不选。 */
export function toggleAll(checked: boolean): string[] {
  return checked ? allSelection() : []
}

/** 勾选或取消单个选项：取消任意一项就退出「全部」，逐项勾满后自动回到「全部」。 */
export function toggleOption(
  selection: readonly string[],
  value: string,
  checked: boolean,
  optionValues: readonly string[],
): string[] {
  const picked = new Set(selectedValues(selection, optionValues))
  if (checked) {
    picked.add(value)
  } else {
    picked.delete(value)
  }
  const next = optionValues.filter((item) => picked.has(item))
  return optionValues.length > 0 && next.length === optionValues.length ? allSelection() : next
}

export function rulesetOptionLabel(ruleset: CatalogRuleset): string {
  return `${ruleset.name} · ${ruleset.enabled_rule_count} 条规则`
}

/** 规则集选择落到具体规则集：「全部」= 目录里全部启用的规则集。 */
export function resolveRulesets(
  selection: readonly string[],
  rulesets: readonly CatalogRuleset[],
): CatalogRuleset[] {
  if (isAllSelected(selection)) return [...rulesets]
  return rulesets.filter((ruleset) => selection.includes(ruleset.ruleset_id))
}

/** 规则集可以多选、一起执行；目录里没有开启的规则集、或一个都没选时不能提交。 */
export function rulesetSelectionError(
  resolved: readonly CatalogRuleset[],
  available: readonly CatalogRuleset[],
): string | null {
  if (!available.length) return NO_RULESET_MESSAGE
  if (!resolved.length) return RULESET_REQUIRED_MESSAGE
  return null
}

/** 所选规则集里的启用规则：按规则编码去重，保持规则目录里的顺序。 */
export function rulesOfRulesets(
  rulesets: readonly CatalogRuleset[],
  rules: readonly CatalogRule[],
): CatalogRule[] {
  const keys = new Set(rulesets.map((ruleset) => `${ruleset.ruleset_code}@${ruleset.version}`))
  const seen = new Set<string>()
  return rules.filter((rule) => {
    if (!keys.has(`${rule.ruleset_code}@${rule.ruleset_version}`) || seen.has(rule.rule_code)) {
      return false
    }
    seen.add(rule.rule_code)
    return true
  })
}

/** 页面范围选项用页面类型名称展示；目录里查不到名称时退回编码。 */
export function pageScopeOptions(
  codes: readonly string[],
  pageTypes: readonly CatalogPageType[],
): PickerOption[] {
  const names = new Map(pageTypes.map((pageType) => [pageType.code, pageType.name]))
  return codes.map((code) => ({ value: code, label: names.get(code) || code }))
}

/**
 * 页面范围落到提交值：选「全部」或一个都没选 → 空数组，由系统自动识别页面；
 * 只选部分 → 这几类页面（按选项顺序），任务必须巡检到它们。永远不显式提交全量列表。
 */
export function resolvePageScope(
  selection: readonly string[],
  codes: readonly string[],
): string[] {
  if (isAllSelected(selection)) return []
  const picked = selectedValues(selection, codes)
  return picked.length === codes.length ? [] : picked
}
