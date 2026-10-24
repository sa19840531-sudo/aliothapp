<script setup lang="ts">
import { computed } from 'vue'

/** 环形占比图（纯 SVG）：任务成功 / 失败 / 待复核分布、平台类型使用占比。 */
export interface DonutItem {
  label: string
  value: number
  color: string
}

// loading：数据还没回来时空白处显示「加载中…」，不显示「暂无数据」
const props = withDefaults(
  defineProps<{ items: DonutItem[]; emptyText?: string; centerLabel?: string; loading?: boolean }>(),
  {
    emptyText: '暂无数据',
    centerLabel: '合计',
    loading: false,
  },
)

const RADIUS = 52
const STROKE = 18
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

const total = computed(() => props.items.reduce((sum, item) => sum + item.value, 0))

const segments = computed(() => {
  let offset = 0
  return props.items
    .filter((item) => item.value > 0)
    .map((item) => {
      const fraction = total.value ? item.value / total.value : 0
      const segment = {
        ...item,
        dash: `${(fraction * CIRCUMFERENCE).toFixed(2)} ${CIRCUMFERENCE.toFixed(2)}`,
        offset: (-offset * CIRCUMFERENCE).toFixed(2),
        percent: Math.round(fraction * 100),
      }
      offset += fraction
      return segment
    })
})
</script>

<template>
  <div class="donut-chart">
    <div v-if="!total" class="chart-empty">{{ loading ? '加载中…' : emptyText }}</div>
    <template v-else>
      <svg viewBox="0 0 140 140" role="img">
        <circle cx="70" cy="70" :r="RADIUS" fill="none" stroke="var(--tint-neutral)" :stroke-width="STROKE" />
        <circle
          v-for="segment in segments"
          :key="segment.label"
          cx="70"
          cy="70"
          :r="RADIUS"
          fill="none"
          :stroke="segment.color"
          :stroke-width="STROKE"
          :stroke-dasharray="segment.dash"
          :stroke-dashoffset="segment.offset"
          transform="rotate(-90 70 70)"
        >
          <title>{{ segment.label }} {{ segment.value }}（{{ segment.percent }}%）</title>
        </circle>
        <text x="70" y="66" text-anchor="middle" class="center-value">{{ total }}</text>
        <text x="70" y="84" text-anchor="middle" class="center-label">{{ centerLabel }}</text>
      </svg>
      <ul class="legend">
        <li v-for="segment in segments" :key="segment.label">
          <i :style="{ background: segment.color }"></i>
          <span class="legend-label">{{ segment.label }}</span>
          <span class="legend-value">{{ segment.value }} · {{ segment.percent }}%</span>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.donut-chart {
  display: flex;
  align-items: center;
  gap: 20px;
}

svg {
  flex-shrink: 0;
  width: 150px;
  height: 150px;
}

.center-value {
  fill: var(--workstation-text-strong);
  font-size: 22px;
  font-weight: 700;
}

.center-label {
  fill: var(--workstation-text-faint);
  font-size: 11px;
}

.legend {
  flex: 1;
  margin: 0;
  padding: 0;
  list-style: none;
}

.legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
  font-size: 13px;
}

.legend i {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.legend-label {
  flex: 1;
  color: var(--workstation-text);
}

.legend-value {
  color: var(--workstation-text-muted);
}

.chart-empty {
  width: 100%;
  padding: 32px 0;
  color: var(--workstation-text-faint);
  font-size: 13px;
  text-align: center;
}
</style>
