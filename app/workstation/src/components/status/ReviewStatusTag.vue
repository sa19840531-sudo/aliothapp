<script setup lang="ts">
import { computed } from 'vue'
import type { TagProps } from 'naive-ui'
import { NTag } from 'naive-ui'
import type { ReviewStatus } from '@/types/review'
import { reviewStatusLabel } from '@/utils/status'

const props = defineProps<{
  status: ReviewStatus | string
}>()

const statusMeta = computed<{ label: string; type: TagProps['type'] }>(() => {
  const map: Record<string, { label: string; type: TagProps['type'] }> = {
    pending: { label: reviewStatusLabel('pending'), type: 'warning' },
    approved: { label: reviewStatusLabel('approved'), type: 'success' },
    rejected: { label: reviewStatusLabel('rejected'), type: 'error' },
    needs_recheck: { label: reviewStatusLabel('needs_recheck'), type: 'info' },
  }
  return map[props.status] || { label: '未知状态', type: 'default' }
})
</script>

<template>
  <n-tag :type="statusMeta.type" round size="small">{{ statusMeta.label }}</n-tag>
</template>
