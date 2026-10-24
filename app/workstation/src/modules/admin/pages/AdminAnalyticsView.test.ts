import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AdminAnalyticsView from './AdminAnalyticsView.vue'

const mocks = vi.hoisted(() => ({
  replace: vi.fn(),
  route: { path: '/admin/analytics', query: {} as Record<string, string> },
  getOperationsAnalytics: vi.fn(),
  listTenants: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRoute: () => mocks.route, useRouter: () => ({ replace: mocks.replace, push: vi.fn() }) }))
vi.mock('../api/operations', () => ({ getOperationsAnalytics: mocks.getOperationsAnalytics }))
vi.mock('../api/tenants', () => ({ listTenants: mocks.listTenants }))

const DATA = {
  range: '7d', start: '2026-09-01T00:00:00Z', end: '2026-09-08T10:00:00Z', tenant_id: null, platform: null, generated_at: '2026-09-08T10:00:00Z',
  summary: { task_count: 12, report_count: 9, report_success_rate: 90, failure_rate: 8.3, done_count: 5, review_count: 4, failed_count: 1, canceled_count: 1, active_count: 1, active_tenant_count: 2, active_account_count: 5, avg_duration_seconds: 252 },
  previous: { task_count: 10, report_count: 8, report_success_rate: 80, failure_rate: 10, done_count: 5, review_count: 3, failed_count: 1, canceled_count: 0, active_count: 0, active_tenant_count: 2, active_account_count: 0, avg_duration_seconds: 200 },
  trend: [{ date: '2026-09-07', task_count: 9, report_count: 7, failed_count: 1 }],
  platform_share: [{ platform: 'h5', task_count: 10 }, { platform: 'app', task_count: 2 }],
  tenants: [
    { tenant_id: 't1', tenant_name: '体验中心', status: 'enabled', product_count: 95, active_account_count: 2, task_count: 11, report_count: 9, report_success_rate: 90, failure_rate: 9.1, last_used_at: '2026-09-08T09:00:00Z', usage_status: 'active' },
  ],
}

describe('AdminAnalyticsView', () => {
  beforeEach(() => {
    mocks.getOperationsAnalytics.mockReset().mockResolvedValue(DATA)
    mocks.listTenants.mockReset().mockResolvedValue([])
    mocks.replace.mockReset()
    mocks.route.query = {}
  })

  it('renders KPIs with period-over-period deltas and the overview charts', async () => {
    const wrapper = mount(AdminAnalyticsView)
    await flushPromises()
    const kpis = wrapper.find('[data-testid="analytics-kpis"]').text()
    expect(kpis).toContain('较上期 +20%')
    expect(kpis).toContain('上期无数据')
    expect(kpis).toContain('目标 ≥ 95%')
    expect(wrapper.text()).toContain('平台类型使用占比')
    expect(wrapper.text()).toContain('分公司使用排名')
    expect(mocks.getOperationsAnalytics).toHaveBeenCalledWith({ range: '7d', tenant_id: undefined, platform: undefined })
  })

  it('switches tabs through the route query and picks up a company from the query', async () => {
    mocks.route.query = { tab: 'account', tenant_id: 't1' }
    const wrapper = mount(AdminAnalyticsView)
    await flushPromises()
    expect(mocks.getOperationsAnalytics).toHaveBeenCalledWith({ range: '7d', tenant_id: 't1', platform: undefined })
    expect(wrapper.find('[data-testid="analytics-account-table"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-kpis"]').exists()).toBe(false)

    await wrapper.find('[data-testid="analytics-tab-task"]').trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({ path: '/admin/analytics', query: { tab: 'task', tenant_id: 't1' } })
  })
})
