/**
 * 任务详情「页面详情」弹窗的大白话：这一页是怎么走到、怎么被认出来的，检查了哪些规则，
 * 没通过的、要人工确认的各自为什么。只用时间线、探索动作和规则结果里现成的字段拼句子，
 * 不调模型（2026-09-22 立川：页面上只留时间线和截图，其余都放进每页的详情弹窗）。
 */
import type {
  ExplorationActionItem,
  PageTimelineNode,
  RuleResultItem,
} from '@/types/task-insight'

export type RuleBucket = 'failed' | 'review' | 'passed' | 'not_applicable' | 'pending'

export interface RuleTally {
  total: number
  failed: number
  review: number
  passed: number
  not_applicable: number
  pending: number
}

export interface ReasonGroup {
  reason: string
  items: RuleResultItem[]
}

export interface PageStoryContext {
  actions: ExplorationActionItem[]
  /** 采集编号 → 时间线上的页面名，用来说「从哪一页过来」。 */
  nodeNameByCapture: Map<string, string>
  rules: RuleResultItem[]
  explorationOnly: boolean
  /** 这一页被弹窗盖着时的弹窗叫法（如「广告/遮挡弹窗」），没有弹窗为 null。 */
  popupLabel: string | null
  /** 规则结果还没加载回来：不能说「没有要检查的规则」。 */
  rulesLoading?: boolean
}

/** 一个时间线节点对应的全部采集编号（主版本、历史版本都算）。 */
export function nodeCaptureIds(node: PageTimelineNode): string[] {
  const ids = [
    node.page_capture_id,
    node.primary_capture_id,
    ...(node.capture_ids ?? []),
    ...(node.capture_versions ?? []).map((version) => version.page_capture_id),
  ]
  return [...new Set(ids.filter((id): id is string => typeof id === 'string' && id.length > 0))]
}

export function ruleBucket(item: RuleResultItem): RuleBucket {
  switch (item.display_status) {
    case 'failed':
      return 'failed'
    case 'passed':
      return 'passed'
    case 'not_applicable':
    case 'not_detected':
      return 'not_applicable'
    case 'running':
    case 'pending':
      return 'pending'
    default:
      // 待人工复核、证据不足、执行异常、未执行、运行未覆盖：机器都没给出能直接用的结论
      return 'review'
  }
}

export function tallyRules(items: RuleResultItem[]): RuleTally {
  const tally: RuleTally = { total: items.length, failed: 0, review: 0, passed: 0, not_applicable: 0, pending: 0 }
  for (const item of items) {
    tally[ruleBucket(item)] += 1
  }
  return tally
}

/** 「通过 8 条、没通过 2 条、需要人工确认 13 条」这类短句，只列有数的。 */
export function tallySummaryText(tally: RuleTally): string {
  const parts: string[] = []
  if (tally.passed) parts.push(`通过 ${tally.passed} 条`)
  if (tally.failed) parts.push(`没通过 ${tally.failed} 条`)
  if (tally.review) parts.push(`未完成判定 ${tally.review} 条`)
  if (tally.not_applicable) parts.push(`不适用 ${tally.not_applicable} 条`)
  if (tally.pending) parts.push(`还在检查 ${tally.pending} 条`)
  return parts.join('、')
}

function ruleText(item: RuleResultItem): string {
  return (item.problem_desc || item.result_summary || '').trim().replace(/[。；;]+$/, '')
}

/** 需要人工确认的规则为什么要人看：状态说法 + 机器留下的说明。 */
export function reviewReason(item: RuleResultItem): string {
  const label = (item.display_status_label || '').trim() || '需要人工确认'
  const text = ruleText(item)
  return text ? `${label}：${text}` : label
}

/** 不适用的规则为什么不适用：机器留下的具体原因（页面上没有这类内容、要登录才能查、平台不做跨端采样等）。 */
export function notApplicableReason(item: RuleResultItem): string {
  return ruleText(item) || '机器判定不适用，但没有留下具体原因'
}

/** 同一个原因的规则合成一组，避免十几条规则把同一句话重复十几遍；人多的组排前面。 */
function groupByReason(items: RuleResultItem[], reasonOf: (item: RuleResultItem) => string): ReasonGroup[] {
  const groups = new Map<string, RuleResultItem[]>()
  for (const item of items) {
    const reason = reasonOf(item)
    groups.set(reason, [...(groups.get(reason) ?? []), item])
  }
  return [...groups.entries()]
    .map(([reason, grouped]) => ({ reason, items: grouped }))
    .sort((a, b) => b.items.length - a.items.length)
}

export function groupReviewItems(items: RuleResultItem[]): ReasonGroup[] {
  return groupByReason(items, reviewReason)
}

export function groupNotApplicableItems(items: RuleResultItem[]): ReasonGroup[] {
  return groupByReason(items, notApplicableReason)
}

/** 没通过的规则：问题是什么，机器看到了什么（和问题说法一样就不重复）。 */
export function failureExplanation(item: RuleResultItem): { problem: string; observed: string | null } {
  const problem = (item.problem_desc || item.result_summary || '机器判定没通过').trim()
  const observed = (item.result_summary || '').trim()
  return {
    problem,
    observed: observed && observed !== problem ? observed : null,
  }
}

/** 探索动作的失败说明翻成人话；认不出的原样保留，只换掉英文词。 */
export function plainActionFailure(text: string | null | undefined): string {
  const raw = (text || '').trim()
  if (!raw) {
    return '没有成功'
  }
  if (/可关闭的唯一弹窗|POPUP_DISMISSAL_NOT_FOUND/i.test(raw)) {
    return '没找到能确认的关闭按钮'
  }
  return raw
    .replace(/UI\s*Snapshot\s*/gi, '界面快照')
    .replace(/Frontier/gi, '待探索入口')
    .replace(/[。；;]+$/, '')
}

function captureName(ctx: PageStoryContext, captureId: string | null): string {
  return (captureId && ctx.nodeNameByCapture.get(captureId)) || '上一页'
}

function isPopupDismissal(action: ExplorationActionItem): boolean {
  const text = `${action.purpose || ''} ${action.target || ''}`
  return /关闭|弹窗|遮挡|dismiss|popup/i.test(text)
}

function arrivalSentence(node: PageTimelineNode, ctx: PageStoryContext, captures: string[]): string | null {
  const arrival = ctx.actions.find(
    (action) => action.result_page_capture_id && captures.includes(action.result_page_capture_id),
  )
  if (!arrival) {
    if (node.parent_page_capture_id) {
      return `接着「${captureName(ctx, node.parent_page_capture_id)}」继续。`
    }
    return node.url ? `打开 ${node.url}。` : null
  }
  const target = (arrival.target || '').trim()
  switch (arrival.action) {
    case 'navigate':
    case 'open':
      return `打开任务入口 ${node.url || target || ''}`.trim() + '。'
    case 'click':
    case 'tap':
      return `在「${captureName(ctx, arrival.source_page_capture_id)}」上点击「${target || '一个入口'}」，来到这一页。`
    case 'back':
      return '从上一页返回到这里。'
    case 'scroll':
    case 'swipe':
      return '在页面上滑动后看到这里。'
    case 'observe':
      return /自动退场|自己消失/.test(arrival.purpose || '')
        ? '上一个弹窗自己消失了，等页面稳定后重新拍了一次。'
        : '在同一页重新观察了一次。'
    default:
      return `在「${captureName(ctx, arrival.source_page_capture_id)}」上操作后来到这一页。`
  }
}

function recognitionSentence(node: PageTimelineNode): string {
  if (node.status === 'pending') {
    return '这一页还在等待识别。'
  }
  if (node.status === 'skipped') {
    return '这一页被跳过了，没有识别。'
  }
  if (node.page_type_name) {
    const confidence = node.detection?.confidence
    const sure = typeof confidence === 'number' ? `，把握 ${Math.round(confidence * 100)}%` : ''
    return `系统认出这是「${node.page_type_name}」${sure}。`
  }
  return '这一张是过渡画面，没有单独识别页面类型。'
}

function popupSentences(ctx: PageStoryContext, outgoing: ExplorationActionItem[]): string[] {
  if (!ctx.popupLabel) {
    return []
  }
  const sentences = [`页面上盖着${ctx.popupLabel}。`]
  const dismissal = outgoing.find(isPopupDismissal)
  if (!dismissal) {
    return sentences
  }
  if (dismissal.status === 'explored') {
    sentences.push('系统把它关掉了。')
  } else if (['failed', 'blocked', 'skipped'].includes(dismissal.status)) {
    sentences.push(`系统想把它关掉，但${plainActionFailure(dismissal.failure_message || dismissal.failure_reason)}。`)
  } else {
    sentences.push('系统正在尝试关掉它。')
  }
  return sentences
}

function onwardSentences(ctx: PageStoryContext, outgoing: ExplorationActionItem[]): string[] {
  return outgoing
    .filter((action) => !isPopupDismissal(action) && action.action !== 'observe')
    .slice(0, 3)
    .map((action) => {
      const target = (action.target || '').trim() || '一个入口'
      if (action.status === 'explored') {
        const next = action.result_page_capture_id ? ctx.nodeNameByCapture.get(action.result_page_capture_id) : null
        return next ? `接着点击「${target}」，进入了「${next}」。` : `接着点击了「${target}」。`
      }
      if (['failed', 'blocked'].includes(action.status)) {
        return `想点「${target}」，没成功：${plainActionFailure(action.failure_message || action.failure_reason)}。`
      }
      return ''
    })
    .filter(Boolean)
}

/** 一个页面节点的经过，按先后顺序一句一句写。 */
export function buildPageStory(node: PageTimelineNode, ctx: PageStoryContext): string[] {
  const captures = nodeCaptureIds(node)
  const outgoing = ctx.actions.filter(
    (action) => action.source_page_capture_id && captures.includes(action.source_page_capture_id),
  )
  const story: string[] = []
  const arrival = arrivalSentence(node, ctx, captures)
  if (arrival) story.push(arrival)
  story.push(recognitionSentence(node))
  story.push(...popupSentences(ctx, outgoing))
  story.push(...onwardSentences(ctx, outgoing))
  if (!ctx.explorationOnly && ctx.rulesLoading) {
    story.push('这一页的规则结果还在加载，稍等片刻再看。')
  } else if (!ctx.explorationOnly) {
    const tally = tallyRules(ctx.rules)
    story.push(
      node.inspection.failure_code === 'PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE'
        ? node.inspection.failure_message || '该页型未包含在本任务选择的质检范围内，仅保留页面证据。'
        : tally.total
        ? `这一页检查了 ${tally.total} 条规则：${tallySummaryText(tally)}。`
        : '这一页尚无规则执行结果。',
    )
  }
  if (node.inspection?.failure_message && node.inspection.failure_code !== 'PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE') {
    story.push(`这一页的检查中途出了问题：${plainActionFailure(node.inspection.failure_message)}。`)
  }
  if ((node.capture_count ?? 0) > 1) {
    story.push(`这一页前后采了 ${node.capture_count} 次证据，以最新的主版本为准。`)
  }
  return story
}

/** 不属于某一个页面的规则（整任务判一次的、没对上页面的）。 */
export function buildTaskLevelStory(rules: RuleResultItem[], rulesLoading = false): string[] {
  const tally = tallyRules(rules)
  return [
    '这些记录没有绑定某个页面，包含整体检查、适用性核对、范围未选择及尚未执行的规则；每条保留原因，核对处置不代表进行过 AI 判定。',
    rulesLoading
      ? '规则结果还在加载，稍等片刻再看。'
      : tally.total
        ? `一共 ${tally.total} 条：${tallySummaryText(tally)}。`
        : '这次没有这类规则。',
  ]
}
