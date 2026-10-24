import { expect, test } from '@playwright/test'
import {
  blockUnmockedApi,
  expectBearerToken,
  fulfillJson,
  mockLogin,
  testTenants,
} from '../../fixtures/api'

test('登录后返回账号与权限并加载账号数据', async ({ page }) => {
  const unmocked = await blockUnmockedApi(page)
  await mockLogin(page)
  await page.route('**/api/v1/tenants', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, { items: testTenants })
  })
  await page.route('**/api/v1/users?*', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      items: [
        {
          id: '00000000-0000-0000-0000-000000000001',
          username: 'e2e-user',
          full_name: '自动化验收员',
          role: '平台管理员',
          status: 'enabled',
          tenant_id: '00000000-0000-0000-0000-000000000001',
          tenant_name: '体验中心',
          department_name: null,
          last_login_at: null,
          data_scope: '全部公司',
          created_at: '2026-08-05T08:00:00Z',
          updated_at: '2026-08-05T08:00:00Z',
        },
        {
          id: '00000000-0000-0000-0000-000000000002',
          username: 'east-admin',
          full_name: '华东管理员',
          role: '公司管理员',
          status: 'enabled',
          tenant_id: '00000000-0000-0000-0000-00000000000a',
          tenant_name: '华东分公司',
          department_name: null,
          last_login_at: null,
          data_scope: '本公司全部部门',
          created_at: '2026-09-07T08:00:00Z',
          updated_at: '2026-09-07T08:00:00Z',
        },
      ],
      total: 2,
      page: 1,
      page_size: 10,
    })
  })

  await page.goto('/admin/users')
  await expect(page).toHaveURL(/\/admin\/login\?redirect=\/admin\/users$/)

  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录' }).click()

  await expect(page).toHaveURL(/\/admin\/users$/)
  await expect(page.getByTestId('admin-crumbs')).toHaveText(/组织管理\s*\/\s*账号与权限/)
  await expect(page.getByRole('heading', { name: '账号列表' })).toBeVisible()
  await expect(page.getByRole('button', { name: '创建账号' })).toBeVisible()

  // 总部视角：公司筛选能选全部公司（展开后可输入文字搜索），列表带「所属公司」「数据权限范围」列，两家公司的账号都能看到。
  const tenantFilter = page.getByTestId('user-filter-tenant')
  await expect(tenantFilter).toContainText('全部公司')
  await tenantFilter.click()
  const tenantOptions = page.locator('.n-base-select-option')
  await expect(tenantOptions).toHaveText(['全部公司', '体验中心', '华东分公司'])
  await page.keyboard.type('华东')
  await expect(tenantOptions).toHaveText(['华东分公司'])
  await page.keyboard.press('Escape')
  await expect(tenantOptions.first()).toBeHidden()
  await expect(page.getByRole('columnheader', { name: '所属公司' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '数据权限范围' })).toBeVisible()
  await expect(page.getByTestId('user-row-e2e-user').getByRole('cell')).toHaveText([
    'e2e-user',
    '自动化验收员',
    '运营中心账号（平台管理员）',
    '体验中心',
    '—',
    '全部公司',
    '从未登录',
    '启用',
    /编辑\s*\|\s*停用\s*\|\s*删除/,
  ])
  await expect(page.getByTestId('user-row-east-admin').getByRole('cell')).toHaveText([
    'east-admin',
    '华东管理员',
    '分公司总管理员（公司管理员）',
    '华东分公司',
    '—',
    '本公司全部部门',
    '从未登录',
    '启用',
    /编辑\s*\|\s*停用\s*\|\s*删除/,
  ])
  await expect(page.getByTestId('user-table')).toContainText('共 2 个账号')

  expect(unmocked).toEqual([])
})
