import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ getH5ShortflowCatalog: vi.fn() }))
vi.mock('@/services/h5-shortflows.api', () => mocks)
import H5ShortflowSelect from './H5ShortflowSelect.vue'

beforeEach(() => {
  mocks.getH5ShortflowCatalog.mockResolvedValue({
    definitions: [{ definition_id: 'controlled-player', version: 2, content_hash: 'a'.repeat(64),
      name: '受控播放器', workflow_skill: 'h5-player-workflow', checkpoints: [{ checkpoint_id: 'pause' }] }],
    rules: [{ rule_id: 'r1', rule_code: 'pause', name: '暂停反馈', ruleset_code: 'selected' }],
  })
})

it('选择已发布版本和实际规则，不自动启用模板或增加预算', async () => {
  const wrapper = mount(H5ShortflowSelect, { props: { modelValue: [], rulesetCodes: ['selected'] } })
  await flushPromises()
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  await wrapper.get('[data-testid="shortflow-definition"]').setValue('controlled-player:2')
  await wrapper.get('[data-testid="shortflow-scenario"]').setValue('pause')
  await wrapper.get('[data-testid="shortflow-rule-r1"]').setValue(true)
  await wrapper.get('[data-testid="shortflow-add"]').trigger('click')
  expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual([{
    definition_id: 'controlled-player', version: 2, content_hash: 'a'.repeat(64),
    scenario_key: 'pause', workflow_skill: 'h5-player-workflow', rule_ids: ['r1'],
  }])
})

it('规则集取消后清除不再适用的选择，迟到目录不能恢复旧选择', async () => {
  const selected = [{ definition_id: 'controlled-player', version: 2, content_hash: 'a'.repeat(64),
    scenario_key: 'pause', workflow_skill: 'h5-player-workflow' as const, rule_ids: ['r1'] }]
  const wrapper = mount(H5ShortflowSelect, { props: { modelValue: selected, rulesetCodes: ['selected'] } })
  await flushPromises()
  await wrapper.setProps({ rulesetCodes: ['other'] })
  expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual([])
})

it('未发布或加载失败不猜测流程，不允许提交不完整场景', async () => {
  mocks.getH5ShortflowCatalog.mockRejectedValue(new Error('目录不可用'))
  const wrapper = mount(H5ShortflowSelect, { props: { modelValue: [], rulesetCodes: ['selected'] } })
  await flushPromises()
  expect(wrapper.text()).toContain('目录不可用')
  expect(wrapper.find('[data-testid="shortflow-add"]').exists()).toBe(false)
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
})
