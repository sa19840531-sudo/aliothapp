import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import type { GreenRuleResultProjection, RuleResultItem } from '../../../../app/workstation/src/types/task-insight'
import { blockUnmockedApi, expectBearerToken, fulfillJson } from '../../fixtures/api'
import { loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

const captureId = '00000000-0000-0000-0000-000000007951'
const imageId = '00000000-0000-0000-0000-000000007952'

// 这些响应只验证正式缺证结果的展示，不代表三端已产生对应业务事实。
const cases = [
  { key: '102', name: '内容安全与范围分项判定', page: true,
    checks: [
      ['minor_safety', 'controlled_minor_policy'], ['political_safety', 'political_independent_evaluation'],
      ['terrorism_safety', 'controlled_terrorism_policy'], ['content_scope', 'actual_screen_coverage'],
    ] },
  { key: 's72', name: '空数据与加载失败分项判定', page: false,
    checks: [['empty_state_meaning', 'actual_empty_response'], ['load_failure_meaning', 'failure_rendering_and_recovery']] },
  { key: '41', name: 'App外链浏览器承接分项判定', page: true,
    checks: [['external_browser_handoff', 'foreground_browser_component']] },
  { key: '62', name: '广告结束与正片衔接分项判定', page: false,
    checks: [
      ['natural_end_handoff', 'natural_ad_end_event'], ['manual_close_handoff', 'formal_ad_close_action'],
      ['content_preload', 'content_preload_original'], ['presented_first_frame', 'matching_first_frame_png'],
    ] },
  { key: 's49', name: '异常原因与恢复路径分项判定', page: false,
    checks: [
      ['failure_reason', 'actual_module_fault'], ['retry_entry', 'retry_refresh_usable_target'],
      ['business_recovery', 'business_recovered_response'], ['video_line_recovery', 'trusted_video_business_identity'],
      ['account_payment_support', 'actual_account_payment_fault'], ['payment_not_interrupted', 'trusted_payment_stage'],
    ] },
  { key: 's73', name: '加载、空态与成功反馈分项判定', page: false,
    checks: [
      ['loading_feedback', 'same_module_loading_window'], ['empty_state_guidance', 'actual_empty_response'],
      ['business_success_feedback', 'trusted_business_success'],
    ] },
] as const

function missingResult(entry: typeof cases[number]): RuleResultItem {
  const subitems = entry.checks.map(([key, missing]) => ({
    key, status: 'evidence_insufficient', evaluated: false, confidence: null,
    reason: '正式材料尚不能支持此分项的业务判断', missing_evidence: [missing],
    evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [],
    evidence_notice: '此分项尚无已核验的关联原图',
  }))
  const green: GreenRuleResultProjection = {
    schema_version: 'green-rule-result.v1', rule_key: entry.key, rule_contract_hash: 'b'.repeat(64),
    linkage_status: 'linked', linkage_notice: null, execution_status: 'evidence_insufficient',
    review_status: 'pending', review_reason: '缺少必要材料与已完成的独立判读',
    missing_evidence: subitems.flatMap((item) => item.missing_evidence), subitems,
  }
  return {
    item_type: 'execution', execution_id: `controlled-missing-${entry.key}`, rule_id: `controlled-rule-${entry.key}`,
    rule_code: `alioth_green_${entry.key}`, rule_name: entry.name,
    category: 'visual', rule_package: '受控绿色第4版', trigger_display: '已有正式输入',
    bound_page_type: entry.page ? 'home' : null, bound_page_type_name: entry.page ? '首页' : null,
    applicable_page_types: ['home'], display_status: 'evidence_insufficient', display_status_label: '证据不足',
    machine_display_status: 'evidence_insufficient', machine_display_status_label: '证据不足',
    execution_status: 'evidence_insufficient', judge_strategy: 'hybrid', confidence: entry.page ? 0 : null,
    severity: 'medium', severity_display: 'P2', result_summary: '受控的正式缺证展示夹具', problem_desc: null,
    review_status: 'pending', human_verdict: null, human_verdict_label: null, human_verdict_note: null,
    human_verdict_at: null, confirmable: false, confirm_status: null,
    evidence_artifact_ids: [imageId], page_capture_id: entry.page ? captureId : null,
    is_bootstrap: false, finished_at: '2026-10-19T08:00:00Z', green_result: green,
  }
}

for (const platform of ['app', 'h5', 'web'] as const) {
  for (const width of [1280, 390]) {
    test(`明确模拟缺证展示：${platform} ${width}，缺项、未判、复核与旧规则隔离`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 })
      const unmocked = await blockUnmockedApi(page)
      const { taskIds } = await mockTaskCenter(page, 'workstation')
      const root = `/api/v1/workstation/tasks/${taskIds.running}`
      const selected = cases.filter((item) => platform === 'app' || item.key !== '41')
      const items: RuleResultItem[] = selected.map(missingResult)
      const candidate = { ...missingResult(cases[0]), item_type: 'candidate' as const, execution_id: null,
        rule_id: 'controlled-unexecuted-green', rule_name: '尚未执行的绿色规则', green_result: null,
        display_status: 'pending' as const, display_status_label: '待执行', execution_status: null, evidence_artifact_ids: [] }
      const oldRule = { ...missingResult(cases[0]), rule_id: 'legacy-qr-rule', rule_code: 'alioth_41',
        execution_id: 'controlled-old-qr', rule_name: '旧二维码规则', green_result: null,
        execution_status: 'fail', display_status: 'failed' as const, display_status_label: '未通过' }
      items.push(candidate, oldRule)
      const json = async (path: string, value: unknown) => page.route(`**${path}`, (route) => {
        expect(route.request().method()).toBe('GET')
        expectBearerToken(route)
        return fulfillJson(route, value)
      })
      await json(root, {
        task_id: taskIds.running, task_name: '受控六条正式缺证展示', status: 'review', progress: 100,
        target_url: 'https://controlled.example.test/', target_platform: platform, execution_mode: 'full_inspection',
        product: { department_name: '质量平台', product_name: '受控展示夹具' }, journey_steps: [],
        report_status: 'ready', report_availability: 'preview', available_actions: ['detail', 'report'],
        run_id: 'controlled-missing-run', ruleset_code: 'green', ruleset_version: 4, channel: 'workbench', priority: 100,
        created_at: '2026-10-19T08:00:00Z', started_at: '2026-10-19T08:00:00Z', finished_at: null, required_page_types: [],
      })
      await json(`${root}/page-timeline`, {
        task_id: taskIds.running, run_id: 'controlled-missing-run', journey_steps: [], exploration_actions: [],
        nodes: [{ node_seq: 1, source: 'discovered', page_capture_id: captureId, page_type: 'home', page_type_name: '首页',
          page_state: 'stable', status: 'recognized', status_label: '已识别', is_current: false,
          url: 'https://controlled.example.test/', captured_at: '2026-10-19T08:00:01Z',
          detection: { status: 'recognized', confidence: 1 }, inspection: { status: 'completed' },
          rule_progress: { executed: selected.length, total: selected.length + 1 },
          result_rollup: 'manual_review', result_rollup_label: '待复核', result_counts: { evidence_insufficient: selected.length }, attention_items: [] }],
      })
      await json(`${root}/rule-results`, { task_id: taskIds.running, total: items.length, items, status_counts: {} })
      await json(`${root}/screenshots`, { task_id: taskIds.running, run_id: 'controlled-missing-run', groups: [{
        page_group_key: 'controlled-home', page_title: '首页', page_display_name: '首页', page_type: 'home', page_type_name: '首页', total: 1,
        items: [{ artifact_id: imageId, page_capture_id: captureId, content_type: 'image/png', size_bytes: 1000,
          duplicate_count: 1, related_rules: [], captured_at: '2026-10-19T08:00:01Z', capture_role: 'observation' }],
      }] })
      await page.route(`**/api/v1/evidence/${imageId}/ticket`, (route) => {
        expectBearerToken(route)
        return fulfillJson(route, { url: `/evidence/${imageId}/file?ticket=controlled-missing`, expires_at: '2099-01-01T00:00:00Z' })
      })
      await page.route(`**/api/v1/evidence/${imageId}/file?ticket=controlled-missing`, (route) => route.fulfill({
        contentType: 'image/png', body: readFileSync(new URL('../../fixtures/green-rule-original.png', import.meta.url)),
      }))

      await loginTaskCenter(page, '/tasks')
      await page.goto(`/tasks/${taskIds.running}`)
      if (width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
      await page.getByTestId('node-detail-1').click()
      const dialog = page.getByTestId('page-detail-dialog')
      const cards = dialog.getByTestId('green-rule-details')
      const pageEntries = selected.filter((entry) => entry.page)
      await expect(cards).toHaveCount(pageEntries.length)
      await expect(dialog.getByTestId('page-failed-rules')).toContainText('旧二维码规则')
      await expect(dialog).toContainText('尚未执行的绿色规则')
      for (const [index, entry] of pageEntries.entries()) {
        const card = cards.nth(index)
        await expect(card).toContainText(entry.name)
        await expect(card.getByTestId('green-rule-status')).toHaveText('证据不足 · 待人工复核')
        await expect(card.getByTestId('green-subitem')).toHaveCount(entry.checks.length)
        await expect(card).not.toContainText('置信度')
        await expect(card.getByTestId('green-view-evidence')).toHaveCount(0)
        await expect(card).not.toContainText('controlled_minor_policy')
        const box = (await card.boundingBox())!
        expect(box.x).toBeGreaterThanOrEqual(0)
        expect(box.x + box.width).toBeLessThanOrEqual(width)
        const last = card.getByTestId('green-subitem').last()
        await last.scrollIntoViewIfNeeded()
        await expect(last).toBeInViewport({ ratio: 0.95 })
      }
      await expect(cards.first()).toContainText('涉政分项判读尚未完成')
      await cards.first().getByTestId('green-subitem').last().scrollIntoViewIfNeeded()
      await page.locator('.page-detail-dialog').screenshot({ path: info.outputPath(`green-content-gaps-last-${platform}-${width}.png`) })
      await cards.first().getByTestId('green-rule-status').scrollIntoViewIfNeeded()
      await page.locator('.page-detail-dialog').screenshot({ path: info.outputPath(`green-content-gaps-${platform}-${width}.png`) })
      await page.keyboard.press('Escape')
      await page.getByTestId('task-level-detail').click()
      const taskEntries = selected.filter((entry) => !entry.page)
      await expect(cards).toHaveCount(taskEntries.length)
      for (const [index, entry] of taskEntries.entries()) {
        const card = cards.nth(index)
        await expect(card).toContainText(entry.name)
        await expect(card.getByTestId('green-rule-status')).toHaveText('证据不足 · 待人工复核')
        await expect(card.getByTestId('green-subitem')).toHaveCount(entry.checks.length)
        await expect(card.getByTestId('green-subitem-status')).toHaveText(entry.checks.map(() => '尚未判定'))
        await expect(card).not.toContainText('置信度')
        await expect(card.getByTestId('green-view-evidence')).toHaveCount(0)
        const box = (await card.boundingBox())!
        expect(box.x).toBeGreaterThanOrEqual(0)
        expect(box.x + box.width).toBeLessThanOrEqual(width)
        const last = card.getByTestId('green-subitem').last()
        await last.scrollIntoViewIfNeeded()
        await expect(last).toBeInViewport({ ratio: 0.95 })
      }
      await expect(dialog).not.toContainText('去报告页人工确认')
      await cards.nth(2).getByTestId('green-subitem').last().scrollIntoViewIfNeeded()
      await page.locator('.page-detail-dialog').screenshot({ path: info.outputPath(`green-recovery-gaps-last-${platform}-${width}.png`) })
      await cards.nth(2).getByTestId('green-rule-status').scrollIntoViewIfNeeded()
      await page.locator('.page-detail-dialog').screenshot({ path: info.outputPath(`green-recovery-gaps-${platform}-${width}.png`) })
      expect(unmocked).toEqual([])
    })
  }
}
