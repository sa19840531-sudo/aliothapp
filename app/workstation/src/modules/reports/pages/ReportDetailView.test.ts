import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getReport: vi.fn(),
  getTask: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { taskId: 'task-web' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const passthrough = defineComponent({
    setup(_, { attrs, slots }) {
      return () => h('div', attrs, slots.default?.())
    },
  })
  return {
    NButton: passthrough,
    NIcon: passthrough,
    NSpin: passthrough,
    useMessage: () => ({ error: vi.fn() }),
  }
})

vi.mock('@vicons/ionicons5', async () => {
  const { defineComponent, h } = await import('vue')
  const icon = defineComponent({ render: () => h('span') })
  return {
    ChevronForwardOutline: icon,
    DocumentTextOutline: icon,
    FolderOpenOutline: icon,
    ImageOutline: icon,
  }
})

vi.mock('@/components/PageExplorationPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ render: () => h('div') }) }
})

vi.mock('@/components/status/RuleExecutionStatusTag.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ render: () => h('span') }) }
})

vi.mock('@/components/status/SeverityBadge.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ render: () => h('span') }) }
})

vi.mock('@/composables/useEvidenceFileUrls', () => ({
  useEvidenceFileUrls: () => ({
    clear: vi.fn(),
    load: vi.fn(),
    getUrl: vi.fn(() => ''),
    revoke: vi.fn(),
  }),
}))

vi.mock('@/services/reports.api', () => ({ getReport: mocks.getReport }))
vi.mock('@/services/tasks.api', () => ({ getTask: mocks.getTask }))

import ReportDetailView from './ReportDetailView.vue'

describe('ReportDetailView 报告状态与阶段耗时', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getTask.mockResolvedValue({ task_name: 'Web 正式闭环' })
    mocks.getReport.mockResolvedValue({
      report_version: 'v2',
      report_stage: 'final',
      is_final: true,
      termination_reason: 'completed',
      generated_at: '2026-09-03T00:05:00Z',
      task: {
        task_id: 'task-web',
        target_url: 'https://example.test',
        ruleset_code: 'alioth_first_ruleset',
        ruleset_version: 1,
        status: 'done',
        run_id: 'run-web',
        created_at: '2026-09-03T00:00:00Z',
        started_at: '2026-09-03T00:00:05Z',
        finished_at: '2026-09-03T00:05:00Z',
      },
      summary: {
        total_rules: 1,
        pass: 1,
        fail: 0,
        warning: 0,
        manual_review: 0,
        evidence_insufficient: 0,
        skipped: 0,
        error: 0,
        attention_required: 0,
        evaluated_rules: 1,
        candidate_execution_coverage: 100,
        overall_status: 'pass',
      },
      page_exploration: null,
      attention_results: [],
      rule_results: [],
      evidence_entries: [],
      stage_durations: {
        exploration_ms: 125_000,
        inspection_ms: 90_000,
        report_generation_ms: 30_000,
        total_ms: 300_000,
      },
    })
  })

  it('正式报告展示与任务详情一致的四项 Web 阶段耗时', async () => {
    const wrapper = mount(ReportDetailView)
    await flushPromises()

    expect(wrapper.text()).toContain('探索耗时2分5秒')
    expect(wrapper.text()).toContain('规则质检耗时1分30秒')
    expect(wrapper.text()).toContain('报告生成耗时30秒')
    expect(wrapper.text()).toContain('提交至报告总耗时5分')
  })

  it.each([
    ['H5_REPORT_NO_BUSINESS_RESULTS', '本次未完成规则质检，不能作为业务质检报告或导出'],
    ['H5_REPORT_NOT_FINALIZED', '质检尚未完成正式收口，当前内容不能作为正式报告'],
    ['COMMAND_HOST_BOUNDARY_VIOLATION', '页面跳转超出本次任务允许的网站范围，质检已停止'],
  ])('将 %s 的报告缺口展示为中文说明', async (reason, explanation) => {
    const report = await mocks.getReport()
    mocks.getReport.mockResolvedValue({ ...report, report_stage: 'final_review', incomplete_reasons: [reason] })
    const wrapper = mount(ReportDetailView)
    await flushPromises()

    expect(wrapper.text()).toContain(explanation)
    expect(wrapper.text()).not.toContain(reason)
    if (reason === 'H5_REPORT_NO_BUSINESS_RESULTS') {
      expect(wrapper.text()).toContain('尚无有效质检结果')
      expect(wrapper.text()).not.toContain('待复核报告（未封存）')
      expect(wrapper.text()).not.toContain('已封存终版')
    }
  })
})
