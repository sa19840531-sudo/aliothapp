import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listRulesets: vi.fn(), createBusinessRuleset: vi.fn(), patchRuleset: vi.fn(),
  message: { success: vi.fn(), error: vi.fn() },
}))
vi.mock('naive-ui', () => ({
  useMessage: () => mocks.message,
  NDrawer: { props: ['show'], template: '<div v-if="show"><slot /></div>' },
  NDrawerContent: { template: '<div><slot /></div>' },
}))
vi.mock('../../api/rules', () => ({
  listRulesets: mocks.listRulesets, createBusinessRuleset: mocks.createBusinessRuleset,
  patchRuleset: mocks.patchRuleset,
}))
import RulesetPanel from './RulesetPanel.vue'

describe('规则集明细入口', () => {
  beforeEach(() => {
    mocks.listRulesets.mockResolvedValue({ items: [
      { id: 'rs-v1', ruleset_code: 'phase', name: '同一期规则集', version: 1, status: 'archived', description: '历史版本', rule_count: 1 },
      { id: 'rs-v2', ruleset_code: 'phase', name: '同一期规则集', version: 2, status: 'enabled', description: '当前版本', rule_count: 2 },
    ] })
  })

  it('只读账号点击规则集名称或查看规则均传递对应版本 ID，历史版本也能核对', async () => {
    const wrapper = mount(RulesetPanel, { props: { canWrite: false } })
    await flushPromises()
    await wrapper.find('[data-testid="ruleset-open-rs-v1"]').trigger('click')
    const second = wrapper.find('[data-testid="ruleset-open-rs-v2"]').element.closest('article')!
    const viewButton = Array.from(second.querySelectorAll('button')).find((button) => button.textContent === '查看规则')!
    viewButton.click()
    expect(wrapper.emitted('viewRules')).toEqual([['rs-v1'], ['rs-v2']])
    expect(wrapper.find('[data-testid="ruleset-create"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('编辑')
    expect(mocks.patchRuleset).not.toHaveBeenCalled()
  })
})
