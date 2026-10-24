<script setup lang="ts">
import { getCurrentInstance, ref, useSlots } from 'vue'
import { NSelect } from 'naive-ui'
import type { SelectInst, SelectProps } from 'naive-ui'
import { matchOption, optionsFromSlot, type NativeOptionValue } from './nativeOptions'

/**
 * 可搜索的单选下拉，原生 <select> 的直接替换件：选项照旧写 <option value="…">文字</option>
 * （v-for、disabled 都照原生语义），展开后可以输入文字筛选。外观与 .qc-input 输入框一致。
 * 没绑 v-model 的只读展示框（如锁定的公司名）学原生 select，显示第一项。
 */
const props = withDefaults(
  defineProps<{
    modelValue?: NativeOptionValue | null
    disabled?: boolean
    placeholder?: string
  }>(),
  { modelValue: undefined, disabled: false, placeholder: '请选择' },
)

const emit = defineEmits<{
  'update:modelValue': [value: NativeOptionValue]
  change: [value: NativeOptionValue]
}>()

const slots = useSlots()
const instance = getCurrentInstance()
const selectRef = ref<SelectInst | null>(null)

// naive-ui 下拉框的外观变量换成设计令牌，浅色 / 暗黑皮肤自动跟随，与 .qc-input 一致
const SELECT_THEME: SelectProps['themeOverrides'] = {
  peers: {
    InternalSelection: {
      heightMedium: '38px',
      fontSizeMedium: '14px',
      borderRadius: 'var(--workstation-radius-sm)',
      paddingSingle: '0 30px 0 12px',
      color: 'var(--workstation-input-bg)',
      colorActive: 'var(--workstation-input-bg)',
      colorDisabled: 'var(--workstation-panel-soft)',
      textColor: 'var(--workstation-text)',
      textColorDisabled: 'var(--workstation-text-muted)',
      placeholderColor: 'var(--workstation-text-faint)',
      caretColor: 'var(--workstation-primary)',
      border: '1px solid var(--workstation-border)',
      borderHover: '1px solid var(--workstation-border)',
      borderActive: '1px solid var(--workstation-primary)',
      borderFocus: '1px solid var(--workstation-primary)',
      boxShadowActive: '0 0 0 2px rgba(37, 99, 235, 0.18)',
      boxShadowFocus: '0 0 0 2px rgba(37, 99, 235, 0.18)',
    },
  },
}

/** 绑了 v-model（或 @update:model-value）才算受控；只读展示框没有更新监听。 */
function isBound() {
  return 'onUpdate:modelValue' in (instance?.vnode.props ?? {})
}

/** 渲染时读 <option>：插槽在渲染函数里调用，父组件的数据变了会跟着更新。 */
function resolveView() {
  const options = optionsFromSlot(slots.default?.())
  const matched = matchOption(options, props.modelValue)
  const fallback = isBound() ? null : (options[0] ?? null)
  return { options, value: (matched ?? fallback)?.value ?? null }
}

function onUpdate(value: NativeOptionValue | null) {
  if (value === null) return
  emit('update:modelValue', value)
  emit('change', value)
}

defineExpose({
  /** 聚焦触发框；支持原生 focus 的 preventScroll，滚动由调用方自己控制。 */
  focus: (options?: FocusOptions) => {
    const root = (selectRef.value as { $el?: HTMLElement } | null)?.$el
    const trigger = root?.querySelector<HTMLElement>('[tabindex="0"]')
    if (trigger) trigger.focus(options)
    else selectRef.value?.focus()
  },
  blur: () => selectRef.value?.blur(),
})
</script>

<template>
  <n-select
    ref="selectRef"
    v-bind="resolveView()"
    class="search-select"
    filterable
    :disabled="disabled"
    :placeholder="placeholder"
    :fallback-option="false"
    :consistent-menu-width="false"
    :theme-overrides="SELECT_THEME"
    @update:value="onUpdate"
  />
</template>
