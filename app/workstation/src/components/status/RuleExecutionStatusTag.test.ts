import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import RuleExecutionStatusTag from './RuleExecutionStatusTag.vue'

describe('规则执行状态标签', () => {
  it('区分覆盖缺口与确认不适用', async () => {
    const wrapper = mount(RuleExecutionStatusTag, {
      props: { status: 'skipped', outcomeClass: 'coverage_gap' },
    })

    expect(wrapper.text()).toBe('未覆盖')

    await wrapper.setProps({ outcomeClass: 'not_applicable' })

    expect(wrapper.text()).toBe('不适用')
  })
})
