import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'
import AdminLayout from './AdminLayout.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  route: { path: '/admin', fullPath: '/admin', query: {} as Record<string, string>, meta: { title: '工作台' } },
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
  RouterView: defineComponent({ name: 'RouterView', render: () => h('div', { class: 'router-view' }) }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: vi.fn(), error: vi.fn() }),
  NDropdown: defineComponent({ name: 'NDropdown', setup: (_props, { slots }) => () => h('div', slots.default?.()) }),
  NIcon: defineComponent({ name: 'NIcon', setup: (_props, { slots }) => () => h('i', slots.default?.()) }),
}))

vi.mock('@vicons/ionicons5', () => {
  const icon = defineComponent({ render: () => h('svg') })
  return { ChevronForwardOutline: icon, MenuOutline: icon, MoonOutline: icon, SunnyOutline: icon }
})

const HQ_PERMISSIONS = [
  'dashboard', 'users', 'users:any', 'tenants', 'departments', 'roles', 'auditLogs', 'qcRules', 'configCenter',
  'costReports', 'analytics', 'system', 'agentRuns', 'systemOperations', 'taskObjects:read:any',
]
const COMPANY_PERMISSIONS = ['dashboard', 'users', 'departments', 'roles', 'qcRules', 'configCenter', 'taskObjects:read:tenant']

function signIn(permissions: string[]) {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: {
      id: 'u1',
      username: 'admin@group.com',
      full_name: '运营中心管理员',
      role: '平台管理员',
      status: 'enabled',
      permissions,
      tenant_id: 't1',
      tenant_name: '体验中心',
    },
  }
}

function mountLayout() {
  return mount(AdminLayout, { global: { stubs: { RouterView: true } } })
}

describe('AdminLayout', () => {
  beforeEach(() => {
    localStorage.clear()
    mocks.push.mockReset()
    mocks.route.path = '/admin'
    mocks.route.fullPath = '/admin'
    mocks.route.query = {}
    mocks.route.meta = { title: '工作台' }
  })

  it('shows the full 2.1 menu tree to the operations centre and hides gated groups from a company admin', async () => {
    signIn(HQ_PERMISSIONS)
    const hq = mountLayout()
    await flushPromises()
    const hqLabels = hq.find('[data-testid="admin-nav"]').text()
    for (const label of ['工作台', '组织管理', '产品管理', '任务中心', '报告中心', '配置中心', '反馈中心', '使用分析', '资源治理', '系统管理']) {
      expect(hqLabels).toContain(label)
    }
    expect(hq.find('[data-testid="admin-crumbs"]').text()).toBe('运营中心/工作台')
    hq.unmount()

    signIn(COMPANY_PERMISSIONS)
    const company = mountLayout()
    await flushPromises()
    const companyLabels = company.find('[data-testid="admin-nav"]').text()
    expect(companyLabels).not.toContain('使用分析')
    expect(companyLabels).not.toContain('系统管理')
    expect(companyLabels).not.toContain('资源治理')
    expect(company.find('[data-testid="admin-nav-org"]').exists()).toBe(true)
    await company.find('[data-testid="admin-nav-org"]').trigger('click')
    expect(company.find('[data-testid="admin-nav-tenants"]').exists()).toBe(false)
    expect(company.find('[data-testid="admin-nav-departments"]').exists()).toBe(true)
  })

  it('expands the group of the current page, marks the child active and navigates on click', async () => {
    signIn(HQ_PERMISSIONS)
    mocks.route.path = '/admin/users'
    mocks.route.fullPath = '/admin/users'
    mocks.route.meta = { title: '账号与权限' }
    const wrapper = mountLayout()
    await flushPromises()
    expect(wrapper.find('[data-testid="admin-nav-org"]').classes()).toContain('side-item-active')
    expect(wrapper.find('[data-testid="admin-nav-users"]').classes()).toContain('side-child-active')
    expect(wrapper.find('[data-testid="admin-crumbs"]').text()).toBe('组织管理/账号与权限')

    await wrapper.find('[data-testid="admin-nav-tenants"]').trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/admin/tenants')
  })

  it('distinguishes tab entries of the same page by the tab query', async () => {
    signIn(HQ_PERMISSIONS)
    mocks.route.path = '/admin/feedback'
    mocks.route.fullPath = '/admin/feedback?tab=handle'
    mocks.route.query = { tab: 'handle' }
    mocks.route.meta = { title: '反馈中心' }
    const wrapper = mountLayout()
    await flushPromises()
    expect(wrapper.find('[data-testid="admin-nav-feedbackHandle"]').classes()).toContain('side-child-active')
    expect(wrapper.find('[data-testid="admin-nav-myFeedback"]').classes()).not.toContain('side-child-active')
    // 面包屑第二段用菜单名，不再重复成「反馈中心/反馈中心」
    expect(wrapper.find('[data-testid="admin-crumbs"]').text()).toBe('反馈中心/反馈处理')
  })

  it('makes the second-level crumb clickable and adds the page title below it on nested pages', async () => {
    signIn(HQ_PERMISSIONS)
    mocks.route.path = '/admin/tasks/task-1'
    mocks.route.fullPath = '/admin/tasks/task-1'
    mocks.route.meta = { title: '任务详情' }
    const wrapper = mountLayout()
    await flushPromises()
    expect(wrapper.find('[data-testid="admin-crumbs"]').text()).toBe('任务中心/全部任务/任务详情')

    await wrapper.find('[data-testid="admin-crumb-page"]').trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/admin/tasks')
  })

  it('highlights the exact entry instead of its parent list', async () => {
    signIn(HQ_PERMISSIONS)
    mocks.route.path = '/admin/products/import'
    mocks.route.fullPath = '/admin/products/import'
    mocks.route.meta = { title: 'Excel 导入' }
    const importPage = mountLayout()
    await flushPromises()
    expect(importPage.find('[data-testid="admin-nav-productImport"]').classes()).toContain('side-child-active')
    expect(importPage.find('[data-testid="admin-nav-productList"]').classes()).not.toContain('side-child-active')
    expect(importPage.find('[data-testid="admin-crumbs"]').text()).toBe('产品管理/Excel 导入')
    importPage.unmount()

    mocks.route.path = '/admin/tasks/new'
    mocks.route.fullPath = '/admin/tasks/new'
    mocks.route.meta = { title: '创建质检任务' }
    const createPage = mountLayout()
    await flushPromises()
    expect(createPage.find('[data-testid="admin-nav-taskCreate"]').classes()).toContain('side-child-active')
    expect(createPage.find('[data-testid="admin-nav-taskAll"]').classes()).not.toContain('side-child-active')
    expect(createPage.find('[data-testid="admin-crumbs"]').text()).toBe('任务中心/创建任务')
  })

  it('collapses the sidebar to badges and remembers it', async () => {
    signIn(HQ_PERMISSIONS)
    const wrapper = mountLayout()
    await flushPromises()
    expect(wrapper.find('.brand-name').exists()).toBe(true)
    await wrapper.find('[data-testid="admin-sidebar-toggle"]').trigger('click')
    expect(wrapper.find('.brand-name').exists()).toBe(false)
    expect(wrapper.find('.admin-shell').classes()).toContain('sidebar-collapsed')
    expect(localStorage.getItem('alioth-admin-sidebar-collapsed')).toBe('1')
  })
})
