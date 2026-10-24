import { defineComponent, getCurrentInstance, h, ref, type VNode } from 'vue'
import { matchOption, optionsFromSlot } from '@/components/pickers/nativeOptions'

/**
 * 单测里替换 SearchSelect：渲染成原生 <select>，选项与取值规则和真组件一致，
 * 页面测试照旧用 find('select').setValue(...) 操作；可搜索下拉本身在 SearchSelect.test.ts 里测。
 */
export const NativeSelectStub = defineComponent({
  name: 'SearchSelect',
  props: {
    modelValue: { type: [String, Number], default: undefined },
    disabled: { type: Boolean, default: false },
    placeholder: { type: String, default: '请选择' },
  },
  emits: ['update:modelValue', 'change'],
  setup(props, { emit, slots, expose }) {
    const instance = getCurrentInstance()
    const selectRef = ref<HTMLSelectElement | null>(null)
    expose({
      focus: (options?: FocusOptions) => selectRef.value?.focus(options),
      blur: () => selectRef.value?.blur(),
    })
    return () => {
      const options = optionsFromSlot(slots.default?.())
      const bound = 'onUpdate:modelValue' in (instance?.vnode.props ?? {})
      const selected = matchOption(options, props.modelValue) ?? (bound ? null : (options[0] ?? null))
      // 与原生 v-model 一致：没有匹配项时一项都不选
      const syncSelection = (vnode: VNode) => {
        ;(vnode.el as HTMLSelectElement).selectedIndex = selected ? options.indexOf(selected) : -1
      }
      return h(
        'select',
        {
          ref: selectRef,
          disabled: props.disabled,
          onVnodeMounted: syncSelection,
          onVnodeUpdated: syncSelection,
          onChange: (event: Event) => {
            const option = options[(event.target as HTMLSelectElement).selectedIndex]
            if (!option) return
            emit('update:modelValue', option.value)
            emit('change', option.value)
          },
        },
        options.map((option) =>
          h('option', { value: String(option.value), disabled: option.disabled }, option.label),
        ),
      )
    }
  },
})
