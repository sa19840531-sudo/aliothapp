/**
 * 报告「规则执行结果列表」按「几期规则集」分组（2026-09-22 立川：先按几期归类，手风琴点开，
 * 带质检率；点详情看这条规则在这次任务里用了几次、每次的证据和判定）。
 * 接口给的是逐次结果（规则 × 页面），这里合成「期 → 规则 → 每次结果」三层，只做纯计算。
 */
import type { ReportDistributionStatus, ReportRuleItem } from '@/types/report-insight'
import { countsAsQualityExecution, currentReportRuleItem, finalResultOf, isUndecidableResult } from './reportDisplay'
import { pageTypeName } from '@/utils/exploration-labels'

/** 得出结论的最终结果：和报告的质检率（后端 qc_success_rate）同一口径。 */
const CONCLUSIVE: ReadonlySet<ReportDistributionStatus> = new Set([
  'passed',
  'failed',
  'not_applicable',
])

/** 一条规则有多次结果时，行上显示最要紧的那一次：未通过 > 检测异常 > 其他 > 计划外未覆盖 > 通过 > 不适用。 */
const LEAD_ORDER: readonly ReportDistributionStatus[] = [
  'failed',
  'detect_error',
  'other',
  'coverage_gap',
  'passed',
  'not_applicable',
]

export const QC_RATE_HELP =
  '质检率 = 实际规则判定中得出结论（通过、未通过、本产品不适用）的次数 ÷ 实际规则判定总次数。未执行占位、适用性核对和判定前处置不进入分母；真实判定缺证仍计次数，100% 不代表规则集已全部覆盖或全部通过。'

const UNGROUPED_KEY = '__ungrouped__'

export interface RuleRow {
  key: string
  rule_code: string
  rule_name: string
  /** 真实逐次结果及无执行记录的适用性处置，按接口顺序。 */
  items: ReportRuleItem[]
  /** 行上代表这条规则的那一次：最要紧的结果，同样要紧取最早的。 */
  lead: ReportRuleItem
  conclusiveCount: number
  pendingCount: number
  confirmedCount: number
  /** 仅真实执行记录计次数，清单候选不计。 */
  executionCount: number
  pageNames: string[]
}

export interface PhaseGroup {
  key: string
  name: string
  rules: RuleRow[]
  executionCount: number
  conclusiveCount: number
  failedRuleCount: number
  pendingRuleCount: number
}

export interface RuleSummary {
  /** 全部清单规则条数，一条规则只算一条。 */
  ruleCount: number
  usedRuleCount: number
  executionCount: number
  /** 每条规则按行上处置归一类，六类合计等于全部清单规则条数。 */
  statusCounts: Record<ReportDistributionStatus, number>
}

export function isConclusive(item: ReportRuleItem): boolean {
  return countsAsQualityExecution(item) && !isUndecidableResult(item) && item.confirm_status !== 'pending' && CONCLUSIVE.has(item.display_status)
}

function leadRank(item: ReportRuleItem): number {
  const rank = LEAD_ORDER.indexOf(item.display_status)
  return rank < 0 ? LEAD_ORDER.length : rank
}

function ruleKey(item: ReportRuleItem): string {
  return item.rule_id || item.rule_code
}

/** 所属页面只使用实际绑定的采集页型，不拿适用范围当作执行页面。 */
export function rulePageName(item: ReportRuleItem): string {
  if (item.bound_page_type_name?.trim()) return item.bound_page_type_name.trim()
  if (item.bound_page_type?.trim()) return pageTypeName(item.bound_page_type.trim())
  return item.page_capture_id ? '页面未识别' : '未关联具体页面'
}

/** 逐次结果按规则合成一行一条规则。 */
export function buildRuleRows(items: ReportRuleItem[]): RuleRow[] {
  const byRule = new Map<string, ReportRuleItem[]>()
  for (const source of items) {
    const item = currentReportRuleItem(source)
    const key = ruleKey(item)
    byRule.set(key, [...(byRule.get(key) ?? []), item])
  }
  return [...byRule.entries()].map(([key, grouped]) => {
    // 后端只在本轮无真实执行时提供此正式处置；旧轮次执行保留在明细和次数中。
    const aggregateDisposition = grouped.find((item) => item.item_type === 'candidate'
      && !item.execution_id
      && item.reason_code === 'AGGREGATE_RULE_SCOPE_NOT_APPLICABLE'
      && item.applicability === 'not_applicable'
      && item.machine_display_status === 'not_applicable'
      && item.display_status === 'not_applicable')
    const scopeDisposition = !grouped.some(countsAsQualityExecution)
      ? grouped.find((item) => item.item_type === 'candidate' && !item.execution_id
        && ((item.applicability === 'not_applicable' && item.display_status === 'not_applicable')
          || (item.applicability === 'out_of_scope' && item.display_status === 'coverage_gap')))
      : undefined
    const lead = aggregateDisposition ?? scopeDisposition
      ?? grouped.reduce((best, item) => (leadRank(item) < leadRank(best) ? item : best))
    const pageNames = [
      ...new Set(
        grouped
          .map(rulePageName),
      ),
    ]
    return {
      key,
      rule_code: lead.rule_code,
      rule_name: lead.rule_name,
      items: grouped,
      lead,
      conclusiveCount: grouped.filter(isConclusive).length,
      pendingCount: grouped.filter((item) => item.confirm_status === 'pending').length,
      confirmedCount: grouped.filter((item) => item.confirm_status === 'confirmed').length,
      executionCount: grouped.filter(countsAsQualityExecution).length,
      pageNames,
    }
  })
}

/** 规则按所属规则集（几期）分组；1 期、2 期、3 期按名称里的数字排，没标规则集的放最后。 */
export function buildPhaseGroups(rows: RuleRow[]): PhaseGroup[] {
  const byPhase = new Map<string, { name: string | null; rules: RuleRow[] }>()
  for (const row of rows) {
    const code = row.lead.ruleset_code || null
    const key = code ?? UNGROUPED_KEY
    const entry = byPhase.get(key) ?? { name: row.lead.ruleset_name || code, rules: [] }
    entry.rules.push(row)
    byPhase.set(key, entry)
  }
  const onlyUngrouped = byPhase.size === 1 && byPhase.has(UNGROUPED_KEY)
  return [...byPhase.entries()]
    .map(([key, entry]) => ({
      key,
      name: entry.name || (onlyUngrouped ? '全部规则' : '其他规则'),
      rules: entry.rules,
      executionCount: entry.rules.reduce((sum, row) => sum + row.executionCount, 0),
      conclusiveCount: entry.rules.reduce((sum, row) => sum + row.conclusiveCount, 0),
      failedRuleCount: entry.rules.filter((row) => row.lead.display_status === 'failed').length,
      pendingRuleCount: entry.rules.filter((row) => row.pendingCount > 0).length,
    }))
    .sort((a, b) => {
      if (a.key === UNGROUPED_KEY || b.key === UNGROUPED_KEY) {
        return Number(a.key === UNGROUPED_KEY) - Number(b.key === UNGROUPED_KEY)
      }
      return a.name.localeCompare(b.name, 'zh-CN', { numeric: true })
    })
}

/**
 * 「规则结果汇总」按规则条数、和规则列表同一口径（2026-09-24 立川）：一条规则只算一次，
 * 判了多次时只要有一次未通过就算未通过，其余按行上最要紧的那一次归类。
 */
export function summarizeRules(rows: RuleRow[]): RuleSummary {
  const statusCounts = Object.fromEntries(LEAD_ORDER.map((status) => [status, 0])) as Record<
    ReportDistributionStatus,
    number
  >
  for (const row of rows) {
    statusCounts[row.lead.display_status] += 1
  }
  return {
    ruleCount: rows.length,
    usedRuleCount: rows.filter((row) => row.executionCount > 0).length,
    executionCount: rows.reduce((sum, row) => sum + row.executionCount, 0),
    statusCounts,
  }
}

/** 百分比最多一位小数，没有次数时写「-」。 */
export function rateText(done: number, total: number): string {
  if (!total) {
    return '-'
  }
  return `${Math.round((done / total) * 1000) / 10}%`
}

/** 「未通过 1 次、通过 2 次」：按最终结果的说法数每一次，要紧的在前。 */
export function resultBreakdown(items: ReportRuleItem[]): Array<{ label: string; count: number }> {
  const counts = new Map<string, { count: number; rank: number }>()
  for (const item of items) {
    const label = finalResultOf(item).label
    const entry = counts.get(label) ?? { count: 0, rank: leadRank(item) }
    entry.count += 1
    entry.rank = Math.min(entry.rank, leadRank(item))
    counts.set(label, entry)
  }
  return [...counts.entries()]
    .sort((a, b) => a[1].rank - b[1].rank)
    .map(([label, entry]) => ({ label, count: entry.count }))
}

/** 详情弹窗顶上那句：一共用了几次、各是什么结果、质检率。 */
export function usageSummaryText(row: RuleRow): string {
  if (!row.executionCount) return `未进行规则判定。${row.lead.result_summary || row.lead.problem_desc || '尚未完成规则判定。'}`
  const breakdown = resultBreakdown(row.items.filter(countsAsQualityExecution))
    .map((entry) => `${entry.label} ${entry.count} 次`)
    .join('、')
  const total = row.executionCount
  return `一共用了 ${total} 次：${breakdown}。质检率 ${rateText(row.conclusiveCount, total)}（${row.conclusiveCount} 次得出结论）。`
}

/** 「所属页面」列：三个及以上只写第一个和总数，完整列表在悬停提示和详情弹窗里。 */
export function pageNamesText(names: string[]): string {
  if (!names.length) {
    return '未关联具体页面'
  }
  return names.length > 2 ? `${names[0]}等 ${names.length} 个页面` : names.join('、')
}
