import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AliothHttpError } from '@/services/http'

const mocks = vi.hoisted(() => ({
  getReportInsight: vi.fn(),
  getTask: vi.fn(),
  getTaskScreenshots: vi.fn(),
  loadEvidence: vi.fn(() => Promise.resolve()),
  confirmReportRule: vi.fn(),
  downloadReportExport: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  route: {
    path: '/reports/task-outside-first-page',
    params: { taskId: 'task-outside-first-page' } as Record<string, string>,
    query: {} as Record<string, string>,
  },
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h, watch } = await import('vue')
  const passthrough = defineComponent({
    setup(_, { attrs, slots }) {
      return () => h('div', attrs, slots.default?.())
    },
  })
  // 和真的弹窗一样只在 show 为真时渲染内容：页面关弹窗只收起开关、内容会留着
  const modal = defineComponent({
    props: { show: Boolean },
    emits: ['after-leave'],
    setup(props, { slots, emit }) {
      // 真弹窗关完动画才发 after-leave；这里没有动画，一收起就发
      watch(
        () => props.show,
        (visible) => {
          if (!visible) emit('after-leave')
        },
      )
      return () => (props.show ? h('div', slots.default?.()) : null)
    },
  })
  return {
    NButton: passthrough,
    NIcon: passthrough,
    NModal: modal,
    NSpin: passthrough,
    useMessage: () => mocks.message,
  }
})

vi.mock('@vicons/ionicons5', async () => {
  const { defineComponent, h } = await import('vue')
  const icon = defineComponent({ render: () => h('span') })
  return {
    ChevronBackOutline: icon,
    ChevronForwardOutline: icon,
    DownloadOutline: icon,
  }
})

vi.mock('@/composables/useEvidenceFileUrls', () => ({
  useEvidenceFileUrls: () => ({
    clear: () => undefined,
    load: mocks.loadEvidence,
    get: (artifactId: string) => `blob:${artifactId}`,
    open: () => Promise.resolve(),
  }),
}))

vi.mock('@/services/reports.api', () => ({
  confirmReportRule: mocks.confirmReportRule,
  downloadReportExport: mocks.downloadReportExport,
  getReportInsight: mocks.getReportInsight,
}))

vi.mock('@/services/task-insight.api', () => ({
  getTaskScreenshots: mocks.getTaskScreenshots,
}))

vi.mock('@/services/tasks.api', () => ({
  getTask: mocks.getTask,
}))

vi.mock('@/stores/auth', () => ({
  authStore: { user: { value: { username: 'u', full_name: '刘一', role: '规则配置员', tenant_name: '北京分公司' } } },
}))

import ReportCenterView from './ReportCenterView.vue'
import type { ReportRuleItem, WebLogicalReportPage } from '@/types/report-insight'

function insight(taskId: string, taskName: string) {
  return {
    task_id: taskId,
    summary: {
      task_name: taskName,
      task_id: taskId,
      target_url: 'https://example.test/',
      task_status: 'review',
      target_platform: 'web',
      started_at: '2026-09-04T00:00:00Z',
      finished_at: '2026-09-04T00:01:00Z',
      created_at: '2026-09-04T00:00:00Z',
      generated_at: '2026-09-04T00:01:00Z',
      report_stage: 'preview',
      page_recognized_count: 1,
      total_rules: 0,
      full_rule_pass_rate: 0,
      qc_success_rate: 0,
      pass_rate: 0,
      candidate_execution_coverage: 0,
      ruleset_candidate_coverage: 0,
      confirmed_not_applicable_rules: 0,
      coverage_gap_rules: 0,
      run_id: 'run-1',
      ruleset_code: 'alioth_first_ruleset',
      ruleset_version: 1,
    },
    qc_success_rate: 0,
    pass_rate: 0,
    page_exploration: null,
    distribution: [],
    confirm_progress: { total: 0, confirmed: 0, pending: 0, completed: true },
    export_ready: false,
    confirm_items: [] as ReportRuleItem[],
    rule_items: [] as ReportRuleItem[],
    report_availability: 'preview',
    is_final: false,
    export_allowed: false,
    incomplete_reasons: ['WEB_REPORT_NOT_FINALIZED'],
    web_preview: {
      trusted_capture_count: 2,
      inspection_progress: { total: 4, completed: 3, pending: 1 },
      page_type_review: { total: 2, completed: 1, pending: 1 },
      obstructions: [{ kind: 'popup_wall' }],
      completion_gate: {
        gate_passed: false,
        report_bound: false,
        missing_page_types: ['video_detail'],
        unrecognized_page_types: [],
        pending_confirmations: 1,
        gaps: ['completion_gate_not_passed', 'report_binding_pending'],
      },
    },
    web_logical_pages: [] as WebLogicalReportPage[],
  }
}

function ruleItem(overrides: Partial<ReportRuleItem> & { rule_code: string }): ReportRuleItem {
  return {
    execution_id: `exec-${overrides.rule_code}`,
    rule_id: `rule-${overrides.rule_code}`,
    rule_name: `规则 ${overrides.rule_code}`,
    category: 'ad',
    rule_package: '广告规则包',
    trigger_display: '进入首页后 3 秒内',
    bound_page_type: 'home',
    bound_page_type_name: '首页',
    display_status: 'failed',
    display_status_label: '未通过',
    machine_display_status: 'failed',
    machine_display_status_label: '未通过',
    execution_status: 'fail',
    severity: 'critical',
    severity_display: 'P0',
    confidence: 0.92,
    result_summary: '机器判定：首页弹窗没有关闭按钮',
    problem_desc: '首页弹窗遮挡主体内容',
    evidence_artifact_ids: [],
    page_capture_id: null,
    is_bootstrap: false,
    finished_at: null,
    confirmable: false,
    human_verdict: null,
    human_verdict_label: null,
    human_verdict_note: null,
    human_verdict_at: null,
    confirm_status: null,
    ...overrides,
  }
}

function passedRule(code: string): ReportRuleItem {
  return ruleItem({
    rule_code: code,
    display_status: 'passed',
    display_status_label: '通过',
    machine_display_status: 'passed',
    machine_display_status_label: '通过',
    execution_status: 'pass',
    severity: 'low',
    severity_display: 'P3',
    result_summary: null,
    problem_desc: null,
  })
}

/** 45 条规则：3 条机器未通过（直接生效）、3 条需人工确认（2 条待确认 + 1 条已确认为未通过）、39 条通过。 */
function reportWithRules() {
  const report = insight('task-outside-first-page', '手机银行首页巡检')
  const failedRules = [
    ruleItem({
      rule_code: 'alioth_1',
      rule_name: '首页弹窗必须可关闭',
      evidence_artifact_ids: ['art-1', 'art-2', 'art-3', 'art-missing'],
    }),
    ruleItem({
      rule_code: 'alioth_2',
      rule_name: '文案不得出现错别字',
      category: 'text',
      rule_package: '文案质检',
      severity: 'high',
      severity_display: 'P1',
      execution_status: 'warning',
      result_summary: '文本扫描发现疑似错别字',
      problem_desc: '「登陆」应为「登录」',
    }),
    ruleItem({
      rule_code: 'alioth_3',
      rule_name: '价格展示需带币种',
      category: 'payment',
      severity: 'medium',
      severity_display: 'P2',
    }),
  ]
  const gapRule = ruleItem({
    rule_code: 'alioth_4',
    rule_name: '播放器需支持倍速',
    category: 'player',
    display_status: 'coverage_gap',
    display_status_label: '计划外未覆盖',
    machine_display_status: 'coverage_gap',
    machine_display_status_label: '运行未覆盖',
    execution_status: 'skipped',
    is_bootstrap: true,
    result_summary: '本次没有进入播放页，未运行这条规则',
    confirmable: false,
    confirm_status: null,
  })
  const otherRule = ruleItem({
    rule_code: 'alioth_5',
    rule_name: '图片未使用 CDN',
    display_status: 'other',
    display_status_label: '其他',
    machine_display_status: 'manual_review',
    machine_display_status_label: '待人工复核',
    execution_status: 'manual_review',
    confidence: null,
    result_summary: '资源检测发现异常，但证据不足以下结论',
    confirmable: true,
    confirm_status: 'pending',
  })
  const confirmedErrorRule = ruleItem({
    rule_code: 'alioth_6',
    rule_name: '页面截图采集',
    severity: 'medium',
    severity_display: 'P2',
    display_status: 'failed',
    display_status_label: '未通过',
    machine_display_status: 'manual_review',
    machine_display_status_label: '待人工复核',
    execution_status: 'manual_review',
    confirmable: true,
    confirm_status: 'confirmed',
    human_verdict: 'fail',
    human_verdict_label: '未通过',
  })
  const confirmRules = [otherRule, confirmedErrorRule]
  const passedRules = Array.from({ length: 39 }, (_, index) => passedRule(`alioth_p${index + 1}`))
  const ruleItems = [...failedRules, gapRule, ...confirmRules, ...passedRules]
  return {
    ...report,
    summary: {
      ...report.summary,
      total_rules: ruleItems.length,
      page_recognized_count: 6,
      task_status: 'done',
      report_stage: 'final' as const,
    },
    pass_rate: 90.7,
    rule_items: ruleItems,
    confirm_items: confirmRules,
    confirm_progress: { total: 2, confirmed: 1, pending: 1, completed: false },
    report_availability: 'final',
    export_ready: false,
  }
}

/** 同一份报告在人工确认全部完成后的样子（接口刷新后返回）。 */
function reportAllConfirmed() {
  const report = reportWithRules()
  const confirm = (item: ReportRuleItem): ReportRuleItem =>
    item.confirm_status === 'pending'
      ? {
          ...item,
          confirm_status: 'confirmed',
          human_verdict: 'ignored',
          human_verdict_label: '本产品不适用',
          display_status: 'not_applicable',
          display_status_label: '本产品不适用',
        }
      : item
  return {
    ...report,
    rule_items: report.rule_items.map(confirm),
    confirm_items: report.confirm_items.map(confirm),
    confirm_progress: { total: 2, confirmed: 2, pending: 0, completed: true },
    export_ready: true,
  }
}

function screenshotsFor(artifactIds: string[]) {
  return {
    task_id: 'task-outside-first-page',
    run_id: 'run-1',
    groups: [
      {
        page_group_key: 'home',
        page_title: null,
        page_display_name: '首页',
        page_type: 'home',
        page_type_name: '首页',
        total: artifactIds.length,
        items: artifactIds.map((artifactId, index) => ({
          artifact_id: artifactId,
          page_capture_id: `capture-${index}`,
          captured_at: '2026-09-04T00:00:30Z',
          content_type: 'image/png',
          size_bytes: 100,
          duplicate_count: 0,
          related_rules: [
            {
              rule_code: 'alioth_1',
              rule_name: '首页弹窗必须可关闭',
              display_status: 'failed',
              display_status_label: '未通过',
            },
          ],
        })),
      },
    ],
  }
}

function row(wrapper: VueWrapper, ruleCode: string) {
  return wrapper.get(`[data-testid="rule-row-${ruleCode}"]`)
}

let mounted: VueWrapper | null = null

function mountView(options: { attach?: boolean } = {}) {
  mounted = mount(ReportCenterView, options.attach ? { attachTo: document.body } : {})
  return mounted
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.route = {
    path: '/reports/task-outside-first-page',
    params: { taskId: 'task-outside-first-page' },
    query: {},
  }
  mocks.getTask.mockResolvedValue({
    task_id: 'task-outside-first-page',
    task_name: '手机银行首页巡检',
    product: { department_name: '一部', product_code: 'MB-001', product_name: '手机银行 Android' },
    target_platform: 'app',
    status: 'done',
    report_status: 'ready',
    tenant_name: '上海分公司',
    ruleset_code: 'alioth_first_ruleset',
    ruleset_version: 1,
    started_at: '2026-08-31T01:28:00Z',
    finished_at: '2026-08-31T01:30:15Z',
  })
  mocks.getTaskScreenshots.mockResolvedValue({ task_id: 'task-outside-first-page', run_id: 'run-1', groups: [] })
  mocks.getReportInsight.mockImplementation((taskId: string) =>
    Promise.resolve(insight(taskId, 'URL 指定的 Web 预览报告')),
  )
})

afterEach(() => {
  mounted?.unmount()
  mounted = null
})

describe('ReportCenterView 报告详情', () => {
  it('规则引用折叠帧时仍加载并展示真实截图，不因任务详情挂起而等待', async () => {
    mocks.getTask.mockReturnValue(new Promise(() => undefined))
    const item = ruleItem({
      rule_code: 'alioth_1',
      evidence_artifact_ids: ['folded-art', 'outside-task'],
      bound_page_type: null,
      bound_page_type_name: null,
      page_capture_id: 'folded-capture',
    })
    mocks.getReportInsight.mockResolvedValue({
      ...reportWithRules(), rule_items: [item], confirm_items: [],
    })
    const shots = screenshotsFor(['representative-art'])
    mocks.getTaskScreenshots.mockResolvedValue({
      ...shots,
      groups: [{ ...shots.groups[0], items: [{
        ...shots.groups[0].items[0],
        related_frames: [{ artifact_id: 'folded-art', page_capture_id: 'folded-capture',
          captured_at: '2026-09-04T00:00:31Z', duplicate_count: 0 }],
      }] }],
    })
    const wrapper = mountView()
    await flushPromises()

    expect(mocks.loadEvidence).toHaveBeenCalledWith(['representative-art', 'folded-art'])
    expect(row(wrapper, 'alioth_1').text()).toContain('页面未识别')
    expect(row(wrapper, 'alioth_1').get('img').attributes('src')).toBe('blob:folded-art')
    await wrapper.get('[data-testid="rule-detail-alioth_1"]').trigger('click')
    const dialog = wrapper.get('[data-testid="rule-usage-dialog"]')
    expect(dialog.find('[data-testid="rule-usage-shot-outside-task"]').exists()).toBe(false)
    await dialog.get('[data-testid="rule-usage-shot-folded-art"]').trigger('click')
    expect(wrapper.get('[data-testid="shot-viewer"] img').attributes('src')).toBe('blob:folded-art')
    expect(wrapper.get('[data-testid="viewer-count"]').text()).toBe('1 / 1')
  })

  it('真实失败和未执行记录在详情分区，未执行规则不进入未通过筛选', async () => {
    const failed = ruleItem({ execution_id: 'failed-attempt', rule_code: 'mixed' })
    const gap = ruleItem({ execution_id: 'gap-attempt', rule_code: 'mixed',
      machine_display_status: 'not_executed', execution_status: 'skipped',
      display_status: 'other', display_status_label: '其他', confirmable: false,
      page_capture_id: null, bound_page_type: null, bound_page_type_name: null })
    const gapOnly = { ...gap, rule_code: 'gap-only', rule_id: 'gap-only-rule', execution_id: 'only-gap' }
    mocks.getReportInsight.mockResolvedValue({
      ...reportWithRules(), rule_items: [failed, gap, gapOnly], confirm_items: [],
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="tile-failed"]').trigger('click')
    expect(wrapper.find('[data-testid="rule-row-gap-only"]').exists()).toBe(false)
    await wrapper.get('[data-testid="rule-detail-mixed"]').trigger('click')
    const dialog = wrapper.get('[data-testid="rule-usage-dialog"]')
    expect(dialog.get('[data-testid="rule-usage-group-executed"]').text()).toContain('未通过')
    const unknown = dialog.get('[data-testid="rule-usage-group-undecidable"]')
    expect(unknown.text()).toContain('未关联具体页面')
    expect(unknown.find('[data-testid="rule-usage-gap-attempt"]').exists()).toBe(true)
    expect(unknown.find('[data-testid="rule-usage-judge-gap-attempt"]').exists()).toBe(false)
  })

  it('截图请求失败显示加载失败，不伪装成无证据', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules())
    mocks.getTaskScreenshots.mockRejectedValue(new Error('网络故障'))
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="rule-detail-alioth_1"]').trigger('click')
    const dialog = wrapper.get('[data-testid="rule-usage-dialog"]')
    expect(dialog.text()).toContain('截图加载失败')
    expect(dialog.text()).not.toContain('这一次没有截图证据')
  })

  it('按路由里的任务编号直接加载该报告，标题行显示报告状态三态', async () => {
    mocks.getTask.mockResolvedValueOnce({
      task_id: 'task-outside-first-page',
      product: { department_name: '一部', product_code: 'MB-001', product_name: '企业网银' },
      status: 'running',
      report_status: 'generating',
      started_at: '2026-09-04T00:00:00Z',
      finished_at: null,
    })
    const wrapper = mountView()
    await flushPromises()

    expect(mocks.getReportInsight).toHaveBeenCalledWith('task-outside-first-page')
    expect(wrapper.text()).toContain('URL 指定的 Web 预览报告')
    const state = wrapper.get('[data-testid="report-header"] [data-testid="report-state"]')
    expect(state.text()).toBe('生成中')
    expect(state.classes()).toContain('tone-primary')
    expect(wrapper.text()).not.toContain('运行中预览')
  })

  it('H5 V2 复核报告不套用 Web 预览水印且允许导出', async () => {
    const report = insight('task-outside-first-page', 'H5 复核报告')
    report.summary.target_platform = 'h5'
    report.export_ready = true
    report.export_allowed = true
    report.incomplete_reasons = ['H5_REPORT_NOT_FINALIZED']
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).not.toContain('预览 · 不可导出')
    expect(wrapper.text()).toContain('复核报告 · 非正式')
    expect(wrapper.find('[data-testid="export-report"]').exists()).toBe(true)
  })

  it('App 预览报告继续保持不可导出展示', async () => {
    const report = insight('task-outside-first-page', 'App 预览报告')
    report.summary.target_platform = 'app'
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('预览 · 不可导出')
    expect(wrapper.text()).not.toContain('复核报告 · 非正式')
    expect(wrapper.find('[data-testid="export-report"]').exists()).toBe(false)
  })

  it('App V2 未完成预览可查看真实规则和统计，导出处理仍阻止下载且不显示 Web 说明', async () => {
    const { web_preview: _webPreview, ...report } = insight('task-outside-first-page', 'Android 全检预览报告')
    report.summary.target_platform = 'app'
    report.summary.report_stage = 'final_review'
    report.summary.total_rules = 3
    report.incomplete_reasons = ['APP_REPORT_NOT_FINALIZED']
    report.rule_items = [
      passedRule('app-pass'),
      ruleItem({ rule_code: 'app-fail', rule_name: 'Android 弹窗需支持关闭' }),
      ruleItem({
        rule_code: 'app-gap',
        rule_name: 'Android 播放页仍缺证据',
        execution_status: 'evidence_insufficient',
        machine_display_status: 'evidence_insufficient',
        machine_display_status_label: '证据不足',
        display_status: 'other',
        display_status_label: '其他',
        result_summary: '尚未获取播放页完整截图',
      }),
    ]
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="report-header"]').text()).toContain('Android 全检预览报告')
    expect(wrapper.get('[data-testid="tile-total"] strong').text()).toBe('3')
    expect(wrapper.get('[data-testid="tile-passed"] strong').text()).toBe('1')
    expect(wrapper.get('[data-testid="tile-failed"] strong').text()).toBe('1')
    expect(wrapper.get('[data-testid="tile-other"] strong').text()).toBe('1')
    expect(row(wrapper, 'app-fail').text()).toContain('Android 弹窗需支持关闭')
    expect(row(wrapper, 'app-gap').text()).toContain('证据不足')
    expect(wrapper.text()).toContain('检测未完整完成：有 1 次检测未形成结论')
    expect(wrapper.text()).toContain('预览 · 不可导出')
    expect(wrapper.find('[data-testid="export-report"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="web-preview-status"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Web 证据说明')
    expect(wrapper.text()).not.toContain('Web 逻辑页面证据')

    // 预览会隐藏导出入口；残留的处理函数也必须遵守当前报告门禁。
    await (wrapper.vm as unknown as { exportReport: () => Promise<void> }).exportReport()
    expect(mocks.downloadReportExport).not.toHaveBeenCalled()
    expect(mocks.message.warning).toHaveBeenCalledWith('报告暂不满足导出条件，请稍后再试')
  })

  it('报告将首页角色和四类业务页型同时展示', async () => {
    const report = insight('task-outside-first-page', 'Web 首页报告')
    report.web_logical_pages = [
      {
        logical_page_id: 'logical-home',
        canonical_url: 'https://example.test/landing',
        menu_entry_ids: ['example.home'],
        capture_count: 1,
        primary_capture_id: 'capture-home',
        page_role: 'home',
        page_type: 'video_list',
        page_type_name: '视频列表页',
        classification_status: 'classified',
        raw_page_type: 'video_list',
        proposed_page_type: 'video_list',
        observed_page_type: 'video_list',
        expected_page_type: 'video_list',
        conflict_reason_code: null,
        diagnostic_message: null,
        confidence: 0.95,
        review_reason: null,
        inspection_status: 'completed',
        evidence_gallery: [],
      },
    ]
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('首页 · 视频列表页')
  })

  it('历史 Web V1 报告缺少三态字段时仍保留导出入口', async () => {
    const { report_availability: _reportAvailability, ...report } = insight(
      'task-outside-first-page',
      'Web V1 历史报告',
    )
    report.summary.report_stage = 'final'
    report.export_ready = true
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).not.toContain('预览 · 不可导出')
    expect(wrapper.find('[data-testid="export-report"]').exists()).toBe(true)
  })

  it('真实报告中心展示未发布原因和失败序列，逐条已写规则不被批次错误覆盖', async () => {
    const report = Object.assign(insight('task-outside-first-page', 'Web 材料解释'), {
      web_identity_diagnostics: [{ capture_id: 'untrusted-frame', reason_code: 'WEB_LOGICAL_ID_UNTRUSTED' }],
      web_report_explanation: {
        schema_version: 'web-report-explanation.v1', task_id: 'task-outside-first-page', agent_run_id: 'run-1', finalization_epoch: 0,
        notifications: [{ capture_id: 'classified-frame', classification_status: 'recognized', notification: { status: 'blocked', reason_code: 'WEB_CAPTURE_ARTIFACT_MISSING' } }],
        sequences: [{ sequence_id: 'sequence-1', purpose: 'failure_sequence', readiness_status: 'loading', readiness_exit_reason: 'timeout', frames: [{ capture_id: 'failed-frame', capture_role: 'after_action', page_state: 'loading' }] },
          { sequence_id: 'sequence-2', purpose: 'failure_sequence', readiness_status: 'loading', readiness_exit_reason: 'loading', frames: [] }],
        primary_content: [{ menu_entry_id: 'menu-1', status: 'failed', reason_code: 'WEB_PRIMARY_CONTENT_CAPTURE_FAILED', parent_capture_id: 'baseline-frame', attempts: [{ stage: 'observe', source_capture_id: 'direct-parent', after_capture_id: 'failed-after' }] }],
        publication_rules: [{ rule_code: 'account-rule', selection: { status: 'evidence_insufficient', reason_code: 'WEB_RULE_ACCOUNT_STATE_UNKNOWN' } }],
        rule_materials: [{ rule_id: 'rule-1', rule_code: 'stable-rule', work_item_id: 'work-1', work_status: 'failed', status: 'written', material_role: 'stable', canonical_status: 'pass', attempts: [{ attempt_id: 'attempt-1', status: 'terminal_error', execution_status: 'pass' }] }],
      },
    })
    mocks.getReportInsight.mockResolvedValue(report)
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('已分类但未发布质检')
    expect(wrapper.text()).toContain('WEB_CAPTURE_ARTIFACT_MISSING')
    expect(wrapper.text()).toContain('有可判定序列不代表页面稳定或覆盖完成')
    expect(wrapper.text()).toContain('attempt-1')
    expect(wrapper.text()).toContain('WEB_RULE_ACCOUNT_STATE_UNKNOWN')
    expect(wrapper.text()).toContain('WEB_LOGICAL_ID_UNTRUSTED')
    expect(wrapper.text()).toContain('WEB_PRIMARY_CONTENT_CAPTURE_FAILED')
    expect(wrapper.text()).toContain('direct-parent')
    expect(wrapper.text()).toContain('稳定性 loading · 出口 timeout')
    expect(wrapper.text()).toContain('稳定性 loading · 出口 loading')
    expect(wrapper.text()).toContain('帧状态 loading')
  })

  it('预览报告展示可信采集、质检、类型复核、阻断和 Gate 缺口', async () => {
    const wrapper = mountView()
    await flushPromises()

    const preview = wrapper.get('[data-testid="web-preview-status"]').text()
    expect(preview).toContain('可信采集 2')
    expect(preview).toContain('质检进度 3/4')
    expect(preview).toContain('页面类型待复核 1')
    expect(preview).toContain('阻断 1')
    expect(preview).toContain('完成校验缺口 2')
  })

  it('无持久化报告事实时展示明确原因而不是空白报告', async () => {
    const report = insight('task-outside-first-page', 'Web 尚无报告事实')
    report.report_availability = 'none'
    report.incomplete_reasons = ['WEB_REPORT_NO_PERSISTED_FACTS']
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('尚未产生可信采集')
    expect(wrapper.text()).toContain('查看任务')
  })

  it('H5 无真实质检结果时保留诊断和任务入口，不能显示报告成功或导出', async () => {
    const report = insight('task-outside-first-page', 'H5 导航未完成质检')
    report.summary.target_platform = 'h5'
    report.summary.report_stage = 'final_review'
    report.report_availability = 'none'
    report.incomplete_reasons = ['H5_REPORT_NO_BUSINESS_RESULTS']
    report.rule_items = [ruleItem({
      rule_code: 'alioth_unstarted',
      display_status: 'other',
      display_status_label: '未执行',
      machine_display_status: 'not_executed',
      machine_display_status_label: '未执行',
      execution_status: 'skipped',
      result_summary: '首次导航超出任务允许的网站范围，规则尚未执行',
    })]
    mocks.route.query = { page: '3', page_size: '10', submitter_name: '立川' }
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="report-state"]').text()).toBe('尚无有效质检结果')
    const notice = wrapper.get('[data-testid="report-none-notice"]')
    expect(notice.text()).toContain('诊断证据保留在任务过程')
    expect(notice.text()).toContain('不能作为业务质检报告或导出')
    expect(wrapper.text()).not.toContain('尚未产生可信采集')
    expect(wrapper.text()).not.toContain('已通过完成校验')
    expect(wrapper.text()).not.toContain('H5_REPORT_NO_BUSINESS_RESULTS')
    expect(wrapper.find('[data-testid="export-report"]').exists()).toBe(false)
    await notice.get('button').trigger('click')
    expect(mocks.push).toHaveBeenCalledWith({ path: '/tasks/task-outside-first-page', query: mocks.route.query })
    await wrapper.get('[data-testid="rule-detail-alioth_unstarted"]').trigger('click')
    expect(wrapper.get('[data-testid="rule-usage-dialog"]').text()).toContain('首次导航超出任务允许的网站范围，规则尚未执行')
  })

  it('正式报告提示冻结的适用页型与不适用页型', async () => {
    const report = insight('task-outside-first-page', 'Web 冻结适用范围')
    report.summary.report_stage = 'final'
    report.report_availability = 'final'
    report.export_ready = true
    report.page_exploration = {
      web_coverage: {
        applicability_snapshot: {
          required_page_types: ['video_list', 'video_detail'],
          optional_page_types: [],
          not_applicable_page_types: ['content_list', 'content_detail'],
          applicability_mode: 'catalog_declared',
          catalog_version: '2026-09-06',
          catalog_hash: 'a'.repeat(64),
        },
      },
    } as never
    mocks.getReportInsight.mockResolvedValue(report)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('适用范围已冻结')
    expect(wrapper.text()).toContain('必检 视频详情页、视频列表页')
    expect(wrapper.text()).toContain('不适用 内容详情页、内容列表页')
  })

  it('标题行只显示报告状态，任务基本信息在部门平台不展示公司与部门', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView()
    await flushPromises()

    const header = wrapper.get('[data-testid="report-header"]')
    expect(header.text()).toContain('手机银行首页巡检')
    expect(header.get('[data-testid="report-state"]').text()).toBe('需复核')
    expect(header.get('[data-testid="report-state"]').classes()).toContain('tone-warning')
    // 单条规则的结果不当作报告状态；反馈入口已下线
    expect(header.text()).not.toContain('未通过')
    expect(header.text()).not.toContain('提交反馈')
    expect(wrapper.text()).not.toContain('反馈')

    const info = wrapper.get('[data-testid="task-info"]').text()
    expect(info).not.toContain('所属公司')
    expect(info).not.toContain('一部')
    expect(info).toContain('手机银行 Android')
    expect(info).toContain('Android')
    expect(info).toContain('规则集alioth_first_ruleset · v1')
    expect(info).toContain('页面识别数6 个页面')
    expect(info).toContain('规则执行数44 条规则 · 用了 44 次')
    expect(info).toContain('2分15秒')
    expect(info).toContain('报告状态需复核')
    expect(info).not.toContain('执行状态')
    // 没有页面识别明细时不编造页面类型
    expect(info).not.toContain('页面类型')
  })

  it('检测无缺口且人工确认完成时保留成功报告和普通导出文案', async () => {
    const report = reportAllConfirmed()
    report.rule_items = report.rule_items.filter((item) => item.rule_code !== 'alioth_4')
    mocks.getReportInsight.mockResolvedValue(report as never)
    const wrapper = mountView()
    await flushPromises()

    const state = wrapper.get('[data-testid="report-header"] [data-testid="report-state"]')
    expect(state.text()).toBe('成功')
    expect(state.classes()).toContain('tone-success')
    expect(wrapper.get('[data-testid="task-info"]').text()).toContain('报告状态成功')
    expect(wrapper.get('[data-testid="export-report"]').text()).toBe('导出报告')
    expect(wrapper.text()).not.toContain('检测未完整完成')
  })

  it.each([
    { applicability: 'not_applicable', reason_code: 'AGGREGATE_RULE_SCOPE_NOT_APPLICABLE', status: 'not_applicable' },
    { applicability: 'out_of_scope', reason_code: 'inspection_scope_excluded', status: 'coverage_gap' },
  ] as const)('当前可信范围处置 $applicability 不被旧未执行占位降成复核报告', async ({ applicability, reason_code, status }) => {
    const audit = ruleItem({ rule_code: 'scope-rule', execution_id: 'old-startup-audit',
      execution_status: 'skipped', execution_reason: 'exploration_not_started',
      machine_display_status: 'not_executed', machine_display_status_label: '未执行', display_status: 'other',
      item_type: 'execution', inventory_source: 'frozen', applicability, reason_code,
      result_summary: '旧轮次未启动原始留痕' })
    const disposition = ruleItem({ rule_code: 'scope-rule', execution_id: null, execution_status: null,
      item_type: 'candidate', inventory_source: 'frozen', applicability, reason_code,
      machine_display_status: status, display_status: status, result_summary: '当前范围核对完成' })
    const report = reportAllConfirmed()
    const original = structuredClone(audit)
    mocks.getReportInsight.mockResolvedValue({ ...report, rule_items: [audit, disposition], confirm_items: [],
      summary: { ...report.summary, target_platform: 'app', total_rules: 0, ruleset_rule_total: 1,
        rule_inventory_status: 'frozen', rule_inventory_total: 1,
        rule_inventory_complete: true, rule_inventory_pending_rules: 0 },
    } as never)
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.get('[data-testid="report-state"]').text()).toBe('成功')
    expect(wrapper.get('[data-testid="export-report"]').text()).toBe('导出报告')
    expect(wrapper.get('[data-testid="tile-used"] strong').text()).toBe('0')
    expect(wrapper.text()).toContain('已通过完成校验，报告内容已固定')
    expect(wrapper.text()).not.toContain('本次仍有规则未进行实际判定')
    expect(audit).toEqual(original)
  })

  it.each([
    { status: 'skipped', machine: 'not_executed', reason: 'exploration_not_started', inventory: 'unavailable' },
    { status: 'evidence_insufficient', machine: 'evidence_insufficient', reason: null, inventory: 'frozen' },
  ] as const)('未验证的范围处置或真实判定缺证仍需复核：$machine/$inventory', async ({ status, machine, reason, inventory }) => {
    const audit = ruleItem({ rule_code: 'scope-rule', execution_status: status,
      execution_reason: reason, machine_display_status: machine, display_status: 'other',
      item_type: 'execution', inventory_source: inventory, applicability: 'not_applicable' })
    const report = reportAllConfirmed()
    mocks.getReportInsight.mockResolvedValue({ ...report, rule_items: [audit], confirm_items: [],
      summary: { ...report.summary, target_platform: 'app', rule_inventory_complete: true },
    } as never)
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.get('[data-testid="report-state"]').text()).toBe('需复核')
    expect(wrapper.get('[data-testid="export-report"]').text()).toBe('导出复核报告')
  })

  it('管理后台的任务基本信息展示所属公司与部门', async () => {
    mocks.route = {
      path: '/admin/reports/task-outside-first-page',
      params: { taskId: 'task-outside-first-page' },
      query: {},
    }
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView()
    await flushPromises()

    const info = wrapper.get('[data-testid="task-info"]').text()
    expect(info).toContain('所属公司上海分公司')
    expect(info).toContain('部门一部')
  })

  it('页面类型取本次识别到的业务页面类型，超过三类时合并展示', async () => {
    const report = reportWithRules()
    mocks.getReportInsight.mockResolvedValue({
      ...report,
      page_exploration: {
        coverage: {
          catalog_observation: {
            business_observed_page_types: ['home', 'video_list', 'video_detail', 'content_list'],
          },
        },
      },
    } as never)
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="task-info"]').text()).toContain(
      '页面类型首页、视频列表页、视频详情页等 4 类',
    )
  })

  it('规则结果汇总：规则总数、实际使用和六类结果都按条数，每个瓦片都有「!」，不再有通过率和质检率', async () => {
    const report = reportWithRules()
    // 新建任务时规则集里启用 50 条，这次实际判到 45 条
    mocks.getReportInsight.mockResolvedValue({
      ...report,
      summary: { ...report.summary, ruleset_rule_total: 50 },
    } as never)
    const wrapper = mountView()
    await flushPromises()

    const summary = wrapper.get('[data-testid="result-summary"]')
    expect(summary.text()).toContain('规则结果汇总')
    expect(summary.text()).toContain('按规则条数算，一条规则只算一次；判了多次时只要有一次未通过就算未通过')
    expect(summary.text()).not.toContain('质检结果汇总')
    const keys = ['total', 'used', 'not_applicable', 'passed', 'failed', 'coverage_gap', 'detect_error', 'other']
    expect(summary.findAll('[data-testid^="tile-"]').map((tile) => tile.attributes('data-testid'))).toEqual(
      keys.map((key) => `tile-${key}`),
    )
    const tileLabel = (key: string) => wrapper.get(`[data-testid="tile-${key}"] .tile-head span`).text()
    const tileValue = (key: string) => wrapper.get(`[data-testid="tile-${key}"] strong`).text()
    expect(keys.map(tileLabel)).toEqual([
      '规则总数',
      '实际使用',
      '本产品不适用',
      '通过',
      '未通过',
      '计划外未覆盖',
      '检测异常',
      '其他',
    ])
    // 45 条规则：3 条机器未通过 + 1 条人工确认为未通过、1 条计划外未覆盖、1 条其他、39 条通过
    expect(keys.map(tileValue)).toEqual(['50', '44', '0', '39', '4', '1', '0', '1'])
    expect(wrapper.find('[data-testid="tile-qc_rate"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="tile-pass_rate"]').exists()).toBe(false)
    expect(summary.text()).not.toContain('通过率')
    expect(summary.text()).not.toContain('质检率')
    expect(wrapper.get('[data-testid="tile-other"]').classes()).toContain('tile-violet')

    // 八个瓦片都有「!」
    for (const key of keys) {
      expect(wrapper.find(`[data-testid="status-help-${key}"]`).exists()).toBe(true)
    }

    expect(wrapper.find('[data-testid="issue-categories"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('问题分类统计')
    expect(wrapper.text()).not.toContain('质检结果状态说明')
    expect(wrapper.text()).not.toContain('底线规则')
  })

  it('六类瓦片的「!」解释状态，说明弹窗保留规则清单，业务复核可定位人工判定', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView({ attach: true })
    await flushPromises()

    const categoryDialog = () => wrapper.get('[data-testid="category-dialog"]')
    const categoryRows = () =>
      wrapper.findAll('[data-testid^="category-rule-"]').map((item) => item.attributes('data-testid'))

    // 未通过：3 条机器未通过 + 1 条人工确认为未通过，一条规则只列一次
    await wrapper.get('[data-testid="status-help-failed"]').trigger('click')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('规则检查未通过')
    expect(wrapper.find('[data-testid="category-dialog"]').exists()).toBe(false)
    await wrapper.get('[data-testid="summary-view-rules"]').trigger('click')
    expect(categoryDialog().text()).toContain('未通过 · 4 条规则')
    // 排序同规则列表：严重级别在前，同级按名称
    expect(categoryRows()).toEqual([
      'category-rule-alioth_1',
      'category-rule-alioth_2',
      'category-rule-alioth_3',
      'category-rule-alioth_6',
    ])
    // 「!」用了 click.stop，表格没有被筛选
    expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('共 45 条')
    await categoryDialog().get('button[aria-label="关闭"]').trigger('click')
    expect(wrapper.find('[data-testid="category-dialog"]').exists()).toBe(false)

    // 没有规则的一类给空状态
    await wrapper.get('[data-testid="status-help-detect_error"]').trigger('click')
    await wrapper.get('[data-testid="summary-view-rules"]').trigger('click')
    expect(categoryDialog().text()).toContain('检测异常 · 0 条规则')
    expect(wrapper.get('[data-testid="category-empty"]').text()).toBe('这一类没有规则')
    await categoryDialog().get('button[aria-label="关闭"]').trigger('click')

    // 计划外未覆盖：清单里点「查看详情」打开规则详情（二级弹窗），清单还在
    await wrapper.get('[data-testid="status-help-coverage_gap"]').trigger('click')
    await wrapper.get('[data-testid="summary-view-rules"]').trigger('click')
    const gapRow = categoryDialog().get('[data-testid="category-rule-alioth_4"]')
    expect(gapRow.text()).toContain('播放器需支持倍速')
    expect(gapRow.text()).toContain('0 次')
    expect(gapRow.text()).toContain('未形成结论')
    await categoryDialog().get('[data-testid="category-open-alioth_4"]').trigger('click')
    expect(wrapper.get('[data-testid="rule-usage-summary"]').text()).toContain('未进行规则判定')
    expect(wrapper.find('[data-testid="category-dialog"]').exists()).toBe(true)

    expect(wrapper.find('[data-testid="rule-usage-judge-exec-alioth_4"]').exists()).toBe(false)
    await categoryDialog().get('button[aria-label="关闭"]').trigger('click')
    await wrapper.get('[data-testid="status-help-other"]').trigger('click')
    await wrapper.get('[data-testid="summary-view-rules"]').trigger('click')
    await categoryDialog().get('[data-testid="category-open-alioth_5"]').trigger('click')
    // 业务复核：两层弹窗收起，再定位人工确认下拉。
    await wrapper.get('[data-testid="rule-usage-judge-exec-alioth_5"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="rule-usage-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="category-dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('[data-testid="confirm-select-alioth_5"]').element)
  })

  it('规则总数、实际使用的「!」是说明；点这两个瓦片不筛选，点六类瓦片按该类结果筛选', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="status-help"]').exists()).toBe(false)
    await wrapper.get('[data-testid="status-help-total"]').trigger('click')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('规则总数 · 说明')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('新建任务时所选规则集里启用的规则条数')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('每条所选规则都应进行适用性核对')
    await wrapper.get('[data-testid="status-help-used"]').trigger('click')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('实际使用 · 说明')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('一条规则不管判了几次都只算一条')
    expect(wrapper.get('[data-testid="status-help"]').text()).toContain('真实判定后证据不足仍计为使用')
    expect(wrapper.get('[data-testid="status-help"]').text()).not.toContain('一般是任务没走到')

    // 规则总数、实际使用不是结果分类，点了不筛选
    for (const key of ['total', 'used']) {
      await wrapper.get(`[data-testid="tile-${key}"]`).trigger('click')
      expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('共 45 条')
    }

    await wrapper.get('[data-testid="tile-coverage_gap"]').trigger('click')
    const table = wrapper.get('[data-testid="rule-table"]')
    expect(table.text()).toContain('共 1 条')
    expect(table.text()).toContain('播放器需支持倍速')
    await wrapper.get('[data-testid="tile-coverage_gap"]').trigger('click')
    expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('共 45 条')
  })

  it('规则总数为 73、实际使用为 20 时，已有说明区明确有记录占比与质检率不是同一指标', async () => {
    const report = reportWithRules()
    const items = report.rule_items.filter((item) => !item.is_bootstrap).slice(0, 20)
    mocks.getReportInsight.mockResolvedValue({
      ...report,
      rule_items: items,
      summary: { ...report.summary, ruleset_rule_total: 73 },
    } as never)
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="tile-total"] strong').text()).toBe('73')
    expect(wrapper.get('[data-testid="tile-used"] strong').text()).toBe('20')
    const accounting = wrapper.get('[data-testid="summary-reconciliation"]').text()
    expect(accounting).toContain('规则总数 73 = 实际使用 20 + 未实际使用 53')
    expect(accounting).toContain('六类处置合计 20 条')
    expect(accounting).toContain('“实际使用”单独统计已实际判定的规则')
    expect(accounting).toContain('不能直接算作“本产品不适用”或“计划外未覆盖”')
    await wrapper.get('[data-testid="status-help-used"]').trigger('click')
    const explanation = wrapper.get('[data-testid="status-help"]').text()
    expect(explanation).toContain('实际使用的规则占规则总数的比例为 20/73（27.4%）')
    expect(explanation).toContain('该比例不是适用范围完成率，也不是质检率')
    expect(explanation).toContain('平台或检查范围筛选、触发条件未出现、页面未覆盖')
    expect(explanation).toContain('原因需查看逐条处置')
  })

  it('完整 73 条清单的六类合计等于总数，53 条候选不混入 343 次执行或人工确认', async () => {
    const report = reportWithRules()
    const executions = Array.from({ length: 343 }, (_, index) => ({
      ...passedRule(`executed-${index % 20}`), execution_id: `execution-${index}`,
    }))
    const candidates = Array.from({ length: 53 }, (_, index) => ruleItem({
      rule_code: `candidate-${index}`, execution_id: null, item_type: 'candidate',
      applicability: index < 2 ? 'not_applicable' : index < 15 ? 'out_of_scope' : 'unproven',
      display_status: index < 2 ? 'not_applicable' : index < 15 ? 'coverage_gap' : 'other',
      display_status_label: index < 2 ? '本平台不适用' : index < 15 ? '计划外未覆盖' : '其他',
      machine_display_status: index < 2 ? 'not_applicable' : index < 15 ? 'coverage_gap' : 'not_executed',
      execution_status: null, confirmable: false, confirm_status: null,
      human_verdict: null, result_summary: '已逐条核对目标平台或未执行原因',
    }))
    mocks.getReportInsight.mockResolvedValue({
      ...report, rule_items: [...executions, ...candidates], confirm_items: [],
      summary: { ...report.summary, ruleset_rule_total: 73, rule_inventory_total: 73,
        rule_inventory_complete: false, rule_inventory_pending_rules: 38 },
    } as never)
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.get('[data-testid="tile-total"] strong').text()).toBe('73')
    expect(wrapper.get('[data-testid="tile-used"] strong').text()).toBe('20')
    expect(wrapper.get('[data-testid="summary-reconciliation"]').text()).toContain('六类处置合计 73 条')
    expect(wrapper.get('[data-testid="summary-reconciliation"]').text()).toContain('核对处置不计为 AI 判定次数')
    expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('用了 343 次')
    await wrapper.get('[data-testid="tile-not_applicable"]').trigger('click')
    const row = wrapper.get('[data-testid="rule-row-candidate-0"]')
    expect(row.get('[data-testid="rule-rate"]').text()).toContain('0/0 次')
    expect(row.get('[data-testid="ai-result"]').text()).toBe('未进行 AI 判定')
    expect(row.get('[data-testid="execution-state"]').text()).toContain('适用性已核对')
    expect(row.find('[data-testid="rule-judge-candidate-0"]').exists()).toBe(false)
    await row.get('[data-testid="rule-detail-candidate-0"]').trigger('click')
    expect(wrapper.get('[data-testid="rule-usage-dialog"]').text()).toContain('未进行规则判定')
  })

  it('规则执行结果列表：新列口径、按最终结果筛选、人工判定只出现在需确认的规则上', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView()
    await flushPromises()

    const table = wrapper.get('[data-testid="rule-table"]')
    expect(table.findAll('thead th').map((th) => th.text())).toEqual([
      '规则名称',
      '所属页面',
      '所属规则包',
      '质检率',
      '执行状态',
      'AI 判定',
      '最终结果',
      '人工确认',
      '截图证据',
      '操作',
    ])
    expect(wrapper.find('[data-testid="filter-handle"]').exists()).toBe(false)
    expect(table.text()).toContain('共 45 条规则 · 用了 44 次')
    // 接口没带规则集时整张表算一组、自动展开；每组先列 10 条，其余点「展开全部」
    expect(wrapper.get('[data-testid="phase-toggle-__ungrouped__"]').text()).toContain('全部规则')
    expect(wrapper.find('[data-testid="rule-pager"]').exists()).toBe(false)
    expect(table.findAll('tbody tr')).toHaveLength(10)
    await wrapper.get('[data-testid="phase-more-__ungrouped__"]').trigger('click')
    expect(table.findAll('tbody tr')).toHaveLength(45)
    // 未通过的排在最前
    expect(table.findAll('tbody tr')[0].text()).toContain('首页弹窗必须可关闭')

    const cell = (ruleCode: string, testid: string) => row(wrapper, ruleCode).get(`[data-testid="${testid}"]`)
    // 机器直接未通过：直接生效，不需要人工确认
    expect(cell('alioth_1', 'ai-result').text()).toBe('未通过')
    expect(cell('alioth_1', 'final-result').text()).toBe('未通过')
    expect(cell('alioth_1', 'confirm-status').text()).toBe('-')
    expect(row(wrapper, 'alioth_1').find('[data-testid="row-judge-alioth_1"]').exists()).toBe(false)
    expect(row(wrapper, 'alioth_1').text()).toContain('P0')
    expect(row(wrapper, 'alioth_1').text()).toContain('广告规则包')
    // 编号写在规则名下面；触发条件挪进详情弹窗
    expect(row(wrapper, 'alioth_1').text()).toContain('alioth_1')
    expect(row(wrapper, 'alioth_1').text()).not.toContain('进入首页后 3 秒内')
    expect(cell('alioth_1', 'rule-rate').text()).toContain('100%')
    expect(cell('alioth_1', 'rule-rate').text()).toContain('1/1 次')
    expect(cell('alioth_5', 'rule-rate').text()).toContain('0/1 次')
    // 其他：带子状态、紫色，待人工确认
    expect(cell('alioth_5', 'ai-result').text()).toBe('其他 · 待人工复核')
    expect(cell('alioth_5', 'ai-result').classes()).toContain('tone-violet')
    expect(cell('alioth_5', 'final-result').text()).toBe('待人工确认')
    expect(cell('alioth_5', 'confirm-status').text()).toBe('待确认')
    expect(row(wrapper, 'alioth_5').find('[data-testid="row-judge-alioth_5"]').exists()).toBe(true)
    // 检测异常已被人工确认为未通过：最终结果跟人工判定走
    expect(cell('alioth_6', 'ai-result').text()).toBe('其他 · 待人工复核')
    expect(cell('alioth_6', 'ai-result').classes()).toContain('tone-violet')
    expect(cell('alioth_6', 'final-result').text()).toBe('未通过')
    expect(cell('alioth_6', 'final-result').classes()).toContain('tone-danger')
    expect(cell('alioth_6', 'confirm-status').text()).toBe('已确认')
    expect(row(wrapper, 'alioth_6').find('[data-testid="row-judge-alioth_6"]').exists()).toBe(true)
    // 通过的规则没有人工判定
    const judgeButtons = wrapper.findAll('[data-testid^="row-judge-"]').map((button) => button.attributes('data-testid'))
    expect(judgeButtons.sort()).toEqual(['row-judge-alioth_5', 'row-judge-alioth_6'])

    // 结果筛选和汇总瓦片同一口径（最终结果）：已被人工改成未通过的检测异常归到未通过，条数与瓦片一致
    await wrapper.get('[data-testid="filter-result"]').setValue('detect_error')
    expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('共 0 条')
    await wrapper.get('[data-testid="filter-result"]').setValue('failed')
    const failedRows = wrapper.get('[data-testid="rule-table"]')
    expect(failedRows.text()).toContain('共 4 条')
    expect(failedRows.text()).toContain('页面截图采集')
    await wrapper.get('[data-testid="filter-result"]').setValue('other')
    expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('共 1 条')
    expect(wrapper.get('[data-testid="rule-table"]').text()).toContain('图片未使用 CDN')
  })

  it('「查看详情」弹窗讲清规则查什么、用了几次，每次的机器说明、执行态与把握度', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="rule-usage-dialog"]').exists()).toBe(false)
    const detailButton = wrapper.get('[data-testid="rule-detail-alioth_2"]')
    expect(detailButton.text()).toBe('查看详情')
    await detailButton.trigger('click')
    const detail = wrapper.get('[data-testid="rule-usage-dialog"]').text()
    expect(detail).toContain('触发条件：进入首页后 3 秒内')
    expect(detail).toContain('一共用了 1 次：未通过 1 次。质检率 100%（1 次得出结论）。')
    expect(detail).toContain('第 1 次 · 首页')
    expect(detail).toContain('机器判定说明文本扫描发现疑似错别字')
    expect(detail).toContain('识别详情「登陆」应为「登录」')
    expect(detail).toContain('原始执行态未通过 · 风险提示')
    expect(detail).toContain('识别把握度92%')
    expect(detail).toContain('这一次没有截图证据')
    expect(detail).not.toContain('置信度')

    await wrapper.get('[data-testid="rule-detail-alioth_5"]').trigger('click')
    const otherDetail = wrapper.get('[data-testid="rule-usage-dialog"]').text()
    expect(otherDetail).toContain('原始执行态其他 · 待人工复核')
    expect(otherDetail).toContain('识别把握度无')
    expect(otherDetail).toContain('人工确认：待确认')
  })

  it('不适用的那一次写明不适用原因', async () => {
    const report = reportWithRules()
    const notApplicable = ruleItem({
      rule_code: 'alioth_24',
      rule_name: '启动广告虚假跳过按钮检查',
      display_status: 'not_applicable',
      display_status_label: '本产品不适用',
      machine_display_status: 'not_applicable',
      machine_display_status_label: '确认不适用',
      execution_status: 'skipped',
      result_summary: '当前页面或检查流程未出现该规则的适用目标，已跳过',
      problem_desc: '当前检查流程直接进入首页，未出现启动广告页',
    })
    mocks.getReportInsight.mockResolvedValue({ ...report, rule_items: [notApplicable] } as never)
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="rule-detail-alioth_24"]').trigger('click')
    const detail = wrapper.get('[data-testid="rule-usage-dialog"]').text()
    expect(detail).toContain('不适用原因当前检查流程直接进入首页，未出现启动广告页')
    expect(detail).not.toContain('识别详情')
  })

  it('按几期规则集分组：手风琴一次展开一期，期和规则都带质检率', async () => {
    const phase1 = { ruleset_code: 'alioth_first_ruleset', ruleset_name: '1期质检规则集' }
    const phase3 = { ruleset_code: 'alioth_ruisheng_ruleset', ruleset_name: '3期瑞升质检规则集' }
    const pendingOther = {
      display_status: 'other',
      display_status_label: '其他',
      machine_display_status: 'evidence_insufficient',
      machine_display_status_label: '证据不足',
      execution_status: 'evidence_insufficient',
      confirmable: true,
      confirm_status: 'pending',
    } as const
    const items = [
      ruleItem({ rule_code: 'alioth_17', rule_name: '广告内容与落地页一致性检查', ...phase1, execution_id: 'exec-17-splash', bound_page_type_name: '启动 / 开屏广告' }),
      { ...passedRule('alioth_17'), rule_name: '广告内容与落地页一致性检查', ...phase1, execution_id: 'exec-17-list', bound_page_type_name: '视频列表页' },
      ruleItem({ rule_code: 'alioth_17', rule_name: '广告内容与落地页一致性检查', ...phase1, ...pendingOther, execution_id: 'exec-17-detail', bound_page_type_name: '内容详情页' }),
      { ...passedRule('alioth_16'), ...phase1 },
      ruleItem({ rule_code: 'alioth_ops_1', ...phase3, ...pendingOther, execution_id: 'exec-ops-1-home' }),
      ruleItem({ rule_code: 'alioth_ops_1', ...phase3, ...pendingOther, execution_id: 'exec-ops-1-list', bound_page_type_name: '视频列表页' }),
    ]
    mocks.getReportInsight.mockResolvedValue({
      ...reportWithRules(),
      rule_items: items,
      confirm_items: items.filter((item) => item.confirmable),
      distribution: [],
    } as never)
    const wrapper = mountView()
    await flushPromises()

    const table = wrapper.get('[data-testid="rule-table"]')
    expect(table.text()).toContain('共 3 条规则 · 用了 6 次')
    const heads = wrapper.findAll('[data-testid^="phase-toggle-"]').map((head) => head.text())
    expect(heads).toHaveLength(2)
    expect(heads[0]).toContain('1期质检规则集')
    expect(heads[0]).toContain('2 条规则 · 用了 4 次')
    expect(heads[0]).toContain('质检率 75%')
    expect(heads[0]).toContain('未通过 1 条')
    expect(heads[0]).toContain('待人工确认 1 条')
    expect(heads[1]).toContain('3期瑞升质检规则集')
    expect(heads[1]).toContain('质检率 0%')
    // 多期时默认都收着，点一期展开
    expect(wrapper.findAll('[data-testid^="rule-row-"]')).toHaveLength(0)

    await wrapper.get('[data-testid="phase-toggle-alioth_first_ruleset"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="rule-row-"]').map((item) => item.attributes('data-testid'))).toEqual([
      'rule-row-alioth_17',
      'rule-row-alioth_16',
    ])
    const multi = row(wrapper, 'alioth_17')
    expect(multi.text()).toContain('启动 / 开屏广告等 3 个页面')
    expect(multi.get('[data-testid="rule-rate"]').text()).toContain('66.7%')
    expect(multi.get('[data-testid="rule-rate"]').text()).toContain('2/3 次')
    expect(multi.get('[data-testid="final-result"]').text()).toBe('未通过')
    expect(multi.get('[data-testid="confirm-status"]').text()).toBe('待确认 1 次')

    // 手风琴：展开 3 期时 1 期收起；再点一次全收起
    await wrapper.get('[data-testid="phase-toggle-alioth_ruisheng_ruleset"]').trigger('click')
    expect(wrapper.find('[data-testid="rule-row-alioth_17"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-row-alioth_ops_1"]').exists()).toBe(true)
    await wrapper.get('[data-testid="phase-toggle-alioth_ruisheng_ruleset"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="rule-row-"]')).toHaveLength(0)

    // 按结果筛选：只留有这个结果的期，并自动展开；次数是筛出的规则一共用了几次
    await wrapper.get('[data-testid="filter-result"]').setValue('failed')
    expect(wrapper.findAll('[data-testid^="phase-toggle-"]')).toHaveLength(1)
    expect(table.text()).toContain('共 1 条规则 · 用了 3 次')
    expect(wrapper.find('[data-testid="rule-row-alioth_17"]').exists()).toBe(true)
  })

  it('规则结果汇总和规则执行结果列表同一口径：按条数数规则，点瓦片筛出的条数就是瓦片上的数', async () => {
    const phase1 = { ruleset_code: 'alioth_first_ruleset', ruleset_name: '1期质检规则集' }
    const phase3 = { ruleset_code: 'alioth_ruisheng_ruleset', ruleset_name: '3期瑞升质检规则集' }
    const pendingOther = {
      display_status: 'other',
      display_status_label: '其他',
      machine_display_status: 'evidence_insufficient',
      machine_display_status_label: '证据不足',
      execution_status: 'evidence_insufficient',
      confirmable: true,
      confirm_status: 'pending',
    } as const
    const items = [
      ruleItem({ rule_code: 'alioth_17', ...phase1, execution_id: 'exec-17-splash' }),
      { ...passedRule('alioth_17'), ...phase1, execution_id: 'exec-17-list' },
      ruleItem({ rule_code: 'alioth_17', ...phase1, ...pendingOther, execution_id: 'exec-17-detail' }),
      { ...passedRule('alioth_16'), ...phase1 },
      ruleItem({ rule_code: 'alioth_ops_1', ...phase3, ...pendingOther, execution_id: 'exec-ops-1-home' }),
      ruleItem({ rule_code: 'alioth_ops_1', ...phase3, ...pendingOther, execution_id: 'exec-ops-1-list' }),
    ]
    // 接口按次数给的汇总（6 次、通过率 50%）不再用于瓦片
    mocks.getReportInsight.mockResolvedValue({
      ...reportWithRules(),
      summary: { ...reportWithRules().summary, total_rules: items.length },
      pass_rate: 50,
      distribution: [
        { status: 'passed', label: '通过', count: 2, percent: 33.3 },
        { status: 'failed', label: '未通过', count: 1, percent: 16.7 },
        { status: 'other', label: '其他', count: 3, percent: 50 },
      ],
      rule_items: items,
      confirm_items: items.filter((item) => item.confirmable),
    } as never)
    const wrapper = mountView()
    await flushPromises()

    const tileValue = (key: string) => wrapper.get(`[data-testid="tile-${key}"] strong`).text()
    const table = () => wrapper.get('[data-testid="rule-table"]')
    // 接口没给规则总数（旧后端）时按实际使用的条数显示
    expect(tileValue('total')).toBe('3')
    expect(tileValue('used')).toBe('3')
    expect(wrapper.find('[data-testid="summary-rule-accounting"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="summary-reconciliation"]').text()).toContain('六类处置合计 3 条')
    expect(table().text()).toContain('共 3 条规则 · 用了 6 次')
    expect(wrapper.get('[data-testid="task-info"]').text()).toContain('规则执行数3 条规则 · 用了 6 次')
    // 用了 3 次的 alioth_17 有一次未通过：整条算未通过，和 1 期标题「未通过 1 条」一致
    expect(
      ['passed', 'failed', 'not_applicable', 'coverage_gap', 'detect_error', 'other'].map(tileValue),
    ).toEqual(['1', '1', '0', '0', '0', '1'])

    // 点「通过」：只列整条通过的 alioth_16，有过一次通过但整条未通过的 alioth_17 不列
    await wrapper.get('[data-testid="tile-passed"]').trigger('click')
    expect(table().text()).toContain('共 1 条规则 · 用了 1 次')
    expect(wrapper.findAll('[data-testid^="rule-row-"]').map((item) => item.attributes('data-testid'))).toEqual([
      'rule-row-alioth_16',
    ])
    await wrapper.get('[data-testid="tile-failed"]').trigger('click')
    expect(table().text()).toContain('共 1 条规则 · 用了 3 次')
    expect(wrapper.findAll('[data-testid^="rule-row-"]').map((item) => item.attributes('data-testid'))).toEqual([
      'rule-row-alioth_17',
    ])
    expect(row(wrapper, 'alioth_17').get('[data-testid="final-result"]').text()).toBe('未通过')
  })

  it('规则详情弹窗列出每一次的页面、判定和截图；去人工判定先关弹窗再定位', async () => {
    const phase1 = { ruleset_code: 'alioth_first_ruleset', ruleset_name: '1期质检规则集' }
    const items = [
      ruleItem({
        rule_code: 'alioth_17',
        rule_name: '广告内容与落地页一致性检查',
        ...phase1,
        check_point: '开屏广告点进去的落地页要和广告内容一致',
        execution_id: 'exec-17-splash',
        bound_page_type_name: '启动 / 开屏广告',
        evidence_artifact_ids: ['art-1'],
      }),
      {
        ...passedRule('alioth_17'),
        rule_name: '广告内容与落地页一致性检查',
        ...phase1,
        execution_id: 'exec-17-list',
        bound_page_type_name: '视频列表页',
        evidence_artifact_ids: ['art-2', 'art-3'],
      },
      ruleItem({
        rule_code: 'alioth_17',
        rule_name: '广告内容与落地页一致性检查',
        ...phase1,
        execution_id: 'exec-17-detail',
        bound_page_type_name: '内容详情页',
        display_status: 'other',
        display_status_label: '其他',
        machine_display_status: 'manual_review',
        machine_display_status_label: '待人工复核',
        execution_status: 'manual_review',
        result_summary: '落地页被弹窗挡住，看不到主体内容',
        confirmable: true,
        confirm_status: 'pending',
      }),
    ]
    mocks.getReportInsight.mockResolvedValue({
      ...reportWithRules(),
      rule_items: items,
      confirm_items: items.filter((item) => item.confirmable),
    } as never)
    mocks.getTaskScreenshots.mockResolvedValue(screenshotsFor(['art-1', 'art-2', 'art-3']))
    const wrapper = mountView({ attach: true })
    await flushPromises()

    // 只有一期时自动展开；行上的缩略图是这条规则所有次数的截图
    expect(row(wrapper, 'alioth_17').get('.evidence-thumb').text()).toContain('+2')
    await wrapper.get('[data-testid="rule-detail-alioth_17"]').trigger('click')
    const dialog = () => wrapper.get('[data-testid="rule-usage-dialog"]')
    expect(dialog().text()).toContain('这条规则查什么')
    expect(dialog().text()).toContain('开屏广告点进去的落地页要和广告内容一致')
    expect(dialog().text()).toContain('alioth_17 · 1期质检规则集 · 广告规则包')
    expect(dialog().get('[data-testid="rule-usage-summary"]').text()).toBe(
      '一共用了 3 次：未通过 1 次、待人工确认 1 次、通过 1 次。质检率 66.7%（2 次得出结论）。',
    )
    expect(dialog().findAll('.usage-card-head strong').map((head) => head.text())).toEqual([
      '第 1 次 · 启动 / 开屏广告',
      '第 2 次 · 视频列表页',
      '第 3 次 · 内容详情页',
    ])
    expect(dialog().get('[data-testid="rule-usage-exec-17-detail"]').text()).toContain(
      '落地页被弹窗挡住，看不到主体内容',
    )

    // 点某一次的第二张截图：大图从那一张开始，只翻这一次的截图
    await dialog().get('[data-testid="rule-usage-shot-art-3"]').trigger('click')
    const viewer = wrapper.get('[data-testid="shot-viewer"]')
    expect(viewer.text()).toContain('截图证据 · 广告内容与落地页一致性检查')
    expect(viewer.get('[data-testid="viewer-count"]').text()).toBe('2 / 2')
    await viewer.findAll('button').find((button) => button.text() === '关闭')?.trigger('click')

    // 去人工判定：弹窗收起后定位到人工确认区里那一次的判定下拉
    await dialog().get('[data-testid="rule-usage-judge-exec-17-detail"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="rule-usage-dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('[data-testid="confirm-select-alioth_17"]').element)
  })

  it('人工确认规则区：列出需确认的规则，选完判定立即提交并刷新', async () => {
    mocks.getReportInsight.mockResolvedValueOnce(reportWithRules() as never)
    mocks.confirmReportRule.mockResolvedValueOnce({ execution_id: 'exec-alioth_5', human_verdict: 'ignored' })
    const wrapper = mountView()
    await flushPromises()

    const section = wrapper.get('[data-testid="confirm-section"]')
    expect(section.text()).toContain('人工确认规则')
    expect(section.text()).toContain(
      '仅已执行且业务结论待复核的规则可人工确认。',
    )
    expect(section.get('[data-testid="confirm-progress"]').text()).toBe('待确认 1 条')
    expect(section.find('[data-testid="confirm-item-alioth_4"]').exists()).toBe(false)
    expect(section.get('[data-testid="confirm-item-alioth_5"]').text()).toContain(
      'alioth_5 · 首页 · 其他 · 待人工复核',
    )
    // 机器说明之外补一行原因，说清为什么还要人工看
    expect(section.get('[data-testid="confirm-item-alioth_5"]').text()).toContain(
      '原因：首页弹窗遮挡主体内容',
    )
    // 机器直接未通过的规则不进人工确认
    expect(section.find('[data-testid="confirm-item-alioth_1"]').exists()).toBe(false)

    const select = section.get('[data-testid="confirm-select-alioth_5"]')
    expect(select.findAll('option').map((option) => option.text())).toEqual([
      '请选择人工判定',
      '通过',
      '未通过',
      '本产品不适用',
    ])
    expect((select.element as HTMLSelectElement).value).toBe('')
    expect(
      (section.get('[data-testid="confirm-select-alioth_6"]').element as HTMLSelectElement).value,
    ).toBe('fail')

    mocks.getReportInsight.mockResolvedValueOnce(reportAllConfirmed() as never)
    await select.setValue('ignored')
    await flushPromises()

    expect(mocks.confirmReportRule).toHaveBeenCalledWith('task-outside-first-page', 'exec-alioth_5', {
      verdict: 'ignored',
    })
    expect(mocks.message.success).toHaveBeenCalled()
    expect(mocks.getReportInsight).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="confirm-progress"]').text()).toBe('已全部确认，可生成导出报告')
    expect(
      (wrapper.get('[data-testid="confirm-select-alioth_5"]').element as HTMLSelectElement).value,
    ).toBe('ignored')
  })

  it('同一条规则落在多个页面时合成一组，整组判定逐条提交', async () => {
    const report = reportWithRules()
    const grouped = ['首页', '内容列表页', '视频列表页'].map((pageName, index) =>
      ruleItem({
        rule_code: 'alioth_20',
        rule_name: '弹窗广告关闭行为检查',
        execution_id: `exec-group-${index}`,
        bound_page_type_name: pageName,
        display_status: 'other',
        display_status_label: '其他',
        machine_display_status: 'manual_review',
        machine_display_status_label: '待人工复核',
        execution_status: 'manual_review',
        result_summary: '已完成检测，业务结论需要人工复核',
        confirmable: true,
        confirm_status: 'pending',
      }),
    )
    mocks.getReportInsight.mockResolvedValue({
      ...report,
      rule_items: [...report.rule_items, ...grouped],
      confirm_items: [...report.confirm_items, ...grouped],
    } as never)
    mocks.confirmReportRule.mockResolvedValue({ execution_id: 'exec-group-0', human_verdict: 'ignored' })
    const wrapper = mountView()
    await flushPromises()

    const group = wrapper.get('[data-testid="confirm-group-alioth_20"]')
    expect(group.text()).toContain('3 条')
    expect(group.text()).toContain('首页、内容列表页、视频列表页')
    expect(group.text()).toContain('待确认 3 条')
    // 收着的组不逐条铺开
    expect(wrapper.findAll('[data-testid="confirm-item-alioth_20"]')).toHaveLength(0)

    await wrapper.get('[data-testid="confirm-group-toggle-alioth_20"]').trigger('click')
    expect(wrapper.findAll('[data-testid="confirm-item-alioth_20"]')).toHaveLength(3)

    await wrapper.get('[data-testid="confirm-group-select-alioth_20"]').setValue('ignored')
    await flushPromises()
    for (const executionId of ['exec-group-0', 'exec-group-1', 'exec-group-2']) {
      expect(mocks.confirmReportRule).toHaveBeenCalledWith('task-outside-first-page', executionId, {
        verdict: 'ignored',
      })
    }
    expect(mocks.message.success).toHaveBeenCalledWith(
      '已提交人工判定：弹窗广告关闭行为检查 · 本产品不适用（3 条）',
    )
  })

  it('人工判定提交失败时提示原因并撤回选择', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    mocks.confirmReportRule.mockRejectedValueOnce(
      new AliothHttpError(409, '规则 alioth_5 当前为「通过」，无需人工确认'),
    )
    const wrapper = mountView()
    await flushPromises()

    const select = wrapper.get('[data-testid="confirm-select-alioth_5"]')
    await select.setValue('pass')
    await flushPromises()

    expect(mocks.message.error).toHaveBeenCalledWith('规则 alioth_5 当前为「通过」，无需人工确认')
    expect((select.element as HTMLSelectElement).value).toBe('')
    expect(mocks.getReportInsight).toHaveBeenCalledTimes(1)

    mocks.confirmReportRule.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await select.setValue('fail')
    await flushPromises()
    expect(mocks.message.error).toHaveBeenLastCalledWith('人工判定提交失败，请稍后重试')
    expect((select.element as HTMLSelectElement).value).toBe('')
  })

  it('规则表「人工判定」定位到人工确认区里这条规则的判定下拉', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView({ attach: true })
    await flushPromises()

    await wrapper.get('[data-testid="row-judge-alioth_5"]').trigger('click')
    expect(document.activeElement).toBe(wrapper.get('[data-testid="confirm-select-alioth_5"]').element)

    await wrapper.get('[data-testid="row-judge-alioth_6"]').trigger('click')
    expect(document.activeElement).toBe(wrapper.get('[data-testid="confirm-select-alioth_6"]').element)
  })

  it('从任务详情带着规则过来时，直接定位到该规则的人工判定', async () => {
    mocks.route = {
      path: '/reports/task-outside-first-page',
      params: { taskId: 'task-outside-first-page' },
      query: { execution: 'exec-alioth_5' },
    }
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView({ attach: true })
    await flushPromises()

    expect(document.activeElement).toBe(wrapper.get('[data-testid="confirm-select-alioth_5"]').element)
    expect(mocks.replace).toHaveBeenCalledWith({ query: {} })
  })

  it('没有需要人工确认的规则时说明清楚', async () => {
    const report = reportWithRules()
    mocks.getReportInsight.mockResolvedValue({
      ...report,
      rule_items: report.rule_items.map((item) => ({ ...item, confirmable: false, confirm_status: null })),
      confirm_items: [],
      confirm_progress: { total: 0, confirmed: 0, pending: 0, completed: true },
    } as never)
    const wrapper = mountView()
    await flushPromises()

    const section = wrapper.get('[data-testid="confirm-section"]')
    expect(section.get('[data-testid="confirm-progress"]').text()).toBe('无需人工确认')
    expect(section.text()).toContain('本报告没有需要人工确认的规则')
    expect(wrapper.find('[data-testid^="row-judge-"]').exists()).toBe(false)
  })

  it('还有待确认规则时导出被拦下并提示先完成人工确认', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    const wrapper = mountView()
    await flushPromises()

    const button = wrapper.get('[data-testid="export-report"]')
    expect(button.attributes('title')).toBe('请先完成人工确认规则，再生成导出报告')
    await button.trigger('click')
    expect(mocks.message.warning).toHaveBeenCalledWith('请先完成人工确认规则，再生成导出报告')
    expect(mocks.downloadReportExport).not.toHaveBeenCalled()
  })

  it('可导出时走后端导出，生成中显示进度文字，结束后提示结果', async () => {
    mocks.getReportInsight.mockResolvedValue(reportAllConfirmed() as never)
    let finishExport: () => void = () => undefined
    mocks.downloadReportExport.mockImplementationOnce(
      () => new Promise<void>((resolve) => {
        finishExport = resolve
      }),
    )
    const wrapper = mountView()
    await flushPromises()

    const button = wrapper.get('[data-testid="export-report"]')
    expect(button.text()).toBe('导出复核报告')
    await button.trigger('click')
    expect(mocks.downloadReportExport).toHaveBeenCalledWith('task-outside-first-page')
    expect(wrapper.get('[data-testid="export-report"]').text()).toBe('正在生成 HTML…')
    expect(wrapper.get('[data-testid="export-report"]').attributes('disabled')).toBeDefined()

    finishExport()
    await flushPromises()
    expect(mocks.message.success).toHaveBeenCalledWith('HTML 报告已生成')
    expect(wrapper.get('[data-testid="export-report"]').text()).toBe('导出复核报告')

    mocks.downloadReportExport.mockRejectedValueOnce(
      new AliothHttpError(409, '仍有 1 条规则待人工确认，暂不能导出报告'),
    )
    await wrapper.get('[data-testid="export-report"]').trigger('click')
    await flushPromises()
    expect(mocks.message.error).toHaveBeenCalledWith('仍有 1 条规则待人工确认，暂不能导出报告')
  })

  it('截图证据列显示首张缩略图与「+N」，大图查看器翻到头不循环', async () => {
    mocks.getReportInsight.mockResolvedValue(reportWithRules() as never)
    mocks.getTaskScreenshots.mockResolvedValue(screenshotsFor(['art-1', 'art-2', 'art-3']))
    const wrapper = mountView()
    await flushPromises()

    const thumb = row(wrapper, 'alioth_1').get('.evidence-thumb')
    expect(thumb.get('img').attributes('src')).toBe('blob:art-1')
    expect(thumb.text()).toContain('+2')
    // 没有截图的规则写「-」
    expect(row(wrapper, 'alioth_2').find('.evidence-thumb').exists()).toBe(false)

    expect(wrapper.find('[data-testid="shot-viewer"]').exists()).toBe(false)
    await thumb.trigger('click')
    const viewer = () => wrapper.get('[data-testid="shot-viewer"]')
    expect(viewer().text()).toContain('截图证据 · 首页弹窗必须可关闭')
    expect(viewer().get('[data-testid="viewer-count"]').text()).toBe('1 / 3')
    expect(viewer().get('[data-testid="viewer-prev"]').attributes('disabled')).toBeDefined()
    expect(viewer().get('[data-testid="viewer-next"]').attributes('disabled')).toBeUndefined()

    await viewer().get('[data-testid="viewer-next"]').trigger('click')
    await viewer().get('[data-testid="viewer-next"]').trigger('click')
    expect(viewer().get('[data-testid="viewer-count"]').text()).toBe('3 / 3')
    expect(viewer().get('[data-testid="viewer-next"]').attributes('disabled')).toBeDefined()
    // 键盘翻页到头也停住
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    await flushPromises()
    expect(viewer().get('[data-testid="viewer-count"]').text()).toBe('3 / 3')

    await viewer().get('[data-testid="viewer-prev"]').trigger('click')
    expect(viewer().get('[data-testid="viewer-count"]').text()).toBe('2 / 3')
    // 缩放与下载仍保留
    expect(viewer().find('[data-testid="viewer-zoom"]').exists()).toBe(true)
    expect(viewer().text()).toContain('下载当前截图')
  })

  it('H5 证据不足规则展示缺失项与证据输入集', async () => {
    const report = reportWithRules()
    report.summary.target_platform = 'h5'
    const item = report.rule_items.find((entry) => entry.rule_code === 'alioth_5')
    expect(item).toBeDefined()
    Object.assign(item!, {
      missing_evidence: ['after_action_screenshot', 'dom_snapshot', 'safe_click_observer_tool'],
      evidence_input_set_id: 'evidence-input-set:h5-preview',
    })
    mocks.getReportInsight.mockResolvedValue(report as never)
    mocks.getTaskScreenshots.mockResolvedValue(screenshotsFor([]))

    const wrapper = mountView()
    await flushPromises()

    const targetRow = row(wrapper, 'alioth_5')
    expect(targetRow.get('[data-testid="evidence-gap"]').text()).toBe('缺少证据')
    await targetRow.get('[data-testid="rule-detail-alioth_5"]').trigger('click')
    const detail = wrapper.get('[data-testid="rule-usage-dialog"]')
    // 广告落地页缺的检测工具也写成人话，不露内部名
    expect(detail.text()).toContain('操作后页面截图、页面结构快照、点击广告前后的页面截图')
    expect(detail.text()).not.toContain('safe_click_observer_tool')
    expect(detail.text()).toContain('evidence-input-set:h5-preview')
  })
})
