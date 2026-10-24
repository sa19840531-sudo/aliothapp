<script setup lang="ts">
import { computed } from 'vue'
import type { WebStageDurations } from '@/types/task'

const props = defineProps<{
  durations: WebStageDurations
}>()

const rows = computed(() => [
  { label: '探索耗时', value: formatDuration(props.durations.exploration_ms) },
  { label: '规则质检耗时', value: formatDuration(props.durations.inspection_ms) },
  { label: '报告生成耗时', value: formatDuration(props.durations.report_generation_ms) },
  { label: '提交至报告总耗时', value: formatDuration(props.durations.total_ms) },
])

function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null || !Number.isFinite(milliseconds) || milliseconds < 0) {
    return '未完成'
  }
  if (milliseconds < 1000) {
    return `${Math.floor(milliseconds)}毫秒`
  }
  const totalSeconds = Math.floor(milliseconds / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const parts: string[] = []
  if (hours > 0) parts.push(`${hours}小时`)
  if (minutes > 0) parts.push(`${minutes}分`)
  if (seconds > 0 || parts.length === 0) parts.push(`${seconds}秒`)
  return parts.join('')
}
</script>

<template>
  <div class="web-stage-duration-grid" data-testid="web-stage-durations">
    <div v-for="row in rows" :key="row.label" class="web-stage-duration-item">
      <span>{{ row.label }}</span>
      <strong>{{ row.value }}</strong>
    </div>
  </div>
</template>

<style scoped>
.web-stage-duration-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.web-stage-duration-item {
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  background: var(--workstation-panel-soft);
}

.web-stage-duration-item span,
.web-stage-duration-item strong {
  display: block;
}

.web-stage-duration-item span {
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.web-stage-duration-item strong {
  margin-top: 5px;
  color: var(--workstation-text);
  font-size: 12px;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

@media (max-width: 860px) {
  .web-stage-duration-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 520px) {
  .web-stage-duration-grid {
    grid-template-columns: 1fr;
  }
}
</style>
