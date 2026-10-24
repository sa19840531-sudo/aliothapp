import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RuleTestRecordDetail, RuleTestRecordSummary } from '../api/ruleTests'

const mocks = vi.hoisted(() => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  listRuleTestRecords: vi.fn(),
  getRuleTestRecord: vi.fn(),
  fetchRuleTestRecordScreenshot: vi.fn(),
  deleteRuleTestRecords: vi.fn(),
  showPreview: vi.fn(),
}))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  // 抽屉换成打开时原样渲染内容的壳
  const drawer = defineComponent({
    props: { show: { type: Boolean, default: false } },
    setup(props, { slots }) {
      return () => (props.show ? h('div', { 'data-test': 'drawer' }, slots.default?.()) : null)
    },
  })
  const drawerContent = defineComponent({
    setup(_, { slots }) {
      return () => h('div', slots.default?.())
    },
  })
  const image = defineComponent({
    props: { src: { type: String, default: '' }, alt: { type: String, default: '' } },
    setup(props, { expose }) {
      expose({ showPreview: () => mocks.showPreview(props.alt) })
      return () => h('img', { src: props.src, alt: props.alt, 'data-test': 'preview-image' })
    },
  })
  const imageGroup = defineComponent({
    setup(_, { slots }) {
      return () => slots.default?.()
    },
  })
  return {
    NDrawer: drawer,
    NDrawerContent: drawerContent,
    NImage: image,
    NImageGroup: imageGroup,
    useMessage: () => mocks.message,
  }
})

vi.mock('../api/ruleTests', () => ({
  listRuleTestRecords: mocks.listRuleTestRecords,
  getRuleTestRecord: mocks.getRuleTestRecord,
  fetchRuleTestRecordScreenshot: mocks.fetchRuleTestRecordScreenshot,
  deleteRuleTestRecords: mocks.deleteRuleTestRecords,
}))

import RuleTestRecords from './RuleTestRecords.vue'

function summary(overrides: Partial<RuleTestRecordSummary> = {}): RuleTestRecordSummary {
  return {
    id: 'record-1',
    rule_id: 'rule-12',
    rule_code: 'alioth_12',
    rule_name: '提示文案叹号结尾检查',
    tester_name: '规则配置员',
    platform: 'h5',
    page_url: 'https://example.test/detail',
    page_title: '详情页',
    outcome: 'fail',
    result_summary: '提示文案以叹号结尾',
    screenshot_count: 1,
    has_visible_text: true,
    has_dom: true,
    has_ui_snapshot: false,
    has_judge_input: false,
    created_at: '2026-09-11T08:00:00Z',
    ...overrides,
  }
}

function detail(): RuleTestRecordDetail {
  return {
    ...summary(),
    check_mode: 'relaxed',
    page_type: 'content_detail',
    page_state: 'stable',
    visible_text: '提交失败！',
    dom_html: '<html><body>提交失败！</body></html>',
    ui_snapshot: { elements: [] },
    judge_input: {},
    second_location_text: '',
    screenshots: [
      { index: 1, filename: '网址截图.png', width: 390, height: 844, capture_role: 'primary' },
    ],
    result: {
      rule_id: 'rule-12',
      rule_code: 'alioth_12',
      rule_name: '提示文案叹号结尾检查',
      executor_type: 'hybrid',
      rule_status: 'enabled',
      check_mode: 'relaxed',
      outcome: 'fail',
      result_summary: '提示文案以叹号结尾',
      problem_desc: '「提交失败！」以叹号结尾',
      confidence: 0.9,
      confidence_threshold: null,
      findings: [],
      missing_evidence: [],
      used_evidence: ['页面文字'],
      notices: [],
      duration_ms: 8352,
      judge_input: {},
      judge_output: { conclusion: 'fail' },
    },
  }
}

beforeEach(() => {
  mocks.listRuleTestRecords.mockResolvedValue({
    items: [summary(), summary({ id: 'record-2', rule_code: 'alioth_14', outcome: 'pass' })],
    total: 2,
    page: 1,
    page_size: 20,
  })
  mocks.getRuleTestRecord.mockResolvedValue(detail())
  mocks.fetchRuleTestRecordScreenshot.mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
  mocks.deleteRuleTestRecords.mockResolvedValue({ deleted: 1 })
  URL.createObjectURL = vi.fn(() => 'blob:shot')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  vi.clearAllMocks()
})

async function mountRecords() {
  const wrapper = mount(RuleTestRecords, { props: { active: true } })
  await flushPromises()
  return wrapper
}

describe('规则测试记录', () => {
  it('列出记录：规则、结论、证据种类一眼能看到', async () => {
    const wrapper = await mountRecords()

    expect(mocks.listRuleTestRecords).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1, pageSize: 20 }),
    )
    const row = wrapper.get('[data-testid="rule-test-record-record-1"]')
    expect(row.text()).toContain('alioth_12')
    expect(row.text()).toContain('未通过')
    expect(row.text()).toContain('截图 1 张 · 文字 · 源码')
    expect(row.text()).toContain('规则配置员')
  })

  it('按结论筛选、按日期筛选时换成整天的时间段', async () => {
    const wrapper = await mountRecords()
    await wrapper.get('[data-testid="rule-test-records-outcome"]').setValue('fail')
    await flushPromises()
    expect(mocks.listRuleTestRecords).toHaveBeenLastCalledWith(
      expect.objectContaining({ outcome: 'fail', page: 1 }),
    )

    const [from, to] = wrapper.findAll('.rr-filters input[type="date"]')
    await from.setValue('2026-09-01')
    await to.setValue('2026-09-10')
    await wrapper.get('[data-testid="rule-test-records-search"]').trigger('click')
    await flushPromises()
    const query = mocks.listRuleTestRecords.mock.lastCall?.[0]
    expect(query.createdFrom).toBe(new Date('2026-09-01T00:00:00').toISOString())
    // 结束日当天也算：到次日 0 点为止
    expect(query.createdTo).toBe(new Date('2026-09-11T00:00:00').toISOString())
  })

  it('勾选后删除：先确认，只删勾选的', async () => {
    const wrapper = await mountRecords()
    const deleteButton = wrapper.get('[data-testid="rule-test-records-delete-selected"]')
    expect(deleteButton.attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="rule-test-record-record-2"] input[type="checkbox"]').setValue(true)
    expect(deleteButton.text()).toContain('删除选中（1）')
    await deleteButton.trigger('click')
    expect(wrapper.get('[data-testid="rule-test-records-confirm-selected"]').text()).toContain(
      '删了找不回来',
    )
    expect(mocks.deleteRuleTestRecords).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="rule-test-records-confirm-selected-ok"]').trigger('click')
    await flushPromises()
    expect(mocks.deleteRuleTestRecords).toHaveBeenCalledWith({ recordIds: ['record-2'] })
    expect(mocks.message.success).toHaveBeenCalledWith('已删除 1 条测试记录')
    expect(mocks.listRuleTestRecords).toHaveBeenCalledTimes(2)
  })

  it('全选只选本页', async () => {
    const wrapper = await mountRecords()
    await wrapper.get('[data-testid="rule-test-records-select-all"]').setValue(true)

    expect(wrapper.get('[data-testid="rule-test-records-delete-selected"]').text()).toContain(
      '删除选中（2）',
    )
  })

  it('按时间删除：先数出这段时间有几条，确认后按时间段删', async () => {
    const wrapper = await mountRecords()
    mocks.listRuleTestRecords.mockResolvedValueOnce({ items: [], total: 37, page: 1, page_size: 1 })
    mocks.deleteRuleTestRecords.mockResolvedValueOnce({ deleted: 37 })

    await wrapper.get('[data-testid="rule-test-records-delete-by-time"]').trigger('click')
    expect(wrapper.get('[data-testid="rule-test-records-confirm-time-ok"]').attributes('disabled'))
      .toBeDefined()
    await wrapper.get('[data-testid="rule-test-records-time-to"]').setValue('2026-08-31')
    await flushPromises()

    expect(mocks.listRuleTestRecords).toHaveBeenLastCalledWith({
      createdFrom: undefined,
      createdTo: new Date('2026-09-01T00:00:00').toISOString(),
      page: 1,
      pageSize: 1,
    })
    expect(wrapper.get('[data-testid="rule-test-records-time-count"]').text()).toContain('37 条')
    await wrapper.get('[data-testid="rule-test-records-confirm-time-ok"]').trigger('click')
    await flushPromises()
    expect(mocks.deleteRuleTestRecords).toHaveBeenCalledWith({
      createdFrom: undefined,
      createdTo: new Date('2026-09-01T00:00:00').toISOString(),
    })
    expect(mocks.message.success).toHaveBeenCalledWith('已删除 37 条测试记录')
    expect(wrapper.find('[data-testid="rule-test-records-confirm-time"]').exists()).toBe(false)
  })

  it('开始日期晚于结束日期时不统计、不能删', async () => {
    const wrapper = await mountRecords()
    await wrapper.get('[data-testid="rule-test-records-delete-by-time"]').trigger('click')
    await wrapper.get('[data-testid="rule-test-records-time-from"]').setValue('2026-09-10')
    await flushPromises()
    const countsBefore = mocks.listRuleTestRecords.mock.calls.length
    await wrapper.get('[data-testid="rule-test-records-time-to"]').setValue('2026-09-01')
    await flushPromises()

    expect(wrapper.get('[data-testid="rule-test-records-confirm-time"]').text()).toContain(
      '开始日期不能晚于结束日期',
    )
    expect(mocks.listRuleTestRecords).toHaveBeenCalledTimes(countsBefore)
    expect(wrapper.find('[data-testid="rule-test-records-time-count"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="rule-test-records-confirm-time-ok"]').attributes('disabled'))
      .toBeDefined()
  })

  it('查看记录：当时的结论和证据都在，能拿这份证据回去重测', async () => {
    const wrapper = await mountRecords()
    await wrapper.get('[data-testid="rule-test-record-record-1"] .qc-link').trigger('click')
    await flushPromises()

    expect(mocks.getRuleTestRecord).toHaveBeenCalledWith('record-1')
    expect(mocks.fetchRuleTestRecordScreenshot).toHaveBeenCalledWith('record-1', 1)
    const drawer = wrapper.get('[data-testid="rule-test-record-detail"]')
    expect(drawer.get('[data-testid="rule-test-outcome"]').text()).toBe('未通过')
    expect(drawer.text()).toContain('页面源码（HTML）')
    // 截图用组件库的图片预览，点一下就能放大
    expect(drawer.get('.rr-shots [data-test="preview-image"]').attributes('src')).toBe('blob:shot')

    await drawer.get('[data-testid="rule-test-record-retest"]').trigger('click')
    const [prefill] = wrapper.emitted('retest')?.[0] ?? []
    expect(prefill).toEqual(
      expect.objectContaining({
        ruleId: 'rule-12',
        ruleCode: 'alioth_12',
        visibleText: '提交失败！',
        uiSnapshot: JSON.stringify({ elements: [] }, null, 2),
        judgeInput: '',
      }),
    )
    const files = (prefill as { screenshots: File[] }).screenshots
    expect(files.map((file) => file.name)).toEqual(['网址截图.png'])
    expect(prefill).toEqual(expect.objectContaining({ beforeScreenshot: null, secondLocationText: '' }))
    expect(wrapper.find('[data-testid="rule-test-record-detail"]').exists()).toBe(false)
  })

  it('结论里的截图引用点得开：直接放大当时那张图，对不上图的不给点', async () => {
    mocks.getRuleTestRecord.mockResolvedValueOnce({
      ...detail(),
      result: {
        ...detail().result,
        findings: [{ summary: '底部按钮被挡住', evidence: ['截图 1（网址截图.png）', '页面文字'] }],
      },
    })
    const wrapper = await mountRecords()
    await wrapper.get('[data-testid="rule-test-record-record-1"] .qc-link').trigger('click')
    await flushPromises()

    const chips = wrapper.findAll('[data-testid="rule-test-result"] .rt-chips button')
    const shotChip = chips.find((item) => item.text().includes('网址截图.png'))
    const textChip = chips.find((item) => item.text() === '页面文字')
    // 对得上当时截图的才可点，页面文字这类没有图的保持纯标签
    expect(shotChip?.classes()).toContain('rt-chip-openable')
    expect(textChip?.classes()).not.toContain('rt-chip-openable')
    expect(textChip?.attributes('disabled')).toBeDefined()

    await shotChip?.trigger('click')
    expect(mocks.showPreview).toHaveBeenCalledWith('网址截图.png')
  })

  it('记录里有操作前的截图和第二个位置的文字：分开标出，重测时各回各的框', async () => {
    mocks.getRuleTestRecord.mockResolvedValueOnce({
      ...detail(),
      second_location_text: '开通月卡 ¥30',
      screenshots: [
        { index: 1, filename: 'after.png', width: 390, height: 844, capture_role: 'primary' },
        { index: 2, filename: 'before.png', width: 390, height: 844, capture_role: 'before' },
      ],
    })
    const wrapper = await mountRecords()
    await wrapper.get('[data-testid="rule-test-record-record-1"] .qc-link').trigger('click')
    await flushPromises()

    const drawer = wrapper.get('[data-testid="rule-test-record-detail"]')
    expect(drawer.findAll('.rr-shots li').map((item) => item.text())).toEqual([
      '截图 1',
      '操作前的截图',
    ])
    expect(drawer.text()).toContain('第二个位置的页面文字')

    await drawer.get('[data-testid="rule-test-record-retest"]').trigger('click')
    const [prefill] = wrapper.emitted('retest')?.[0] ?? []
    const data = prefill as { screenshots: File[]; beforeScreenshot: File | null }
    expect(data.screenshots.map((file) => file.name)).toEqual(['after.png'])
    expect(data.beforeScreenshot?.name).toBe('before.png')
    expect(prefill).toEqual(expect.objectContaining({ secondLocationText: '开通月卡 ¥30' }))
  })
})
