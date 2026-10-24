<script setup lang="ts">
import { computed } from 'vue'
import type { TagProps } from 'naive-ui'
import { NTag } from 'naive-ui'
import type { TaskStatus } from '@/types/task'
import { taskStatusLabel } from '@/utils/status'

const props = defineProps<{
  status: TaskStatus | string
}>()

const statusMeta = computed<{ label: string; type: TagProps['type'] }>(() => {
  const map: Record<string, { label: string; type: TagProps['type'] }> = {
    queued: { label: taskStatusLabel('queued'), type: 'default' },
    running: { label: taskStatusLabel('running'), type: 'info' },
    canceling: { label: taskStatusLabel('canceling'), type: 'warning' },
    review: { label: taskStatusLabel('review'), type: 'warning' },
    done: { label: taskStatusLabel('done'), type: 'success' },
    failed: { label: taskStatusLabel('failed'), type: 'error' },
    canceled: { label: taskStatusLabel('canceled'), type: 'default' },
  }
  return map[props.status] || { label: '未知状态', type: 'default' }
})
</script>

<template>
  <n-tag :type="statusMeta.type" round size="small">{{ statusMeta.label }}</n-tag>
</template>
