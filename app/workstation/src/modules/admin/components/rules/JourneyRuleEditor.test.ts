import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import JourneyRuleEditor from './JourneyRuleEditor.vue'

const mocks = vi.hoisted(() => ({ catalog: vi.fn() }))
vi.mock('@/services/h5-shortflows.api', () => ({ getH5ShortflowCatalog: mocks.catalog }))
const definition = {
  definition_id: 'player', version: 1, content_hash: 'a'.repeat(64),
  workflow_skill: 'h5-player-workflow' as const, name: '暂停',
  evidence_conditions: [{ checkpoint_id: 'pause', requirements: [{
    role: 'paused', artifact_types: ['screenshot' as const, 'dom' as const],
    minimum_count: 1, capture_phase: 'after_action' as const,
  }] }],
}
const contract = () => ({ ...structuredClone(definition), scenario_key: 'pause' })
beforeEach(() => {
  vi.clearAllMocks()
  mocks.catalog.mockResolvedValue({ definitions: [structuredClone(definition)], rules: [] })
})

it('原版本、场景和证据条件回显，取消编辑不修改调用方原对象', async () => {
  const value = contract()
  const original = structuredClone(value)
  const wrapper = mount(JourneyRuleEditor, { props: { modelValue: value } })
  await flushPromises()
  expect(wrapper.get('[data-testid="journey-scenario"]').element).toHaveProperty('value', 'pause')
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(true)
  expect(value).toEqual(original)
})

it('目录没有原发布版本不能猜配对或按其他版本保存', async () => {
  mocks.catalog.mockResolvedValue({ definitions: [{ ...definition, version: 2 }], rules: [] })
  const wrapper = mount(JourneyRuleEditor, { props: { modelValue: contract() } })
  await flushPromises()
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(false)
  await flushPromises()
  expect(wrapper.text()).toContain('不自动选择其他流程')
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
})

it('场景空白和无证据条件拒绝保存，不改发布合同', async () => {
  const value = contract()
  value.scenario_key = ' '
  const wrapper = mount(JourneyRuleEditor, { props: { modelValue: value } })
  await flushPromises()
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(false)
  const empty = contract()
  empty.evidence_conditions[0]!.requirements[0]!.artifact_types = []
  await wrapper.setProps({ modelValue: empty })
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(false)
  expect(definition.evidence_conditions[0]!.requirements[0]!.artifact_types).toEqual(['screenshot', 'dom'])
})

it('已配置规则不能清空选择后假装保存成功、继续沿用旧合同', async () => {
  const wrapper = mount(JourneyRuleEditor, { props: { modelValue: null, required: true } })
  await flushPromises()
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(false)
})

it('规则可选择检查点子集，取消选择不修改发布合同，重新选择恢复发布要求', async () => {
  const extended = structuredClone(definition)
  extended.evidence_conditions.push({ ...structuredClone(extended.evidence_conditions[0]!), checkpoint_id: 'restore' })
  mocks.catalog.mockResolvedValue({ definitions: [extended], rules: [] })
  const value = { ...structuredClone(extended), scenario_key: 'player' }
  const wrapper = mount(JourneyRuleEditor, { props: { modelValue: value } })
  await flushPromises()

  await wrapper.get('[data-testid="journey-checkpoint-restore"]').setValue(false)
  const subset = wrapper.emitted('update:modelValue')!.at(-1)![0] as typeof value
  expect(subset.evidence_conditions.map((item) => item.checkpoint_id)).toEqual(['pause'])
  expect(extended.evidence_conditions).toHaveLength(2)
  expect(value.evidence_conditions).toHaveLength(2)
  await wrapper.setProps({ modelValue: subset })
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(true)
  expect(wrapper.get('[data-testid="journey-checkpoint-restore"]').element).toHaveProperty('checked', false)

  await wrapper.get('[data-testid="journey-checkpoint-restore"]').setValue(true)
  const restored = wrapper.emitted('update:modelValue')!.at(-1)![0] as typeof value
  expect(restored.evidence_conditions).toEqual(extended.evidence_conditions)
})

it('未选检查点或检查点没有材料要求时拒绝保存', async () => {
  const value = contract()
  value.evidence_conditions = []
  const wrapper = mount(JourneyRuleEditor, { props: { modelValue: value } })
  await flushPromises()
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(false)
  const empty = contract()
  empty.evidence_conditions[0]!.requirements = []
  await wrapper.setProps({ modelValue: empty })
  expect((wrapper.vm as unknown as { validate(): boolean }).validate()).toBe(false)
})
