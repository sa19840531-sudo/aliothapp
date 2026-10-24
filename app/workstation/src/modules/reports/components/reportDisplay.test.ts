import { describe, expect, it } from 'vitest'
import type { ReportRuleItem } from '@/types/report-insight'
import type { GreenRuleResultProjection } from '@/types/task-insight'
import { buildRuleRows, summarizeRules } from './ruleGroups'
import { currentReportRuleItem, finalResultOf, humanConfirmationAuditText, ruleExecutionStateText, ruleExecutionStateDetail } from './reportDisplay'

describe('报告执行事实与业务结论', () => {
  it('同规则某次未通过时，其他次未执行仍明确显示部分未执行', () => {
    const items = [
      { execution_id: 'failed-record', machine_display_status: 'failed', display_status: 'failed' },
      { execution_id: 'gap-record', machine_display_status: 'not_executed', display_status: 'other', human_verdict: 'pass' },
    ] as ReportRuleItem[]
    expect(ruleExecutionStateText(items)).toBe('部分未执行')
    expect(ruleExecutionStateDetail(items)).toBe('已执行 1 次；未执行 1 次')
    expect(finalResultOf(items[1]!).label).toBe('未形成结论')
  })
  it('已执行与证据不足并存时不能整条写已执行', () => {
    const items = [{ machine_display_status: 'passed' }, { machine_display_status: 'evidence_insufficient' }] as ReportRuleItem[]
    expect(ruleExecutionStateText(items)).toBe('部分无法判定')
    expect(finalResultOf(items[1]!).label).toBe('未形成结论')
  })
  it('人工审计只展示保存的原账号与确认时间，不借用机器完成时间', () => {
    const item = { human_verdict: 'fail', human_verdict_by: 'actor-original',
      human_verdict_at: '2026-09-30T01:02:00Z', finished_at: '2026-01-01T00:00:00Z' } as ReportRuleItem
    const text = humanConfirmationAuditText(item)!
    expect(text).toContain('确认人：actor-original')
    expect(text).toContain('2026/9/30')
    expect(text).not.toContain('2026/1/1')
    expect(humanConfirmationAuditText({ ...item, human_verdict_by: null, human_verdict_at: null }))
      .toBe('确认人：未记录 · 确认时间：未记录')
    expect(humanConfirmationAuditText({ ...item, human_verdict: null, human_verdict_by: null, human_verdict_at: null }))
      .toBeNull()
  })

  it.each([
    ['fail', 'pending', 'linked', 'failed', '未通过 · 待人工复核'],
    ['pass', 'pending', 'linked', 'other', '待人工复核'],
    ['pass', null, 'evidence_changed', 'other', '关联待核对'],
    ['evidence_insufficient', 'needs_recheck', 'linked', 'other', '未形成结论'],
  ])('当前绿色%s/%s/%s不借历史人工通过补成完整结果', (status, review, linkage, bucket, label) => {
    const green = { schema_version: 'green-rule-result.v1', rule_key: '32', rule_contract_hash: 'frozen',
      linkage_status: linkage, linkage_notice: null, execution_status: status, review_status: review, review_reason: null,
      missing_evidence: status === 'fail' || status === 'evidence_insufficient' ? ['text_bounds'] : [], subitems: [] } as GreenRuleResultProjection
    const original: ReportRuleItem = { execution_id: 'green-execution', rule_id: 'green-rule', rule_code: 'green',
      rule_name: '绿色规则', category: null, rule_package: '绿色规则集', trigger_display: '页面',
      bound_page_type: null, bound_page_type_name: null, machine_display_status_label: '通过',
      display_status: 'passed', display_status_label: '通过', machine_display_status: 'passed',
      severity: null, severity_display: null, confidence: 1, result_summary: '历史通过', problem_desc: null,
      page_capture_id: null, is_bootstrap: false, finished_at: null, human_verdict_label: '通过',
      human_verdict_note: null, human_verdict_at: null,
      execution_status: status, confirm_status: 'confirmed', human_verdict: 'pass', confirmable: false,
      evidence_artifact_ids: [], green_result: green }
    const current = currentReportRuleItem(original)
    expect(current.display_status).toBe(bucket)
    expect(finalResultOf(current).label).toBe(label)
    const rows = buildRuleRows([original])
    expect(rows[0]!.conclusiveCount).toBe(0)
    expect(summarizeRules(rows).statusCounts[bucket as 'failed' | 'other']).toBe(1)
    expect(original.display_status).toBe('passed')
  })

  it('无绿色投影的封存记录保持原对象、人工结论与完整计数', () => {
    const sealed = { execution_id: 'sealed', rule_id: 'rule', rule_code: 'old', display_status: 'passed',
      display_status_label: '通过', machine_display_status: 'passed', execution_status: 'pass',
      confirm_status: 'confirmed', human_verdict: 'pass' } as ReportRuleItem
    expect(currentReportRuleItem(sealed)).toBe(sealed)
    expect(buildRuleRows([sealed])[0]!.conclusiveCount).toBe(1)
    expect(finalResultOf(sealed).label).toBe('通过')
  })

  it('分项仍未判定而顶层缺码暂为空时，也不能借已确认的通过计完整质检', () => {
    const green = { schema_version: 'green-rule-result.v1', rule_key: '32', rule_contract_hash: 'frozen',
      linkage_status: 'linked', linkage_notice: null, execution_status: 'pass', review_status: 'approved', review_reason: null,
      missing_evidence: [], subitems: [{ key: 'fit_navigation_spacing', status: 'evidence_insufficient', evaluated: false,
        confidence: null, reason: '文字边界尚不完整', missing_evidence: [], evidence_artifact_ids: [],
        screenshot_artifact_ids: [], capture_ids: [] }] } as GreenRuleResultProjection
    const item = { execution_id: 'green', rule_id: 'green', rule_code: 'green', display_status: 'passed',
      confirm_status: 'confirmed', human_verdict: 'pass', green_result: green } as ReportRuleItem
    const row = buildRuleRows([item])[0]!
    expect(row.lead.display_status).toBe('other')
    expect(row.conclusiveCount).toBe(0)
    expect(finalResultOf(row.lead).label).toBe('未形成结论')
  })
})
