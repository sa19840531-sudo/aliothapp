<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  PICKER_ALL,
  isAllSelected,
  selectedValues,
  toggleAll,
  toggleOption,
  type PickerOption,
} from './selection'

/**
 * 带「全部」的下拉多选（对齐 2.1 原型 config-complete.js 的规则集 / 页面范围选择器）：
 * 触发框显示「全部」或已选项，展开后可输入文字筛选再逐项勾选；点页面其他地方或按 Esc 收起。
 */
const props = withDefaults(
  defineProps<{
    options: PickerOption[]
    /** 选中值；含 PICKER_ALL 表示「全部」 */
    modelValue: string[]
    placeholder?: string
    loading?: boolean
    disabled?: boolean
  }>(),
  { placeholder: '请选择', loading: false, disabled: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

const open = ref(false)
const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const keyword = ref('')

const optionValues = computed(() => props.options.map((option) => option.value))
const searching = computed(() => keyword.value.trim() !== '')
/** 按文字筛选（不区分大小写）；筛选只影响显示，「全部」和勾选仍按完整选项算。 */
const visibleOptions = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  return text ? props.options.filter((option) => option.label.toLowerCase().includes(text)) : props.options
})

// 展开时聚焦搜索框，收起时清空关键字
watch(open, (value) => {
  if (value) void nextTick(() => searchRef.value?.focus())
  else keyword.value = ''
})
const allChecked = computed(() => isAllSelected(props.modelValue))
const checkedValues = computed(() => new Set(selectedValues(props.modelValue, optionValues.value)))

const triggerLabel = computed(() => {
  if (allChecked.value) return '全部'
  const labels = props.options
    .filter((option) => checkedValues.value.has(option.value))
    .map((option) => option.label)
  return labels.length ? labels.join('、') : props.placeholder
})

const showPlaceholder = computed(() => !allChecked.value && checkedValues.value.size === 0)

function checkedOf(event: Event) {
  return (event.target as HTMLInputElement).checked
}

function onToggleAll(event: Event) {
  emit('update:modelValue', toggleAll(checkedOf(event)))
}

function onToggleOption(value: string, event: Event) {
  emit(
    'update:modelValue',
    toggleOption(props.modelValue, value, checkedOf(event), optionValues.value),
  )
}

function toggleMenu() {
  if (!props.disabled) open.value = !open.value
}

function onDocumentClick(event: MouseEvent) {
  if (open.value && rootRef.value && !rootRef.value.contains(event.target as Node)) {
    open.value = false
  }
}

/** Esc 收起菜单，焦点回到触发框，避免停在已隐藏的勾选框上。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !open.value) return
  open.value = false
  triggerRef.value?.focus()
}

onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))
</script>

<template>
  <div ref="rootRef" class="multi-select" :class="{ 'is-open': open }" @keydown="onKeydown">
    <button
      ref="triggerRef"
      type="button"
      class="multi-trigger"
      :disabled="disabled"
      :aria-expanded="open"
      :title="triggerLabel"
      @click="toggleMenu"
    >
      <span class="multi-trigger-label" :class="{ 'is-placeholder': showPlaceholder }">
        {{ triggerLabel }}
      </span>
      <span class="multi-arrow" aria-hidden="true">▾</span>
    </button>
    <div v-show="open" class="multi-menu">
      <input
        v-if="options.length"
        ref="searchRef"
        v-model="keyword"
        type="search"
        class="multi-search"
        placeholder="输入文字筛选"
        aria-label="筛选选项"
      />
      <label v-if="!searching" class="multi-option">
        <input type="checkbox" :value="PICKER_ALL" :checked="allChecked" @change="onToggleAll" />
        <span>全部</span>
      </label>
      <label v-for="option in visibleOptions" :key="option.value" class="multi-option">
        <input
          type="checkbox"
          :value="option.value"
          :checked="checkedValues.has(option.value)"
          @change="onToggleOption(option.value, $event)"
        />
        <span>{{ option.label }}</span>
      </label>
      <p v-if="!options.length" class="multi-empty">{{ loading ? '加载中…' : '暂无可选项' }}</p>
      <p v-else-if="!visibleOptions.length" class="multi-empty">没有匹配的选项</p>
    </div>
  </div>
</template>

<style scoped>
.multi-select {
  position: relative;
  width: 100%;
  min-width: 0;
}

.multi-select.is-open {
  z-index: 30;
}

/* 触发框与 .qc-input、SearchSelect 同一套外观 */
.multi-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text);
  background: var(--workstation-input-bg);
  font: inherit;
  font-size: 14px;
  line-height: 1.4;
  text-align: left;
  cursor: pointer;
}

.multi-trigger:focus-visible,
.multi-select.is-open .multi-trigger {
  border-color: var(--workstation-primary);
  outline: 0;
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.18);
}

.multi-trigger:disabled {
  color: var(--workstation-text-muted);
  background: var(--workstation-panel-soft);
  cursor: not-allowed;
}

.multi-trigger-label {
  overflow: hidden;
  min-width: 0;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.multi-trigger-label.is-placeholder {
  color: var(--workstation-text-faint);
}

.multi-arrow {
  flex-shrink: 0;
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.multi-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  left: 0;
  max-height: 286px;
  padding: 4px;
  overflow-y: auto;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel);
  box-shadow: var(--workstation-shadow-lg);
}

/* 选项多时列表滚动，搜索框停在顶部 */
.multi-search {
  position: sticky;
  top: 0;
  z-index: 1;
  display: block;
  width: 100%;
  margin-bottom: 4px;
  padding: 6px 10px;
  border: 1px solid var(--workstation-border);
  border-radius: 6px;
  color: var(--workstation-text);
  background: var(--workstation-input-bg);
  font: inherit;
  font-size: 13px;
  outline: 0;
}

.multi-search:focus {
  border-color: var(--workstation-primary);
}

.multi-search::placeholder {
  color: var(--workstation-text-faint);
}

.multi-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  color: var(--workstation-text);
  font-size: 14px;
  cursor: pointer;
}

.multi-option:hover {
  background: var(--workstation-panel-soft);
}

.multi-option input {
  flex-shrink: 0;
  margin: 0;
  accent-color: var(--workstation-primary);
}

.multi-empty {
  margin: 0;
  padding: 8px 12px;
  color: var(--workstation-text-muted);
  font-size: 13px;
}
</style>
