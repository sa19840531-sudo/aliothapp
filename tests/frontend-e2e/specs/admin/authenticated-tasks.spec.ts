import { expect, test } from '@playwright/test'
import { blockUnmockedApi } from '../../fixtures/api'
import { expectTaskColumnsStayVisible, loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

test('管理端任务固定列、发起人筛选、详情返回与单项删除确认', async ({ page }, testInfo) => {
  const unmocked = await blockUnmockedApi(page)
  const { requests, deletedIds, taskIds } = await mockTaskCenter(page, 'admin')
  await loginTaskCenter(page, '/admin/tasks?page=3&page_size=10&submitter_name=自动化验收员')
  expect(requests.at(-1)!.searchParams.get('page')).toBe('3')
  expect(requests.at(-1)!.searchParams.get('submitter_name')).toBe('自动化验收员')
  await expectTaskColumnsStayVisible(page)
  await page.screenshot({ path: testInfo.outputPath('管理端任务列表固定列.png'), fullPage: true, animations: 'disabled' })

  await page.getByTestId('task-table').locator('tbody tr').first().getByRole('button', { name: '查看详情', exact: true }).click()
  await expect(page.locator('.progress-meta strong')).toHaveText('99%')
  await page.getByRole('button', { name: '任务中心', exact: true }).click()
  await expect(page.getByTestId('task-filter-submitter')).toHaveValue('自动化验收员')
  expect(new URL(page.url()).searchParams.get('page')).toBe('3')
  expect(requests.at(-1)!.searchParams.get('page')).toBe('3')

  const completed = page.getByTestId('task-table').locator('tbody tr').filter({ hasText: '可删除的已完成任务' })
  await completed.getByRole('button', { name: '删除', exact: true }).click()
  expect(deletedIds).toEqual([])
  await expect(page.getByText('确认删除任务「可删除的已完成任务」？', { exact: false })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('管理端任务删除确认.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '保留任务', exact: true }).click()
  expect(deletedIds).toEqual([])
  await completed.getByRole('button', { name: '删除', exact: true }).click()
  await page.getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(completed).toHaveCount(0)
  expect(deletedIds).toEqual([taskIds.done])
  expect(unmocked).toEqual([])
})
