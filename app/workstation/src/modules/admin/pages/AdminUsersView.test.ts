import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'

const HQ = 'tenant-hq'
const SOUTH = 'tenant-south'

const mocks = vi.hoisted(() => ({
  listUsers: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
  listTenants: vi.fn(),
  listDepartments: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('../api/users', () => ({
  listUsers: mocks.listUsers,
  createUser: mocks.createUser,
  updateUser: mocks.updateUser,
  deleteUser: mocks.deleteUser,
}))
vi.mock('../api/tenants', () => ({ listTenants: mocks.listTenants }))
vi.mock('../api/departments', () => ({ listDepartments: mocks.listDepartments }))

import AdminUsersView from './AdminUsersView.vue'

function signInHq() {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: {
      id: 'u1',
      username: 'admin',
      full_name: '管理员',
      role: '平台管理员',
      status: 'enabled',
      permissions: ['users', 'users:any', 'tenants', 'departments'],
      tenant_id: HQ,
      tenant_name: '体验中心',
    },
  }
}

async function openCreateDialog() {
  const wrapper = mount(AdminUsersView)
  await flushPromises()
  await wrapper.get('[data-testid="user-create"]').trigger('click')
  await wrapper.get('[data-testid="user-form-username"]').setValue('lichuan')
  await wrapper.get('[data-testid="user-form-name"]').setValue('立川')
  await wrapper.get('[data-testid="user-form-password"]').setValue('secret123')
  return wrapper
}

function departmentOptions(wrapper: Awaited<ReturnType<typeof openCreateDialog>>) {
  return wrapper.findAll('[data-testid="user-form-department"] option').map((node) => node.text())
}

describe('账号与权限 · 所属部门', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    signInHq()
    mocks.listUsers.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 })
    mocks.createUser.mockResolvedValue({})
    mocks.listTenants.mockResolvedValue([
      { id: HQ, display_name: '体验中心', status: 'enabled' },
      { id: SOUTH, display_name: '华南分公司', status: 'enabled' },
    ])
    mocks.listDepartments.mockResolvedValue([
      { id: 'd1', tenant_id: HQ, name: '质检一部', status: 'enabled' },
      { id: 'd2', tenant_id: HQ, name: '旧部门', status: 'disabled' },
      { id: 'd3', tenant_id: SOUTH, name: '华南一部', status: 'enabled' },
    ])
  })

  it('部门账号必须从所选公司的启用部门里选', async () => {
    const wrapper = await openCreateDialog()

    // 只列本公司启用的部门，停用的和别的公司的都不出现
    expect(departmentOptions(wrapper)).toEqual(['请选择部门', '质检一部'])

    await wrapper.get('[data-testid="user-form-submit"]').trigger('click')
    expect(mocks.message.warning).toHaveBeenCalledWith('部门账号要选择所属部门')
    expect(mocks.createUser).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="user-form-department"]').setValue('质检一部')
    await wrapper.get('[data-testid="user-form-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.createUser).toHaveBeenCalledWith(
      expect.objectContaining({ role: '规则配置员', tenant_id: HQ, department_name: '质检一部' }),
    )
  })

  it('换公司后清空已选部门，并换成新公司的部门', async () => {
    const wrapper = await openCreateDialog()
    await wrapper.get('[data-testid="user-form-department"]').setValue('质检一部')

    await wrapper.get('[data-testid="user-form-tenant"]').setValue(SOUTH)
    expect((wrapper.get('[data-testid="user-form-department"]').element as HTMLSelectElement).value).toBe('')
    expect(departmentOptions(wrapper)).toEqual(['请选择部门', '华南一部'])
  })

  it('公司级角色不显示部门，也不提交部门', async () => {
    const wrapper = await openCreateDialog()
    await wrapper.get('[data-testid="user-form-department"]').setValue('质检一部')

    await wrapper.get('[data-testid="user-form-role"]').setValue('公司管理员')
    expect(wrapper.find('[data-testid="user-form-department"]').exists()).toBe(false)

    await wrapper.get('[data-testid="user-form-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.createUser).toHaveBeenCalledWith(
      expect.objectContaining({ role: '公司管理员', department_name: null }),
    )
  })

  it('所选公司没有部门时直接说明是哪个公司，提示换公司或先去部门管理新增', async () => {
    // 部门只建在华南分公司下，弹窗默认的所属公司是体验中心
    mocks.listDepartments.mockResolvedValue([{ id: 'd3', tenant_id: SOUTH, name: '华南一部', status: 'enabled' }])
    const wrapper = await openCreateDialog()

    expect(departmentOptions(wrapper)).toEqual(['该公司还没有部门'])
    const hint = wrapper.get('[data-testid="user-form-department-empty"]').text()
    expect(hint).toContain('「体验中心」下还没有部门')
    expect(hint).toContain('部门管理')

    await wrapper.get('[data-testid="user-form-tenant"]').setValue(SOUTH)
    expect(departmentOptions(wrapper)).toEqual(['请选择部门', '华南一部'])
    expect(wrapper.find('[data-testid="user-form-department-empty"]').exists()).toBe(false)
  })
})
