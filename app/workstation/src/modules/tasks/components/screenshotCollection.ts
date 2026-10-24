import type {
  PageGraph,
  PageTimeline,
  RuleResultItem,
  ScreenshotGroup,
  ScreenshotItem,
  ScreenshotRelatedRule,
} from '@/types/task-insight'

export interface ScreenshotViewerAction {
  id: string
  action_label: string
  purpose: string | null
  status_label: string
  source_capture_id: string | null
  target_capture_id: string | null
}

export interface ScreenshotViewerEntry {
  artifact_id: string
  page_name: string
  captured_at: string | null
  capture_role: string | null
  duplicate_count: number
  related_rules: ScreenshotRelatedRule[]
  page_capture_id?: string | null
  capture_purpose?: string | null
  page_state?: string | null
  actions?: ScreenshotViewerAction[]
}

export interface ScreenshotCollectionContext {
  rules?: RuleResultItem[]
  timeline?: PageTimeline | null
  pageGraph?: PageGraph | null
}

function captureCount(value: number) {
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : 1
}

export function buildScreenshotCollection(
  group: Pick<ScreenshotGroup, 'page_display_name'>,
  item: ScreenshotItem,
  context: ScreenshotCollectionContext = {},
): ScreenshotViewerEntry[] {
  const entries: ScreenshotViewerEntry[] = [{
    artifact_id: item.artifact_id,
    page_name: group.page_display_name,
    captured_at: item.captured_at,
    capture_role: item.capture_role ?? null,
    duplicate_count: captureCount(item.duplicate_count),
    related_rules: item.related_rules ?? [],
    page_capture_id: item.page_capture_id,
    capture_purpose: item.capture_purpose ?? null,
    page_state: item.page_state ?? null,
  }, ...(item.related_frames ?? []).map((frame) => ({
    artifact_id: frame.artifact_id,
    page_name: group.page_display_name,
    captured_at: frame.captured_at,
    capture_role: frame.capture_role ?? null,
    duplicate_count: captureCount(frame.duplicate_count),
    related_rules: [],
    page_capture_id: frame.page_capture_id,
    capture_purpose: null,
    page_state: frame.page_state ?? null,
  }))]
  const actions = collectionActions(context)
  for (const entry of entries) {
    const captureId = entry.page_capture_id
    const version = captureId ? context.timeline?.nodes?.flatMap((node) => node.capture_versions ?? [])
      .find((candidate) => candidate.page_capture_id === captureId) : undefined
    entry.capture_purpose ||= version?.capture_purpose ?? null
    entry.page_state ||= context.pageGraph?.states?.find((state) => state.state_id === captureId)?.page_state ?? null
    entry.actions = captureId ? actions.filter((action) =>
      action.source_capture_id === captureId || action.target_capture_id === captureId) : []
    const related = new Map(entry.related_rules.map((rule) => [`${rule.rule_code}:${rule.display_status}`, rule]))
    // 子图只恢复明确引用该制品的规则，不能继承代表图或同页面的全部规则。
    for (const rule of context.rules ?? []) {
      const key = `${rule.rule_code}:${rule.display_status}`
      if (!(rule.evidence_artifact_ids ?? []).includes(entry.artifact_id) || related.has(key)) continue
      related.set(key, {
        rule_code: rule.rule_code,
        rule_name: rule.rule_name,
        display_status: rule.display_status,
        display_status_label: rule.display_status_label,
      })
    }
    entry.related_rules = [...related.values()]
  }
  const seen = new Set<string>()
  return entries.filter((entry) => {
    if (!entry.artifact_id || seen.has(entry.artifact_id)) return false
    seen.add(entry.artifact_id)
    return true
  }).sort((left, right) => {
    // 仅按真实采集时间排序，未知时间保留原顺序，不猜测操作前后关系。
    const leftTime = left.captured_at ? Date.parse(left.captured_at) : Number.NaN
    const rightTime = right.captured_at ? Date.parse(right.captured_at) : Number.NaN
    if (!Number.isFinite(leftTime)) return Number.isFinite(rightTime) ? 1 : 0
    if (!Number.isFinite(rightTime)) return -1
    return leftTime - rightTime
  })
}

const ACTION_LABELS: Record<string, string> = {
  click: '点击', tap: '点击', scroll: '滑动', swipe: '滑动', navigate: '打开', open: '打开',
  back: '返回', dismiss: '关闭弹窗', input: '输入', observe: '观察', wait: '等待',
}

const ACTION_STATUS_LABELS: Record<string, string> = {
  completed: '已执行', explored: '已执行', failed: '执行失败', timed_out: '执行超时',
  evidence_failed: '证据采集失败', blocked: '被阻断', no_effect: '未生效', pending: '待执行',
  candidate: '待执行', planned: '待执行', selected: '执行中', running: '执行中',
  skipped: '已跳过', cancelled: '已取消', canceled: '已取消',
}

function actionLabel(kind: string | null, target: string | null) {
  const label = ACTION_LABELS[kind || ''] || '操作'
  return target?.trim() ? `${label}「${target.trim()}」` : label
}

function collectionActions(context: ScreenshotCollectionContext): ScreenshotViewerAction[] {
  const timeline = context.timeline?.exploration_actions ?? []
  const actions = new Map<string, ScreenshotViewerAction>()
  const conflicting = new Set<string>()
  const transitions = context.pageGraph?.status === 'unavailable' ? [] : context.pageGraph?.transitions ?? []
  for (const transition of transitions) {
    const explicitId = transition.action_execution_id?.trim() || null
    const id = explicitId || transition.transition_id
    if (!id) continue
    const source = transition.source_state_id ?? null
    const target = transition.target_state_id ?? null
    const existing = actions.get(id)
    if (existing) {
      if (existing.source_capture_id !== source || existing.target_capture_id !== target) conflicting.add(id)
      continue
    }
    const matches = timeline.filter((action) => action.source_page_capture_id === source
      && action.result_page_capture_id === target)
    // 显式动作身份必须一致；只有旧图缺少动作身份时才允许唯一端点回退。
    const matching = explicitId ? matches.find((action) => action.exploration_action_id === id)
      : (matches.length === 1 ? matches[0] : undefined)
    actions.set(id, {
      id,
      action_label: actionLabel(transition.action_kind, transition.semantic_target),
      purpose: matching?.purpose?.trim() || null,
      status_label: ACTION_STATUS_LABELS[transition.status] || '状态未知',
      source_capture_id: source,
      target_capture_id: target,
    })
  }
  for (const action of timeline) {
    const source = action.source_page_capture_id
    const target = action.result_page_capture_id
    // 页面图优先；旧动作只有明确给出不同的前后采集编号时才补充配对。
    if (!action.exploration_action_id || !source || !target || source === target
      || conflicting.has(action.exploration_action_id) || actions.has(action.exploration_action_id)
      || [...actions.values()].some((known) => known.source_capture_id === source
        && known.target_capture_id === target)) continue
    actions.set(action.exploration_action_id, {
      id: action.exploration_action_id,
      action_label: actionLabel(action.action, action.target),
      purpose: action.purpose?.trim() || null,
      status_label: ACTION_STATUS_LABELS[action.status] || '状态未知',
      source_capture_id: source,
      target_capture_id: target,
    })
  }
  return [...actions.values()].filter((action) => !conflicting.has(action.id))
}

export function buildScreenshotActionGroups(entries: ScreenshotViewerEntry[]): {
  groups: Array<{ action: ScreenshotViewerAction; before: ScreenshotViewerEntry[]; after: ScreenshotViewerEntry[] }>
  unassociated: ScreenshotViewerEntry[]
} {
  const actions = new Map<string, ScreenshotViewerAction>()
  const conflicting = new Set<string>()
  for (const entry of entries) {
    for (const action of entry.actions ?? []) {
      if (!action.id) continue
      const existing = actions.get(action.id)
      if (existing && (existing.source_capture_id !== action.source_capture_id
        || existing.target_capture_id !== action.target_capture_id)) conflicting.add(action.id)
      else actions.set(action.id, action)
    }
  }
  const associated = new Set<ScreenshotViewerEntry>()
  const groups = []
  for (const action of actions.values()) {
    // 相同采集编号或冲突动作没有可区分的前后事实，保留为未关联截图。
    if (conflicting.has(action.id) || action.source_capture_id === action.target_capture_id) continue
    const before = action.source_capture_id ? entries.filter((entry) => entry.page_capture_id === action.source_capture_id) : []
    const after = action.target_capture_id ? entries.filter((entry) => entry.page_capture_id === action.target_capture_id) : []
    if (!before.length && !after.length) continue
    before.forEach((entry) => associated.add(entry))
    after.forEach((entry) => associated.add(entry))
    groups.push({ action, before, after })
  }
  return { groups, unassociated: entries.filter((entry) => !associated.has(entry)) }
}
