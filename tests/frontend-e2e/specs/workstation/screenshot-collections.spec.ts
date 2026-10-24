import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { expectBearerToken, fulfillJson } from '../../fixtures/api'
import { assertSheetReadIsolation, longTask, mockId, mockSheetFollowup, sheetTask } from '../../fixtures/sheet-followup'

const taskId = longTask.task_id
const shots = [
  { id: mockId(5100), label: 'A1 · BEFORE', color: '#d9f1e8', width: 720, height: 1280 },
  { id: mockId(5101), label: 'A2 · AFTER', color: '#e0eaff', width: 720, height: 1280 },
  { id: mockId(5102), label: 'A3 · OBSERVATION', color: '#ffebca', width: 720, height: 1280 },
  { id: mockId(5103), label: 'B1 · COVER', color: '#e8dcfa', width: 900, height: 1400 },
  { id: mockId(5104), label: 'B2 · AFTER', color: '#ffdfe1', width: 900, height: 1400 },
] as const
const firstIds = shots.slice(0, 3).map((shot) => shot.id)
const secondIds = shots.slice(3).map((shot) => shot.id)
const actionId = mockId(5400)
const actionTarget = '模拟关闭广告按钮'
const actionPurpose = '验证点击关闭按钮后广告是否消失'
const referencedRuleName = '模拟仅引用 A1 的规则'

/** 只覆盖独立模拟任务的证据，认证、接口白名单和禁止真实后端请求复用已有夹具。 */
async function mockScreenshotCollections(page: Page) {
  const audit = await mockSheetFollowup(page)
  const task = { ...sheetTask('review', 2100, 'app'), task_name: '模拟截图合集边界验收' }
  const root = `/api/v1/workstation/tasks/${taskId}`
  const json = async (path: string, payload: unknown) => {
    await page.route(`**${path}`, async (route) => {
      expect(route.request().method()).toBe('GET')
      expectBearerToken(route)
      await fulfillJson(route, payload)
    })
  }
  await json(root, task)
  await json(`${root}/page-graph`, {
    task_id: taskId, agent_run_id: task.run_id, protocol_version: 'page-graph.v1',
    status: 'available', finalization_epoch: 0, source: 'explicit-mock-action-ledger', counts: {},
    states: [0, 1].map((index) => ({
      state_id: mockId(5300 + index), task_id: taskId, agent_run_id: task.run_id,
      surface_kind: 'android', surface_session_id: 'mock-gallery-session', finalization_epoch: 0,
      command_sequence: 1, capture_role: index === 0 ? 'before_action' : 'after_action',
      logical_page_key: 'mock-gallery-home', identity_status: 'identified', page_type: 'home',
      page_state: index === 0 ? 'startup_ad' : 'stable', is_overlay: index === 0,
      is_external: false, counts_as_business_page: index === 1,
    })),
    transitions: [{
      transition_id: mockId(5401), action_execution_id: actionId,
      source_state_id: mockId(5300), target_state_id: mockId(5301),
      action_kind: 'click', semantic_target: actionTarget, risk_level: 'safe', status: 'completed',
      terminal: true, proof: 'mock-explicit-action-pair', counts_as_business_arrival: true,
      replay_eligible: false, reason_codes: [],
    }],
  })
  await json(`${root}/page-timeline`, {
    task_id: taskId, run_id: task.run_id, nodes: [], journey_steps: [], current_node_seq: null,
    frontier: { pending_count: 0, blocked_count: 0, failed_count: 0, executed_count: 1, covered_page_types: [] },
    exploration_actions: [{
      exploration_action_id: actionId, source_page_capture_id: mockId(5300), source_page_type: 'home',
      action: 'click', origin: 'discovered', target: actionTarget, target_keywords: [], risk_level: 'safe',
      purpose: actionPurpose, expected_next_page_type: 'home', navigation_depth: 0, status: 'explored',
      result_page_capture_id: mockId(5301), failure_reason: null, failure_code: null,
      failure_message: null, failure_retryable: null, attempt_count: 1,
      created_at: '2026-10-08T10:00:01Z', updated_at: '2026-10-08T10:00:02Z',
    }],
  })
  await json(`${root}/stats`, { task_id: taskId, task_status: 'review', total: 1, triggered: 1,
    finished: 1, frontier_pending: 0, progress_percent: 100, status_counts: { failed: 1 }, recent_anomalies: [] })
  await json(`${root}/rule-results`, { task_id: taskId, task_status: 'review', run_id: task.run_id,
    total: 1, status_counts: { failed: 1 }, items: [{
      item_type: 'execution', execution_id: mockId(5200), rule_id: mockId(5201), rule_code: 'mock-gallery-rule',
      rule_name: referencedRuleName, category: 'ad', rule_package: '明确模拟规则集', trigger_display: '模拟广告出现',
      bound_page_type: 'home', bound_page_type_name: '首页', applicable_page_types: ['home'],
      display_status: 'failed', display_status_label: '未通过', machine_display_status: 'failed',
      machine_display_status_label: '未通过', execution_status: 'fail', judge_strategy: 'mock', confidence: 1,
      severity: 'high', severity_display: 'P1', result_summary: '模拟按钮区域不足', problem_desc: '仅 A1 是这条规则的引用证据',
      review_status: null, human_verdict: null, human_verdict_label: null, human_verdict_note: null,
      human_verdict_at: null, confirmable: false, confirm_status: null,
      evidence_artifact_ids: [shots[0].id], page_capture_id: null, is_bootstrap: false, finished_at: '2026-10-08T10:05:00Z',
    }] })
  const frame = (index: number, capturedAt: string | null, count = 1, role = 'observation') => ({
    artifact_id: shots[index]!.id, page_capture_id: mockId(5300 + index), captured_at: capturedAt,
    capture_role: role, duplicate_count: count,
  })
  await json(`${root}/screenshots`, { task_id: taskId, run_id: task.run_id, groups: [{
    page_group_key: 'same-home-group', page_title: '模拟首页', page_display_name: '模拟首页',
    page_type: 'home', page_type_name: '首页', total: 12, items: [
      { ...frame(1, '2026-10-08T10:00:02Z', 5, 'after_action'), content_type: 'image/svg+xml', size_bytes: 1000,
        gallery_visible: true, occlusion_kind: 'startup', related_rules: [], related_frames: [
          frame(2, null), frame(0, '2026-10-08T10:00:01Z', 4, 'before_action'),
        ] },
      { ...frame(3, '2026-10-08T10:01:00Z', 1, 'before_action'), content_type: 'image/svg+xml', size_bytes: 1000,
        gallery_visible: true, occlusion_kind: 'overlay', related_rules: [],
        related_frames: [frame(4, '2026-10-08T10:01:01Z', 1, 'after_action')] },
    ],
  }] })
  for (const shot of shots) {
    await page.route(`**/api/v1/evidence/${shot.id}/ticket`, async (route) => {
      expect(route.request().method()).toBe('POST')
      expectBearerToken(route)
      await fulfillJson(route, { url: `/evidence/${shot.id}/file?ticket=gallery-mock`, expires_at: '2099-01-01T00:00:00Z' })
    })
    await page.route(`**/api/v1/evidence/${shot.id}/file?ticket=gallery-mock`, async (route) => {
      expect(route.request().method()).toBe('GET')
      expect(route.request().headers().authorization).toBeUndefined()
      await route.fulfill({ contentType: 'image/svg+xml', body:
        `<svg xmlns="http://www.w3.org/2000/svg" width="${shot.width}" height="${shot.height}">
          <rect width="100%" height="100%" fill="${shot.color}"/>
          <rect x="32" y="44" width="${shot.width - 64}" height="${shot.height - 88}" rx="24" fill="white"/>
          <text x="64" y="135" font-size="48" font-family="sans-serif" fill="#152344">${shot.label}</text>
          <text x="64" y="200" font-size="34" font-family="sans-serif" fill="#344760">LOCAL MOCK EVIDENCE</text>
          <rect x="64" y="260" width="${shot.width - 128}" height="220" rx="18" fill="${shot.color}"/>
          <text x="90" y="400" font-size="104" font-family="sans-serif" fill="#152344">${shot.label.slice(0, 2)}</text>
          <text x="64" y="570" font-size="34" font-family="sans-serif" fill="#344760">${shot.width} x ${shot.height}</text>
          <text x="64" y="635" font-size="34" font-family="sans-serif" fill="#344760">Distinct capture frame</text>
        </svg>` })
    })
  }
  return audit
}

async function expectFrameIds(viewer: Locator, expected: string[]) {
  const frames = viewer.getByTestId('collection-frame')
  await expect(frames).toHaveCount(expected.length)
  expect(await frames.evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-artifact-id')))).toEqual(expected)
  await expect.poll(() => frames.locator('img').evaluateAll((nodes) => nodes.every((node) =>
    (node as HTMLImageElement).complete && (node as HTMLImageElement).naturalWidth >= 720))).toBe(true)
}

async function expectReadableImage(image: Locator, width: number) {
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(width)
  await expect.poll(async () => (await image.boundingBox())?.width ?? 0).toBeGreaterThan(180)
  const box = (await image.boundingBox())!
  const natural = await image.evaluate((node) => ({ width: (node as HTMLImageElement).naturalWidth, height: (node as HTMLImageElement).naturalHeight }))
  expect(Math.abs(box.height / box.width - natural.height / natural.width)).toBeLessThan(0.02)
}

async function captureLayout(page: Page, viewer: Locator, info: TestInfo, filename: string) {
  const measurements = await viewer.evaluate((node) => {
    const box = node.getBoundingClientRect()
    return { viewport: window.innerWidth, left: box.left, right: box.right,
      dialogWidth: node.clientWidth, dialogScrollWidth: node.scrollWidth,
      documentWidth: document.documentElement.clientWidth, documentScrollWidth: document.documentElement.scrollWidth }
  })
  expect(measurements.left).toBeGreaterThanOrEqual(0)
  expect(measurements.right).toBeLessThanOrEqual(measurements.viewport)
  expect(measurements.dialogScrollWidth).toBeLessThanOrEqual(measurements.dialogWidth + 1)
  expect(measurements.documentScrollWidth).toBeLessThanOrEqual(measurements.documentWidth + 1)
  await page.screenshot({ path: info.outputPath(filename), fullPage: true, animations: 'disabled' })
  await info.attach(`${filename}-measurements`, { body: JSON.stringify(measurements, null, 2), contentType: 'application/json' })
}

for (const device of [{ name: 'desktop', width: 1440, height: 1000 }, { name: 'mobile-390', width: 390, height: 844 }]) {
  test(`明确模拟截图合集：${device.name} 真实动作分组、卡片隔离、放大返回及规则引用`, async ({ page }, info) => {
    await page.setViewportSize({ width: device.width, height: device.height })
    const audit = await mockScreenshotCollections(page)
    await page.goto(`/tasks/${taskId}`)
    if (device.width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
    const cards = page.getByTestId('screenshot-collection-card')
    await expect(cards).toHaveCount(2)
    await expect(cards.nth(0)).toContainText(/合集\s*·\s*3\s*张/)
    await expect(cards.nth(1)).toContainText(/合集\s*·\s*2\s*张/)
    await expect(page.locator('.screenshot-group > header')).toContainText('2个合集')

    await cards.nth(0).click()
    const viewer = page.getByTestId('screenshot-viewer')
    await expect(viewer.getByTestId('collection-grid')).toBeVisible()
    await expectFrameIds(viewer, firstIds)
    const actionGroup = viewer.getByTestId('screenshot-action-group')
    await expect(actionGroup).toHaveCount(1)
    await expect(actionGroup).toContainText(actionTarget)
    await expect(actionGroup).toContainText(actionPurpose)
    await expect(actionGroup).toContainText(referencedRuleName)
    await expect(actionGroup.getByTestId('screenshot-action-before').locator('img')).toHaveAttribute('src', new RegExp(shots[0].id))
    await expect(actionGroup.getByTestId('screenshot-action-after').locator('img')).toHaveAttribute('src', new RegExp(shots[1].id))
    await expect(viewer).toContainText('其他采集截图')
    await expect(viewer).toContainText('共 3 张不同图片')
    await expect(viewer).toContainText('同图采集 4 次')
    await expect(viewer).toContainText('同图采集 5 次')
    const overviewImage = viewer.getByTestId('collection-frame').first().locator('img')
    await expect.poll(async () => (await overviewImage.boundingBox())?.width ?? 0).toBeGreaterThan(140)
    await captureLayout(page, viewer, info, `${device.name}-a-collection.png`)
    if (device.name === 'desktop') await viewer.screenshot({
      path: fileURLToPath(new URL('../../../../.alioth/screenshot-collections-20261008/desktop-preview.png', import.meta.url)),
      animations: 'disabled',
    })

    await viewer.getByTestId('collection-frame').nth(1).click()
    const image = viewer.getByTestId('viewer-image').locator('img')
    await expect(image).toHaveAttribute('src', new RegExp(shots[1].id))
    await expectReadableImage(image, 720)
    await expect(viewer.getByTestId('viewer-prev')).toBeEnabled()
    await expect(viewer.getByTestId('viewer-next')).toBeEnabled()
    await captureLayout(page, viewer, info, `${device.name}-a-cover-large.png`)
    await viewer.getByTestId('viewer-prev').click()
    await expect(image).toHaveAttribute('src', new RegExp(shots[0].id))
    await expect(viewer.getByTestId('viewer-prev')).toBeDisabled()
    await page.keyboard.press('ArrowLeft')
    await expect(image).toHaveAttribute('src', new RegExp(shots[0].id))
    await viewer.getByTestId('viewer-next').click()
    await viewer.getByTestId('viewer-next').click()
    await expect(image).toHaveAttribute('src', new RegExp(shots[2].id))
    await expect(viewer.getByTestId('viewer-next')).toBeDisabled()
    await page.keyboard.press('ArrowRight')
    await expect(image).toHaveAttribute('src', new RegExp(shots[2].id))
    await viewer.getByTestId('viewer-back').click()
    await expectFrameIds(viewer, firstIds)
    await expect(viewer.getByTestId('collection-frame').nth(2)).toBeFocused()
    await viewer.getByTestId('viewer-close').click()
    await expect(viewer).not.toBeVisible()
    await expect(cards.nth(0)).toBeFocused()

    await cards.nth(1).click()
    await expectFrameIds(viewer, secondIds)
    await expect(viewer.getByTestId('screenshot-action-group')).toHaveCount(0)
    await expect(viewer).toContainText('其他采集截图')
    await expect(viewer).not.toContainText(actionPurpose)
    await expect(viewer).not.toContainText(referencedRuleName)
    await expect(viewer).toContainText('共 2 张不同图片')
    await captureLayout(page, viewer, info, `${device.name}-b-collection.png`)
    await viewer.getByTestId('collection-frame').first().click()
    await expect(image).toHaveAttribute('src', new RegExp(shots[3].id))
    await expectReadableImage(image, 900)
    await expect(viewer.getByTestId('viewer-prev')).toBeDisabled()
    await viewer.getByTestId('viewer-next').click()
    await expect(image).toHaveAttribute('src', new RegExp(shots[4].id))
    await expect(viewer.getByTestId('viewer-next')).toBeDisabled()
    await viewer.getByTestId('viewer-back').click()
    await expectFrameIds(viewer, secondIds)
    await expect(viewer.getByTestId('collection-frame').nth(1)).toBeFocused()
    await viewer.getByTestId('viewer-close').click()
    await expect(viewer).not.toBeVisible()
    await expect(cards.nth(1)).toBeFocused()

    await page.getByTestId('task-level-detail').click()
    await page.getByTestId('page-failed-rules').getByRole('button', { name: '看截图', exact: true }).click()
    await expect(image).toHaveAttribute('src', new RegExp(shots[0].id))
    await expectReadableImage(image, 720)
    await expect(viewer.getByTestId('collection-grid')).toHaveCount(0)
    await expect(viewer.getByTestId('viewer-back')).toHaveCount(0)
    await expect(viewer.getByTestId('viewer-prev')).toBeDisabled()
    await expect(viewer.getByTestId('viewer-next')).toBeDisabled()
    await expect(viewer).toContainText('1 / 1')
    await captureLayout(page, viewer, info, `${device.name}-rule-linked-only.png`)
    assertSheetReadIsolation(audit)
  })
}
