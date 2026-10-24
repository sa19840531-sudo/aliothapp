import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import EvidenceImageViewport from './EvidenceImageViewport.vue'

it('短时地址还没签发时不请求空src，也不把等待显示成原图损坏', async () => {
  const wrapper = mount(EvidenceImageViewport, { props: { src: '', alt: '原图' } })
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.text()).toContain('加载中')
  await wrapper.setProps({ src: '/fresh' })
  expect(wrapper.get('img').attributes('src')).toBe('/fresh')
  await wrapper.get('img').trigger('error')
  expect(wrapper.text()).toContain('截图加载失败')
  await wrapper.setProps({ src: '/refreshed' })
  expect(wrapper.get('img').attributes('src')).toBe('/refreshed')
})

it('旧图片的晚到错误不能污染新地址，签发拒绝不能永久显示加载中', async () => {
  const wrapper = mount(EvidenceImageViewport, { props: { src: '/old', alt: '原图' } })
  const previous = wrapper.get('img').element
  await wrapper.setProps({ src: '/fresh' })
  previous.dispatchEvent(new Event('error'))
  expect(wrapper.get('img').attributes('src')).toBe('/fresh')
  await wrapper.setProps({ src: '', error: '证据访问被拒绝' })
  expect(wrapper.text()).toContain('证据访问被拒绝')
  expect(wrapper.text()).not.toContain('加载中')
})
