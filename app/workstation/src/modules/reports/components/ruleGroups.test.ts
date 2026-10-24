import { describe, expect, it } from 'vitest'
import type { ReportRuleItem } from '@/types/report-insight'
import {
  buildPhaseGroups,
  buildRuleRows,
  isConclusive,
  pageNamesText,
  rateText,
  resultBreakdown,
  summarizeRules,
  usageSummaryText,
  QC_RATE_HELP,
} from './ruleGroups'

function item(overrides: Partial<ReportRuleItem> & { rule_code: string }): ReportRuleItem {
  return {
    execution_id: `exec-${overrides.rule_code}-${Math.random()}`,
    rule_id: `rule-${overrides.rule_code}`,
    rule_name: `规则 ${overrides.rule_code}`,
    category: 'ad',
    rule_package: '广告质检',
    trigger_display: '所有页面默认触发',
    bound_page_type: 'home',
    bound_page_type_name: '首页',
    display_status: 'passed',
    display_status_label: '通过',
    machine_display_status: 'passed',
    machine_display_status_label: '通过',
    execution_status: 'pass',
    severity: 'high',
    severity_display: 'P1',
    confidence: 0.9,
    result_summary: null,
    problem_desc: null,
    evidence_artifact_ids: [],
    page_capture_id: 'capture-1',
    is_bootstrap: false,
    finished_at: null,
    confirmable: false,
    human_verdict: null,
    human_verdict_label: null,
    human_verdict_note: null,
    human_verdict_at: null,
    confirm_status: null,
    ...overrides,
  }
}

const failed = { display_status: 'failed', display_status_label: '未通过' } as const
const pending = {
  display_status: 'other',
  display_status_label: '其他',
  confirmable: true,
  confirm_status: 'pending',
} as const

describe('ruleGroups', () => {
  it('未启动、bootstrap 和聚合前置缺证保留原记录，实际使用及质检率只计真实判定', () => {
    const startup = item({ rule_code: 'startup', execution_id: 'startup-audit',
      execution_status: 'skipped', execution_reason: 'exploration_not_started',
      machine_display_status: 'not_executed', display_status: 'other', result_summary: '启动失败原始留痕' })
    const bootstrap = item({ rule_code: 'bootstrap', execution_status: 'skipped', is_bootstrap: true,
      machine_display_status: 'coverage_gap', display_status: 'coverage_gap' })
    const preflight = item({ rule_code: 'preflight', execution_id: 'preflight-audit',
      execution_status: 'evidence_insufficient', execution_reason: 'AGGREGATE_APPLICABILITY_UNPROVEN',
      machine_display_status: 'evidence_insufficient', display_status: 'other' })
    const original = structuredClone([startup, bootstrap, preflight])
    const disposition = item({ rule_code: 'startup', execution_id: null, execution_status: null,
      item_type: 'candidate', applicability: 'not_applicable', reason_code: 'platform_not_supported',
      machine_display_status: 'not_applicable', display_status: 'not_applicable' })
    const rows = buildRuleRows([startup, bootstrap, preflight, disposition])
    expect(summarizeRules(rows)).toMatchObject({ usedRuleCount: 0, executionCount: 0,
      statusCounts: { not_applicable: 1, coverage_gap: 1, other: 1 } })
    expect(rows[0]!.lead).toBe(disposition)
    expect(rows[0]!.items).toContain(startup)
    expect([startup, bootstrap, preflight]).toEqual(original)
    expect(usageSummaryText(rows[2]!)).toContain('未进行规则判定')

    const genuineGap = item({ rule_code: 'preflight', execution_id: 'real-judge-gap',
      execution_status: 'evidence_insufficient', execution_reason: null,
      machine_display_status: 'evidence_insufficient', display_status: 'other' })
    const passed = item({ rule_code: 'preflight' })
    const [mixed] = buildRuleRows([preflight, genuineGap, passed])
    expect(mixed!.executionCount).toBe(2)
    expect(usageSummaryText(mixed!)).toContain('质检率 50%（1 次得出结论）')
  })

  it('当前可信聚合不适用作为规则处置，旧失败记录及执行质检率仍保留', () => {
    const oldExecution = item({
      rule_code: 'aggregate', execution_id: 'old-run-execution', item_type: 'execution',
      ...failed, machine_display_status: 'failed', execution_status: 'fail',
      result_summary: '旧轮次原始失败依据',
    })
    const disposition = item({
      rule_code: 'aggregate', execution_id: null, item_type: 'candidate',
      reason_code: 'AGGREGATE_RULE_SCOPE_NOT_APPLICABLE', applicability: 'not_applicable',
      display_status: 'not_applicable', machine_display_status: 'not_applicable',
      display_status_label: '本产品不适用', execution_status: null,
      result_summary: '冻结范围已核验不适用于当前任务，未进行 AI 判定',
    })
    const original = structuredClone(oldExecution)
    const rows = buildRuleRows([oldExecution, disposition])
    expect(rows[0]!.lead).toBe(disposition)
    expect(rows[0]!.items).toEqual([oldExecution, disposition])
    expect(oldExecution).toEqual(original)
    expect(summarizeRules(rows)).toMatchObject({
      ruleCount: 1, usedRuleCount: 1, executionCount: 1,
      statusCounts: { not_applicable: 1, failed: 0, other: 0 },
    })
    expect(rows[0]!.conclusiveCount).toBe(1)
    expect(isConclusive(disposition)).toBe(false)
    expect(usageSummaryText(rows[0]!)).toBe('一共用了 1 次：未通过 1 次。质检率 100%（1 次得出结论）。')
    expect(buildPhaseGroups(rows)[0]!.failedRuleCount).toBe(0)
  })

  it('只有正式不适用处置时仍为无执行记录，不增加使用数或质检率分子', () => {
    const disposition = item({
      rule_code: 'aggregate', execution_id: null, item_type: 'candidate',
      reason_code: 'AGGREGATE_RULE_SCOPE_NOT_APPLICABLE', applicability: 'not_applicable',
      display_status: 'not_applicable', machine_display_status: 'not_applicable',
      execution_status: null, result_summary: '范围已核验；未进行 AI 判定',
    })
    const rows = buildRuleRows([disposition])
    expect(summarizeRules(rows)).toMatchObject({
      ruleCount: 1, usedRuleCount: 0, executionCount: 0,
      statusCounts: { not_applicable: 1 },
    })
    expect(rows[0]!.conclusiveCount).toBe(0)
    expect(usageSummaryText(rows[0]!)).toBe('未进行规则判定。范围已核验；未进行 AI 判定')
  })

  it.each([
    { reason_code: 'AGGREGATE_PRESENTATION_EVIDENCE_REJECTED' },
    { reason_code: null },
    { display_status: 'other', machine_display_status: 'evidence_insufficient' },
    { machine_display_status: 'evidence_insufficient' },
    { applicability: 'unproven' },
    { item_type: 'execution', execution_id: 'actual-na-execution' },
  ] satisfies Partial<ReportRuleItem>[])('不让缺证或其他不适用记录掩盖当前真实执行缺口：%j', (overrides) => {
    const currentGap = item({
      rule_code: 'aggregate', item_type: 'execution',
      display_status: 'other', machine_display_status: 'evidence_insufficient',
      execution_status: 'evidence_insufficient', confirmable: false,
    })
    const unresolved = item({
      rule_code: 'aggregate', item_type: 'candidate', execution_id: null,
      reason_code: 'AGGREGATE_RULE_SCOPE_NOT_APPLICABLE', applicability: 'not_applicable',
      display_status: 'not_applicable', machine_display_status: 'not_applicable',
      execution_status: null, ...overrides,
    })
    const [row] = buildRuleRows([currentGap, unresolved])
    expect(row!.lead).toBe(currentGap)
    expect(summarizeRules([row!]).statusCounts.other).toBe(1)
    expect(isConclusive(currentGap)).toBe(false)
    expect(row!.items[0]!.machine_display_status).toBe('evidence_insufficient')
  })

  it('73 条全清单中 53 条候选不增加真实 343 次执行，也不增加质检率分子', () => {
    const records = Array.from({ length: 343 }, (_, index) => item({
      rule_code: `executed-${index % 20}`, execution_id: `exec-${index}`, item_type: 'execution',
    }))
    const candidates = Array.from({ length: 53 }, (_, index) => item({
      rule_code: `candidate-${index}`, execution_id: null, item_type: 'candidate',
      applicability: index < 2 ? 'not_applicable' : index < 15 ? 'out_of_scope' : 'unproven',
      machine_display_status: index < 2 ? 'not_applicable' : index < 15 ? 'coverage_gap' : 'not_executed',
      display_status: index < 2 ? 'not_applicable' : index < 15 ? 'coverage_gap' : 'other',
      execution_status: null, result_summary: '已逐条核对，尚无执行记录',
    }))
    const rows = buildRuleRows([...records, ...candidates])
    const summary = summarizeRules(rows)
    expect(summary.ruleCount).toBe(73)
    expect(summary.usedRuleCount).toBe(20)
    expect(summary.executionCount).toBe(343)
    expect(Object.values(summary.statusCounts).reduce((sum, count) => sum + count, 0)).toBe(73)
    expect(buildPhaseGroups(rows)[0]!.executionCount).toBe(343)
    expect(candidates.every((record) => !isConclusive(record))).toBe(true)
    expect(usageSummaryText(rows[20]!)).toBe('未进行规则判定。已逐条核对，尚无执行记录')
    expect(rows[20]!.conclusiveCount).toBe(0)
  })
  it.each([
    'not_executed',
    'coverage_gap',
    'detect_error',
    'evidence_insufficient',
    'pending',
    'running',
    'manual_review',
  ] as const)('历史最终结果即使是不适用，%s 的检测缺口也不计入质检率分子', (status) => {
    const records = [
      item({ rule_code: 'app_rule' }),
      item({
        rule_code: 'app_rule',
        display_status: 'not_applicable',
        display_status_label: '本产品不适用',
        machine_display_status: status,
        confirmable: false,
        human_verdict: 'ignored',
        confirm_status: 'confirmed',
      }),
    ]
    const [row] = buildRuleRows(records)

    expect(isConclusive(records[1]!)).toBe(false)
    expect(row!.conclusiveCount).toBe(1)
    expect(usageSummaryText(row!)).toContain(['not_executed', 'coverage_gap'].includes(status)
      ? '质检率 100%（1 次得出结论）' : '质检率 50%（1 次得出结论）')
    expect(resultBreakdown(records)).toContainEqual({ label: '未形成结论', count: 1 })
  })

  it('已确认不适用仍算结论，质检率说明明确已有记录的分母和整集覆盖的区别', () => {
    const record = item({
      rule_code: 'app_rule',
      display_status: 'not_applicable',
      display_status_label: '本产品不适用',
      machine_display_status: 'not_applicable',
    })

    expect(isConclusive(record)).toBe(true)
    expect(QC_RATE_HELP).toContain('未执行占位、适用性核对和判定前处置不进入分母')
    expect(QC_RATE_HELP).toContain('100% 不代表规则集已全部覆盖或全部通过')
  })

  it('同一条规则的多次结果合成一行，行上用最要紧的那一次', () => {
    const rows = buildRuleRows([
      item({ rule_code: 'alioth_17' }),
      item({ rule_code: 'alioth_17', ...failed, bound_page_type_name: '视频列表页' }),
      item({ rule_code: 'alioth_17', ...pending, bound_page_type_name: '首页' }),
      item({ rule_code: 'alioth_16' }),
    ])

    expect(rows.map((row) => [row.rule_code, row.items.length])).toEqual([
      ['alioth_17', 3],
      ['alioth_16', 1],
    ])
    const [row] = rows
    expect(row.lead.display_status).toBe('failed')
    expect(row.pageNames).toEqual(['首页', '视频列表页'])
    expect(row.conclusiveCount).toBe(2)
    expect(row.pendingCount).toBe(1)
    expect(resultBreakdown(row.items)).toEqual([
      { label: '未通过', count: 1 },
      { label: '待人工确认', count: 1 },
      { label: '通过', count: 1 },
    ])
    expect(usageSummaryText(row)).toBe(
      '一共用了 3 次：未通过 1 次、待人工确认 1 次、通过 1 次。质检率 66.7%（2 次得出结论）。',
    )
  })

  it('按所属规则集分期，1、2、3 期按数字排，没标规则集的放最后', () => {
    const phase = (code: string, name: string) => ({ ruleset_code: code, ruleset_name: name })
    const groups = buildPhaseGroups(
      buildRuleRows([
        item({ rule_code: 'alioth_ops_1', ...phase('ruisheng', '3期瑞升质检规则集'), ...pending }),
        item({ rule_code: 'alioth_2', ...phase('first', '1期质检规则集') }),
        item({ rule_code: 'alioth_2', ...phase('first', '1期质检规则集'), ...failed }),
        item({ rule_code: 'alioth_spec_17', ...phase('phase2', '2期质检规则集') }),
        item({ rule_code: 'custom_1' }),
        item({ rule_code: 'alioth_3', ...phase('first', '1期质检规则集'), ...pending }),
      ]),
    )

    expect(
      groups.map((group) => [
        group.name,
        group.rules.length,
        group.executionCount,
        rateText(group.conclusiveCount, group.executionCount),
        group.failedRuleCount,
        group.pendingRuleCount,
      ]),
    ).toEqual([
      ['1期质检规则集', 2, 3, '66.7%', 1, 1],
      ['2期质检规则集', 1, 1, '100%', 0, 0],
      ['3期瑞升质检规则集', 1, 1, '0%', 0, 1],
      ['其他规则', 1, 1, '100%', 0, 0],
    ])
  })

  it('汇总按规则条数：只要有一次未通过就算未通过，其余按行上最要紧的一次归类', () => {
    const phase = (code: string, name: string) => ({ ruleset_code: code, ruleset_name: name })
    const notApplicable = { display_status: 'not_applicable', display_status_label: '本产品不适用' } as const
    const rows = buildRuleRows([
      item({ rule_code: 'alioth_2', ...phase('first', '1期质检规则集') }),
      item({ rule_code: 'alioth_2', ...phase('first', '1期质检规则集'), ...failed }),
      item({ rule_code: 'alioth_2', ...phase('first', '1期质检规则集'), ...failed }),
      item({ rule_code: 'alioth_3', ...phase('first', '1期质检规则集'), ...pending }),
      item({ rule_code: 'alioth_3', ...phase('first', '1期质检规则集') }),
      item({ rule_code: 'alioth_4', ...phase('first', '1期质检规则集') }),
      item({ rule_code: 'alioth_4', ...phase('first', '1期质检规则集'), ...notApplicable }),
      item({ rule_code: 'alioth_ops_1', ...phase('ruisheng', '3期瑞升质检规则集'), ...notApplicable }),
    ])
    const summary = summarizeRules(rows)
    const groups = buildPhaseGroups(rows)

    expect(summary.ruleCount).toBe(4)
    expect(summary.executionCount).toBe(8)
    expect(summary.statusCounts).toEqual({
      failed: 1,
      detect_error: 0,
      other: 1,
      coverage_gap: 0,
      passed: 1,
      not_applicable: 1,
    })
    // 未通过的条数 = 各期「未通过 N 条」相加，不是未通过的次数（这里是 2 次）
    expect(summary.statusCounts.failed).toBe(groups.reduce((sum, group) => sum + group.failedRuleCount, 0))
    expect(summary.executionCount).toBe(groups.reduce((sum, group) => sum + group.executionCount, 0))
    // 六类加起来就是实际使用的规则条数
    expect(Object.values(summary.statusCounts).reduce((sum, count) => sum + count, 0)).toBe(summary.ruleCount)
  })

  it('没有规则时汇总全是 0', () => {
    const summary = summarizeRules([])
    expect(summary.ruleCount).toBe(0)
    expect(Object.values(summary.statusCounts).every((count) => count === 0)).toBe(true)
  })

  it('接口没带规则集时整张表算一组「全部规则」', () => {
    const groups = buildPhaseGroups(buildRuleRows([item({ rule_code: 'a' }), item({ rule_code: 'b' })]))
    expect(groups.map((group) => group.name)).toEqual(['全部规则'])
  })

  it('页面多时只列前两个；没有次数时质检率写「-」', () => {
    expect(pageNamesText([])).toBe('未关联具体页面')
    expect(pageNamesText(['首页', '视频列表页'])).toBe('首页、视频列表页')
    expect(pageNamesText(['首页', '视频列表页', '个人中心'])).toBe('首页等 3 个页面')
    expect(rateText(0, 0)).toBe('-')
  })
})
