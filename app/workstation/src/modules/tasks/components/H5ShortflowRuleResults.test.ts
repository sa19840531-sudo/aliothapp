import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import { shortflowPlan } from '@/test/shortflowFixtures'
import H5ShortflowRuleResults from './H5ShortflowRuleResults.vue'

it('显示后端分项和反例，定位与原图事件携带精确引用', async () => {
  const plan = shortflowPlan()
  const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
  expect(wrapper.get('[data-testid="shortflow-rule-verdict"]').text()).toBe('未通过')
  expect(wrapper.text()).toContain('暂停后播放进度停止：不符合')
  await wrapper.get('button').trigger('click')
  expect(wrapper.emitted('locate-checkpoint')).toEqual([['pause']])
  await wrapper.get('[data-testid="shortflow-rule-evidence"]').trigger('click')
  expect(wrapper.emitted('view-evidence')).toEqual([[
    plan.rule_results![0]!.checkpoints[0]!.evidence_refs[0], 'frame-0',
  ]])
})

it('证据不足和复核独立保留，不以已有材料质量覆盖', () => {
  const plan = shortflowPlan()
  const result = plan.rule_results![0]!
  result.execution_status = 'evidence_insufficient'
  result.review_status = 'pending'
  result.review_reason = '缺少完整过程，请复核'
  result.checkpoints[0]!.status = 'evidence_insufficient'
  result.checkpoints[0]!.missing = ['measurement:paused', 'latest_manifest_not_pass']
  const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
  expect(wrapper.get('[data-testid="shortflow-rule-verdict"]').text()).toContain('证据不足')
  expect(wrapper.get('[data-testid="shortflow-rule-review"]').text()).toContain('待处理')
  expect(wrapper.text()).toContain('缺少暂停状态的测量')
  expect(wrapper.text()).toContain('所需材料质量尚未通过检查')
})

it('机器通过但仍待人工复核时，不使用最终通过的文案和绿色标记', () => {
  const plan = shortflowPlan()
  plan.rule_results![0]!.execution_status = 'pass'
  plan.rule_results![0]!.review_status = 'pending'
  const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
  const badge = wrapper.get('[data-testid="shortflow-rule-verdict"]')
  expect(badge.text()).toBe('机器通过 · 待人工复核')
  expect(badge.classes()).not.toContain('is-pass')
  expect(wrapper.get('[data-testid="shortflow-rule-review"]').text()).toContain('待处理')
})

it('失配的历史成功载荷不能显示为当前通过或开放证据', () => {
  const plan = shortflowPlan()
  const result = plan.rule_results![0]!
  result.linkage_status = 'evidence_changed'
  result.linkage_notice = '原判定的输入或原件已变化'
  result.execution_status = 'pass'
  result.result_summary = '过期的成功摘要'
  const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
  expect(wrapper.get('[data-testid="shortflow-rule-verdict"]').text()).toBe('关联待核对')
  expect(wrapper.text()).not.toContain('过期的成功摘要')
  expect(wrapper.findAll('[data-testid="shortflow-rule-evidence"]')).toHaveLength(0)
})

it('旧接口缺少结果字段时明确未关联，不从已绑定材料推算', () => {
  const plan = shortflowPlan()
  delete plan.rule_results
  delete plan.rule_results_status
  const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
  expect(wrapper.text()).toContain('尚无已关联的规则判定结果')
  expect(wrapper.findAll('[data-testid="shortflow-rule-verdict"]')).toHaveLength(0)
})

it('引用跨会话、版本变化或只有非截图附件时不提供原图按钮', () => {
  for (const mutation of ['session', 'manifest', 'artifact']) {
    const plan = shortflowPlan()
    const refs = plan.rule_results![0]!.checkpoints[0]!.evidence_refs
    for (const reference of refs) {
      if (mutation === 'session') reference.session_id = 'another-session'
      if (mutation === 'manifest') reference.manifest_version = 99
      if (mutation === 'artifact') reference.artifact_ids = ['unrelated-frame']
    }
    const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
    expect(wrapper.findAll('[data-testid="shortflow-rule-evidence"]')).toHaveLength(0)
  }
})

it('动作绑定原版本与正式判定采用的最新质量版本不同，仍可查看同一原件', () => {
  const plan = shortflowPlan()
  const refs = plan.rule_results![0]!.checkpoints[0]!.evidence_refs
  for (const binding of plan.checkpoints[0]!.attempts[0]!.bindings) binding.latest_manifest_version = 2
  for (const reference of refs) {
    reference.manifest_version = 2
    reference.manifest_id = `${reference.manifest_id}-quality-v2`
  }
  const wrapper = mount(H5ShortflowRuleResults, { props: { plan } })
  expect(wrapper.findAll('[data-testid="shortflow-rule-evidence"]')).toHaveLength(2)
})
