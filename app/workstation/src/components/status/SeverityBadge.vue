<script setup lang="ts">
import { computed } from 'vue'
import type { RuleSeverity } from '@/types/rule-execution'
import { severityClass, severityLabel } from '@/utils/status'

const props = defineProps<{
  severity: RuleSeverity | string | null
}>()

const label = computed(() => severityLabel(props.severity) || '未定级')
const cls = computed(() => severityClass(props.severity))
</script>

<template>
  <span class="severity-chip" :class="cls">{{ label }}</span>
</template>
