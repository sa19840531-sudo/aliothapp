import { expect, test } from '@playwright/test'
import { createHash } from 'node:crypto'
import { blockUnmockedApi, expectBearerToken, fulfillJson, testUser } from '../../fixtures/api'
import { loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

for (const width of [1280, 390]) {
test(`受控短流程${width}：原创建页到V2检查点、前后截图和整组鉴权下载`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 900 })
  const unmocked = await blockUnmockedApi(page)
  const { taskIds } = await mockTaskCenter(page, 'workstation')
  const selection = { definition_id: 'controlled-player', version: 2, content_hash: 'a'.repeat(64),
    scenario_key: 'pause', rule_ids: ['controlled-rule'], workflow_skill: 'h5-player-workflow' }
  await page.route('**/api/v2/capabilities', (route) => fulfillJson(route, { web: { enabled: false, execution_modes: [], browser_profiles: [] }, devices: [] }))
  await page.route('**/api/v1/workstation/task-drafts', (route) => fulfillJson(route, { items: [] }))
  await page.route('**/api/v1/workstation/rule-catalog', (route) => fulfillJson(route, {
    rulesets: [{ ruleset_id: 'controlled-ruleset', ruleset_code: 'controlled', version: 1, name: '受控规则集', status: 'enabled', rule_codes: [], enabled_rule_count: 1 }],
    rules: [], page_types: [], page_scope_options: { h5: [], app: [], web: [] },
  }))
  await page.route('**/api/v2/h5-shortflows/catalog', (route) => fulfillJson(route, {
    definitions: [{ ...selection, name: '受控播放器', checkpoints: [{ checkpoint_id: 'pause', depends_on: [] }] }],
    rules: [{ rule_id: 'controlled-rule', rule_code: 'pause', name: '暂停反馈', ruleset_code: 'controlled' }],
  }))
  let submitted = false
  await page.route('**/api/v2/tasks', async (route) => {
    expectBearerToken(route)
    const payload = route.request().postDataJSON()
    expect(payload.h5_shortflows).toEqual([selection])
    expect(payload.target.platform).toBe('h5')
    expect(payload).not.toHaveProperty('loop_guard')
    expect(route.request().headers()['idempotency-key']).toBeTruthy()
    submitted = true
    await fulfillJson(route, { task_id: taskIds.running, run_id: 'controlled-run', status: 'queued' })
  })
  const domText = '<script>window.reviewCanary = "executed"</script><p>保留原始 DOM 内容</p>'
  const domArtifact = { artifact_id: 'dom-1', artifact_type: 'dom', content_type: 'text/html',
    size_bytes: Buffer.byteLength(domText), checksum: 'sha256:' + createHash('sha256').update(domText).digest('hex') }
  const bindings = ['before_action', 'after_action'].map((role, index) => ({
    role, capture_id: `capture-${index}`, action_execution_id: 'action', manifest_id: `manifest-${index}`,
    manifest_version: 1, latest_manifest_version: index + 1, quality_status: index ? 'fail' : 'pending', integrity: 'current',
    captured_at: `2026-10-17T00:00:0${index}Z`,
    artifacts: [{ artifact_id: `frame-${index}`, artifact_type: 'screenshot' }, ...(index ? [domArtifact] : [])],
    manifest_versions: index ? [{ manifest_id: 'old-exact-manifest', manifest_version: 1,
      manifest_hash: 'a'.repeat(64), created_at: '2026-10-17T00:00:00Z', quality_status: 'pending',
      quality_reason: null, is_bound: true, is_latest: false, review_only: true,
      integrity: 'verified', artifacts: [domArtifact] }] : [],
  }))
  await page.route(`**/api/v2/tasks/${taskIds.running}/h5-shortflows`, (route) => fulfillJson(route, {
    task_id: taskIds.running, snapshot_hash: 'f'.repeat(64), plans: [{ ...selection, execution_plan_id: 'controlled-plan',
      agent_run_id: 'controlled-run', timeline: [{ event_id: 'controlled-attempt:capture:capture-0', event_type: 'capture',
        occurred_at: '2026-10-17T00:00:00Z', checkpoint_id: 'pause', planned_order: 1,
        attempt_id: 'controlled-attempt', session_id: 'controlled-session', finalization_epoch: 1,
        role: 'before_action', capture_id: 'capture-0', action_execution_id: 'action' }],
      checkpoints: [{ checkpoint_id: 'pause', attempts: [{ attempt_id: 'controlled-attempt',
        session_id: 'controlled-session', finalization_epoch: 1, status: 'bound', observation_status: 'needs_review', created_at: '2026-10-17T00:00:00Z', bindings }] }] }],
  }))
  await page.route('**/api/v1/evidence/frame-*/ticket', async (route) => {
    expectBearerToken(route)
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!
    await fulfillJson(route, { url: `/evidence/${id}/file?ticket=controlled`, expires_at: '2099-01-01T00:00:00Z' })
  })
  await page.route('**/api/v1/evidence/frame-*/file?ticket=controlled', (route) => route.fulfill({
    contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXZkAAAAASUVORK5CYII=', 'base64'),
  }))
  await page.route('**/api/v1/evidence/dom-1/ticket', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, { url: '/evidence/dom-1/file?ticket=controlled', expires_at: '2099-01-01T00:00:00Z' })
  })
  await page.route('**/api/v1/evidence/dom-1/file?*', (route) => route.fulfill({ contentType: 'text/html', body: domText }))
  const exportModes: string[] = []
  await page.route(`**/api/v2/tasks/${taskIds.running}/h5-shortflows/controlled-plan/archive?*`, async (route) => {
    expectBearerToken(route)
    expect(new URL(route.request().url()).searchParams.get('snapshot_hash')).toBe('f'.repeat(64))
    exportModes.push(new URL(route.request().url()).searchParams.get('mode') || 'original')
    await route.fulfill({ contentType: 'application/zip', headers: { 'Content-Disposition': 'attachment; filename="controlled-group.zip"' },
      body: Buffer.from('UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==', 'base64') })
  })
  await loginTaskCenter(page, '/tasks')
  await page.goto('/tasks/new')
  if (width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
  await page.getByTestId('task-name').fill('明确受控短流程，不是原站验收')
  await page.locator('.field').filter({ hasText: '产品' }).locator('.n-base-selection').click()
  await page.getByText('E2E-001 · 固定列验收产品', { exact: true }).click()
  await page.getByTestId('h5-target-url').fill('https://controlled.example.test/')
  await page.getByTestId('shortflow-definition').click()
  await page.locator('.n-base-select-option').filter({ hasText: '受控播放器 · v2' }).click()
  await page.getByTestId('shortflow-scenario').fill('pause')
  await page.getByTestId('shortflow-rule-controlled-rule').check()
  await page.getByTestId('shortflow-add').click()
  await page.screenshot({ path: info.outputPath('shortflow-create.png'), fullPage: true })
  await page.locator('button.submit-button').click()
  const panel = page.getByTestId('h5-checkpoint-panel')
  await expect(panel).toContainText('待复核')
  await expect(panel).toContainText('材料质量不合格')
  await expect(panel).toContainText('非质检通过')
  expect(submitted).toBe(true)
  await panel.getByRole('button', { name: '查看截图 frame-0' }).click()
  await expect(page.getByTestId('screenshot-viewer').getByTestId('collection-frame')).toHaveCount(2)
  await page.keyboard.press('Escape')
  await panel.getByTestId('manifest-history').locator('summary').click()
  await panel.getByRole('button', { name: '查看历史材料 dom-1', exact: true }).click()
  const textViewer = page.getByTestId('evidence-text-viewer')
  await expect(textViewer.locator('pre')).toHaveText(domText)
  await expect(textViewer).toContainText('历史只读')
  expect(await page.evaluate(() => (window as Window & { reviewCanary?: string }).reviewCanary)).toBeUndefined()
  await expect(textViewer.locator('script')).toHaveCount(0)
  await textViewer.getByRole('button', { name: '关闭材料', exact: true }).click()
  await expect(panel.getByTestId('actual-evidence-timeline')).toContainText('实际发生时间线')
  await expect(panel).toContainText('计划检查点顺序')
  const download = page.waitForEvent('download')
  await panel.getByTestId('shortflow-export').click()
  expect((await download).suggestedFilename()).toBe('controlled-group.zip')
  const redactedDownload = page.waitForEvent('download')
  await panel.getByTestId('shortflow-export-redacted').click()
  await redactedDownload
  expect(exportModes).toEqual(['original', 'redacted'])
  await expect(panel).toContainText('整图遮盖，不可据此判定业务')
  await panel.screenshot({ path: info.outputPath('shortflow-checkpoints.png') })
  expect((await panel.boundingBox())!.width).toBeLessThanOrEqual(width)
  expect(unmocked).toEqual([])
})
}

for (const width of [1280, 390]) {
test(`受控管理员${width}：复用规则抽屉编辑流程合同，不覆盖系统保护字段`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 900 })
  const unmocked = await blockUnmockedApi(page)
  const user = { ...testUser, permissions: [...testUser.permissions, 'qcRules:write'] }
  await page.addInitScript((current) => localStorage.setItem('alioth-workstation-session', JSON.stringify({
    token: 'e2e-access-token', user: current, expires_at: '2099-01-01T00:00:00Z',
  })), user)
  await page.route('**/api/v1/auth/me', (route) => fulfillJson(route, user))
  const rule = { id: 'controlled-rule', rule_code: 'pause', name: '暂停反馈', check_point: '连续观测反馈',
    ruleset_id: 'controlled', ruleset_name: '受控规则集', applicable_page_types: ['video_detail'], severity: 'high',
    executor_type: 'llm', status: 'disabled', category: 'player', input_contract: { target_clients: ['H5'] },
    judge_config: { capability_config: { evidence_input_scope: 'page' } } }
  await page.route('**/api/v1/rulesets?*', (route) => fulfillJson(route, { items: [{ id: 'controlled', name: '受控规则集', version: 1, status: 'enabled' }], total: 1 }))
  await page.route('**/api/v1/rules?*', (route) => fulfillJson(route, { items: [rule], total: 1 }))
  await page.route('**/api/v1/workstation/rule-catalog', (route) => fulfillJson(route, { page_types: [{ code: 'video_detail', name: '视频详情' }] }))
  let saved = false
  const contract = { definition_id: 'controlled-player', version: 2, content_hash: 'a'.repeat(64),
    workflow_skill: 'h5-player-workflow', scenario_key: 'pause', evidence_conditions: [{
      checkpoint_id: 'pause', requirements: [{ role: 'paused', artifact_types: ['screenshot', 'dom'] }],
    }] }
  await page.route('**/api/v2/h5-shortflows/catalog', (route) => fulfillJson(route, { definitions: [{
    ...contract, name: '受控暂停流程', checkpoints: [{ checkpoint_id: 'pause', depends_on: [] }],
  }], rules: [] }))
  await page.route('**/api/v1/rules/controlled-rule', async (route) => {
    expectBearerToken(route)
    expect(route.request().method()).toBe('PATCH')
    expect(route.request().postDataJSON()).toEqual({ name: rule.name, check_point: rule.check_point,
      applicable_page_types: ['video_detail'], severity: 'high', evidence_input_scope: 'journey',
      journey_rule_contract: contract })
    saved = true
    await fulfillJson(route, rule)
  })
  await page.goto('/admin/rules')
  await page.getByTestId('rule-table').getByRole('button', { name: '编辑', exact: true }).click()
  await page.getByTestId('rule-form-evidence-scope').click()
  await page.locator('.n-base-select-option').filter({ hasText: 'H5 V2 短流程证据' }).click()
  await page.getByTestId('journey-definition').click()
  await page.locator('.n-base-select-option').filter({ hasText: '受控暂停流程 · v2' }).click()
  await page.getByTestId('journey-scenario').fill('pause')
  expect((await page.locator('.n-drawer').boundingBox())!.width).toBeLessThanOrEqual(width)
  await page.screenshot({ path: info.outputPath('shortflow-rule-editor.png'), fullPage: true })
  await page.getByTestId('rule-form-save').click()
  await expect(page.getByText('规则已保存', { exact: true })).toBeVisible()
  expect(saved).toBe(true)
  expect(unmocked).toEqual([])
})
}
