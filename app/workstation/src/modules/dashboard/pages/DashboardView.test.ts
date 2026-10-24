import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getDashboardOverview: vi.fn(),
  listTasks: vi.fn(),
  listReports: vi.fn(),
  push: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
  useRoute: () => ({ query: {} }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}))

vi.mock('@/modules/quick/components/QuickStartCard.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      setup: () => () => h('div', { 'data-testid': 'quick-start-card' }),
    }),
  }
})

vi.mock('@/services/dashboard.api', () => ({ getDashboardOverview: mocks.getDashboardOverview }))
vi.mock('@/services/tasks.api', () => ({ listTasks: mocks.listTasks }))
vi.mock('@/services/reports.api', () => ({ listReports: mocks.listReports }))
vi.mock('@/stores/auth', () => ({
  authStore: { user: { value: { username: 'u', full_name: '刘一', role: '规则配置员', tenant_name: '北京分公司' } } },
}))

import DashboardView from './DashboardView.vue'

function task(overrides: Record<string, unknown>) {
  return {
    task_id: 'task-1',
    task_name: '商城 8.3.1 回归',
    product: { department_name: '一部', product_code: 'P1', product_name: '商城' },
    target_url: null,
    target_platform: 'app',
    execution_mode: 'full_inspection',
    journey_steps: [],
    status: 'running',
    progress: 68,
    report_status: 'not_started',
    failure_reason: null,
    available_actions: [],
    created_at: '2026-09-08T10:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getDashboardOverview.mockResolvedValue({
    summary_cards: [{ key: 'done_tasks', label: '已完成', value: 40 }],
    today_metrics: { executed_tasks_today: 18 },
    recent_tasks: [],
    status_distribution: [
      { status: 'queued', count: 2 },
      { status: 'running', count: 1 },
    ],
    risk_distribution: [],
    execution_status_distribution: [],
    latest_rule_executions: [],
    token_usage: { month: '2026-09', total_tokens: 36_800_000, total_cost: 128.5, currency: 'CNY', task_count: 18 },
  })
  mocks.listTasks.mockResolvedValue({
    items: [
      task({}),
      task({ task_id: 'task-2', task_name: '活动页合规检查', target_platform: 'h5', status: 'queued', progress: 0 }),
      task({ task_id: 'task-3', task_name: '后台表单检查', target_platform: 'web', status: 'done', progress: 100 }),
    ],
    total: 3,
  })
  mocks.listReports.mockResolvedValue({
    items: [
      { task_id: 'task-3', task_name: '后台表单检查', report_status: 'ready', overall_status: 'fail', attention_required: 12, generated_at: null, pending_confirm_count: 3, pass_rate: 86.4 },
      { task_id: 'task-9', task_name: 'Web 登录流程质检', report_status: 'generating', overall_status: 'pending', attention_required: null, generated_at: null },
    ],
    total: 2,
    page: 1,
    page_size: 8,
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('DashboardView（部门工作台）', () => {
  it.each([
    [9, '上午好'],
    [13, '下午好'],
    [19, '晚上好'],
    [5, '晚上好'],
  ])('%i 点的问候语是「%s」，不带团队名、副标题和跳转按钮', async (hour, greeting) => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 8, 11, hour, 30))
    const wrapper = mount(DashboardView)
    await flushPromises()

    expect(wrapper.get('[data-testid="dashboard-greeting"]').text()).toBe(greeting)
    const hero = wrapper.get('.hero')
    expect(hero.findAll('button')).toHaveLength(0)
    expect(hero.text()).toBe(greeting)
    expect(wrapper.text()).not.toContain('质检团队')
    expect(wrapper.text()).not.toContain('完整配置任务')
  })

  it('概览与队列按账号数据展示', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()

    expect(wrapper.find('[data-testid="quick-start-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="dashboard-load-error"]').exists()).toBe(false)

    const stats = wrapper.find('[data-testid="stat-grid"]').text()
    expect(stats).toContain('今日任务')
    expect(stats).toContain('18')
    expect(stats).toContain('排队中')
    expect(stats).toContain('等待执行服务领取')
    expect(stats).toContain('进度见下方任务与队列')
    expect(stats).toContain('待查看报告')
    expect(stats).toContain('其中 1 份待人工确认')
    expect(stats).toContain('36.8M')
    expect(stats).toContain('18 个任务')

    const queue = wrapper.find('[data-testid="queue-panel"]')
    expect(queue.text()).toContain('商城 8.3.1 回归')
    expect(queue.text()).toContain('68%')
    expect(queue.text()).toContain('活动页合规检查')
    expect(queue.text()).toContain('排队中')
    // 只列排队/执行中的任务，已完成的不进队列表
    expect(queue.text()).not.toContain('后台表单检查')

    const reports = wrapper.find('[data-testid="reports-panel"]')
    expect(reports.get('.panel-head').text()).toContain('查看全部')
    expect(reports.text()).not.toContain('报告中心')
    expect(reports.text()).toContain('通过率 86.4% · 12 个问题 · 待确认 3')
    // 报告状态统一用三态文案：生成中 / 生成失败 / 成功
    expect(reports.text()).toContain('生成中')
    expect(reports.text()).not.toContain('报告生成中')
    expect(reports.text()).toContain('稍后查看')
  })

  it('最近报告没有通过率时只显示问题数，生成失败的报告用统一文案', async () => {
    mocks.listReports.mockResolvedValue({
      items: [
        { task_id: 'task-4', task_name: '会员中心回归', report_status: 'generated', overall_status: 'pass', attention_required: 4, generated_at: null, pass_rate: null },
        { task_id: 'task-5', task_name: '支付页检查', report_status: 'failed', overall_status: 'error', attention_required: null, generated_at: null },
      ],
      total: 2,
      page: 1,
      page_size: 8,
    })
    const wrapper = mount(DashboardView)
    await flushPromises()

    const reports = wrapper.get('[data-testid="reports-panel"]').text()
    expect(reports).toContain('4 个问题')
    expect(reports).not.toContain('通过率')
    expect(reports).toContain('生成失败')
  })

  it('没有排队和执行中的任务时两张卡片直说，不显示读不到的执行服务状态', async () => {
    mocks.getDashboardOverview.mockResolvedValue({
      summary_cards: [],
      today_metrics: { executed_tasks_today: 0 },
      recent_tasks: [],
      status_distribution: [],
      risk_distribution: [],
      execution_status_distribution: [],
      latest_rule_executions: [],
      token_usage: null,
    })
    mocks.listTasks.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mount(DashboardView)
    await flushPromises()
    const stats = wrapper.find('[data-testid="stat-grid"]').text()
    expect(stats).toContain('当前没有排队任务')
    expect(stats).toContain('当前没有执行中任务')
    expect(stats).not.toContain('状态未知')
    expect(wrapper.find('[data-testid="dashboard-load-error"]').exists()).toBe(false)
  })

  it('数据还没回来时统计卡片显示「…」和「加载中…」，不下「当前没有…」的结论', async () => {
    mocks.getDashboardOverview.mockReturnValue(new Promise(() => undefined))
    const wrapper = mount(DashboardView)
    await flushPromises()
    const stats = wrapper.get('[data-testid="stat-grid"]').text()
    expect(stats).toContain('…')
    expect(stats).toContain('加载中…')
    expect(stats).not.toContain('当前没有排队任务')
    expect(stats).not.toContain('当前没有执行中任务')
  })

  it('任务列表加载失败时面板说「加载失败」，不说「还没有任务」', async () => {
    mocks.listTasks.mockRejectedValue(new Error('boom'))
    const wrapper = mount(DashboardView)
    await flushPromises()
    const queue = wrapper.get('[data-testid="queue-panel"]').text()
    expect(queue).toContain('任务列表加载失败')
    expect(queue).not.toContain('还没有任务')
  })

  it('部分模块加载失败时保留其余内容并提示', async () => {
    mocks.listReports.mockRejectedValue(new Error('boom'))
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.find('[data-testid="dashboard-load-error"]').text()).toContain('1 个模块加载失败')
    expect(wrapper.find('[data-testid="queue-panel"]').text()).toContain('商城 8.3.1 回归')
  })
})
