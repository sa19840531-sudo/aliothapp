import { expect, test } from '@playwright/test'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { blockUnmockedApi, departmentUser, expectBearerToken, fulfillJson } from '../../fixtures/api'
import { loginTaskCenter, mockTaskCenter } from '../../fixtures/tasks'

// 派生 PNG 由平台 render_redaction 从同目录原件生成；浏览器路由受控，不代表真实产品验收。
const original = readFileSync(new URL('../../fixtures/redaction-source.png', import.meta.url))
const derived = readFileSync(new URL('../../fixtures/redaction-reviewed.png', import.meta.url))
const checksum = (bytes: Buffer) => `sha256:${createHash('sha256').update(bytes).digest('hex')}`

for (const width of [1280, 390]) for (const canManage of [true, false]) {
  test(`受控局部脱敏 ${width} ${canManage ? '编辑与业务复核' : '仅脱敏复核零原件访问'}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const unmocked = await blockUnmockedApi(page)
    const { taskIds } = await mockTaskCenter(page, 'workstation')
    const user = { ...departmentUser, permissions: [...departmentUser.permissions, 'evidenceExports:redacted', ...(canManage ? ['evidenceExports:original', 'evidenceRedactions:manage'] : [])] }
    await page.route('**/api/v1/auth/me', (route) => fulfillJson(route, user))
    const artifact = { artifact_id: 'controlled-image', artifact_type: 'screenshot', checksum: checksum(original), content_type: 'image/png', size_bytes: original.length }
    const summary = { redaction_id: 'controlled-redaction-v1', artifact_id: artifact.artifact_id,
      source: { artifact_id: artifact.artifact_id, artifact_type: 'screenshot', task_id: taskIds.running, agent_run_id: 'controlled-run', session_id: 'controlled-session', capture_id: 'controlled-capture', manifest_id: 'controlled-manifest', manifest_version: 1, manifest_hash: 'm'.repeat(64), source_hash: 's'.repeat(64), original_checksum: artifact.checksum, original_size_bytes: artifact.size_bytes, original_content_type: artifact.content_type },
      policy_version: 'evidence-local-mask.v1', plan_hash: 'p'.repeat(64), derived_checksum: checksum(derived), derived_size_bytes: derived.length,
      derived_content_type: 'image/png', business_reviewable: true, reviewed_by: 'controlled-reviewer', created_at: '2026-10-23T09:00:00Z' }
    await page.route(`**/api/v2/tasks/${taskIds.running}/h5-shortflows`, (route) => fulfillJson(route, {
      task_id: taskIds.running, snapshot_hash: 'f'.repeat(64), plans: [{ execution_plan_id: 'controlled-plan', definition_id: 'controlled-player', version: 1,
        content_hash: 'a'.repeat(64), scenario_key: 'payment', agent_run_id: 'controlled-run', checkpoints: [{ checkpoint_id: 'payment', attempts: [{
          attempt_id: 'controlled-attempt', session_id: 'controlled-session', finalization_epoch: 1, status: 'bound', observation_status: 'observed', created_at: '2026-10-23T09:00:00Z',
          bindings: [{ role: 'after_action', capture_id: 'controlled-capture', action_execution_id: 'controlled-action', manifest_id: 'controlled-manifest', manifest_version: 1,
            latest_manifest_version: 1, integrity: 'current', quality_status: 'pass', captured_at: '2026-10-23T09:00:00Z', artifacts: [artifact] }],
        }] }] }],
    }))
    let created = !canManage
    let originalTickets = 0
    let previews = 0
    let exports = 0
    await page.route('**/api/v1/evidence/controlled-image/ticket', async (route) => {
      originalTickets++; expect(canManage).toBe(true); expectBearerToken(route)
      await fulfillJson(route, { url: '/evidence/controlled-image/file?ticket=controlled', expires_at: '2099-01-01T00:00:00Z' })
    })
    await page.route('**/api/v1/evidence/controlled-image/file?*', (route) => route.fulfill({ contentType: 'image/png', body: original }))
    await page.route('**/api/v2/evidence/controlled-image/redactions', async (route) => {
      expectBearerToken(route)
      if (route.request().method() === 'POST') {
        expect(canManage).toBe(true)
        expect(route.request().postDataJSON().review_confirmed).toBe(true)
        created = true
        await fulfillJson(route, summary)
      } else await fulfillJson(route, { can_manage: canManage, items: created ? [summary] : [] })
    })
    await page.route('**/api/v2/evidence/controlled-image/redactions/preview', async (route) => {
      expectBearerToken(route); previews++
      expect(route.request().postDataJSON()).toEqual({ source_checksum: artifact.checksum, manifest_id: 'controlled-manifest', manifest_version: 1,
        review_confirmed: false, spec: { method: 'image_regions', image_width: 800, image_height: 380, regions: [{ x: 20, y: 40, width: 700, height: 110 }] } })
      await fulfillJson(route, { ...summary, content_base64: derived.toString('base64') })
    })
    await page.route('**/api/v2/evidence-redactions/controlled-redaction-v1/file', async (route) => {
      expectBearerToken(route)
      await route.fulfill({ contentType: 'image/png', body: derived })
    })
    await page.route(`**/api/v2/tasks/${taskIds.running}/h5-shortflows/controlled-plan/archive`, async (route) => {
      expectBearerToken(route); exports++
      expect(route.request().method()).toBe('POST')
      expect(route.request().postDataJSON()).toEqual({ snapshot_hash: 'f'.repeat(64), redaction_ids: { 'controlled-image': 'controlled-redaction-v1' } })
      await route.fulfill({ contentType: 'application/zip', body: Buffer.from('UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==', 'base64') })
    })
    await loginTaskCenter(page, '/tasks')
    await page.goto(`/tasks/${taskIds.running}`)
    if (width === 390) await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
    const panel = page.getByTestId('h5-checkpoint-panel')
    await panel.getByTestId('shortflow-redaction-open').click()
    const redaction = panel.getByTestId('redaction-panel')
    await expect(redaction.getByTestId('redaction-export')).toBeDisabled()
    if (canManage) {
      await redaction.getByTestId('redaction-edit').click()
      const image = redaction.getByTestId('redaction-image-editor').locator('img')
      await expect(image).toBeVisible()
      await image.scrollIntoViewIfNeeded()
      await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(800)
      const bounds = (await image.boundingBox())!
      await page.mouse.move(bounds.x + bounds.width * 20.25 / 800, bounds.y + bounds.height * 40.25 / 380)
      await page.mouse.down()
      await page.mouse.move(bounds.x + bounds.width * 719.75 / 800, bounds.y + bounds.height * 149.75 / 380)
      await page.mouse.up()
      await expect(redaction.getByTestId('redaction-ranges')).toHaveValue('20,40,700,110')
      await redaction.getByTestId('redaction-preview').click()
      const preview = redaction.getByTestId('redaction-review').locator('img')
      await expect(preview).toBeVisible()
      await expect.poll(() => preview.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(800)
      await preview.screenshot({ path: info.outputPath('server-derived-business-review.png') })
      await redaction.getByTestId('redaction-confirm').check()
      await redaction.getByTestId('redaction-save').click()
    } else {
      await expect(redaction.getByTestId('redaction-edit')).toHaveCount(0)
      await redaction.getByTestId('redaction-version').selectOption('controlled-redaction-v1')
    }
    const review = redaction.getByTestId('redaction-review')
    await expect(review.locator('img')).toBeVisible()
    await expect(redaction.getByTestId('redaction-choose')).toBeEnabled()
    const pixels = await review.locator('img').evaluate((node) => {
      const image = node as HTMLImageElement
      const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight
      const context = canvas.getContext('2d')!; context.drawImage(image, 0, 0)
      return { masked: [...context.getImageData(25, 45, 1, 1).data], business: [...context.getImageData(10, 200, 1, 1).data] }
    })
    expect(pixels.masked).toEqual([32, 32, 32, 255])
    expect(pixels.business).toEqual([255, 255, 255, 255])
    await review.locator('img').screenshot({ path: info.outputPath('reviewed-business-evidence.png') })
    await redaction.getByTestId('redaction-choose').click()
    const download = page.waitForEvent('download')
    await redaction.getByTestId('redaction-export').click()
    await download
    expect(exports).toBe(1)
    expect(originalTickets).toBe(canManage ? 1 : 0)
    expect(previews).toBe(canManage ? 1 : 0)
    expect((await redaction.boundingBox())!.width).toBeLessThanOrEqual(width)
    await redaction.screenshot({ path: info.outputPath('redaction-panel.png') })
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: info.outputPath('redaction-page.png'), fullPage: true })
    expect(unmocked).toEqual([])
  })
}
