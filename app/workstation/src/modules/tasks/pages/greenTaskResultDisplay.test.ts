import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import type { GreenRuleResultProjection, RuleResultItem } from '@/types/task-insight'

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())

const mocks = vi.hoisted(() => ({
  route: { path: '/tasks/task-a', params: { taskId: 'task-a' }, query: {} },
  push: vi.fn(), getTask: vi.fn(), getPageTimeline: vi.fn(), getRuleResults: vi.fn(),
  getTaskScreenshots: vi.fn(), issueEvidenceFileUrl: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRoute: () => mocks.route, useRouter: () => ({ push: mocks.push }) }))
vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const plain = defineComponent({ setup: (_, { attrs, slots }) => () => h('div', attrs, slots.default?.()) })
  return {
    NButton: defineComponent({ setup: (_, { attrs, slots }) => () => h('button', attrs, slots.default?.()) }),
    NModal: defineComponent({ props: { show: Boolean }, setup: (props, { slots }) => () => props.show ? h('div', slots.default?.()) : null }),
    NIcon: plain, NSpin: plain, NPopconfirm: plain,
    useMessage: () => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
  }
})
vi.mock('@/services/tasks.api', () => ({
  getTask: mocks.getTask, getAgentRun: vi.fn(async () => ({ run: null })), performTaskOperation: vi.fn(),
}))
vi.mock('@/services/system.api', () => ({ getSystemStatus: vi.fn() }))
vi.mock('@/services/task-insight.api', () => ({
  getPageTimeline: mocks.getPageTimeline, getRuleResults: mocks.getRuleResults,
  getTaskScreenshots: mocks.getTaskScreenshots, issueEvidenceFileUrl: mocks.issueEvidenceFileUrl,
  getTaskStats: vi.fn(async () => ({ total: 3, finished: 1, status_counts: {}, recent_anomalies: [] })),
  getTaskLogs: vi.fn(async () => ({ items: [], total: 0 })),
  getAppStage: vi.fn(async () => ({ applicable: false })),
  getInspectionWorkItems: vi.fn(async () => ({ summary: { work_item_count: 0 }, items: [] })),
  getPageGraph: vi.fn(async () => null), getPageRecoveryRoutes: vi.fn(async () => null),
  getEvidenceGaps: vi.fn(async () => null), getRuleReuse: vi.fn(async () => null),
  getPageTypeJudgements: vi.fn(async () => null), getClosureOutcome: vi.fn(async () => null),
  downloadLogsExport: vi.fn(), downloadPlatformProgressExport: vi.fn(), downloadScreenshotsArchive: vi.fn(),
}))

import TaskDetailView from './TaskDetailView.vue'
import PageDetailDialog from '../components/PageDetailDialog.vue'
import GreenRuleResultDetails from '../components/GreenRuleResultDetails.vue'

function task(taskId: string) {
  return {
    task_id: taskId, task_name: `展示任务 ${taskId}`, product: { department_name: '质检部', product_name: '展示夹具' },
    target_url: 'https://example.test/', target_platform: 'web', execution_mode: 'full_inspection',
    journey_steps: [], status: 'review', progress: 100, report_status: 'generated', available_actions: ['detail'],
    started_at: '2026-10-19T00:00:00Z', finished_at: '2026-10-19T00:01:00Z', run_id: `run-${taskId}`,
    ruleset_code: 'green', ruleset_version: 4, required_page_types: [],
  }
}

function timeline(taskId: string) {
  return {
    task_id: taskId, journey_steps: [], exploration_actions: [],
    nodes: [{ node_seq: 1, source: 'discovered', page_capture_id: `${taskId}-capture`, page_type: 'home',
      page_type_name: '首页', page_state: 'stable', status: 'recognized', status_label: '已识别',
      is_current: false, url: 'https://example.test/', captured_at: '2026-10-19T00:00:01Z',
      detection: { status: 'recognized', confidence: 1 }, inspection: { status: 'completed' },
      rule_progress: { executed: 2, total: 2 }, result_rollup: null, result_counts: {}, attention_items: [] }],
  }
}

function projection(ruleKey: '32' | 's35'): GreenRuleResultProjection {
  return {
    schema_version: 'green-rule-result.v1', rule_key: ruleKey, rule_contract_hash: 'a'.repeat(64),
    linkage_status: 'linked', linkage_notice: null, execution_status: ruleKey === '32' ? 'fail' : 'evidence_insufficient',
    review_status: 'pending', review_reason: '仍缺少实际操作材料', missing_evidence: ['native_horizontal_scroll'],
    subitems: [{ key: ruleKey === '32' ? 'equal_width_navigation' : 'correct_response', status: ruleKey === '32' ? 'fail' : 'pass',
      evaluated: true, confidence: 1, reason: '后端已保存的独立分项结论', missing_evidence: [],
      evidence_artifact_ids: ['proof-png', 'proof-ui'], screenshot_artifact_ids: ['proof-png'], capture_ids: [] },
    ...(ruleKey === '32' ? ['fit_navigation_spacing', 'overflow_scroll', 'initial_next_label']
      : ['usable_layout', 'comparable_state_stability']).map((key) => ({ key, status: 'evidence_insufficient',
      evaluated: false, confidence: null, reason: '尚缺少对应的独立材料', missing_evidence: ['native_horizontal_scroll'],
      evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [] }))],
  }
}

type AdditionalRuleKey = Exclude<GreenRuleResultProjection['rule_key'], '32' | 's35'>

function additionalProjection(ruleKey: AdditionalRuleKey): GreenRuleResultProjection {
  const subchecks: Record<AdditionalRuleKey, string[]> = {
    '102': ['minor_safety', 'political_safety', 'terrorism_safety', 'content_scope'],
    '41': ['external_browser_handoff'],
    s72: ['empty_state_meaning', 'load_failure_meaning'],
    '62': ['natural_end_handoff', 'manual_close_handoff', 'content_preload', 'presented_first_frame'],
    s49: ['failure_reason', 'retry_entry', 'business_recovery', 'video_line_recovery', 'account_payment_support', 'payment_not_interrupted'],
    s73: ['loading_feedback', 'empty_state_guidance', 'business_success_feedback'],
  }
  return {
    schema_version: 'green-rule-result.v1', rule_key: ruleKey, rule_contract_hash: 'b'.repeat(64),
    linkage_status: 'linked', linkage_notice: null, execution_status: 'evidence_insufficient',
    review_status: 'pending', review_reason: '必要材料或共享分项判读尚不完整',
    missing_evidence: ['trusted_page_originals'],
    subitems: subchecks[ruleKey].map((key) => ({ key, status: 'evidence_insufficient', evaluated: false,
      confidence: null, reason: '该分项尚缺必要原件或已接入的独立判读，未形成业务结论',
      missing_evidence: ['trusted_page_originals'], evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [] })),
  }
}

function rule(code: string, overrides: Partial<RuleResultItem> = {}): RuleResultItem {
  return {
    item_type: 'execution', execution_id: `execution-${code}`, rule_id: `rule-${code}`, rule_code: code,
    rule_name: code, category: 'layout', rule_package: '冻结规则', trigger_display: '页面检查',
    bound_page_type: 'home', bound_page_type_name: '首页', applicable_page_types: ['home'],
    display_status: 'failed', display_status_label: '未通过', machine_display_status: 'failed', machine_display_status_label: '未通过',
    execution_status: 'fail', judge_strategy: 'hybrid', confidence: 1, severity: 'medium', severity_display: 'P2',
    result_summary: '后端正式结论', problem_desc: '原有规则问题说明', review_status: null,
    human_verdict: null, human_verdict_label: null, human_verdict_note: null, human_verdict_at: null,
    confirmable: false, confirm_status: null, evidence_artifact_ids: ['gallery-png', 'proof-ui'],
    page_capture_id: 'task-a-capture', is_bootstrap: false, finished_at: '2026-10-19T00:01:00Z', ...overrides,
  }
}

function screenshots(taskId: string) {
  return { task_id: taskId, groups: [{ page_group_key: 'home', page_display_name: '首页', page_type_name: '首页',
    items: ['proof-png', 'gallery-png'].map((artifact_id) => ({ artifact_id, page_capture_id: `${taskId}-capture`,
      content_type: 'image/png', duplicate_count: 1, related_rules: [], captured_at: '2026-10-19T00:00:01Z' })) }] }
}

function mountTask() {
  return mount(TaskDetailView, { global: { stubs: {
    PageExplorationPanel: true, WebStageDurationPanel: true, WebReportExplanationPanel: true,
    AppStagePanel: true, PageGraphPanel: true, ClosureOutcomePanel: true, ExplorationProcessPanel: true,
    DeliveryIncidentPanel: true, H5CheckpointPanel: true,
    ScreenshotEvidenceViewer: { name: 'ScreenshotEvidenceViewer', props: ['entries', 'initialArtifactId', 'title'], template: '<div data-testid="viewer" />' },
  } } })
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.route = reactive({ path: '/tasks/task-a', params: { taskId: 'task-a' }, query: {} })
  mocks.getTask.mockImplementation(async (taskId: string) => task(taskId))
  mocks.getPageTimeline.mockImplementation(async (taskId: string) => timeline(taskId))
  mocks.getTaskScreenshots.mockImplementation(async (taskId: string) => screenshots(taskId))
  mocks.getRuleResults.mockResolvedValue({ items: [] })
  mocks.issueEvidenceFileUrl.mockImplementation(async (id: string) => `/fixture/${id}.png`)
})

describe('绿色结果接入任务详情的真实宿主', () => {
  it('六条正式缺证结果按页面和TASK原宿主展示，空原图引用不借图库补图', async () => {
    mocks.getTask.mockImplementation(async (taskId: string) => ({ ...task(taskId), target_platform: 'app', target_url: '' }))
    const keys: AdditionalRuleKey[] = ['102', '41', 's72', '62', 's49', 's73']
    mocks.getRuleResults.mockResolvedValue({ items: [
      ...keys.map((key) => rule(`正式绿色${key}`, {
        green_result: additionalProjection(key), display_status: 'evidence_insufficient',
        machine_display_status: 'evidence_insufficient', execution_status: 'evidence_insufficient',
        confidence: null, review_status: 'pending', page_capture_id: key === '102' || key === '41' ? 'task-a-capture' : null,
      })),
      rule('alioth_41', { rule_name: '旧二维码规则', display_status: 'passed', machine_display_status: 'passed', execution_status: 'pass' }),
    ] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    let dialog = wrapper.getComponent(PageDetailDialog)
    let cards = dialog.findAllComponents(GreenRuleResultDetails)
    expect(cards.map((card) => card.props('result')?.rule_key)).toEqual(['102', '41'])
    expect(cards.map((card) => card.findAll('[data-testid="green-subitem"]').length)).toEqual([4, 1])
    expect(dialog.text()).toContain('内容安全与范围分项判定')
    expect(dialog.text()).toContain('App外链浏览器承接分项判定')
    expect(dialog.text()).toContain('旧二维码规则')
    expect(dialog.text()).toContain('通过的规则（1 条）')
    expect(dialog.text()).not.toContain('置信度')
    expect(dialog.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
    expect(dialog.findAll('button').some((button) => button.text().includes('人工确认'))).toBe(false)
    await wrapper.get('[data-testid="task-level-node"] button').trigger('click')
    dialog = wrapper.getComponent(PageDetailDialog)
    cards = dialog.findAllComponents(GreenRuleResultDetails)
    expect(cards.map((card) => card.props('result')?.rule_key)).toEqual(['s72', '62', 's49', 's73'])
    expect(cards.map((card) => card.findAll('[data-testid="green-subitem"]').length)).toEqual([2, 4, 6, 3])
    expect(cards.flatMap((card) => card.findAll('[data-testid="green-subitem-status"]').map((status) => status.text())))
      .toEqual(Array(15).fill('尚未判定'))
    expect(dialog.text()).not.toContain('置信度')
    expect(dialog.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
    expect(mocks.push).not.toHaveBeenCalled()
  })

  it.each(['web', 'h5'])('非App的%s排除项与未执行候选不从规则编号生成绿色结果', async (platform) => {
    mocks.getTask.mockImplementation(async (taskId: string) => ({ ...task(taskId), target_platform: platform }))
    mocks.getRuleResults.mockResolvedValue({ items: [
      rule('alioth_green_41', { item_type: 'candidate', execution_id: null, page_capture_id: null,
        display_status: 'not_applicable', machine_display_status: 'not_applicable', execution_status: null,
        result_summary: '此规则仅适用于App', problem_desc: null }),
      rule('alioth_green_102', { item_type: 'candidate', execution_id: null, display_status: 'not_executed',
        machine_display_status: 'not_executed', execution_status: null, result_summary: '本次尚未产生正式执行结果', problem_desc: null }),
      rule('alioth_41', { rule_name: '原有二维码失败', result_summary: '旧合同结论仍保留' }),
    ] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    let dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.findComponent(GreenRuleResultDetails).exists()).toBe(false)
    expect(dialog.get('[data-testid="page-failed-rules"]').text()).toContain('原有二维码失败')
    expect(dialog.get('[data-testid="page-review-rules"]').text()).toContain('本次尚未产生正式执行结果')
    expect(dialog.get('button.rule-chip').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="task-level-node"] button').trigger('click')
    dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.findComponent(GreenRuleResultDetails).exists()).toBe(false)
    expect(dialog.get('[data-testid="page-na-rules"]').text()).toContain('此规则仅适用于App')
    expect(dialog.text()).not.toContain('App外链浏览器承接分项判定')
  })

  it('新执行批次概览先到而规则接口仍在等待时，不保留上一批绿色结论或原图', async () => {
    vi.useFakeTimers()
    mocks.getTask.mockImplementation(async (taskId: string) => ({ ...task(taskId), status: 'running' }))
    mocks.getRuleResults.mockResolvedValueOnce({ items: [rule('旧批导航32', { green_result: projection('32') })] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    await wrapper.getComponent(PageDetailDialog).get('[data-testid="green-view-evidence"]').trigger('click')
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(true)
    let finish!: (value: { items: RuleResultItem[] }) => void
    mocks.getTask.mockImplementation(async (taskId: string) => ({ ...task(taskId), status: 'running', run_id: 'new-run' }))
    mocks.getRuleResults.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(false)
    expect(wrapper.findComponent(GreenRuleResultDetails).exists()).toBe(false)
    expect(wrapper.text()).not.toContain('旧批导航32')
    finish({ items: [] })
    await flushPromises()
  })

  it.each(['关联撤销', '原图成员变化'])('同任务轮询%s时关闭已打开的旧原图', async (change) => {
    vi.useFakeTimers()
    mocks.getTask.mockImplementation(async (taskId: string) => ({ ...task(taskId), status: 'running' }))
    mocks.getRuleResults.mockResolvedValue({ items: [rule('导航32', { green_result: projection('32') })] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    await wrapper.getComponent(PageDetailDialog).get('[data-testid="green-view-evidence"]').trigger('click')
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(true)
    const result = projection('32')
    if (change === '关联撤销') {
      result.linkage_status = 'evidence_changed'
      result.subitems = []
    } else {
      result.subitems[0]!.screenshot_artifact_ids = ['gallery-png']
    }
    mocks.getRuleResults.mockResolvedValue({ items: [rule('导航32', { green_result: result })] })
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(false)
  })

  it('32留在页面详情，S35留在整体检查，旧规则仍走原有分类', async () => {
    mocks.getRuleResults.mockResolvedValue({ items: [
      rule('导航32', { green_result: projection('32'), review_status: 'pending' }),
      rule('任务S35', { green_result: projection('s35'), page_capture_id: null, display_status: 'evidence_insufficient',
        machine_display_status: 'evidence_insufficient', execution_status: 'evidence_insufficient', review_status: 'pending' }),
      rule('旧页面失败'), rule('旧整体通过', { page_capture_id: null, display_status: 'passed', machine_display_status: 'passed', execution_status: 'pass' }),
    ] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    let dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.props('title')).toBe('首页')
    expect(dialog.findAllComponents(GreenRuleResultDetails).map((card) => card.props('result')?.rule_key)).toEqual(['32'])
    expect(dialog.get('[data-testid="page-failed-rules"]').text()).toContain('旧页面失败')
    expect(dialog.get('[data-testid="page-failed-rules"]').text()).not.toContain('导航32')
    expect(dialog.text()).not.toContain('任务S35')
    await wrapper.get('[data-testid="task-level-node"] button').trigger('click')
    dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.props('title')).toBe('整体检查')
    expect(dialog.findAllComponents(GreenRuleResultDetails).map((card) => card.props('result')?.rule_key)).toEqual(['s35'])
    expect(dialog.text()).toContain('旧整体通过')
    expect(dialog.text()).toContain('通过的规则（1 条）')
    expect(dialog.find('[data-testid="page-failed-rules"]').exists()).toBe(false)
  })

  it('分项只打开后端截图白名单，页面图库与界面结构不能越过父层边界', async () => {
    mocks.getRuleResults.mockResolvedValue({ items: [rule('导航32', { green_result: projection('32'), review_status: 'pending' })] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    const dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.findAll('button').some((button) => button.text().includes('人工确认'))).toBe(false)
    expect(dialog.getComponent(GreenRuleResultDetails).get('[data-testid="green-rule-status"]').text()).toContain('未通过 · 待人工复核')
    await dialog.get('[data-testid="green-view-evidence"]').trigger('click')
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    expect(viewer.props('initialArtifactId')).toBe('proof-png')
    expect(viewer.props('entries').map((entry: { artifact_id: string }) => entry.artifact_id)).toEqual(['proof-png'])
    for (const excluded of ['gallery-png', 'proof-ui']) {
      dialog.vm.$emit('view-evidence', dialog.props('rules')[0], excluded)
      await flushPromises()
      expect(viewer.props('initialArtifactId')).toBe('proof-png')
      expect(viewer.props('entries').map((entry: { artifact_id: string }) => entry.artifact_id)).toEqual(['proof-png'])
    }
    expect(mocks.push).not.toHaveBeenCalled()
  })

  it('后端将未关联的绿色结果标为待复核时，页面摘要不再把历史机器通过计为通过', async () => {
    const result = projection('32')
    Object.assign(result, {
      linkage_status: 'evidence_changed', linkage_notice: '正式材料已变化，暂不沿用原结论',
      execution_status: null, review_status: null, review_reason: null, subitems: [], missing_evidence: [],
    })
    mocks.getRuleResults.mockResolvedValue({ items: [rule('关联待核对32', {
      green_result: result, display_status: 'manual_review', display_status_label: '关联待核对',
      machine_display_status: 'passed', machine_display_status_label: '通过', execution_status: 'pass',
      confirmable: false, evidence_artifact_ids: ['proof-png'],
    })] })
    const wrapper = mountTask()
    await flushPromises()
    expect(wrapper.get('.timeline-card .timeline-metrics').text()).toContain('未完成判定 1 条')
    expect(wrapper.get('.timeline-card .timeline-metrics').text()).not.toContain('通过 1 条')
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    const dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.get('[data-testid="page-story"]').text()).toContain('未完成判定 1 条')
    expect(dialog.get('[data-testid="page-story"]').text()).not.toContain('通过 1 条')
    expect(dialog.get('[data-testid="green-rule-status"]').text()).toBe('关联待核对')
    expect(dialog.find('[data-testid="green-subitem"]').exists()).toBe(false)
    expect(dialog.find('[data-testid="green-view-evidence"]').exists()).toBe(false)
    expect(dialog.findAll('button').some((button) => button.text().includes('人工确认'))).toBe(false)
  })

  it('后端允许业务确认时仍使用原报告入口，不将缺证复核伪装成确认通过', async () => {
    const result = projection('32')
    result.execution_status = 'manual_review'
    result.missing_evidence = []
    result.review_reason = '已执行分项的把握度不足，等待人工核对'
    result.subitems = result.subitems.map((item) => ({ ...item, status: 'manual_review', evaluated: true, confidence: 0.5, missing_evidence: [] }))
    mocks.getRuleResults.mockResolvedValue({ items: [rule('可确认32', {
      green_result: result, confirmable: true, confirm_status: 'pending', display_status: 'manual_review',
      machine_display_status: 'manual_review', execution_status: 'manual_review', review_status: 'pending',
    })] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    const confirm = wrapper.getComponent(PageDetailDialog).findAll('button').find((button) => button.text() === '去报告页人工确认')!
    expect(confirm).toBeDefined()
    await confirm.trigger('click')
    expect(mocks.push).toHaveBeenCalledWith({ path: '/reports', query: { task: 'task-a', execution: 'execution-可确认32' } })
  })

  it('切换任务关闭旧原图，迟到的旧分项响应不能混入新任务', async () => {
    let finishOld!: (value: { items: RuleResultItem[] }) => void
    mocks.getRuleResults.mockResolvedValueOnce({ items: [rule('旧任务导航', { green_result: projection('32') })] })
    const wrapper = mountTask()
    await flushPromises()
    await wrapper.get('[data-testid="node-detail-1"]').trigger('click')
    await wrapper.getComponent(PageDetailDialog).get('[data-testid="green-view-evidence"]').trigger('click')
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(true)
    mocks.getRuleResults.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
    mocks.route.params.taskId = 'task-b'
    mocks.route.path = '/tasks/task-b'
    await flushPromises()
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="page-detail-dialog"]').exists()).toBe(false)
    mocks.getRuleResults.mockResolvedValue({ items: [rule('新任务S35', {
      page_capture_id: null, green_result: projection('s35'), display_status: 'evidence_insufficient',
      machine_display_status: 'evidence_insufficient', execution_status: 'evidence_insufficient', review_status: 'pending',
    })] })
    mocks.route.params.taskId = 'task-c'
    mocks.route.path = '/tasks/task-c'
    await flushPromises()
    expect(wrapper.find('[data-testid="viewer"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="page-detail-dialog"]').exists()).toBe(false)
    finishOld({ items: [rule('迟到旧任务导航', { green_result: projection('32') })] })
    await flushPromises()
    await wrapper.get('[data-testid="task-level-node"] button').trigger('click')
    const dialog = wrapper.getComponent(PageDetailDialog)
    expect(dialog.text()).toContain('新任务S35')
    expect(dialog.text()).not.toContain('旧任务导航')
    expect(dialog.findAllComponents(GreenRuleResultDetails).map((card) => card.props('result')?.rule_key)).toEqual(['s35'])
  })
})
