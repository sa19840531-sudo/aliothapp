import { expect, test } from '@playwright/test'
import {
  blockUnmockedApi,
  expectBearerToken,
  fulfillJson,
  mockLogin,
  testTenants,
} from '../../fixtures/api'

test('总部账号能打开分公司管理并新增分公司', async ({ page }) => {
  const unmocked = await blockUnmockedApi(page)
  await mockLogin(page)
  const tenants = [...testTenants]
  await page.route('**/api/v1/tenants', async (route) => {
    expectBearerToken(route)
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as Record<string, unknown>
      expect(body).toMatchObject({
        display_name: '华南分公司',
        admin_username: 'south-admin',
        admin_password: 'e2e-initial-password',
      })
      expect(body).not.toHaveProperty('tenant_key')
      const created = {
        id: '00000000-0000-0000-0000-00000000000b',
        tenant_key: 'co-1a2b3c4d',
        display_name: '华南分公司',
        status: 'enabled',
        contact_name: null,
        remark: null,
        admin_username: 'south-admin',
        user_count: 1,
        department_count: 0,
        product_count: 0,
        is_headquarters: false,
        created_at: '2026-09-07T09:00:00Z',
        updated_at: '2026-09-07T09:00:00Z',
      }
      tenants.push(created)
      await fulfillJson(route, created, 201)
      return
    }
    await fulfillJson(route, { items: tenants })
  })

  await page.goto('/admin/tenants')
  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录' }).click()

  await expect(page).toHaveURL(/\/admin\/tenants$/)
  await expect(page.getByRole('heading', { name: '分公司管理', exact: true })).toBeVisible()
  await expect(page.getByTestId('admin-crumbs')).toHaveText(/组织管理\s*\/\s*分公司管理/)

  // 管理后台菜单按登录返回的权限键过滤：有权限的出现，没有权限的（部门管理、使用分析、系统管理）不出现。
  for (const key of ['tenants', 'users', 'roles', 'resource']) {
    await expect(page.getByTestId(`admin-nav-${key}`)).toBeVisible()
  }
  for (const key of ['departments', 'analytics', 'system']) {
    await expect(page.getByTestId(`admin-nav-${key}`)).toHaveCount(0)
  }

  // 分公司列表：名称（总部标记）、总管理员、部门数、产品数、状态、创建时间；总部不能停用。
  await expect(page.getByRole('columnheader', { name: '公司编码', exact: true })).toHaveCount(0)
  await expect(page.getByPlaceholder('请输入公司编码')).toHaveCount(0)
  await expect(page.getByTestId('tenant-row-default').getByRole('cell')).toHaveText([
    /体验中心\s*总部/,
    'e2e-user',
    '2',
    '5',
    '启用',
    '2026/08/18',
    '编辑',
  ])
  await expect(page.getByTestId('tenant-row-east').getByRole('cell')).toHaveText([
    '华东分公司',
    'east-admin',
    '1',
    '3',
    '启用',
    '2026/09/07',
    /编辑\s*\|\s*停用/,
  ])
  await expect(page.getByTestId('tenant-toggle-default')).toHaveCount(0)
  await expect(page.getByTestId('tenant-toggle-east')).toBeVisible()

  await page.getByRole('button', { name: '新增分公司' }).click()
  const createModal = page.getByTestId('tenant-create-modal')
  await expect(createModal).not.toContainText('公司编码')
  await createModal.getByPlaceholder('例如：华东分公司').fill('华南分公司')
  await createModal.getByTestId('tenant-form-admin').fill('south-admin')
  await createModal.getByTestId('tenant-form-password').fill('e2e-initial-password')
  await createModal.getByRole('button', { name: '确认创建' }).click()

  await expect(page.getByText('分公司已创建，总管理员账号可直接登录')).toBeVisible()
  await expect(createModal).toHaveCount(0)
  await expect(page.getByTestId('tenant-row-co-1a2b3c4d')).toContainText('华南分公司')
  await expect(page.getByTestId('tenant-row-co-1a2b3c4d')).toContainText('south-admin')
  await expect(page.getByTestId('tenant-table')).toContainText('共 3 条')

  expect(unmocked).toEqual([])
})
