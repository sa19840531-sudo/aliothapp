<script setup lang="ts">
import { computed, ref } from 'vue'
import type {
  EvidenceGapItem,
  EvidenceGaps,
  PageTypeJudgements,
  RuleReuse,
  ExplorationTreeNode,
  PageGraph,
  PageGraphState,
  PageGraphTransition,
  RecoveryRoutes,
  ScreenshotItem,
  TaskScreenshots,
} from '@/types/task-insight'
import { pageStateName, pageTypeName, reasonText } from '@/utils/exploration-labels'

/**
 * 任务详情「探索过程」面板：页面路线、弹窗与外跳、截图质量、补采缺口与路线、规则复用。
 *
 * 只消费工作台只读接口的投影（页面图 / 只读恢复路线 / 证据缺口台账 / 截图证据），
 * 不产生任何页面到达、覆盖或补采事实；数据不可用时明确说明，不把空列表伪装成“没有探索”。
 */
const props = withDefaults(
  defineProps<{
    graph: PageGraph | null
    routes: RecoveryRoutes | null
    gaps: EvidenceGaps | null
    screenshots: TaskScreenshots | null
    reuse?: RuleReuse | null
    judgements?: PageTypeJudgements | null
    defaultExpanded?: boolean
    /** 截图还在加载：先说「加载中…」，不说「还没有截图证据」 */
    screenshotsLoading?: boolean
  }>(),
  { defaultExpanded: true, reuse: null, judgements: null, screenshotsLoading: false },
)

const expanded = ref(props.defaultExpanded)
const gapLimit = 40

function reasonCodes(value: unknown): string[] {
  if (value == null) return []
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { return ['graph_reason_codes_invalid'] }
  }
  return Array.isArray(value) && value.every((code) => typeof code === 'string')
    ? value : ['graph_reason_codes_invalid']
}


const TREE_STATUS: Record<string, { label: string; tone: string }> = {
  completed: { label: '完成', tone: 'ok' },
  visited: { label: '已访问', tone: 'info' },
  needs_evidence: { label: '缺证', tone: 'warn' },
  blocked: { label: '阻断', tone: 'bad' },
  unvisited: { label: '未访问', tone: 'muted' },
}

const TARGET_STATUS: Record<string, string> = {
  executed: '已执行',
  unvisited: '待探索',
  blocked: '阻断',
  failed: '失败',
  skipped: '跳过',
  expired: '过期',
  completed: '已执行',
}

const TRANSITION_STATUS: Record<string, string> = {
  completed: '已执行',
  blocked: '被阻断',
  failed: '失败',
  no_effect: '无效果',
  pending: '待执行',
}

const ACTION_KIND: Record<string, string> = {
  navigate: '打开地址',
  click: '点击',
  tap: '点击',
  back: '返回',
  scroll: '滑动',
  swipe: '滑动',
  dismiss: '关闭弹窗',
  input: '输入',
}

const PHASE_LABELS: Record<string, string> = {
  before_close: '关闭前',
  after_close: '关闭后',
  not_applicable: '不涉及弹窗',
}

const POLICY_LABELS: Record<string, string> = {
  exact: '原状态',
  exact_state: '原状态',
  equivalent: '等价状态',
  equivalent_state: '等价状态',
  fresh: '重新观察',
  fresh_observation: '重新观察',
  manual: '人工',
  manual_only: '人工',
}

const DISPOSITION_LABELS: Record<string, string> = {
  recorded_only: '已记录，未执行',
  requested: '已发出补采请求',
  executed: '已补采',
  expired: '已过期',
}

const PRESENTATION_CODES: Record<string, string> = {
  BASE_PAGE_OCCLUDED: '底层页面被遮挡',
  DIAGNOSTIC_EVIDENCE_EXCLUDED: '来自诊断操作的材料',
  EXTERNAL_SURFACE: '外部页面',
}

type QualityKind = 'usable' | 'occluded' | 'diagnostic' | 'unassessed'

const QUALITY_LABELS: Record<QualityKind, string> = {
  usable: '可用',
  occluded: '底页被遮挡',
  diagnostic: '诊断材料',
  unassessed: '未评估',
}

function labelOf(map: Record<string, string>, code: string | null | undefined, fallback = '未知') {
  if (!code) return fallback
  return map[code] ?? code
}

function payloadOf(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
    } catch {
      return {}
    }
  }
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

function urlOf(state: PageGraphState | undefined) {
  if (!state) return ''
  const url = payloadOf(state.state_payload).url
  return typeof url === 'string' ? url : ''
}

function pageKeyLabel(pageKey: string) {
  if (pageKey.startsWith('url:')) {
    const raw = pageKey.slice(4)
    try {
      const parsed = new URL(raw)
      const path = `${parsed.pathname}${parsed.search}`
      return path && path !== '/' ? path : `${parsed.host}/`
    } catch {
      return raw
    }
  }
  if (pageKey.startsWith('unknown:')) return '未确认身份的页面'
  return pageKey
}

const graphAvailable = computed(() => props.graph?.status === 'available')

const graphUnavailableText = computed(() => {
  if (!props.graph) return '页面路线尚未加载'
  if (props.graph.status === 'available') return ''
  const reason = props.graph.reason_code ?? ''
  if (reason === 'graph_repository_not_configured') return '页面状态图未启用'
  if (reason === 'graph_projection_not_complete') return '页面状态图还在投影中'
  return reason ? `页面状态图不可用（${reason}）` : '页面状态图不可用'
})

const stateById = computed(() => {
  const map = new Map<string, PageGraphState>()
  for (const state of props.graph?.states ?? []) map.set(state.state_id, state)
  return map
})

const transitions = computed<PageGraphTransition[]>(() => props.graph?.transitions ?? [])

const tree = computed(() => props.graph?.exploration_tree ?? null)

const nodeByKey = computed(() => {
  const map = new Map<string, ExplorationTreeNode>()
  for (const node of tree.value?.nodes ?? []) map.set(node.page_key, node)
  return map
})

interface TreeRow {
  key: string
  depth: number
  label: string
  pageType: string
  status: { label: string; tone: string }
  versionCount: number
  overlayCount: number
  targetSummary: string
  reasons: string[]
  url: string
}

function latestState(node: ExplorationTreeNode): PageGraphState | undefined {
  for (let index = node.states.length - 1; index >= 0; index -= 1) {
    const captureId = node.states[index]?.latest_capture_id
    if (captureId && stateById.value.has(captureId)) return stateById.value.get(captureId)
  }
  return undefined
}

function targetSummary(node: ExplorationTreeNode) {
  const counts = new Map<string, number>()
  for (const target of node.targets) {
    const status = target.progress_status || target.status || 'unknown'
    counts.set(status, (counts.get(status) ?? 0) + 1)
  }
  if (!counts.size) return '无待探索入口'
  return Array.from(counts.entries())
    .map(([status, count]) => `${labelOf(TARGET_STATUS, status)} ${count}`)
    .join(' · ')
}

const treeRows = computed<TreeRow[]>(() => {
  const current = tree.value
  if (!current) return []
  const rows: TreeRow[] = []
  const seen = new Set<string>()
  const walk = (key: string, depth: number) => {
    if (seen.has(key)) return
    seen.add(key)
    const node = nodeByKey.value.get(key)
    if (!node) return
    const state = latestState(node)
    const reasons = Array.from(
      new Set(node.states.flatMap((version) => reasonCodes(version.reason_codes))),
    ).map(reasonText)
    rows.push({
      key,
      depth,
      label: pageKeyLabel(key),
      pageType: pageTypeName(state?.page_type),
      status: TREE_STATUS[node.status] ?? { label: node.status, tone: 'muted' },
      versionCount: node.states.length,
      overlayCount: node.overlays.length,
      targetSummary: targetSummary(node),
      reasons,
      url: urlOf(state),
    })
    for (const child of node.children) walk(child, depth + 1)
  }
  for (const root of current.roots) walk(root, 0)
  for (const node of current.nodes) walk(node.page_key, 0)
  return rows
})

const treeExtras = computed(() => {
  const current = tree.value
  if (!current) return []
  const items: string[] = []
  if (current.unattached_overlays.length) items.push(`未挂到页面的弹窗 ${current.unattached_overlays.length}`)
  if (current.unknown_observation_ids.length) items.push(`未确认身份的观察 ${current.unknown_observation_ids.length}`)
  if (current.unassigned_targets.length) items.push(`未归属的入口 ${current.unassigned_targets.length}`)
  if (current.cross_transition_ids.length) items.push(`回路或替代入口 ${current.cross_transition_ids.length}`)
  return items
})

interface FailureReason {
  code: string
  label: string
  count: number
}

const failureReasons = computed<FailureReason[]>(() => {
  const counts = new Map<string, number>()
  for (const edge of transitions.value) {
    if (edge.status !== 'blocked' && edge.status !== 'failed') continue
    for (const code of reasonCodes(edge.reason_codes)) {
      counts.set(code, (counts.get(code) ?? 0) + 1)
    }
  }
  return Array.from(counts.entries())
    .map(([code, count]) => ({ code, label: reasonText(code), count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 6)
})

const blockedTransitionCount = computed(
  () =>
    transitions.value.filter((edge) => edge.status === 'blocked' || edge.status === 'failed')
      .length,
)

const trustedTransitionCount = computed(
  () => transitions.value.filter((edge) => edge.proof === 'trusted').length,
)

function ownerPageLabel(captureId: string) {
  for (const node of tree.value?.nodes ?? []) {
    if (node.overlays.some((overlay) => overlay.capture_id === captureId)) {
      return pageKeyLabel(node.page_key)
    }
  }
  return '未挂到页面'
}

function transitionOutcome(edge: PageGraphTransition | undefined) {
  if (!edge) return '没有记录关闭动作'
  const action = labelOf(ACTION_KIND, edge.action_kind, '动作')
  const status = labelOf(TRANSITION_STATUS, edge.status)
  const errors = reasonCodes(edge.reason_codes).filter((code) => code.startsWith('error:'))
  const suffix = errors.length ? `：${errors.map(reasonText).join('，')}` : ''
  return `${action} ${status}${suffix}`
}

interface OverlayRow {
  captureId: string
  owner: string
  state: string
  outcome: string
}

const overlayRows = computed<OverlayRow[]>(() =>
  (props.graph?.states ?? [])
    .filter((state) => state.is_overlay)
    .map((state) => ({
      captureId: state.state_id,
      owner: ownerPageLabel(state.state_id),
      state: pageStateName(state.page_state) || '弹窗',
      outcome: transitionOutcome(
        transitions.value.find((edge) => edge.source_state_id === state.state_id),
      ),
    })),
)

interface ExternalRow {
  captureId: string
  url: string
  from: string
  outcome: string
}

const externalRows = computed<ExternalRow[]>(() =>
  (props.graph?.states ?? [])
    .filter((state) => state.is_external)
    .map((state) => {
      const inbound = transitions.value.find((edge) => edge.target_state_id === state.state_id)
      const sourceState = inbound?.source_state_id
        ? stateById.value.get(inbound.source_state_id)
        : undefined
      const returned = transitions.value.some((edge) => edge.source_state_id === state.state_id)
      return {
        captureId: state.state_id,
        url: urlOf(state) || '未知地址',
        from: sourceState ? pageKeyLabel(sourceState.logical_page_key) : '未知来源',
        outcome: returned ? '已回到产品页面' : '未记录回源',
      }
    }),
)

function qualityOf(item: ScreenshotItem): QualityKind {
  const presentation = item.presentation
  if (!presentation) return 'unassessed'
  if (presentation.mutation_mode === 'instrumented_diagnostic') return 'diagnostic'
  if (
    (presentation.topmost_layer_kind === 'overlay' ||
      presentation.topmost_layer_kind === 'system_dialog') &&
    presentation.base_page_visibility === 'fully_occluded'
  ) {
    return 'occluded'
  }
  if (
    presentation.base_page_visibility === 'visible' ||
    presentation.base_page_visibility === 'partially_occluded'
  ) {
    return 'usable'
  }
  return 'unassessed'
}

interface QualityRow {
  key: string
  page: string
  total: number
  usable: number
  occluded: number
  diagnostic: number
  unassessed: number
  unsettled: number
  notes: string[]
}

const qualityRows = computed<QualityRow[]>(() =>
  (props.screenshots?.groups ?? []).map((group) => {
    const row: QualityRow = {
      key: group.page_group_key,
      page: group.page_display_name || group.page_type_name,
      total: 0,
      usable: 0,
      occluded: 0,
      diagnostic: 0,
      unassessed: 0,
      unsettled: 0,
      notes: [],
    }
    const notes = new Set<string>()
    for (const item of group.items.filter((candidate) => candidate.gallery_visible !== false)) {
      const extra = (item.related_frames ?? []).length
      row.total += 1 + extra
      row[qualityOf(item)] += 1 + extra
      if (item.settle && item.settle.outcome && item.settle.outcome !== 'settled') {
        row.unsettled += 1
        for (const reason of item.settle.blocking_reasons ?? []) notes.add(`未稳定：${reason}`)
      }
      for (const operation of item.presentation?.diagnostic_operations ?? []) {
        notes.add(`诊断操作：${operation.kind}`)
      }
      if (item.presentation?.overlay_semantic_kind && qualityOf(item) === 'occluded') {
        notes.add(`遮挡物：${item.presentation.overlay_semantic_kind}`)
      }
    }
    row.notes = Array.from(notes).slice(0, 4)
    return row
  }),
)

const qualityTotals = computed(() => {
  const totals = { total: 0, usable: 0, occluded: 0, diagnostic: 0, unassessed: 0, unsettled: 0 }
  for (const row of qualityRows.value) {
    totals.total += row.total
    totals.usable += row.usable
    totals.occluded += row.occluded
    totals.diagnostic += row.diagnostic
    totals.unassessed += row.unassessed
    totals.unsettled += row.unsettled
  }
  return totals
})

interface GapRow {
  key: string
  rule: string
  page: string
  missing: string
  policy: string
  phase: string
  route: string
  advice: string
  disposition: string
}

function missingText(item: EvidenceGapItem) {
  const missing = item.gap?.missing
  if (!missing) return item.gap?.evidence_goal || '未说明'
  if (missing.presentation?.code) {
    return labelOf(PRESENTATION_CODES, missing.presentation.code)
  }
  const parts: string[] = []
  if (missing.page_states?.length) parts.push(`页面状态：${missing.page_states.map(pageStateName).join('、')}`)
  if (missing.capture_roles?.length) parts.push(`采集角色：${missing.capture_roles.join('、')}`)
  if (missing.artifact_types?.length) parts.push(`证据类型：${missing.artifact_types.join('、')}`)
  if (missing.judge_statements?.length) parts.push(missing.judge_statements.slice(0, 2).join('；'))
  if (missing.quality_gate_sources?.length) parts.push(`质量门禁：${missing.quality_gate_sources.join('、')}`)
  return parts.length ? parts.join('；') : item.gap?.evidence_goal || '未说明'
}

function routeText(item: EvidenceGapItem) {
  // 服务端已经替这条缺口选过路，就用服务端的结论，别让页面自己再猜一遍。
  const plan = item.route_plan
  if (plan?.reason_label) {
    const blocked = plan.blocked_action_kinds?.length
      ? `（挡路动作：${plan.blocked_action_kinds.join('、')}）`
      : ''
    return `${plan.state}：${plan.reason_label}${blocked}`
  }
  if (plan && plan.state !== '未提补采请求') return plan.state
  const routes = props.routes
  if (!routes) return '路线未加载'
  const matched = routes.routes.filter(
    (route) => item.logical_page_key && route.target_page_key === item.logical_page_key,
  )
  if (matched.length) {
    const steps = Math.min(...matched.map((route) => route.steps.length))
    return `可信路线 ${matched.length} 条，最短 ${steps} 步`
  }
  if (routes.status === 'unreachable') return '暂无可信路线'
  if (routes.status === 'unavailable') return '页面图未就绪'
  return '暂无可信路线'
}

const gapRows = computed<GapRow[]>(() =>
  (props.gaps?.items ?? []).slice(0, gapLimit).map((item) => {
    const gap = item.gap
    const policy = labelOf(POLICY_LABELS, gap?.recapture_policy)
    const fallback = gap?.recapture_fallback ? labelOf(POLICY_LABELS, gap.recapture_fallback) : ''
    const pageState = pageStateName(gap?.target?.observed_page_state)
    return {
      key: item.execution_id || gap?.gap_id || `${item.rule_code}:${item.page_capture_id}`,
      rule: `${item.rule_code} ${item.rule_name ?? ''}`.trim(),
      page: pageState
        ? `${pageTypeName(gap?.target?.page_type)}（${pageState}）`
        : pageTypeName(gap?.target?.page_type),
      missing: missingText(item),
      policy: fallback ? `${policy}，备选${fallback}` : policy,
      phase: labelOf(PHASE_LABELS, gap?.overlay_phase, '未说明'),
      route: routeText(item),
      advice: item.route_plan?.advice ?? '',
      disposition: labelOf(DISPOSITION_LABELS, gap?.disposition, '未说明'),
    }
  }),
)

// 没规划出路线的原因分布：直接用服务端登记好的人话，页面不另起一套说法。
const gapRouteChips = computed(() =>
  Object.entries(props.gaps?.route_plan_counts ?? {})
    .filter(([, count]) => count > 0)
    .map(([state, count]) => `${state} ${count}`),
)

const gapOverflow = computed(() => Math.max((props.gaps?.items.length ?? 0) - gapLimit, 0))

function countChips(counts: Record<string, number> | undefined, labels: Record<string, string>) {
  return Object.entries(counts ?? {})
    .filter(([, count]) => count > 0)
    .map(([code, count]) => `${labelOf(labels, code, '未说明')} ${count}`)
}

const gapPhaseChips = computed(() => countChips(props.gaps?.phase_counts, PHASE_LABELS))
const gapPolicyChips = computed(() => countChips(props.gaps?.policy_counts, POLICY_LABELS))

const routePolicyText = computed(() => props.routes?.route_policy ?? '')

// ---------- 规则结论复用：只列沿用来的结论，真实执行看规则结果列表 ----------
const REUSE_SCOPE_LABELS: Record<string, string> = {
  per_page: '每页各自执行',
  same_object: '同一对象复用',
  same_material: '同一素材复用',
  per_task: '整个任务一次',
  sealed_set: '跨页汇总一次',
}
const REUSE_STATUS_LABELS: Record<string, string> = { pass: '通过', fail: '未通过', warning: '提醒' }
const reuseLimit = 40

interface ReuseRow {
  key: string
  rule: string
  page: string
  source: string
  status: string
  scope: string
  reason: string
  invalidation: string
}

const reuseRows = computed<ReuseRow[]>(() =>
  (props.reuse?.items ?? []).slice(0, reuseLimit).map((item) => ({
    key: item.execution_id || `${item.rule_code}:${item.page_capture_id ?? ''}`,
    rule: `${item.rule_code} ${item.rule_name ?? ''}`.trim(),
    page: pageTypeName(item.page_type),
    source: pageTypeName(item.source?.page_type),
    status: labelOf(REUSE_STATUS_LABELS, item.execution_status, item.execution_status ?? '未知'),
    scope: item.scope_label || labelOf(REUSE_SCOPE_LABELS, item.scope, item.scope),
    reason: item.reason || '—',
    invalidation: item.invalidate_on?.length
      ? item.invalidate_on.map((condition) => condition.label).join('、')
      : '—',
  })),
)
const reuseOverflow = computed(() => Math.max((props.reuse?.items.length ?? 0) - reuseLimit, 0))
const reuseScopeChips = computed(() => countChips(props.reuse?.scope_counts, REUSE_SCOPE_LABELS))

// ---------- 页型判断与改判：各来源判断各留依据，冲突标未知，改判只追加；不改正式页型 ----------
const JUDGEMENT_STATUS_LABELS: Record<string, string> = {
  confirmed: '判断一致',
  revised: '已改判',
  conflicting: '判断冲突',
  unknown: '未知',
  no_judgement: '没有判断记录',
}
const judgementLimit = 40

interface JudgementRow {
  key: string
  page: string
  formal: string
  sources: string
  resolution: string
  revisions: string
  reason: string
  differs: boolean
}

function judgementPageLabel(item: PageTypeJudgements['items'][number]): string {
  const seq = item.page_seq != null ? `第 ${item.page_seq} 页` : ''
  const slug = item.page_slug ? ` ${item.page_slug}` : ''
  return `${seq}${slug}`.trim() || item.page_capture_id.slice(0, 8)
}

const judgementRows = computed<JudgementRow[]>(() =>
  (props.judgements?.items ?? [])
    .filter(
      (item) =>
        item.differs_from_formal ||
        item.resolution.status === 'conflicting' ||
        item.resolution.status === 'revised',
    )
    .slice(0, judgementLimit)
    .map((item) => ({
      key: item.page_capture_id,
      page: judgementPageLabel(item),
      formal: pageTypeName(item.formal_page_type),
      sources: item.judgements
        .map((record) => `${record.source_label}：${pageTypeName(record.page_type)}`)
        .join('；'),
      resolution:
        item.resolution.status === 'conflicting'
          ? `未知（${item.resolution.conflicting_page_types.map((code) => pageTypeName(code)).join(' / ')}）`
          : `${labelOf(JUDGEMENT_STATUS_LABELS, item.resolution.status, item.resolution.status)} ${pageTypeName(item.resolution.page_type)}`.trim(),
      revisions: item.resolution.revisions.length
        ? item.resolution.revisions
            .map(
              (revision) =>
                `${revision.source_label} ${pageTypeName(revision.from_page_type)} → ${pageTypeName(revision.to_page_type)}`,
            )
            .join('；')
        : '—',
      reason: item.resolution.reason,
      differs: item.differs_from_formal,
    })),
)
const judgementOverflow = computed(() => {
  const flagged = (props.judgements?.items ?? []).filter(
    (item) =>
      item.differs_from_formal ||
      item.resolution.status === 'conflicting' ||
      item.resolution.status === 'revised',
  ).length
  return Math.max(flagged - judgementLimit, 0)
})
const judgementStatusChips = computed(() =>
  countChips(props.judgements?.status_counts, JUDGEMENT_STATUS_LABELS),
)

const headlineChips = computed(() => {
  const chips: string[] = []
  if (graphAvailable.value) {
    chips.push(`页面 ${treeRows.value.length}`)
    chips.push(`弹窗 ${overlayRows.value.length}`)
    chips.push(`外跳 ${externalRows.value.length}`)
    chips.push(`可信跳转 ${trustedTransitionCount.value}/${transitions.value.length}`)
  }
  if (props.gaps) chips.push(`补采缺口 ${props.gaps.total}`)
  if (props.reuse) chips.push(`结论复用 ${props.reuse.total}`)
  if (props.judgements) chips.push(`页型冲突 ${props.judgements.conflicting_count}`)
  if (qualityTotals.value.total) {
    chips.push(`可用截图 ${qualityTotals.value.usable}/${qualityTotals.value.total}`)
  }
  return chips
})
</script>

<template>
  <section class="exploration-process-panel" data-testid="exploration-process-panel">
    <button
      type="button"
      class="process-head"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <div>
        <strong>探索过程</strong>
        <span>页面路线、弹窗与外跳、截图质量、补采缺口与路线；只读投影，不代表设备当前状态</span>
      </div>
      <span class="head-actions">
        <em v-for="chip in headlineChips" :key="chip">{{ chip }}</em>
        <span class="collapse-label">{{ expanded ? '收起' : '展开' }}</span>
      </span>
    </button>

    <div v-if="expanded" class="process-body">
      <section class="process-block" data-testid="process-routes">
        <header>
          <strong>页面路线</strong>
          <span v-if="graphAvailable">
            以到达证明为准；未访问入口不算页面，弹窗挂在所属页面下
          </span>
        </header>
        <p v-if="!graphAvailable" class="empty-text">{{ graphUnavailableText }}</p>
        <template v-else>
          <p v-if="!treeRows.length" class="empty-text">
            还没有身份已确认的业务页面；探索到的观察都停留在未确认状态
          </p>
          <ul v-else class="route-tree">
            <li
              v-for="row in treeRows"
              :key="row.key"
              class="route-row"
              :style="{ paddingLeft: `${row.depth * 18}px` }"
            >
              <span class="route-dot" :class="`tone-${row.status.tone}`"></span>
              <div class="route-main">
                <div class="route-title">
                  <strong>{{ row.pageType }}</strong>
                  <span class="mono">{{ row.label }}</span>
                  <em class="status-badge" :class="`tone-${row.status.tone}`">{{ row.status.label }}</em>
                </div>
                <div class="route-meta">
                  <span>页面状态版本 {{ row.versionCount }}</span>
                  <span>弹窗 {{ row.overlayCount }}</span>
                  <span>{{ row.targetSummary }}</span>
                </div>
                <p v-if="row.reasons.length" class="route-reasons">{{ row.reasons.join('；') }}</p>
              </div>
            </li>
          </ul>
          <div v-if="treeExtras.length" class="chip-row">
            <span v-for="item in treeExtras" :key="item">{{ item }}</span>
          </div>
          <div v-if="blockedTransitionCount" class="failure-box" data-testid="process-failures">
            <strong>阻断或失败的跳转 {{ blockedTransitionCount }} 次</strong>
            <ul>
              <li v-for="reason in failureReasons" :key="reason.code">
                {{ reason.label }} <b>{{ reason.count }}</b>
              </li>
            </ul>
          </div>
        </template>
      </section>

      <section class="process-block" data-testid="process-overlays">
        <header>
          <strong>弹窗与外跳</strong>
          <span>弹窗只留证不算页面；外部页面只采允许的证据，不继续探索</span>
        </header>
        <p v-if="!graphAvailable" class="empty-text">{{ graphUnavailableText }}</p>
        <template v-else>
          <p v-if="!overlayRows.length && !externalRows.length" class="empty-text">
            本次运行没有记录到弹窗或外部跳转
          </p>
          <table v-if="overlayRows.length" class="process-table">
            <thead>
              <tr>
                <th>弹窗</th>
                <th>所在页面</th>
                <th>关闭结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in overlayRows" :key="row.captureId">
                <td>{{ row.state }}</td>
                <td>{{ row.owner }}</td>
                <td>{{ row.outcome }}</td>
              </tr>
            </tbody>
          </table>
          <table v-if="externalRows.length" class="process-table">
            <thead>
              <tr>
                <th>外部地址</th>
                <th>来源页面</th>
                <th>回源</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in externalRows" :key="row.captureId">
                <td class="mono url-cell">{{ row.url }}</td>
                <td>{{ row.from }}</td>
                <td>{{ row.outcome }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </section>

      <section class="process-block" data-testid="process-quality">
        <header>
          <strong>截图质量</strong>
          <span>按展示层评估：底页被遮挡或来自诊断操作的截图不能当作页面证据</span>
        </header>
        <p v-if="!screenshots && screenshotsLoading" class="empty-text">截图证据加载中…</p>
        <p v-else-if="!screenshots" class="empty-text">截图证据尚未加载</p>
        <p v-else-if="!qualityRows.length" class="empty-text">还没有截图证据</p>
        <template v-else>
          <div class="chip-row">
            <span>共 {{ qualityTotals.total }}</span>
            <span class="tone-ok">{{ QUALITY_LABELS.usable }} {{ qualityTotals.usable }}</span>
            <span class="tone-warn">{{ QUALITY_LABELS.occluded }} {{ qualityTotals.occluded }}</span>
            <span class="tone-bad">{{ QUALITY_LABELS.diagnostic }} {{ qualityTotals.diagnostic }}</span>
            <span class="tone-muted">{{ QUALITY_LABELS.unassessed }} {{ qualityTotals.unassessed }}</span>
            <span v-if="qualityTotals.unsettled" class="tone-warn">未稳定 {{ qualityTotals.unsettled }}</span>
          </div>
          <table class="process-table">
            <thead>
              <tr>
                <th>页面</th>
                <th>截图</th>
                <th>可用</th>
                <th>被遮挡</th>
                <th>诊断材料</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in qualityRows" :key="row.key">
                <td>{{ row.page }}</td>
                <td>{{ row.total }}</td>
                <td>{{ row.usable }}</td>
                <td>{{ row.occluded }}</td>
                <td>{{ row.diagnostic }}</td>
                <td>{{ row.notes.length ? row.notes.join('；') : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </section>

      <section class="process-block" data-testid="process-gaps">
        <header>
          <strong>补采缺口与路线</strong>
          <span v-if="gaps">
            {{ labelOf(DISPOSITION_LABELS, gaps.execution_policy, '只记录') }}；路线来自页面图的可信历史跳转
          </span>
        </header>
        <p v-if="!gaps" class="empty-text">证据缺口台账尚未加载</p>
        <template v-else>
          <div class="chip-row">
            <span>缺口 {{ gaps.total }}</span>
            <span v-for="chip in gapPhaseChips" :key="`phase-${chip}`">{{ chip }}</span>
            <span v-for="chip in gapPolicyChips" :key="`policy-${chip}`">{{ chip }}</span>
            <span v-for="chip in gapRouteChips" :key="`route-${chip}`">{{ chip }}</span>
          </div>
          <p v-if="routePolicyText" class="policy-text">{{ routePolicyText }}</p>
          <p v-if="!gapRows.length" class="empty-text">当前运行没有证据不足的判定</p>
          <table v-else class="process-table">
            <thead>
              <tr>
                <th>规则</th>
                <th>页面</th>
                <th>缺什么</th>
                <th>补采方式</th>
                <th>阶段</th>
                <th>可用路线</th>
                <th>处置</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in gapRows" :key="row.key">
                <td>{{ row.rule }}</td>
                <td>{{ row.page }}</td>
                <td class="missing-cell">{{ row.missing }}</td>
                <td>{{ row.policy }}</td>
                <td>{{ row.phase }}</td>
                <td>
                  {{ row.route }}
                  <em v-if="row.advice" class="route-advice">{{ row.advice }}</em>
                </td>
                <td>{{ row.disposition }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="gapOverflow" class="empty-text">还有 {{ gapOverflow }} 条缺口未展开</p>
        </template>
      </section>

      <section class="process-block" data-testid="process-judgements">
        <header>
          <strong>页型判断与改判</strong>
          <span v-if="judgements">只记录不改正式页型；各来源判断各留依据，打架就按未知</span>
        </header>
        <p v-if="!judgements" class="empty-text">页型判断记录尚未加载</p>
        <template v-else>
          <div class="chip-row">
            <span>有判断记录的页面 {{ judgements.total }}</span>
            <span v-for="chip in judgementStatusChips" :key="`judgement-${chip}`">{{ chip }}</span>
            <span v-if="judgements.differs_count" class="tone-warn">
              与正式识别不一致 {{ judgements.differs_count }}
            </span>
          </div>
          <p v-if="!judgementRows.length" class="empty-text">
            各页面的判断一致，没有冲突或改判
          </p>
          <table v-else class="process-table">
            <thead>
              <tr>
                <th>页面</th>
                <th>正式识别</th>
                <th>各来源判断</th>
                <th>按记录裁定</th>
                <th>改判</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in judgementRows" :key="row.key">
                <td>{{ row.page }}</td>
                <td>{{ row.formal }}</td>
                <td>{{ row.sources }}</td>
                <td :class="{ 'tone-warn': row.differs }">{{ row.resolution }}</td>
                <td>{{ row.revisions }}</td>
                <td>{{ row.reason }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="judgementOverflow" class="empty-text">还有 {{ judgementOverflow }} 个页面未展开</p>
        </template>
      </section>

      <section class="process-block" data-testid="process-reuse">
        <header>
          <strong>规则复用</strong>
          <span v-if="reuse">只列沿用来的结论，真实执行看规则结果列表</span>
        </header>
        <p v-if="!reuse" class="empty-text">规则复用记录尚未加载</p>
        <template v-else>
          <div class="chip-row">
            <span>复用 {{ reuse.total }}</span>
            <span v-for="chip in reuseScopeChips" :key="`scope-${chip}`">{{ chip }}</span>
          </div>
          <p v-if="!reuseRows.length" class="empty-text">
            本次任务没有沿用别处的结论，规则都按页面各自执行
          </p>
          <table v-else class="process-table">
            <thead>
              <tr>
                <th>规则</th>
                <th>这一页</th>
                <th>沿用自</th>
                <th>结论</th>
                <th>复用范围</th>
                <th>为什么能复用</th>
                <th>什么情况失效</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in reuseRows" :key="row.key">
                <td>{{ row.rule }}</td>
                <td>{{ row.page }}</td>
                <td>{{ row.source }}</td>
                <td>{{ row.status }}</td>
                <td>{{ row.scope }}</td>
                <td>{{ row.reason }}</td>
                <td>{{ row.invalidation }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="reuseOverflow" class="empty-text">还有 {{ reuseOverflow }} 条复用记录未展开</p>
        </template>
      </section>
    </div>
  </section>
</template>

<style scoped>
.exploration-process-panel {
  margin: 0 20px 16px;
  padding: 14px 16px;
  border: 1px solid rgba(129, 140, 248, 0.2);
  border-radius: 12px;
  background: rgba(99, 102, 241, 0.06);
}

.process-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  width: 100%;
  padding: 0;
  border: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  background: transparent;
  cursor: pointer;
}

.process-head strong {
  font-size: 15px;
}

.process-head > div > span {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  opacity: 0.72;
}

.head-actions {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.head-actions em {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-style: normal;
  background: rgba(129, 140, 248, 0.16);
}

.collapse-label {
  font-size: 12px;
  opacity: 0.7;
}

.process-body {
  display: grid;
  gap: 14px;
  margin-top: 14px;
}

.process-block {
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--workstation-panel-soft);
}

.process-block header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
}

.process-block header span {
  font-size: 12px;
  opacity: 0.7;
}

.empty-text,
.policy-text {
  margin: 4px 0;
  font-size: 12px;
  opacity: 0.75;
}

.route-tree {
  margin: 0;
  padding: 0;
  list-style: none;
}

.route-row {
  display: flex;
  gap: 10px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--workstation-border);
}

.route-row:last-child {
  border-bottom: 0;
}

.route-dot {
  flex-shrink: 0;
  width: 9px;
  height: 9px;
  margin-top: 6px;
  border-radius: 50%;
  background: #94a3b8;
}

.route-main {
  min-width: 0;
  flex: 1;
}

.route-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.route-meta,
.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.8;
}

.chip-row span {
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.14);
}

.route-reasons {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--workstation-warning);
}

.status-badge {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-style: normal;
  background: rgba(148, 163, 184, 0.18);
}

.tone-ok {
  color: var(--workstation-success);
}

.tone-warn {
  color: var(--workstation-warning);
}

.tone-bad {
  color: var(--workstation-danger);
}

.tone-info {
  color: var(--workstation-primary);
}

.tone-muted {
  color: var(--workstation-text-muted);
}

.route-dot.tone-ok {
  background: #22c55e;
}

.route-dot.tone-warn {
  background: #fbbf24;
}

.route-dot.tone-bad {
  background: #ef4444;
}

.route-dot.tone-info {
  background: #38bdf8;
}

.failure-box {
  margin-top: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(239, 68, 68, 0.1);
  font-size: 12px;
}

.failure-box ul {
  margin: 6px 0 0;
  padding-left: 18px;
}

.process-table {
  width: 100%;
  margin-top: 8px;
  border-collapse: collapse;
  font-size: 12px;
}

.process-table th,
.process-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--workstation-border);
  text-align: left;
  vertical-align: top;
}

.process-table th {
  font-weight: 600;
  opacity: 0.8;
}

.missing-cell {
  max-width: 360px;
}

.route-advice {
  display: block;
  margin-top: 2px;
  font-style: normal;
  opacity: 0.7;
}

.url-cell {
  max-width: 320px;
  word-break: break-all;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  opacity: 0.85;
}
</style>
