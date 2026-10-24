import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  getRuleCatalog: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/rules' }), useRouter: () => ({ push: mocks.push }) }))
vi.mock('naive-ui', () => ({
  NDrawer: { props: ['show'], template: '<div v-if="show"><slot /></div>' },
  NDrawerContent: { props: ['title'], template: '<section><h2>{{ title }}</h2><slot /></section>' },
}))
vi.mock('@/services/ruleCatalog.api', () => ({ getRuleCatalog: mocks.getRuleCatalog }))
vi.mock('@/components/pickers/SearchSelect.vue', () => ({
  default: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><slot /></select>',
  },
}))

import RuleCenterView from './RuleCenterView.vue'

const CATALOG = {
  rulesets: [
    {
      ruleset_id: 'rs1',
      ruleset_code: 'alioth_default',
      version: 3,
      name: '默认规则集',
      description: '平台默认检查项',
      status: 'enabled',
      enabled_rule_count: 2,
      rule_codes: ['alioth_1', 'alioth_2'],
    },
  ],
  rules: [
    {
      rule_id: 'r1',
      ruleset_id: 'rs1',
      rule_code: 'alioth_1',
      name: '首页弹窗可以关闭',
      category: 'ad',
      severity: 'high',
      executor_type: 'llm',
      check_point: '弹窗要有能点的关闭按钮',
      applicable_page_types: ['home'],
      source_type: 'spreadsheet',
      ruleset_code: 'alioth_default',
      ruleset_version: 3,
    },
    {
      rule_id: 'r2',
      ruleset_id: 'rs1',
      rule_code: 'alioth_2',
      name: '充值金额前后一致',
      category: 'payment',
      severity: 'critical',
      executor_type: 'llm',
      check_point: '下单金额和收银台金额一致',
      applicable_page_types: ['payment'],
      source_type: 'manual',
      ruleset_code: 'alioth_default',
      ruleset_version: 3,
    },
  ],
  page_types: [
    { code: 'home', name: '首页', category: 'business', rule_matchable: true, coverage_required: true, description: '产品首页', related_rule_codes: ['alioth_1'] },
    { code: 'splash', name: '开屏页', category: 'lifecycle', rule_matchable: false, coverage_required: false, description: '启动开屏', related_rule_codes: [] },
  ],
  page_scope_options: { h5: ['home'], web: [], app: ['home'] },
}

describe('部门规则中心', () => {
  beforeEach(() => {
    mocks.push.mockReset()
    mocks.getRuleCatalog.mockReset().mockResolvedValue(CATALOG)
  })

  it('只读展示已发布的规则、页面和规则集，没有新增和编辑入口', async () => {
    const wrapper = mount(RuleCenterView)
    await flushPromises()

    const rules = wrapper.find('[data-testid="rule-center-rules"]').text()
    expect(rules).toContain('首页弹窗可以关闭')
    expect(rules).toContain('严重')
    expect(wrapper.text()).toContain('共 2 条已开启规则')
    expect(wrapper.text()).not.toContain('新增规则')
    expect(wrapper.text()).not.toContain('编辑')

    await wrapper.find('[data-testid="rule-center-keyword"]').setValue('充值')
    expect(wrapper.text()).toContain('共 1 条已开启规则')

    await wrapper.find('[data-testid="rule-center-tab-pages"]').trigger('click')
    const pages = wrapper.find('[data-testid="rule-center-pages"]').text()
    expect(pages).toContain('1 条：首页弹窗可以关闭')
    expect(pages).toContain('未关联')

    await wrapper.find('[data-testid="rule-center-tab-sets"]').trigger('click')
    expect(wrapper.find('[data-testid="rule-center-sets"]').text()).toContain('包含 2 条已开启规则')
  })

  it('从规则详情反馈，带上规则编码和名称跳到我的反馈', async () => {
    const wrapper = mount(RuleCenterView)
    await flushPromises()

    await wrapper.find('[data-testid="rule-center-rules"] .qc-link').trigger('click')
    expect(wrapper.find('[data-testid="rule-center-rule-detail"]').text()).toContain('默认规则集 · v3')
    const action = wrapper.findAll('button').find((node) => node.text() === '反馈这条规则')
    await action!.trigger('click')
    expect(mocks.push).toHaveBeenCalledWith({
      path: '/feedback',
      query: { tab: 'my', source: 'department-rules', rule: 'alioth_1', name: '首页弹窗可以关闭' },
    })
  })

  it('风险、真实来源、适用页面和关键词组合筛选，重置恢复全量', async () => {
    const wrapper = mount(RuleCenterView)
    await flushPromises()
    const table = () => wrapper.get('[data-testid="rule-center-rules"]').text()

    await wrapper.get('[data-testid="rule-center-severity"]').setValue('high')
    await wrapper.get('[data-testid="rule-center-source"]').setValue('spreadsheet')
    await wrapper.get('[data-testid="rule-center-page"]').setValue('home')
    await wrapper.get('[data-testid="rule-center-keyword"]').setValue('alioth_1')
    expect(table()).toContain('首页弹窗可以关闭')
    expect(table()).not.toContain('充值金额前后一致')
    expect(wrapper.text()).toContain('共 1 条已开启规则')

    await wrapper.get('[data-testid="rule-center-source"]').setValue('manual')
    expect(table()).toContain('没有符合条件的规则')

    await wrapper.get('[data-testid="rule-center-reset"]').trigger('click')
    expect(table()).toContain('首页弹窗可以关闭')
    expect(table()).toContain('充值金额前后一致')
    expect(wrapper.text()).toContain('共 2 条已开启规则')
  })

  it('没有来源记录的规则保持未知，不按执行器猜测来源', async () => {
    mocks.getRuleCatalog.mockResolvedValue({
      ...CATALOG,
      rules: [{ ...CATALOG.rules[0], source_type: null }, CATALOG.rules[1]],
    })
    const wrapper = mount(RuleCenterView)
    await flushPromises()
    await wrapper.get('[data-testid="rule-center-source"]').setValue('__unknown__')
    expect(wrapper.get('[data-testid="rule-center-rules"]').text()).toContain('首页弹窗可以关闭')
    expect(wrapper.get('[data-testid="rule-center-rules"]').text()).not.toContain('充值金额前后一致')
    await wrapper.get('[data-testid="rule-center-rules"] .qc-link').trigger('click')
    expect(wrapper.get('[data-testid="rule-center-rule-detail"]').text()).toContain('来源未记录')
  })

  it('点击规则集只读展示该期完整规则，不混入同编码其他版本或其他版本ID', async () => {
    mocks.getRuleCatalog.mockResolvedValue({
      ...CATALOG,
      rulesets: [
        CATALOG.rulesets[0],
        { ...CATALOG.rulesets[0], ruleset_id: 'rs2', version: 4, enabled_rule_count: 1 },
      ],
      rules: [
        ...CATALOG.rules,
        { ...CATALOG.rules[0], rule_id: 'r3', ruleset_id: 'rs2', ruleset_version: 4, name: '新版弹窗检查', check_point: '新版检查内容' },
        { ...CATALOG.rules[0], rule_id: 'r4', ruleset_id: 'rs-other', name: '其他版本ID规则' },
      ],
    })
    const wrapper = mount(RuleCenterView)
    await flushPromises()
    // 规则明细筛选不应删去规则集详情里本来属于该版本的规则。
    await wrapper.get('[data-testid="rule-center-severity"]').setValue('high')
    await wrapper.get('[data-testid="rule-center-tab-sets"]').trigger('click')
    await wrapper.get('[data-testid="rule-center-ruleset-rs1"]').trigger('click')
    let detail = wrapper.get('[data-testid="rule-center-ruleset-detail"]').text()
    expect(detail).toContain('该版本共 2 条已开启规则')
    expect(detail).toContain('首页弹窗可以关闭')
    expect(detail).toContain('充值金额前后一致')
    expect(detail).toContain('下单金额和收银台金额一致')
    expect(detail).not.toContain('新版弹窗检查')
    expect(detail).not.toContain('其他版本ID规则')

    await wrapper.get('[data-testid="rule-center-ruleset-rs2"]').trigger('keydown', { key: 'Enter' })
    detail = wrapper.get('[data-testid="rule-center-ruleset-detail"]').text()
    expect(detail).toContain('该版本共 1 条已开启规则')
    expect(detail).toContain('新版检查内容')
    expect(detail).not.toContain('充值金额前后一致')
  })

  it('兼容旧目录时同时按编码与版本展示，空规则集不借用当前版本规则', async () => {
    mocks.getRuleCatalog.mockResolvedValue({
      ...CATALOG,
      rulesets: [CATALOG.rulesets[0], { ...CATALOG.rulesets[0], ruleset_id: 'rs-empty', version: 5, enabled_rule_count: 0 }],
      rules: CATALOG.rules.map((rule) => ({ ...rule, ruleset_id: undefined })),
    })
    const wrapper = mount(RuleCenterView)
    await flushPromises()
    await wrapper.get('[data-testid="rule-center-tab-sets"]').trigger('click')
    await wrapper.get('[data-testid="rule-center-ruleset-rs1"]').trigger('keydown', { key: ' ' })
    expect(wrapper.get('[data-testid="rule-center-ruleset-detail"]').text()).toContain('该版本共 2 条已开启规则')
    await wrapper.get('[data-testid="rule-center-ruleset-rs-empty"]').trigger('click')
    expect(wrapper.get('[data-testid="rule-center-ruleset-detail"]').text()).toContain('该版本没有已开启规则')
    expect(wrapper.get('[data-testid="rule-center-ruleset-detail"]').text()).not.toContain('首页弹窗可以关闭')
  })
})
