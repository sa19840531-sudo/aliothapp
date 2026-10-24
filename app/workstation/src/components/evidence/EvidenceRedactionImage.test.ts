import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import { nextTick } from 'vue'

async function pointer(element: Element, type: string, values: Record<string, number>) {
  const event = new Event(type, { bubbles: true, cancelable: true })
  for (const [key, value] of Object.entries(values)) Object.defineProperty(event, key, { value })
  element.dispatchEvent(event)
  await nextTick()
}
import EvidenceRedactionImage from './EvidenceRedactionImage.vue'

it('框选以原图坐标记录，缩放和反向拖动不改变选区身份', async () => {
  const wrapper = mount(EvidenceRedactionImage, { props: { src: 'blob:original', regions: [] } })
  const image = wrapper.get('img')
  Object.defineProperties(image.element, { naturalWidth: { value: 1000 }, naturalHeight: { value: 500 } })
  image.element.getBoundingClientRect = () => ({ left: 20, top: 30, width: 500, height: 250 } as DOMRect)
  await image.trigger('load')
  const editor = wrapper.get('[data-testid="redaction-image-editor"]')
  await pointer(editor.element, 'pointerdown', { button: 0, pointerId: 1, clientX: 220, clientY: 130 })
  await pointer(editor.element, 'pointerup', { pointerId: 1, clientX: 120, clientY: 80 })
  expect(wrapper.emitted('region')).toEqual([[{ x: 200, y: 100, width: 200, height: 100 }]])
  image.element.getBoundingClientRect = () => ({ left: 20, top: 30, width: 250, height: 125 } as DOMRect)
  await pointer(editor.element, 'pointerdown', { button: 0, pointerId: 2, clientX: 70, clientY: 55 })
  await pointer(editor.element, 'pointerup', { pointerId: 2, clientX: 120, clientY: 80 })
  expect(wrapper.emitted('region')?.[1]).toEqual([{ x: 200, y: 100, width: 200, height: 100 }])
  wrapper.unmount()
})

it('取消、零宽和未就绪框选不会添加区域', async () => {
  const wrapper = mount(EvidenceRedactionImage, { props: { src: 'blob:original', regions: [] } })
  const editor = wrapper.get('[data-testid="redaction-image-editor"]')
  await pointer(editor.element, 'pointerdown', { button: 0, pointerId: 1, clientX: 0, clientY: 0 })
  await pointer(editor.element, 'pointercancel', { pointerId: 1 })
  await pointer(editor.element, 'pointerup', { pointerId: 1, clientX: 100, clientY: 100 })
  expect(wrapper.emitted('region')).toBeUndefined()
  wrapper.unmount()
})
