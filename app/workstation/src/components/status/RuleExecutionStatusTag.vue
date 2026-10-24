<script setup lang="ts">
import { computed } from 'vue'
import type { TagProps } from 'naive-ui'
import { NTag } from 'naive-ui'
import type { RuleExecutionStatus } from '@/types/rule-execution'
import { ruleExecutionStatusLabel } from '@/utils/status'

const props = defineProps<{
  status: RuleExecutionStatus | string
  outcomeClass?: string
}>()

const statusMeta = computed<{ label: string; type: TagProps['type'] }>(() => {
  if (props.status === 'skipped' && props.outcomeClass === 'coverage_gap') {
    return { label: '未覆盖', type: 'warning' }
  }
  if (props.status === 'skipped' && props.outcomeClass === 'not_applicable') {
    return { label: '不适用', type: 'default' }
  }
  const map: Record<string, { label: string; type: TagProps['type'] }> = {
    pass: { label: ruleExecutionStatusLabel('pass'), type: 'success' },
    fail: { label: ruleExecutionStatusLabel('fail'), type: 'error' },
    warning: { label: ruleExecutionStatusLabel('warning'), type: 'warning' },
    manual_review: { label: ruleExecutionStatusLabel('manual_review'), type: 'warning' },
    evidence_insufficient: {
      label: ruleExecutionStatusLabel('evidence_insufficient'),
      type: 'default',
    },
    skipped: { label: ruleExecutionStatusLabel('skipped'), type: 'default' },
    ignored: { label: ruleExecutionStatusLabel('ignored'), type: 'default' },
    error: { label: ruleExecutionStatusLabel('error'), type: 'error' },
  }
  return map[props.status] || { label: '未知状态', type: 'default' }
})
</script>

<template>
  <n-tag :type="statusMeta.type" round size="small">{{ statusMeta.label }}</n-tag>
</template>
