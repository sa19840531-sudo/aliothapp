<script setup lang="ts">
import { computed } from 'vue'

/** 横向条形图（纯 CSS）：各分公司报告数量对比 / 分公司使用排名。 */
export interface BarItem {
  label: string
  value: number
  hint?: string
  color?: string
}

// loading：数据还没回来时空白处显示「加载中…」，不显示「暂无数据」
const props = withDefaults(
  defineProps<{ items: BarItem[]; emptyText?: string; unit?: string; loading?: boolean }>(),
  {
    emptyText: '暂无数据',
    unit: '',
    loading: false,
  },
)

const maxValue = computed(() => Math.max(1, ...props.items.map((item) => item.value)))
</script>

<template>
  <div class="bar-chart">
    <div v-if="!items.length" class="chart-empty">{{ loading ? '加载中…' : emptyText }}</div>
    <div v-for="(item, index) in items" :key="item.label" class="bar-row">
      <span class="bar-rank">{{ index + 1 }}</span>
      <div class="bar-body">
        <div class="bar-meta">
          <span class="bar-label" :title="item.label">{{ item.label }}</span>
          <span class="bar-value">{{ item.value }}{{ unit }}<small v-if="item.hint"> · {{ item.hint }}</small></span>
        </div>
        <div class="bar-track">
          <i :style="{ width: `${(item.value / maxValue) * 100}%`, background: item.color || 'var(--workstation-primary)' }"></i>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bar-chart {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.bar-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.bar-rank {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--workstation-text-muted);
  background: var(--tint-neutral);
  font-size: 12px;
  font-weight: 600;
}

.bar-body {
  flex: 1;
  min-width: 0;
}

.bar-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
  font-size: 13px;
}

.bar-label {
  overflow: hidden;
  color: var(--workstation-text);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bar-value {
  flex-shrink: 0;
  color: var(--workstation-text-strong);
  font-weight: 600;
}

.bar-value small {
  color: var(--workstation-text-faint);
  font-weight: 400;
}

.bar-track {
  height: 8px;
  border-radius: 999px;
  background: var(--tint-neutral);
  overflow: hidden;
}

.bar-track i {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

.chart-empty {
  padding: 32px 0;
  color: var(--workstation-text-faint);
  font-size: 13px;
  text-align: center;
}
</style>
