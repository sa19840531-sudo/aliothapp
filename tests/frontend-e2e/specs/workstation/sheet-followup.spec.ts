import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test'
import {
  assertSheetReadIsolation, attachSheetFollowupAudit, listTasks, longTask, mockId, mockSheetFollowup,
  queuedTasks, reportItems, sheetCatalog, shotIds, simulatedNow,
} from '../../fixtures/sheet-followup'

test.use({ viewport: { width: 1440, height: 1000 } })
test.afterEach(async ({ page }, info) => { await attachSheetFollowupAudit(page, info) })

async function screenshot(page: Page, info: TestInfo, name: string) {
  await page.screenshot({ path: info.outputPath(name), fullPage: true, animations: 'disabled' })
}

async function choose(page: Page, control: Locator, name: string) {
  await control.click()
  await page.locator('.n-base-select-option').filter({ has: page.getByText(name, { exact: true }) }).click()
}

test('明确模拟22/23：部门筛选、三期原卡片以及准确版本明细', async ({ page }, info) => {
  const audit = await mockSheetFollowup(page)
  await page.goto('/rules')
  await expect(page.getByTestId('rule-center-rules').locator('tbody tr')).toHaveCount(72)
  await choose(page, page.getByTestId('rule-center-severity'), '高')
  await choose(page, page.getByTestId('rule-center-source'), '电子表格')
  await choose(page, page.getByTestId('rule-center-page'), '首页')
  const table = page.getByTestId('rule-center-rules')
  await expect(table.locator('tbody tr')).toHaveCount(7)
  await expect(table).toContainText('模拟1期规则1广告规范')
  await expect(table).not.toContainText('模拟2期')
  await screenshot(page, info, '01-department-rule-filters.png')
  await page.getByTestId('rule-center-keyword').fill('规则13')
  await expect(table.locator('tbody tr')).toHaveCount(1)
  await expect(table).toContainText('模拟1期规则13广告规范')
  await page.getByTestId('rule-center-reset').click()
  await expect(table.locator('tbody tr')).toHaveCount(72)
  await page.getByTestId('rule-center-tab-sets').click()
  for (const [index, ruleset] of sheetCatalog.rulesets.entries()) {
    await page.getByTestId(`rule-center-ruleset-${ruleset.ruleset_id}`).click()
    const detail = page.getByTestId('rule-center-ruleset-detail')
    await expect(detail.locator('.rc-ruleset-rules > li')).toHaveCount(ruleset.enabled_rule_count)
    await expect(detail).toContainText(`模拟第${index + 1}期第1条检查内容`)
    await expect(detail).not.toContainText(`模拟第${index === 0 ? 2 : 1}期第1条检查内容`)
    if (index === 0) await screenshot(page, info, '02-department-ruleset-detail.png')
    await page.keyboard.press('Escape')
    await expect(detail).not.toBeVisible()
  }
  assertSheetReadIsolation(audit)
  await info.attach('mock-request-audit', { body: JSON.stringify(audit, null, 2), contentType: 'application/json' })
})

test('明确模拟23：同编码不同版本卡片不借用最新内容', async ({ page }, info) => {
  const oldSet = { ...sheetCatalog.rulesets[0]!, enabled_rule_count: 1 }
  const newSet = { ...oldSet, ruleset_id: mockId(799), version: 2 }
  const first = sheetCatalog.rules[0]!
  const audit = await mockSheetFollowup(page, { catalog: {
    ...sheetCatalog, rulesets: [oldSet, newSet], rules: [
      { ...first, name: '模拟原期专属规则' },
      { ...first, rule_id: mockId(1799), ruleset_id: newSet.ruleset_id, ruleset_version: 2, name: '模拟新版专属规则' },
    ],
  } })
  await page.goto('/rules')
  await page.getByTestId('rule-center-tab-sets').click()
  await page.getByTestId(`rule-center-ruleset-${oldSet.ruleset_id}`).click()
  await expect(page.getByTestId('rule-center-ruleset-detail')).toContainText('模拟原期专属规则')
  await expect(page.getByTestId('rule-center-ruleset-detail')).not.toContainText('模拟新版专属规则')
  await screenshot(page, info, '03-exact-ruleset-version.png')
  assertSheetReadIsolation(audit)
  await info.attach('mock-request-audit', { body: JSON.stringify(audit, null, 2), contentType: 'application/json' })
})

test('明确模拟21：待确认21、未完整预览、正式报告导出与批量门禁', async ({ page }, info) => {
  const audit = await mockSheetFollowup(page)
  await page.goto('/reports')
  const table = page.getByTestId('report-table')
  await expect(table.locator('tbody tr')).toHaveCount(5)
  const states = ['待人工确认（21）', '质检未完整完成', '已完成', '生成失败', '生成中']
  for (const [index, item] of reportItems.entries()) {
    const row = table.locator('tbody tr').filter({ hasText: item.task_name })
    await expect(row.getByTestId('report-state')).toHaveText(states[index]!)
    if (index < 3) await expect(row.getByRole('button', { name: '在线查看', exact: true })).toBeEnabled()
    else await expect(row.getByRole('button', { name: '在线查看', exact: true })).toBeDisabled()
    if (index === 2) {
      await expect(row.getByRole('button', { name: '导出', exact: true })).toBeEnabled()
      await expect(row.getByRole('checkbox')).toBeEnabled()
    } else {
      await expect(row.getByRole('button', { name: '导出', exact: true })).toBeDisabled()
      await expect(row.getByRole('checkbox')).toBeDisabled()
    }
  }
  await table.locator('thead input[type=checkbox]').check()
  await expect(table.locator('tbody input:checked')).toHaveCount(1)
  await expect(table).toContainText('已选 1 项')
  await screenshot(page, info, '04-report-flow-and-export-gate.png')
  const finalRow = table.locator('tbody tr').filter({ hasText: reportItems[2]!.task_name })
  const oneDownload = page.waitForEvent('download')
  await finalRow.getByRole('button', { name: '导出', exact: true }).click()
  expect((await oneDownload).suggestedFilename()).toBe(`task_${mockId(3002)}_report.html`)
  const batchDownload = page.waitForEvent('download')
  await page.getByTestId('report-export-batch').click()
  expect((await batchDownload).suggestedFilename()).toBe('质检报告.zip')
  await choose(page, page.getByTestId('report-filter-status'), '已生成（含待确认）')
  await page.getByTestId('report-search').click()
  await expect(table.locator('tbody tr')).toHaveCount(3)
  expect(audit.requests.some((request) => request.path === '/api/v1/workstation/reports' && request.query.includes('report_status=ready'))).toBe(true)
  assertSheetReadIsolation(audit)
  await info.attach('mock-request-audit', { body: JSON.stringify(audit, null, 2), contentType: 'application/json' })
})

test('明确模拟25/31：全部状态名称进入详情保留第二页，取消保持固定槽位', async ({ page }, info) => {
  const audit = await mockSheetFollowup(page)
  const target = '/tasks?page=2&page_size=20&submitter_name=模拟验收员'
  await page.goto(target)
  const table = page.getByTestId('task-table')
  await expect(table.locator('tbody tr')).toHaveCount(7)
  const queued = table.locator('tbody tr').filter({ hasText: listTasks[0]!.task_name })
  const running = table.locator('tbody tr').filter({ hasText: listTasks[1]!.task_name })
  const buttons = [queued, running].map((row) => row.locator('.cancel-slot').getByRole('button', { name: '取消', exact: true }))
  const before = await Promise.all(buttons.map((button) => button.boundingBox()))
  expect(Math.abs(before[0]!.x - before[1]!.x)).toBeLessThan(1)
  const scrollLeft = await table.locator('.table-wrap').evaluate((element) => {
    element.scrollLeft = element.scrollWidth
    return element.scrollLeft
  })
  expect(scrollLeft).toBeGreaterThan(0)
  const after = await Promise.all(buttons.map((button) => button.boundingBox()))
  expect(Math.abs(after[0]!.x - after[1]!.x)).toBeLessThan(1)
  for (const [index, button] of buttons.entries()) {
    expect(Math.abs(after[index]!.x - before[index]!.x)).toBeLessThan(1)
    await expect(button).toBeInViewport()
  }
  await info.attach('browser-measurements', { body: JSON.stringify({ before, after, scrollLeft }, null, 2), contentType: 'application/json' })
  await screenshot(page, info, '05-task-cancel-fixed-column.png')
  for (const task of listTasks) {
    await table.getByRole('button', { name: task.task_name, exact: true }).click()
    await expect(page.locator('.progress-meta strong')).toBeVisible()
    expect(new URL(page.url()).pathname).toBe(`/tasks/${task.task_id}`)
    expect(new URL(page.url()).searchParams.get('page')).toBe('2')
    expect(new URL(page.url()).searchParams.get('submitter_name')).toBe('模拟验收员')
    await page.getByRole('button', { name: '任务中心', exact: true }).click()
    await expect(table.locator('tbody tr')).toHaveCount(7)
    expect(new URL(page.url()).searchParams.get('page')).toBe('2')
  }
  assertSheetReadIsolation(audit)
  await info.attach('mock-request-audit', { body: JSON.stringify(audit, null, 2), contentType: 'application/json' })
})

test('明确模拟34/39：42小时预警不自动操作，合集放大缩放、端点及返回', async ({ page }, info) => {
  await page.clock.setFixedTime(new Date(simulatedNow))
  const audit = await mockSheetFollowup(page)
  await page.goto(`/tasks/${longTask.task_id}`)
  const alert = page.getByTestId('long-execution-alert')
  await expect(alert).toContainText('任务持续执行已超过 2.5 小时（42:30:00）')
  await expect(alert.getByRole('button', { name: '取消任务', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: '重新执行', exact: true })).toHaveCount(0)
  await screenshot(page, info, '06-task-long-execution-warning.png')
  await page.getByTestId('screenshot-collection-card').first().click()
  const viewer = page.getByTestId('screenshot-viewer')
  await expect(viewer.getByTestId('collection-grid')).toBeVisible()
  await expect(viewer.getByTestId('collection-frame')).toHaveCount(3)
  expect(await viewer.getByTestId('collection-frame').evaluateAll((frames) =>
    frames.map((frame) => frame.getAttribute('data-artifact-id')))).toEqual(shotIds)
  await screenshot(page, info, '07-task-screenshot-collection.png')
  await viewer.getByTestId('collection-frame').first().click()
  const image = viewer.getByTestId('viewer-image').locator('img')
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(900)
  await expect(viewer.getByTestId('screenshot-zoom')).toHaveText('100%')
  await expect(viewer).toContainText('1 / 3')
  await expect(viewer.getByTestId('viewer-prev')).toBeDisabled()
  await expect(viewer.getByTestId('viewer-next')).toBeEnabled()
  // 图片自然尺寸先可读，load 回调与弹窗入场随后完成；基准尺寸必须等待实际适配落稳。
  await expect.poll(() => image.evaluate((node) => {
    const element = node as HTMLImageElement
    return element.style.width.endsWith('px') ? Math.abs(element.getBoundingClientRect().width - parseFloat(element.style.width)) : 100
  })).toBeLessThan(1)
  const fittedWidth = (await image.boundingBox())!.width
  await viewer.getByRole('button', { name: '放大截图', exact: true }).click()
  await viewer.getByRole('button', { name: '放大截图', exact: true }).click()
  await expect(viewer.getByTestId('screenshot-zoom')).toHaveText('150%')
  const zoomedWidth = (await image.boundingBox())!.width
  expect(Math.abs(zoomedWidth - fittedWidth * 1.5)).toBeLessThan(1)
  await screenshot(page, info, '07-task-screenshot-zoom-150.png')
  await viewer.getByRole('button', { name: '缩小截图', exact: true }).click()
  await expect(viewer.getByTestId('screenshot-zoom')).toHaveText('125%')
  const reducedWidth = (await image.boundingBox())!.width
  expect(Math.abs(reducedWidth - fittedWidth * 1.25)).toBeLessThan(1)
  await viewer.getByRole('button', { name: '重置截图缩放', exact: true }).click()
  await expect(viewer.getByTestId('screenshot-zoom')).toHaveText('100%')
  const resetWidth = (await image.boundingBox())!.width
  expect(Math.abs(resetWidth - fittedWidth)).toBeLessThan(1)
  await info.attach('browser-measurements', { body: JSON.stringify({ fittedWidth, zoomedWidth, reducedWidth, resetWidth }, null, 2), contentType: 'application/json' })
  await viewer.getByRole('button', { name: '放大截图', exact: true }).click()
  await viewer.getByTestId('viewer-next').click()
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(640)
  await expect(viewer.getByTestId('screenshot-zoom')).toHaveText('100%')
  await expect(image).toHaveAttribute('src', new RegExp(shotIds[1]!))
  await expect(viewer).toContainText('2 / 3')
  await screenshot(page, info, '08-task-folded-frame-fit.png')
  await viewer.getByTestId('viewer-next').click()
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(1200)
  await expect(viewer).toContainText('3 / 3')
  await expect(viewer.getByTestId('viewer-next')).toBeDisabled()
  await page.keyboard.press('ArrowRight')
  await expect(image).toHaveAttribute('src', new RegExp(shotIds[2]!))
  await viewer.getByTestId('viewer-back').click()
  await expect(viewer.getByTestId('collection-grid')).toBeVisible()
  await expect(viewer.getByTestId('collection-frame')).toHaveCount(3)
  assertSheetReadIsolation(audit)
  await info.attach('mock-request-audit', { body: JSON.stringify(audit, null, 2), contentType: 'application/json' })
})

for (const task of queuedTasks) {
  test(`明确模拟8/29：${task.target_platform}只显示对应队列事实和在线实例`, async ({ page }, info) => {
    const audit = await mockSheetFollowup(page)
    await page.goto(`/tasks/${task.task_id}`)
    const state = page.getByTestId('web-execution-queue-state')
    await expect(state).toContainText('队列位置 2 · 前方任务 1 · 在线适配 Worker 3')
    await expect(state).toContainText('在线心跳不能证明有空闲执行槽')
    await expect(page.getByTestId('long-execution-alert')).toHaveCount(0)
    await screenshot(page, info, `09-${task.target_platform}-queue-read-state.png`)
    assertSheetReadIsolation(audit)
    await info.attach('mock-request-audit', { body: JSON.stringify(audit, null, 2), contentType: 'application/json' })
  })
}
