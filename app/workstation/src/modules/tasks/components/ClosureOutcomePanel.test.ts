import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClosureOutcomePanel from '@/modules/tasks/components/ClosureOutcomePanel.vue'
import type { ClosureOutcome } from '@/types/task-insight'

function outcome(): ClosureOutcome {
  return {
    protocol_version: 'task-closure-outcome.v1',
    task_id: 'task-1',
    task_status: 'review',
    run_id: 'run-1',
    summary: '最终完成检查被挡住，任务不能按正常完成收口。',
    supplement_request_counts: { pending: 1, unfulfillable: 2 },
    dimensions: [
      {
        key: 'exploration',
        title: '页面探索',
        state: 'satisfied',
        state_label: '达标',
        summary: '探索已结束，走到了 6 类页面，动作和截图都收尾了。',
        metrics: {
          visited_page_type_count: 6,
          pending_action_count: 0,
          pending_capture_commit_count: 0,
          background_execution_count: 0,
        },
        blockers: [],
      },
      {
        key: 'evidence',
        title: '证据充分',
        state: 'incomplete',
        state_label: '未走完',
        summary: '还有 1 个补采请求没有结清，先等它们出结果。',
        metrics: {
          evidence_gap_count: 3,
          presentation_violation_count: 0,
          open_supplement_request_count: 1,
          settled_supplement_request_count: 2,
        },
        blockers: [],
      },
      {
        key: 'rules',
        title: '规则结果',
        state: 'incomplete',
        state_label: '未走完',
        summary: '共 4 条：通过 2，未通过 1，没有确定结论 1。',
        metrics: {
          total: 4,
          group_counts: { passed: 2, failed: 1, other: 1 },
          group_labels: { passed: '通过', failed: '未通过', other: '其他' },
        },
        blockers: [],
      },
      {
        key: 'completion_gate',
        title: '最终完成检查',
        state: 'blocked',
        state_label: '被挡住',
        summary: '还有规则检查没有结果、还有人工确认没做。',
        metrics: {
          gate_passed: false,
          nonterminal_work_item_count: 2,
          open_review_count: 5,
        },
        blockers: [
          {
            code: 'nonterminal_enforced_work_item',
            label: '还有规则检查没有结果',
            advice: '有规则还停在等证据或重试里，等它们结算。',
            waiting_for_people: false,
          },
          {
            code: 'review_policy_unsatisfied',
            label: '还有人工确认没做',
            advice: '去复核中心把待确认的项处理完。',
            waiting_for_people: true,
          },
        ],
      },
    ],
  }
}

describe('ClosureOutcomePanel', () => {
  it('四个维度各自给出状态、说明与支撑数字', () => {
    const wrapper = mount(ClosureOutcomePanel, { props: { outcome: outcome() } })
    const text = wrapper.text()

    expect(text).toContain('收口结论')
    expect(text).toContain('最终完成检查被挡住')
    expect(text).toContain('被挡住 1')

    const exploration = wrapper.find('[data-testid="closure-exploration"]')
    expect(exploration.text()).toContain('页面探索')
    expect(exploration.text()).toContain('达标')
    expect(exploration.text()).toContain('走到 6 类页面')
    // 数字为 0 的指标不摆出来
    expect(exploration.text()).not.toContain('动作未完成')

    const evidence = wrapper.find('[data-testid="closure-evidence"]')
    expect(evidence.text()).toContain('证据缺口 3')
    expect(evidence.text()).toContain('补采在途 1')
    expect(evidence.text()).toContain('补采已结 2')

    const rules = wrapper.find('[data-testid="closure-rules"]')
    expect(rules.text()).toContain('通过 2')
    expect(rules.text()).toContain('未通过 1')
    expect(rules.text()).toContain('其他 1')
  })

  it('完成检查被挡住时逐条列出原因和处理建议，等人工与卡住分开着色', () => {
    const wrapper = mount(ClosureOutcomePanel, { props: { outcome: outcome() } })
    const gate = wrapper.find('[data-testid="closure-completion_gate"]')
    const blockers = gate.findAll('li')

    expect(blockers).toHaveLength(2)
    expect(blockers[0].text()).toContain('还有规则检查没有结果')
    expect(blockers[0].text()).toContain('等它们结算')
    expect(blockers[0].find('.tone-bad').exists()).toBe(true)
    expect(blockers[1].find('.tone-warn').exists()).toBe(true)
    expect(gate.text()).toContain('规则检查未结算 2')
    expect(gate.text()).toContain('待人工确认 5')
  })

  it('还没加载出结论时明说，不用空白冒充达标', () => {
    const wrapper = mount(ClosureOutcomePanel, { props: { outcome: null } })
    expect(wrapper.text()).toContain('收口结论尚未加载')
    expect(wrapper.findAll('[data-testid^="closure-"]').length).toBe(1)
  })

  it('可以收起展开', async () => {
    const wrapper = mount(ClosureOutcomePanel, {
      props: { outcome: outcome(), defaultExpanded: false },
    })
    expect(wrapper.find('[data-testid="closure-exploration"]').exists()).toBe(false)
    await wrapper.find('button.closure-head').trigger('click')
    expect(wrapper.find('[data-testid="closure-exploration"]').exists()).toBe(true)
  })
})
