import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'

const mocks = vi.hoisted(() => ({
  route: { path: '/admin/rules', query: {} as Record<string, string> },
  replace: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRoute: () => mocks.route, useRouter: () => ({ replace: mocks.replace }) }))
vi.mock('@/modules/config/components/ConfigCenterTabs.vue', () => ({
  default: { props: ['active'], template: '<div data-testid="config-tabs-stub">{{ active }}</div>' },
}))
vi.mock('../components/rules/RuleListPanel.vue', () => ({
  default: { props: ['canWrite'], template: '<div data-testid="list-stub">{{ canWrite }}</div>' },
}))
vi.mock('../components/rules/RulesetPanel.vue', () => ({
  default: { props: ['canWrite'], emits: ['viewRules'], template: '<button data-testid="set-stub" @click="$emit(\'viewRules\', \'rs2\')">{{ canWrite }}</button>' },
}))
vi.mock('../components/rules/RuleTestTab.vue', () => ({
  default: { props: ['active'], template: '<div data-testid="test-stub"></div>' },
}))

import AdminQcRulesView from './AdminQcRulesView.vue'

function signIn(permissions: string[]) {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: { id: 'u1', username: 'admin', full_name: '管理员', role: '平台管理员', status: 'enabled', permissions, tenant_id: 't1', tenant_name: '体验中心' },
  }
}

describe('配置中心 · 规则配置', () => {
  beforeEach(() => {
    mocks.route.query = {}
    mocks.replace.mockReset()
  })

  it('只读管理账号看不到规则测试，带 ?tab=test 也回到规则明细', () => {
    signIn(['qcRules', 'configCenter', 'taskObjects:read:tenant'])
    mocks.route.query = { tab: 'test' }
    const wrapper = mount(AdminQcRulesView)

    const tabs = wrapper.find('[data-testid="qc-rules-tabs"]').text()
    expect(tabs).toContain('规则明细')
    expect(tabs).toContain('规则集')
    expect(tabs).not.toContain('规则测试')
    expect(wrapper.find('[data-testid="list-stub"]').text()).toBe('false')
    expect(wrapper.find('[data-testid="test-stub"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="config-tabs-stub"]').text()).toBe('rule')
  })

  it('体验中心管理员可以编辑并打开规则测试，切页签写回地址栏', async () => {
    signIn(['qcRules', 'qcRules:write', 'taskObjects:read:any'])
    const wrapper = mount(AdminQcRulesView)

    expect(wrapper.find('[data-testid="qc-rules-tab-test"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="list-stub"]').text()).toBe('true')
    await wrapper.find('[data-testid="qc-rules-tab-set"]').trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({ path: '/admin/rules', query: { tab: 'set' } })
  })
  it('从规则集切到对应版本的规则明细，地址栏保留规则集 ID', async () => {
    signIn(['qcRules', 'configCenter'])
    mocks.route.query = { tab: 'set' }
    const wrapper = mount(AdminQcRulesView)
    await wrapper.find('[data-testid="set-stub"]').trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({ path: '/admin/rules', query: { ruleset_id: 'rs2' } })
  })

})
