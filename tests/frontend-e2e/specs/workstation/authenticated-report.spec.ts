import { expect, test } from '@playwright/test'
import { blockUnmockedApi, departmentUser, fulfillJson, mockLogin } from '../../fixtures/api'

test('报告说明与规则清单、执行缺口、间距和截图实际缩放', async ({ page }, testInfo) => {
  const unmocked = await blockUnmockedApi(page)
  await mockLogin(page, departmentUser)
  await page.route('**/api/v1/auth/me', (route) => fulfillJson(route, departmentUser))
  const taskId = '00000000-0000-0000-0000-000000000501'
  const baseRule = {
    execution_id: 'failed-execution', rule_id: 'failed-rule', rule_code: 'failed-rule',
    rule_name: '广告关闭入口检查', category: 'ad', rule_package: '广告通用规范',
    ruleset_code: 'phase-1', ruleset_name: '1期质检规则集', trigger_display: '出现广告时',
    bound_page_type: 'home', bound_page_type_name: '首页', display_status: 'failed',
    display_status_label: '未通过', machine_display_status: 'failed', machine_display_status_label: '未通过',
    execution_status: 'fail', severity: 'high', severity_display: 'P1', confidence: 0.9,
    result_summary: '未发现可操作关闭入口', problem_desc: '广告遮挡正文', evidence_artifact_ids: ['report-shot'],
    page_capture_id: 'capture-1', is_bootstrap: false, finished_at: '2026-09-30T01:02:00Z', confirmable: false,
    human_verdict: null, human_verdict_label: null, human_verdict_note: null, human_verdict_at: null, confirm_status: null,
  }
  const skipped = { ...baseRule, execution_id: 'skipped-execution', rule_id: 'skipped-rule', rule_code: 'skipped-rule',
    rule_name: '未执行的规则', display_status: 'other', display_status_label: '其他',
    machine_display_status: 'not_executed', machine_display_status_label: '未执行', execution_status: 'skipped', execution_reason: 'candidate_not_executed',
    evidence_artifact_ids: [], result_summary: '本轮未完成该检测', problem_desc: null }
  // 未执行占位与真实判定缺证分开，只有后者计入实际次数和检测缺口。
  const insufficient = { ...baseRule, execution_id: 'insufficient-execution', rule_id: 'insufficient-rule', rule_code: 'insufficient-rule',
    rule_name: '内容截图完整性检查', display_status: 'other', display_status_label: '其他',
    machine_display_status: 'evidence_insufficient', machine_display_status_label: '证据不足', execution_status: 'evidence_insufficient',
    evidence_artifact_ids: [], result_summary: '已进行判定，缺少完整截图证据', problem_desc: null }
  await page.route(`**/api/v1/workstation/reports/${taskId}/insight`, (route) => fulfillJson(route, {
    task_id: taskId, summary: { task_id: taskId, task_name: '报告交互验收', target_url: 'https://example.test', target_platform: 'app',
      task_status: 'done', report_stage: 'final_review', page_recognized_count: 1, total_rules: 2, qc_success_rate: 50,
      pass_rate: 0, full_rule_pass_rate: 0, candidate_execution_coverage: 33.3, confirmed_not_applicable_rules: 0,
      coverage_gap_rules: 0, run_id: 'run-1', ruleset_code: 'phase-1', ruleset_version: 1, ruleset_rule_total: 3,
      started_at: '2026-09-30T01:00:00Z', finished_at: '2026-09-30T01:02:00Z', created_at: '2026-09-30T01:00:00Z', generated_at: '2026-09-30T01:02:00Z' },
    page_exploration: { page_results: [], page_total: 1 }, distribution: [], rule_items: [baseRule, skipped, insufficient], confirm_items: [],
    confirm_progress: { total: 0, confirmed: 0, pending: 0, completed: true }, export_ready: true, qc_success_rate: 50, pass_rate: 0,
  }))
  await page.route(`**/api/v1/workstation/tasks/${taskId}`, (route) => fulfillJson(route, {
    task_id: taskId, task_name: '报告交互验收', target_platform: 'app', status: 'done', report_status: 'ready',
    product: { department_name: '质量平台', product_code: 'E2E', product_name: '报告验收产品' },
  }))
  await page.route(`**/api/v1/workstation/tasks/${taskId}/screenshots`, (route) => fulfillJson(route, {
    task_id: taskId, run_id: 'run-1', groups: [{ page_group_key: 'home', page_display_name: '首页', page_type: 'home', page_type_name: '首页',
      items: [{ artifact_id: 'report-shot', page_capture_id: 'capture-1', captured_at: '2026-09-30T01:00:00Z', content_type: 'image/svg+xml', related_rules: [] }] }],
  }))
  await page.route('**/api/v1/evidence/report-shot/ticket', (route) => fulfillJson(route, { url: '/api/v1/evidence/report-shot/file', expires_at: new Date(Date.now() + 60_000).toISOString() }))
  await page.route('**/api/v1/evidence/report-shot/file', (route) => route.fulfill({ contentType: 'image/svg+xml',
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1400"><rect width="900" height="1400" fill="#d9f0eb"/><rect x="40" y="90" width="820" height="900" rx="24" fill="#fff"/><text x="90" y="200" font-size="55">Report screenshot</text></svg>' }))
  await page.goto(`/reports?task=${taskId}&page=3&page_size=10`)
  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByTestId('rule-table')).toBeVisible()
  await expect(page.getByTestId('execution-gap-note')).toHaveText('有 1 次检测未形成结论，详见列表执行状态；人工确认完成不代表这些检测缺口已解决。')
  await expect(page.getByTestId('summary-rule-accounting')).toHaveText('规则总数 3 = 实际使用 2 + 未实际使用 1。')
  await expect(page.getByTestId('task-info')).toContainText('2 条规则 · 用了 2 次')
  await expect(page.getByTestId('phase-rate')).toContainText('50%')
  await expect(page.getByTestId('confirm-progress')).toHaveText('无需人工确认')
  await expect(page.getByTestId('report-state')).toHaveText('需复核')
  await expect(page.getByTestId('report-state')).toHaveClass(/tone-warning/)
  await expect(page.getByTestId('task-info')).toContainText('需复核')
  await expect(page.getByTestId('export-report')).toHaveText('导出复核报告')
  const skippedRow = page.getByTestId('rule-row-skipped-rule')
  await expect(skippedRow.getByTestId('final-result')).toHaveText('未形成结论')
  await expect(skippedRow.getByTestId('ai-result')).toHaveText('未进行 AI 判定')
  await expect(skippedRow.getByTestId('rule-rate')).toContainText('0/0 次')
  await expect(page.getByTestId('rule-row-insufficient-rule').getByTestId('rule-rate')).toContainText('0/1 次')
  expect(await page.locator('.phase-list').evaluate((node) => getComputedStyle(node).paddingTop)).toBe('16px')
  await page.screenshot({ path: testInfo.outputPath('报告执行缺口与列表间距.png'), fullPage: true, animations: 'disabled' })
  await page.getByTestId('status-help-failed').click()
  await expect(page.getByTestId('status-help')).toContainText('规则检查未通过')
  await expect(page.getByTestId('category-dialog')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('规则结果状态说明.png'), fullPage: true, animations: 'disabled' })
  await page.getByTestId('summary-view-rules').click()
  await expect(page.getByTestId('category-dialog')).toContainText('未通过 · 1 条规则')
  await page.getByTestId('category-dialog').getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByTestId('rule-row-failed-rule').locator('.evidence-thumb').click()
  const viewer = page.getByTestId('shot-viewer')
  const image = viewer.locator('.shot-stage img')
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(900)
  await viewer.getByTitle('缩小', { exact: true }).click()
  await viewer.getByTitle('缩小', { exact: true }).click()
  for (const zoom of [0.5, 0.75, 1, 1.5, 2, 3]) {
    await expect(viewer.locator('.zoom-label')).toHaveText(`${zoom * 100}%`)
    expect(Math.abs((await image.boundingBox())!.width - 900 * zoom)).toBeLessThan(1)
    if (zoom !== 3) await viewer.getByTitle('放大', { exact: true }).click()
  }
  await page.screenshot({ path: testInfo.outputPath('报告截图300倍百分比.png'), fullPage: true, animations: 'disabled' })
  await viewer.getByTitle('适应窗口', { exact: true }).click()
  await expect(viewer.locator('.zoom-label')).toHaveText('适应窗口')
  const fitted = (await image.boundingBox())!
  const stage = (await viewer.locator('.shot-stage').boundingBox())!
  expect(fitted.width).toBeLessThanOrEqual(stage.width)
  expect(fitted.height).toBeLessThanOrEqual(stage.height)
  await page.screenshot({ path: testInfo.outputPath('报告截图适应窗口.png'), fullPage: true, animations: 'disabled' })
  expect(unmocked).toEqual([])
})
