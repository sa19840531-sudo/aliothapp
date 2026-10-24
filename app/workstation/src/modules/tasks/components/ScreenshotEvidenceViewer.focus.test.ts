import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import ScreenshotEvidenceViewer from './ScreenshotEvidenceViewer.vue'
import type { ScreenshotViewerEntry } from './screenshotCollection'

let wrapper: ReturnType<typeof mount> | undefined

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  document.body.innerHTML = ''
})

describe('截图合集的真实弹层焦点', () => {
  it('在 Naive UI 焦点约束中放大切换后，返回当前图片并在关闭时恢复入口焦点', async () => {
    const opener = document.createElement('button')
    document.body.appendChild(opener)
    opener.focus()
    const entries: ScreenshotViewerEntry[] = ['first', 'second', 'third'].map((id) => ({
      artifact_id: id, page_name: '首页', captured_at: null, capture_role: null, duplicate_count: 1, related_rules: [],
    }))
    wrapper = mount(ScreenshotEvidenceViewer, {
      attachTo: document.body,
      global: { stubs: { teleport: true, transition: false } },
      props: {
        entries, title: '首页', collection: true,
        imageUrl: (id: string) => `/artifacts/${id}`,
        formatTime: () => '--:--:--',
      },
    })
    await flushPromises()
    await wrapper.findAll('[data-testid="collection-frame"]')[1]!.trigger('click')
    await wrapper.get('[data-testid="viewer-next"]').trigger('click')
    await wrapper.get('[data-testid="viewer-back"]').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(wrapper.findAll('[data-testid="collection-frame"]')[2]!.element)
    wrapper.unmount()
    wrapper = undefined
    expect(document.activeElement).toBe(opener)
  })
})
