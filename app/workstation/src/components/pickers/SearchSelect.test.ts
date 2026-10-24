import { mount } from '@vue/test-utils'
import { NSelect } from 'naive-ui'
import { describe, expect, it } from 'vitest'
import { Comment, Fragment, h } from 'vue'
import SearchSelect from './SearchSelect.vue'
import { matchOption, optionsFromSlot } from './nativeOptions'

/** 模拟模板里常见的写法：静态「全部」项 + v-for 片段 + v-if 没渲染出的注释节点。 */
function templateLikeOptions() {
  return [
    h('option', { value: '' }, '全部平台'),
    h(Fragment, null, [
      h('option', { key: 'h5', value: 'h5' }, ' H5 '),
      h('option', { key: 'web', value: 'web', disabled: '' }, 'Web（未开放）'),
    ]),
    h(Comment, 'v-if'),
  ]
}

describe('optionsFromSlot（读取原生 <option>）', () => {
  it('展开 v-for 片段、跳过注释，文字去掉多余空白，disabled 照原生语义', () => {
    expect(optionsFromSlot(templateLikeOptions())).toEqual([
      { label: '全部平台', value: '', disabled: false },
      { label: 'H5', value: 'h5', disabled: false },
      { label: 'Web（未开放）', value: 'web', disabled: true },
    ])
  })

  it('没写 value 时取文字，数字值保持数字', () => {
    expect(optionsFromSlot([h('option', null, '立川公司'), h('option', { value: 20 }, '20 条/页')])).toEqual([
      { label: '立川公司', value: '立川公司', disabled: false },
      { label: '20 条/页', value: 20, disabled: false },
    ])
  })

  it('按原生 select 的宽松相等匹配选中项', () => {
    const options = optionsFromSlot([h('option', { value: 10 }, '10 条/页')])
    expect(matchOption(options, '10')?.value).toBe(10)
    expect(matchOption(options, 30)).toBeNull()
    expect(matchOption(options, undefined)).toBeNull()
  })
})

describe('SearchSelect（可搜索下拉）', () => {
  it('交给开了搜索的 naive-ui 下拉，选中项按 v-model 显示', () => {
    const wrapper = mount(SearchSelect, {
      props: { modelValue: 'h5', 'onUpdate:modelValue': () => undefined },
      slots: { default: templateLikeOptions },
    })
    const select = wrapper.findComponent(NSelect)
    expect(select.props('filterable')).toBe(true)
    expect(select.props('value')).toBe('h5')
    expect(select.props('options')).toEqual([
      { label: '全部平台', value: '', disabled: false },
      { label: 'H5', value: 'h5', disabled: false },
      { label: 'Web（未开放）', value: 'web', disabled: true },
    ])
  })

  it('选中后按原始类型回传 v-model，并补发 change', async () => {
    const wrapper = mount(SearchSelect, {
      props: { modelValue: 10, 'onUpdate:modelValue': () => undefined },
      slots: { default: () => [10, 20, 50].map((size) => h('option', { value: size }, `${size} 条/页`)) },
    })
    await wrapper.findComponent(NSelect).vm.$emit('update:value', 20)
    expect(wrapper.emitted('update:modelValue')).toEqual([[20]])
    expect(wrapper.emitted('change')).toEqual([[20]])
  })

  it('v-model 没有对应选项时不硬选第一项，显示占位', () => {
    const wrapper = mount(SearchSelect, {
      props: { modelValue: 'gone', 'onUpdate:modelValue': () => undefined },
      slots: { default: templateLikeOptions },
    })
    expect(wrapper.findComponent(NSelect).props('value')).toBeNull()
  })

  it('没绑 v-model 的只读展示框学原生 select，显示第一项', () => {
    const wrapper = mount(SearchSelect, {
      props: { disabled: true },
      slots: { default: () => [h('option', null, '立川公司')] },
    })
    const select = wrapper.findComponent(NSelect)
    expect(select.props('value')).toBe('立川公司')
    expect(select.props('disabled')).toBe(true)
  })
})
