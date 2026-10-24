import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AdminProvidersView from './AdminProvidersView.vue'

const mocks = vi.hoisted(() => ({
  getProvidersOverview: vi.fn(),
  setPrimaryProvider: vi.fn(),
  testProviderConnection: vi.fn(),
  updateProviderSettings: vi.fn(),
  createProvider: vi.fn(),
  updateProvider: vi.fn(),
  listProviderEvents: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('../api/providers', () => ({
  getProvidersOverview: mocks.getProvidersOverview,
  setPrimaryProvider: mocks.setPrimaryProvider,
  testProviderConnection: mocks.testProviderConnection,
  updateProviderSettings: mocks.updateProviderSettings,
  createProvider: mocks.createProvider,
  updateProvider: mocks.updateProvider,
  listProviderEvents: mocks.listProviderEvents,
}))

function provider(overrides: Record<string, unknown>) {
  return {
    id: 'p1', code: 'env:custom', name: 'custom · api.routin.ai', base_url: 'https://api.routin.ai/v1', model: 'grok-4.5',
    priority: 1, weight: 100, breaker_rule: '连续 3 次失败或错误率 > 5%', status: 'active', is_primary: true,
    api_key_configured: true, managed_by_env: true, quota_remaining_percent: null, health: 'healthy', health_label: '健康',
    last_check_status: null, last_check_latency_ms: null, last_check_message: null, last_checked_at: null,
    metrics: { call_count: 170, failed_count: 1, error_rate: 0.6, total_tokens: 6_536_052, last_call_at: '2026-09-08T06:23:52Z' },
    created_at: '2026-09-08T00:00:00Z', updated_at: '2026-09-08T00:00:00Z',
    ...overrides,
  }
}

const OVERVIEW = {
  monitor_status: 'warning',
  monitor_label: '有预警',
  generated_at: '2026-09-08T10:00:00Z',
  providers: [
    provider({}),
    provider({ id: 'p2', code: 'manual:b', name: '供应商 B', priority: 2, weight: 10, status: 'restricted', is_primary: false, managed_by_env: false, api_key_configured: false, quota_remaining_percent: 14, health: 'warning', health_label: '额度预警', last_check_latency_ms: 840, metrics: { call_count: 0, failed_count: 0, error_rate: null, total_tokens: 0, last_call_at: null } }),
  ],
  settings: {
    routing: { auto_failover: false },
    alert_policy: { service_down: true, quota_low: true, error_rate: true, latency: true },
    effective_provider_code: 'env:custom',
    note: '切换只改登记与审计记录；执行侧仍按环境变量配置调用模型。',
  },
  warnings: ['供应商 B 额度预警：剩余额度 14%'],
  events: [{ id: 'e1', provider_id: 'p2', provider_name: '供应商 B', event_type: 'created', description: '新增供应商登记', action: '备用优先级 2', result: '待接入', operator_name: '运营中心', occurred_at: '2026-09-08T09:18:00Z' }],
}

describe('AdminProvidersView', () => {
  beforeEach(() => {
    mocks.getProvidersOverview.mockReset().mockResolvedValue(JSON.parse(JSON.stringify(OVERVIEW)))
    mocks.setPrimaryProvider.mockReset().mockResolvedValue(provider({ id: 'p2', is_primary: true }))
    mocks.updateProviderSettings.mockReset().mockImplementation(async (payload) => ({ ...OVERVIEW.settings, ...payload }))
    mocks.testProviderConnection.mockReset().mockResolvedValue({ provider: provider({ last_check_latency_ms: 620, last_check_status: 'ok' }), status: 'ok', latency_ms: 620, message: '连接正常（HTTP 200）' })
    mocks.message.success.mockReset()
  })

  it('renders monitor status, warning bar, provider cards and the routing note', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-monitor"]').text()).toContain('有预警')
    expect(wrapper.find('[data-testid="provider-warning"]').text()).toContain('供应商 B 额度预警')
    const primary = wrapper.find('[data-testid="provider-card-env:custom"]')
    expect(primary.classes()).toContain('is-primary')
    expect(primary.text()).toContain('当前主供应商')
    expect(primary.text()).toContain('环境配置')
    expect(primary.text()).toContain('0.6%')
    const backup = wrapper.find('[data-testid="provider-card-manual:b"]')
    expect(backup.text()).toContain('备用优先级 2')
    expect(backup.text()).toContain('840ms')
    expect(backup.text()).toContain('14%')
    expect(wrapper.find('[data-testid="provider-routing"]').text()).toContain('主路由 · 100%')
    expect(wrapper.find('[data-testid="provider-routing-note"]').text()).toContain('当前生效：env:custom')
    expect(wrapper.find('[data-testid="provider-events"]').text()).toContain('新增供应商登记')
  })

  it('switches the primary provider through the confirm modal and saves settings on toggle', async () => {
    const wrapper = mount(AdminProvidersView)
    await flushPromises()

    await wrapper.find('[data-testid="provider-switch-manual:b"]').trigger('click')
    const modal = wrapper.find('[data-testid="provider-switch-modal"]')
    expect(modal.text()).toContain('由「custom · api.routin.ai」切换为「供应商 B」')
    await modal.find('[data-testid="provider-switch-confirm"]').trigger('click')
    await flushPromises()
    expect(mocks.setPrimaryProvider).toHaveBeenCalledWith('p2')
    expect(mocks.message.success).toHaveBeenCalledWith(expect.stringContaining('接入路由后生效'))

    await wrapper.find('[data-testid="provider-auto-failover"]').setValue(true)
    await flushPromises()
    expect(mocks.updateProviderSettings).toHaveBeenCalledWith({
      routing: { auto_failover: true },
      alert_policy: { service_down: true, quota_low: true, error_rate: true, latency: true },
    })

    await wrapper.find('[data-testid="provider-test-env:custom"]').trigger('click')
    await flushPromises()
    expect(mocks.testProviderConnection).toHaveBeenCalledWith('p1')
  })
})
