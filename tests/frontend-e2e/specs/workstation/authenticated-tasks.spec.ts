import { expect, test } from '@playwright/test'
import { blockUnmockedApi } from '../../fixtures/api'
import { expectTaskColumnsStayVisible, loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

test('工作台横向滚动保留任务和操作，浏览器返回恢复筛选和第 3 页', async ({ page }, testInfo) => {
  const unmocked = await blockUnmockedApi(page)
  const { requests } = await mockTaskCenter(page, 'workstation')
  await loginTaskCenter(page, '/tasks?page=3&page_size=10&submitter_name=自动化验收员')
  await expectTaskColumnsStayVisible(page)
  await expect(page.getByTestId('task-table').getByRole('button', { name: '删除', exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('工作台任务列表固定列.png'), fullPage: true, animations: 'disabled' })
  await page.getByTestId('task-table').locator('tbody tr').first().getByRole('button', { name: '查看详情', exact: true }).click()
  await expect(page.locator('.progress-meta strong')).toHaveText('99%')
  await page.goBack()
  await expect(page.getByTestId('task-filter-submitter')).toHaveValue('自动化验收员')
  expect(requests.at(-1)!.searchParams.get('page')).toBe('3')
  expect(requests.at(-1)!.searchParams.get('page_size')).toBe('10')
  expect(unmocked).toEqual([])
})
