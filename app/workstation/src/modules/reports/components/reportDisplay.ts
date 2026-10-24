import { countsAsQualityExecution } from '@/utils/rule-execution'
export { countsAsQualityExecution } from '@/utils/rule-execution'
import type { ReportDistributionStatus, ReportExecutionItem, ReportRuleItem } from '@/types/report-insight'
import { greenResultStatus, isLinkedGreenResult } from '@/modules/tasks/components/greenRuleResultDetails'
import {
  RULE_EXECUTION_STATUS_LABELS,
  severityClass,
  severityLabel,
  type ReportState,
} from '@/utils/status'

/** 报告状态三态配色（2.1 原型：成功绿 / 生成中蓝 / 生成失败红），报告列表与报告详情共用。 */
export const REPORT_STATE_TONES: Record<ReportState, string> = {
  success: 'tone-success',
  generating: 'tone-primary',
  failed: 'tone-danger',
  none: 'tone-muted',
}

/** 单条规则的六类结果：文案、标签配色、汇总瓦片配色与「!」状态说明（说明文字取自 2.1 原型）。 */
export interface ResultGroupMeta {
  key: ReportDistributionStatus
  label: string
  tone: string
  tile: string
  help: string
}

export const RESULT_GROUPS: readonly ResultGroupMeta[] = [
  {
    key: 'passed',
    label: '通过',
    tone: 'tone-success',
    tile: 'tile-success',
    help: '规则检查通过，未发现需要处理的问题。',
  },
  {
    key: 'failed',
    label: '未通过',
    tone: 'tone-danger',
    tile: 'tile-danger',
    help: '规则检查未通过，页面存在需要关注或修复的问题。',
  },
  {
    key: 'not_applicable',
    label: '本产品不适用',
    tone: 'tone-muted',
    tile: 'tile-neutral',
    help: '已核对规则不适用于当前平台、渠道或产品场景。适用性核对不等同于进行过 AI 规则判定。',
  },
  {
    key: 'coverage_gap',
    label: '计划外未覆盖',
    tone: 'tone-warning',
    tile: 'tile-warning',
    help: '页面或场景不在当前规则覆盖范围内，未形成有效结论。',
  },
  {
    key: 'detect_error',
    label: '检测异常',
    tone: 'tone-orange',
    tile: 'tile-orange',
    help: '执行过程中发生异常，当前结果不能作为完整结论。',
  },
  {
    key: 'other',
    label: '其他',
    tone: 'tone-violet',
    tile: 'tile-violet',
    help: '未执行、证据不足或待业务复核等没有确定结论的规则。缺口不会计为通过、未通过或本产品不适用。',
  },
]

const OTHER_GROUP = RESULT_GROUPS[RESULT_GROUPS.length - 1]
const GROUP_BY_KEY = new Map(RESULT_GROUPS.map((meta) => [meta.key, meta] as const))

/**
 * 机器细粒度结果 → 六类，与后端 report_insight._display_group 同口径：
 * 历史 not_detected 与确认不适用映射为「本产品不适用」；未执行、缺证和转人工归「其他」。
 * 六类键本身（最终结果 display_status）原样返回。
 */
const MACHINE_STATUS_GROUPS: Record<string, ReportDistributionStatus> = {
  passed: 'passed',
  failed: 'failed',
  not_detected: 'not_applicable',
  not_applicable: 'not_applicable',
  coverage_gap: 'coverage_gap',
  detect_error: 'detect_error',
}

function greenReviewPending(item: ReportRuleItem): boolean {
  return item.green_result?.review_status === 'pending' || item.green_result?.review_status === 'needs_recheck'
}

function greenHasMaterialGap(item: ReportRuleItem): boolean {
  const green = item.green_result!
  return green.execution_status === 'evidence_insufficient' || green.missing_evidence.length > 0
    || green.subitems.some((part) => !part.evaluated || part.status === 'evidence_insufficient' || part.missing_evidence.length > 0)
}

/** 只对服务端附带当前绿色投影的记录收紧展示，封存快照与旧规则不变。 */
export function currentReportRuleItem(item: ReportRuleItem): ReportRuleItem {
  const green = item.green_result
  if (!green) return item
  if (!isLinkedGreenResult(green)) {
    return { ...item, display_status: 'other', display_status_label: '关联待核对', confirmable: false, confirm_status: null }
  }
  if (green.execution_status === 'fail' && (greenHasMaterialGap(item) || greenReviewPending(item))) {
    return { ...item, display_status: 'failed', display_status_label: '未通过', confirmable: false, confirm_status: null }
  }
  if (greenHasMaterialGap(item) || (greenReviewPending(item) && green.execution_status === 'pass')) {
    return { ...item, display_status: 'other', display_status_label: greenHasMaterialGap(item) ? '证据不足' : '待人工复核',
      confirmable: false, confirm_status: null }
  }
  if (green.execution_status === 'manual_review' && greenReviewPending(item)) {
    return { ...item, display_status: 'other', display_status_label: '待人工复核' }
  }
  return item
}

export function resultGroupOf(status: string | null | undefined): ResultGroupMeta {
  const key = status ? MACHINE_STATUS_GROUPS[status] : undefined
  return (key && GROUP_BY_KEY.get(key)) || OTHER_GROUP
}

/** 子状态：机器细粒度文案；与六类文案重复时退到执行态文案，仍重复就不显示。 */
export function subStatusText(item: ReportRuleItem): string | null {
  const groupLabel = resultGroupOf(item.machine_display_status).label
  const executionLabel = item.execution_status
    ? RULE_EXECUTION_STATUS_LABELS[item.execution_status]
    : undefined
  return (
    [item.machine_display_status_label, executionLabel].find(
      (text): text is string => !!text && text !== groupLabel,
    ) ?? null
  )
}

/** 「AI 结果 · 子状态」：展开详情的原始执行态与人工确认卡片共用。 */
export function rawResultText(item: ReportRuleItem): string {
  if (!isExecutionReportItem(item)) return '无执行记录'
  const groupLabel = resultGroupOf(item.machine_display_status).label
  const sub = subStatusText(item)
  return sub ? `${groupLabel} · ${sub}` : groupLabel
}

/** 表格「AI 判定」：机器结果的六类文案；「其他」带上子状态，例如「其他 · 证据不足」。 */
export function aiResultOf(item: ReportRuleItem): { label: string; tone: string } {
  if (item.green_result) {
    const status = greenResultStatus(item.green_result)
    return { label: status.label, tone: status.className === 'is-fail' ? 'tone-danger'
      : status.className === 'is-pass' ? 'tone-success' : 'tone-muted' }
  }
  if (!countsAsQualityExecution(item)) return { label: '未进行 AI 判定', tone: 'tone-muted' }
  const group = resultGroupOf(item.machine_display_status)
  if (isUndecidableResult(item)) {
    return { label: item.machine_display_status_label || '无法判定', tone: group.tone }
  }
  return { label: group.key === 'other' ? rawResultText(item) : group.label, tone: group.tone }
}

/** 原始执行记录保留审计 ID，是否实际判定另按共享口径核对。 */
export function isExecutionReportItem(item: ReportRuleItem): item is ReportExecutionItem {
  return item.item_type !== 'candidate' && typeof item.execution_id === 'string' && item.execution_id.length > 0
}

/** 缺口与检测故障没有业务结论，保留原始执行事实，不使用历史人工判定补成完成。 */
export function isUndecidableResult(item: ReportRuleItem): boolean {
  if (item.green_result) {
    if (!isLinkedGreenResult(item.green_result)) return true
    return greenHasMaterialGap(item)
      || (greenReviewPending(item) && item.green_result.execution_status !== 'manual_review')
      || !['pass', 'fail', 'skipped', 'manual_review'].includes(item.green_result.execution_status || '')
      || (item.green_result.execution_status === 'manual_review' && !item.confirmable
        && !['passed', 'failed', 'not_applicable'].includes(item.display_status))
  }
  return ['not_executed', 'coverage_gap', 'detect_error', 'evidence_insufficient', 'pending', 'running'].includes(
    item.machine_display_status,
  ) || (item.machine_display_status === 'manual_review' && !item.confirmable)
}

/** 执行状态单列展示，与通过、未通过等业务结论分开。 */
export function executionStateText(item: ReportRuleItem): string {
  if (item.green_result) {
    if (!isLinkedGreenResult(item.green_result)) return '关联待核对'
    if (item.green_result.execution_status === 'evidence_insufficient') return '无法判定 · 证据不足'
    if (greenHasMaterialGap(item)) return '已执行 · 分项未完整'
    if (greenReviewPending(item)) return '已执行 · 待人工复核'
  }
  if (!isExecutionReportItem(item) && item.applicability === 'not_applicable') return '适用性已核对 · 不适用'
  if (!isExecutionReportItem(item) && item.applicability === 'out_of_scope') return '本次范围未选择 · 未执行'
  const status = item.machine_display_status
  if (status === 'not_executed' || status === 'coverage_gap') return '未执行'
  if (status === 'pending') return '待执行'
  if (status === 'running') return '执行中'
  if (status === 'detect_error' || (status === 'manual_review' && !item.confirmable)) return '检测异常'
  if (status === 'evidence_insufficient') return '无法判定 · 证据不足'
  return '已执行'
}

/** 同一规则跨页面使用时汇总全部执行事实，不能让一条未通过掩盖其他次的检测缺口。 */
export function ruleExecutionStateText(items: ReportRuleItem[]): string {
  const states = new Set(items.map(executionStateText))
  if (states.size <= 1) return states.values().next().value || '未知'
  if (states.has('未执行') || states.has('待执行')) return '部分未执行'
  if (states.has('无法判定 · 证据不足')) return '部分无法判定'
  if (states.has('关联待核对')) return '关联待核对'
  if (states.has('已执行 · 分项未完整')) return '部分无法判定'
  if (states.has('已执行 · 待人工复核')) return '待人工复核'
  if (states.has('检测异常')) return '存在检测异常'
  return '部分执行中'
}

export function ruleExecutionStateDetail(items: ReportRuleItem[]): string {
  if (!items.length) return '无执行记录'
  if (!items.some(isExecutionReportItem)) return `${executionStateText(items[0]!)}（无执行记录）`
  const counts = new Map<string, number>()
  for (const item of items) {
    const state = executionStateText(item)
    counts.set(state, (counts.get(state) || 0) + 1)
  }
  return [...counts].map(([state, count]) => `${state} ${count} 次`).join('；')
}

/** 表格「最终结果」：待人工确认的规则先写「待人工确认」，其余取人工判定优先后的六类结果。 */
export function finalResultOf(item: ReportRuleItem): { label: string; tone: string } {
  if (item.green_result) {
    if (!isLinkedGreenResult(item.green_result)) return { label: '关联待核对', tone: 'tone-muted' }
    if (item.green_result.execution_status === 'fail' && (greenHasMaterialGap(item) || greenReviewPending(item))) {
      return { label: greenResultStatus(item.green_result).label, tone: 'tone-danger' }
    }
    if (greenReviewPending(item) && item.green_result.execution_status === 'pass') {
      return { label: '待人工复核', tone: 'tone-muted' }
    }
  }
  if (!isExecutionReportItem(item)) {
    return item.applicability === 'not_applicable'
      ? { label: item.display_status_label || '不适用', tone: 'tone-muted' }
      : { label: item.machine_display_status_label || '未形成结论', tone: 'tone-muted' }
  }
  if (isUndecidableResult(item)) {
    return { label: '未形成结论', tone: 'tone-muted' }
  }
  if (item.confirm_status === 'pending') {
    return { label: '待人工确认', tone: 'tone-muted' }
  }
  const group = resultGroupOf(item.display_status)
  return { label: item.display_status_label || group.label, tone: group.tone }
}

const CONFIRM_STATUS_LABELS: Record<'pending' | 'confirmed', string> = {
  pending: '待确认',
  confirmed: '已确认',
}

/** 表格「人工确认」：不需要人工确认的规则写「-」。 */
export function confirmStatusText(item: ReportRuleItem): string {
  if (item.green_result) {
    if (!isLinkedGreenResult(item.green_result)) return '关联待核对'
    if (greenReviewPending(item)) return '待人工复核'
  }
  return item.confirm_status ? CONFIRM_STATUS_LABELS[item.confirm_status] : '-'
}

/** 人工确认的操作人和时间只使用已保存事实，不把机器执行时间充作确认时间。 */
export function humanConfirmationAuditText(item: ReportRuleItem): string | null {
  if (!item.human_verdict && !item.human_verdict_by && !item.human_verdict_at) return null
  const actor = item.human_verdict_by?.trim() || '未记录'
  const date = item.human_verdict_at ? new Date(item.human_verdict_at) : null
  const time = date && Number.isFinite(date.getTime())
    ? date.toLocaleString('zh-CN', { hour12: false })
    : '未记录'
  return `确认人：${actor} · 确认时间：${time}`
}

/**
 * 缺失证据的说法：接口给的是证据类型或检测工具的内部名，页面写成运营看得懂的话；
 * 没登记的原样显示。广告落地页那三份是判定侧缺哪项就记哪个检测工具。
 */
const EVIDENCE_REQUIREMENT_LABELS: Record<string, string> = {
  after_action_screenshot: '操作后页面截图',
  before_action_screenshot: '操作前页面截图',
  dom_snapshot: '页面结构快照',
  ocr_text: '页面文字识别结果',
  network_log: '网络请求记录',
  safe_click_observer_tool: '点击广告前后的页面截图',
  landing_page_probe_tool: '点开广告后到达的落地页',
  visual_semantic_compare_tool: '广告画面和落地页的比对',
}

export function missingEvidenceText(item: ReportRuleItem): string {
  return (item.missing_evidence ?? [])
    .map((value) => EVIDENCE_REQUIREMENT_LABELS[value] ?? value)
    .join('、')
}

/** 识别把握度：接口给 0~1 的小数，页面只写百分比，没有就写「无」。 */
export function confidenceText(value: number | null | undefined): string {
  return value == null ? '无' : `${Math.round(value * 100)}%`
}

/** 机器判定说明：优先机器结论摘要，没有时用识别详情兜底。 */
export function machineExplanation(item: ReportRuleItem): string {
  return item.result_summary || item.problem_desc || '暂无机器判定说明'
}

/** 人工确认卡片的「原因」：机器说明之外，为什么这条还要人工看（如把握度不够、疑似红线）。 */
export function confirmReason(item: ReportRuleItem): string | null {
  const reason = item.problem_desc?.trim()
  if (!reason || reason === machineExplanation(item).trim()) {
    return null
  }
  return reason
}

export function severityText(item: ReportRuleItem): string {
  return item.severity_display || severityLabel(item.severity) || '未定级'
}

export function severityChipClass(item: ReportRuleItem): string {
  return severityClass(item.severity || item.severity_display)
}
