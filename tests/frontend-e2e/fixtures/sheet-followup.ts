/// <reference path="../../../app/workstation/env.d.ts" />
import { expect, type Page, type Route, type TestInfo } from '@playwright/test'
import type { RuleCatalog } from '../../../app/workstation/src/services/ruleCatalog.api'
import type { WorkstationTaskDetail } from '../../../app/workstation/src/types/task'
import type { WorkstationReportListItem } from '../../../app/workstation/src/types/report'
import { blockUnmockedApi, departmentUser, expectBearerToken, fulfillJson } from './api'

export const mockId = (value: number) => `10000000-0000-4000-8000-${String(value).padStart(12, '0')}`
export const simulatedNow = '2026-09-30T10:00:00Z'

export const sheetCatalog: RuleCatalog = {
  rulesets: [
    ['alioth_first_ruleset', '1期质检规则集', 40],
    ['alioth_phase2_ruleset', '2期质检规则集', 21],
    ['alioth_ruisheng_ruleset', '3期福升质检规则集', 11],
  ].map(([code, name, count], index) => ({
    ruleset_id: mockId(700 + index), ruleset_code: String(code), name: String(name), version: 1,
    description: '明确模拟：原表卡片结构的浏览器验收目录', status: 'enabled', enabled_rule_count: Number(count),
    rule_codes: Array.from({ length: Number(count) }, (_, ordinal) => `mock_${index + 1}_${ordinal + 1}`),
  })),
  rules: [],
  page_types: [
    { code: 'home', name: '首页', category: 'business', description: '模拟首页', rule_matchable: true, coverage_required: true, related_rule_codes: [] },
    { code: 'payment', name: '充值页', category: 'business', description: '模拟充值页', rule_matchable: true, coverage_required: true, related_rule_codes: [] },
  ],
  page_scope_options: { h5: ['home', 'payment'], app: ['home', 'payment'], web: [] },
}
sheetCatalog.rules = sheetCatalog.rulesets.flatMap((ruleset, phase) =>
  Array.from({ length: ruleset.enabled_rule_count }, (_, index) => ({
    rule_id: mockId(1000 + phase * 100 + index), ruleset_id: ruleset.ruleset_id,
    rule_code: `mock_${phase + 1}_${index + 1}`, name: `模拟${phase + 1}期规则${index + 1}广告规范`,
    category: 'ad', severity: index % 2 === 0 ? 'high' : 'critical', executor_type: 'llm',
    check_point: `模拟第${phase + 1}期第${index + 1}条检查内容：广告关闭入口可见`,
    applicable_page_types: [index % 3 === 0 ? 'home' : 'payment'],
    source_type: ['spreadsheet', 'manual', 'api'][phase],
    ruleset_code: ruleset.ruleset_code, ruleset_version: 1,
  })),
)

export function sheetTask(status: string, identity: number, platform: 'h5' | 'web' | 'app' = 'h5'): WorkstationTaskDetail {
  const terminal = ['done', 'review', 'failed', 'canceled'].includes(status)
  const started = status === 'queued' ? null : '2026-09-30T09:00:00Z'
  return {
    task_id: mockId(identity), task_name: `模拟${status}任务`, target_platform: platform,
    product: { department_name: '模拟质检部', product_code: 'MOCK-SHEET', product_name: '明确模拟验收产品' },
    tenant_name: '模拟验收公司', submitter_name: '模拟验收员',
    target_url: platform === 'app' ? null : 'https://example.test/simulated-inspection',
    execution_mode: 'full_inspection', journey_steps: [], status, progress: terminal ? 100 : 55,
    report_status: status === 'failed' ? 'failed' : terminal ? 'ready' : 'not_started',
    report_availability: ['done', 'review'].includes(status) ? 'final' : 'none',
    failure_reason: status === 'failed' ? '模拟执行失败原因' : null,
    available_actions: status === 'queued' ? ['start', 'edit', 'copy', 'cancel']
      : status === 'running' ? ['detail', 'cancel'] : status === 'failed' ? ['failure', 'rerun', 'copy']
      : status === 'canceled' ? ['copy'] : ['detail', 'copy'],
    created_at: '2026-09-30T08:00:00Z', execution_started_at: started,
    execution_finished_at: terminal ? '2026-09-30T09:30:00Z' : null,
    started_at: started, finished_at: terminal ? '2026-09-30T09:30:00Z' : null,
    duration_ms: started ? 1_800_000 : null, ruleset_code: 'mock_sheet_ruleset', ruleset_version: 1,
    channel: 'workbench', priority: 100, run_id: status === 'queued' ? null : mockId(5000 + identity),
    run_status: terminal ? 'done' : status === 'queued' ? null : 'running', workflow_version: 'v2',
    scan_mode: 'full', remark: '明确模拟数据，未访问或执行真实质检任务', required_page_types: [],
    browser_profile_id: null, viewport_width: null, viewport_height: null,
    execution_phase: status === 'queued' ? 'queued' : status === 'running' ? 'running' : null,
    queue_position: status === 'queued' ? 2 : null, tasks_ahead: status === 'queued' ? 1 : null,
    available_worker_count: status === 'queued' ? 3 : null,
    start_condition: status === 'queued' ? '模拟对应平台队列，在线心跳不能证明有空闲执行槽' : null,
  }
}

export const listTasks = ['queued', 'running', 'canceling', 'review', 'done', 'failed', 'canceled']
  .map((status, index) => sheetTask(status, 2000 + index))

export const longTask: WorkstationTaskDetail = {
  ...sheetTask('running', 2100), task_name: '模拟持续执行42小时任务',
  started_at: '2026-09-28T15:30:00Z', execution_started_at: '2026-09-28T15:30:00Z',
  duration_ms: 42.5 * 60 * 60 * 1000,
}
export const queuedTasks = (['h5', 'web', 'app'] as const).map((platform, index) => ({
  ...sheetTask('queued', 2200 + index, platform), task_name: `模拟${platform}独立队列任务`,
}))
const taskById = new Map([...listTasks, longTask, ...queuedTasks].map((task) => [task.task_id, task]))

export const reportItems: WorkstationReportListItem[] = [
  { task_id: mockId(3000), task_name: '模拟21项待人工确认报告', report_status: 'ready', report_availability: 'preview', pending_confirm_count: 21, pass_rate: null },
  { task_id: mockId(3001), task_name: '模拟未完整预览报告', report_status: 'ready', report_availability: 'preview', pending_confirm_count: 0, pass_rate: null },
  { task_id: mockId(3002), task_name: '模拟正式完成报告', report_status: 'ready', report_availability: 'final', pending_confirm_count: 0, pass_rate: 96 },
  { task_id: mockId(3003), task_name: '模拟生成失败报告', report_status: 'failed', report_availability: 'none', pending_confirm_count: 0, pass_rate: null },
  { task_id: mockId(3004), task_name: '模拟正在生成报告', report_status: 'generating', report_availability: 'none', pending_confirm_count: 0, pass_rate: null },
].map((item) => ({
  ...item, report_availability: item.report_availability as 'none' | 'preview' | 'final',
  overall_status: 'pending', attention_required: 0, generated_at: simulatedNow,
  target_platform: 'h5', product_name: '明确模拟验收产品', issue_count: 0,
}))

export const shotIds = [mockId(4000), mockId(4001), mockId(4002)]
const imageDimensions = [[900, 1400], [640, 960], [1200, 700]]

const retainedAudits = new WeakMap<Page, object>()

/** 断言提前失败也保存请求与模块错误，不能只留下最终的DOM缺失。 */
export async function attachSheetFollowupAudit(page: Page, info: TestInfo) {
  const audit = retainedAudits.get(page)
  if (audit) await info.attach('mock-request-audit-retained', {
    body: JSON.stringify(audit, null, 2), contentType: 'application/json',
  })
}

/** 所有响应使用独立模拟身份。未知路径/方法继续交给兜底并失败，禁止请求真实后端。 */
export async function mockSheetFollowup(page: Page, options: { catalog?: RuleCatalog } = {}) {
  const unmocked = await blockUnmockedApi(page)
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  const requests: Array<{ method: string; path: string; query: string }> = []
  const moduleFailures: Array<{ path: string; status?: number; error?: string }> = []
  const navigations: string[] = []
  const audit = { unmocked, pageErrors, consoleErrors, requests, moduleFailures, navigations }
  retainedAudits.set(page, audit)
  page.on('response', (response) => {
    if (response.request().resourceType() === 'script' && response.status() >= 400) {
      moduleFailures.push({ path: new URL(response.url()).pathname, status: response.status() })
    }
  })
  page.on('requestfailed', (request) => {
    if (request.resourceType() === 'script') moduleFailures.push({
      path: new URL(request.url()).pathname, error: request.failure()?.errorText,
    })
  })
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations.push(frame.url())
  })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error' || message.type() === 'warning') consoleErrors.push(`${message.type()}: ${message.text()}`)
  })
  const user = { ...departmentUser, full_name: '模拟部门验收员', tenant_name: '模拟验收公司', department_name: '模拟质检部' }
  await page.addInitScript((currentUser) => {
    localStorage.setItem('alioth-workstation-session', JSON.stringify({
      token: 'e2e-access-token', user: currentUser, expires_at: '2099-01-01T00:00:00Z',
    }))
  }, user)
  const handlers = new Map<string, (route: Route, url: URL) => Promise<void>>()
  const register = (method: string, path: string, handler: (route: Route, url: URL) => Promise<void>) => {
    handlers.set(`${method} ${path}`, handler)
  }
  const json = (path: string, body: unknown) => register('GET', path, async (route, url) => {
    expect(url.search).toBe('')
    await fulfillJson(route, body)
  })
  json('/api/v1/auth/me', user)
  json('/api/v1/workstation/rule-catalog', options.catalog ?? sheetCatalog)
  json('/api/v1/workstation/products', { items: [{ project_code: 'MOCK-SHEET', product_name: '明确模拟验收产品' }] })
  register('GET', '/api/v1/workstation/tasks', async (route, url) => {
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size', 'submitter_name'])
    expect(url.searchParams.get('page')).toBe('2')
    expect(url.searchParams.get('page_size')).toBe('20')
    expect(url.searchParams.get('submitter_name')).toBe('模拟验收员')
    await fulfillJson(route, { items: listTasks, total: 27, page: 2, page_size: 20 })
  })
  register('GET', '/api/v1/workstation/reports', async (route, url) => {
    expect(url.searchParams.get('page')).toBe('1')
    expect(url.searchParams.get('page_size')).toBe('10')
    const filter = url.searchParams.get('report_status')
    expect([...url.searchParams.keys()].sort()).toEqual(filter ? ['page', 'page_size', 'report_status'] : ['page', 'page_size'])
    expect([null, 'ready', 'generating', 'failed']).toContain(filter)
    const items = filter ? reportItems.filter((item) => item.report_status === filter) : reportItems
    await fulfillJson(route, { items, total: items.length, page: 1, page_size: 10 })
  })
  register('GET', `/api/v1/workstation/reports/${mockId(3002)}/export`, async (route, url) => {
    expect(url.search).toBe('')
    await route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>明确模拟导出</title><p>Mock exported final report</p>' })
  })
  register('GET', '/api/v1/workstation/reports/export-batch', async (route, url) => {
    expect([...url.searchParams.keys()]).toEqual(['task_ids'])
    expect(url.searchParams.getAll('task_ids')).toEqual([mockId(3002)])
    await route.fulfill({ contentType: 'application/zip', body: Buffer.from('UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==', 'base64') })
  })

  for (const [id, task] of taskById) {
    const root = `/api/v1/workstation/tasks/${id}`
    json(root, task)
    json(`/api/v2/tasks/${id}/h5-shortflows`, { task_id: id, snapshot_hash: 'a'.repeat(64), plans: [] })
    json(`/api/v1/tasks/${id}/agent-run`, { run: null })
    json(`${root}/page-timeline`, { task_id: id, run_id: task.run_id, nodes: [], journey_steps: [], current_node_seq: null,
      frontier: { pending_count: 0, blocked_count: 0, failed_count: 0, executed_count: 0, covered_page_types: [] } })
    json(`${root}/rule-results`, { task_id: id, items: [] })
    json(`${root}/stats`, { task_id: id, task_status: task.status, total: 0, triggered: 0, finished: 0,
      frontier_pending: 0, progress_percent: 0, status_counts: {}, status_labels: {}, recent_anomalies: [] })
    json(`${root}/app-stage`, { task_id: id, applicable: false })
    json(`${root}/inspection-work-items`, { task_id: id, task_status: task.status, run_id: task.run_id,
      control_mode: null, summary: { total: 0, nonterminal_count: 0, failed_count: 0, status_counts: {}, control_mode_counts: {} }, items: [] })
    json(`${root}/page-graph`, { task_id: id, status: 'available', summary: {}, pages: [], states: [], transitions: [], exploration_tree: { roots: [] } })
    register('GET', `${root}/page-graph/recovery-route`, async (route, url) => {
      expect(url.search).toBe('?limit=100')
      await fulfillJson(route, { task_id: id, protocol_version: 'page-graph-recovery.v1', status: 'available', routes: [] })
    })
    json(`${root}/evidence-gaps`, { task_id: id, items: [], total: 0, phase_counts: {}, policy_counts: {}, disposition_counts: {}, execution_policy: 'readonly' })
    json(`${root}/rule-reuse`, { task_id: id, items: [], total: 0, scope_counts: {}, status_counts: {} })
    json(`${root}/page-type-judgements`, { task_id: id, items: [], total: 0, status_counts: {}, conflicting_count: 0, revised_count: 0, differs_count: 0 })
    json(`${root}/closure-outcome`, { task_id: id, task_status: task.status, summary: '明确模拟空明细', dimensions: [], supplement_request_counts: {} })
    register('GET', `${root}/logs`, async (route, url) => {
      expect(url.search).toBe('?page=1&page_size=100')
      await fulfillJson(route, { task_id: id, items: [], total: 0, page: 1, page_size: 100 })
    })
    json(`${root}/screenshots`, { task_id: id, run_id: task.run_id, groups: id === longTask.task_id ? [{
      page_group_key: 'mock-home', page_display_name: '模拟首页证据', page_type: 'home', page_type_name: '首页', page_title: '明确模拟截图', total: 1,
      items: [{ artifact_id: shotIds[0], page_capture_id: mockId(4500), captured_at: simulatedNow,
        content_type: 'image/svg+xml', size_bytes: 300, duplicate_count: 1, related_rules: [], gallery_visible: true,
        related_frames: shotIds.slice(1).map((artifact_id, index) => ({ artifact_id, captured_at: simulatedNow, duplicate_count: 1, content_type: 'image/svg+xml', size_bytes: 300 })) }],
    }] : [] })
  }
  for (const [index, id] of shotIds.entries()) {
    register('POST', `/api/v1/evidence/${id}/ticket`, async (route, url) => {
      expect(url.search).toBe('')
      await fulfillJson(route, { url: `/evidence/${id}/file?ticket=mock-ticket-${index}`, expires_at: '2099-01-01T00:00:00Z' })
    })
    register('GET', `/api/v1/evidence/${id}/file`, async (route, url) => {
      expect(url.search).toBe(`?ticket=mock-ticket-${index}`)
      expect(route.request().headers().authorization).toBeUndefined()
      const [width, height] = imageDimensions[index]!
      await route.fulfill({ contentType: 'image/svg+xml', body: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><rect width="100%" height="100%" fill="${['#e8eefc', '#d6efe6', '#ffe9cf'][index]}"/><rect x="30" y="80" width="${width - 60}" height="${height - 140}" rx="20" fill="white"/><text x="55" y="150" font-size="28">SIMULATED SNAPSHOT ${index + 1}</text><text x="55" y="205" font-size="20">${width} x ${height} - mock evidence only</text></svg>` })
    })
  }
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const url = new URL(route.request().url())
    const method = route.request().method()
    const handler = handlers.get(`${method} ${url.pathname}`)
    if (!handler) return route.fallback()
    requests.push({ method, path: url.pathname, query: url.search })
    if (!url.pathname.endsWith('/file')) expectBearerToken(route)
    await handler(route, url)
  })
  return audit
}

export function assertSheetReadIsolation(audit: Awaited<ReturnType<typeof mockSheetFollowup>>) {
  expect(audit.unmocked).toEqual([])
  expect(audit.pageErrors).toEqual([])
  expect(audit.consoleErrors).toEqual([])
  expect(audit.moduleFailures).toEqual([])
  expect(audit.requests.filter((request) => request.method !== 'GET' && !request.path.endsWith('/ticket'))).toEqual([])
}
