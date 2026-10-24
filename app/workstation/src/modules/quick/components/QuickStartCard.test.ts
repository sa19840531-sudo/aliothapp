import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listWorkstationProducts: vi.fn(),
  listWorkstationDevices: vi.fn(),
  getV2Capabilities: vi.fn(),
  getRuleCatalog: vi.fn(),
  uploadAppBuild: vi.fn(),
  submitV2Task: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    NIcon: defineComponent({ setup: (_, { slots }) => () => h('span', slots.default?.()) }),
    useMessage: () => mocks.message,
  }
})
vi.mock('@vicons/ionicons5', async () => {
  const { defineComponent, h } = await import('vue')
  return { LayersOutline: defineComponent({ render: () => h('i') }) }
})
vi.mock('@/services/app-builds.api', () => ({ uploadAppBuild: mocks.uploadAppBuild }))
vi.mock('@/services/capabilities.api', () => ({ getV2Capabilities: mocks.getV2Capabilities }))
vi.mock('@/services/devices.api', () => ({ listWorkstationDevices: mocks.listWorkstationDevices }))
vi.mock('@/services/products.api', () => ({ listWorkstationProducts: mocks.listWorkstationProducts }))
vi.mock('@/services/ruleCatalog.api', () => ({ getRuleCatalog: mocks.getRuleCatalog }))
vi.mock('@/services/tasks.api', () => ({ submitV2Task: mocks.submitV2Task }))
vi.mock('@/stores/auth', () => ({
  authStore: { user: { value: { username: 'u', role: '部门账号', tenant_name: '北京分公司', department_name: '一部' } } },
}))

import QuickStartCard from './QuickStartCard.vue'

function catalogRuleset(overrides: Record<string, unknown> = {}) {
  return {
    ruleset_id: 'rs-1',
    ruleset_code: 'dept_ruleset',
    version: 3,
    name: '玉衡平台首期质检规则集',
    description: '',
    status: 'enabled',
    enabled_rule_count: 2,
    rule_codes: [],
    ...overrides,
  }
}

function ruleCatalog(rulesets = [catalogRuleset()]) {
  return {
    rulesets,
    rules: [],
    page_types: [
      { code: 'home', name: '首页' },
      { code: 'content_list', name: '内容列表页' },
      { code: 'video_list', name: '视频列表页' },
    ].map((item) => ({
      ...item,
      category: 'business',
      rule_matchable: true,
      coverage_required: true,
      description: '',
      related_rule_codes: [],
    })),
    page_scope_options: {
      h5: ['home', 'content_list', 'video_list'],
      web: ['video_list', 'content_list'],
      app: ['home', 'content_list', 'video_list'],
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listWorkstationProducts.mockResolvedValue({
    items: [{ project_code: 'APP-001', product_name: '示例产品', h5_url: 'https://example.test/h5' }],
  })
  mocks.listWorkstationDevices.mockResolvedValue({ items: [] })
  mocks.getV2Capabilities.mockResolvedValue({
    web: { enabled: true, execution_modes: ['full_inspection'], default_browser_profile_id: 'p', browser_profiles: [] },
  })
  mocks.getRuleCatalog.mockResolvedValue(ruleCatalog())
  mocks.uploadAppBuild.mockResolvedValue({
    platform_build_ref: 'app-build:quick',
    status: 'verified',
    original_filename: 'shop.apk',
    sha256: 'x',
    size_bytes: 1024,
    package_name: 'com.example.shop',
    version_name: '1.0',
    version_code: 1,
  })
  mocks.submitV2Task.mockResolvedValue({ task_id: 'task-quick', run_id: 'run', ruleset_id: 'rs', status: 'queued' })
})

async function mountCard() {
  const wrapper = mount(QuickStartCard)
  await flushPromises()
  return wrapper
}

function hintText(wrapper: ReturnType<typeof mount>) {
  return wrapper.get('[data-testid="quick-hint"]').text()
}

function urlValue(wrapper: ReturnType<typeof mount>) {
  return (wrapper.get('[data-testid="target-url"]').element as HTMLInputElement).value
}

const PRODUCTS = [
  { project_code: 'SC04', product_name: '黑料六点半', h5_url: 'https://hl630.example.test/' },
  { project_code: 'SC05', product_name: '海角社区', h5_url: 'https://haijiao.example.test/h5' },
  { project_code: 'APP-9', product_name: '只有安装包的产品', h5_url: null },
]

async function mountUrlCardWithProducts() {
  mocks.listWorkstationProducts.mockResolvedValueOnce({ items: PRODUCTS })
  const wrapper = await mountCard()
  await wrapper.get('[data-testid="mode-url"]').trigger('click')
  return wrapper
}

async function pickProduct(wrapper: ReturnType<typeof mount>, code: string) {
  await wrapper.get('[data-testid="product-select"]').setValue(code)
  await flushPromises()
}

describe('QuickStartCard（快速开始质检）', () => {
  it('头部不再有批量质检按钮和当前工作空间，规则方案换成规则集与页面范围选择器', async () => {
    const wrapper = await mountCard()
    expect(wrapper.find('.quick-head button').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('当前工作空间')
    expect(wrapper.text()).not.toContain('规则方案')
    expect(wrapper.get('[data-testid="quick-ruleset"] .multi-trigger-label').text()).toBe('全部')
    expect(wrapper.get('[data-testid="quick-page-scope"] .multi-trigger-label').text()).toBe('全部')
  })

  it('安装包入口：页面范围全部时回显「全部可匹配页型」，提交空页面列表和选定规则集', async () => {
    const wrapper = await mountCard()
    const apk = new File(['PK'], 'shop.apk', { type: 'application/vnd.android.package-archive' })
    const input = wrapper.get('[data-testid="apk-input"]')
    Object.defineProperty(input.element, 'files', { value: [apk], configurable: true })
    await input.trigger('change')
    await flushPromises()

    expect(hintText(wrapper)).toBe(
      '已选择 玉衡平台首期质检规则集 · 2 条规则，页面范围 全部可匹配页型；提交后进入执行队列',
    )
    await wrapper.get('[data-testid="quick-submit"]').trigger('click')
    await flushPromises()

    const payload = mocks.submitV2Task.mock.calls[0][0]
    expect(payload.ruleset_codes).toEqual(['dept_ruleset'])
    expect(payload).not.toHaveProperty('ruleset_code')
    expect(payload.target).toEqual({
      platform: 'app',
      source: { type: 'build_ref', platform_build_ref: 'app-build:quick' },
      package_name: 'com.example.shop',
      device: { backend: 'cloud_phone', profile_id: 'qa-b' },
      exploration_target: { required_page_types: [] },
    })
    expect(wrapper.emitted('submitted')?.[0]).toEqual([{ task_id: 'task-quick', task_name: '示例产品 1.0 App 质检' }])
  })

  it('H5 / Web 地址一行放地址、规则集、页面范围和「识别并创建任务」，只选部分页面时回显 N 类', async () => {
    const wrapper = await mountCard()
    await wrapper.get('[data-testid="mode-url"]').trigger('click')

    const row = wrapper.get('.url-row')
    expect(row.find('[data-testid="target-url"]').exists()).toBe(true)
    expect(row.find('[data-testid="quick-ruleset"]').exists()).toBe(true)
    expect(row.find('[data-testid="quick-page-scope"]').exists()).toBe(true)
    expect(row.get('[data-testid="quick-submit"]').text()).toBe('识别并创建任务')

    await wrapper.get('[data-testid="target-url"]').setValue('https://example.test/h5/list')
    await wrapper.get('[data-testid="quick-page-scope"] input[value="content_list"]').setValue(false)
    await wrapper.get('[data-testid="quick-page-scope"] input[value="video_list"]').setValue(false)
    expect(hintText(wrapper)).toBe(
      '已选择 玉衡平台首期质检规则集 · 2 条规则，页面范围 1 类；提交后进入执行队列',
    )

    await wrapper.get('[data-testid="quick-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.submitV2Task.mock.calls[0][0].target).toEqual({
      platform: 'h5',
      url: 'https://example.test/h5/list',
      exploration_target: { required_page_types: ['home'] },
    })
  })

  it('选了产品就带出它登记的地址：换产品跟着换，换成没登记地址的产品时清掉', async () => {
    const wrapper = await mountUrlCardWithProducts()
    expect(urlValue(wrapper)).toBe('')

    await pickProduct(wrapper, 'SC04')
    expect(urlValue(wrapper)).toBe('https://hl630.example.test/')
    await pickProduct(wrapper, 'SC05')
    expect(urlValue(wrapper)).toBe('https://haijiao.example.test/h5')
    await pickProduct(wrapper, 'APP-9')
    expect(urlValue(wrapper)).toBe('')
  })

  it('手填的地址不被选产品覆盖；按地址自动匹配出产品时地址保持原样', async () => {
    const wrapper = await mountUrlCardWithProducts()
    await wrapper.get('[data-testid="target-url"]').setValue('https://hl630.example.test/video/123')
    await flushPromises()
    // 主域一致自动匹配到产品，地址还是用户填的深层页面
    expect((wrapper.get('[data-testid="product-select"]').element as HTMLSelectElement).value).toBe('SC04')
    expect(urlValue(wrapper)).toBe('https://hl630.example.test/video/123')

    await pickProduct(wrapper, 'SC05')
    expect(urlValue(wrapper)).toBe('https://hl630.example.test/video/123')
  })

  it('只有一个产品时自动选中，切到 H5 / Web 地址就带出它登记的地址', async () => {
    const wrapper = await mountCard()
    await wrapper.get('[data-testid="mode-url"]').trigger('click')
    await flushPromises()
    expect(urlValue(wrapper)).toBe('https://example.test/h5')
  })

  it('多个规则集可以一起跑：回显规则集个数和规则总数，提交时带上全部选中的规则集', async () => {
    mocks.getRuleCatalog.mockResolvedValueOnce(
      ruleCatalog([catalogRuleset(), catalogRuleset({ ruleset_id: 'rs-2', ruleset_code: 'basic', name: '基础合规规则集' })]),
    )
    const wrapper = await mountCard()
    await wrapper.get('[data-testid="mode-url"]').trigger('click')
    await wrapper.get('[data-testid="target-url"]').setValue('https://example.test/h5')

    expect(hintText(wrapper)).toBe('已选择 2 个规则集 · 4 条规则，页面范围 全部可匹配页型；提交后进入执行队列')
    expect(wrapper.get('[data-testid="quick-submit"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="quick-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.submitV2Task.mock.calls[0][0].ruleset_codes).toEqual(['dept_ruleset', 'basic'])

    // 提交成功后地址清空；重新填地址、只留一个规则集时回显那一个
    await wrapper.get('[data-testid="target-url"]').setValue('https://example.test/h5')
    await wrapper.get('[data-testid="quick-ruleset"] input[value="rs-1"]').setValue(false)
    expect(hintText(wrapper)).toContain('已选择 基础合规规则集 · 2 条规则')
  })
})
