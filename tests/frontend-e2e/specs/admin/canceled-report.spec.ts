import { expect, test, type Page } from '@playwright/test'
import type { WorkstationTaskListItem } from '../../../../app/workstation/src/types/task'
import {
  blockUnmockedApi,
  expectBearerToken,
  fulfillJson,
  mockLogin,
  testUser,
} from '../../fixtures/api'

test.use({ viewport: { width: 1920, height: 1080 } })

function mockTask(
  identity: number,
  name: string,
  status: 'canceled' | 'running' | 'canceling',
  reportStatus: WorkstationTaskListItem['report_status'],
  availability: WorkstationTaskListItem['report_availability'] = 'none',
): WorkstationTaskListItem {
  return {
    task_id: `00000000-0000-0000-0000-${String(identity).padStart(12, '0')}`,
    task_name: name,
    product: { department_name: '模拟质检部', product_code: 'MOCK-REPORT', product_name: '模拟报告状态产品' },
    target_url: 'https://example.test/report-status',
    target_platform: 'h5',
    execution_mode: 'full_inspection',
    journey_steps: [],
    status,
    progress: status === 'canceled' ? 95 : 58,
    report_status: reportStatus,
    report_availability: availability,
    failure_reason: null,
    available_actions: ['detail'],
    created_at: '2026-09-30T01:00:00Z',
    tenant_name: '体验中心',
    submitter_name: '自动化验收员',
    duration_ms: 120_000,
  }
}

async function openMockTaskList(page: Page, tasks: WorkstationTaskListItem[]) {
  const unmocked = await blockUnmockedApi(page)
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'warning' || message.type() === 'error') {
      consoleErrors.push(`${message.type()}: ${message.text()}`)
    }
  })
  await mockLogin(page)
  await page.route('**/api/v1/auth/me', (route) => fulfillJson(route, testUser))
  await page.route('**/api/v1/workstation/tasks?*', async (route) => {
    expectBearerToken(route)
    expect(route.request().method()).toBe('GET')
    await fulfillJson(route, { items: tasks, total: tasks.length, page: 1, page_size: 20 })
  })
  await page.route('**/api/v1/workstation/products', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, { items: [] })
  })

  await page.goto('/admin/tasks')
  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  const table = page.getByTestId('task-table')
  await expect(table.locator('tbody tr')).toHaveCount(tasks.length)
  await table.locator('.table-wrap').evaluate((node) => { node.scrollLeft = node.scrollWidth })
  return { table, unmocked, pageErrors, consoleErrors }
}

test('明确模拟：已取消任务的旧生成中报告显示未生成', async ({ page }, testInfo) => {
  const task = mockTask(401, '模拟已取消且无报告', 'canceled', 'generating')
  const { table, unmocked, pageErrors, consoleErrors } = await openMockTaskList(page, [task])
  const row = table.locator('tbody tr').filter({ hasText: task.task_name })
  await expect(row.getByRole('cell', { name: '已取消', exact: true })).toBeVisible()
  await expect(row.getByRole('cell', { name: '未生成', exact: true })).toBeVisible()
  await expect(row.getByRole('cell', { name: '生成中', exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('01-已取消旧生成态.png'), fullPage: true, animations: 'disabled' })
  expect(unmocked).toEqual([])
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('明确模拟：取消保留已有预览、正式报告和生成失败事实', async ({ page }, testInfo) => {
  const cases = [
    { task: mockTask(402, '模拟已取消保留报告预览', 'canceled', 'generating', 'preview'), label: '报告预览' },
    { task: mockTask(403, '模拟已取消保留正式报告', 'canceled', 'generating', 'final'), label: '正式报告' },
    { task: mockTask(404, '模拟已取消保留ready', 'canceled', 'ready'), label: '已生成' },
    { task: mockTask(405, '模拟已取消保留generated', 'canceled', 'generated'), label: '已生成' },
    { task: mockTask(406, '模拟已取消保留生成失败', 'canceled', 'failed'), label: '生成失败' },
  ]
  const { table, unmocked, pageErrors, consoleErrors } = await openMockTaskList(page, cases.map((item) => item.task))
  for (const { task, label } of cases) {
    const row = table.locator('tbody tr').filter({ hasText: task.task_name })
    await expect(row.getByRole('cell', { name: label, exact: true })).toBeVisible()
    await expect(row.getByRole('cell', { name: '未生成', exact: true })).toHaveCount(0)
  }
  await page.screenshot({ path: testInfo.outputPath('02-已有报告与失败事实.png'), fullPage: true, animations: 'disabled' })
  expect(unmocked).toEqual([])
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('明确模拟：执行中与取消中的任务仍显示报告生成中', async ({ page }, testInfo) => {
  const tasks = [
    mockTask(407, '模拟执行中报告仍生成', 'running', 'generating'),
    mockTask(408, '模拟取消中报告仍生成', 'canceling', 'generating'),
  ]
  const { table, unmocked, pageErrors, consoleErrors } = await openMockTaskList(page, tasks)
  for (const task of tasks) {
    const row = table.locator('tbody tr').filter({ hasText: task.task_name })
    await expect(row.getByRole('cell', { name: '生成中', exact: true })).toBeVisible()
    await expect(row.getByRole('cell', { name: '未生成', exact: true })).toHaveCount(0)
  }
  await page.screenshot({ path: testInfo.outputPath('03-执行中与取消中.png'), fullPage: true, animations: 'disabled' })
  expect(unmocked).toEqual([])
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})
