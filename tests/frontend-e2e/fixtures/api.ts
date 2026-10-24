import { expect, type Page, type Route } from '@playwright/test'

export const testUser = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'e2e-user',
  full_name: '自动化验收员',
  role: '平台管理员',
  status: 'enabled',
  permissions: [
    'dashboard',
    'users',
    'users:any',
    'tenants',
    'roles',
    'auditLogs',
    'qcRules',
    'configCenter',
    'costReports',
  ],
  tenant_id: '00000000-0000-0000-0000-000000000001',
  tenant_name: '体验中心',
}

/** 部门账号：双门户严格隔离后只能用部门质检平台（管理账号登录部门平台会被换到 /admin）。 */
export const departmentUser = {
  id: '00000000-0000-0000-0000-000000000002',
  username: 'e2e-user',
  full_name: '自动化验收员',
  role: '规则配置员',
  status: 'enabled',
  permissions: [
    'dashboard',
    'qcRules',
    'configCenter',
    'taskObjects:read:owner',
    'taskObjects:write:owner',
    'taskObjects:read:department',
    'taskObjects:review:department',
  ],
  tenant_id: '00000000-0000-0000-0000-00000000000a',
  tenant_name: '华东分公司',
  department_name: '质量平台',
}

export const testTenants = [
  {
    id: '00000000-0000-0000-0000-000000000001',
    tenant_key: 'default',
    display_name: '体验中心',
    status: 'enabled',
    contact_name: null,
    remark: null,
    admin_username: 'e2e-user',
    user_count: 1,
    department_count: 2,
    product_count: 5,
    is_headquarters: true,
    created_at: '2026-08-18T08:00:00Z',
    updated_at: '2026-08-18T08:00:00Z',
  },
  {
    id: '00000000-0000-0000-0000-00000000000a',
    tenant_key: 'east',
    display_name: '华东分公司',
    status: 'enabled',
    contact_name: null,
    remark: null,
    admin_username: 'east-admin',
    user_count: 0,
    department_count: 1,
    product_count: 3,
    is_headquarters: false,
    created_at: '2026-09-07T08:00:00Z',
    updated_at: '2026-09-07T08:00:00Z',
  },
]

export async function fulfillJson(route: Route, payload: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  })
}

/**
 * 兜底拦截：没有单独 Mock 的 /api 请求就地返回 501 并记下来，不让它穿过 Vite 代理打到本机后端。
 * 必须在其它 page.route 之前调用（Playwright 后注册的路由优先），用例结尾断言返回的列表为空。
 */
export async function blockUnmockedApi(page: Page) {
  const unmocked: string[] = []
  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      const request = route.request()
      const { pathname, search } = new URL(request.url())
      unmocked.push(`${request.method()} ${pathname}${search}`)
      await fulfillJson(route, { detail: `E2E 未 Mock 的接口：${request.method()} ${pathname}` }, 501)
    },
  )
  return unmocked
}

export async function mockLogin(page: Page, user: typeof testUser | typeof departmentUser = testUser) {
  await page.route('**/api/v1/auth/login', async (route) => {
    const request = route.request()
    expect(request.method()).toBe('POST')
    expect(request.postDataJSON()).toEqual({
      username: 'e2e-user',
      password: 'e2e-password',
    })
    await fulfillJson(route, {
      token: 'e2e-access-token',
      user,
      expires_at: '2099-01-01T00:00:00Z',
    })
  })
}

export function expectBearerToken(route: Route) {
  expect(route.request().headers().authorization).toBe('Bearer e2e-access-token')
}
