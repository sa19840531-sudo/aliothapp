import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import JourneyResultDetails from './JourneyResultDetails.vue'

const result = {
  execution_id: 'execution', input_set_id: 'inputs', execution_plan_id: 'plan', scenario_key: 'pause',
  execution_status: 'fail', checkpoints: [{ checkpoint_id: 'pause', status: 'fail',
    checks: { paused: false }, missing: [], integrity: 'bound', bindings: [
      { role: 'before_action', capture_id: 'before', action_execution_id: 'action',
        manifest_id: 'before-manifest', manifest_version: 2, artifact_ids: ['before-png', 'before-dom'] },
      { role: 'after_action', capture_id: 'after', action_execution_id: 'action',
        manifest_id: 'after-manifest', manifest_version: 3, artifact_ids: ['after-png', 'after-dom'] },
    ] }],
}

describe('流程分项到原始绑定定位', () => {
  it('失败分项只打开本检查点前后原图，不打开同规则其他截图', async () => {
    const wrapper = mount(JourneyResultDetails, { props: { result, availableShotIds: ['foreign-png', 'before-png', 'after-png'] } })
    expect(wrapper.text()).toContain('未满足')
    expect(wrapper.text()).toContain('before-manifest')
    expect(wrapper.text()).toContain('v3')
    await wrapper.get('[data-testid="checkpoint-shot-after-png"]').trigger('click')
    expect(wrapper.emitted('open-shots')).toEqual([[['before-png', 'after-png'], 1]])
    expect(wrapper.find('[data-testid="checkpoint-shot-foreign-png"]').exists()).toBe(false)
  })
  it('待复核无绑定时明确缺口，不能显示通过或拿其他截图补齐', () => {
    const wrapper = mount(JourneyResultDetails, { props: { result: { ...result, checkpoints: [{
      ...result.checkpoints[0]!, status: 'evidence_insufficient', integrity: 'missing',
      bindings: [], missing: ['measurement:paused'],
    }] }, availableShotIds: ['foreign-png'] } })
    expect(wrapper.text()).toContain('证据不足')
    expect(wrapper.text()).toContain('measurement:paused')
    expect(wrapper.findAll('button')).toHaveLength(0)
  })
  it('截图清单缺少原附件时不猜截图，允许按原绑定下载材料', async () => {
    const wrapper = mount(JourneyResultDetails, { props: { result, availableShotIds: ['foreign-png'] } })
    expect(wrapper.text()).toContain('before-png')
    expect(wrapper.find('[data-testid="checkpoint-shot-foreign-png"]').exists()).toBe(false)
    await wrapper.get('[data-testid="checkpoint-material-before-dom"]').trigger('click')
    expect(wrapper.emitted('open-material')).toEqual([['before-dom']])
  })
})
