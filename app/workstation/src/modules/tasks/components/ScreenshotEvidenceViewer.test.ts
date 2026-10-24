import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import ScreenshotEvidenceViewer from './ScreenshotEvidenceViewer.vue'
import type { ScreenshotViewerAction, ScreenshotViewerEntry } from './screenshotCollection'

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    NModal: defineComponent({
      name: 'NModal',
      props: { show: Boolean, closeOnEsc: Boolean },
      emits: ['update:show'],
      setup: (_, { slots }) => () => h('div', slots.default?.()),
    }),
  }
})

const mounted: Array<ReturnType<typeof mount>> = []

afterEach(() => {
  for (const wrapper of mounted.splice(0)) wrapper.unmount()
  document.body.innerHTML = ''
})

function entry(artifactId: string, overrides: Partial<ScreenshotViewerEntry> = {}): ScreenshotViewerEntry {
  return {
    artifact_id: artifactId,
    page_name: '帖子列表页',
    captured_at: '2026-10-08T14:22:17Z',
    capture_role: null,
    duplicate_count: 1,
    related_rules: [],
    ...overrides,
  }
}

function viewer(entries: ScreenshotViewerEntry[], overrides: Record<string, unknown> = {}) {
  const wrapper = mount(ScreenshotEvidenceViewer, {
    attachTo: document.body,
    props: {
      entries,
      title: '帖子列表页',
      imageUrl: (id: string) => `/artifacts/${id}`,
      formatTime: (value: string | null) => value ? `采集于 ${value}` : '采集时间未知',
      ...overrides,
    },
  })
  mounted.push(wrapper)
  return wrapper
}

function currentImage(wrapper: ReturnType<typeof viewer>) {
  return wrapper.get('[data-testid="viewer-image"] img')
}

describe('截图合集查看', () => {
  it('同次操作成组展示明确前后状态和目的，重排后返回仍聚焦原图', async () => {
    const action: ScreenshotViewerAction = {
      id: 'close-ad', action_label: '点击“关闭广告”', purpose: '验证广告关闭后是否恢复页面',
      status_label: '已执行', source_capture_id: 'capture-before', target_capture_id: 'capture-after',
    }
    const wrapper = viewer([
      entry('after', { page_capture_id: 'capture-after', actions: [action] }),
      entry('independent', { capture_role: 'before_action' }),
      entry('before', { page_capture_id: 'capture-before', actions: [action], related_rules: [{
        rule_code: 'close-rule', rule_name: '广告关闭检查', display_status: 'passed', display_status_label: '通过',
      }] }),
    ], { collection: true })
    const group = wrapper.get('[data-testid="screenshot-action-group"]')
    expect(group.text()).toContain('点击“关闭广告”')
    expect(group.text()).toContain('验证广告关闭后是否恢复页面')
    expect(group.get('[data-testid="screenshot-action-before"] img').attributes('src')).toBe('/artifacts/before')
    expect(group.get('[data-testid="screenshot-action-after"] img').attributes('src')).toBe('/artifacts/after')
    expect(group.get('[data-testid="screenshot-action-before"]').text()).toContain('广告关闭检查 · 通过')
    expect(group.get('[data-testid="screenshot-action-after"]').text()).not.toContain('广告关闭检查')
    expect(wrapper.get('.unassociated-section').text()).toContain('未记录与具体操作的关联')
    await group.get('[data-artifact-id="before"]').trigger('click')
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/before')
    expect(wrapper.text()).toContain('3 / 3')
    expect(wrapper.text()).toContain('验证广告关闭后是否恢复页面')
    await wrapper.get('[data-testid="viewer-back"]').trigger('click')
    expect(document.activeElement).toBe(wrapper.get('[data-artifact-id="before"]').element)
  })

  it('缺少另一侧截图和操作目的时明确说明，不将规则引用当作目的', () => {
    const action: ScreenshotViewerAction = {
      id: 'scroll', action_label: '滑动页面', purpose: null, status_label: '已执行',
      source_capture_id: 'missing-before', target_capture_id: 'capture-after',
    }
    const wrapper = viewer([
      entry('after', { page_capture_id: 'capture-after', actions: [action], related_rules: [{
        rule_code: 'scroll-rule', rule_name: '返回顶部检查', display_status: 'evidence_insufficient', display_status_label: '证据不足',
      }] }),
      entry('unrelated'),
    ], { collection: true })
    const group = wrapper.get('[data-testid="screenshot-action-group"]')
    expect(group.text()).toContain('当前合集中未提供操作前截图')
    expect(group.get('.action-purpose').text()).toContain('未记录操作目的')
    expect(group.get('.action-purpose').text()).not.toContain('返回顶部检查')
    expect(group.findAll('[data-testid="collection-frame"]')).toHaveLength(1)
  })

  it('共享图片按选中的动作说明前后，返回同一操作位置且补齐信息不退出放大', async () => {
    const first: ScreenshotViewerAction = {
      id: 'first-action', action_label: '关闭弹层', purpose: null, status_label: '已执行',
      source_capture_id: 'capture-first', target_capture_id: 'capture-shared',
    }
    const second: ScreenshotViewerAction = {
      id: 'second-action', action_label: '打开详情', purpose: null, status_label: '已执行',
      source_capture_id: 'capture-shared', target_capture_id: 'capture-last',
    }
    const entries = [
      entry('first', { page_capture_id: 'capture-first', actions: [first] }),
      entry('shared', { page_capture_id: 'capture-shared', capture_role: 'after_action', actions: [first, second] }),
      entry('last', { page_capture_id: 'capture-last', actions: [second] }),
    ]
    const wrapper = viewer(entries, { collection: true })
    await wrapper.findAll('[data-testid="screenshot-action-group"]')[1]!
      .get('[data-testid="screenshot-action-before"] [data-testid="collection-frame"]').trigger('click')
    expect(wrapper.get('.viewer-heading').text()).toContain('操作前状态')
    expect(wrapper.get('.current-actions').text()).toContain('关闭弹层 · 操作后状态')
    expect(wrapper.get('.current-actions').text()).toContain('打开详情 · 操作前状态')
    await wrapper.get('[aria-label="放大截图"]').trigger('click')
    await wrapper.setProps({ entries: entries.map((item) => ({
      ...item, actions: item.actions?.map((action) => ({ ...action, purpose: '补齐的操作目的' })),
    })) })
    expect(wrapper.find('[data-testid="collection-grid"]').exists()).toBe(false)
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/shared')
    expect(wrapper.get('[data-testid="screenshot-zoom"]').text()).toBe('125%')
    expect(wrapper.get('.current-actions').text()).toContain('补齐的操作目的')
    await wrapper.get('[data-testid="viewer-back"]').trigger('click')
    expect(document.activeElement).toBe(wrapper.findAll('[data-testid="screenshot-action-group"]')[1]!
      .get('[data-testid="screenshot-action-before"] [data-testid="collection-frame"]').element)
  })

  it('同一窗口默认展示传入合集全部图片，初始编号不跳过合集总览', async () => {
    const wrapper = viewer([entry('before', { capture_role: 'before_action' }), entry('after', { capture_role: 'after_action' })], {
      collection: true,
      initialArtifactId: 'after',
    })
    await nextTick()
    expect(wrapper.get('[data-testid="collection-grid"]').findAll('img').map((image) => image.attributes('src')))
      .toEqual(['/artifacts/before', '/artifacts/after'])
    expect(wrapper.findAll('[data-testid="collection-frame"]')).toHaveLength(2)
    expect(wrapper.text()).toContain('图片合集')
    expect(wrapper.text()).toContain('共 2 张不同图片')
    expect(wrapper.text()).toContain('操作前')
    expect(wrapper.text()).toContain('操作后')
    expect(wrapper.find('[data-testid="viewer-image"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="viewer-back"]').exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('[data-testid="screenshot-viewer"]').element)
    await wrapper.get('[data-testid="screenshot-viewer"]').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.find('[data-testid="collection-grid"]').exists()).toBe(true)
  })

  it('放大选中图片，并在返回合集后把焦点交还给原图片', async () => {
    const wrapper = viewer([entry('first'), entry('second'), entry('third')], { collection: true })
    await wrapper.findAll('[data-testid="collection-frame"]')[1]!.trigger('click')
    expect(wrapper.find('[data-testid="collection-grid"]').exists()).toBe(false)
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/second')
    expect(wrapper.text()).toContain('2 / 3')
    await wrapper.get('[aria-label="放大截图"]').trigger('click')
    expect(wrapper.get('[data-testid="screenshot-zoom"]').text()).toBe('125%')
    await wrapper.get('[data-testid="viewer-back"]').trigger('click')
    expect(wrapper.find('[data-testid="collection-grid"]').exists()).toBe(true)
    expect(document.activeElement).toBe(wrapper.findAll('[data-testid="collection-frame"]')[1]!.element)
  })

  it('左右按钮和方向键仅在当前合集内切换，端点禁用且不环绕', async () => {
    const wrapper = viewer([entry('collection-first'), entry('collection-last')], { collection: true })
    await wrapper.findAll('[data-testid="collection-frame"]')[0]!.trigger('click')
    expect(wrapper.get('[data-testid="viewer-prev"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="viewer-next"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="screenshot-viewer"]').trigger('keydown', { key: 'ArrowLeft' })
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/collection-first')
    await wrapper.get('[data-testid="viewer-next"]').trigger('click')
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/collection-last')
    expect(wrapper.get('[data-testid="viewer-next"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="screenshot-viewer"]').trigger('keydown', { key: 'ArrowRight' })
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/collection-last')
    await wrapper.get('[data-testid="screenshot-viewer"]').trigger('keydown', { key: 'ArrowLeft' })
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/collection-first')
    await wrapper.get('[data-testid="viewer-next"]').trigger('click')
    await wrapper.get('[data-testid="viewer-prev"]').trigger('click')
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/collection-first')
    expect(wrapper.findAll('img').map((image) => image.attributes('src'))).toEqual(['/artifacts/collection-first'])
  })

  it('单张合集直接放大，保留缩放且不显示返回合集', async () => {
    const wrapper = viewer([entry('single')], { collection: true })
    expect(wrapper.find('[data-testid="collection-grid"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="viewer-back"]').exists()).toBe(false)
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/single')
    expect(wrapper.get('[data-testid="viewer-prev"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="viewer-next"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="放大截图"]').trigger('click')
    expect(wrapper.get('[data-testid="screenshot-zoom"]').text()).toBe('125%')
  })

  it('规则证据直接放大指定图片，切换图片重置缩放', async () => {
    const wrapper = viewer([entry('rule-before'), entry('rule-after')], { initialArtifactId: 'rule-after' })
    expect(wrapper.find('[data-testid="collection-grid"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="viewer-back"]').exists()).toBe(false)
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/rule-after')
    await wrapper.get('[aria-label="放大截图"]').trigger('click')
    await wrapper.get('[data-testid="viewer-prev"]').trigger('click')
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/rule-before')
    expect(wrapper.get('[data-testid="screenshot-zoom"]').text()).toBe('100%')
  })

  it('只根据真实角色显示中文，保留同图采集次数和关联规则', async () => {
    const wrapper = viewer([
      entry('unknown', { captured_at: null, capture_role: null, duplicate_count: 3, related_rules: [{
        rule_code: 'alioth_19', rule_name: '跳过按钮面积', display_status: 'passed', display_status_label: '通过',
      }] }),
      entry('other', { capture_role: 'unrecognized_role' }),
    ], { collection: true })
    const frames = wrapper.findAll('[data-testid="collection-frame"]')
    expect(frames[0]!.text()).toContain('采集截图')
    expect(frames[0]!.text()).toContain('采集时间未知')
    expect(frames[0]!.text()).toContain('同图采集 3 次')
    expect(frames[0]!.text()).toContain('跳过按钮面积 · 通过')
    expect(frames[1]!.text()).toContain('采集截图')
    expect(frames[1]!.text()).not.toContain('同图采集')
    expect(wrapper.text()).not.toContain('操作前')
    expect(wrapper.text()).not.toContain('操作后')
    await frames[0]!.trigger('click')
    expect(wrapper.text()).toContain('同图采集 3 次')
    expect(wrapper.get('.viewer-foot').text()).toContain('跳过按钮面积 · 通过')
  })

  it('下载当前放大截图，关闭按钮、Escape 和弹层关闭事件同步通知父页', async () => {
    const wrapper = viewer([entry('first'), entry('chosen')], { collection: true })
    await wrapper.findAll('[data-testid="collection-frame"]')[1]!.trigger('click')
    await wrapper.get('[data-testid="viewer-download"]').trigger('click')
    expect(wrapper.emitted('download')).toEqual([['chosen']])
    await wrapper.get('[data-testid="viewer-close"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    await wrapper.get('[data-testid="screenshot-viewer"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(2)
    const modal = wrapper.getComponent({ name: 'NModal' })
    expect(modal.props('closeOnEsc')).toBe(false)
    modal.vm.$emit('update:show', true)
    expect(wrapper.emitted('close')).toHaveLength(2)
    modal.vm.$emit('update:show', false)
    expect(wrapper.emitted('close')).toHaveLength(3)
  })

  it('切换传入合集后重置视图，不能继续浏览旧合集', async () => {
    const wrapper = viewer([entry('old-first'), entry('old-last')], { collection: true })
    await wrapper.findAll('[data-testid="collection-frame"]')[1]!.trigger('click')
    await wrapper.setProps({ entries: [entry('new-first'), entry('new-last')] })
    expect(wrapper.get('[data-testid="collection-grid"]').findAll('img').map((image) => image.attributes('src')))
      .toEqual(['/artifacts/new-first', '/artifacts/new-last'])
    await wrapper.findAll('[data-testid="collection-frame"]')[0]!.trigger('click')
    await wrapper.get('[data-testid="viewer-prev"]').trigger('click')
    expect(currentImage(wrapper).attributes('src')).toBe('/artifacts/new-first')
    expect(wrapper.html()).not.toContain('/artifacts/old-')
  })

  it('图片不可用时合集保留入口，仍能查看并下载对应原图', async () => {
    const wrapper = viewer([entry('failed'), entry('missing')], {
      collection: true,
      imageUrl: (id: string) => id === 'missing' ? '' : `/artifacts/${id}`,
    })
    await wrapper.findAll('[data-testid="collection-frame"]')[0]!.get('img').trigger('error')
    const frames = wrapper.findAll('[data-testid="collection-frame"]')
    expect(frames[0]!.text()).toContain('截图暂不可用')
    expect(frames[1]!.text()).toContain('正在加载截图')
    await frames[0]!.trigger('click')
    await currentImage(wrapper).trigger('error')
    expect(wrapper.text()).toContain('截图加载失败')
    await wrapper.get('[data-testid="viewer-download"]').trigger('click')
    expect(wrapper.emitted('download')).toEqual([['failed']])
  })

  it('空合集安全显示占位并可以关闭，不暴露无效下载', async () => {
    const wrapper = viewer([], { collection: true })
    expect(wrapper.text()).toContain('暂无截图')
    expect(wrapper.find('[data-testid="viewer-download"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="viewer-image"]').exists()).toBe(false)
    await wrapper.get('[data-testid="viewer-close"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
