import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import type { CatalogPageType, CatalogRuleset } from '@/services/ruleCatalog.api'
import PageScopeMultiSelect from './PageScopeMultiSelect.vue'
import RulesetMultiSelect from './RulesetMultiSelect.vue'
import { PICKER_ALL, allSelection } from './selection'

const rulesets: CatalogRuleset[] = [
  {
    ruleset_id: 'rs-1',
    ruleset_code: 'alioth_first_ruleset',
    version: 1,
    name: '玉衡平台首期质检规则集',
    description: '',
    status: 'enabled',
    enabled_rule_count: 70,
    rule_codes: [],
  },
  {
    ruleset_id: 'rs-2',
    ruleset_code: 'basic_compliance',
    version: 1,
    name: '基础合规规则集',
    description: '',
    status: 'enabled',
    enabled_rule_count: 2,
    rule_codes: [],
  },
]

function pageType(code: string, name: string): CatalogPageType {
  return {
    code,
    name,
    category: 'business',
    rule_matchable: true,
    coverage_required: true,
    description: '',
    related_rule_codes: [],
  }
}

const pageTypes = [pageType('home', '首页'), pageType('video_list', '视频列表页'), pageType('video_detail', '视频详情页')]

function checkedStates(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('input[type="checkbox"]').map((box) => (box.element as HTMLInputElement).checked)
}

function triggerText(wrapper: ReturnType<typeof mount>) {
  return wrapper.get('.multi-trigger-label').text()
}

/** 挂载时把 v-model 回写到 props，模拟父组件双向绑定。 */
function mountRulesetPicker() {
  const wrapper = mount(RulesetMultiSelect, {
    props: {
      rulesets,
      modelValue: allSelection(),
      'onUpdate:modelValue': (value: string[]) => {
        void wrapper.setProps({ modelValue: value })
      },
    },
    attachTo: document.body,
  })
  return wrapper
}

function mountPageScopePicker(props: { codes: string[]; pageTypes: CatalogPageType[]; loading?: boolean }) {
  const wrapper = mount(PageScopeMultiSelect, {
    props: {
      ...props,
      modelValue: allSelection(),
      'onUpdate:modelValue': (value: string[]) => {
        void wrapper.setProps({ modelValue: value })
      },
    },
    attachTo: document.body,
  })
  return wrapper
}

// 每个用例结束后卸载，顺带移除选择器挂在 document 上的点击监听
enableAutoUnmount(afterEach)

describe('规则集多选', () => {
  it('默认显示「全部」，展开后全部与每个规则集都已勾选', async () => {
    const wrapper = mountRulesetPicker()
    expect(triggerText(wrapper)).toBe('全部')

    await wrapper.get('button.multi-trigger').trigger('click')
    expect(wrapper.get('.multi-menu').attributes('style') ?? '').not.toContain('display: none')
    expect(checkedStates(wrapper)).toEqual([true, true, true])
    expect(wrapper.text()).toContain('玉衡平台首期质检规则集 · 70 条规则')
    expect(wrapper.text()).toContain('基础合规规则集 · 2 条规则')
  })

  it('取消一个规则集后退出「全部」，触发框显示剩下的规则集', async () => {
    const wrapper = mountRulesetPicker()
    await wrapper.get('input[value="rs-2"]').setValue(false)

    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual([['rs-1']])
    expect(triggerText(wrapper)).toBe('玉衡平台首期质检规则集 · 70 条规则')
    expect(checkedStates(wrapper)).toEqual([false, true, false])

    await wrapper.get('input[value="rs-2"]').setValue(true)
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual([[PICKER_ALL]])
  })

  it('取消「全部」后一个都不选，显示占位文字', async () => {
    const wrapper = mountRulesetPicker()
    await wrapper.get(`input[value="${PICKER_ALL}"]`).setValue(false)

    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual([[]])
    expect(triggerText(wrapper)).toBe('请选择规则集')
  })
})

describe('页面范围多选', () => {
  it('选项显示页面名称，默认「全部」', async () => {
    const wrapper = mountPageScopePicker({
      codes: ['video_list', 'video_detail'],
      pageTypes,
    })
    expect(triggerText(wrapper)).toBe('全部')
    const labels = wrapper.findAll('.multi-option').map((row) => row.text())
    expect(labels).toEqual(['全部', '视频列表页', '视频详情页'])
  })

  it('只留部分页面时，触发框列出所选页面名称', async () => {
    const wrapper = mountPageScopePicker({
      codes: ['home', 'video_list', 'video_detail'],
      pageTypes,
    })
    await wrapper.get('input[value="video_list"]').setValue(false)
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual([['home', 'video_detail']])
    expect(triggerText(wrapper)).toBe('首页、视频详情页')
  })

  it('点选择器外面的地方收起菜单', async () => {
    const wrapper = mountPageScopePicker({
      codes: ['home'],
      pageTypes,
    })
    await wrapper.get('button.multi-trigger').trigger('click')
    expect(wrapper.get('.multi-menu').attributes('style') ?? '').not.toContain('display: none')

    document.body.click()
    await nextTick()
    expect(wrapper.get('.multi-menu').attributes('style') ?? '').toContain('display: none')
  })

  it('按 Esc 收起菜单，焦点回到触发框', async () => {
    const wrapper = mountPageScopePicker({
      codes: ['home'],
      pageTypes,
    })
    await wrapper.get('button.multi-trigger').trigger('click')
    const box = wrapper.get('input[value="home"]')
    ;(box.element as HTMLInputElement).focus()
    await box.trigger('keydown', { key: 'Escape' })

    expect(wrapper.get('.multi-menu').attributes('style') ?? '').toContain('display: none')
    expect(document.activeElement).toBe(wrapper.get('button.multi-trigger').element)
  })

  it('展开后可输入文字筛选，筛选时不显示「全部」，收起后清空', async () => {
    const wrapper = mountPageScopePicker({
      codes: ['home', 'video_list', 'video_detail'],
      pageTypes,
    })
    await wrapper.get('button.multi-trigger').trigger('click')
    await nextTick()
    const search = wrapper.get('input.multi-search')
    expect(document.activeElement).toBe(search.element)

    await search.setValue('视频')
    expect(wrapper.findAll('.multi-option').map((row) => row.text())).toEqual(['视频列表页', '视频详情页'])

    // 筛选只影响显示：取消一项后按完整选项回写
    await wrapper.get('input[value="video_list"]').setValue(false)
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual([['home', 'video_detail']])

    await search.setValue('不存在的页面')
    expect(wrapper.get('.multi-empty').text()).toBe('没有匹配的选项')

    await wrapper.get('button.multi-trigger').trigger('click')
    expect((search.element as HTMLInputElement).value).toBe('')
  })

  it('目录还没加载时菜单里提示加载中', async () => {
    const wrapper = mountPageScopePicker({
      codes: [],
      pageTypes: [],
      loading: true,
    })
    expect(wrapper.get('.multi-empty').text()).toBe('加载中…')
    expect(triggerText(wrapper)).toBe('全部')
  })
})
