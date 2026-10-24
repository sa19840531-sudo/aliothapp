import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { ReportRuleItem } from '@/types/report-insight'
import { buildRuleRows } from './ruleGroups'

vi.mock('naive-ui', () => ({
  NModal: defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) }),
  useMessage: () => ({ success: vi.fn(), error: vi.fn() }),
}))
vi.mock('@/services/reports.api', () => ({ confirmReportRule: vi.fn() }))
const mocks = vi.hoisted(() => ({ ticket: vi.fn() }))
vi.mock('@/services/task-insight.api', () => ({ issueEvidenceFileUrl: mocks.ticket }))

import ReportConfirmSection from './ReportConfirmSection.vue'
import RuleUsageDialog from './RuleUsageDialog.vue'
import JourneyResultDetails from './JourneyResultDetails.vue'

function item(overrides: Partial<ReportRuleItem> = {}): ReportRuleItem {
  return {
    execution_id: 'execution-1', rule_id: 'rule-1', rule_code: 'rule-1', rule_name: '广告关闭检查',
    category: 'ad', rule_package: '广告规范', trigger_display: '出现广告时',
    bound_page_type: 'home', bound_page_type_name: '首页', display_status: 'failed',
    display_status_label: '未通过', machine_display_status: 'failed', machine_display_status_label: '未通过',
    execution_status: 'fail', severity: 'high', severity_display: 'P1', confidence: 0.9,
    result_summary: '关闭入口遮挡', problem_desc: '广告覆盖关闭入口', evidence_artifact_ids: ['shot-a', 'missing', 'shot-b'],
    page_capture_id: 'capture-1', is_bootstrap: false, finished_at: '2026-01-01T00:00:00Z',
    confirmable: true, human_verdict: 'fail', human_verdict_label: '不通过', human_verdict_note: '依据已有两张截图确认',
    human_verdict_by: 'original-reviewer', human_verdict_at: '2026-09-30T01:02:00Z', confirm_status: 'confirmed',
    ...overrides,
  }
}

describe('报告人工审计与已有证据', () => {
  it.each([false, true])('原材料鉴权下载，关闭后的迟到响应不能打开旧材料：%s', async (close) => {
    let complete!: (value: string) => void
    mocks.ticket.mockReturnValueOnce(new Promise<string>((resolve) => { complete = resolve }))
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const execution = item({ journey_result: {
      execution_id: 'execution-1', input_set_id: 'inputs', execution_plan_id: 'plan',
      scenario_key: 'pause', execution_status: 'fail', checkpoints: [],
    } })
    const wrapper = mount(RuleUsageDialog, { props: {
      show: true, rule: buildRuleRows([execution])[0]!, targetPlatform: 'h5',
      shotIds: () => [], shotUrl: (id) => id,
    } })
    wrapper.getComponent(JourneyResultDetails).vm.$emit('open-material', 'missing')
    expect(mocks.ticket).toHaveBeenLastCalledWith('missing', { download: true })
    if (close) await wrapper.setProps({ show: false })
    complete('/original/material?ticket=controlled')
    await flushPromises()
    if (close) expect(open).not.toHaveBeenCalled()
    else expect(open).toHaveBeenCalledWith('/original/material?ticket=controlled', '_blank', 'noopener,noreferrer')
    wrapper.unmount()
    open.mockRestore()
  })
  it('空执行 ID 的规则只有适用性处置，不生成 AI 判定、截图次数或人工确认入口', () => {
    const candidate = item({
      item_type: 'candidate', execution_id: null, applicability: 'not_applicable',
      display_status: 'not_applicable', display_status_label: '本平台不适用',
      machine_display_status: 'not_applicable', execution_status: null,
      result_summary: '规则声明仅支持 WEB，本任务为 APP', evidence_artifact_ids: [],
      human_verdict: null, human_verdict_at: null, confirm_status: null,
      confirmable: true,
    })
    const dialog = mount(RuleUsageDialog, { props: {
      show: true, rule: buildRuleRows([candidate])[0]!, targetPlatform: 'app',
      shotIds: () => [], shotUrl: (id) => id,
    } })
    expect(dialog.text()).toContain('未进行规则判定')
    expect(dialog.text()).toContain('未进行 AI 判定')
    expect(dialog.text()).toContain('规则声明仅支持 WEB')
    expect(dialog.text()).toContain('适用性核对与未执行处置（1 条）')
    expect(dialog.find('.usage-judge').exists()).toBe(false)
    const confirm = mount(ReportConfirmSection, { props: { taskId: 'task-1', items: [candidate] } })
    expect(confirm.text()).toContain('无需人工确认')
    expect(confirm.find('select').exists()).toBe(false)
    dialog.unmount()
    confirm.unmount()
  })
  it('人工确认卡显示既有账号和时间，历史缺失明确未记录', async () => {
    const wrapper = mount(ReportConfirmSection, {
      props: { taskId: 'task-1', items: [item()] },
      global: { stubs: { SearchSelect: true } },
    })
    expect(wrapper.get('[data-testid="confirm-audit-execution-1"]').text()).toContain('确认人：original-reviewer')
    expect(wrapper.get('[data-testid="confirm-audit-execution-1"]').text()).toContain('2026/9/30')
    await wrapper.setProps({ items: [item({ human_verdict_by: null, human_verdict_at: null })] })
    expect(wrapper.get('[data-testid="confirm-audit-execution-1"]').text()).toBe('确认人：未记录 · 确认时间：未记录')
    wrapper.unmount()
  })

  it('逐次规则保留多张截图及打开顺序，同时明确缺失材料标识', async () => {
    const execution = item()
    const wrapper = mount(RuleUsageDialog, { props: {
      show: true, rule: buildRuleRows([execution])[0]!, targetPlatform: 'h5',
      shotIds: () => ['shot-a', 'shot-b'], shotUrl: (id) => `blob:${id}`,
    } })
    expect(wrapper.findAll('.usage-shot')).toHaveLength(2)
    expect(wrapper.get('[data-testid="rule-confirm-audit-execution-1"]').text()).toContain('original-reviewer')
    expect(wrapper.get('[data-testid="rule-missing-shots-execution-1"]').text()).toContain('missing')
    await wrapper.get('[data-testid="rule-usage-shot-shot-b"]').trigger('click')
    expect(wrapper.emitted('open-shots')).toEqual([[['shot-a', 'shot-b'], 1]])
    await wrapper.setProps({ shotsLoading: true, shotIds: () => [] })
    expect(wrapper.text()).toContain('截图加载中…')
    expect(wrapper.find('[data-testid="rule-missing-shots-execution-1"]').exists()).toBe(false)
    await wrapper.setProps({ shotsLoading: false, shotsError: '截图查询失败' })
    expect(wrapper.text()).toContain('截图查询失败')
    expect(wrapper.find('[data-testid="rule-missing-shots-execution-1"]').exists()).toBe(false)
    await wrapper.setProps({ shotsError: undefined })
    expect(wrapper.text()).toContain('关联材料未提供可查看截图')
    expect(wrapper.get('[data-testid="rule-missing-shots-execution-1"]').text()).toContain('shot-a、missing、shot-b')
    wrapper.unmount()
  })
})
