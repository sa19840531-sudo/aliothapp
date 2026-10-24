import { describe, expect, it, beforeEach, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  message: { success: vi.fn(), error: vi.fn() },
  listWorkstationProducts: vi.fn(),
  listTasks: vi.fn(),
  listCandidateRules: vi.fn(),
  submitRuleEvaluationRun: vi.fn(),
  listRuleEvaluationRuns: vi.fn(),
  getRuleEvaluationRun: vi.fn(),
  getTask: vi.fn(),
  routeQuery: {} as Record<string, string>,
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: mocks.routeQuery }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => mocks.message,
}))

vi.mock('@/services/products.api', () => ({
  listWorkstationProducts: mocks.listWorkstationProducts,
}))

vi.mock('@/services/tasks.api', () => ({
  getTask: mocks.getTask,
  listTasks: mocks.listTasks,
  listCandidateRules: mocks.listCandidateRules,
}))

vi.mock('@/services/rule-test.api', () => ({
  submitRuleEvaluationRun: mocks.submitRuleEvaluationRun,
  listRuleEvaluationRuns: mocks.listRuleEvaluationRuns,
  getRuleEvaluationRun: mocks.getRuleEvaluationRun,
}))

import RuleTestView from './RuleTestView.vue'

function rule(ruleCode: string) {
  return {
    rule_id: `rule-${ruleCode}`,
    rule_code: ruleCode,
    name: `规则 ${ruleCode}`,
    category: 'visual',
    severity: 'medium',
    executor_type: 'hybrid',
    page_type: 'home',
    applicable_page_types: ['home'],
    check_point: '',
    judge_config: {},
    sort_order: 1,
    match_reason: 'applicable_page_types',
  }
}

beforeEach(() => {
  mocks.routeQuery = {}
  mocks.listWorkstationProducts.mockResolvedValue({
    items: [{ project_code: 'WEB-001', product_name: '样例产品', h5_url: 'https://example.test' }],
  })
  mocks.listTasks.mockResolvedValue({
    items: [
      {
        task_id: 'task-1',
        task_name: '样例任务',
        status: 'succeeded',
        created_at: '2026-08-11T10:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    page_size: 20,
  })
  mocks.getTask.mockResolvedValue({
    task_id: 'task-deep-link',
    task_name: '深链任务',
    product: { department_name: '鼎丰', product_code: 'APP-001', product_name: 'App' },
    target_url: null,
    target_platform: 'app',
    execution_mode: 'exploration_only',
    journey_steps: [],
    status: 'done',
    progress: 100,
    report_status: 'not_applicable',
    failure_reason: null,
    available_actions: ['detail'],
    created_at: '2026-08-14T08:00:00Z',
    started_at: null,
    finished_at: null,
    ruleset_code: 'alioth_first_ruleset',
    ruleset_version: 1,
    channel: 'workbench',
    priority: 100,
    run_id: 'run-1',
    scan_mode: 'full',
    remark: null,
  })
  mocks.listCandidateRules.mockResolvedValue({
    items: [rule('alioth_1'), rule('alioth_2')],
  })
  mocks.listRuleEvaluationRuns.mockResolvedValue({ task_id: 'task-1', runs: [] })
  mocks.submitRuleEvaluationRun.mockResolvedValue({
    task_id: 'task-1',
    rule_evaluation_run_id: 'rule-eval:abcdef123456',
    status: 'pending',
  })
})

async function mountAndPickTask() {
  const wrapper = mount(RuleTestView)
  await flushPromises()
  await wrapper.get('[data-test="task-select"]').setValue('task-1')
  await flushPromises()
  return wrapper
}

describe('规则测试页', () => {
  it('深链任务不在当前列表页时仍按 task 参数精确加载并选中', async () => {
    mocks.routeQuery = { task: 'task-deep-link' }

    const wrapper = mount(RuleTestView)
    await flushPromises()

    expect(mocks.getTask).toHaveBeenCalledWith('task-deep-link')
    expect(mocks.listCandidateRules).toHaveBeenCalledWith('task-deep-link')
    expect(wrapper.get('[data-test="task-select"]').element).toHaveProperty(
      'value',
      'task-deep-link',
    )
    wrapper.unmount()
  })

  it('选任务后加载候选规则并默认全选，提交时测全部', async () => {
    const wrapper = await mountAndPickTask()

    const checkboxes = wrapper.findAll('.rules-table input[type="checkbox"]')
    expect(checkboxes).toHaveLength(2)
    expect(checkboxes.every((box) => (box.element as HTMLInputElement).checked)).toBe(true)
    expect(mocks.listRuleEvaluationRuns).toHaveBeenCalledWith('task-1')

    await wrapper.get('.submit-run-button').trigger('click')
    await flushPromises()

    expect(mocks.submitRuleEvaluationRun).toHaveBeenCalledWith('task-1', undefined)
    expect(mocks.message.success).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('勾掉规则后提交携带剩余子集', async () => {
    const wrapper = await mountAndPickTask()

    const checkboxes = wrapper.findAll('.rules-table input[type="checkbox"]')
    await checkboxes[1].trigger('click')
    await wrapper.get('.submit-run-button').trigger('click')
    await flushPromises()

    expect(mocks.submitRuleEvaluationRun).toHaveBeenCalledWith('task-1', ['alioth_1'])
    wrapper.unmount()
  })

  it('任务没有候选规则时展示空态引导', async () => {
    mocks.listCandidateRules.mockResolvedValue({ items: [] })
    const wrapper = await mountAndPickTask()

    expect(wrapper.text()).toContain('暂无可测规则')
    wrapper.unmount()
  })
})
