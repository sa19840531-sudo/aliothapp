<script setup lang="ts">
import { computed } from 'vue'
import type { CatalogRuleset } from '@/services/ruleCatalog.api'
import CheckboxMultiSelect from './CheckboxMultiSelect.vue'
import { rulesetOptionLabel } from './selection'

/** 规则集多选：选项是启用中的规则集，显示为「名称 · N 条规则」，默认选「全部」。 */
const props = withDefaults(
  defineProps<{
    rulesets: CatalogRuleset[]
    modelValue: string[]
    loading?: boolean
    disabled?: boolean
  }>(),
  { loading: false, disabled: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

const options = computed(() =>
  props.rulesets.map((ruleset) => ({
    value: ruleset.ruleset_id,
    label: rulesetOptionLabel(ruleset),
  })),
)
</script>

<template>
  <CheckboxMultiSelect
    :model-value="modelValue"
    :options="options"
    :loading="loading"
    :disabled="disabled"
    placeholder="请选择规则集"
    @update:model-value="emit('update:modelValue', $event)"
  />
</template>
