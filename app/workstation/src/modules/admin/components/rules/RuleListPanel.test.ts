import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listRules: vi.fn(),
  listRulesets: vi.fn(),
  patchRule: vi.fn(),
  setRuleStatus: vi.fn(),
  createBusinessRule: vi.fn(),
  getRuleCatalog: vi.fn(),
  getH5ShortflowCatalog: vi.fn(),
  message: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('naive-ui', () => ({
  useMessage: () => mocks.message,
  NDrawer: { props: ['show'], template: '<div v-if="show"><slot /></div>' },
  NDrawerContent: { props: ['title'], template: '<section><h2>{{ title }}</h2><slot /></section>' },
  NSwitch: {
    props: ['value', 'loading'],
    emits: ['update:value'],
    template: '<button type="button" class="stub-switch" @click="$emit(\'update:value\', !value)">{{ value ? \'开\' : \'关\' }}</button>',
  },
  NSelect: {
    props: ['value', 'options'],
    emits: ['update:value'],
    template:
      '<select multiple class="stub-select" @change="$emit(\'update:value\', Array.from($event.target.selectedOptions).map((o) => o.value))"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>',
  },
}))
vi.mock('../../api/rules', () => ({
  listRules: mocks.listRules,
  listRulesets: mocks.listRulesets,
  patchRule: mocks.patchRule,
  setRuleStatus: mocks.setRuleStatus,
  createBusinessRule: mocks.createBusinessRule,
}))
vi.mock('@/services/ruleCatalog.api', () => ({ getRuleCatalog: mocks.getRuleCatalog }))
vi.mock('@/services/h5-shortflows.api', () => ({ getH5ShortflowCatalog: mocks.getH5ShortflowCatalog }))

import RuleListPanel from './RuleListPanel.vue'

const RULE = {
  id: 'r1',
  rule_code: 'alioth_1',
  name: '首页弹窗可以关闭',
  category: 'ad',
  severity: 'high',
  executor_type: 'llm',
  status: 'enabled',
  check_point: '弹窗要有能点的关闭按钮',
  applicable_page_types: ['home', 'video_list'],
  ruleset_id: 'rs1',
  ruleset_code: 'alioth_default',
  ruleset_name: '默认规则集',
  input_contract: { target_clients: ['H5'] },
}

describe('规则配置 · 规则明细', () => {
  it('在原规则抽屉切换流程证据合同，不提交完整系统配置', async () => {
    const wrapper = mount(RuleListPanel, { props: { canWrite: true } })
    await flushPromises()
    await wrapper.findAll('button').find((node) => node.text() === '编辑')!.trigger('click')
    await wrapper.get('[data-testid="rule-form-evidence-scope"]').setValue('journey')
    await wrapper.findAll('button').find((node) => node.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(mocks.patchRule).toHaveBeenCalledWith('r1', expect.objectContaining({ evidence_input_scope: 'journey' }))
    expect(mocks.patchRule.mock.calls[0]![1]).not.toHaveProperty('judge_config')
  })

  it('未知汇总合同默认保留，不按页面合同覆盖', async () => {
    mocks.listRules.mockResolvedValue({ items: [{ ...RULE, judge_config: { capability_config: {
      evidence_input_scope: 'environment_matrix', evidence_scope_key: 'environment_variation',
    } } }], total: 1 })
    const wrapper = mount(RuleListPanel, { props: { canWrite: true } })
    await flushPromises()
    await wrapper.findAll('button').find((node) => node.text() === '编辑')!.trigger('click')
    expect(wrapper.get('[data-testid="rule-form-evidence-scope"]').attributes('disabled')).toBeDefined()
    await wrapper.findAll('button').find((node) => node.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(mocks.patchRule.mock.calls[0]![1]).not.toHaveProperty('evidence_input_scope')
  })
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.listRules.mockResolvedValue({ items: [RULE], total: 1, page: 1, page_size: 20 })
    mocks.listRulesets.mockResolvedValue({
      items: [
        { id: 'rs0', name: '旧规则集', version: 1, status: 'disabled' },
        { id: 'rs1', name: '默认规则集', version: 3, status: 'enabled' },
      ],
      total: 2,
    })
    mocks.getRuleCatalog.mockResolvedValue({
      page_types: [
        { code: 'home', name: '首页' },
        { code: 'video_list', name: '视频列表' },
      ],
    })
    mocks.getH5ShortflowCatalog.mockResolvedValue({ definitions: [{
      definition_id: 'player', version: 1, content_hash: 'a'.repeat(64), name: '暂停流程',
      workflow_skill: 'h5-player-workflow', checkpoints: [{ checkpoint_id: 'pause', depends_on: [] }],
      evidence_conditions: [{ checkpoint_id: 'pause', requirements: [{
        role: 'paused', artifact_types: ['screenshot', 'dom'], minimum_count: 1, capture_phase: 'after_action',
      }] }],
    }], rules: [] })
  })

  it('第22行编辑实际发布流程、场景和证据条件，往返保存而非只改输入范围', async () => {
    const wrapper = mount(RuleListPanel, { props: { canWrite: true } })
    await flushPromises()
    await wrapper.findAll('button').find((node) => node.text() === '编辑')!.trigger('click')
    await wrapper.get('[data-testid="rule-form-evidence-scope"]').setValue('journey')
    await flushPromises()
    await wrapper.get('[data-testid="journey-definition"]').setValue('player:1')
    await wrapper.get('[data-testid="journey-scenario"]').setValue('pause')
    await wrapper.findAll('button').find((node) => node.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(mocks.patchRule).toHaveBeenCalledWith('r1', expect.objectContaining({
      journey_rule_contract: expect.objectContaining({ definition_id: 'player', scenario_key: 'pause',
        content_hash: 'a'.repeat(64), evidence_conditions: [{ checkpoint_id: 'pause', requirements: [{
          role: 'paused', artifact_types: ['screenshot', 'dom'], minimum_count: 1, capture_phase: 'after_action',
        }] }] }),
    }))
    expect(mocks.patchRule.mock.calls[0]![1]).not.toHaveProperty('judge_config')
  })

  it('只读账号只看列表，没有新增、编辑和开关', async () => {
    const wrapper = mount(RuleListPanel, { props: { canWrite: false } })
    await flushPromises()

    const table = wrapper.find('[data-testid="rule-table"]').text()
    expect(table).toContain('首页弹窗可以关闭')
    expect(table).toContain('默认规则集')
    expect(table).toContain('2 个页型')
    expect(table).toContain('已开启')
    expect(wrapper.find('[data-testid="rule-create"]').exists()).toBe(false)
    expect(wrapper.find('.stub-switch').exists()).toBe(false)
    expect(table).not.toContain('编辑')
  })

  it('管理员用开关启停规则，新增规则只填四项并加到启用中的规则集', async () => {
    mocks.setRuleStatus.mockResolvedValue({ ...RULE, status: 'disabled' })
    mocks.createBusinessRule.mockResolvedValue({ ...RULE, id: 'r2' })
    const wrapper = mount(RuleListPanel, { props: { canWrite: true } })
    await flushPromises()

    await wrapper.find('[data-testid="rule-switch-alioth_1"]').trigger('click')
    await flushPromises()
    expect(mocks.setRuleStatus).toHaveBeenCalledWith('r1', 'disabled')

    await wrapper.find('[data-testid="rule-create"]').trigger('click')
    await wrapper.find('[data-testid="rule-form-name"]').setValue('首页不应出现错别字')
    await wrapper.find('[data-testid="rule-form-detail"]').setValue('首页文案不能有错别字')
    await wrapper.find('.stub-select').setValue(['home'])
    await wrapper.find('[data-testid="rule-form-severity"]').setValue('low')
    const save = wrapper.findAll('button').find((node) => node.text() === '保存')
    await save!.trigger('click')
    await flushPromises()

    expect(mocks.createBusinessRule).toHaveBeenCalledWith('rs1', {
      name: '首页不应出现错别字',
      check_point: '首页文案不能有错别字',
      applicable_page_types: ['home'],
      severity: 'low',
    })
    expect(mocks.message.success).toHaveBeenCalledWith('规则已新增，默认关闭；测试没问题后再开启')
  })
  it('风险、来源和适用页面组合筛选保留条件并返回第一页，具体规则集版本按 ID 查看', async () => {
    const wrapper = mount(RuleListPanel, { props: { canWrite: false, selectedRulesetId: 'rs0' } })
    await flushPromises()
    expect(mocks.listRules).toHaveBeenLastCalledWith(expect.objectContaining({ ruleset_id: 'rs0', page: 1 }))
    await wrapper.find('[data-testid="rule-severity-filter"]').setValue('high')
    await wrapper.find('[data-testid="rule-source-filter"]').setValue('spreadsheet')
    await wrapper.find('[data-testid="rule-page-filter"]').setValue('home')
    await flushPromises()
    expect(mocks.listRules).toHaveBeenLastCalledWith(expect.objectContaining({
      ruleset_id: 'rs0', severity: 'high', source_type: 'spreadsheet', page_type: 'home', page: 1,
    }))
    await wrapper.setProps({ selectedRulesetId: 'rs1' })
    await flushPromises()
    expect(mocks.listRules).toHaveBeenLastCalledWith(expect.objectContaining({ ruleset_id: 'rs1', severity: 'high', page: 1 }))
  })

  it('快速改筛选时旧查询较晚返回不能覆盖新结果', async () => {
    let finishOld!: (value: unknown) => void
    mocks.listRules.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve }))
    const wrapper = mount(RuleListPanel, { props: { canWrite: false } })
    await wrapper.find('[data-testid="rule-severity-filter"]').setValue('high')
    await flushPromises()
    finishOld({ items: [{ ...RULE, name: '过时结果' }], total: 1 })
    await flushPromises()
    expect(wrapper.text()).toContain(RULE.name)
    expect(wrapper.text()).not.toContain('过时结果')
  })

})
