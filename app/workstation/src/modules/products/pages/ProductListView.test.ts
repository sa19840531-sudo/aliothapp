import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'
import ProductListView from './ProductListView.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  route: { path: '/products', query: {} },
  listProducts: vi.fn(),
  createProduct: vi.fn(),
  updateProduct: vi.fn(),
  listTenants: vi.fn(),
  listDepartments: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('vue-router', () => ({ useRoute: () => mocks.route, useRouter: () => ({ push: mocks.push, replace: vi.fn() }) }))
vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('@/services/products.api', () => ({
  PRODUCT_PLATFORM_LABELS: { h5: 'H5', web: 'Web', app: 'Android', ios: 'iOS' },
  listProducts: mocks.listProducts,
  createProduct: mocks.createProduct,
  updateProduct: mocks.updateProduct,
}))
vi.mock('@/modules/admin/api/tenants', () => ({ listTenants: mocks.listTenants }))
vi.mock('@/modules/admin/api/departments', () => ({ listDepartments: mocks.listDepartments }))
vi.mock('../components/ProductImportPanel.vue', () => ({
  default: { name: 'ProductImportPanel', props: ['tab'], template: '<div data-testid="import-panel-stub">{{ tab }}</div>' },
}))

const PRODUCTS = {
  items: [
    { id: 'p1', project_code: 'MB-001', product_name: '手机银行', h5_url: null, department_name: '一部', owner_name: '小明', platforms: ['app', 'ios'], status: 'enabled', tenant_id: 't2', tenant_name: '北京分公司', created_at: '2026-09-08T00:00:00Z', updated_at: '2026-09-08T00:00:00Z' },
    { id: 'p2', project_code: 'JHG-024', product_name: '共享站点', h5_url: 'https://example.com', department_name: null, owner_name: null, platforms: ['h5'], status: 'disabled', tenant_id: null, tenant_name: null, created_at: '2026-09-08T00:00:00Z', updated_at: '2026-09-08T00:00:00Z' },
  ],
  total: 2,
  page: 1,
  page_size: 10,
}

function signIn(permissions: string[]) {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: { id: 'u1', username: 'admin', full_name: '管理员', role: '平台管理员', status: 'enabled', permissions, tenant_id: 't1', tenant_name: '体验中心' },
  }
}

describe('ProductListView', () => {
  beforeEach(() => {
    mocks.listProducts.mockReset().mockResolvedValue(PRODUCTS)
    mocks.listTenants.mockReset().mockResolvedValue([{ id: 't2', display_name: '北京分公司', status: 'enabled' }])
    mocks.listDepartments.mockReset().mockResolvedValue([{ id: 'd1', tenant_id: 't2', name: '一部' }])
    mocks.updateProduct.mockReset().mockResolvedValue(PRODUCTS.items[1])
    mocks.push.mockReset()
    mocks.route.path = '/products'
  })

  it('shows the company column and filter to the operations centre, including disabled and iOS products', async () => {
    signIn(['qcRules', 'tenants', 'departments', 'taskObjects:read:any'])
    mocks.route.path = '/admin/products'
    const wrapper = mount(ProductListView)
    await flushPromises()

    expect(mocks.listProducts).toHaveBeenCalledWith(expect.objectContaining({ page: 1, page_size: 10 }))
    expect(wrapper.find('[data-testid="product-filter-tenant"]').exists()).toBe(true)
    const first = wrapper.find('[data-testid="product-row-MB-001"]').text()
    expect(first).toContain('北京分公司')
    expect(first).toContain('iOS')
    const shared = wrapper.find('[data-testid="product-row-JHG-024"]').text()
    expect(shared).toContain('运营中心（共享）')
    expect(shared).toContain('停用')

    await wrapper.find('[data-testid="product-filter-tenant"]').setValue('shared')
    await wrapper.find('[data-testid="product-filter-status"]').setValue('disabled')
    await wrapper.find('[data-testid="product-search"]').trigger('keyup.enter')
    await flushPromises()
    expect(mocks.listProducts).toHaveBeenLastCalledWith(expect.objectContaining({ tenant_id: 'shared', status: 'disabled' }))
  })

  it('hides the company column for a department account and keeps the import tabs on the department portal', async () => {
    signIn(['qcRules', 'taskObjects:read:owner'])
    const wrapper = mount(ProductListView)
    await flushPromises()
    expect(wrapper.find('[data-testid="product-filter-tenant"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="product-tabs"]').exists()).toBe(true)
    expect(mocks.listTenants).not.toHaveBeenCalled()
    // 部门平台不展示公司 / 部门，共享产品由运营中心维护，不给编辑 / 停用
    const headers = wrapper.findAll('th').map((node) => node.text())
    expect(headers).not.toContain('所属部门')
    const sharedLinks = wrapper.findAll('[data-testid="product-row-JHG-024"] .qc-link').map((node) => node.text())
    expect(sharedLinks).not.toContain('编辑')
    const ownLinks = wrapper.findAll('[data-testid="product-row-MB-001"] .qc-link').map((node) => node.text())
    expect(ownLinks).toContain('编辑')

    await wrapper.find('[data-testid="product-tab-records"]').trigger('click')
    expect(wrapper.find('[data-testid="import-panel-stub"]').text()).toBe('record')
  })

  it('drops the tabs inside the admin portal and routes 发起质检 through /admin', async () => {
    signIn(['qcRules', 'taskObjects:read:any'])
    mocks.route.path = '/admin/products'
    const wrapper = mount(ProductListView)
    await flushPromises()
    expect(wrapper.find('[data-testid="product-tabs"]').exists()).toBe(false)
    const inspect = wrapper.findAll('[data-testid="product-row-MB-001"] .qc-link').find((node) => node.text() === '发起质检')
    await inspect!.trigger('click')
    expect(mocks.push).toHaveBeenCalledWith({ path: '/admin/quick', query: { product: 'MB-001' } })
  })
})
