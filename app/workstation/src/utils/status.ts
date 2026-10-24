/** 工作台跨页面状态文案的唯一来源，禁止页面自行维护同义映射。 */

export const TASK_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  canceling: '取消中',
  review: '待复核',
  done: '已完成',
  failed: '执行失败',
  canceled: '已取消',
}

export const RULE_EXECUTION_STATUS_LABELS: Record<string, string> = {
  pass: '通过',
  fail: '未通过',
  warning: '风险提示',
  manual_review: '待人工复核',
  evidence_insufficient: '证据不足',
  skipped: '已跳过',
  ignored: '忽略',
  error: '执行异常',
}

export const REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  approved: '已确认',
  rejected: '已驳回',
  needs_recheck: '需复核',
}

/** 人工判定只有三个选项（2.1 原型）；ignored 沿用库里的取值，含义是「本产品不适用」。 */
export const HUMAN_VERDICT_LABELS: Record<string, string> = {
  pass: '通过',
  fail: '未通过',
  ignored: '本产品不适用',
}

/**
 * 报告状态三态（2.1 原型）：生成中 / 生成失败 / 成功。「成功」只表示报告已生成，
 * 不代表每条规则都通过；单条规则的六类结果不作为报告状态。没有报告可看的任务记为 none。
 */
export type ReportState = 'success' | 'generating' | 'failed' | 'none'

export const REPORT_STATE_LABELS: Record<ReportState, string> = {
  success: '成功',
  generating: '生成中',
  failed: '生成失败',
  none: '暂无报告',
}

export function reportStateOf(
  reportStatus: string | null | undefined,
  taskStatus?: string | null,
): ReportState {
  if (reportStatus === 'ready' || reportStatus === 'generated') return 'success'
  if (reportStatus === 'failed') return 'failed'
  // 已取消任务不能继续生成报告，保留已经生成或失败的真实报告结果。
  if (taskStatus === 'canceled') return 'none'
  if (reportStatus === 'generating') return 'generating'
  if (reportStatus === 'not_started' && (taskStatus === 'queued' || taskStatus === 'running')) {
    return 'generating'
  }
  return 'none'
}

export function reportStateLabel(reportStatus: string | null | undefined, taskStatus?: string | null) {
  return REPORT_STATE_LABELS[reportStateOf(reportStatus, taskStatus)]
}

/** 严重等级展示：文案 + 配色 class；配色的样式定义在 styles/workstation.css。 */
export const SEVERITY_META: Record<string, { label: string; cls: string }> = {
  critical: { label: 'P0', cls: 'sev-critical' },
  high: { label: 'P1', cls: 'sev-high' },
  medium: { label: 'P2', cls: 'sev-medium' },
  low: { label: 'P3', cls: 'sev-low' },
  info: { label: '提示', cls: 'sev-info' },
}

/** 部分读模型只回传展示文案（P0..P3），需反查原始等级取配色。 */
const SEVERITY_DISPLAY_TO_RAW: Record<string, string> = {
  P0: 'critical',
  P1: 'high',
  P2: 'medium',
  P3: 'low',
}

/** 入参兼容原始等级（critical..info）与展示文案（P0..P3）。 */
export function severityClass(value: string | null | undefined): string {
  if (!value) {
    return 'sev-unknown'
  }
  const raw = SEVERITY_META[value] ? value : SEVERITY_DISPLAY_TO_RAW[value.toUpperCase()]
  return raw ? SEVERITY_META[raw].cls : 'sev-unknown'
}

export function severityLabel(severity: string | null | undefined): string | null {
  return severity ? SEVERITY_META[severity]?.label || severity : null
}

export function taskStatusLabel(status: string) {
  return TASK_STATUS_LABELS[status] || status || '-'
}

export function ruleExecutionStatusLabel(status: string) {
  return RULE_EXECUTION_STATUS_LABELS[status] || status || '-'
}

export function reviewStatusLabel(status: string) {
  return REVIEW_STATUS_LABELS[status] || status || '-'
}

export function humanVerdictLabel(verdict: string | null | undefined) {
  return verdict ? HUMAN_VERDICT_LABELS[verdict] || verdict : null
}
