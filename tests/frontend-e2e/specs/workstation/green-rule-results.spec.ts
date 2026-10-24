import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import type { GreenRuleResultProjection, RuleResultItem } from '../../../../app/workstation/src/types/task-insight'
import { blockUnmockedApi, expectBearerToken, fulfillJson } from '../../fixtures/api'
import { loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

const imageId = '00000000-0000-0000-0000-000000007901'
const captureId = '00000000-0000-0000-0000-000000007902'
const unusedImageId = '00000000-0000-0000-0000-000000007903'

function projection(key: '32' | 's35'): GreenRuleResultProjection {
  const navigation = key === '32'
  return {
    schema_version: 'green-rule-result.v1', rule_key: key, rule_contract_hash: 'a'.repeat(64),
    linkage_status: 'linked', linkage_notice: null,
    execution_status: navigation ? 'fail' : 'evidence_insufficient', review_status: 'pending',
    review_reason: '缺少必要材料：navigation:1:initial_navigation_state',
    missing_evidence: navigation ? ['navigation:1:initial_navigation_state'] : ['complete_interactive_layout'],
    subitems: [
      { key: navigation ? 'equal_width_navigation' : 'correct_response', status: navigation ? 'fail' : 'pass',
        evaluated: true, confidence: 1, reason: navigation ? '完整同级导航槽位宽度不同' : '同一正式展示动作已有预期响应',
        missing_evidence: [], evidence_artifact_ids: ['controlled-ui', 'controlled-dom'],
        screenshot_artifact_ids: [imageId], capture_ids: [captureId] },
      { key: navigation ? 'initial_next_label' : 'usable_layout', status: 'evidence_insufficient',
        evaluated: false, confidence: null, reason: '所需材料尚未完整提供',
        missing_evidence: navigation ? ['navigation:1:next_label_text_bounds'] : ['occlusion_effect_on_use'],
        evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [],
        evidence_notice: '此分项尚无已核验的关联原图' },
    ],
  }
}

function result(key: '32' | 's35'): RuleResultItem {
  const navigation = key === '32'
  return {
    item_type: 'execution', execution_id: `controlled-green-${key}`, rule_id: `controlled-rule-${key}`,
    rule_code: `alioth_green_${key}`, rule_name: navigation ? '同级分类导航检查' : '交互响应与稳定性检查',
    category: 'visual', rule_package: '受控绿色第4版', trigger_display: '已有正式材料',
    bound_page_type: navigation ? 'home' : null, bound_page_type_name: navigation ? '首页' : null,
    applicable_page_types: ['home'], display_status: navigation ? 'failed' : 'evidence_insufficient',
    display_status_label: navigation ? '未通过' : '证据不足', machine_display_status: navigation ? 'failed' : 'evidence_insufficient',
    machine_display_status_label: navigation ? '未通过' : '证据不足', execution_status: navigation ? 'fail' : 'evidence_insufficient',
    judge_strategy: 'hybrid', confidence: navigation ? 1 : null, severity: 'medium', severity_display: 'P2',
    result_summary: '明确标注的受控展示数据，不代表产品验收', problem_desc: null, review_status: 'pending',
    human_verdict: null, human_verdict_label: null, human_verdict_note: null, human_verdict_at: null,
    confirmable: false, confirm_status: null, evidence_artifact_ids: [imageId, unusedImageId],
    page_capture_id: navigation ? captureId : null, is_bootstrap: false, finished_at: '2026-10-19T08:00:00Z',
    green_result: projection(key),
  }
}

for (const width of [1280, 390]) {
  test(`绿色分项${width}：页面32、整体S35、缺证复核及精确原图`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const unmocked = await blockUnmockedApi(page)
    const { taskIds } = await mockTaskCenter(page, 'workstation')
    const root = `/api/v1/workstation/tasks/${taskIds.running}`
    let mismatch = false
    const json = async (path: string, value: unknown) => page.route(`**${path}`, (route) => {
      expect(route.request().method()).toBe('GET')
      expectBearerToken(route)
      return fulfillJson(route, value)
    })
    await json(root, {
      task_id: taskIds.running, task_name: '受控绿色分项展示验证', status: 'running', progress: 100,
      target_url: 'https://controlled.example.test/', target_platform: 'h5', execution_mode: 'full_inspection',
      product: { department_name: '质量平台', product_name: '受控展示夹具' }, journey_steps: [],
      report_status: 'ready', report_availability: 'preview', available_actions: ['detail', 'report'],
      run_id: 'controlled-green-run', ruleset_code: 'green', ruleset_version: 4, channel: 'workbench', priority: 100,
      created_at: '2026-10-19T08:00:00Z', started_at: '2026-10-19T08:00:00Z', finished_at: null, required_page_types: [],
    })
    await json(`${root}/page-timeline`, {
      task_id: taskIds.running, run_id: 'controlled-green-run', journey_steps: [], exploration_actions: [],
      nodes: [{ node_seq: 1, source: 'discovered', page_capture_id: captureId, page_type: 'home', page_type_name: '首页',
        page_state: 'stable', status: 'recognized', status_label: '已识别', is_current: false,
        url: 'https://controlled.example.test/', captured_at: '2026-10-19T08:00:01Z',
        detection: { status: 'recognized', confidence: 1 }, inspection: { status: 'completed' },
        rule_progress: { executed: 1, total: 1 }, result_rollup: 'failed', result_rollup_label: '未通过',
        result_counts: { failed: 1 }, attention_items: [] }],
    })
    await page.route(`**${root}/rule-results`, (route) => {
      const items = [result('32'), result('s35')]
      if (mismatch) {
        Object.assign(items[0]!.green_result!, {
          linkage_status: 'evidence_changed', linkage_notice: '原件关联已变化，暂不显示旧结论和原图',
          execution_status: null, subitems: [], missing_evidence: [],
        })
        Object.assign(items[0]!, { display_status: 'manual_review', display_status_label: '关联待核对', confirmable: false })
      }
      return fulfillJson(route, { task_id: taskIds.running, total: 2, items, status_counts: {} })
    })
    await json(`${root}/screenshots`, { task_id: taskIds.running, run_id: 'controlled-green-run', groups: [{
      page_group_key: 'controlled-home', page_title: '首页', page_display_name: '首页', page_type: 'home', page_type_name: '首页', total: 2,
      items: [imageId, unusedImageId].map((artifact_id) => ({ artifact_id, page_capture_id: captureId,
        content_type: 'image/png', size_bytes: 1000, duplicate_count: 1, related_rules: [],
        captured_at: '2026-10-19T08:00:01Z', capture_role: 'observation' })),
    }] })
    for (const id of [imageId, unusedImageId]) {
      await page.route(`**/api/v1/evidence/${id}/ticket`, (route) => {
        expectBearerToken(route)
        return fulfillJson(route, { url: `/evidence/${id}/file?ticket=controlled-green`, expires_at: '2099-01-01T00:00:00Z' })
      })
      await page.route(`**/api/v1/evidence/${id}/file?ticket=controlled-green`, (route) => route.fulfill({
        contentType: 'image/png', body: readFileSync(new URL('../../fixtures/green-rule-original.png', import.meta.url)),
      }))
    }

    await loginTaskCenter(page, '/tasks')
    await page.goto(`/tasks/${taskIds.running}`)
    if (width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
    await page.getByTestId('node-detail-1').click()
    let details = page.getByTestId('green-rule-details')
    await expect(details.getByTestId('green-rule-status')).toHaveText('未通过 · 待人工复核')
    await expect(details).toContainText('缺少导航初始状态的证据')
    await expect(details).toContainText('尚未判定')
    await expect(details).not.toContainText('initial_navigation_state')
    await expect(details.getByTestId('green-view-evidence')).toHaveCount(1)
    await details.screenshot({ path: info.outputPath(`green-32-${width}.png`) })
    const box = (await details.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(width)
    await details.getByTestId('green-view-evidence').click()
    const viewer = page.getByTestId('screenshot-viewer')
    await expect(viewer).toBeVisible()
    await expect.poll(() => viewer.getByTestId('viewer-image').locator('img').evaluate((node) =>
      (node as HTMLImageElement).naturalWidth)).toBe(360)
    await expect(viewer.getByTestId('viewer-next')).toBeDisabled()
    await viewer.screenshot({ path: info.outputPath(`green-original-${width}.png`) })
    await viewer.getByTestId('viewer-close').click()
    await page.keyboard.press('Escape')
    await page.getByTestId('task-level-detail').click()
    details = page.getByTestId('green-rule-details')
    await expect(details.getByTestId('green-rule-status')).toHaveText('证据不足 · 待人工复核')
    await expect(details.getByTestId('green-subitem-status').first()).toHaveText('机器通过 · 待人工复核')
    await expect(details).toContainText('尚未确认遮挡是否影响实际使用')
    await expect(details).not.toContainText('equal_width_navigation')
    await details.screenshot({ path: info.outputPath(`green-s35-${width}.png`) })

    await page.keyboard.press('Escape')
    await page.getByTestId('node-detail-1').click()
    await page.getByTestId('green-view-evidence').click()
    await expect(viewer).toBeVisible()
    mismatch = true
    await expect(viewer).toBeHidden({ timeout: 8000 })
    details = page.getByTestId('green-rule-details')
    await expect(details.getByTestId('green-rule-status')).toHaveText('关联待核对')
    await expect(details).toContainText('原件关联已变化')
    await expect(details.getByTestId('green-subitem')).toHaveCount(0)
    await expect(details.getByTestId('green-view-evidence')).toHaveCount(0)
    expect(unmocked).toEqual([])
  })
}
