import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AdminTenantsView from './AdminTenantsView.vue'

const mocks = vi.hoisted(() => ({
  listTenants: vi.fn(),
  createTenant: vi.fn(),
  updateTenant: vi.fn(),
  deleteTenant: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('../api/tenants', () => ({
  listTenants: mocks.listTenants,
  createTenant: mocks.createTenant,
  updateTenant: mocks.updateTenant,
  deleteTenant: mocks.deleteTenant,
}))

const TENANTS = [
  { id: 't1', tenant_key: 'default', display_name: '体验中心', status: 'enabled', contact_name: null, remark: null, admin_username: 'admin', user_count: 2, department_count: 0, product_count: 95, is_headquarters: true, created_at: '2026-08-18T14:21:27Z', updated_at: '2026-09-07T10:12:34Z' },
  { id: 't2', tenant_key: 'c-1001', display_name: '华东分公司', status: 'enabled', contact_name: '张三', remark: null, admin_username: 'admin@hd', user_count: 6, department_count: 6, product_count: 12, is_headquarters: false, created_at: '2025-01-15T00:00:00Z', updated_at: '2025-01-15T00:00:00Z' },
]

describe('AdminTenantsView', () => {
  beforeEach(() => {
    mocks.deleteTenant.mockReset().mockResolvedValue(undefined)
    mocks.listTenants.mockReset().mockResolvedValue(TENANTS)
    mocks.createTenant.mockReset().mockResolvedValue(TENANTS[1])
    mocks.updateTenant.mockReset().mockResolvedValue({ ...TENANTS[1], status: 'disabled' })
    mocks.message.warning.mockReset()
  })

  it('lists companies with admin account, department and product counts and hides the toggle for headquarters', async () => {
    const wrapper = mount(AdminTenantsView)
    await flushPromises()
    const row = wrapper.find('[data-testid="tenant-row-c-1001"]')
    expect(row.text()).toContain('admin@hd')
    expect(row.text()).toContain('6')
    expect(row.text()).toContain('12')
    expect(wrapper.find('[data-testid="tenant-toggle-c-1001"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="tenant-toggle-default"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="tenant-row-default"]').text()).toContain('总部')
    expect(wrapper.text()).not.toContain('公司编码')
    expect(row.text()).not.toContain('c-1001')
    expect(row.findAll('td')).toHaveLength(7)
    expect(wrapper.find('[placeholder="请输入公司编码"]').exists()).toBe(false)
  })

  it('requires an admin account and password before creating and sends them together', async () => {
    const wrapper = mount(AdminTenantsView)
    await flushPromises()

    await wrapper.find('[data-testid="tenant-create"]').trigger('click')
    expect(wrapper.find('[data-testid="tenant-create-modal"]').text()).not.toContain('公司编码')
    await wrapper.find('[data-testid="tenant-form-name"]').setValue('华南分公司')
    await wrapper.find('[data-testid="tenant-form-submit"]').trigger('click')
    expect(mocks.message.warning).toHaveBeenCalledWith('请输入总管理员账号')
    expect(mocks.createTenant).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="tenant-form-admin"]').setValue('admin@hn')
    await wrapper.find('[data-testid="tenant-form-password"]').setValue('secret123')
    await wrapper.find('[data-testid="tenant-form-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.createTenant).toHaveBeenCalledWith({
      display_name: '华南分公司',
      admin_username: 'admin@hn',
      admin_full_name: undefined,
      admin_password: 'secret123',
      contact_name: null,
      remark: null,
    })
    expect(wrapper.find('[data-testid="tenant-create-modal"]').exists()).toBe(false)
  })

  it('confirms before disabling a company', async () => {
    const wrapper = mount(AdminTenantsView)
    await flushPromises()
    await wrapper.find('[data-testid="tenant-toggle-c-1001"]').trigger('click')
    expect(wrapper.find('[data-testid="tenant-toggle-modal"]').text()).toContain('无法登录')
    await wrapper.find('[data-testid="tenant-toggle-confirm"]').trigger('click')
    await flushPromises()
    expect(mocks.updateTenant).toHaveBeenCalledWith('t2', { display_name: '华东分公司', status: 'disabled', contact_name: '张三', remark: null })
  })

  it('按公司名称查询和重置，编辑弹层使用公司名称而不显示编码', async () => {
    const wrapper = mount(AdminTenantsView)
    await flushPromises()
    await wrapper.find('[placeholder="请输入公司名称"]').setValue('华东')
    await wrapper.find('[placeholder="请输入公司名称"]').trigger('keyup', { key: 'Enter' })
    expect(wrapper.find('[data-testid="tenant-row-default"]').exists()).toBe(false)
    const row = wrapper.find('[data-testid="tenant-row-c-1001"]')
    await row.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const modal = wrapper.find('[data-testid="tenant-edit-modal"]')
    expect(modal.text()).toContain('编辑分公司 · 华东分公司')
    expect(modal.text()).not.toContain('c-1001')
    await modal.findAll('button').find((button) => button.text() === '取消')!.trigger('click')
    await wrapper.findAll('button').find((button) => button.text() === '重置')!.trigger('click')
    expect(wrapper.find('[data-testid="tenant-row-default"]').exists()).toBe(true)
  })
  it('删除需确认，成功刷新列表，关联限制说明留在弹层并保留记录', async () => {
    const wrapper = mount(AdminTenantsView)
    await flushPromises()
    await wrapper.find('[data-testid="tenant-delete-c-1001"]').trigger('click')
    expect(mocks.deleteTenant).not.toHaveBeenCalled()
    await wrapper.find('[data-testid="tenant-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(mocks.deleteTenant).toHaveBeenCalledWith('t2')
    expect(wrapper.find('[data-testid="tenant-delete-modal"]').exists()).toBe(false)
    mocks.deleteTenant.mockRejectedValueOnce(new Error('关联 3 条历史任务，不能删除'))
    await wrapper.find('[data-testid="tenant-delete-c-1001"]').trigger('click')
    await wrapper.find('[data-testid="tenant-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="tenant-delete-modal"]').text()).toContain('关联 3 条历史任务')
    expect(wrapper.find('[data-testid="tenant-row-c-1001"]').exists()).toBe(true)
  })

})
