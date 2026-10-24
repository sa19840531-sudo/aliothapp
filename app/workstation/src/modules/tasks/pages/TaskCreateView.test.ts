import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listWorkstationProducts: vi.fn(),
  listWorkstationDevices: vi.fn(),
  getV2Capabilities: vi.fn(),
  getRuleCatalog: vi.fn(),
  getH5ShortflowCatalog: vi.fn(),
  uploadAppBuild: vi.fn(),
  submitV2Task: vi.fn(),
  submitExplorationTask: vi.fn(),
  listTaskDrafts: vi.fn(),
  createTaskDraft: vi.fn(),
  deleteTaskDraft: vi.fn(),
  routerPush: vi.fn(),
  message: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: mocks.routerPush }),
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const passthrough = defineComponent({
    setup(_, context) {
      return () => h('div', context.slots.default?.())
    },
  })
  const select = defineComponent({
    props: {
      value: { type: String, default: undefined },
      options: { type: Array, default: () => [] },
    },
    emits: ['update:value'],
    setup(props, { emit }) {
      return () =>
        h(
          'select',
          {
            value: props.value,
            onChange: (event: Event) =>
              emit('update:value', (event.target as HTMLSelectElement).value),
          },
          (props.options as Array<{ label: string; value: string; disabled?: boolean }>).map((option) =>
            h('option', { value: option.value, disabled: option.disabled }, option.label),
          ),
        )
    },
  })
  return {
    NConfigProvider: passthrough,
    NIcon: passthrough,
    NSelect: select,
    darkTheme: {},
    useMessage: () => mocks.message,
  }
})

vi.mock('@/services/app-builds.api', () => ({
  uploadAppBuild: mocks.uploadAppBuild,
}))
vi.mock('@/services/capabilities.api', () => ({
  getV2Capabilities: mocks.getV2Capabilities,
}))
vi.mock('@/services/ruleCatalog.api', () => ({
  getRuleCatalog: mocks.getRuleCatalog,
}))
vi.mock('@/services/products.api', () => ({
  listWorkstationProducts: mocks.listWorkstationProducts,
}))
vi.mock('@/services/task-drafts.api', () => ({
  listTaskDrafts: mocks.listTaskDrafts,
  createTaskDraft: mocks.createTaskDraft,
  deleteTaskDraft: mocks.deleteTaskDraft,
}))
vi.mock('@/services/devices.api', async () => {
  const actual = await vi.importActual<typeof import('@/services/devices.api')>(
    '@/services/devices.api',
  )
  return {
    ...actual,
    listWorkstationDevices: mocks.listWorkstationDevices,
  }
})
vi.mock('@/services/tasks.api', () => ({
  submitV2Task: mocks.submitV2Task,
  submitExplorationTask: mocks.submitExplorationTask,
}))
vi.mock('@/services/h5-shortflows.api', () => ({ getH5ShortflowCatalog: mocks.getH5ShortflowCatalog }))

import TaskCreateView from './TaskCreateView.vue'

const BUSINESS_PAGE_TYPES = ['home', 'content_list', 'content_detail', 'video_list', 'video_detail']
const WEB_PAGE_TYPES = ['video_list', 'video_detail', 'content_list', 'content_detail']
const PAGE_NAMES: Record<string, string> = {
  home: '首页',
  content_list: '内容列表页',
  content_detail: '内容详情页',
  video_list: '视频列表页',
  video_detail: '视频详情页',
}

function catalogRuleset(overrides: Record<string, unknown> = {}) {
  return {
    ruleset_id: 'rs-1',
    ruleset_code: 'dept_ruleset',
    version: 3,
    name: '玉衡平台首期质检规则集',
    description: '',
    status: 'enabled',
    enabled_rule_count: 2,
    rule_codes: ['alioth_5', 'alioth_8'],
    ...overrides,
  }
}

function catalogRule(code: string, name: string, category: string, rulesetCode = 'dept_ruleset') {
  return {
    rule_id: `id-${code}`,
    rule_code: code,
    name,
    category,
    severity: 'high',
    executor_type: 'hybrid',
    check_point: '',
    applicable_page_types: ['home'],
    ruleset_code: rulesetCode,
    ruleset_version: rulesetCode === 'dept_ruleset' ? 3 : 1,
  }
}

function ruleCatalog(overrides: Record<string, unknown> = {}) {
  return {
    rulesets: [catalogRuleset()],
    rules: [
      catalogRule('alioth_5', '页面模块空白兜底检查', 'visual'),
      catalogRule('alioth_8', '敏感词与文案风险检查', 'text'),
    ],
    page_types: Object.entries(PAGE_NAMES).map(([code, name]) => ({
      code,
      name,
      category: 'business',
      rule_matchable: true,
      coverage_required: true,
      description: '',
      related_rule_codes: [],
    })),
    page_scope_options: { h5: BUSINESS_PAGE_TYPES, web: WEB_PAGE_TYPES, app: BUSINESS_PAGE_TYPES },
    ...overrides,
  }
}

function webCapabilities(executionModes: string[]) {
  return {
    schema_version: 'v2-capabilities.1',
    web: {
      enabled: true,
      execution_modes: executionModes,
      default_browser_profile_id: 'desktop_fhd_1920x1080_v1',
      browser_profiles: [
        {
          profile_id: 'desktop_fhd_1920x1080_v1',
          viewport_width: 1920,
          viewport_height: 1080,
          screen_width: 1920,
          screen_height: 1080,
          device_scale_factor: 1,
          is_mobile: false,
          has_touch: false,
          color_scheme: 'light',
        },
      ],
    },
  }
}

beforeEach(() => {
  mocks.getH5ShortflowCatalog.mockResolvedValue({ definitions: [], rules: [] })
  vi.clearAllMocks()
  mocks.listWorkstationProducts.mockResolvedValue({
    items: [
      {
        project_code: 'APP-001',
        product_name: '示例产品',
        h5_url: 'https://example.test/h5',
      },
    ],
  })
  mocks.listWorkstationDevices.mockResolvedValue({
    items: [
      {
        device_ref: 'cloud_phone:qa-a',
        backend: 'cloud_phone',
        profile_id: 'qa-a',
        enabled: true,
        health_state: 'available',
        occupied: true,
        occupancy_status: 'occupied',
        lease_task_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        lease_session_id: 'session-a',
        lease_state: 'active',
        lease_expires_at: null,
        morelogin_env_id: 'env-a',
        queued_preferred_count: 1,
      },
      {
        device_ref: 'cloud_phone:qa-b',
        backend: 'cloud_phone',
        profile_id: 'qa-b',
        enabled: true,
        health_state: 'available',
        occupied: false,
        occupancy_status: 'available',
        lease_task_id: null,
        lease_session_id: null,
        lease_state: null,
        lease_expires_at: null,
        morelogin_env_id: 'env-b',
        queued_preferred_count: 0,
      },
      {
        device_ref: 'ldplayer:qa-a',
        backend: 'ldplayer',
        profile_id: 'qa-a',
        enabled: true,
        health_state: 'available',
        occupied: false,
        occupancy_status: 'available',
        lease_task_id: null,
        lease_session_id: null,
        lease_state: null,
        lease_expires_at: null,
        morelogin_env_id: '',
        queued_preferred_count: 0,
      },
    ],
  })
  mocks.getV2Capabilities.mockResolvedValue(webCapabilities(['exploration_only']))
  mocks.getRuleCatalog.mockResolvedValue(ruleCatalog())
  mocks.listTaskDrafts.mockResolvedValue({ items: [] })
  mocks.createTaskDraft.mockImplementation(async (payload: { name: string }) => ({
    id: 'draft-1',
    name: payload.name,
    target_platform: 'app',
    payload: {},
    created_at: '2026-09-11T10:00:00Z',
    updated_at: '2026-09-11T10:00:00Z',
  }))
  mocks.uploadAppBuild.mockResolvedValue({
    platform_build_ref: 'app-build:uploaded',
    package_name: null,
    original_filename: 'demo.apk',
  })
  mocks.submitV2Task.mockResolvedValue({
    task_id: 'task-v2',
    run_id: 'run-v2',
    ruleset_id: 'ruleset-v2',
    status: 'queued',
  })
  mocks.submitExplorationTask.mockResolvedValue({
    task_id: 'task-exploration',
    run_id: 'run-exploration',
    status: 'queued',
  })
})

type Wrapper = ReturnType<typeof mount>

async function mountTaskCreateView() {
  const wrapper = mount(TaskCreateView)
  await flushPromises()
  return wrapper
}

function findSelectWithOption(wrapper: Wrapper, text: string) {
  return wrapper.findAll('select').find((select) => select.text().includes(text))
}

function stepShown(wrapper: Wrapper, testId: string) {
  return !(wrapper.get(`[data-testid="${testId}"]`).attributes('style') ?? '').includes('display: none')
}

async function fillBasics(wrapper: Wrapper, taskName: string) {
  await wrapper.get('[data-testid="task-name"]').setValue(taskName)
  await findSelectWithOption(wrapper, 'APP-001')!.setValue('APP-001')
}

async function uploadApk(wrapper: Wrapper, name = 'demo.apk') {
  const apk = new File(['PK'], name, {
    type: 'application/vnd.android.package-archive',
  })
  const fileInput = wrapper.get('input[type="file"]')
  Object.defineProperty(fileInput.element, 'files', { value: [apk], configurable: true })
  await fileInput.trigger('change')
  await flushPromises()
  return apk
}

/** 从基础信息一路走到巡检确认（第三步）。 */
async function goToConfirmStep(wrapper: Wrapper, packageName = 'com.example.demo') {
  const packageInput = wrapper.get('[data-testid="app-package-name"]')
  if (!(packageInput.element as HTMLInputElement).value) {
    await packageInput.setValue(packageName)
  }
  await wrapper.get('[data-testid="wizard-next"]').trigger('click')
  await wrapper.get('[data-testid="wizard-next"]').trigger('click')
  await flushPromises()
}

/** 在指定页面范围选择器里取消若干页面。 */
async function uncheckPages(wrapper: Wrapper, pickerTestId: string, codes: string[]) {
  for (const code of codes) {
    await wrapper.get(`[data-testid="${pickerTestId}"] input[value="${code}"]`).setValue(false)
  }
}

function lastPayload(fn: typeof mocks.submitV2Task) {
  return fn.mock.calls.at(-1)![0]
}

describe('创建任务：平台与基础信息', () => {
  it('H5 正式任务提交精确短流程选择，探索模式不携带旧选择', async () => {
    mocks.getH5ShortflowCatalog.mockResolvedValue({
      definitions: [{ definition_id: 'player', version: 2, content_hash: 'a'.repeat(64), name: '受控播放器',
        workflow_skill: 'h5-player-workflow', checkpoints: [] }],
      rules: [{ rule_id: 'r1', rule_code: 'pause', name: '暂停反馈', ruleset_code: 'dept_ruleset' }],
    })
    const wrapper = await mountTaskCreateView()
    await fillBasics(wrapper, '受控短流程创建')
    await wrapper.get('[data-testid="shortflow-definition"]').setValue('player:2')
    await wrapper.get('[data-testid="shortflow-scenario"]').setValue('pause')
    await wrapper.get('[data-testid="shortflow-rule-r1"]').setValue(true)
    await wrapper.get('[data-testid="shortflow-add"]').trigger('click')
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()
    expect(lastPayload(mocks.submitV2Task).h5_shortflows).toEqual([{
      definition_id: 'player', version: 2, content_hash: 'a'.repeat(64), scenario_key: 'pause',
      rule_ids: ['r1'], workflow_skill: 'h5-player-workflow',
    }])
    await wrapper.get('input[value="exploration_only"]').setValue(true)
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()
    expect(lastPayload(mocks.submitExplorationTask).h5_shortflows).toBeUndefined()
  })
  it('提供 H5、Web、Android 平台胶囊；不再展示当前工作空间和优先级', async () => {
    const wrapper = await mountTaskCreateView()
    expect(wrapper.get('[data-testid="platform-h5"]').text()).toBe('H5')
    expect(wrapper.get('[data-testid="platform-web"]').text()).toBe('Web')
    expect(wrapper.get('[data-testid="platform-web"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="platform-app"]').text()).toBe('Android')
    expect(wrapper.get('[data-testid="platform-ios"]').text()).toContain('待接入')

    const text = wrapper.text()
    expect(text).not.toContain('当前工作空间')
    expect(text).not.toContain('由登录账号自动锁定')
    expect(text).not.toContain('优先级')
  })

  it('iOS 可以点开看待接入说明，但不能提交或存草稿', async () => {
    const wrapper = await mountTaskCreateView()
    const ios = wrapper.get('[data-testid="platform-ios"]')
    expect(ios.attributes('disabled')).toBeUndefined()

    await ios.trigger('click')
    await flushPromises()
    expect(ios.classes()).toContain('active')
    expect(wrapper.get('[data-testid="ios-config"]').text()).toContain('iOS 自动质检能力待接入')
    expect(wrapper.find('[data-testid="h5-config"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="android-config"]').exists()).toBe(false)
    expect(wrapper.get('button.submit-button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="save-draft"]').attributes('disabled')).toBeDefined()

    await fillBasics(wrapper, 'iOS 任务')
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()
    expect(mocks.submitV2Task).not.toHaveBeenCalled()
    expect(mocks.submitExplorationTask).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="platform-h5"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="ios-config"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="h5-config"]').exists()).toBe(true)
  })
})

describe('创建任务：H5 / Web', () => {
  it('H5 只保留页面地址、规则集和页面范围，默认都选全部', async () => {
    const wrapper = await mountTaskCreateView()
    const section = wrapper.get('[data-testid="h5-config"]')
    const text = section.text()
    expect(text).toContain('H5 页面地址')
    expect(text).toContain('规则集')
    expect(text).toContain('页面范围')
    expect(text).toContain('默认选择全部，可按需取消页面')
    expect(text).not.toContain('视口')
    expect(text).not.toContain('用户旅程步骤')
    expect(text).not.toContain('要求页面类型')
    expect(wrapper.find('[data-testid="create-steps"]').exists()).toBe(false)

    expect(section.get('[data-testid="ruleset-picker"] .multi-trigger-label').text()).toBe('全部')
    expect(section.get('[data-testid="page-scope-picker"] .multi-trigger-label').text()).toBe('全部')
    expect(section.get('[data-testid="ruleset-picker"]').text()).toContain('玉衡平台首期质检规则集 · 2 条规则')
    const pageLabels = section.findAll('[data-testid="page-scope-picker"] .multi-option').map((row) => row.text())
    expect(pageLabels).toEqual(['全部', '首页', '内容列表页', '内容详情页', '视频列表页', '视频详情页'])
  })

  it('H5 页面范围选全部时提交空列表，规则集用目录里选中的那个', async () => {
    const wrapper = await mountTaskCreateView()
    await fillBasics(wrapper, 'H5 默认范围')
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.submitExplorationTask).not.toHaveBeenCalled()
    expect(mocks.submitV2Task).toHaveBeenCalledWith({
      task_name: 'H5 默认范围',
      target: {
        platform: 'h5',
        url: 'https://example.test/h5',
        exploration_target: { required_page_types: [] },
      },
      ruleset_codes: ['dept_ruleset'],
      channel: 'workbench',
      priority: 100,
      product: {
        department_name: '-',
        product_code: 'APP-001',
        product_name: '示例产品',
      },
      scan_mode: 'full',
      remark: null,
    })
    expect(lastPayload(mocks.submitV2Task)).not.toHaveProperty('journey_steps')
    expect(mocks.message.success).toHaveBeenCalledWith('质检任务已提交')
    expect(mocks.routerPush).toHaveBeenCalledWith('/tasks/task-v2')
  })

  it('H5 仅页面探索且只选部分页面时，提交到独立探索入口并带上这几类页面', async () => {
    const wrapper = await mountTaskCreateView()
    await fillBasics(wrapper, 'H5 探索留证')
    await wrapper.get('input[value="exploration_only"]').setValue()
    await uncheckPages(wrapper, 'page-scope-picker', ['content_list', 'video_list', 'video_detail'])
    expect(wrapper.get('[data-testid="page-scope-picker"] .multi-trigger-label').text()).toBe('首页、内容详情页')

    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.submitV2Task).not.toHaveBeenCalled()
    expect(mocks.submitExplorationTask).toHaveBeenCalledOnce()
    expect(lastPayload(mocks.submitExplorationTask).target).toEqual({
      platform: 'h5',
      url: 'https://example.test/h5',
      exploration_target: { required_page_types: ['home', 'content_detail'] },
    })
    expect(mocks.message.success).toHaveBeenCalledWith('页面探索任务已提交')
    expect(mocks.routerPush).toHaveBeenCalledWith('/tasks/task-exploration')
  })

  it('多个规则集一起执行：默认全部时带上所有规则集，取消一个后只带剩下的', async () => {
    mocks.getRuleCatalog.mockResolvedValueOnce(
      ruleCatalog({
        rulesets: [
          catalogRuleset(),
          catalogRuleset({ ruleset_id: 'rs-2', ruleset_code: 'basic_compliance', version: 1, name: '基础合规规则集' }),
        ],
      }),
    )
    const wrapper = await mountTaskCreateView()
    await fillBasics(wrapper, '两个规则集')
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.submitV2Task).toHaveBeenCalledOnce()
    expect(lastPayload(mocks.submitV2Task)).toMatchObject({
      ruleset_codes: ['dept_ruleset', 'basic_compliance'],
    })
    expect(lastPayload(mocks.submitV2Task)).not.toHaveProperty('ruleset_code')

    await wrapper.get('[data-testid="ruleset-picker"] input[value="rs-1"]').setValue(false)
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.submitV2Task).toHaveBeenCalledTimes(2)
    expect(lastPayload(mocks.submitV2Task)).toMatchObject({ ruleset_codes: ['basic_compliance'] })
  })

  it('一个规则集都不选时提示请选择规则集', async () => {
    const wrapper = await mountTaskCreateView()
    await fillBasics(wrapper, '没选规则集')
    await wrapper.get('[data-testid="ruleset-picker"] input[value="__all__"]').setValue(false)
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.message.error).toHaveBeenCalledWith('请选择规则集')
    expect(mocks.submitV2Task).not.toHaveBeenCalled()
  })

  it('Web 去掉浏览器、分辨率和旅程；只开放探索时带固定桌面画像提交，不复用产品 H5 地址', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-web"]').trigger('click')
    await flushPromises()

    const section = wrapper.get('[data-testid="web-config"]')
    expect(section.text()).not.toContain('浏览器')
    expect(section.text()).not.toContain('分辨率')
    expect(section.text()).not.toContain('用户旅程步骤')
    expect(section.text()).not.toContain('1920')
    const pageLabels = section.findAll('[data-testid="page-scope-picker"] .multi-option').map((row) => row.text())
    expect(pageLabels).toEqual(['全部', '视频列表页', '视频详情页', '内容列表页', '内容详情页'])

    await fillBasics(wrapper, 'Web 桌面探索')
    const urlInput = wrapper.get('[data-testid="web-target-url"]')
    expect((urlInput.element as HTMLInputElement).value).toBe('')
    await urlInput.setValue('https://www.example.test/')
    expect(
      (wrapper.get('input[value="exploration_only"]').element as HTMLInputElement).checked,
    ).toBe(true)
    expect(wrapper.get('input[value="full_inspection"]').attributes('disabled')).toBeDefined()
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.submitV2Task).not.toHaveBeenCalled()
    expect(mocks.submitExplorationTask).toHaveBeenCalledWith({
      task_name: 'Web 桌面探索',
      target: {
        platform: 'web',
        url: 'https://www.example.test/',
        browser_profile_id: 'desktop_fhd_1920x1080_v1',
        exploration_target: { required_page_types: [] },
      },
      ruleset_codes: ['dept_ruleset'],
      channel: 'workbench',
      priority: 100,
      product: {
        department_name: '-',
        product_code: 'APP-001',
        product_name: '示例产品',
      },
      scan_mode: 'full',
      remark: null,
    })
  })

  it('Web 正式能力开启时默认提交完整质检，只选部分页面时放进探索目标', async () => {
    mocks.getV2Capabilities.mockResolvedValueOnce(webCapabilities(['full_inspection', 'exploration_only']))
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-web"]').trigger('click')
    await flushPromises()

    await fillBasics(wrapper, 'Web 完整质检')
    await wrapper.get('[data-testid="web-target-url"]').setValue('https://www.example.test/')
    await uncheckPages(wrapper, 'page-scope-picker', ['video_detail', 'content_list', 'content_detail'])
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(wrapper.get('input[value="full_inspection"]').attributes('disabled')).toBeUndefined()
    expect(mocks.submitExplorationTask).not.toHaveBeenCalled()
    expect(lastPayload(mocks.submitV2Task).target).toEqual({
      platform: 'web',
      url: 'https://www.example.test/',
      exploration_target: { required_page_types: ['video_list'] },
    })
  })

  it('没填页面地址时用大白话提示', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="task-name"]').setValue('缺地址')
    await findSelectWithOption(wrapper, 'APP-001')!.setValue('APP-001')
    await wrapper.get('[data-testid="h5-target-url"]').setValue('')
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(mocks.message.error).toHaveBeenCalledWith('请填写 H5 页面地址')
    expect(mocks.submitV2Task).not.toHaveBeenCalled()
  })
})

describe('创建任务：Android 三步向导', () => {
  it('三步：基础信息 / 设备配置 / 巡检确认，底部操作栏不显示', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()

    const steps = wrapper.get('[data-testid="create-steps"]').text()
    expect(steps).toContain('基础信息')
    expect(steps).toContain('设备配置')
    expect(steps).toContain('巡检确认')
    expect(steps).not.toContain('巡检范围')
    expect(steps).not.toContain('确认并创建')
    expect(wrapper.get('[data-testid="step-indicator"]').text()).toBe('第 1 步 / 3')
    expect(wrapper.find('[data-testid="form-footer"]').exists()).toBe(false)

    const text = wrapper.get('[data-testid="android-config"]').text()
    expect(text).toContain('Android 包名')
    expect(text).not.toContain('应用版本')
    expect(text).not.toContain('设备类型')
    expect(text).not.toContain('设备参数')
    expect(text).not.toContain('用户旅程步骤')
    expect(text).not.toContain('最低业务页数')
  })

  it('第一步要上传 APK 并填包名，第二步默认模板与默认参数，第三步列出规则清单', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="wizard-next"]').trigger('click')
    expect(mocks.message.error).toHaveBeenCalledWith('请先上传并通过校验的 APK')
    expect(stepShown(wrapper, 'step-basic')).toBe(true)

    await uploadApk(wrapper)
    await wrapper.get('[data-testid="wizard-next"]').trigger('click')
    expect(mocks.message.error).toHaveBeenLastCalledWith('请填写 Android 包名')
    expect(stepShown(wrapper, 'step-basic')).toBe(true)

    await wrapper.get('[data-testid="app-package-name"]').setValue('com.example.demo')
    await wrapper.get('[data-testid="wizard-next"]').trigger('click')
    await flushPromises()
    expect(stepShown(wrapper, 'step-device')).toBe(true)
    expect(wrapper.get('[data-testid="step-indicator"]').text()).toBe('第 2 步 / 3')

    const summary = wrapper.get('[data-testid="app-device-summary"]')
    expect(summary.text()).toContain('已选择 1 个设备配置')
    expect(summary.text()).toContain('云手机（MoreLogin）')
    expect(summary.text()).toContain('qa-b')
    expect(wrapper.get('.device-picker').attributes('style') ?? '').toContain('display: none')
    await summary.get('button').trigger('click')
    expect(wrapper.get('.device-picker').attributes('style') ?? '').not.toContain('display: none')

    const template = findSelectWithOption(wrapper, '使用默认模板')!
    expect((template.element as HTMLSelectElement).value).toBe('default')
    const preset = wrapper.get('[data-testid="app-qc-parameter-preset"]')
    expect(preset.attributes('disabled')).toBeDefined()
    expect(preset.text()).toBe('使用默认参数')

    await wrapper.get('[data-testid="wizard-next"]').trigger('click')
    await flushPromises()
    expect(stepShown(wrapper, 'step-confirm')).toBe(true)
    expect(wrapper.get('[data-testid="step-indicator"]').text()).toBe('第 3 步 / 3')
    expect(stepShown(wrapper, 'wizard-next')).toBe(false)
    expect(stepShown(wrapper, 'wizard-submit')).toBe(true)
    expect(wrapper.get('[data-testid="wizard-submit"]').text()).toBe('开始巡检')

    const confirm = wrapper.get('[data-testid="step-confirm"]')
    expect(confirm.get('[data-testid="app-ruleset-picker"] .multi-trigger-label').text()).toBe('全部')
    expect(confirm.get('[data-testid="app-page-scope-picker"] .multi-trigger-label').text()).toBe('全部')
    const checklist = confirm.get('[data-testid="rule-checklist"]')
    const boxes = checklist.findAll('input[type="checkbox"]')
    expect(boxes).toHaveLength(2)
    expect(boxes.every((box) => (box.element as HTMLInputElement).checked)).toBe(true)
    expect(boxes.every((box) => (box.element as HTMLInputElement).disabled)).toBe(true)
    expect(checklist.text()).toContain('页面模块空白兜底检查')
    expect(checklist.text()).toContain('视觉质检')
    expect(confirm.text()).toContain('逐条取消规则暂未开放')

    const tiles = confirm.get('[data-testid="confirm-tiles"]').text()
    expect(tiles).toContain('巡检页面自动识别')
    expect(tiles).toContain('质检规则2 条')
    expect(tiles).toContain('设备1 个')
    expect(tiles).toContain('参数方案默认参数')

    await wrapper.get('[data-testid="wizard-prev"]').trigger('click')
    expect(stepShown(wrapper, 'step-device')).toBe(true)
  })

  it('开始巡检只提交 build_ref、包名、设备和页面范围，不再提交旅程、里程碑和最低页数', async () => {
    mocks.uploadAppBuild.mockResolvedValueOnce({
      platform_build_ref: 'app-build:uploaded',
      package_name: 'com.example.demo',
      original_filename: 'demo.apk',
    })
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()

    const apk = await uploadApk(wrapper)
    expect(mocks.uploadAppBuild).toHaveBeenCalledWith(apk)
    expect((wrapper.get('[data-testid="app-package-name"]').element as HTMLInputElement).value).toBe(
      'com.example.demo',
    )
    await fillBasics(wrapper, 'App APK 测试')
    await goToConfirmStep(wrapper)
    await wrapper.get('[data-testid="wizard-submit"]').trigger('click')
    await flushPromises()

    expect(mocks.submitV2Task).toHaveBeenCalledOnce()
    expect(mocks.submitV2Task).toHaveBeenCalledWith({
      task_name: 'App APK 测试',
      target: {
        platform: 'app',
        source: {
          type: 'build_ref',
          platform_build_ref: 'app-build:uploaded',
        },
        package_name: 'com.example.demo',
        device: { backend: 'cloud_phone', profile_id: 'qa-b' },
        exploration_target: { required_page_types: [] },
      },
      ruleset_codes: ['dept_ruleset'],
      channel: 'workbench',
      priority: 100,
      product: {
        department_name: '-',
        product_code: 'APP-001',
        product_name: '示例产品',
      },
      scan_mode: 'full',
      remark: null,
    })
    const payload = lastPayload(mocks.submitV2Task)
    expect(payload).not.toHaveProperty('journey_steps')
    expect(payload.target.exploration_target).toEqual({ required_page_types: [] })
    expect(mocks.routerPush).toHaveBeenCalledWith('/tasks/task-v2')
  })

  it('换设备后端、选增强模板、只选部分页面时按选择提交', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()
    await uploadApk(wrapper, 'target.apk')
    await fillBasics(wrapper, 'App 目标锁定')

    await findSelectWithOption(wrapper, '雷电模拟器')!.setValue('ldplayer')
    await flushPromises()
    await findSelectWithOption(wrapper, 'qa-a')!.setValue('qa-a')
    await findSelectWithOption(wrapper, '增强模板')!.setValue('android_visual_evidence_v1')
    await goToConfirmStep(wrapper)
    await uncheckPages(wrapper, 'app-page-scope-picker', ['content_list', 'video_list', 'video_detail'])
    expect(wrapper.get('[data-testid="confirm-tiles"]').text()).toContain('巡检页面2 类')

    await wrapper.get('[data-testid="wizard-submit"]').trigger('click')
    await flushPromises()

    const payload = lastPayload(mocks.submitV2Task)
    expect(payload.target.device).toEqual({ backend: 'ldplayer', profile_id: 'qa-a' })
    expect(payload.target.exploration_target).toEqual({
      required_page_types: ['home', 'content_detail'],
      device_capability_template: 'android_visual_evidence_v1',
    })
  })

  it('开始巡检前发现前面步骤没填好时，跳回那一步提示', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()
    await uploadApk(wrapper)
    await fillBasics(wrapper, 'App 回跳')
    await goToConfirmStep(wrapper)
    expect(stepShown(wrapper, 'step-confirm')).toBe(true)

    await wrapper.get('[data-testid="app-package-name"]').setValue('')
    await wrapper.get('[data-testid="wizard-submit"]').trigger('click')
    await flushPromises()

    expect(mocks.message.error).toHaveBeenCalledWith('请填写 Android 包名')
    expect(stepShown(wrapper, 'step-basic')).toBe(true)
    expect(mocks.submitV2Task).not.toHaveBeenCalled()
  })

  it('仅页面探索提交到独立探索入口，质检规则显示不执行', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()
    await uploadApk(wrapper, 'explore.apk')
    await fillBasics(wrapper, 'App 探索留证')
    await wrapper.get('input[value="exploration_only"]').setValue()
    await goToConfirmStep(wrapper)
    expect(wrapper.get('[data-testid="confirm-tiles"]').text()).toContain('质检规则不执行')

    await wrapper.get('[data-testid="wizard-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('input[value="exploration_only"]').attributes('disabled')).toBeUndefined()
    expect(mocks.submitV2Task).not.toHaveBeenCalled()
    expect(lastPayload(mocks.submitExplorationTask).target).toEqual({
      platform: 'app',
      source: { type: 'build_ref', platform_build_ref: 'app-build:uploaded' },
      package_name: 'com.example.demo',
      device: { backend: 'cloud_phone', profile_id: 'qa-b' },
      exploration_target: { required_page_types: [] },
    })
    expect(mocks.routerPush).toHaveBeenCalledWith('/tasks/task-exploration')
  })

  it('切换到 Android 后展示设备占用与排队状态', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()

    expect(mocks.listWorkstationDevices).toHaveBeenCalled()
    expect(wrapper.text()).toContain('质检中')
    expect(wrapper.text()).toContain('排队 1')
    expect(wrapper.text()).toContain('空闲')
    expect(wrapper.find('[data-testid="app-device-status-list"]').exists()).toBe(true)
  })

  it('向导里仍可保存草稿', async () => {
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="platform-app"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="task-name"]').setValue('App 草稿')
    await wrapper.get('[data-testid="wizard-save-draft"]').trigger('click')
    await flushPromises()

    expect(mocks.createTaskDraft).toHaveBeenCalledOnce()
    const draft = mocks.createTaskDraft.mock.calls[0][0]
    expect(draft.name).toBe('App 草稿')
    expect(draft.target_platform).toBe('app')
    expect(draft.payload.form.ruleset_selection).toEqual(['__all__'])
    expect(draft.payload.form.page_scope.app).toEqual(['__all__'])
  })
})

describe('创建任务：草稿恢复', () => {
  it('旧草稿里的旅程、要求页面类型等字段不再生效，规则集与页面范围回到全部', async () => {
    mocks.listTaskDrafts.mockResolvedValue({
      items: [
        {
          id: 'draft-old',
          name: '旧 H5 草稿',
          target_platform: 'h5',
          payload: {
            form: {
              task_name: '旧 H5 草稿',
              target_url: 'https://old.example.test/h5',
              journey_steps_text: '登录\n详情',
              h5_required_page_types_text: 'home',
              minimum_business_pages: 5,
            },
            product_code: 'APP-001',
            current_step: 4,
          },
          created_at: '2026-09-01T10:00:00Z',
          updated_at: '2026-09-01T10:00:00Z',
        },
      ],
    })
    const wrapper = await mountTaskCreateView()
    await wrapper.get('[data-testid="draft-bar"] .qc-link').trigger('click')
    const restore = wrapper.findAll('[data-testid="draft-bar"] button').find((button) => button.text() === '恢复')!
    await restore.trigger('click')
    await flushPromises()

    expect(mocks.message.info).toHaveBeenCalledWith('已恢复草稿')
    await wrapper.get('button.submit-button').trigger('click')
    await flushPromises()

    expect(lastPayload(mocks.submitV2Task)).toMatchObject({
      task_name: '旧 H5 草稿',
      target: {
        platform: 'h5',
        url: 'https://old.example.test/h5',
        exploration_target: { required_page_types: [] },
      },
      ruleset_codes: ['dept_ruleset'],
    })
    expect(lastPayload(mocks.submitV2Task)).not.toHaveProperty('journey_steps')
  })
})
