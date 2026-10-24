import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'
import AdminDepartmentsView from './AdminDepartmentsView.vue'

const mocks = vi.hoisted(() => ({
  listDepartments: vi.fn(),
  createDepartment: vi.fn(),
  updateDepartment: vi.fn(),
  deleteDepartment: vi.fn(),
  listTenants: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('../api/departments', () => ({
  listDepartments: mocks.listDepartments,
  createDepartment: mocks.createDepartment,
  updateDepartment: mocks.updateDepartment,
  deleteDepartment: mocks.deleteDepartment,
}))
vi.mock('../api/tenants', () => ({ listTenants: mocks.listTenants }))

const DEPARTMENT = {
  id: 'd1', tenant_id: 't2', tenant_name: '北京分公司', name: '一部', owner_name: '小明', status: 'enabled',
  monthly_token_budget: 4000, remark: null, product_count: 2, user_count: 1, created_at: '2026-09-08T00:00:00Z', updated_at: '2026-09-08T00:00:00Z',
}

function signIn(permissions: string[], tenantId = 't1') {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: { id: 'u1', username: 'admin', full_name: '管理员', role: '平台管理员', status: 'enabled', permissions, tenant_id: tenantId, tenant_name: tenantId === 't1' ? '体验中心' : '北京分公司' },
  }
}

describe('AdminDepartmentsView', () => {
  beforeEach(() => {
    mocks.deleteDepartment.mockReset().mockResolvedValue(undefined)
    mocks.listDepartments.mockReset().mockResolvedValue([DEPARTMENT])
    mocks.listTenants.mockReset().mockResolvedValue([
      { id: 't1', display_name: '体验中心', status: 'enabled' },
      { id: 't2', display_name: '北京分公司', status: 'enabled' },
    ])
    mocks.createDepartment.mockReset().mockResolvedValue({ ...DEPARTMENT, id: 'd2', name: '二部' })
    mocks.updateDepartment.mockReset().mockResolvedValue({ ...DEPARTMENT, status: 'disabled' })
    mocks.message.error.mockReset()
  })

  it('lets the operations centre pick the company when creating a department', async () => {
    signIn(['dashboard', 'users', 'users:any', 'tenants', 'departments'])
    const wrapper = mount(AdminDepartmentsView)
    await flushPromises()

    expect(wrapper.find('[data-testid="department-row-一部"]').text()).toContain('¥4,000')
    await wrapper.find('[data-testid="department-create"]').trigger('click')
    await wrapper.find('[data-testid="department-form-tenant"]').setValue('t2')
    await wrapper.find('[data-testid="department-form-name"]').setValue('二部')
    await wrapper.find('[data-testid="department-form-submit"]').trigger('click')
    await flushPromises()

    expect(mocks.createDepartment).toHaveBeenCalledWith({
      name: '二部', owner_name: null, monthly_token_budget: null, remark: null, status: 'enabled', tenant_id: 't2',
    })
    expect(mocks.listDepartments).toHaveBeenCalledTimes(2)
  })

  it('pins a company admin to their own company and surfaces the disable guard error', async () => {
    signIn(['dashboard', 'users', 'departments'], 't2')
    mocks.updateDepartment.mockRejectedValueOnce(new Error('部门下还有 2 个启用产品、1 个启用账号，先转移或停用后再停用部门'))
    const wrapper = mount(AdminDepartmentsView)
    await flushPromises()

    expect(wrapper.find('[data-testid="department-filter-tenant"]').exists()).toBe(false)
    expect(mocks.listTenants).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="department-toggle-一部"]').trigger('click')
    await flushPromises()
    expect(mocks.updateDepartment).toHaveBeenCalledWith('d1', expect.objectContaining({ status: 'disabled', name: '一部' }))
    expect(mocks.message.error).toHaveBeenCalledWith(expect.stringContaining('2 个启用产品'))

    await wrapper.find('[data-testid="department-create"]').trigger('click')
    await wrapper.find('[data-testid="department-form-name"]').setValue('三部')
    await wrapper.find('[data-testid="department-form-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.createDepartment).toHaveBeenCalledWith(expect.objectContaining({ name: '三部', tenant_id: undefined }))
  })
  it('删除需确认，成功刷新列表，关联限制说明留在弹层并保留记录', async () => {
    signIn(['departments', 'users:any', 'tenants'])
    const wrapper = mount(AdminDepartmentsView)
    await flushPromises()
    await wrapper.find('[data-testid="department-delete-一部"]').trigger('click')
    expect(mocks.deleteDepartment).not.toHaveBeenCalled()
    await wrapper.find('[data-testid="department-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(mocks.deleteDepartment).toHaveBeenCalledWith('d1')
    expect(wrapper.find('[data-testid="department-delete-modal"]').exists()).toBe(false)
    mocks.deleteDepartment.mockRejectedValueOnce(new Error('关联 3 条历史任务，不能删除'))
    await wrapper.find('[data-testid="department-delete-一部"]').trigger('click')
    await wrapper.find('[data-testid="department-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="department-delete-modal"]').text()).toContain('关联 3 条历史任务')
    expect(wrapper.find('[data-testid="department-row-一部"]').exists()).toBe(true)
  })

})
