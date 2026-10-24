import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RuleTestOption, RuleTestRunResult } from '../api/ruleTests'

const mocks = vi.hoisted(() => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  listRuleTestOptions: vi.fn(),
  runRuleTest: vi.fn(),
  captureRuleTestPage: vi.fn(),
  imageGroupMounts: 0,
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  // 分组下拉换成原生 select：组内选项摊平，保留禁用态
  const select = defineComponent({
    props: {
      value: { type: String, default: null },
      options: { type: Array, default: () => [] },
    },
    emits: ['update:value'],
    setup(props, { emit }) {
      return () => {
        const flat = (props.options as Array<Record<string, unknown>>).flatMap((option) =>
          option.type === 'group' ? (option.children as Array<Record<string, unknown>>) : [option],
        )
        return h(
          'select',
          {
            'data-test': 'rule-select',
            value: props.value ?? '',
            onChange: (event: Event) =>
              emit('update:value', (event.target as HTMLSelectElement).value),
          },
          [
            h('option', { value: '' }, '请选择'),
            ...flat.map((option) =>
              h(
                'option',
                { value: option.value as string, disabled: Boolean(option.disabled) },
                option.label as string,
              ),
            ),
          ],
        )
      }
    },
  })
  // 点击放大由组件库的图片预览负责，这里只看缩略图用的是它、图片组有没有随截图增删重建
  const image = defineComponent({
    props: { src: { type: String, default: '' }, alt: { type: String, default: '' } },
    setup(props) {
      return () => h('img', { src: props.src, alt: props.alt, 'data-test': 'preview-image' })
    },
  })
  const imageGroup = defineComponent({
    setup(_, { slots }) {
      mocks.imageGroupMounts += 1
      return () => slots.default?.()
    },
  })
  return {
    NImage: image,
    NImageGroup: imageGroup,
    NSelect: select,
    useMessage: () => mocks.message,
  }
})

vi.mock('../api/ruleTests', () => ({
  listRuleTestOptions: mocks.listRuleTestOptions,
  runRuleTest: mocks.runRuleTest,
  captureRuleTestPage: mocks.captureRuleTestPage,
}))

import RuleTestPanel from './RuleTestPanel.vue'

function option(overrides: Partial<RuleTestOption> = {}): RuleTestOption {
  return {
    rule_id: 'rule-12',
    rule_code: 'alioth_12',
    name: '提示文案叹号结尾检查',
    category: 'text',
    severity: 'medium',
    executor_type: 'hybrid',
    status: 'enabled',
    check_point: '提示文字不应使用叹号结尾',
    applicable_page_types: ['content_detail'],
    ruleset_id: 'ruleset-1',
    ruleset_code: 'alioth_first_ruleset',
    ruleset_name: '首期规则集',
    ruleset_version: 1,
    ruleset_status: 'enabled',
    testable: true,
    untestable_reason: null,
    config_problem: null,
    relaxable: true,
    evidence_needs: [
      { artifact_type: 'screenshot', label: '页面截图', form_supported: true },
      { artifact_type: 'ui_snapshot', label: '界面快照（页面元素结构）', form_supported: true },
      { artifact_type: 'har', label: '网络请求记录', form_supported: false },
    ],
    min_screenshot_count: 1,
    required_page_states: [],
    target_clients: [],
    notes: [],
    judge_input_fields: [],
    extra_inputs: [],
    ...overrides,
  }
}

const BEFORE_INPUT = {
  role: 'before',
  kind: 'screenshot' as const,
  label: '操作前的截图',
  hint: '这里放操作前那张，页面截图里放操作后那张（只放 1 张）',
}
const SECOND_LOCATION_INPUT = {
  role: 'location_1',
  kind: 'text' as const,
  label: '第二个位置的页面文字',
  hint: '页面文字填第一个位置的，这里贴第二个位置的',
}

function runResult(overrides: Partial<RuleTestRunResult> = {}): RuleTestRunResult {
  return {
    rule_id: 'rule-12',
    rule_code: 'alioth_12',
    rule_name: '提示文案叹号结尾检查',
    executor_type: 'hybrid',
    rule_status: 'enabled',
    check_mode: 'relaxed',
    outcome: 'fail',
    result_summary: '提示文案以叹号结尾',
    problem_desc: '「提交失败！」带有指责语气',
    confidence: 0.9,
    confidence_threshold: null,
    findings: [{ summary: '提示「提交失败！」以叹号结尾', evidence: ['截图 1（shot.png）'] }],
    missing_evidence: [],
    used_evidence: ['截图 1（shot.png）', '页面文字'],
    notices: ['已按你给的证据直接判定。正式任务里这条规则还要求：界面快照（页面元素结构）'],
    duration_ms: 8352,
    judge_input: {},
    judge_output: { conclusion: 'fail' },
    ...overrides,
  }
}

beforeEach(() => {
  mocks.listRuleTestOptions.mockResolvedValue({
    items: [
      option(),
      option({
        rule_id: 'rule-ops-7',
        rule_code: 'alioth_ops_7',
        name: '官方群入口',
        testable: false,
        untestable_reason: '这条规则是整任务汇总判定（要把多个页面放在一起看），暂不支持单页测试',
      }),
      option({
        rule_id: 'rule-app',
        rule_code: 'alioth_app_1',
        name: 'App 专属规则',
        status: 'draft',
        target_clients: ['APP'],
        applicable_page_types: ['home'],
        config_problem: 'Handler 未注册：no_such_handler',
        evidence_needs: [
          { artifact_type: 'screenshot', label: '页面截图', form_supported: true },
          { artifact_type: 'dom', label: '页面源码（HTML）', form_supported: true },
        ],
      }),
      option({
        rule_id: 'rule-price',
        rule_code: 'alioth_14',
        name: '优惠价低于原价检查',
        executor_type: 'rule_engine',
        relaxable: false,
        evidence_needs: [{ artifact_type: 'visible_text', label: '页面文字', form_supported: true }],
        min_screenshot_count: 0,
        judge_input_fields: ['discount_price', 'original_price'],
      }),
      option({
        rule_id: 'rule-change',
        rule_code: 'alioth_20',
        name: '弹窗广告关闭行为检查',
        executor_type: 'rule_engine',
        relaxable: false,
        applicable_page_types: ['home'],
        evidence_needs: [{ artifact_type: 'screenshot', label: '页面截图', form_supported: true }],
        extra_inputs: [BEFORE_INPUT],
      }),
      option({
        rule_id: 'rule-13',
        rule_code: 'alioth_13',
        name: '商品价格一致性检查',
        executor_type: 'rule_engine',
        relaxable: false,
        applicable_page_types: ['payment'],
        evidence_needs: [{ artifact_type: 'visible_text', label: '页面文字', form_supported: true }],
        min_screenshot_count: 0,
        extra_inputs: [SECOND_LOCATION_INPUT],
      }),
    ],
    page_types: [
      { code: 'home', name: '首页' },
      { code: 'content_detail', name: '内容详情页' },
    ],
  })
  mocks.runRuleTest.mockResolvedValue(runResult())
  mocks.captureRuleTestPage.mockResolvedValue({
    final_url: 'https://example.test/detail',
    title: '详情页',
    visible_text: '提交失败！',
    dom_html: '<html><body>提交失败！</body></html>',
    ui_snapshot: { elements: [{ tag: 'button', text: '提交失败！' }] },
    screenshot_png_base64: 'iVBORw0KGgo=',
    notices: [],
  })
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  vi.clearAllMocks()
})

async function mountPanel(active = true) {
  const wrapper = mount(RuleTestPanel, { props: { active } })
  await flushPromises()
  return wrapper
}

async function pickRule(wrapper: Awaited<ReturnType<typeof mountPanel>>, ruleId: string) {
  await wrapper.get('[data-test="rule-select"]').setValue(ruleId)
  await flushPromises()
}

describe('规则测试板块', () => {
  it('按规则要的证据放出输入框：要什么放什么，测试页给不了的单独提示', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.find('[data-testid="rule-test-drop"]').exists()).toBe(false)

    await pickRule(wrapper, 'rule-12')

    expect(wrapper.get('[data-testid="rule-test-rule-card"]').text()).toContain(
      '提示文字不应使用叹号结尾',
    )
    expect(wrapper.find('[data-testid="rule-test-page-url"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="rule-test-drop"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('这条规则至少要 1 张')
    expect(wrapper.find('[data-testid="rule-test-ui-snapshot"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="rule-test-visible-text"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-test-dom"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-test-judge-input"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="rule-test-unsupported"]').text()).toContain('网络请求记录')
    // 端、页面类型、页面状态、严格口径不再让人选
    expect(wrapper.find('[data-testid="rule-test-page-type"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-test-strict"]').exists()).toBe(false)
    const disabledOption = wrapper.find('option[value="rule-ops-7"]').element as HTMLOptionElement
    expect(disabledOption.disabled).toBe(true)
    expect(disabledOption.textContent).toContain('暂不支持单页测试')
  })

  it('填网址抓取后只把这条规则要的内容填进输入框，截图进列表', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')
    await wrapper.get('[data-testid="rule-test-page-url"]').setValue('https://example.test/a')
    await wrapper.get('[data-testid="rule-test-capture"]').trigger('click')
    await flushPromises()

    // 带上这条规则的页面类型：开屏广告规则会告诉采集别去跳过开屏层
    expect(mocks.captureRuleTestPage).toHaveBeenCalledWith({
      url: 'https://example.test/a',
      platform: 'h5',
      page_type: 'content_detail',
    })
    const snapshot = wrapper.get('[data-testid="rule-test-ui-snapshot"]')
      .element as HTMLTextAreaElement
    expect(JSON.parse(snapshot.value)).toEqual({
      elements: [{ tag: 'button', text: '提交失败！' }],
    })
    expect(wrapper.findAll('.rt-shots li')).toHaveLength(1)

    // 再抓一次替换上次抓来的截图，不重复堆
    await wrapper.get('[data-testid="rule-test-capture"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.rt-shots li')).toHaveLength(1)

    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()
    expect(mocks.runRuleTest).toHaveBeenCalledWith(
      expect.objectContaining({
        ruleId: 'rule-12',
        platform: 'h5',
        checkMode: 'relaxed',
        pageType: 'content_detail',
        pageState: 'stable',
        pageUrl: 'https://example.test/detail',
        pageTitle: '详情页',
        visibleText: '',
        domHtml: '',
        // 这条规则用不到另一组证据，不放框也不带
        beforeScreenshot: null,
        secondLocationText: '',
      }),
    )
    expect(wrapper.find('[data-testid="rule-test-before-drop"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-test-second-location"]').exists()).toBe(false)
    expect(mocks.runRuleTest.mock.calls[0][0].screenshots).toHaveLength(1)
    expect(wrapper.get('[data-testid="rule-test-outcome"]').text()).toBe('未通过')
  })

  it('比操作前后截图的规则放出「操作前的截图」：页面截图改叫操作后、只收 1 张，提交带上两张', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-change')

    expect(wrapper.text()).toContain('操作后的截图')
    expect(wrapper.get('[data-testid="rule-test-before"]').text()).toContain('只放 1 张')
    const after = ['a.png', 'b.png'].map((name) => new File(['png'], name, { type: 'image/png' }))
    const input = wrapper.get('[data-testid="rule-test-file-input"]')
    Object.defineProperty(input.element, 'files', { value: after, configurable: true })
    await input.trigger('change')
    expect(wrapper.findAll('.rt-shots li')).toHaveLength(1)
    expect(mocks.message.warning).toHaveBeenCalledWith('操作后的截图只放 1 张')

    const before = new File(['png-before'], 'before.png', { type: 'image/png' })
    const beforeInput = wrapper.get('[data-testid="rule-test-before-input"]')
    Object.defineProperty(beforeInput.element, 'files', { value: [before], configurable: true })
    await beforeInput.trigger('change')
    expect(wrapper.findAll('[data-testid="rule-test-before"] [data-test="preview-image"]')).toHaveLength(1)

    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()
    const call = mocks.runRuleTest.mock.calls[0][0]
    expect(call.beforeScreenshot).toBe(before)
    expect(call.screenshots).toEqual([after[0]])
  })

  it('比两个位置价格的规则放出「第二个位置的页面文字」，并随提交带上', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-13')

    expect(wrapper.text()).toContain('第一个位置')
    expect(wrapper.get('[data-testid="rule-test-second-location-field"]').text()).toContain(
      '这里贴第二个位置的',
    )
    await wrapper.get('[data-testid="rule-test-visible-text"]').setValue('月卡 ¥30')
    await wrapper.get('[data-testid="rule-test-second-location"]').setValue('开通月卡 ¥30')
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()

    expect(mocks.runRuleTest).toHaveBeenCalledWith(
      expect.objectContaining({
        ruleId: 'rule-13',
        visibleText: '月卡 ¥30',
        secondLocationText: '开通月卡 ¥30',
        beforeScreenshot: null,
      }),
    )
  })

  it('网址不是 http(s) 时直接提示，不去抓', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')
    await wrapper.get('[data-testid="rule-test-page-url"]').setValue('example.test')
    await wrapper.get('[data-testid="rule-test-capture"]').trigger('click')
    await flushPromises()

    expect(mocks.captureRuleTestPage).not.toHaveBeenCalled()
    expect(mocks.message.error).toHaveBeenCalledWith('请填以 http:// 或 https:// 开头的网址')
  })

  it('只在 App 跑的规则自动按 Android 测，不放网址和源码框，并提示判定配置有问题', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-app')

    expect(wrapper.find('[data-testid="rule-test-page-url"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-test-dom"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="rule-test-drop"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Handler 未注册：no_such_handler')
    expect(wrapper.find('option[value="rule-app"]').text()).toContain('草稿')

    const image = new File(['png-bytes'], 'shot.png', { type: 'image/png' })
    const input = wrapper.get('[data-testid="rule-test-file-input"]')
    Object.defineProperty(input.element, 'files', { value: [image], configurable: true })
    await input.trigger('change')
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()
    expect(mocks.runRuleTest).toHaveBeenCalledWith(
      expect.objectContaining({ platform: 'android', pageType: 'home' }),
    )
  })

  it('要结构化判定输入的规则放出补充输入框，并随提交带上', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-price')

    expect(wrapper.find('[data-testid="rule-test-drop"]').exists()).toBe(false)
    const judgeInput = wrapper.get('[data-testid="rule-test-judge-input"]')
    expect((judgeInput.element as HTMLTextAreaElement).placeholder).toContain('discount_price')
    await wrapper.get('[data-testid="rule-test-visible-text"]').setValue('现价 9.9 原价 19.9')
    await judgeInput.setValue('{"discount_price": 9.9, "original_price": 19.9}')
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()

    expect(mocks.runRuleTest).toHaveBeenCalledWith(
      expect.objectContaining({
        visibleText: '现价 9.9 原价 19.9',
        judgeInput: '{"discount_price": 9.9, "original_price": 19.9}',
        screenshots: [],
      }),
    )
  })

  it('没有证据时不能提交', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')

    expect(wrapper.get('[data-testid="rule-test-run"]').attributes('disabled')).toBeDefined()
  })

  it('界面快照不是 JSON 对象时直接提示，不调接口', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')
    await wrapper.get('[data-testid="rule-test-ui-snapshot"]').setValue('[1, 2]')
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()

    expect(mocks.runRuleTest).not.toHaveBeenCalled()
    expect(mocks.message.error).toHaveBeenCalledWith('界面快照必须是 JSON 对象（用大括号包起来）')
  })

  it('选中的图片进截图列表并随提交带上，非图片被跳过', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')
    const image = new File(['png-bytes'], 'shot.png', { type: 'image/png' })
    const text = new File(['hello'], 'notes.txt', { type: 'text/plain' })
    const input = wrapper.get('[data-testid="rule-test-file-input"]')
    Object.defineProperty(input.element, 'files', { value: [image, text], configurable: true })
    await input.trigger('change')

    expect(wrapper.findAll('.rt-shots li')).toHaveLength(1)
    expect(mocks.message.warning).toHaveBeenCalledWith('「notes.txt」不是图片，已跳过')
    // 缩略图用组件库的图片预览，点一下就能放大
    expect(wrapper.get('.rt-shots [data-test="preview-image"]').attributes('src')).toBe(
      'blob:preview',
    )

    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()
    expect(mocks.runRuleTest.mock.calls[0][0].screenshots).toEqual([image])
  })

  it('截图增删后放大用的图片组跟着重建，翻页不会翻到已移除的截图', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')
    const input = wrapper.get('[data-testid="rule-test-file-input"]')
    const files = ['a.png', 'b.png'].map((name) => new File(['png'], name, { type: 'image/png' }))
    Object.defineProperty(input.element, 'files', { value: files, configurable: true })
    await input.trigger('change')
    const mountsWithTwo = mocks.imageGroupMounts

    await wrapper.findAll('.rt-shots .qc-link')[0].trigger('click')

    expect(wrapper.findAll('.rt-shots li')).toHaveLength(1)
    expect(mocks.imageGroupMounts).toBe(mountsWithTwo + 1)
  })

  it('接口报错时提示失败原因', async () => {
    mocks.runRuleTest.mockRejectedValueOnce(new Error('规则 rule-12 不存在'))
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-12')
    await wrapper.get('[data-testid="rule-test-ui-snapshot"]').setValue('{"elements": []}')
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()

    expect(mocks.message.error).toHaveBeenCalledWith('检查失败：规则 rule-12 不存在')
    expect(wrapper.find('[data-testid="rule-test-result"]').exists()).toBe(false)
  })

  it('板块没打开时不拉规则，打开后才拉', async () => {
    const wrapper = await mountPanel(false)
    expect(mocks.listRuleTestOptions).not.toHaveBeenCalled()

    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.listRuleTestOptions).toHaveBeenCalledTimes(1)
  })

  it('从测试记录带证据回来重测：选好规则、证据全部换成记录里的', async () => {
    const wrapper = await mountPanel()
    await pickRule(wrapper, 'rule-price')
    const recorded = new File(['png'], '网址截图.png', { type: 'image/png' })

    await (wrapper.vm as unknown as { prefill: (data: unknown) => Promise<void> }).prefill({
      ruleId: 'rule-gone',
      ruleCode: 'alioth_12',
      pageUrl: 'https://example.test/detail',
      pageTitle: '详情页',
      visibleText: '',
      domHtml: '',
      uiSnapshot: '{\n  "elements": []\n}',
      judgeInput: '',
      screenshots: [recorded],
    })
    await flushPromises()

    // 规则 ID 变了（比如重建过），按编码找回
    expect((wrapper.get('[data-test="rule-select"]').element as HTMLSelectElement).value).toBe(
      'rule-12',
    )
    const snapshot = wrapper.get('[data-testid="rule-test-ui-snapshot"]')
      .element as HTMLTextAreaElement
    expect(snapshot.value).toBe('{\n  "elements": []\n}')
    expect(wrapper.findAll('.rt-shots li')).toHaveLength(1)
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()
    expect(mocks.runRuleTest).toHaveBeenCalledWith(
      expect.objectContaining({ ruleId: 'rule-12', pageUrl: 'https://example.test/detail' }),
    )
    expect(mocks.runRuleTest.mock.calls[0][0].screenshots).toEqual([recorded])
  })

  it('带回来的记录里有操作前的截图和第二个位置的文字时也一起填回', async () => {
    const wrapper = await mountPanel()
    const before = new File(['png'], 'before.png', { type: 'image/png' })
    const after = new File(['png'], 'after.png', { type: 'image/png' })

    await (wrapper.vm as unknown as { prefill: (data: unknown) => Promise<void> }).prefill({
      ruleId: 'rule-change',
      ruleCode: 'alioth_20',
      pageUrl: '',
      pageTitle: '',
      visibleText: '',
      domHtml: '',
      uiSnapshot: '',
      judgeInput: '',
      screenshots: [after],
      beforeScreenshot: before,
      secondLocationText: '',
    })
    await flushPromises()
    await wrapper.get('[data-testid="rule-test-run"]').trigger('click')
    await flushPromises()

    const call = mocks.runRuleTest.mock.calls[0][0]
    expect(call.beforeScreenshot).toBe(before)
    expect(call.screenshots).toEqual([after])

    await (wrapper.vm as unknown as { prefill: (data: unknown) => Promise<void> }).prefill({
      ruleId: 'rule-13',
      ruleCode: 'alioth_13',
      pageUrl: '',
      pageTitle: '',
      visibleText: '月卡 ¥30',
      domHtml: '',
      uiSnapshot: '',
      judgeInput: '',
      screenshots: [],
      beforeScreenshot: null,
      secondLocationText: '月卡 ¥25',
    })
    await flushPromises()
    const second = wrapper.get('[data-testid="rule-test-second-location"]')
      .element as HTMLTextAreaElement
    expect(second.value).toBe('月卡 ¥25')
  })

  it('记录里的规则已经不在了：证据照样填好，提示重新选规则', async () => {
    const wrapper = await mountPanel()

    await (wrapper.vm as unknown as { prefill: (data: unknown) => Promise<void> }).prefill({
      ruleId: null,
      ruleCode: 'alioth_deleted',
      pageUrl: '',
      pageTitle: '',
      visibleText: '提交失败！',
      domHtml: '',
      uiSnapshot: '',
      judgeInput: '',
      screenshots: [],
    })
    await flushPromises()

    expect(mocks.message.warning).toHaveBeenCalledWith(
      '规则 alioth_deleted 已经不在了，证据已填好，请重新选一条规则',
    )
    await pickRule(wrapper, 'rule-price')
    const text = wrapper.get('[data-testid="rule-test-visible-text"]').element as HTMLTextAreaElement
    expect(text.value).toBe('提交失败！')
  })
})
