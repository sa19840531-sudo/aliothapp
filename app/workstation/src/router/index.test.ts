import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RouteRecordRaw } from 'vue-router'
import { authStore } from '@/stores/auth'

vi.mock('@/layouts/WorkstationLayout.vue', () => ({ default: { name: 'WorkstationLayout', template: '<div />' } }))
vi.mock('@/layouts/AdminLayout.vue', () => ({ default: { name: 'AdminLayout', template: '<div />' } }))

vi.mock('./routes', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./routes')>()
  const view = { name: 'RouterGuardView', render: () => null }
  // 保留真实路径、权限、重定向与嵌套关系，只隔离导航时的页面懒加载。
  function isolateViews(record: RouteRecordRaw): RouteRecordRaw {
    return {
      ...record,
      ...('component' in record && record.component ? { component: view } : {}),
      ...(record.children ? { children: record.children.map(isolateViews) } : {}),
    } as RouteRecordRaw
  }
  return { ...actual, routes: actual.routes.map(isolateViews) }
})

const { default: router } = await import('./index')

function signIn(permissions: string[] | null) {
  if (!permissions) {
    authStore.session.value = null
    return
  }
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: { id: 'u1', username: 'user', full_name: '用户', role: '规则配置员', status: 'enabled', permissions, tenant_id: 't1', tenant_name: '北京分公司' },
  }
}

const HQ = ['dashboard', 'users', 'users:any', 'tenants', 'departments', 'roles', 'analytics', 'system', 'costReports', 'taskObjects:read:any']
const COMPANY_ADMIN = ['dashboard', 'users', 'departments', 'roles', 'qcRules', 'taskObjects:read:tenant']
const DEPARTMENT_USER = ['dashboard', 'qcRules', 'configCenter', 'taskObjects:read:owner', 'taskObjects:write:owner']

describe('router guard', () => {
  beforeEach(async () => {
    signIn(null)
    await router.push('/login')
  })

  it('keeps both login pages reachable without a session and remembers where to go back', async () => {
    await router.push('/admin/login')
    expect(router.currentRoute.value.path).toBe('/admin/login')

    await router.push('/admin/users')
    expect(router.currentRoute.value.name).toBe('adminLogin')
    expect(router.currentRoute.value.query.redirect).toBe('/admin/users')

    await router.push('/tasks')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/tasks')
  })

  it('sends a department account back to the department portal even when it types /admin', async () => {
    signIn(DEPARTMENT_USER)
    await router.push('/admin')
    expect(router.currentRoute.value.path).toBe('/')
    await router.push('/admin/tasks')
    expect(router.currentRoute.value.path).toBe('/')
    await router.push('/admin/login')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('lets a company admin into the admin portal but not into operations-centre-only pages', async () => {
    signIn(COMPANY_ADMIN)
    await router.push('/admin/departments')
    expect(router.currentRoute.value.path).toBe('/admin/departments')
    await router.push('/admin/analytics')
    expect(router.currentRoute.value.path).toBe('/admin')
    await router.push('/admin/tasks/abc')
    expect(router.currentRoute.value.name).toBe('admin:taskDetail')
    await router.push('/admin/login')
    expect(router.currentRoute.value.path).toBe('/admin')
  })

  it('keeps management accounts out of the department portal by sending them to the admin twin page', async () => {
    signIn(HQ)
    await router.push('/')
    expect(router.currentRoute.value.path).toBe('/admin')
    await router.push('/tasks/abc?tab=rules')
    expect(router.currentRoute.value.fullPath).toBe('/admin/tasks/abc?tab=rules')
    signIn(COMPANY_ADMIN)
    await router.push('/products')
    expect(router.currentRoute.value.path).toBe('/admin/products')
    await router.push('/login')
    expect(router.currentRoute.value.path).toBe('/admin')
  })

  it('keeps the department portal free of config and rule-test pages', async () => {
    signIn(DEPARTMENT_USER)
    await router.push('/rules')
    expect(router.currentRoute.value.name).toBe('ruleCenter')
    expect(router.resolve('/config').matched).toHaveLength(0)
    expect(router.resolve('/rule-test').matched).toHaveLength(0)
  })

  it('folds the old admin rule-test address into the rule config test tab', async () => {
    signIn([...HQ, 'qcRules', 'qcRules:write'])
    await router.push('/admin/rule-test?task=t-1')
    expect(router.currentRoute.value.path).toBe('/admin/rules')
    expect(router.currentRoute.value.query).toEqual({ task: 't-1', tab: 'test' })
  })

  it('opens every 2.1 admin page for the operations centre', async () => {
    signIn(HQ)
    for (const path of ['/admin', '/admin/analytics', '/admin/tenants', '/admin/departments', '/admin/users', '/admin/token-usage', '/admin/system', '/admin/products/import', '/admin/reports']) {
      await router.push(path)
      expect(router.currentRoute.value.path).toBe(path)
    }
  })
})
