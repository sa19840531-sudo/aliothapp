import { expect, test } from '../../fixtures/browser-telemetry'
import { blockUnmockedApi, fulfillJson } from '../../fixtures/api'
import { loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

for (const state of [
  { title: '自动执行结束', runStatus: 'done', finalizationState: 'sealed', progress: 100, ended: true },
  { title: '会话仍在排空', runStatus: 'running', finalizationState: 'session_draining', progress: 72, ended: false },
]) {
  test(`明确模拟：${state.title}，独立保留 92 项规则检查缺口且不冒充通过`, async ({ page }, testInfo) => {
    const unmocked = await blockUnmockedApi(page)
    const { taskIds } = await mockTaskCenter(page, 'workstation')
    await loginTaskCenter(page, '/tasks')
    const detailPath = `/api/v1/workstation/tasks/${taskIds.running}`
    const responsePromise = page.waitForResponse((response) => new URL(response.url()).pathname === detailPath)
    await page.getByTestId('task-table').locator('tbody tr').first().getByRole('button', { name: '查看详情', exact: true }).click()
    const detail = await (await responsePromise).json()

    // 使用线上脱敏检查分布，任务执行进度与规则检查缺口独立展示。
    await page.route(`**${detailPath}`, (route) => fulfillJson(route, {
      ...detail,
      status: 'review',
      run_status: state.runStatus,
      execution_status: 'needs_review',
      finalization_state: state.finalizationState,
      finished_at: state.ended ? '2026-10-08T09:36:29Z' : null,
      progress: state.progress,
      report_availability: 'preview',
      completion_gate_passed: false,
      available_actions: ['detail', 'report', 'rerun', 'copy'],
    }))
    await page.route(`**${detailPath}/stats`, (route) => fulfillJson(route, {
      total: 208,
      triggered: 188,
      finished: 116,
      frontier_pending: 0,
      progress_percent: state.progress,
      status_counts: { passed: 87, failed: 11, not_applicable: 18, evidence_insufficient: 69, detect_error: 3, not_executed: 20 },
      recent_anomalies: [],
    }))
    await page.reload()
    const progress = page.locator('.progress-block')
    await expect(progress.locator('.progress-meta')).toContainText('任务进度')
    await expect(progress.locator('.progress-meta strong')).toHaveText(`${state.progress}%`)
    const ruleProgress = progress.locator('.progress-footnote:not([data-testid="h5-progress-explanation"])')
    await expect(ruleProgress).toHaveCount(1)
    await expect(ruleProgress).toContainText('规则已完成 116/208 项')
    await expect(ruleProgress).toContainText('规则检查未完成 92 项')
    await expect(progress.getByTestId('h5-progress-explanation')).toContainText('待复核不代表质检通过')
    await expect(progress.getByTestId('h5-progress-explanation')).toContainText('证据不足或执行异常的缺口独立保留')
    await expect(progress.locator('.progress-review-note')).toContainText(state.ended ? '自动执行已结束，待复核' : '已进入复核')
    if (!state.ended) await expect(progress.locator('.progress-review-note')).not.toContainText('自动执行已结束')
    await expect(progress.locator('.progress-review-note')).toContainText('规则检查缺口已保留在结果中。')
    await page.screenshot({ path: testInfo.outputPath('待复核任务执行进度与规则缺口.png'), fullPage: true, animations: 'disabled' })
    expect(unmocked).toEqual([])
  })
}
