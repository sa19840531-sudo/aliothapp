<script setup lang="ts">
import { computed } from 'vue'
import type { CatalogPageType } from '@/services/ruleCatalog.api'
import CheckboxMultiSelect from './CheckboxMultiSelect.vue'
import { pageScopeOptions } from './selection'

/**
 * 页面范围多选：选项是当前平台后端认可的页面类型（显示页面名称），默认选「全部」。
 * 选全部或都不选时由系统自动识别页面；只选部分时这几类页面都必须巡检到（见 resolvePageScope）。
 */
const props = withDefaults(
  defineProps<{
    /** 当前平台可选的页面类型编码（规则目录 page_scope_options） */
    codes: string[]
    pageTypes: CatalogPageType[]
    modelValue: string[]
    loading?: boolean
    disabled?: boolean
  }>(),
  { loading: false, disabled: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

const options = computed(() => pageScopeOptions(props.codes, props.pageTypes))
</script>

<template>
  <CheckboxMultiSelect
    :model-value="modelValue"
    :options="options"
    :loading="loading"
    :disabled="disabled"
    placeholder="请选择页面"
    @update:model-value="emit('update:modelValue', $event)"
  />
</template>
