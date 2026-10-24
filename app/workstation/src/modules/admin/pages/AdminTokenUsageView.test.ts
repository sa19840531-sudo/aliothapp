import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AdminTokenUsageView from './AdminTokenUsageView.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  getDepartmentCostReport: vi.fn(),
  getDepartmentCostDetail: vi.fn(),
  exportDepartmentCostReport: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('../api/tokenUsage', () => ({
  getDepartmentCostReport: mocks.getDepartmentCostReport,
  getDepartmentCostDetail: mocks.getDepartmentCostDetail,
  exportDepartmentCostReport: mocks.exportDepartmentCostReport,
}))

const REPORT = {
  month: '2026-09',
  month_label: '2026 年 9 月',
  start: '2026-09-01T00:00:00Z',
  end: '2026-10-01T00:00:00Z',
  generated_at: '2026-09-08T10:30:00Z',
  provider: null,
  providers: ['custom'],
  months: ['2026-09', '2026-08'],
  totals: { input_tokens: 5_693_804, output_tokens: 165_804, cache_hit_tokens: 1_098_880, total_tokens: 5_859_608, ai_calls: 149, task_count: 8, total_cost: 0, unpriced_calls: 149, currency: 'CNY' },
  previous_totals: { input_tokens: 0, output_tokens: 0, cache_hit_tokens: 0, total_tokens: 0, ai_calls: 0, task_count: 0, total_cost: 0, unpriced_calls: 0, currency: 'CNY' },
  token_change_percent: null,
  cost_change_percent: null,
  alert_department_count: 1,
  over_90_department_count: 1,
  rows: [
    { tenant_id: 't1', tenant_name: '体验中心', department_name: '规则验证', input_tokens: 3_391_245, output_tokens: 94_193, cache_hit_tokens: 0, total_tokens: 3_485_438, ai_calls: 82, task_count: 3, total_cost: 2864.2, main_provider: 'custom', main_model: 'grok-4.5', monthly_budget: 3000, budget_used_percent: 95.5, budget_level: 'critical' },
    { tenant_id: 't1', tenant_name: '体验中心', department_name: '未填写部门', input_tokens: 181_454, output_tokens: 6_927, cache_hit_tokens: 0, total_tokens: 188_381, ai_calls: 6, task_count: 1, total_cost: 0, main_provider: 'custom', main_model: 'grok-4.5', monthly_budget: null, budget_used_percent: null, budget_level: 'none' },
  ],
  current_price: { input_price: 0, output_price: 0, cache_hit_price: 0, pricing_source: 'runtime_config_per_call', pricing_configured: false, currency: 'CNY', effective_from: '2026-09-07T08:43:28Z', last_seen_at: '2026-09-08T06:23:52Z', event_count: 170 },
  price_history: [{ input_price: 0, output_price: 0, cache_hit_price: 0, pricing_source: 'runtime_config_per_call', pricing_configured: false, currency: 'CNY', effective_from: '2026-09-07T08:43:28Z', last_seen_at: '2026-09-08T06:23:52Z', event_count: 170 }],
}

describe('AdminTokenUsageView', () => {
  beforeEach(() => {
    mocks.getDepartmentCostReport.mockReset().mockResolvedValue(REPORT)
    mocks.getDepartmentCostDetail.mockReset().mockResolvedValue([
      { task_id: 'task-1', task_name: '规则验证第二跑', department_name: '规则验证', provider: 'custom', model: 'grok-4.5', ai_calls: 53, input_tokens: 2_046_315, output_tokens: 60_346, total_tokens: 2_106_661, total_cost: 0, first_call_at: null, last_call_at: '2026-09-08T05:38:56Z' },
    ])
    mocks.exportDepartmentCostReport.mockReset().mockResolvedValue(undefined)
  })

  it('renders totals, budget grading and the unpriced-call warning', async () => {
    const wrapper = mount(AdminTokenUsageView)
    await flushPromises()

    const kpis = wrapper.find('[data-testid="token-kpis"]').text()
    expect(kpis).toContain('5.9M')
    expect(kpis).toContain('5.7M')
    expect(kpis).toContain('149 次调用单价未配置')
    expect(kpis).toContain('1 个已超过 90%')

    const table = wrapper.find('[data-testid="token-table"]').text()
    expect(table).toContain('体验中心 · 规则验证')
    expect(table).toContain('95.5%')
    expect(table).toContain('¥3,000.00')
    expect(table).toContain('未设预算')
    expect(wrapper.find('[data-testid="token-price"]').text()).toContain('单价还没配置')
  })

  it('opens the per-task detail and exports with the current filters', async () => {
    const wrapper = mount(AdminTokenUsageView)
    await flushPromises()

    await wrapper.find('[data-testid="token-provider"]').setValue('custom')
    await flushPromises()
    expect(mocks.getDepartmentCostReport).toHaveBeenLastCalledWith({ month: '2026-09', provider: 'custom' })

    await wrapper.findAll('[data-testid="token-table"] .qc-link')[0].trigger('click')
    await flushPromises()
    expect(mocks.getDepartmentCostDetail).toHaveBeenCalledWith({ month: '2026-09', provider: 'custom', tenant_id: 't1', department_name: '规则验证' })
    expect(wrapper.find('[data-testid="token-detail-modal"]').text()).toContain('规则验证第二跑')

    await wrapper.find('[data-testid="token-export"]').trigger('click')
    expect(mocks.exportDepartmentCostReport).toHaveBeenCalledWith({ month: '2026-09', provider: 'custom' })
  })
})
