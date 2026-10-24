import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ getPageConfig: vi.fn() }))

vi.mock('naive-ui', () => ({
  NButton: { template: '<button type="button"><slot /></button>' },
  NDrawer: { props: ['show'], template: '<div v-if="show"><slot /></div>' },
  NDrawerContent: { props: ['title'], template: '<section><h2>{{ title }}</h2><slot /></section>' },
}))
vi.mock('@/services/config.api', () => ({ getPageConfig: mocks.getPageConfig }))
vi.mock('../components/ConfigCenterTabs.vue', () => ({
  default: { props: ['active'], template: '<div data-testid="config-tabs-stub">{{ active }}</div>' },
}))

import ConfigCenterView from './ConfigCenterView.vue'

function page(overrides: Record<string, unknown> = {}) {
  return {
    code: 'home',
    name: '首页',
    status: 'supported',
    description: '产品首页',
    feature_count: 3,
    bound_rule_count: 1,
    bound_rule_codes: ['alioth_1'],
    category: 'business',
    coverage_required: true,
    rule_matchable: true,
    purpose: '产品入口页',
    aliases: ['主页'],
    ocr_keywords: ['推荐', '热门'],
    dom_hints: [],
    layout_hints: ['顶部导航'],
    entry_hints: ['打开产品地址'],
    exit_hints: [],
    next_page_types: ['video_list'],
    risk_actions: [],
    route_prefixes: ['/'],
    navigation_aliases: [],
    default_detail_page_type: null,
    source: '识别目录',
    remark: '',
    bound_rules: [{ rule_code: 'alioth_1', name: '首页弹窗可以关闭', severity: 'high' }],
    ...overrides,
  }
}

describe('配置中心 · 页面识别配置', () => {
  beforeEach(() => {
    mocks.getPageConfig.mockReset().mockResolvedValue({
      catalog_version: 'v3',
      ruleset_code: 'alioth_default',
      ruleset_version: 3,
      stats: { page_type_total: 2, supported_total: 1, feature_total: 5, enabled_rule_total: 1, rule_binding_total: 1, covered_page_total: 1 },
      pages: [
        page(),
        page({ code: 'video_list', name: '视频列表', status: 'unsupported', bound_rules: [], bound_rule_codes: [], bound_rule_count: 0 }),
      ],
    })
  })

  it('展示统计、页面关联规则，新增页面按钮暂不可用，详情里有识别特征和关联规则', async () => {
    const wrapper = mount(ConfigCenterView)
    await flushPromises()

    expect(wrapper.find('[data-testid="config-tabs-stub"]').text()).toBe('page')
    expect(wrapper.text()).toContain('页面类型总数')
    const table = wrapper.find('[data-testid="page-config-table"]').text()
    expect(table).toContain('1 条：首页弹窗可以关闭')
    expect(table).toContain('未关联')
    expect(table).toContain('已关闭')
    const create = wrapper.findAll('button').find((node) => node.text() === '新增页面')
    expect(create!.attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="page-config-table"] .qc-link').trigger('click')
    const detail = wrapper.find('[data-testid="page-config-detail"]').text()
    expect(detail).toContain('推荐、热门')
    expect(detail).toContain('视频列表')
    expect(detail).toContain('首页弹窗可以关闭')
  })
})
