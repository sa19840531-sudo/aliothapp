import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import WebStageDurationPanel from './WebStageDurationPanel.vue'

describe('WebStageDurationPanel', () => {
  it('未完成阶段显示稳定空值语义并保留已完成的毫秒边界', () => {
    const wrapper = mount(WebStageDurationPanel, {
      props: {
        durations: {
          exploration_ms: 999,
          inspection_ms: null,
          report_generation_ms: null,
          total_ms: null,
        },
      },
    })

    expect(wrapper.text()).toContain('探索耗时999毫秒')
    expect(wrapper.text()).toContain('规则质检耗时未完成')
    expect(wrapper.text()).toContain('报告生成耗时未完成')
    expect(wrapper.text()).toContain('提交至报告总耗时未完成')
  })
})
