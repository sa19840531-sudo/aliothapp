import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LocationQuery } from 'vue-router'
import type { ScreenshotGroup, ScreenshotItem } from '@/types/task-insight'

enableAutoUnmount(afterEach)

const mocks = vi.hoisted(() => ({
  getTask: vi.fn(),
  getAgentRun: vi.fn(),
  getPageTimeline: vi.fn(),
  getRuleResults: vi.fn(),
  getTaskStats: vi.fn(),
  getTaskScreenshots: vi.fn(),
  getTaskLogs: vi.fn(),
  getAppStage: vi.fn(),
  getInspectionWorkItems: vi.fn(),
  getPageGraph: vi.fn(),
  getPageRecoveryRoutes: vi.fn(),
  getEvidenceGaps: vi.fn(),
  getRuleReuse: vi.fn(),
  getPageTypeJudgements: vi.fn(),
  getClosureOutcome: vi.fn(),
  getSystemStatus: vi.fn(),
  performTaskOperation: vi.fn(),
  routerPush: vi.fn(),
  route: { path: '/tasks/task-h5', params: { taskId: 'task-h5' }, query: {} as LocationQuery },
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.routerPush }),
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const passthrough = defineComponent({
    setup(_, { attrs, slots }) {
      return () => h('div', attrs, [slots.icon?.(), slots.trigger?.(), slots.default?.()])
    },
  })
  const button = defineComponent({
    setup(_, { attrs, slots }) {
      return () => h('button', attrs, [slots.icon?.(), slots.default?.()])
    },
  })
  // 和真的弹窗一样只在 show 为真时渲染内容
  const modal = defineComponent({
    props: { show: Boolean },
    setup(props, { slots }) {
      return () => (props.show ? h('div', { 'data-testid': 'modal' }, slots.default?.()) : null)
    },
  })
  const popconfirm = defineComponent({
    name: 'TaskCancelConfirm',
    emits: ['positive-click'],
    setup(_, { attrs, slots }) {
      return () => h('div', attrs, [slots.trigger?.(), slots.default?.()])
    },
  })
  return {
    NButton: button,
    NIcon: passthrough,
    NModal: modal,
    NPopconfirm: popconfirm,
    NSelect: passthrough,
    NSpin: passthrough,
    useMessage: () => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
  }
})

vi.mock('@vicons/ionicons5', async () => {
  const { defineComponent, h } = await import('vue')
  const icon = defineComponent({ render: () => h('span') })
  return {
    ChevronBackOutline: icon,
    ChevronForwardOutline: icon,
    ClipboardOutline: icon,
    CloseCircleOutline: icon,
    CopyOutline: icon,
    DocumentTextOutline: icon,
    DownloadOutline: icon,
    ImageOutline: icon,
    RefreshOutline: icon,
    SearchOutline: icon,
    TerminalOutline: icon,
    WarningOutline: icon,
  }
})

vi.mock('@/components/PageExplorationPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      props: { summary: { type: Object, required: true } },
      setup: () => () => h('div', { 'data-testid': 'page-exploration-panel' }),
    }),
  }
})

vi.mock('@/modules/tasks/components/ExplorationProcessPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      props: { graph: { type: Object, default: null }, gaps: { type: Object, default: null } },
      setup: () => () => h('div', { 'data-testid': 'exploration-process-panel' }),
    }),
  }
})

vi.mock('@/modules/tasks/components/AppStagePanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ render: () => h('div') }) }
})

vi.mock('@/modules/tasks/components/ScreenshotEvidenceViewer.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'ScreenshotEvidenceViewer',
      props: ['entries', 'title', 'collection', 'initialArtifactId', 'imageUrl', 'formatTime'],
      emits: ['close', 'download'],
      setup: () => () => h('div', { 'data-testid': 'screenshot-evidence-viewer' }),
    }),
  }
})

vi.mock('@/services/tasks.api', () => ({
  getTask: mocks.getTask,
  getAgentRun: mocks.getAgentRun,
  performTaskOperation: mocks.performTaskOperation,
}))
vi.mock('@/services/system.api', () => ({ getSystemStatus: mocks.getSystemStatus }))
vi.mock('@/services/task-insight.api', () => ({
  issueEvidenceFileUrl: vi.fn(async () => ''),
  issueEvidenceFileTicket: vi.fn(async () => ({ url: '', expires_at: '' })),
  downloadLogsExport: vi.fn(async () => undefined),
  downloadPlatformProgressExport: vi.fn(async () => undefined),
  downloadScreenshotsArchive: vi.fn(async () => undefined),
  getAppStage: mocks.getAppStage,
  getInspectionWorkItems: mocks.getInspectionWorkItems,
  getPageGraph: mocks.getPageGraph,
  getPageRecoveryRoutes: mocks.getPageRecoveryRoutes,
  getEvidenceGaps: mocks.getEvidenceGaps,
  getRuleReuse: mocks.getRuleReuse,
  getPageTypeJudgements: mocks.getPageTypeJudgements,
  getClosureOutcome: mocks.getClosureOutcome,
  getPageTimeline: mocks.getPageTimeline,
  getRuleResults: mocks.getRuleResults,
  getTaskLogs: mocks.getTaskLogs,
  getTaskScreenshots: mocks.getTaskScreenshots,
  getTaskStats: mocks.getTaskStats,
}))

import { authStore } from '@/stores/auth'
import TaskDetailView from './TaskDetailView.vue'

function explorationSummary(qualityStatus: 'complete' | 'needs_review' | 'technical_failed') {
  return {
    tracking_available: true,
    complete: qualityStatus === 'complete',
    quality_status: qualityStatus,
    requires_review: qualityStatus === 'needs_review',
    page_total: 1,
    active_page_count: 1,
    terminal_page_count: 1,
    completed_page_count: 1,
    degraded_page_count: 0,
    failed_page_count: 0,
    pending_page_count: 0,
    superseded_page_count: 0,
    abandoned_page_count: 0,
    evidence_only_page_count: 0,
    page_status_counts: { completed: 1 },
    page_lifecycle_counts: { active: 1 },
    action_total: 2,
    pending_action_count: 0,
    explored_action_count: 2,
    inactive_action_count: 0,
    failed_action_count: 0,
    blocked_action_count: 0,
    skipped_action_count: 0,
    action_status_counts: { explored: 2 },
    pending_work_count: 0,
    coverage: {
      catalog_observation: {
        catalog_page_types: ['home'],
        catalog_page_type_total: 1,
        observed_page_types: ['home'],
        observed_page_type_count: 1,
        business_observed_page_types: ['home'],
        missing_catalog_page_types: [],
        raw_coverage_rate: 1,
      },
      lifecycle_shells: {
        observed_page_types: [],
        strict_line_loading_observed: false,
        startup_ad_observed: false,
        loading_state_observed: false,
      },
      product_business: {
        assessment_available: true,
        assessment_scope_complete: true,
        assessment_scope_verified: true,
        assessment_complete: true,
        missing_assessment_page_types: [],
        applicable_page_types: ['home'],
        covered_page_types: ['home'],
        known_uncovered_page_types: [],
        unresolved_page_types: [],
        not_applicable_page_types: [],
        covered_count: 1,
        applicable_count: 1,
        coverage_rate: 1,
      },
      exploration: {
        action_total: 2,
        terminal_count: 2,
        pending_count: 0,
        explored_count: 2,
        blocked_count: 0,
        skipped_count: 0,
        failed_count: 0,
        terminal_rate: 1,
        explored_rate: 1,
      },
      gaps: [],
      acceptance_complete: true,
    },
    page_results: [],
  }
}

function h5Timeline(options: {
  passCount: number
  insufficientCount?: number
  failedCount?: number
  qualityStatus?: 'complete' | 'needs_review' | 'technical_failed'
  terminalReason?: string | null
}) {
  const insufficientCount = options.insufficientCount ?? 0
  const failedCount = options.failedCount ?? 0
  const qualityStatus = options.qualityStatus ?? 'complete'
  return {
    task_id: 'task-h5',
    task_status: 'done',
    run_id: 'run-h5',
    journey_steps: [],
    current_node_seq: null,
    exploration_summary: explorationSummary(qualityStatus),
    exploration_actions: [],
    nodes: [] as Array<Record<string, unknown>>,
    frontier: {
      snapshot_id: 'frontier-1',
      pending_count: 0,
      blocked_count: 0,
      failed_count: 0,
      executed_count: 2,
      covered_page_types: ['home'],
    },
    h5_assessments: [
      {
        assessment_id: 'assessment-1',
        page_capture_id: 'capture-1',
        page_capture_ids: ['capture-1'],
        page_type: 'home',
        page_state: 'stable',
        status: insufficientCount ? 'evidence_insufficient' : 'covered',
        reason_codes: insufficientCount ? ['missing_screenshot'] : [],
        reason_code: insufficientCount ? 'missing_screenshot' : 'covered',
        catalog_sha256: 'catalog-1',
      },
    ],
    h5_evidence_inventory: {
      item_count: options.passCount + insufficientCount + failedCount,
      pass_count: options.passCount,
      insufficient_count: insufficientCount,
      failed_count: failedCount,
      items: [],
    },
    terminal_reason: options.terminalReason ?? null,
    execution_mode: 'exploration_only',
    full_inspection_closure: {},
  }
}

function task(overrides: Record<string, unknown> = {}) {
  return {
    task_id: 'task-h5',
    task_name: 'H5 探索任务',
    product: { department_name: '质检部', product_code: 'H5-1', product_name: '移动站点' },
    target_url: 'https://example.test',
    target_platform: 'h5',
    execution_mode: 'exploration_only',
    journey_steps: [],
    status: 'done',
    progress: 100,
    report_status: 'generated',
    failure_reason: null,
    available_actions: ['detail', 'report', 'exploration', 'evidence', 'rerun', 'copy'],
    created_at: '2026-08-13T00:00:00Z',
    started_at: '2026-08-13T00:00:01Z',
    finished_at: '2026-08-13T00:01:00Z',
    ruleset_code: 'alioth_first_ruleset',
    ruleset_version: 1,
    channel: 'workbench',
    priority: 100,
    run_id: 'run-h5',
    scan_mode: 'full',
    remark: null,
    required_page_types: [],
    browser_profile_id: null,
    ...overrides,
  }
}

async function mountTaskDetail(
  timeline: ReturnType<typeof h5Timeline>,
  taskOverrides: Record<string, unknown> = {},
) {
  mocks.getTask.mockResolvedValue(task(taskOverrides))
  mocks.getPageTimeline.mockResolvedValue(timeline)
  const wrapper = mount(TaskDetailView)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.route.path = '/tasks/task-h5'
  mocks.route.query = {}
  authStore.session.value = null
  mocks.getAgentRun.mockResolvedValue({ run: null })
  mocks.getRuleResults.mockResolvedValue({ items: [] })
  mocks.getTaskStats.mockResolvedValue({
    total: 0,
    triggered: 0,
    frontier_pending: 0,
    progress_percent: 100,
    finished: 5,
    status_counts: {},
    recent_anomalies: [],
  })
  mocks.getTaskScreenshots.mockResolvedValue({ groups: [] })
  mocks.getTaskLogs.mockResolvedValue({ items: [], total: 0 })
  mocks.getAppStage.mockResolvedValue({ applicable: false })
  mocks.getPageGraph.mockResolvedValue({
    protocol_version: 'page_graph.v1',
    status: 'unavailable',
    reason_code: 'graph_repository_not_configured',
    task_id: 'task-1',
    states: [],
    transitions: [],
  })
  mocks.getPageRecoveryRoutes.mockResolvedValue({
    protocol_version: 'page_graph_recovery.v1',
    status: 'unavailable',
    routes: [],
  })
  mocks.getEvidenceGaps.mockResolvedValue({
    protocol_version: 'evidence-gap-list.v1',
    task_id: 'task-1',
    task_status: 'review',
    run_id: 'run-1',
    total: 0,
    phase_counts: {},
    policy_counts: {},
    disposition_counts: {},
    execution_policy: 'recorded_only',
    items: [],
  })
  mocks.getInspectionWorkItems.mockResolvedValue({
    summary: { work_item_count: 0, status_counts: {}, rule_status_counts: {}, attempt_status_counts: {} },
    items: [],
  })
})

describe('任务状态及时同步', () => {
  it('42小时仍执行的任务提示核查并提供取消，终态刷新后移除提醒', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    vi.setSystemTime(new Date('2026-09-30T12:00:00Z'))
    mocks.getTask.mockResolvedValueOnce(task({ status: 'running', started_at: '2026-09-28T18:00:00Z', finished_at: null, available_actions: ['detail', 'cancel'] }))
      .mockResolvedValue(task({ status: 'canceled', started_at: '2026-09-28T18:00:00Z', finished_at: '2026-09-30T12:00:01Z', available_actions: ['copy'] }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    const wrapper = mount(TaskDetailView)
    await flushPromises()
    expect(wrapper.get('[data-testid="long-execution-alert"]').text()).toContain('42:00:00')
    expect(wrapper.get('[data-testid="long-execution-alert"]').text()).toContain('取消任务')
    expect(mocks.performTaskOperation).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.find('[data-testid="long-execution-alert"]').exists()).toBe(false)
  })

  it.each([null, 'invalid'])('运行时间缺失或无效%s时不伪造超时提醒', async (started_at) => {
    mocks.getTask.mockResolvedValue(task({ status: 'running', started_at, finished_at: null }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    const wrapper = mount(TaskDetailView)
    await flushPromises()
    expect(wrapper.find('[data-testid="long-execution-alert"]').exists()).toBe(false)
    expect(wrapper.get('.progress-footnote').text()).toContain('--:--:--')
  })

  afterEach(() => vi.useRealTimers())

  it('规则明细挂起时仍轮询概览，取消中继续刷新，终态立即移除取消入口', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    mocks.getTask
      .mockResolvedValueOnce(task({ status: 'running', progress: 95, available_actions: ['detail', 'cancel'] }))
      .mockResolvedValueOnce(task({ status: 'canceling', progress: 95, available_actions: ['detail'] }))
      .mockResolvedValue(task({ status: 'canceled', progress: 0, available_actions: ['copy'] }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    mocks.getRuleResults.mockImplementation(() => new Promise(() => {}))
    const wrapper = mount(TaskDetailView)
    await flushPromises()
    expect(wrapper.findAll('button').some((button) => button.text() === '取消任务')).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(mocks.getTask).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('button').some((button) => button.text() === '取消任务')).toBe(false)
    expect(wrapper.text()).toContain('取消中')
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(mocks.getTask).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain('已取消')
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(6000)
    expect(mocks.getTask).toHaveBeenCalledTimes(3)
  })

  it.each(['canceled', 'done'])('旧明细在途进入 %s 后串行补读终态明细，迟到结果不覆盖概览', async (status) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    let finishOldRules!: (value: { items: [] }) => void
    let finishOldStats!: (value: Record<string, unknown>) => void
    mocks.getTask
      .mockResolvedValueOnce(task({ execution_mode: 'full_inspection', status: 'running', progress: 95, available_actions: ['detail', 'cancel'] }))
      .mockResolvedValue(task({ execution_mode: 'full_inspection', status, progress: status === 'done' ? 100 : 0, available_actions: ['detail'] }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    mocks.getRuleResults
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldRules = resolve }))
      .mockResolvedValue({ items: [] })
    mocks.getTaskStats
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldStats = resolve }))
      .mockResolvedValue({ total: 2, finished: 2, triggered: 2, frontier_pending: 0, progress_percent: 100, status_counts: {}, recent_anomalies: [] })
    const wrapper = mount(TaskDetailView)
    await flushPromises()

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(mocks.getTask).toHaveBeenCalledTimes(2)
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('button').some((button) => button.text() === '取消任务')).toBe(false)

    finishOldStats({ total: 99, finished: 0, triggered: 99, frontier_pending: 0, progress_percent: 0, status_counts: {}, recent_anomalies: [] })
    await flushPromises()
    expect(wrapper.get('.progress-footnote').text()).not.toContain('0/99')
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(1)

    finishOldRules({ items: [] })
    await flushPromises()
    expect(mocks.getTask).toHaveBeenCalledTimes(3)
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(2)
    expect(mocks.getTaskStats).toHaveBeenCalledTimes(2)
    expect(wrapper.get('.progress-footnote').text()).toContain('规则已完成 2/2 项')
    await vi.advanceTimersByTimeAsync(9000)
    expect(mocks.getTask).toHaveBeenCalledTimes(3)
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(2)
  })

  it('同一任务出现新的 Run 时等待旧批次完成后补读，不混入旧 Run 的慢明细', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    let finishOldRules!: (value: { items: [] }) => void
    mocks.getTask
      .mockResolvedValueOnce(task({ status: 'running', run_id: 'run-old' }))
      .mockResolvedValue(task({ status: 'running', run_id: 'run-new' }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    mocks.getRuleResults
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldRules = resolve }))
      .mockResolvedValue({ items: [] })
    const wrapper = mount(TaskDetailView)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(1)

    finishOldRules({ items: [] })
    await flushPromises()
    expect(mocks.getTask).toHaveBeenCalledTimes(3)
    expect(mocks.getRuleResults).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('执行中')
  })

  it('Main 失败但根任务仍收口时显示执行异常和真实原因，保留可取消入口', async () => {
    mocks.getTask.mockResolvedValue(task({
      status: 'running', run_status: 'failed', progress: 95,
      failure_reason: 'popup_dismissal_stalled:3/3', available_actions: ['detail', 'cancel'],
      execution_mode: 'full_inspection',
    }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    const wrapper = mount(TaskDetailView)
    await flushPromises()
    expect(wrapper.text()).toContain('执行异常，收口中')
    expect(wrapper.get('[data-testid="stopped-run-alert"]').text()).toContain('popup_dismissal_stalled:3/3')
    expect(wrapper.findAll('button').some((button) => button.text() === '取消任务')).toBe(true)
  })

  it('取消与任务结束并发导致接口拒绝时立即重新读取任务并移除操作', async () => {
    mocks.getTask
      .mockResolvedValueOnce(task({ status: 'running', available_actions: ['detail', 'cancel'] }))
      .mockResolvedValue(task({ status: 'failed', available_actions: ['failure', 'rerun', 'copy'] }))
    mocks.getPageTimeline.mockResolvedValue(h5Timeline({ passCount: 0 }))
    mocks.performTaskOperation.mockRejectedValueOnce(new Error('任务运行记录不存在或已进入终态'))
    const wrapper = mount(TaskDetailView)
    await flushPromises()
    wrapper.getComponent({ name: 'TaskCancelConfirm' }).vm.$emit('positive-click')
    await flushPromises()
    expect(mocks.performTaskOperation).toHaveBeenCalledWith('task-h5', 'cancel')
    expect(mocks.getTask).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('button').some((button) => button.text() === '取消任务')).toBe(false)
    expect(wrapper.text()).toContain('执行失败')
  })
})

describe('任务中心进度和返回状态', () => {
  it('H5 同页和弹窗采集不展示成独立业务页面数量', async () => {
    const timeline = h5Timeline({ passCount: 1 })
    timeline.exploration_summary.page_total = 14
    const wrapper = await mountTaskDetail(timeline, { status: 'running' })
    expect(wrapper.get('.progress-footnote').text()).toContain('页面观察 14 条（含同页与弹窗）')
    expect(wrapper.get('[data-testid="timeline-summary"]').text()).toContain('条页面观察')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('已探索页面')
  })

  it('H5 待复核的部分完成保留执行进度与缺口说明，不强改为百分之百', async () => {
    mocks.getTaskStats.mockResolvedValue({ total: 265, triggered: 265, finished: 147, frontier_pending: 0, progress_percent: 55, status_counts: {}, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 1 }), { execution_mode: 'full_inspection', status: 'review', progress: 55 })
    expect(wrapper.get('.progress-meta').text()).toContain('任务进度')
    expect(wrapper.get('.progress-meta strong').text()).toBe('55%')
    expect(wrapper.get('[data-testid="h5-progress-explanation"]').text()).toContain('证据不足或执行异常的缺口独立保留')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则已完成 147/265 项')
  })

  it.each(['app', 'web'])('其他平台 %s 保留原进度展示', async (target_platform) => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 1 }), { target_platform, execution_mode: 'full_inspection', status: 'review', progress: 55 })
    expect(wrapper.get('.progress-meta').text()).toContain('任务进度')
    expect(wrapper.find('[data-testid="h5-progress-explanation"]').exists()).toBe(false)
    expect(wrapper.get('.progress-footnote').text()).toContain('已探索页面 1 个')
  })

  it('未执行规则的待复核任务显示执行进度 100%，独立保留规则零完成', async () => {
    mocks.getTaskStats.mockResolvedValue({ total: 73, triggered: 73, finished: 0, frontier_pending: 0, progress_percent: 100, status_counts: { not_executed: 73 }, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100 })
    expect(wrapper.get('.progress-meta strong').text()).toBe('100%')
    expect(wrapper.get('.progress-fill').attributes('style')).toContain('width: 100%')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则已完成 0/73 项')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则检查未完成 73 项')
    expect(wrapper.get('.progress-review-note').text()).toContain('规则检查缺口已保留在结果中。')
  })

  it('待复核任务执行进度为 100%，独立保留 116/208 规则完成和 92 项缺口', async () => {
    mocks.getTaskStats.mockResolvedValue({ total: 208, triggered: 188, finished: 116, frontier_pending: 0, progress_percent: 100, status_counts: { passed: 87, failed: 11, not_applicable: 18, evidence_insufficient: 69, detect_error: 3, not_executed: 20 }, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100, finalization_state: 'sealed' })
    expect(wrapper.get('.progress-meta').text()).toContain('任务进度')
    expect(wrapper.get('.progress-meta strong').text()).toBe('100%')
    expect(wrapper.get('.progress-fill').attributes('style')).toContain('width: 100%')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则已完成 116/208 项')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则检查未完成 92 项')
    expect(wrapper.get('.progress-review-note').text()).toContain('自动执行已结束，待复核')
    expect(wrapper.get('.progress-review-note').text()).not.toContain('已进入复核')
    expect(wrapper.get('.progress-review-note').text()).toContain('规则检查缺口已保留在结果中。')
  })

  it.each([
    { finalization_state: 'finalizing' },
    { finalization_state: undefined },
    { finalization_state: 'sealed', finished_at: null },
  ])('待复核但缺少正式执行结束事实时仅说明已进入复核：%j', async (overrides) => {
    mocks.getTaskStats.mockResolvedValue({ total: 208, triggered: 208, finished: 116, frontier_pending: 0, progress_percent: 100, status_counts: {}, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100, ...overrides })
    expect(wrapper.get('.progress-review-note').text()).toContain('已进入复核')
    expect(wrapper.get('.progress-review-note').text()).not.toContain('自动执行已结束')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则检查未完成 92 项')
  })

  it('已执行完毕的真实业务复核保持完整进度和规则完成数', async () => {
    mocks.getTaskStats.mockResolvedValue({ total: 73, triggered: 73, finished: 73, frontier_pending: 0, progress_percent: 100, status_counts: { manual_review: 73 }, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100 })
    expect(wrapper.get('.progress-meta strong').text()).toBe('100%')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则已完成 73/73 项')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('未完成')
    expect(wrapper.get('.progress-review-note').text()).not.toContain('规则检查缺口已保留在结果中。')
  })

  it('待复核规则统计尚未加载时不显示 0/0 或推断未完成数量', async () => {
    let finishStats!: (value: Record<string, unknown>) => void
    mocks.getTaskStats.mockImplementationOnce(() => new Promise((resolve) => { finishStats = resolve }))
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100, finalization_state: 'sealed' })
    expect(wrapper.get('.progress-footnote').text()).toContain('规则统计加载中')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('0/0')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('未完成')
    expect(wrapper.get('.progress-review-note').text()).not.toContain('规则检查缺口已保留在结果中。')
    finishStats({ total: 208, triggered: 208, finished: 116, frontier_pending: 0, progress_percent: 100, status_counts: {}, recent_anomalies: [] })
    await flushPromises()
    expect(wrapper.get('.progress-footnote').text()).toContain('规则检查未完成 92 项')
  })

  it('规则统计加载失败时说明暂不可用，不把未知统计展示为全部完成', async () => {
    mocks.getTaskStats.mockRejectedValue(new Error('统计暂不可用'))
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100 })
    expect(wrapper.get('.progress-footnote').text()).toContain('规则统计暂不可用')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('0/0')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('未完成')
  })

  it.each([
    { total: 208 },
    { finished: 116 },
    { total: -1, finished: 116 },
    { total: 208, finished: Number.NaN },
    { total: Number.POSITIVE_INFINITY, finished: 116 },
  ])('规则统计完成计数缺失或无效时不展示完成数量：%j', async (counts) => {
    mocks.getTaskStats.mockResolvedValue({ ...counts, triggered: 188, frontier_pending: 0, progress_percent: 100, status_counts: {}, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { execution_mode: 'full_inspection', status: 'review', progress: 100, finalization_state: 'sealed' })
    expect(wrapper.get('.progress-meta strong').text()).toBe('100%')
    expect(wrapper.get('.progress-footnote').text()).toContain('规则统计暂不可用')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('规则已完成')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('未完成')
    expect(wrapper.get('.progress-review-note').text()).not.toContain('规则检查缺口已保留在结果中。')
  })

  it('仅探索任务保留任务进度并且不展示规则缺口', async () => {
    mocks.getTaskStats.mockResolvedValue({ total: 208, triggered: 208, finished: 116, frontier_pending: 0, progress_percent: 100, status_counts: {}, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), { status: 'review', progress: 100, finalization_state: 'sealed' })
    expect(wrapper.get('.progress-meta').text()).toContain('任务进度')
    expect(wrapper.get('.progress-meta').text()).not.toContain('规则检查进度')
    expect(wrapper.get('.progress-footnote').text()).toContain('不执行正式规则')
    expect(wrapper.get('.progress-footnote').text()).not.toContain('未完成')
    expect(wrapper.get('.progress-review-note').text()).not.toContain('规则检查缺口已保留在结果中。')
  })

  it('主任务进度采用与列表一致的 progress，不受规则统计百分比或 95% 上限影响', async () => {
    mocks.getTaskStats.mockResolvedValue({ total: 10, triggered: 5, frontier_pending: 0, progress_percent: 58, status_counts: {}, recent_anomalies: [] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 1 }), { status: 'running', progress: 99 })
    expect(wrapper.get('.progress-meta strong').text()).toBe('99%')
    expect(wrapper.get('.progress-fill').attributes('style')).toContain('width: 99%')
  })

  it('详情返回任务中心保留列表页码和筛选，同时沿用管理端门户', async () => {
    mocks.route.path = '/admin/tasks/task-h5'
    mocks.route.query = { page: '4', page_size: '10', task_name: '巡检', product_code: 'H5-1', submitter_name: '张三', status: 'done' }
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 1 }))
    await wrapper.get('.breadcrumb-link').trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith({ path: '/admin/tasks', query: mocks.route.query })
  })
})

describe('H5 探索结果终态', () => {
  it('Web 逻辑页面卡片展示主版本和历史证据数量', async () => {
    const timeline: any = h5Timeline({ passCount: 1 })
    timeline.nodes = [
      {
        node_seq: 1,
        source: 'h5_v2_capture',
        step_name: 'content_list',
        page_type: 'content_list',
        page_type_name: '内容列表页',
        page_state: 'stable',
        status: 'recognized',
        status_label: '已识别',
        is_current: false,
        page_capture_id: 'capture-content',
        logical_page_id: 'web-logical:abc',
        canonical_url: 'https://example.test/news?id=42',
        capture_ids: ['capture-ready', 'capture-content'],
        capture_count: 2,
        primary_capture_id: 'capture-content',
        capture_versions: [
          {
            page_capture_id: 'capture-ready',
            capture_purpose: 'web_page_ready_baseline',
            captured_at: '2026-08-13T00:00:02Z',
            page_type: 'content_list',
            inspection_status: 'completed',
            screenshot_artifact_id: 'shot-ready',
          },
          {
            page_capture_id: 'capture-content',
            capture_purpose: 'web_primary_content_evidence',
            captured_at: '2026-08-13T00:00:03Z',
            page_type: 'content_list',
            inspection_status: 'completed',
            screenshot_artifact_id: 'shot-content',
          },
        ],
        url: 'https://example.test/news?id=42',
        screenshot_artifact_id: null,
        detection: { status: 'recognized', confidence: 0.96, duration_ms: 20 },
        inspection: { status: 'completed' },
        element_count: 42,
        rule_progress: { executed: 1, total: 1 },
        result_rollup: 'pass',
        result_rollup_label: '通过',
        result_counts: { pass: 1 },
        attention_items: [],
        current_rule_code: null,
        first_visited_at: '2026-08-13T00:00:02Z',
        captured_at: '2026-08-13T00:00:03Z',
        finished_at: '2026-08-13T00:00:03Z',
      },
    ]

    const wrapper = await mountTaskDetail(timeline, {
      target_platform: 'web',
      execution_mode: 'full_inspection',
    })

    expect(wrapper.findAll('.timeline-node')).toHaveLength(1)
    expect(wrapper.text()).toContain('证据版本 2 个')
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    expect(wrapper.get('[data-testid="page-story"]').text()).toContain('前后采了 2 次证据，以最新的主版本为准')
  })

  it('时间线不显示导航前的浏览器空白页', async () => {
    const timeline: any = h5Timeline({ passCount: 1 })
    const baseNode = {
      source: 'h5_v2_capture',
      step_name: null,
      page_state: 'loaded',
      status: 'captured',
      status_label: '已采集',
      is_current: false,
      screenshot_artifact_id: null,
      detection: { status: null, confidence: null, duration_ms: null },
      inspection: { status: null },
      element_count: null,
      rule_progress: { executed: 0, total: null },
      result_rollup: null,
      result_rollup_label: null,
      result_counts: {},
      attention_items: [],
      current_rule_code: null,
      first_visited_at: '2026-08-13T00:00:02Z',
      captured_at: '2026-08-13T00:00:02Z',
      finished_at: null,
    }
    timeline.nodes = [
      { ...baseNode, node_seq: 1, page_type: null, page_type_name: null, page_capture_id: 'capture-blank', url: 'about:blank' },
      { ...baseNode, node_seq: 2, page_type: 'home', page_type_name: '首页', page_capture_id: 'capture-home', url: 'https://heiliao.com/' },
    ]

    const wrapper = await mountTaskDetail(timeline, { execution_mode: 'full_inspection' })

    expect(wrapper.findAll('.timeline-node')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('about:blank')
  })

  it('Web 探索任务展示冻结的浏览器配置与原始截图分辨率', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      task_id: 'task-web',
      task_name: 'Web 官网探索',
      target_platform: 'web',
      target_url: 'https://example.test',
      browser_profile_id: 'desktop_fhd_1920x1080_v1',
      viewport_width: 1920,
      viewport_height: 1080,
    })

    const profile = wrapper.get('[data-testid="web-browser-profile"]')
    expect(profile.text()).toContain('desktop_fhd_1920x1080_v1')
    expect(profile.text()).toContain('1920 × 1080')
    expect(profile.text()).toContain('PNG 无损压缩')
  })

  it('仅探索完成时即使后端误带报告动作也隐藏报告入口并展示证据结果', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 2 }))

    expect(wrapper.text()).not.toContain('查看报告')
    expect(wrapper.text()).toContain('页面探索已完成')
    expect(wrapper.text()).toContain('证据完整 2 项')
  })

  it('证据不足时展示需要复核而不是任务失败', async () => {
    const wrapper = await mountTaskDetail(
      h5Timeline({
        passCount: 1,
        insufficientCount: 1,
        qualityStatus: 'needs_review',
        terminalReason: 'evidence_insufficient',
      }),
    )

    expect(wrapper.text()).toContain('证据不足 1 项')
    expect(wrapper.text()).toContain('需要复核')
    expect(wrapper.text()).not.toContain('任务失败')
  })

  it('正式终态为技术失败时展示技术失败而不是证据复核', async () => {
    const wrapper = await mountTaskDetail(
      h5Timeline({
        passCount: 0,
        failedCount: 1,
        qualityStatus: 'technical_failed',
        terminalReason: 'evidence_failed',
      }),
    )

    expect(wrapper.text()).toContain('探索技术失败')
    expect(wrapper.text()).toContain('证据失败 1 项')
  })

  it('用户取消探索时展示已取消而不是技术失败', async () => {
    const wrapper = await mountTaskDetail(
      h5Timeline({
        passCount: 0,
        failedCount: 1,
        qualityStatus: 'needs_review',
        terminalReason: 'work_cancelled',
      }),
    )

    expect(wrapper.text()).toContain('页面探索已取消')
    expect(wrapper.text()).toContain('已取消')
    expect(wrapper.text()).not.toContain('探索技术失败')
    expect(wrapper.find('.h5-result-needs-review').exists()).toBe(true)
    expect(wrapper.find('.h5-result-technical-failed').exists()).toBe(false)
  })

  it('摘要完成但库存证据不足时失败关闭为需要复核', async () => {
    const wrapper = await mountTaskDetail(
      h5Timeline({ passCount: 1, insufficientCount: 1, qualityStatus: 'complete' }),
    )

    expect(wrapper.text()).toContain('探索结果需要复核')
    expect(wrapper.text()).toContain('证据不足 1 项')
    expect(wrapper.text()).not.toContain('页面探索已完成')
  })

  it('摘要待复核但库存存在失败时优先展示技术失败', async () => {
    const wrapper = await mountTaskDetail(
      h5Timeline({ passCount: 0, failedCount: 1, qualityStatus: 'needs_review' }),
    )

    expect(wrapper.text()).toContain('探索技术失败')
    expect(wrapper.text()).toContain('证据失败 1 项')
  })

  it('正式任务字段在时间线延迟期间立即隐藏仅探索报告入口', async () => {
    let resolveTimeline!: (value: ReturnType<typeof h5Timeline>) => void
    mocks.getTask.mockResolvedValue(task())
    mocks.getPageTimeline.mockReturnValue(
      new Promise((resolve) => {
        resolveTimeline = resolve
      }),
    )

    const wrapper = mount(TaskDetailView)
    await flushPromises()
    expect(wrapper.text()).not.toContain('查看报告')

    resolveTimeline(h5Timeline({ passCount: 2 }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('查看报告')
  })

  it('正式任务字段在时间线加载失败时仍隐藏仅探索报告入口', async () => {
    mocks.getTask.mockResolvedValue(task())
    mocks.getPageTimeline.mockRejectedValue(new Error('timeline unavailable'))

    const wrapper = mount(TaskDetailView)
    await flushPromises()

    expect(wrapper.text()).not.toContain('查看报告')
  })

  it('完整质检且报告已生成时正常显示报告入口', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 2 }), {
      target_platform: 'h5',
      execution_mode: 'full_inspection',
      report_status: 'generated',
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告')
  })

  it('H5 完整质检展示当前 epoch 的通知四阶段与异常事实', async () => {
    const fullTimeline = h5Timeline({ passCount: 1 })
    fullTimeline.execution_mode = 'full_inspection'
    fullTimeline.full_inspection_closure = {
      inspection_count: 1,
      nonterminal_inspection_count: 0,
      failed_inspection_count: 0,
      gate_snapshot_id: 'gate-1',
      gate_passed: true,
      gate_block_reasons: [],
      publication_projection: {
        current_stage: 'inspection_completed',
        publication_count: 1,
        status_counts: { published: 1 },
        stage_counts: {
          publication_persisted: 0,
          wakeup_delivered: 0,
          work_claimed: 0,
          inspection_completed: 1,
        },
        publication_persisted_count: 1,
        wakeup_delivered_count: 1,
        work_claimed_count: 1,
        inspection_completed_count: 1,
        outbox_pending_count: 0,
        outbox_dead_letter_count: 0,
        inbox_pending_count: 0,
        inbox_dead_letter_count: 0,
        failed_count: 0,
        items: [
          {
            publication_id: 'publication-1',
            mode: 'enforced',
            status: 'published',
            current_stage: 'inspection_completed',
            page_capture_id: 'capture-1',
            page_type: 'home',
            row_version: 2,
            attempt_count: 1,
            max_attempts: 3,
            candidate_rule_count: 2,
            expected_batch_count: 1,
            published_work_item_count: 1,
            excluded_active_session_rule_count: 0,
            outbox_count: 1,
            outbox_published_count: 1,
            outbox_pending_count: 0,
            outbox_dead_letter_count: 0,
            inbox_count: 1,
            inbox_completed_count: 1,
            inbox_pending_count: 0,
            inbox_dead_letter_count: 0,
            linked_work_count: 1,
            claimed_work_count: 1,
            terminal_work_count: 1,
            failed_work_count: 0,
            inspection_attempt_count: 1,
            failure_code: null,
            failure_message: null,
            next_attempt_at: '2026-08-17T00:00:00Z',
            created_at: '2026-08-17T00:00:00Z',
            updated_at: '2026-08-17T00:00:01Z',
            finished_at: '2026-08-17T00:00:01Z',
          },
        ],
      },
    }
    mocks.route.path = '/admin/tasks/task-h5'
    const wrapper = await mountTaskDetail(fullTimeline, {
      target_platform: 'h5',
      execution_mode: 'full_inspection',
    })

    // 通知闭环属于研发排查区：默认收起，展开才渲染
    expect(wrapper.text()).not.toContain('H5 证据通知闭环')
    await wrapper.get('[data-testid="diagnostics-toggle"]').trigger('click')
    expect(wrapper.text()).toContain('H5 证据通知闭环')
    expect(wrapper.text()).toContain('Publication 已持久化 1')
    expect(wrapper.text()).toContain('唤醒已送达 1')
    expect(wrapper.text()).toContain('Work 已领取 1')
    expect(wrapper.text()).toContain('质检已完成 1')
    expect(wrapper.text()).toContain('publication=publication-1')
  })

  it('部门平台看不到研发排查区，管理后台默认收起', async () => {
    const department = await mountTaskDetail(h5Timeline({ passCount: 1 }))
    expect(department.find('[data-testid="diagnostics-panel"]').exists()).toBe(false)

    mocks.route.path = '/admin/tasks/task-h5'
    const admin = await mountTaskDetail(h5Timeline({ passCount: 1 }))
    expect(admin.find('[data-testid="diagnostics-panel"]').exists()).toBe(true)
    expect(admin.text()).not.toContain('执行日志')
    await admin.get('[data-testid="diagnostics-toggle"]').trigger('click')
    expect(admin.text()).toContain('执行日志')
    expect(admin.text()).toContain('任务编号')
  })

  it('页面详情弹窗用大白话讲清经过、没通过和要人工确认的规则', async () => {
    const timeline: any = h5Timeline({ passCount: 1 })
    timeline.execution_mode = 'full_inspection'
    timeline.nodes = [
      {
        node_seq: 1,
        source: 'discovered',
        step_name: null,
        page_type: 'home',
        page_type_name: '首页',
        page_state: 'popup',
        status: 'recognized',
        status_label: '已识别',
        is_current: false,
        page_capture_id: 'capture-home',
        url: 'https://heiliao.com/',
        screenshot_artifact_id: null,
        detection: { status: 'recognized', confidence: 0.92, duration_ms: 20 },
        inspection: { status: 'completed' },
        element_count: 10,
        rule_progress: { executed: 4, total: 4 },
        result_rollup: 'failed',
        result_rollup_label: '未通过',
        result_counts: {},
        attention_items: [],
        current_rule_code: null,
        first_visited_at: '2026-09-22T09:35:57Z',
        captured_at: '2026-09-22T09:35:57Z',
        finished_at: null,
      },
    ]
    timeline.exploration_actions = [
      {
        exploration_action_id: 'action-open',
        source_page_capture_id: 'capture-blank',
        source_page_type: null,
        action: 'navigate',
        origin: 'discovered',
        target: 'H5 任务锁定入口',
        target_keywords: [],
        risk_level: 'low',
        purpose: '打开任务锁定的 H5 目标 URL',
        expected_next_page_type: null,
        navigation_depth: 0,
        status: 'explored',
        result_page_capture_id: 'capture-home',
        failure_reason: null,
        failure_code: null,
        failure_message: null,
        failure_retryable: null,
        attempt_count: 1,
        created_at: '2026-09-22T09:35:50Z',
        updated_at: '2026-09-22T09:35:57Z',
      },
      {
        exploration_action_id: 'action-close',
        source_page_capture_id: 'capture-home',
        source_page_type: 'home',
        action: 'click',
        origin: 'discovered',
        target: '关闭当前弹窗',
        target_keywords: [],
        risk_level: 'low',
        purpose: '关闭当前遮挡弹窗并在同一浏览器会话重新采集',
        expected_next_page_type: 'home',
        navigation_depth: 0,
        status: 'blocked',
        result_page_capture_id: null,
        failure_reason: null,
        failure_code: 'POPUP_DISMISSAL_NOT_FOUND',
        failure_message: '当前 UI Snapshot 未证明可关闭的唯一弹窗',
        failure_retryable: false,
        attempt_count: 1,
        created_at: '2026-09-22T09:36:00Z',
        updated_at: '2026-09-22T09:36:19Z',
      },
    ]
    const rule = (overrides: Record<string, unknown>) => ({
      item_type: 'execution',
      execution_id: `exec-${overrides.rule_code}`,
      rule_id: `rule-${overrides.rule_code}`,
      rule_name: `规则 ${overrides.rule_code}`,
      category: null,
      rule_package: '通用',
      trigger_display: '首页',
      bound_page_type: 'home',
      bound_page_type_name: '首页',
      applicable_page_types: ['home'],
      judge_strategy: 'hybrid',
      confidence: 0.9,
      severity: 'high',
      severity_display: 'P1',
      review_status: null,
      human_verdict: null,
      human_verdict_label: null,
      human_verdict_note: null,
      human_verdict_at: null,
      confirm_status: null,
      evidence_artifact_ids: [],
      is_bootstrap: false,
      finished_at: null,
      page_capture_id: 'capture-home',
      confirmable: false,
      ...overrides,
    })
    const notExecuted = {
      display_status: 'not_executed',
      display_status_label: '未执行',
      machine_display_status: 'not_executed',
      machine_display_status_label: '未执行',
      execution_status: 'skipped',
      result_summary: '规则已匹配但任务结束前未执行判定',
      problem_desc: '规则匹配到了这个页面，但任务结束前没轮到它判定',
      confirmable: true,
      confirm_status: 'pending',
    }
    mocks.getRuleResults.mockResolvedValue({
      items: [
        rule({
          rule_code: 'alioth_20',
          rule_name: '弹窗广告关闭行为检查',
          display_status: 'failed',
          display_status_label: '未通过',
          machine_display_status: 'failed',
          machine_display_status_label: '未通过',
          execution_status: 'fail',
          result_summary: '操作前后截图未发生变化',
          problem_desc: '前后可信截图内容完全一致',
        }),
        rule({ rule_code: 'alioth_ops_8', rule_name: '封面加载完整性检查', ...notExecuted }),
        rule({ rule_code: 'alioth_ops_1', rule_name: '产品域名可访问检查', ...notExecuted }),
        rule({
          rule_code: 'alioth_4',
          rule_name: '文案错别字检查',
          display_status: 'passed',
          display_status_label: '通过',
          machine_display_status: 'passed',
          machine_display_status_label: '通过',
          execution_status: 'pass',
          result_summary: null,
          problem_desc: null,
        }),
        ...['alioth_spec_20', 'alioth_spec_21'].map((code, index) =>
          rule({
            rule_code: code,
            rule_name: index ? '多浏览器兼容检查' : '双端显示一致性检查',
            display_status: 'not_applicable',
            display_status_label: '确认不适用',
            machine_display_status: 'not_applicable',
            machine_display_status_label: '确认不适用',
            execution_status: 'skipped',
            result_summary: '当前页面或检查流程未出现该规则的适用目标，已跳过',
            problem_desc: '平台只用一种浏览器采集，不做多浏览器采样，这条规则不适用',
          }),
        ),
        rule({
          rule_code: 'alioth_24',
          rule_name: '启动广告虚假跳过按钮检查',
          display_status: 'not_applicable',
          display_status_label: '确认不适用',
          machine_display_status: 'not_applicable',
          machine_display_status_label: '确认不适用',
          execution_status: 'skipped',
          result_summary: '当前页面或检查流程未出现该规则的适用目标，已跳过',
          problem_desc: '当前检查流程直接进入首页，未出现启动广告页',
        }),
        rule({
          rule_code: 'alioth_ops_7',
          rule_name: '官方群入口跳转检查',
          page_capture_id: null,
          bound_page_type: null,
          bound_page_type_name: null,
          display_status: 'evidence_insufficient',
          display_status_label: '证据不足',
          machine_display_status: 'evidence_insufficient',
          machine_display_status_label: '证据不足',
          execution_status: 'evidence_insufficient',
          result_summary: '聚合规则证据不足，无法形成自动结论',
          problem_desc: '首页和个人中心都没有采到，不能断定产品没有官方群入口',
          confirmable: true,
          confirm_status: 'pending',
        }),
      ],
    })
    const wrapper = await mountTaskDetail(timeline, {
      target_platform: 'h5',
      execution_mode: 'full_inspection',
    })

    // 页面上不再有独立的统计块和规则结果表，规则都进了时间线
    expect(wrapper.text()).not.toContain('命中规则统计')
    expect(wrapper.find('.result-panel').exists()).toBe(false)
    expect(wrapper.get('[data-testid="timeline-summary"]').text()).toContain(
      '规则 8 条：通过 1 条、没通过 1 条、未完成判定 3 条、不适用 3 条',
    )
    expect(wrapper.find('[data-testid="page-detail-dialog"]').exists()).toBe(false)

    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    const story = wrapper.get('[data-testid="page-story"]').text()
    expect(story).toContain('打开任务入口 https://heiliao.com/')
    expect(story).toContain('系统认出这是「首页」，把握 92%')
    expect(story).toContain('页面上盖着广告/遮挡弹窗')
    expect(story).toContain('系统想把它关掉，但没找到能确认的关闭按钮')
    expect(story).toContain('这一页检查了 7 条规则：通过 1 条、没通过 1 条、未完成判定 2 条、不适用 3 条')
    const failed = wrapper.get('[data-testid="page-failed-rules"]').text()
    expect(failed).toContain('弹窗广告关闭行为检查')
    expect(failed).toContain('前后可信截图内容完全一致')
    expect(failed).toContain('机器看到的：操作前后截图未发生变化')
    const review = wrapper.get('[data-testid="page-review-rules"]').text()
    expect(review).toContain('这 2 条是同一个原因：未执行：规则匹配到了这个页面，但任务结束前没轮到它判定')
    expect(review).toContain('封面加载完整性检查')
    expect(review).not.toContain('官方群入口跳转检查')
    // 不适用的也写明为什么：同一个原因合成一组
    const notApplicable = wrapper.get('[data-testid="page-na-rules"]').text()
    expect(notApplicable).toContain('不适用的规则（3 条）')
    expect(notApplicable).toContain(
      '这 2 条不适用的原因相同：平台只用一种浏览器采集，不做多浏览器采样，这条规则不适用',
    )
    expect(notApplicable).toContain('双端显示一致性检查、多浏览器兼容检查')
    expect(notApplicable).toContain('不适用原因：当前检查流程直接进入首页，未出现启动广告页')
    expect(notApplicable).toContain('启动广告虚假跳过按钮检查')

    // 对不上页面的规则归到「整体检查」
    await wrapper.get('[data-testid="task-level-detail"]').trigger('click')
    const taskLevel = wrapper.get('[data-testid="page-review-rules"]').text()
    expect(taskLevel).toContain('官方群入口跳转检查')
    expect(taskLevel).toContain('证据不足：首页和个人中心都没有采到')
  })

  it('全清单候选单独计数，73 条规则不会显示成 396 次执行', async () => {
    const rule = (overrides: Record<string, unknown>) => ({
      rule_code: 'inventory-rule', rule_name: '冻结清单规则', page_capture_id: null,
      bound_page_type: null, applicable_page_types: ['home'], evidence_artifact_ids: [],
      display_status: 'passed', machine_display_status: 'passed', execution_status: 'pass',
      ...overrides,
    })
    const realItems = Array.from({ length: 343 }, (_, index) => rule({
      execution_id: `execution-${index}`, rule_id: `rule-${index % 20}`,
      item_type: 'execution', inventory_source: 'frozen',
    }))
    const candidates = Array.from({ length: 53 }, (_, index) => rule({
      execution_id: null, rule_id: `rule-${index + 20}`, item_type: 'candidate',
      inventory_source: 'frozen', display_status: 'not_executed', confirmable: false,
    }))
    mocks.getRuleResults.mockResolvedValue({ items: [...realItems, ...candidates] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'app', execution_mode: 'full_inspection',
    })
    expect(wrapper.get('[data-testid="timeline-summary"]').text()).toContain(
      '清单 73 条规则，实际使用 20 条，实际执行 343 次',
    )
    expect(wrapper.get('[data-testid="timeline-summary"]').text()).not.toContain('396')
  })

  it('启动失败保留 76 条处置记录，实际使用与执行次数都为零', async () => {
    const placeholders = Array.from({ length: 73 }, (_, index) => ({
      rule_id: `rule-${index}`, rule_code: `rule-${index}`, rule_name: '未启动规则',
      execution_id: `startup-audit-${index}`, item_type: 'execution', inventory_source: 'frozen',
      execution_status: 'skipped', execution_reason: 'exploration_not_started',
      display_status: 'not_executed', machine_display_status: 'not_executed',
      page_capture_id: null, evidence_artifact_ids: [], applicable_page_types: ['home'],
    }))
    const preflights = Array.from({ length: 3 }, (_, index) => ({
      ...placeholders[index], execution_id: `preflight-${index}`,
      execution_status: 'evidence_insufficient', execution_reason: 'AGGREGATE_APPLICABILITY_UNPROVEN',
      display_status: 'evidence_insufficient', machine_display_status: 'evidence_insufficient',
    }))
    mocks.getRuleResults.mockResolvedValue({ items: [...placeholders, ...preflights] })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'app', execution_mode: 'full_inspection',
    })
    expect(wrapper.get('[data-testid="timeline-summary"]').text()).toContain(
      '清单 73 条规则，实际使用 0 条，实际执行 0 次',
    )
  })

  it('App 仅页面探索隐藏正式报告与规则结果，规则测试入口只给管理后台的体验中心管理员', async () => {
    const appExploration = {
      target_platform: 'app',
      execution_mode: 'exploration_only',
      report_status: 'generated',
      available_actions: ['detail', 'report'],
    }
    const department = await mountTaskDetail(h5Timeline({ passCount: 2 }), appExploration)
    expect(department.text()).not.toContain('独立规则测试')
    expect(department.text()).toContain('不执行正式规则')
    department.unmount()

    mocks.route.path = '/admin/tasks/task-h5'
    authStore.session.value = {
      token: 'token',
      expires_at: null,
      user: { id: 'u1', username: 'admin', full_name: '管理员', role: '平台管理员', status: 'enabled', permissions: ['qcRules', 'qcRules:write'], tenant_id: 't1', tenant_name: '体验中心' },
    }
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 2 }), appExploration)

    expect(wrapper.text()).not.toContain('查看报告')
    expect(wrapper.text()).toContain('独立规则测试')
    expect(wrapper.text()).not.toContain('命中规则统计')
    expect(wrapper.text()).not.toContain('质检工作项')
    expect(wrapper.text()).not.toContain('规则执行结果')
    expect(wrapper.text()).not.toContain('页面探索已完成')
  })

  it('App 完整质检在残留 H5 时间线下仍显示已生成报告', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 2 }), {
      target_platform: 'app',
      execution_mode: 'full_inspection',
      report_status: 'generated',
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告')
    expect(wrapper.text()).not.toContain('页面探索已完成')
  })

  it('Web 任务详情展示服务端冻结的桌面浏览器画像', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'exploration_only',
      browser_profile_id: 'desktop_fhd_1920x1080_v1',
    })

    expect(wrapper.text()).toContain('桌面浏览器画像')
    expect(wrapper.text()).toContain('desktop_fhd_1920x1080_v1')
  })

  it('Web 正式任务按权威闭环投影展示三阶段和证据缺口终态', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      task_name: 'Web 正式闭环',
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      status: 'done',
      execution_status: 'completed_with_gaps',
      finalization_state: 'sealed',
      coverage_status: 'completed_with_gaps',
      coverage_observed_page_types: ['content_list', 'video_list'],
      coverage_missing_page_types: ['video_detail'],
      coverage_unrecognized_page_types: ['web_unknown'],
      coverage_normalization_protocol_version: 'web-page-type-assessment.v2',
      stage_durations: {
        exploration_ms: 125_000,
        inspection_ms: 90_000,
        report_generation_ms: 30_000,
        total_ms: 300_000,
      },
      completion_gate_passed: true,
      report_available: true,
      report_status: 'not_started',
      available_actions: ['detail'],
    })

    expect(wrapper.text()).toContain('已完成，有证据缺口')
    expect(wrapper.text()).toContain('探索')
    expect(wrapper.text()).toContain('规则质检')
    expect(wrapper.text()).toContain('报告生成')
    expect(wrapper.text()).toContain('探索耗时2分5秒')
    expect(wrapper.text()).toContain('规则质检耗时1分30秒')
    expect(wrapper.text()).toContain('报告生成耗时30秒')
    expect(wrapper.text()).toContain('提交至报告总耗时5分')
    expect(wrapper.text()).toContain('缺失页型 视频详情页')
    expect(wrapper.text()).toContain('未识别页型 web_unknown')
    expect(wrapper.text()).toContain('查看报告')
  })

  it('Web 正式任务在 Gate 阻断或当前报告未绑定时展示不可导出的预览报告入口', async () => {
    const blocked = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      status: 'done',
      execution_status: 'completed',
      finalization_state: 'finalizing',
      completion_gate_passed: false,
      report_available: false,
      report_availability: 'preview',
      report_status: 'generated',
      available_actions: ['detail', 'report'],
    })

    expect(blocked.text()).toContain('查看报告（预览）')
    await blocked.findAll('button').find(button => button.text().includes('查看报告'))!.trigger('click')
    expect(mocks.routerPush).toHaveBeenLastCalledWith({ path: '/reports', query: { task: 'task-h5' } })

    const unbound = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      status: 'done',
      execution_status: 'completed',
      finalization_state: 'sealed',
      completion_gate_passed: true,
      report_available: false,
      report_availability: 'preview',
      report_status: 'generated',
      available_actions: ['detail', 'report'],
    })

    expect(unbound.text()).toContain('查看报告（预览）')
    await unbound.findAll('button').find(button => button.text().includes('查看报告'))!.trigger('click')
    expect(mocks.routerPush).toHaveBeenLastCalledWith({ path: '/reports', query: { task: 'task-h5' } })
  })

  it('H5 V2 复核任务展示报告入口但不套用 Web 预览文案', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'h5',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      status: 'review',
      report_available: false,
      report_availability: 'preview',
      report_status: 'ready',
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告（复核）')
    expect(wrapper.text()).not.toContain('查看报告（预览）')
  })

  it('App 预览报告继续沿用原预览文案', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'app',
      execution_mode: 'full_inspection',
      status: 'review',
      report_available: true,
      report_availability: 'preview',
      report_status: 'ready',
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告（预览）')
    expect(wrapper.text()).not.toContain('查看报告（复核）')
  })

  it('隔离的 Web 正式报告收口显示管理员显式恢复动作', async () => {
    mocks.performTaskOperation.mockResolvedValue({
      task_id: 'task-h5',
      status: 'running',
      run_id: 'run-1',
      message: '正式报告收口重试已恢复',
    })
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      status: 'review',
      report_availability: 'preview',
      available_actions: ['detail', 'report', 'retry-finalization'],
    })

    const retry = wrapper.findAll('button').find((button) => button.text().includes('恢复报告收口'))
    expect(retry).toBeDefined()
    wrapper.getComponent({ name: 'TaskCancelConfirm' }).vm.$emit('positive-click')
    await flushPromises()

    expect(mocks.performTaskOperation).toHaveBeenCalledWith('task-h5', 'retry-finalization')
  })

  it('Web 时间线将首页角色和四类业务页型同时展示', async () => {
    const timeline = h5Timeline({ passCount: 1 })
    timeline.nodes = [
      {
        node_seq: 1,
        source: 'discovered',
        step_name: null,
        page_role: 'home',
        page_type: 'video_list',
        page_type_name: '视频列表页',
        page_state: 'stable',
        status: 'completed',
        status_label: '已完成',
        is_current: false,
        page_capture_id: 'capture-home',
        url: 'https://example.test/landing',
        screenshot_artifact_id: null,
        detection: { status: 'recognized', confidence: 0.95 },
        inspection: { lifecycle_status: 'active', status: 'completed' },
        parent_page_capture_id: null,
        element_count: 20,
        rule_progress: { executed: 1, total: 1 },
        result_rollup: 'passed',
        result_rollup_label: '通过',
        result_counts: { passed: 1 },
        attention_items: [],
        current_rule_code: null,
        first_visited_at: '2026-09-05T00:00:00Z',
      },
    ]

    const wrapper = await mountTaskDetail(timeline, {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      report_availability: 'preview',
    })

    expect(wrapper.text()).toContain('首页 · 视频列表页')
  })

  it('Web-v1 正式任务保留旧报告入口且不显示 V2 三阶段', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v1',
      status: 'done',
      report_status: 'generated',
      report_available: false,
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告')
    expect(wrapper.find('[data-testid="web-formal-closure"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="web-stage-durations"]').exists()).toBe(false)
  })

  it('Web-v1 正式任务主状态保留旧任务状态而不读取 V2 execution_status', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v1',
      status: 'done',
      execution_status: 'failed',
      report_status: 'generated',
      available_actions: ['detail', 'report'],
    })

    const status = wrapper.get('.overview-header .status-pill').text()
    expect(status).toContain('已完成')
    expect(status).not.toContain('执行失败')
  })

  it('缺失 workflow_version 的 Web 正式任务按旧详情合同展示', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      status: 'done',
      report_status: 'generated',
      report_available: false,
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告')
    expect(wrapper.find('[data-testid="web-formal-closure"]').exists()).toBe(false)
  })

  it('Web-V2 workflow_version 大小写不敏感并启用权威报告门禁', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'V2',
      status: 'done',
      report_status: 'generated',
      report_available: false,
      report_availability: 'preview',
      available_actions: ['detail', 'report'],
    })

    expect(wrapper.text()).toContain('查看报告（预览）')
    expect(wrapper.find('[data-testid="web-formal-closure"]').exists()).toBe(true)
  })

  it('Web 正式任务没有任何持久化报告事实时显示禁用入口和原因', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'full_inspection',
      workflow_version: 'v2',
      status: 'queued',
      report_available: false,
      report_availability: 'none',
      available_actions: ['detail'],
    })

    expect(wrapper.text()).toContain('查看报告')
    expect(wrapper.text()).toContain('尚未产生可信采集')
    expect(wrapper.get('[data-testid="report-none-action"]').attributes('disabled')).toBeDefined()
  })

  it('Web 排队详情展示专属队列位置、Worker 与启动条件', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'exploration_only',
      workflow_version: 'v2',
      status: 'queued',
      execution_phase: 'queued',
      queue_position: 3,
      tasks_ahead: 2,
      available_worker_count: 1,
      start_condition: '等待前方 Web 任务完成',
    })

    const state = wrapper.get('[data-testid="web-execution-queue-state"]').text()
    expect(state).toContain('已进入 Web 专属队列')
    expect(state).toContain('队列位置 3 · 前方任务 2 · 在线适配 Worker 1')
    expect(state).toContain('启动条件：等待前方 Web 任务完成')
  })

  it.each([
    ['configuration_error', '运行配置错误'],
    ['navigation_failed', '页面导航失败'],
    ['evidence_failed', '证据留存失败'],
    ['classification_review', '页面类型待复核'],
    ['done', '探索已完成'],
    ['canceled', '探索已取消'],
  ])('Web 执行阶段 %s 使用后端权威枚举显示', async (executionPhase, expected) => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'web',
      execution_mode: 'exploration_only',
      workflow_version: 'v2',
      status: executionPhase === 'done' ? 'done' : 'failed',
      execution_phase: executionPhase,
    })

    expect(wrapper.get('[data-testid="web-execution-queue-state"]').text()).toContain(expected)
  })

  it.each([
    ['complete', 'completed', '探索 已完成', '规则质检 已完成'],
    [
      'completed_with_gaps',
      'completed_with_gaps',
      '探索 已完成，有证据缺口',
      '规则质检 已完成，有证据缺口',
    ],
    ['failed', 'failed', '探索 失败', '规则质检 失败'],
    ['complete', 'needs_review', '探索 已完成', '规则质检 待复核'],
  ])(
    'Web 正式三阶段显式映射 coverage=%s execution=%s',
    async (coverageStatus, executionStatus, explorationText, inspectionText) => {
      const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
        target_platform: 'web',
        execution_mode: 'full_inspection',
        workflow_version: 'v2',
        status: executionStatus === 'failed' ? 'failed' : 'done',
        coverage_status: coverageStatus,
        execution_status: executionStatus,
        completion_gate_passed: false,
        report_available: false,
        available_actions: ['detail'],
      })

      const closure = wrapper.get('[data-testid="web-formal-closure"]').text()
      expect(closure).toContain(explorationText)
      expect(closure).toContain(inspectionText)
    },
  )
})

function pageNode(overrides: Record<string, unknown>) {
  return {
    source: 'h5_v2_capture',
    step_name: null,
    page_state: 'stable',
    status: 'captured',
    status_label: '已采集',
    is_current: false,
    screenshot_artifact_id: null,
    detection: { status: 'recognized', confidence: 0.95, duration_ms: null },
    inspection: { status: null },
    element_count: null,
    rule_progress: { executed: 0, total: null },
    result_rollup: null,
    result_rollup_label: null,
    result_counts: {},
    attention_items: [],
    current_rule_code: null,
    first_visited_at: '2026-09-23T09:15:38Z',
    captured_at: '2026-09-23T09:15:38Z',
    finished_at: null,
    ...overrides,
  }
}

describe('明细区块各自加载，加载中不说「暂无」', () => {
  it('截图还没返回时显示「截图加载中…」，已经回来的时间线先显示', async () => {
    const timeline = h5Timeline({ passCount: 1 })
    timeline.nodes = [pageNode({ node_seq: 1, page_type: 'home', page_type_name: '首页', page_capture_id: 'capture-home', url: 'https://example.test/' })]
    mocks.getTaskScreenshots.mockReturnValue(new Promise(() => undefined))
    const wrapper = await mountTaskDetail(timeline, { execution_mode: 'full_inspection', status: 'review' })

    expect(wrapper.get('[data-testid="screenshots-loading"]').text()).toContain('截图加载中…')
    expect(wrapper.text()).not.toContain('暂无截图证据')
    expect(wrapper.findAll('.timeline-node')).toHaveLength(1)
    wrapper.unmount()
  })

  it('时间线还没返回时显示「页面加载中…」，不说「暂无页面节点」', async () => {
    mocks.getTask.mockResolvedValue(task({ execution_mode: 'full_inspection', status: 'review' }))
    mocks.getPageTimeline.mockReturnValue(new Promise(() => undefined))
    const wrapper = mount(TaskDetailView)
    await flushPromises()

    expect(wrapper.find('[data-testid="timeline-loading"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="timeline-summary"]').text()).toBe('页面加载中…')
    expect(wrapper.text()).not.toContain('暂无页面节点')
    wrapper.unmount()
  })
})

describe('截图合集入口与规则证据范围', () => {
  function screenshot(artifactId: string, overrides: Partial<ScreenshotItem> = {}): ScreenshotItem {
    return {
      artifact_id: artifactId,
      page_capture_id: 'capture-home',
      captured_at: '2026-10-08T10:00:02Z',
      content_type: 'image/png',
      size_bytes: 100,
      duplicate_count: 1,
      related_rules: [],
      ...overrides,
    }
  }

  function twoCollections(): ScreenshotGroup {
    return {
      page_group_key: 'same-home-group',
      page_title: '首页',
      page_display_name: '首页',
      page_type: 'home',
      page_type_name: '首页',
      total: 12,
      items: [
        screenshot('collection-a-cover', {
          duplicate_count: 5,
          related_frames: [
            {
              artifact_id: 'collection-a-earlier',
              page_capture_id: 'capture-a-earlier',
              captured_at: '2026-10-08T10:00:01Z',
              duplicate_count: 4,
            },
            {
              artifact_id: 'collection-a-unknown-time',
              page_capture_id: 'capture-a-unknown-time',
              captured_at: null,
              duplicate_count: 1,
            },
          ],
        }),
        screenshot('collection-b-cover', {
          captured_at: '2026-10-08T11:00:00Z',
          related_frames: [{
            artifact_id: 'collection-b-later',
            page_capture_id: 'capture-b-later',
            captured_at: '2026-10-08T11:00:01Z',
            duplicate_count: 1,
          }],
        }),
      ],
    }
  }

  async function mountWithScreenshots(group: ScreenshotGroup) {
    mocks.getTaskScreenshots.mockResolvedValue({ groups: [group] })
    return mountTaskDetail(h5Timeline({ passCount: 0 }), {
      target_platform: 'app',
      execution_mode: 'full_inspection',
      status: 'review',
    })
  }

  it('动作明细晚于截图返回时自动补齐已打开合集，仍限制在原合集内', async () => {
    let finishGraph!: (value: unknown) => void
    mocks.getPageGraph.mockImplementation(() => new Promise((resolve) => { finishGraph = resolve }))
    const wrapper = await mountWithScreenshots(twoCollections())
    await wrapper.findAll('[data-testid="screenshot-collection-card"]')[0]!.trigger('click')
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    expect(viewer.props('entries')[0].actions).toEqual([])
    finishGraph({ status: 'available', states: [], transitions: [{
      transition_id: 'late-transition', action_execution_id: 'late-action',
      source_state_id: 'capture-a-earlier', target_state_id: 'capture-home',
      action_kind: 'click', semantic_target: '关闭广告', status: 'completed',
    }] })
    await flushPromises()
    const entries = viewer.props('entries') as Array<{ artifact_id: string; actions: Array<{ id: string }> }>
    expect(entries[0]!.actions[0]!.id).toBe('late-action')
    expect(entries.map((item) => item.artifact_id)).toEqual([
      'collection-a-earlier', 'collection-a-cover', 'collection-a-unknown-time',
    ])
    expect(viewer.props('initialArtifactId')).toBe('collection-a-cover')
  })

  it('同页型两组合集各自打开，切换后重置封面且缺时间的图仍保留', async () => {
    const wrapper = await mountWithScreenshots(twoCollections())
    const cards = wrapper.findAll('[data-testid="screenshot-collection-card"]')
    expect(cards).toHaveLength(2)
    expect(cards[0]!.text()).toMatch(/合集\s*·\s*3\s*张/)
    expect(cards[1]!.text()).toMatch(/合集\s*·\s*2\s*张/)
    expect(wrapper.get('.screenshot-group > header').text()).toMatch(/2\s*个合集/)

    await cards[0]!.trigger('click')
    const firstViewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    const firstEntries = firstViewer.props('entries') as Array<{ artifact_id: string }>
    expect(firstEntries.map((entry) => entry.artifact_id)).toEqual([
      'collection-a-earlier', 'collection-a-cover', 'collection-a-unknown-time',
    ])
    expect(firstViewer.props('collection')).toBe(true)
    expect(firstViewer.props('initialArtifactId')).toBe('collection-a-cover')

    firstViewer.vm.$emit('close')
    await flushPromises()
    await cards[1]!.trigger('click')
    const secondViewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    expect((secondViewer.props('entries') as Array<{ artifact_id: string }>).map(
      (entry) => entry.artifact_id,
    )).toEqual(['collection-b-cover', 'collection-b-later'])
    expect(secondViewer.props('initialArtifactId')).toBe('collection-b-cover')
    expect(secondViewer.props('collection')).toBe(true)
  })

  it('同图重复采集只说明采集次数，不伪造成多张图片的合集', async () => {
    const group = twoCollections()
    group.items = [screenshot('repeated-cover', { duplicate_count: 7, related_frames: [] })]
    const wrapper = await mountWithScreenshots(group)
    const card = wrapper.get('[data-testid="screenshot-collection-card"]')

    expect(card.text()).toMatch(/同图采集\s*7\s*次/)
    expect(card.text()).not.toContain('合集')
    await card.trigger('click')
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    expect((viewer.props('entries') as Array<{ artifact_id: string }>).map(
      (entry) => entry.artifact_id,
    )).toEqual(['repeated-cover'])
    expect(viewer.props('collection')).toBe(false)
  })

  it('旧截图数据没有 related_frames 时仍按单张打开', async () => {
    const group = twoCollections()
    group.items = [screenshot('legacy-single')]
    const wrapper = await mountWithScreenshots(group)
    const card = wrapper.get('[data-testid="screenshot-collection-card"]')

    expect(card.text()).not.toContain('合集')
    expect(wrapper.get('.screenshot-group > header').text()).toMatch(/1\s*张单图/)
    await card.trigger('click')
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    expect((viewer.props('entries') as Array<{ artifact_id: string }>).map(
      (entry) => entry.artifact_id,
    )).toEqual(['legacy-single'])
    expect(viewer.props('initialArtifactId')).toBe('legacy-single')
    expect(viewer.props('collection')).toBe(false)
  })

  it('规则的看截图入口只打开该规则引用的图片，不带入所属合集其它图', async () => {
    const timeline = h5Timeline({ passCount: 0 })
    timeline.nodes = [pageNode({
      node_seq: 1, page_type: 'home', page_type_name: '首页',
      page_capture_id: 'capture-home', url: 'app://example',
    })]
    mocks.getTaskScreenshots.mockResolvedValue({ groups: [twoCollections()] })
    mocks.getRuleResults.mockResolvedValue({ items: [{
      item_type: 'execution',
      execution_id: 'exec-screenshot-rule',
      rule_id: 'rule-screenshot',
      rule_code: 'alioth_19',
      rule_name: '跳过按钮点击区域检查',
      page_capture_id: 'capture-home',
      bound_page_type: 'home',
      bound_page_type_name: '首页',
      applicable_page_types: ['home'],
      execution_status: 'fail',
      display_status: 'failed',
      display_status_label: '未通过',
      machine_display_status: 'failed',
      machine_display_status_label: '未通过',
      result_summary: '点击区域过小',
      problem_desc: '按钮点击区域未达到要求',
      evidence_artifact_ids: ['collection-a-earlier', 'collection-b-cover', 'other-artifact'],
      is_bootstrap: false,
      confirmable: false,
    }] })
    const wrapper = await mountTaskDetail(timeline, {
      target_platform: 'app', execution_mode: 'full_inspection', status: 'review',
    })

    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    await wrapper.get('[data-testid="page-failed-rules"] .dialog-link').trigger('click')
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    expect((viewer.props('entries') as Array<{ artifact_id: string }>).map(
      (entry) => entry.artifact_id,
    )).toEqual(['collection-a-earlier', 'collection-b-cover'])
    expect(viewer.props('initialArtifactId')).toBe('collection-a-earlier')
    expect(viewer.props('collection')).toBe(false)
  })
})

describe('地址打开就是 App 下载 / 引导页', () => {
  const blankNode = pageNode({ node_seq: 1, page_type: null, page_type_name: null, page_capture_id: 'capture-blank', url: 'about:blank' })
  const guideNode = pageNode({ node_seq: 2, page_type: 'guide_page', page_type_name: '引导页', page_capture_id: 'capture-guide', url: 'https://haijiaoq.com/' })

  it('探索结束后时间线上只有引导页时，在时间线上方说明原因', async () => {
    const timeline = h5Timeline({ passCount: 0 })
    timeline.nodes = [blankNode, guideNode]
    const wrapper = await mountTaskDetail(timeline, {
      execution_mode: 'full_inspection',
      status: 'review',
      target_url: 'https://haijiaoq.com',
    })

    const hint = wrapper.get('[data-testid="guide-page-hint"]').text()
    expect(hint).toContain('这个地址打开是 App 下载 / 引导页')
    expect(hint).toContain('请换成产品真正的站点地址')
    wrapper.unmount()
  })

  it('除了引导页还走到了别的页面、或任务还在跑时不提示', async () => {
    const reached = h5Timeline({ passCount: 1 })
    reached.nodes = [
      guideNode,
      pageNode({ node_seq: 3, page_type: 'home', page_type_name: '首页', page_capture_id: 'capture-home', url: 'https://example.test/' }),
    ]
    const reachedWrapper = await mountTaskDetail(reached, { execution_mode: 'full_inspection', status: 'review' })
    expect(reachedWrapper.find('[data-testid="guide-page-hint"]').exists()).toBe(false)
    reachedWrapper.unmount()

    const running = h5Timeline({ passCount: 0 })
    running.nodes = [guideNode]
    const runningWrapper = await mountTaskDetail(running, { execution_mode: 'full_inspection', status: 'running' })
    expect(runningWrapper.find('[data-testid="guide-page-hint"]').exists()).toBe(false)
    runningWrapper.unmount()
  })

  it('排队太久但读不到服务状态时，不请求也不下「Worker 存活」的结论', async () => {
    const wrapper = await mountTaskDetail(h5Timeline({ passCount: 0 }), {
      execution_mode: 'full_inspection',
      status: 'queued',
      created_at: '2026-08-13T00:00:00Z',
    })

    expect(mocks.getSystemStatus).not.toHaveBeenCalled()
    const alert = wrapper.get('.scheduler-alert').text()
    expect(alert).toContain('仍未开始')
    expect(alert).toContain('真实队列位置和启动条件')
    expect(alert).toContain('可取消任务')
    expect(alert).not.toContain('Worker 存活')
    wrapper.unmount()
  })
})
