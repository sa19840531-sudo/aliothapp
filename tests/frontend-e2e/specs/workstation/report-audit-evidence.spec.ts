import { expect, test } from '../../fixtures/browser-telemetry'
import { blockUnmockedApi, departmentUser, fulfillJson, mockLogin } from '../../fixtures/api'

test('模拟报告显示人工审计与原引用多证据，不把缺失材料或未执行补成结果', async ({ page }, testInfo) => {
  const unmocked = await blockUnmockedApi(page)
  const browserProblems: string[] = []
  page.on('pageerror', (error) => browserProblems.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'warning' || message.type() === 'error') browserProblems.push(message.text())
  })
  await mockLogin(page, departmentUser)
  await page.route('**/api/v1/auth/me', (route) => fulfillJson(route, departmentUser))
  const taskId = '00000000-0000-0000-0000-000000000601'
  const reviewerId = '00000000-0000-0000-0000-000000000602'
  const confirmed = {
    execution_id: 'audit-confirmed', rule_id: 'audit-rule', rule_code: 'audit-rule',
    rule_name: '模拟广告关闭入口检查', category: 'ad', rule_package: '广告通用规范',
    ruleset_code: 'phase-1', ruleset_name: '1期质检规则集', trigger_display: '出现广告时',
    bound_page_type: 'home', bound_page_type_name: '首页', display_status: 'failed', display_status_label: '未通过',
    machine_display_status: 'manual_review', machine_display_status_label: '待人工复核',
    execution_status: 'manual_review', severity: 'high', severity_display: 'P1', confidence: 0.5,
    result_summary: '关闭入口遮挡，待复核', problem_desc: '模拟原始检测事实',
    evidence_artifact_ids: ['audit-before', 'missing-original', 'audit-after'],
    page_capture_id: 'capture-1', is_bootstrap: false, finished_at: '2026-01-01T00:00:00Z',
    confirmable: true, human_verdict: 'fail', human_verdict_label: '不通过',
    human_verdict_note: '模拟备注：依据已有前后两张截图确认', human_verdict_by: reviewerId,
    human_verdict_at: '2026-09-30T01:02:00Z', confirm_status: 'confirmed',
  }
  const unexecuted = { ...confirmed, execution_id: 'audit-unexecuted', rule_id: 'unexecuted-rule', rule_code: 'unexecuted-rule',
    rule_name: '模拟未执行规则', display_status: 'other', display_status_label: '其他',
    machine_display_status: 'not_executed', machine_display_status_label: '未执行', execution_status: 'skipped',
    execution_reason: 'candidate_not_executed', evidence_artifact_ids: [], result_summary: '本次未进入判定',
    confirmable: false, confirm_status: null }
  await page.route(`**/api/v1/workstation/reports/${taskId}/insight`, (route) => fulfillJson(route, {
    task_id: taskId, report_availability: 'preview', export_allowed: true,
    summary: { task_id: taskId, task_name: '模拟报告人工审计验收', target_url: 'https://example.test',
      target_platform: 'h5', task_status: 'review', report_stage: 'final_review', page_recognized_count: 1,
      total_rules: 2, qc_success_rate: 50, pass_rate: 0, full_rule_pass_rate: 0, candidate_execution_coverage: 50,
      confirmed_not_applicable_rules: 0, coverage_gap_rules: 0, run_id: 'run-1', ruleset_code: 'phase-1',
      ruleset_version: 1, ruleset_rule_total: 2, started_at: '2026-09-30T01:00:00Z',
      finished_at: '2026-09-30T01:02:00Z', created_at: '2026-09-30T01:00:00Z', generated_at: '2026-09-30T01:02:00Z' },
    page_exploration: { page_results: [], page_total: 1 }, distribution: [], rule_items: [{ ...confirmed,
      evidence_input_set_id: 'controlled-input', journey_result: {
        execution_id: confirmed.execution_id, input_set_id: 'controlled-input',
        execution_plan_id: 'controlled-plan', scenario_key: 'pause', execution_status: 'evidence_insufficient',
        checkpoints: [{ checkpoint_id: 'pause', status: 'fail', checks: { paused: false }, missing: [], integrity: 'bound',
          bindings: ['before', 'after'].map((role) => ({ role: `${role}_action`, capture_id: `capture-${role}`,
            action_execution_id: 'controlled-action', manifest_id: `manifest-${role}`, manifest_version: 2,
            artifact_ids: [`audit-${role}`] })) },
          { checkpoint_id: 'stable', status: 'evidence_insufficient', checks: {}, missing: ['measurement:stable_frame'], integrity: 'missing', bindings: [] }],
      } }, unexecuted],
    confirm_items: [confirmed], confirm_progress: { total: 1, confirmed: 1, pending: 0, completed: true },
    export_ready: true, qc_success_rate: 50, pass_rate: 0,
  }))
  await page.route(`**/api/v1/workstation/tasks/${taskId}`, (route) => fulfillJson(route, {
    task_id: taskId, task_name: '模拟报告人工审计验收', target_platform: 'h5', status: 'review', report_status: 'preview',
    product: { department_name: '质量平台', product_code: 'E2E', product_name: '模拟验收产品' },
  }))
  await page.route(`**/api/v1/workstation/tasks/${taskId}/screenshots`, (route) => fulfillJson(route, {
    task_id: taskId, run_id: 'run-1', groups: [{ page_group_key: 'home', page_display_name: '首页',
      page_type: 'home', page_type_name: '首页', items: ['audit-before', 'audit-after'].map((artifactId) => ({
        artifact_id: artifactId, page_capture_id: 'capture-1', captured_at: '2026-09-30T01:00:00Z',
        content_type: 'image/svg+xml', related_rules: [],
      })) }],
  }))
  for (const id of ['audit-before', 'audit-after']) {
    await page.route(`**/api/v1/evidence/${id}/ticket`, (route) => fulfillJson(route, { url: `/evidence/${id}/file?ticket=mock-evidence-ticket`, expires_at: new Date(Date.now() + 60_000).toISOString() }))
    await page.route(`**/api/v1/evidence/${id}/file?ticket=mock-evidence-ticket`, (route) => route.fulfill({ contentType: 'image/svg+xml',
      body: `<svg xmlns="http://www.w3.org/2000/svg" width="700" height="950"><rect width="700" height="950" fill="${id === 'audit-before' ? '#d9f0eb' : '#dce6f9'}"/><text x="50" y="200" font-size="40">Mock ${id}</text></svg>` }))
  }
  await page.goto(`/reports?task=${taskId}`)
  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByTestId('confirm-audit-audit-confirmed')).toContainText(reviewerId)
  await expect(page.getByTestId('confirm-audit-audit-confirmed')).toContainText('2026/9/30')
  await expect(page.getByTestId('confirm-section')).not.toContainText('模拟未执行规则')
  await expect(page.getByTestId('rule-row-unexecuted-rule').getByTestId('final-result')).toHaveText('未形成结论')
  await page.screenshot({ path: testInfo.outputPath('模拟报告人工确认账号与时间.png'), fullPage: true, animations: 'disabled' })
  await page.getByTestId('rule-detail-audit-rule').click()
  const dialog = page.getByTestId('rule-usage-dialog')
  await expect(dialog.getByTestId('rule-confirm-audit-audit-confirmed')).toContainText(reviewerId)
  await expect(dialog.getByTestId('rule-missing-shots-audit-confirmed')).toContainText('missing-original')
  await expect(dialog.locator('.usage-shot')).toHaveCount(2)
  await expect.poll(() => dialog.locator('.usage-shot img').evaluateAll((images) => images.every((img) => (img as HTMLImageElement).naturalWidth === 700))).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('模拟逐次规则多证据与缺失原标识.png'), fullPage: true, animations: 'disabled' })
  await expect(dialog.getByTestId('journey-result-details')).toContainText('measurement:stable_frame')
  await dialog.getByTestId('checkpoint-shot-audit-after').click()
  await expect(page.getByTestId('shot-viewer').locator('.shot-stage img')).toHaveAttribute('src', '/api/v1/evidence/audit-after/file?ticket=mock-evidence-ticket')
  await expect.poll(() => page.getByTestId('shot-viewer').locator('.shot-stage img').evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBe(700)
  expect(unmocked).toEqual([])
  expect(browserProblems).toEqual([])
})
