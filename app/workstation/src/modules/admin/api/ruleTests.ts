import { requestBlob, requestForm } from '@/services/http'
import { request } from './http'

/** 规则在正式任务里要的一类证据。 */
export interface RuleTestEvidenceNeed {
  artifact_type: string
  label: string
  /** 测试页能不能直接提供这类证据 */
  form_supported: boolean
}

/** 正式任务里另一组采集才有的证据（操作前的截图、第二个位置的页面文字），测试页单独放输入框。 */
export interface RuleTestExtraInput {
  /** before 操作前页面 / location_1 第二个价格位置 */
  role: string
  kind: 'screenshot' | 'text'
  label: string
  hint: string
}

export interface RuleTestOption {
  rule_id: string
  rule_code: string
  name: string
  category: string
  severity: string
  executor_type: string
  status: string
  check_point: string
  applicable_page_types: string[]
  ruleset_id: string
  ruleset_code: string
  ruleset_name: string
  ruleset_version: number
  ruleset_status: string
  testable: boolean
  untestable_reason: string | null
  config_problem: string | null
  /** 大模型 / 混合规则才能按已给证据放宽齐全性 */
  relaxable: boolean
  evidence_needs: RuleTestEvidenceNeed[]
  min_screenshot_count: number
  required_page_states: string[]
  target_clients: string[]
  notes: string[]
  /** 正式任务里由采集程序结构化填好的判定输入字段（如优惠价、原价），测试页要人工补 */
  judge_input_fields: string[]
  extra_inputs: RuleTestExtraInput[]
}

export interface RuleTestPageType {
  code: string
  name: string
}

export interface RuleTestOptionsResponse {
  items: RuleTestOption[]
  page_types: RuleTestPageType[]
}

export interface RuleTestFinding {
  summary: string
  evidence: string[]
}

export type RuleTestPlatform = 'h5' | 'web' | 'android' | 'ios'
export type RuleTestCheckMode = 'relaxed' | 'strict'

export interface RuleTestRunResult {
  rule_id: string
  rule_code: string
  rule_name: string
  executor_type: string
  rule_status: string
  check_mode: RuleTestCheckMode
  outcome: string
  result_summary: string
  problem_desc: string | null
  /** 0-1 小数，仅大模型给出；展示时转百分比 */
  confidence: number | null
  confidence_threshold: number | null
  findings: RuleTestFinding[]
  missing_evidence: string[]
  used_evidence: string[]
  notices: string[]
  duration_ms: number
  /** 只传了截图时模型从截图里读出的页面文字（判定用的就是它） */
  screenshot_text?: string | null
  judge_input: Record<string, unknown>
  judge_output: Record<string, unknown>
  /** 存进测试记录后的记录 ID；没存上时为空（notices 里会有提示） */
  record_id?: string | null
}

export interface RuleTestRunInput {
  ruleId: string
  platform: RuleTestPlatform
  checkMode: RuleTestCheckMode
  pageType: string
  pageState: string
  pageUrl: string
  pageTitle: string
  appPackageName: string
  visibleText: string
  domHtml: string
  uiSnapshot: string
  judgeInput: string
  screenshots: File[]
  /** 比「操作前 / 操作后」两张截图的规则：操作前那张 */
  beforeScreenshot: File | null
  /** 比两个位置价格的规则：第二个位置的页面文字 */
  secondLocationText: string
}

export function listRuleTestOptions() {
  return request<RuleTestOptionsResponse>('/rule-tests/options')
}

/** 同步跑一次规则测试：与正式任务同一套判定，结果只回给页面、不落库。 */
export function runRuleTest(input: RuleTestRunInput) {
  const form = new FormData()
  form.append('rule_id', input.ruleId)
  form.append('platform', input.platform)
  form.append('check_mode', input.checkMode)
  form.append('page_type', input.pageType)
  form.append('page_state', input.pageState)
  form.append('page_url', input.pageUrl)
  form.append('page_title', input.pageTitle)
  form.append('app_package_name', input.appPackageName)
  form.append('visible_text', input.visibleText)
  form.append('dom_html', input.domHtml)
  form.append('ui_snapshot', input.uiSnapshot)
  form.append('judge_input', input.judgeInput)
  input.screenshots.forEach((file) => form.append('screenshots', file, file.name))
  if (input.beforeScreenshot) {
    form.append('before_screenshot', input.beforeScreenshot, input.beforeScreenshot.name)
  }
  form.append('second_location_text', input.secondLocationText)
  return requestForm<RuleTestRunResult>('/rule-tests', form)
}

/** 按网址抓下来的一页证据，填进测试页的各个输入框。 */
export interface RuleTestPageCapture {
  final_url: string
  title: string
  visible_text: string
  dom_html: string
  /** 与正式采集同形的界面快照（页面元素结构） */
  ui_snapshot: Record<string, unknown> | null
  screenshot_png_base64: string | null
  notices: string[]
}

/** 用正式采集同一套程序把网址打开一次，抓下页面源码、文字、结构和截图（不落库）。 */
export function captureRuleTestPage(input: {
  url: string
  platform: RuleTestPlatform
  /** 要测的页面类型；开屏广告规则传 startup_ad，抓到开屏层就不去跳过它 */
  page_type?: string
}) {
  return request<RuleTestPageCapture>('/rule-tests/page-capture', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

/** 测试记录列表里的一行（只有摘要，证据在详情里）。 */
export interface RuleTestRecordSummary {
  id: string
  /** 规则被删了就是空的，编码和名字照样留着 */
  rule_id: string | null
  rule_code: string
  rule_name: string
  tester_name: string
  platform: RuleTestPlatform
  page_url: string
  page_title: string
  outcome: string
  result_summary: string
  screenshot_count: number
  has_visible_text: boolean
  has_dom: boolean
  has_ui_snapshot: boolean
  has_judge_input: boolean
  created_at: string
}

export interface RuleTestRecordPage {
  items: RuleTestRecordSummary[]
  total: number
  page: number
  page_size: number
}

export interface RuleTestRecordScreenshot {
  /** 从 1 开始，取图时用它 */
  index: number
  filename: string
  width: number
  height: number
  /** primary 页面截图 / before 操作前的截图 */
  capture_role: string
}

/** 一条测试记录：当时的证据 + 当时的结论（结论与测试接口返回同形）。 */
export interface RuleTestRecordDetail extends RuleTestRecordSummary {
  check_mode: RuleTestCheckMode
  page_type: string
  page_state: string
  visible_text: string
  dom_html: string
  ui_snapshot: Record<string, unknown> | null
  judge_input: Record<string, unknown>
  second_location_text: string
  screenshots: RuleTestRecordScreenshot[]
  result: RuleTestRunResult
}

export interface RuleTestRecordQuery {
  keyword?: string
  outcome?: string
  /** ISO 时间，含 */
  createdFrom?: string
  /** ISO 时间，不含 */
  createdTo?: string
  page: number
  pageSize: number
}

export function listRuleTestRecords(query: RuleTestRecordQuery) {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.keyword) params.set('keyword', query.keyword)
  if (query.outcome) params.set('outcome', query.outcome)
  if (query.createdFrom) params.set('created_from', query.createdFrom)
  if (query.createdTo) params.set('created_to', query.createdTo)
  return request<RuleTestRecordPage>(`/rule-tests/records?${params.toString()}`)
}

export function getRuleTestRecord(recordId: string) {
  return request<RuleTestRecordDetail>(`/rule-tests/records/${encodeURIComponent(recordId)}`)
}

/** 取记录里的一张截图（PNG），走登录态请求头，不把令牌放进地址。 */
export function fetchRuleTestRecordScreenshot(recordId: string, index: number) {
  return requestBlob(`/rule-tests/records/${encodeURIComponent(recordId)}/screenshots/${index}`)
}

/** 拿测试记录里的证据回测试页重测：规则按 ID 找，找不到再按编码找。 */
export interface RuleTestPrefill {
  ruleId: string | null
  ruleCode: string
  pageUrl: string
  pageTitle: string
  visibleText: string
  domHtml: string
  uiSnapshot: string
  judgeInput: string
  screenshots: File[]
  beforeScreenshot: File | null
  secondLocationText: string
}

/** 批量删除（连同截图，删了找不回来）：勾选的记录，或者一个时间段，二选一。 */
export function deleteRuleTestRecords(
  input: { recordIds: string[] } | { createdFrom?: string; createdTo?: string },
) {
  const body =
    'recordIds' in input
      ? { record_ids: input.recordIds }
      : { created_from: input.createdFrom ?? null, created_to: input.createdTo ?? null }
  return request<{ deleted: number }>('/rule-tests/records/delete', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
