import { expect, type Page } from '@playwright/test'
import { departmentUser, expectBearerToken, fulfillJson, mockLogin, testUser } from './api'

const taskIds = {
  running: '00000000-0000-0000-0000-000000000301',
  done: '00000000-0000-0000-0000-000000000302',
}

export async function mockTaskCenter(page: Page, portal: 'admin' | 'workstation') {
  const user = portal === 'admin' ? { ...testUser, permissions: [...testUser.permissions, 'taskObjects:write:any'] } : departmentUser
  await mockLogin(page, user)
  await page.route('**/api/v1/auth/me', (route) => fulfillJson(route, user))
  const tasks = [
    {
      task_id: taskIds.running,
      task_name: '运行中任务的较长名称用于检查固定列横向滚动',
      status: 'running',
      progress: 99,
      available_actions: ['cancel', 'detail', 'report', 'copy'],
    },
    {
      task_id: taskIds.done,
      task_name: '可删除的已完成任务',
      status: 'done',
      progress: 100,
      available_actions: ['detail', 'report', 'rerun', 'copy', 'delete'],
    },
  ].map((task) => ({
    ...task,
    product: { department_name: '质量平台', product_code: 'E2E-001', product_name: '固定列验收产品' },
    target_url: 'https://example.test/long-path-for-task-list-display',
    target_platform: 'h5',
    execution_mode: 'full_inspection',
    journey_steps: [],
    tenant_name: '体验中心',
    submitter_name: '自动化验收员',
    report_status: 'ready',
    report_availability: 'final',
    failure_reason: null,
    created_at: '2026-09-30T01:00:00Z',
    duration_ms: 120_000,
  }))
  const requests: URL[] = []
  const deletedIds: string[] = []
  await page.route('**/api/v1/workstation/tasks?*', async (route) => {
    expectBearerToken(route)
    const url = new URL(route.request().url())
    requests.push(url)
    await fulfillJson(route, { items: tasks.filter((task) => !deletedIds.includes(task.task_id)), total: 80 - deletedIds.length, page: Number(url.searchParams.get('page') || 1), page_size: Number(url.searchParams.get('page_size') || 20) })
  })
  await page.route('**/api/v1/workstation/products', (route) => fulfillJson(route, { items: [{ project_code: 'E2E-001', product_name: '固定列验收产品' }] }))
  await page.route(/\/api\/v1\/workstation\/tasks\/[^/?]+$/, async (route) => {
    expectBearerToken(route)
    const taskId = new URL(route.request().url()).pathname.split('/').at(-1)!
    const task = tasks.find((item) => item.task_id === taskId)!
    if (route.request().method() === 'DELETE') {
      expect(portal).toBe('admin')
      expect(task.status).toBe('done')
      deletedIds.push(taskId)
      await fulfillJson(route, { task_id: taskId, status: task.status, run_id: null, message: '任务已删除' })
      return
    }
    await fulfillJson(route, { ...task, started_at: task.created_at, finished_at: null, ruleset_code: 'e2e', ruleset_version: 1, channel: 'workbench', priority: 100, run_id: 'e2e-run', scan_mode: 'full', remark: null, required_page_types: [] })
  })
  await page.route(/\/api\/v1\/tasks\/[^/?]+\/agent-run$/, (route) => fulfillJson(route, { run: null }))
  await page.route(/\/api\/v2\/tasks\/[^/?]+\/h5-shortflows$/, (route) => {
    const taskId = new URL(route.request().url()).pathname.split('/').at(-2)!
    return fulfillJson(route, { task_id: taskId, snapshot_hash: 'a'.repeat(64), plans: [] })
  })
  const detailResponses: Record<string, unknown> = {
    'stats': { total: 10, triggered: 5, frontier_pending: 0, progress_percent: 58, status_counts: {}, recent_anomalies: [] },
    'page-timeline': { nodes: [], journey_steps: [], current_node_seq: null, frontier: { pending_count: 0, blocked_count: 0, failed_count: 0, executed_count: 0, covered_page_types: [] } },
    'rule-results': { items: [] },
    'screenshots': { groups: [] },
    'logs': { items: [], total: 0 },
  }
  const otherSections = ['app-stage', 'inspection-work-items', 'page-graph', 'page-graph/recovery-route', 'evidence-gaps', 'rule-reuse', 'page-type-judgements', 'closure-outcome']
  for (const [path, response] of Object.entries(detailResponses)) {
    await page.route(new RegExp(`/api/v1/workstation/tasks/[^/]+/${path}(?:\\?.*)?$`), (route) => fulfillJson(route, response))
  }
  // 本用例只验收任务概览；未覆盖的明细模块明确返回不可用，不能访问真实接口。
  for (const path of otherSections) {
    await page.route(new RegExp(`/api/v1/workstation/tasks/[^/]+/${path}(?:\\?.*)?$`), (route) => fulfillJson(route, { detail: '本用例未提供该明细模块' }, 503))
  }
  return { requests, deletedIds, taskIds }
}

export async function loginTaskCenter(page: Page, target: string) {
  await page.goto(target)
  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByTestId('task-table').locator('tbody tr')).toHaveCount(2)
}

export async function expectTaskColumnsStayVisible(page: Page) {
  const table = page.getByTestId('task-table')
  const row = table.locator('tbody tr').first()
  const fixedCells = [row.locator('.check-col'), row.locator('.name-col'), row.locator('.action-col')]
  const before = await Promise.all(fixedCells.map((cell) => cell.boundingBox()))
  const scrollDistance = await table.locator('.table-wrap').evaluate((node) => {
    node.scrollLeft = node.scrollWidth
    return node.scrollLeft
  })
  expect(scrollDistance).toBeGreaterThan(0)
  const after = await Promise.all(fixedCells.map((cell) => cell.boundingBox()))
  for (let index = 0; index < fixedCells.length; index += 1) {
    expect(Math.abs(before[index]!.x - after[index]!.x)).toBeLessThan(1)
    await expect(fixedCells[index]!).toBeInViewport()
  }
  await expect(row.getByRole('button', { name: '取消', exact: true })).toBeInViewport()
}
