<script setup lang="ts">
import { computed } from 'vue'

/**
 * 轻量折线图（纯 SVG，不引第三方图表库）：运营总览 / 使用分析的趋势图。
 * 多条线共用一个坐标轴；横轴只标首、中、尾三个日期，避免 30 个点挤在一起。
 */
export interface LineSeries {
  name: string
  color: string
  values: number[]
}

const props = withDefaults(
  defineProps<{
    labels: string[]
    series: LineSeries[]
    height?: number
    emptyText?: string
    /** 数据还没回来时空白处显示「加载中…」，不显示「暂无数据」 */
    loading?: boolean
  }>(),
  { height: 220, emptyText: '暂无数据', loading: false },
)

const WIDTH = 640
const PADDING = { top: 16, right: 16, bottom: 28, left: 40 }

const plotWidth = WIDTH - PADDING.left - PADDING.right
const plotHeight = computed(() => props.height - PADDING.top - PADDING.bottom)

const maxValue = computed(() => {
  const all = props.series.flatMap((item) => item.values)
  const max = Math.max(0, ...all)
  if (max === 0) return 1
  // 顶部留一点空间，刻度取整
  const magnitude = 10 ** Math.floor(Math.log10(max))
  return Math.ceil((max * 1.15) / magnitude) * magnitude
})

const hasData = computed(() => props.series.some((item) => item.values.some((value) => value > 0)))

function x(index: number) {
  const count = Math.max(props.labels.length - 1, 1)
  return PADDING.left + (plotWidth * index) / count
}

function y(value: number) {
  return PADDING.top + plotHeight.value - (plotHeight.value * value) / maxValue.value
}

const gridLines = computed(() =>
  [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    y: PADDING.top + plotHeight.value * (1 - ratio),
    label: formatTick(maxValue.value * ratio),
  })),
)

function formatTick(value: number) {
  if (value >= 1000) return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}k`
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

const paths = computed(() =>
  props.series.map((item) => {
    const points = item.values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`)
    const line = points.length ? `M${points.join(' L')}` : ''
    const area = points.length
      ? `${line} L${x(item.values.length - 1).toFixed(1)},${y(0).toFixed(1)} L${x(0).toFixed(1)},${y(0).toFixed(1)} Z`
      : ''
    return { ...item, line, area }
  }),
)

const axisLabels = computed(() => {
  const count = props.labels.length
  if (!count) return []
  const picks = Array.from(new Set([0, Math.floor((count - 1) / 2), count - 1]))
  return picks.map((index) => ({ index, text: props.labels[index]?.slice(5) ?? '' }))
})
</script>

<template>
  <div class="line-chart" :style="{ height: `${height}px` }">
    <svg v-if="hasData" :viewBox="`0 0 ${WIDTH} ${height}`" preserveAspectRatio="none" role="img">
      <g class="grid">
        <template v-for="line in gridLines" :key="line.y">
          <line :x1="PADDING.left" :x2="WIDTH - PADDING.right" :y1="line.y" :y2="line.y" />
          <text :x="PADDING.left - 6" :y="line.y + 4" text-anchor="end">{{ line.label }}</text>
        </template>
      </g>
      <g v-for="item in paths" :key="item.name">
        <path :d="item.area" :fill="item.color" fill-opacity="0.08" />
        <path :d="item.line" :stroke="item.color" fill="none" stroke-width="2" stroke-linejoin="round" />
        <circle
          v-for="(value, index) in item.values"
          :key="index"
          :cx="x(index)"
          :cy="y(value)"
          r="2.5"
          :fill="item.color"
        >
          <title>{{ labels[index] }} · {{ item.name }} {{ value }}</title>
        </circle>
      </g>
      <g class="axis">
        <text v-for="tick in axisLabels" :key="tick.index" :x="x(tick.index)" :y="height - 8" text-anchor="middle">
          {{ tick.text }}
        </text>
      </g>
    </svg>
    <div v-else class="chart-empty">{{ loading ? '加载中…' : emptyText }}</div>
    <div v-if="hasData && series.length > 1" class="legend">
      <span v-for="item in series" :key="item.name"><i :style="{ background: item.color }"></i>{{ item.name }}</span>
    </div>
  </div>
</template>

<style scoped>
.line-chart {
  position: relative;
  width: 100%;
}

svg {
  width: 100%;
  height: 100%;
}

.grid line {
  stroke: var(--workstation-border);
  stroke-dasharray: 3 3;
}

.grid text,
.axis text {
  fill: var(--workstation-text-faint);
  font-size: 11px;
}

.chart-empty {
  display: grid;
  place-items: center;
  height: 100%;
  color: var(--workstation-text-faint);
  font-size: 13px;
}

.legend {
  position: absolute;
  top: 0;
  right: 8px;
  display: flex;
  gap: 12px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.legend i {
  display: inline-block;
  width: 10px;
  height: 3px;
  margin-right: 4px;
  border-radius: 2px;
  vertical-align: middle;
}
</style>
