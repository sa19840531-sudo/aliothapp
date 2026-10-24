import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import type { ReportInsight, ReportRuleItem } from '@/types/report-insight'
import type { GreenRuleResultProjection } from '@/types/task-insight'

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())

const mocks = vi.hoisted(() => ({
  route: { path: '/reports/task-a', params: { taskId: 'task-a' }, query: {} },
  getReportInsight: vi.fn(), getTask: vi.fn(), getTaskScreenshots: vi.fn(),
  getRuleResults: vi.fn(), issueEvidenceFileUrl: vi.fn(), downloadReportExport: vi.fn(),
  openEvidence: vi.fn(), loadEvidence: vi.fn(), clearEvidence: vi.fn(),
  push: vi.fn(), replace: vi.fn(),
}))
vi.mock('vue-router', () => ({ useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }) }))
vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const plain = defineComponent({ setup: (_, { attrs, slots }) => () => h('div', attrs, slots.default?.()) })
  return { NIcon: plain, NSpin: plain,
    NButton: defineComponent({ setup: (_, { attrs, slots }) => () => h('button', attrs, slots.default?.()) }),
    NModal: defineComponent({ props: { show: Boolean }, setup: (props, { slots }) => () => props.show ? h('div', slots.default?.()) : null }),
    useMessage: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }) }
})
vi.mock('@/composables/useEvidenceFileUrls', () => ({ useEvidenceFileUrls: () => ({
  clear: mocks.clearEvidence, load: mocks.loadEvidence, open: mocks.openEvidence, get: (id: string) => `/fixture/${id}.png`,
}) }))
vi.mock('@/services/reports.api', () => ({ getReportInsight: mocks.getReportInsight,
  downloadReportExport: mocks.downloadReportExport, confirmReportRule: vi.fn() }))
vi.mock('@/services/tasks.api', () => ({ getTask: mocks.getTask }))
vi.mock('@/services/task-insight.api', () => ({ getTaskScreenshots: mocks.getTaskScreenshots,
  issueEvidenceFileUrl: mocks.issueEvidenceFileUrl, getRuleResults: mocks.getRuleResults }))
vi.mock('@/stores/auth', () => ({ authStore: { user: { value: { full_name: '展示夹具', role: '规则配置员' } } } }))

import ReportCenterView from './ReportCenterView.vue'
import RuleUsageDialog from '../components/RuleUsageDialog.vue'

function green(): GreenRuleResultProjection {
  return { schema_version: 'green-rule-result.v1', rule_key: '32', rule_contract_hash: 'a'.repeat(64),
    linkage_status: 'linked', linkage_notice: null, execution_status: 'fail', review_status: 'pending',
    review_reason: '仍有分项缺少必要材料', missing_evidence: ['text_bounds'], subitems: [
      { key: 'equal_width_navigation', status: 'fail', evaluated: true, confidence: 1, reason: '完整同级槽位宽度不同',
        missing_evidence: [], evidence_artifact_ids: ['ui-original'], screenshot_artifact_ids: ['green-one'], capture_ids: ['capture-1'] },
      { key: 'fit_navigation_spacing', status: 'evidence_insufficient', evaluated: false, confidence: null,
        reason: '文字边界仍缺证', missing_evidence: ['text_bounds'], evidence_artifact_ids: [],
        screenshot_artifact_ids: ['green-two'], capture_ids: ['capture-2'] },
      ...['overflow_scroll', 'initial_next_label'].map((key) => ({ key, status: 'evidence_insufficient', evaluated: false,
        confidence: null, reason: '材料尚不完整', missing_evidence: ['initial_navigation_state'],
        evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [] })),
    ] }
}

function item(overrides: Partial<ReportRuleItem> = {}): ReportRuleItem {
  return { item_type: 'execution', execution_id: 'execution-1', rule_id: 'rule-32', rule_code: 'alioth_green_32',
    rule_name: '导航规则', category: 'visual', rule_package: '受控规则', trigger_display: '页面导航',
    ruleset_code: 'green', ruleset_name: '绿色第4版', bound_page_type: 'home', bound_page_type_name: '首页',
    display_status: 'failed', display_status_label: '未通过', machine_display_status: 'failed', machine_display_status_label: '未通过',
    execution_status: 'fail', severity: 'medium', severity_display: 'P2', confidence: 1,
    result_summary: '旧顶层说明不覆盖当前分项', problem_desc: '旧顶层问题', evidence_artifact_ids: ['gallery', 'green-one', 'green-two'],
    page_capture_id: 'capture-1', is_bootstrap: false, finished_at: '2026-10-19T00:01:00Z', confirmable: false,
    human_verdict: 'pass', human_verdict_label: '通过', human_verdict_note: '历史确认', human_verdict_at: null,
    confirm_status: 'confirmed', green_result: green(), ...overrides }
}

function report(taskId = 'task-a', entry = item()): ReportInsight {
  return { task_id: taskId, summary: { task_id: taskId, task_name: `报告 ${taskId}`, target_url: 'https://example.test/',
    task_status: 'review', target_platform: 'web', started_at: null, finished_at: null, created_at: null, generated_at: null,
    report_stage: 'preview', page_recognized_count: 1, total_rules: 1, full_rule_pass_rate: 0, qc_success_rate: 0, pass_rate: 0,
    candidate_execution_coverage: 100, confirmed_not_applicable_rules: 0, coverage_gap_rules: 0, run_id: 'run-1',
    ruleset_code: 'green', ruleset_version: 4, ruleset_rule_total: 1 },
    qc_success_rate: 0, pass_rate: 0, page_exploration: { page_results: [] } as unknown as ReportInsight['page_exploration'],
    distribution: [], confirm_progress: { total: 0, confirmed: 0, pending: 0, completed: true }, export_ready: true,
    rule_items: [entry], confirm_items: [], green_results_incomplete: true,
    report_availability: 'preview', is_final: false, export_allowed: false }
}

function shots(ids = ['green-one', 'green-two', 'gallery']) {
  return { groups: [{ page_type_name: '首页', items: ids.map((artifact_id) => ({ artifact_id,
    page_capture_id: 'capture-1', captured_at: '2026-10-19T00:00:00Z', related_rules: [] })) }] }
}

function mountReport() {
  return mount(ReportCenterView, { global: { stubs: { WebReportExplanationPanel: true } } })
}

async function openDetails(wrapper: ReturnType<typeof mountReport>) {
  await flushPromises()
  await wrapper.get('[data-testid="rule-detail-alioth_green_32"]').trigger('click')
  return wrapper.getComponent(RuleUsageDialog)
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.route = reactive({ path: '/reports/task-a', params: { taskId: 'task-a' }, query: {} })
  mocks.getReportInsight.mockImplementation(async (taskId: string) => report(taskId))
  mocks.getTask.mockResolvedValue({ task_id: 'task-a', status: 'review', report_status: 'ready', run_id: 'run-1' })
  mocks.getTaskScreenshots.mockResolvedValue(shots())
  mocks.issueEvidenceFileUrl.mockResolvedValue('/fixture/download.png')
})

describe('未封存报告的当前绿色结果', () => {
  it('保留明确失败、缺项和实际待复核，完整质检为零且零人工确认不能解锁导出', async () => {
    const current = report()
    current.summary.target_platform = 'h5'
    mocks.getReportInsight.mockResolvedValue(current)
    const wrapper = mountReport()
    const dialog = await openDetails(wrapper)
    expect(wrapper.get('[data-testid="rule-row-alioth_green_32"] [data-testid="final-result"]').text()).toBe('未通过 · 待人工复核')
    expect(wrapper.get('[data-testid="phase-rate"]').text()).toContain('0%')
    expect(dialog.get('[data-testid="rule-usage-group-executed"]').text()).toContain('完整同级槽位宽度不同')
    expect(dialog.text()).toContain('尚未判定')
    expect(dialog.text()).not.toContain('旧顶层说明不覆盖当前分项')
    expect(dialog.text()).not.toContain('历史确认')
    expect(dialog.find('.usage-judge').exists()).toBe(false)
    expect(wrapper.get('[data-testid="green-review-gap-note"]').text()).toContain('无需业务确认不代表这些缺口已解决')
    expect(wrapper.get('[data-testid="export-report"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="export-report"]').trigger('click')
    expect(mocks.downloadReportExport).not.toHaveBeenCalled()
    expect(mocks.getRuleResults).not.toHaveBeenCalled()
  })

  it('分项原图仅打开点击的已核验PNG，拒绝同规则其他分项与图库补图', async () => {
    const wrapper = mountReport()
    const dialog = await openDetails(wrapper)
    await dialog.findAll('[data-testid="green-view-evidence"]')[0]!.trigger('click')
    expect(wrapper.get('[data-testid="shot-viewer"] img').attributes('src')).toBe('/fixture/green-one.png')
    expect(wrapper.get('[data-testid="viewer-next"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="shot-viewer"]').findAll('button').find((button) => button.text() === '关闭')!.trigger('click')
    dialog.vm.$emit('green-view-evidence', item(), 'gallery')
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-viewer"]').exists()).toBe(false)
  })

  it.each(['失联', '原图撤销', '合同变化'])('静默读取发生%s时撤图，旧组件事件不能重新开图', async (change) => {
    vi.useFakeTimers()
    const first = report()
    first.summary.task_status = 'running'
    mocks.getReportInsight.mockResolvedValue(first)
    const wrapper = mountReport()
    const dialog = await openDetails(wrapper)
    const old = dialog.props('rule')!.items[0]!
    await dialog.findAll('[data-testid="green-view-evidence"]')[0]!.trigger('click')
    const next = structuredClone(first)
    const current = next.rule_items[0]!.green_result!
    if (change === '失联') { current.linkage_status = 'evidence_changed'; current.subitems = [] }
    if (change === '原图撤销') current.subitems[0]!.screenshot_artifact_ids = []
    if (change === '合同变化') current.rule_contract_hash = 'b'.repeat(64)
    mocks.getReportInsight.mockResolvedValue(next)
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-viewer"]').exists()).toBe(false)
    dialog.vm.$emit('green-view-evidence', old, 'green-one')
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-viewer"]').exists()).toBe(false)
  })

  it('新Run概览使旧绿色卡和原图退出，即使下一份报告仍未返回', async () => {
    vi.useFakeTimers()
    const first = report()
    first.summary.task_status = 'running'
    mocks.getReportInsight.mockResolvedValue(first)
    const wrapper = mountReport()
    const dialog = await openDetails(wrapper)
    await dialog.findAll('[data-testid="green-view-evidence"]')[0]!.trigger('click')
    mocks.getTask.mockResolvedValue({ run_id: 'run-2', status: 'running', report_status: 'ready' })
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-viewer"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="green-rule-details"]').exists()).toBe(false)
  })

  it('A→B→A的旧报告迟到时不能覆盖当前读取', async () => {
    let oldReport!: (value: ReportInsight) => void
    mocks.getReportInsight.mockImplementationOnce(() => new Promise((resolve) => { oldReport = resolve }))
    const wrapper = mountReport()
    await flushPromises()
    mocks.route.params.taskId = 'task-b'
    await flushPromises()
    mocks.route.params.taskId = 'task-a'
    await flushPromises()
    const obsolete = report('task-a')
    obsolete.summary.task_name = '不应显示的迟到报告'
    oldReport(obsolete)
    await flushPromises()
    expect(wrapper.text()).not.toContain('不应显示的迟到报告')
    expect(wrapper.text()).toContain('报告 task-a')
  })

  it('A→B→A的旧截图索引迟到时不能替换当前原图交集', async () => {
    let oldShots!: (value: ReturnType<typeof shots>) => void
    mocks.getTaskScreenshots.mockImplementationOnce(() => new Promise((resolve) => { oldShots = resolve }))
    const wrapper = mountReport()
    await flushPromises()
    mocks.route.params.taskId = 'task-b'
    await flushPromises()
    mocks.route.params.taskId = 'task-a'
    await flushPromises()
    const dialog = await openDetails(wrapper)
    expect(dialog.findAll('[data-testid="green-view-evidence"]')).toHaveLength(2)
    oldShots(shots(['gallery']))
    await flushPromises()
    expect(dialog.findAll('[data-testid="green-view-evidence"]')).toHaveLength(2)
    await dialog.findAll('[data-testid="green-view-evidence"]')[0]!.trigger('click')
    expect(wrapper.get('[data-testid="shot-viewer"] img').attributes('src')).toBe('/fixture/green-one.png')
  })

  it('旧Task的下载票据迟到时不打开旧原图', async () => {
    let finishTicket!: (value: string) => void
    mocks.issueEvidenceFileUrl.mockImplementation(() => new Promise((resolve) => { finishTicket = resolve }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const wrapper = mountReport()
    const dialog = await openDetails(wrapper)
    await dialog.findAll('[data-testid="green-view-evidence"]')[0]!.trigger('click')
    await wrapper.get('[data-testid="shot-viewer"]').findAll('button').find((button) => button.text() === '下载当前截图')!.trigger('click')
    mocks.route.params.taskId = 'task-b'
    await flushPromises()
    finishTicket('/fixture/old-ticket.png')
    await flushPromises()
    expect(click).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="shot-viewer"]').exists()).toBe(false)
  })

  it('后端仅提供未完整标记且确认数为零时也禁止导出', async () => {
    const current = report()
    current.summary.target_platform = 'h5'
    current.rule_items = []
    mocks.getReportInsight.mockResolvedValue(current)
    const wrapper = mountReport()
    await flushPromises()
    expect(wrapper.get('[data-testid="export-report"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="green-review-gap-note"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="report-state"]').text()).toBe('需复核')
  })

  it('材料完整且实际复核通过的机器待复核记录沿服务端公开结论完成，不永久阻塞导出', async () => {
    const complete = green()
    complete.execution_status = 'manual_review'
    complete.review_status = 'approved'
    complete.missing_evidence = []
    complete.subitems = complete.subitems.map((part) => ({ ...part, evaluated: true, status: 'pass',
      confidence: 1, missing_evidence: [] }))
    const current = report('task-a', item({ green_result: complete, execution_status: 'manual_review',
      display_status: 'passed', display_status_label: '通过' }))
    current.summary.target_platform = 'h5'
    current.green_results_incomplete = false
    mocks.getReportInsight.mockResolvedValue(current)
    const wrapper = mountReport()
    await flushPromises()
    expect(wrapper.get('[data-testid="rule-row-alioth_green_32"] [data-testid="final-result"]').text()).toBe('通过')
    expect(wrapper.get('[data-testid="phase-rate"]').text()).toContain('100%')
    expect(wrapper.get('[data-testid="export-report"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="green-review-gap-note"]').exists()).toBe(false)
  })

  it('同规则多次执行按实际绿色待复核显示次数，不借旧确认清单为空显示无待办', async () => {
    const current = report()
    current.rule_items.push(item({ execution_id: 'execution-2', page_capture_id: 'capture-2' }))
    mocks.getReportInsight.mockResolvedValue(current)
    const wrapper = mountReport()
    await flushPromises()
    expect(wrapper.get('[data-testid="rule-row-alioth_green_32"] [data-testid="confirm-status"]').text()).toBe('待人工复核 2 次')
  })

  it('没有green_result或新增标记的封存报告保留原统计、原图与导出路径', async () => {
    const sealed = report('task-a', item({ green_result: undefined, display_status: 'passed', display_status_label: '通过',
      machine_display_status: 'passed', machine_display_status_label: '通过', execution_status: 'pass' }))
    delete sealed.green_results_incomplete
    Object.assign(sealed, { is_final: true, report_availability: 'final', export_allowed: true })
    mocks.getReportInsight.mockResolvedValue(sealed)
    const wrapper = mountReport()
    const dialog = await openDetails(wrapper)
    expect(dialog.find('[data-testid="green-rule-details"]').exists()).toBe(false)
    expect(dialog.findAll('.usage-shot')).toHaveLength(3)
    expect(wrapper.get('[data-testid="phase-rate"]').text()).toContain('100%')
    expect(wrapper.get('[data-testid="export-report"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="green-review-gap-note"]').exists()).toBe(false)
    expect(mocks.getRuleResults).not.toHaveBeenCalled()
  })
})
