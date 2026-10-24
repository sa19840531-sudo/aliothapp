import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { RuleResultItem } from '@/types/task-insight'
import PageDetailDialog from './PageDetailDialog.vue'

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  return { NModal: defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) }) }
})

function item(overrides: Partial<RuleResultItem> = {}): RuleResultItem {
  return {
    item_type: 'execution',
    execution_id: 'old-execution',
    rule_id: 'rule-1',
    rule_name: '原始通过规则',
    display_status: 'passed',
    machine_display_status: 'passed',
    confirmable: false,
    ...overrides,
  } as RuleResultItem
}

function dialog(rules: RuleResultItem[]) {
  return mount(PageDetailDialog, {
    props: { show: true, title: '规则详情', story: [], rules, explorationOnly: false, screenshotIds: () => [] },
  })
}

describe('规则清单核对详情', () => {
  it('保留原通过结果并展示无法证明当前轮次完成的原因', () => {
    const wrapper = dialog([item({ inventory_coverage_reason: '已有记录无法关联当前主运行和收口轮次' })])
    expect(wrapper.text()).toContain('通过的规则（1 条）')
    expect(wrapper.get('[data-testid="inventory-coverage-reason"]').text()).toContain('已有记录无法关联当前主运行和收口轮次')
  })

  it('无执行编号的候选不能人工确认成判定结果', () => {
    const wrapper = dialog([item({
      item_type: 'candidate', execution_id: null, display_status: 'not_executed',
      machine_display_status: 'not_executed', display_status_label: '未执行',
      problem_desc: '任务已结束，未产生执行记录', confirmable: true,
    })])
    const button = wrapper.get('button.rule-chip')
    expect(button.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('任务已结束，未产生执行记录')
  })
})
