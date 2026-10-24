import { expect, test } from '@playwright/test'
import { assertSheetReadIsolation, attachSheetFollowupAudit, listTasks, mockSheetFollowup } from '../../fixtures/sheet-followup'

test.afterEach(async ({ page }, info) => { await attachSheetFollowupAudit(page, info) })

test('稳定时序：真实详情模块未加载时，筛选定时器不能抢走名称点击导航', async ({ page }, info) => {
  const audit = await mockSheetFollowup(page)
  let requested!: () => void
  let release!: () => void
  const started = new Promise<void>((resolve) => { requested = resolve })
  const held = new Promise<void>((resolve) => { release = resolve })
  await page.route((url) => url.pathname === '/src/modules/tasks/pages/TaskDetailView.vue', async (route) => {
    requested()
    await held
    await route.continue()
  })
  try {
    await page.goto('/tasks?page=2&page_size=20&submitter_name=模拟验收员')
    const table = page.getByTestId('task-table')
    await expect(table.locator('tbody tr')).toHaveCount(7)
    await page.clock.install({ time: new Date('2026-10-17T00:00:00Z') })
    await page.clock.pauseAt(new Date('2026-10-17T00:00:01Z'))
    await page.getByTestId('task-filter-submitter').fill('新筛选')
    await table.getByRole('button', { name: listTasks[0]!.task_name, exact: true }).click()
    await started
    await page.clock.runFor(1000)
    expect(new URL(page.url()).pathname).toBe('/tasks')
    expect(audit.requests.filter((request) => request.path === '/api/v1/workstation/tasks')).toHaveLength(1)
    await page.screenshot({ path: info.outputPath('detail-module-held.png') })
    release()
    await expect(page.locator('.progress-meta strong')).toBeVisible()
    expect(new URL(page.url()).pathname).toBe(`/tasks/${listTasks[0]!.task_id}`)
    expect(new URL(page.url()).searchParams.get('page')).toBe('2')
    expect(new URL(page.url()).searchParams.get('submitter_name')).toBe('新筛选')
    assertSheetReadIsolation(audit)
  } finally {
    release()
  }
})
