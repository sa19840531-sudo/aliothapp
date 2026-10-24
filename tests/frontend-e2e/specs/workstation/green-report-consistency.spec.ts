import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import type { ReportRuleItem } from '../../../../app/workstation/src/types/report-insight'
import type { GreenRuleResultProjection } from '../../../../app/workstation/src/types/task-insight'
import { blockUnmockedApi, expectBearerToken, fulfillJson } from '../../fixtures/api'
import { loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

const imageId = '00000000-0000-0000-0000-000000007981'
const otherImageId = '00000000-0000-0000-0000-000000007982'
const captureId = '00000000-0000-0000-0000-000000007983'

function greenItem(key: '32' | 's35' | 's72' | 's73'): ReportRuleItem {
  const failed = key === '32'
  const insufficient = key === 's35'
  const unlinked = key === 's73'
  const state = failed ? 'fail' : insufficient ? 'evidence_insufficient' : 'pass'
  const green: GreenRuleResultProjection = {
    schema_version: 'green-rule-result.v1', rule_key: key, rule_contract_hash: 'c'.repeat(64),
    linkage_status: unlinked ? 'evidence_changed' : 'linked',
    linkage_notice: unlinked ? '正式原件关联已变化，暂不沿用旧结论' : null,
    execution_status: unlinked ? null : state, review_status: unlinked ? null : 'pending',
    review_reason: unlinked ? null : '正式复核仍待处理',
    missing_evidence: failed ? ['initial_navigation_state'] : insufficient ? ['complete_interactive_layout'] : [],
    subitems: unlinked ? [] : [
      { key: failed ? 'equal_width_navigation' : insufficient ? 'correct_response' : 'empty_state_meaning',
        status: failed ? 'fail' : 'pass', evaluated: true, confidence: 1, reason: '后端保存的受控分项结论',
        missing_evidence: [], evidence_artifact_ids: ['controlled-ui'], screenshot_artifact_ids: [imageId], capture_ids: [captureId] },
      ...(failed || insufficient ? [{ key: failed ? 'initial_next_label' : 'usable_layout',
        status: 'evidence_insufficient', evaluated: false, confidence: null, reason: '分项尚缺必要材料',
        missing_evidence: [failed ? 'initial_navigation_state' : 'complete_interactive_layout'],
        evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [] }] : []),
    ],
  }
  return {
    item_type: 'execution', execution_id: `report-green-${key}`, rule_id: `rule-${key}`,
    rule_code: `alioth_green_${key}`, rule_name: `受控绿色规则${key}`, category: 'visual',
    rule_package: '受控第4版', ruleset_code: 'green-v4', ruleset_name: '受控绿色规则集', trigger_display: '正式冻结输入',
    bound_page_type: failed ? 'home' : null, bound_page_type_name: failed ? '首页' : null,
    display_status: failed ? 'failed' : 'other', display_status_label: failed ? '未通过' : unlinked ? '关联待核对' : '其他',
    machine_display_status: failed ? 'failed' : insufficient ? 'evidence_insufficient' : unlinked ? 'passed' : 'manual_review',
    machine_display_status_label: failed ? '未通过' : insufficient ? '证据不足' : unlinked ? '通过' : '待人工复核',
    execution_status: state, confidence: 1, severity: 'medium', severity_display: 'P2',
    result_summary: '明确标记的受控报告数据，不代表产品验收', problem_desc: null,
    evidence_artifact_ids: [imageId, otherImageId], page_capture_id: failed ? captureId : null,
    is_bootstrap: false, finished_at: '2026-10-19T08:00:00Z', confirmable: false,
    human_verdict: unlinked || key === 's72' ? 'pass' : null,
    human_verdict_label: unlinked || key === 's72' ? '通过' : null,
    human_verdict_note: null, human_verdict_at: null, confirm_status: null, green_result: green,
  }
}

async function setup(page: Page, sealed: boolean) {
  const browserProblems: string[] = []
  page.on('pageerror', (error) => browserProblems.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'warning' || message.type() === 'error') browserProblems.push(message.text())
  })
  const unmocked = await blockUnmockedApi(page)
  const { taskIds } = await mockTaskCenter(page, 'workstation')
  const taskId = taskIds.running
  let invalidated = false
  const ruleResultReads: string[] = []
  page.on('request', (request) => {
    if (request.url().includes('/rule-results')) ruleResultReads.push(request.url())
  })
  const json = async (path: string, value: unknown) => page.route(`**${path}`, (route) => {
    expect(route.request().method()).toBe('GET')
    expectBearerToken(route)
    return fulfillJson(route, value)
  })
  await page.route(`**/api/v1/workstation/reports/${taskId}/insight`, (route) => {
    expect(route.request().method()).toBe('GET')
    expectBearerToken(route)
    const items = sealed ? [
      { ...greenItem('32'), execution_id: 'frozen-fail', rule_id: 'frozen-fail', rule_code: 'legacy-fail',
        rule_name: '封存的失败结果', green_result: undefined, human_verdict: null },
      { ...greenItem('s72'), execution_id: 'frozen-pass', rule_id: 'frozen-pass', rule_code: 'legacy-pass',
        rule_name: '封存的通过结果', green_result: undefined, display_status: 'passed' as const,
        display_status_label: '通过', machine_display_status: 'passed' as const, machine_display_status_label: '通过', human_verdict: null },
    ] : [greenItem('32'), greenItem('s35'), greenItem('s72'), greenItem('s73')]
    if (!sealed && invalidated) {
      const first = items[0]!
      first.display_status = 'other'
      first.display_status_label = '关联待核对'
      Object.assign(first.green_result!, { linkage_status: 'evidence_changed', linkage_notice: '正式原件关联已变化，暂不沿用旧结论',
        execution_status: null, review_status: null, review_reason: null, missing_evidence: [], subitems: [] })
    }
    return fulfillJson(route, {
      task_id: taskId, report_availability: sealed ? 'final' : 'preview', is_final: sealed,
      export_allowed: sealed, export_ready: sealed,
      ...(!sealed ? { green_results_incomplete: true, green_results_notice: '仍有规则缺证、待复核或关联待核对，尚未形成完整结论' } : {}),
      summary: { task_id: taskId, task_name: sealed ? '受控封存报告保留验证' : '受控绿色报告口径验证',
        target_url: 'https://controlled.example.test/', target_platform: 'h5', task_status: sealed ? 'done' : 'running',
        report_stage: sealed ? 'final' : 'preview', page_recognized_count: 1, total_rules: items.length,
        qc_success_rate: sealed ? 100 : 0, pass_rate: sealed ? 50 : 0, full_rule_pass_rate: sealed ? 50 : 0,
        candidate_execution_coverage: sealed ? 100 : 0, confirmed_not_applicable_rules: 0, coverage_gap_rules: 0,
        run_id: 'controlled-report-run', ruleset_code: 'green-v4', ruleset_version: 4, ruleset_rule_total: items.length,
        started_at: '2026-10-19T08:00:00Z', finished_at: sealed ? '2026-10-19T08:01:00Z' : null,
        created_at: '2026-10-19T08:00:00Z', generated_at: '2026-10-19T08:01:00Z' },
      page_exploration: { page_results: [], page_total: 1 }, distribution: [], rule_items: items, confirm_items: [],
      confirm_progress: { total: 0, confirmed: 0, pending: 0, completed: sealed },
      qc_success_rate: sealed ? 100 : 0, pass_rate: sealed ? 50 : 0,
    })
  })
  await json(`/api/v1/workstation/tasks/${taskId}`, {
    task_id: taskId, task_name: sealed ? '受控封存报告保留验证' : '受控绿色报告口径验证', target_platform: 'h5',
    status: sealed ? 'done' : 'running', report_status: 'ready', run_id: 'controlled-report-run',
    product: { department_name: '质量平台', product_code: 'CONTROLLED', product_name: '受控展示夹具' },
  })
  await json(`/api/v1/workstation/tasks/${taskId}/screenshots`, { task_id: taskId, run_id: 'controlled-report-run',
    groups: [{ page_group_key: 'home', page_display_name: '首页', page_type: 'home', page_type_name: '首页',
      items: [imageId, otherImageId].map((artifact_id) => ({ artifact_id, page_capture_id: captureId,
        content_type: 'image/png', captured_at: '2026-10-19T08:00:00Z', related_rules: [] })) }] })
  for (const id of [imageId, otherImageId]) {
    await page.route(`**/api/v1/evidence/${id}/ticket`, (route) => {
      expectBearerToken(route)
      return fulfillJson(route, { url: `/evidence/${id}/file?ticket=controlled-report`, expires_at: '2099-01-01T00:00:00Z' })
    })
    await page.route(`**/api/v1/evidence/${id}/file?ticket=controlled-report`, (route) => route.fulfill({
      contentType: 'image/png', body: readFileSync(new URL('../../fixtures/green-rule-original.png', import.meta.url)),
    }))
  }
  await loginTaskCenter(page, '/tasks')
  await page.goto(`/reports?task=${taskId}`)
  return { unmocked, ruleResultReads, browserProblems, invalidate: () => { invalidated = true } }
}

for (const width of [1280, 390]) {
  test(`明确模拟未封存绿色报告${width}：缺证、当前复核、失联撤图及导出阻断`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const { unmocked, ruleResultReads, browserProblems, invalidate } = await setup(page, false)
    if (width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
    await expect(page.getByTestId('rule-table')).toBeVisible()
    await expect(page.getByTestId('export-report')).toBeDisabled()
    await expect(page.getByTestId('phase-rate')).toContainText('0%')
    await expect(page.getByTestId('rule-row-alioth_green_s72').getByTestId('final-result')).not.toHaveText('通过')
    await expect(page.getByTestId('rule-row-alioth_green_s73').getByTestId('final-result')).not.toHaveText('通过')
    await page.getByTestId('rule-detail-alioth_green_s35').click()
    let card = page.getByTestId('green-rule-details')
    await expect(card.getByTestId('green-rule-status')).toHaveText('证据不足 · 待人工复核')
    await expect(card).toContainText('缺少完整的交互布局材料')
    await expect(card.getByTestId('green-subitem-status').first()).toHaveText('机器通过 · 待人工复核')
    await page.keyboard.press('Escape')
    await page.getByTestId('rule-detail-alioth_green_s73').click()
    card = page.getByTestId('green-rule-details')
    await expect(card.getByTestId('green-rule-status')).toHaveText('关联待核对')
    await expect(card.getByTestId('green-subitem')).toHaveCount(0)
    await expect(page.getByTestId('rule-usage-dialog').locator('.usage-shot')).toHaveCount(0)
    await page.keyboard.press('Escape')
    await page.getByTestId('rule-detail-alioth_green_32').click()
    card = page.getByTestId('green-rule-details')
    await expect(card.getByTestId('green-rule-status')).toHaveText('未通过 · 待人工复核')
    await expect(card).toContainText('缺少导航初始状态的证据')
    const box = (await card.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(width)
    await page.locator('.rule-usage-modal').screenshot({ path: info.outputPath(`green-report-details-${width}.png`) })
    await card.getByTestId('green-view-evidence').first().click()
    const viewer = page.getByTestId('shot-viewer')
    await expect(viewer).toBeVisible()
    await expect.poll(() => viewer.locator('.shot-stage img').evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(360)
    await expect(viewer.getByTestId('viewer-next')).toBeDisabled()
    invalidate()
    await expect(viewer).toBeHidden({ timeout: 12000 })
    await expect(card.getByTestId('green-rule-status')).toHaveText('关联待核对')
    await expect(card.getByTestId('green-view-evidence')).toHaveCount(0)
    await expect(page.getByTestId('export-report')).toBeDisabled()
    expect(ruleResultReads).toEqual([])
    expect(unmocked).toEqual([])
    expect(browserProblems).toEqual([])
  })

  test(`明确模拟已封存报告${width}：旧结论、统计与原图保持原展示`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const { unmocked, ruleResultReads, browserProblems } = await setup(page, true)
    if (width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
    await expect(page.getByTestId('rule-table')).toBeVisible()
    await expect(page.getByTestId('phase-rate')).toContainText('100%')
    await expect(page.getByTestId('rule-row-legacy-pass').getByTestId('final-result')).toHaveText('通过')
    await expect(page.getByTestId('rule-row-legacy-fail').getByTestId('final-result')).toHaveText('未通过')
    await expect(page.getByTestId('green-rule-details')).toHaveCount(0)
    await expect(page.getByTestId('export-report')).toBeEnabled()
    await page.getByTestId('rule-detail-legacy-fail').click()
    await expect(page.getByTestId('rule-usage-dialog').locator('.usage-shot')).toHaveCount(2)
    await expect(page.getByTestId('green-rule-details')).toHaveCount(0)
    await page.locator('.rule-usage-modal').screenshot({ path: info.outputPath(`frozen-report-unchanged-${width}.png`) })
    expect(ruleResultReads).toEqual([])
    expect(unmocked).toEqual([])
    expect(browserProblems).toEqual([])
  })
}
