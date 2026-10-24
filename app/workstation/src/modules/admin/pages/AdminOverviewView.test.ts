import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'
import AdminOverviewView from './AdminOverviewView.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  getOperationsOverview: vi.fn(),
  listTenants: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('../api/operations', () => ({ getOperationsOverview: mocks.getOperationsOverview }))
vi.mock('../api/tenants', () => ({ listTenants: mocks.listTenants }))

const OVERVIEW = {
  range: '7d',
  start: '2026-09-01T00:00:00Z',
  end: '2026-09-08T10:00:00Z',
  tenant_id: null,
  platform: null,
  generated_at: '2026-09-08T10:00:00Z',
  summary: {
    task_count: 12, report_count: 9, report_success_rate: 90, failure_rate: 8.3, done_count: 5, review_count: 4,
    failed_count: 1, canceled_count: 1, active_count: 1, active_tenant_count: 2, active_account_count: 5, avg_duration_seconds: 252,
  },
  previous: {
    task_count: 4, report_count: 3, report_success_rate: 75, failure_rate: 25, done_count: 3, review_count: 0,
    failed_count: 1, canceled_count: 0, active_count: 0, active_tenant_count: 1, active_account_count: 2, avg_duration_seconds: 100,
  },
  trend: [
    { date: '2026-09-07', task_count: 9, report_count: 7, failed_count: 1 },
    { date: '2026-09-08', task_count: 3, report_count: 2, failed_count: 0 },
  ],
  platform_share: [{ platform: 'h5', task_count: 12 }],
  tenants: [
    { tenant_id: 't1', tenant_name: '体验中心', status: 'enabled', product_count: 95, active_account_count: 2, task_count: 11, report_count: 9, report_success_rate: 90, failure_rate: 9.1, last_used_at: '2026-09-08T09:00:00Z', usage_status: 'active' },
    { tenant_id: 't2', tenant_name: '北京分公司', status: 'enabled', product_count: 3, active_account_count: 0, task_count: 0, report_count: 0, report_success_rate: null, failure_rate: null, last_used_at: null, usage_status: 'idle' },
  ],
}

function signIn(permissions: string[]) {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: { id: 'u1', username: 'admin', full_name: '管理员', role: '平台管理员', status: 'enabled', permissions, tenant_id: 't1', tenant_name: '体验中心' },
  }
}

describe('AdminOverviewView', () => {
  beforeEach(() => {
    mocks.getOperationsOverview.mockReset().mockResolvedValue(OVERVIEW)
    mocks.listTenants.mockReset().mockResolvedValue([{ id: 't2', display_name: '北京分公司', status: 'enabled' }])
    mocks.push.mockReset()
  })

  it('renders the six KPIs, charts and the company summary table for the operations centre', async () => {
    signIn(['dashboard', 'tenants', 'analytics', 'taskObjects:read:any'])
    const wrapper = mount(AdminOverviewView)
    await flushPromises()

    const kpis = wrapper.find('[data-testid="overview-kpis"]').text()
    expect(kpis).toContain('质检任务总数')
    expect(kpis).toContain('12')
    expect(kpis).toContain('90%')
    expect(kpis).toContain('4m 12s')
    const durationLabel = wrapper.findAll('.ap-kpi-label').find((item) => item.text() === '平均执行时长')
    expect(durationLabel?.attributes('title')).toContain('不含排队')
    expect(durationLabel?.attributes('title')).toContain('待复核')
    expect(wrapper.find('[data-testid="overview-tenant"]').exists()).toBe(true)
    expect(mocks.getOperationsOverview).toHaveBeenCalledWith({ range: '7d', tenant_id: undefined, platform: undefined })

    const table = wrapper.find('[data-testid="overview-tenants"]').text()
    expect(table).toContain('体验中心')
    expect(table).toContain('活跃')
    expect(table).toContain('沉默')
    expect(wrapper.findAll('svg').length).toBeGreaterThan(0)

    await wrapper.findAll('[data-testid="overview-tenants"] .qc-link')[0].trigger('click')
    expect(mocks.push).toHaveBeenCalledWith({ path: '/admin/analytics', query: { tenant_id: 't1' } })
  })

  it('locks the company filter for a company admin and passes platform filters to the API', async () => {
    signIn(['dashboard', 'users', 'taskObjects:read:tenant'])
    const wrapper = mount(AdminOverviewView)
    await flushPromises()
    expect(wrapper.find('[data-testid="overview-tenant"]').exists()).toBe(false)
    expect(mocks.listTenants).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="overview-platform"]').setValue('h5')
    await wrapper.find('[data-testid="overview-range"]').setValue('quarter')
    await wrapper.find('[data-testid="overview-search"]').trigger('click')
    await flushPromises()
    expect(mocks.getOperationsOverview).toHaveBeenLastCalledWith({ range: 'quarter', tenant_id: undefined, platform: 'h5' })
  })

  it('shows the error when the API fails', async () => {
    signIn(['dashboard'])
    mocks.getOperationsOverview.mockRejectedValueOnce(new Error('后端不可用'))
    const wrapper = mount(AdminOverviewView)
    await flushPromises()
    expect(wrapper.find('[data-testid="overview-error"]').text()).toBe('后端不可用')
  })

  it.each([
    [0, '0s'],
    [59.6, '1m 0s'],
    [null, '—'],
  ])('平均耗时 %s 秒显示为 %s', async (seconds, expected) => {
    signIn(['dashboard'])
    mocks.getOperationsOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      summary: { ...OVERVIEW.summary, avg_duration_seconds: seconds },
    })
    const wrapper = mount(AdminOverviewView)
    await flushPromises()
    const kpi = wrapper.findAll('.ap-kpi').find((item) => item.text().includes('平均执行时长'))
    expect(kpi?.find('.ap-kpi-value').text()).toBe(expected)
  })
})
