<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NGi,
  NGrid,
  NSelect,
  NSpace,
  NStatistic,
  NTag,
  NText,
  NTooltip,
  type DataTableColumns,
} from 'naive-ui'
import {
  getLlmCostReport,
  getLlmUsageEventReport,
  type CostBucket,
  type CostReport,
  type CostRunItem,
  type CostUsageEventItem,
  type CostUsageEventReport,
  type CostTaskItem,
} from '../api/costReports'

const loading = ref(false)
/** 加载失败要说出来：原来没有 catch，首次失败整页一直空白 */
const loadError = ref('')
const days = ref(30)
const report = ref<CostReport | null>(null)
const eventReport = ref<CostUsageEventReport | null>(null)

const dayOptions = [
  { label: '近 7 天', value: 7 },
  { label: '近 30 天', value: 30 },
  { label: '近 90 天', value: 90 },
  { label: '近 180 天', value: 180 },
]

const maxDayCost = computed(() =>
  Math.max(...(report.value?.by_day.map((item) => item.total_cost) ?? [0]), 0),
)

const taskColumns: DataTableColumns<CostTaskItem> = [
  {
    title: '任务',
    key: 'task_name',
    width: 300,
    fixed: 'left',
    render(row) {
      return renderTitleCell(row.task_name || '未命名任务', row.task_id || '无任务 ID')
    },
  },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render(row) {
      return renderStatusTag(row.status)
    },
  },
  {
    title: 'Run',
    key: 'run_count',
    width: 80,
    align: 'right',
  },
  {
    title: '模型',
    key: 'model',
    width: 170,
    render(row) {
      return renderEllipsis(`${row.provider}/${row.model}`)
    },
  },
  {
    title: '调用',
    key: 'ai_calls',
    width: 90,
    align: 'right',
  },
  {
    title: '用量完整性',
    key: 'usage_missing_count',
    width: 130,
    render(row) {
      return renderUsageCompleteness(row)
    },
  },
  {
    title: 'Tokens',
    key: 'total_tokens',
    width: 180,
    align: 'right',
    render(row) {
      return renderTokenBreakdown(row)
    },
  },
  {
    title: '成本',
    key: 'total_cost',
    width: 180,
    align: 'right',
    render(row) {
      return renderCostBreakdown(row, row.currency)
    },
  },
  {
    title: '计价',
    key: 'pricing_source',
    width: 240,
    render(row) {
      return renderPricingCell(row.pricing_source, row.applied_pricing_tiers)
    },
  },
  {
    title: '最近 Run',
    key: 'latest_run_id',
    width: 220,
    render(row) {
      return renderTitleCell(row.latest_run_status, row.latest_run_id || '无 Run ID')
    },
  },
  {
    title: '最近创建时间',
    key: 'latest_run_created_at',
    width: 180,
    render(row) {
      return formatDateTime(row.latest_run_created_at)
    },
  },
]

const runColumns: DataTableColumns<CostRunItem> = [
  {
    title: '任务 / Run',
    key: 'task_name',
    width: 300,
    fixed: 'left',
    render(row) {
      return renderTitleCell(row.task_name || '未命名任务', row.run_id)
    },
  },
  {
    title: 'Agent',
    key: 'agent_name',
    width: 190,
    render(row) {
      return renderTitleCell(row.agent_name || row.agent_type || '未知 Agent', row.agent_type)
    },
  },
  {
    title: '模型',
    key: 'model',
    width: 170,
    render(row) {
      return renderEllipsis(`${row.provider}/${row.model}`)
    },
  },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render(row) {
      return renderStatusTag(row.status)
    },
  },
  {
    title: '调用',
    key: 'ai_calls',
    width: 90,
    align: 'right',
  },
  {
    title: '用量完整性',
    key: 'usage_missing_count',
    width: 130,
    render(row) {
      return renderUsageCompleteness(row)
    },
  },
  {
    title: 'Tokens',
    key: 'total_tokens',
    width: 180,
    align: 'right',
    render(row) {
      return renderTokenBreakdown(row)
    },
  },
  {
    title: '成本',
    key: 'total_cost',
    width: 180,
    align: 'right',
    render(row) {
      return renderCostBreakdown(row, row.currency)
    },
  },
  {
    title: '计价',
    key: 'pricing_source',
    width: 240,
    render(row) {
      return renderPricingCell(
        row.pricing_configured ? row.pricing_source : '未配置单价',
        row.applied_pricing_tiers,
      )
    },
  },
  {
    title: '创建时间',
    key: 'created_at',
    width: 180,
    render(row) {
      return formatDateTime(row.created_at)
    },
  },
]

const eventColumns: DataTableColumns<CostUsageEventItem> = [
  {
    title: '来源 / 归属',
    key: 'source',
    width: 280,
    fixed: 'left',
    render(row) {
      const title = `${scopeLabel(row.scope_type)} / ${row.source}`
      const secondary = [row.operation, row.task_name || row.task_id, row.agent_run_id]
        .filter(Boolean)
        .join(' · ')
      return renderTitleCell(title, secondary || row.event_id)
    },
  },
  {
    title: '模型',
    key: 'model',
    width: 180,
    render(row) {
      return renderTitleCell(`${row.provider}/${row.model}`, row.api_style)
    },
  },
  {
    title: '用量状态',
    key: 'usage_available',
    width: 120,
    render(row) {
      return h(
        NTag,
        {
          size: 'small',
          type: row.usage_available ? 'success' : 'warning',
          bordered: false,
        },
        { default: () => (row.usage_available ? '已返回用量' : '缺少用量') },
      )
    },
  },
  {
    title: 'Tokens',
    key: 'total_tokens',
    width: 190,
    align: 'right',
    render(row) {
      return renderTokenBreakdown(row)
    },
  },
  {
    title: '成本',
    key: 'total_cost',
    width: 190,
    align: 'right',
    render(row) {
      return renderCostBreakdown(row, row.currency)
    },
  },
  {
    title: '阶梯 / 单价',
    key: 'pricing_tier',
    width: 260,
    render(row) {
      return renderTitleCell(
        row.pricing_tier || '未命中阶梯',
        `in ${formatPrice(row.input_price_per_million_tokens)} / out ${formatPrice(
          row.output_price_per_million_tokens,
        )} / cache ${formatPrice(row.cache_hit_price_per_million_tokens)}`,
      )
    },
  },
  {
    title: '公式',
    key: 'calculation_formula',
    width: 260,
    render(row) {
      return renderEllipsis(row.calculation_formula || '无公式')
    },
  },
  {
    title: '创建时间',
    key: 'created_at',
    width: 180,
    render(row) {
      return formatDateTime(row.created_at)
    },
  },
]

function renderStatusTag(status: string) {
  return h(
    NTag,
    { size: 'small', type: statusTagType(status), bordered: false },
    { default: () => status },
  )
}

function renderTitleCell(title: string, secondary?: string | null) {
  return h('div', { class: 'run-title' }, [
    renderEllipsis(title || '-', 'title-text'),
    secondary ? renderEllipsis(secondary, 'muted-text mono-text') : null,
  ])
}

function renderEllipsis(value: string, className = '') {
  const text = value || '-'
  return h(
    NTooltip,
    { trigger: 'hover', placement: 'top' },
    {
      trigger: () => h('span', { class: ['ellipsis-cell', className] }, text),
      default: () => text,
    },
  )
}

async function loadReport() {
  loading.value = true
  try {
    const [runReport, usageEventReport] = await Promise.all([
      getLlmCostReport({ days: days.value, limit: 80 }),
      getLlmUsageEventReport({ days: days.value, limit: 120 }),
    ])
    report.value = runReport
    eventReport.value = usageEventReport
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '模型调用明细加载失败'
  } finally {
    loading.value = false
  }
}

function statusTagType(status: string) {
  if (status === 'completed' || status === 'done') {
    return 'success'
  }
  if (status === 'failed' || status === 'error') {
    return 'error'
  }
  return 'default'
}

function formatMoney(value: number, currency: string) {
  const prefix = currency.toUpperCase() === 'CNY' ? '¥' : `${currency.toUpperCase()} `
  return `${prefix}${value.toLocaleString('zh-CN', {
    minimumFractionDigits: value < 1 ? 6 : 2,
    maximumFractionDigits: value < 1 ? 6 : 2,
  })}`
}

function formatNumber(value: number) {
  return value.toLocaleString('zh-CN')
}

function formatPrice(value: number) {
  return `¥${value.toLocaleString('zh-CN', {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}/1M`
}

function formatDateTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function bucketRatio(bucket: CostBucket) {
  if (maxDayCost.value <= 0) {
    return '0%'
  }
  return `${Math.max(4, Math.round((bucket.total_cost / maxDayCost.value) * 100))}%`
}

function bucketColumns(currency: string, countTitle = 'Run'): DataTableColumns<CostBucket> {
  return [
    { title: '分组', key: 'key', minWidth: 180 },
    { title: countTitle, key: 'run_count', width: 90, align: 'right' },
    { title: '调用', key: 'ai_calls', width: 90, align: 'right' },
    {
      title: '缺失用量',
      key: 'usage_missing_count',
      width: 100,
      align: 'right',
    },
    {
      title: 'Tokens',
      key: 'total_tokens',
      width: 130,
      align: 'right',
      render(row) {
        return formatNumber(row.total_tokens)
      },
    },
    {
      title: '成本',
      key: 'total_cost',
      width: 120,
      align: 'right',
      render(row) {
        return formatMoney(row.total_cost, currency)
      },
    },
  ]
}

function renderTokenBreakdown(row: {
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  billable_input_tokens: number
  total_tokens: number
}) {
  return h('div', { class: 'metric-cell align-right' }, [
    h('strong', formatNumber(row.total_tokens)),
    h(
      'small',
      `输入 ${formatNumber(row.input_tokens)} / 计费 ${formatNumber(
        row.billable_input_tokens,
      )} / 输出 ${formatNumber(row.output_tokens)} / 缓存 ${formatNumber(row.cache_hit_tokens)}`,
    ),
  ])
}

function renderCostBreakdown(
  row: {
    input_cost: number
    output_cost: number
    cache_hit_cost: number
    total_cost: number
    usage_missing_count?: number
  },
  currency: string,
) {
  return h('div', { class: 'metric-cell align-right' }, [
    h('strong', formatMoney(row.total_cost, currency)),
    h(
      'small',
      `${row.usage_missing_count ? '已知成本下界 · ' : ''}输入 ${formatMoney(
        row.input_cost,
        currency,
      )} / 输出 ${formatMoney(row.output_cost, currency)} / 缓存 ${formatMoney(
        row.cache_hit_cost,
        currency,
      )}`,
    ),
  ])
}

function renderUsageCompleteness(row: { ai_calls: number; usage_missing_count: number }) {
  if (!row.ai_calls) {
    return h(NText, { depth: 3 }, { default: () => '无调用' })
  }
  if (!row.usage_missing_count) {
    return h(
      NTag,
      { size: 'small', type: 'success', bordered: false },
      { default: () => '完整' },
    )
  }
  return h(
    NTooltip,
    { trigger: 'hover', placement: 'top' },
    {
      trigger: () =>
        h(
          NTag,
          { size: 'small', type: 'warning', bordered: false },
          { default: () => `缺 ${formatNumber(row.usage_missing_count)} 次` },
        ),
      default: () => '供应商未返回 usage，当前 Token 和成本仅代表已知下界，不能按 0 元理解。',
    },
  )
}

function renderPricingCell(source: string, tiers: string[]) {
  const tierText = tiers.length ? tiers.join(', ') : '无阶梯'
  return renderTitleCell(source || '-', tierText)
}

function scopeLabel(scope: string) {
  if (scope === 'task_run') {
    return '任务'
  }
  if (scope === 'debug') {
    return '调试'
  }
  if (scope === 'agent_run') {
    return 'Run'
  }
  return '未绑定'
}

onMounted(() => {
  void loadReport()
})
</script>

<template>
  <n-space vertical :size="16">
    <n-card size="small">
      <div class="toolbar">
        <div>
          <h2>LLM 成本报表</h2>
          <p>按任务总账聚合模型调用、Token 与人民币成本，保留 Run 明细服务开发排查。</p>
        </div>
        <n-space>
          <n-select v-model:value="days" :options="dayOptions" filterable class="range-select" />
          <n-button :loading="loading" type="primary" @click="loadReport">刷新</n-button>
        </n-space>
      </div>
    </n-card>

    <n-alert v-if="loadError" type="error" :show-icon="false">{{ loadError }}</n-alert>
    <n-card v-if="loading && !report" size="small">
      <n-text depth="3">加载中…</n-text>
    </n-card>

    <n-alert v-if="!loading && report && !report.run_count" type="warning" :show-icon="false">
      当前时间范围内暂无 Agent Run 成本数据。新任务完成后会自动写入成本统计。
    </n-alert>

    <n-alert
      v-if="report && report.usage_missing_count"
      type="warning"
      :show-icon="false"
    >
      有 {{ formatNumber(report.usage_missing_count) }} 次模型调用未返回 usage；当前 Token
      与成本是已知下界，不代表这些调用成本为 0。
    </n-alert>

    <n-grid
      v-if="report && eventReport"
      cols="1 s:2 m:4"
      responsive="screen"
      :x-gap="16"
      :y-gap="12"
    >
      <n-gi>
        <n-card size="small">
          <n-statistic
            label="任务成本"
            :value="formatMoney(report.total_cost, report.currency)"
          />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small">
          <n-statistic
            label="全量调用成本"
            :value="formatMoney(eventReport.total_cost, eventReport.currency)"
          />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small">
          <n-statistic
            label="任务 / 调试调用"
            :value="`${formatNumber(eventReport.task_event_count)} / ${formatNumber(eventReport.debug_event_count)}`"
          />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small">
          <n-statistic
            label="缺少用量响应"
            :value="formatNumber(eventReport.usage_missing_count)"
          />
        </n-card>
      </n-gi>
    </n-grid>

    <n-grid v-if="report" cols="1 l:2" responsive="screen" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card title="成本构成" size="small">
          <div class="metric-list">
            <div>
              <span>输入</span>
              <strong>{{ formatMoney(report.input_cost, report.currency) }}</strong>
              <small>{{ formatNumber(report.input_tokens) }} tokens</small>
            </div>
            <div>
              <span>计费输入</span>
              <strong>{{ formatNumber(report.billable_input_tokens) }}</strong>
              <small>已扣除缓存命中 token</small>
            </div>
            <div>
              <span>输出</span>
              <strong>{{ formatMoney(report.output_cost, report.currency) }}</strong>
              <small>{{ formatNumber(report.output_tokens) }} tokens</small>
            </div>
            <div>
              <span>缓存命中</span>
              <strong>{{ formatMoney(report.cache_hit_cost, report.currency) }}</strong>
              <small>{{ formatNumber(report.cache_hit_tokens) }} tokens</small>
            </div>
            <div>
              <span>均次 Run</span>
              <strong>{{ formatMoney(report.average_run_cost, report.currency) }}</strong>
              <small>{{ formatMoney(report.average_call_cost, report.currency) }} / call</small>
            </div>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="运行状态" size="small">
          <div class="metric-list">
            <div>
              <span>完成 Run</span>
              <strong>{{ formatNumber(report.completed_run_count) }}</strong>
              <small>可用于核算有效成本</small>
            </div>
            <div>
              <span>失败 Run</span>
              <strong>{{ formatNumber(report.failed_run_count) }}</strong>
              <small>建议结合失败原因排查浪费</small>
            </div>
            <div v-if="eventReport">
              <span>全量调用</span>
              <strong>{{ formatNumber(eventReport.ai_calls) }}</strong>
              <small>{{ formatNumber(eventReport.total_tokens) }} tokens</small>
            </div>
            <div v-if="eventReport">
              <span>非任务调用</span>
              <strong>{{ formatNumber(eventReport.debug_event_count + eventReport.unbound_event_count) }}</strong>
              <small>DevUI / 调试 / 未绑定入口</small>
            </div>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <n-grid v-if="report" cols="1 l:2" responsive="screen" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card title="按天趋势" size="small">
          <div v-if="report.by_day.length" class="bar-list">
            <div v-for="bucket in report.by_day" :key="bucket.key" class="bar-row">
              <span>{{ bucket.key }}</span>
              <div class="bar-track"><div class="bar-fill" :style="{ width: bucketRatio(bucket) }"></div></div>
              <strong>{{ formatMoney(bucket.total_cost, report.currency) }}</strong>
            </div>
          </div>
          <n-text v-else depth="3">{{ loading ? '加载中…' : '暂无趋势数据' }}</n-text>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="按模型统计" size="small">
          <n-data-table
            :loading="loading"
            :columns="bucketColumns(report.currency)"
            :data="report.by_model"
            :pagination="false"
            size="small"
          />
        </n-card>
      </n-gi>
    </n-grid>

    <n-card v-if="report" title="按状态统计" size="small">
      <n-data-table
        :loading="loading"
        :columns="bucketColumns(report.currency)"
        :data="report.by_status"
        :pagination="false"
        size="small"
      />
    </n-card>

    <n-card v-if="report" title="近期任务成本" size="small">
      <n-data-table
        :columns="taskColumns"
        :data="report.task_items"
        :loading="loading"
        :pagination="{ pageSize: 10 }"
        :scroll-x="2080"
        size="small"
      />
    </n-card>

    <n-card v-if="report" title="近期 Run 明细（排查用）" size="small">
      <n-data-table
        :columns="runColumns"
        :data="report.recent_runs"
        :loading="loading"
        :pagination="{ pageSize: 10 }"
        :scroll-x="1850"
        size="small"
      />
    </n-card>

    <n-grid v-if="eventReport" cols="1 l:2" responsive="screen" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card title="按调用来源统计" size="small">
          <n-data-table
            :loading="loading"
            :columns="bucketColumns(eventReport.currency, '事件')"
            :data="eventReport.by_source"
            :pagination="{ pageSize: 6 }"
            size="small"
          />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="按调用归属统计" size="small">
          <n-data-table
            :loading="loading"
            :columns="bucketColumns(eventReport.currency, '事件')"
            :data="eventReport.by_scope"
            :pagination="false"
            size="small"
          />
        </n-card>
      </n-gi>
    </n-grid>

    <n-card v-if="eventReport" title="全量 LLM 调用流水（含 DevUI / 调试）" size="small">
      <n-data-table
        :columns="eventColumns"
        :data="eventReport.recent_events"
        :loading="loading"
        :pagination="{ pageSize: 10 }"
        :scroll-x="1860"
        size="small"
      />
    </n-card>
  </n-space>
</template>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}

.toolbar h2 {
  margin: 0 0 6px;
}

.toolbar p {
  margin: 0;
  color: #667085;
}

.range-select {
  width: 140px;
}

.metric-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.metric-list > div {
  padding: 14px;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  background: #fafafa;
}

.metric-list span,
.metric-list small {
  display: block;
  color: #667085;
}

.metric-list strong {
  display: block;
  margin: 6px 0;
  font-size: 20px;
}

.bar-list {
  display: grid;
  gap: 10px;
}

.bar-row {
  display: grid;
  grid-template-columns: 92px minmax(80px, 1fr) 120px;
  gap: 10px;
  align-items: center;
}

.bar-track {
  height: 10px;
  overflow: hidden;
  background: #edf2f7;
  border-radius: 999px;
}

.bar-fill {
  height: 100%;
  background: linear-gradient(90deg, #16a34a, #0ea5e9);
  border-radius: inherit;
}

.run-title {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.ellipsis-cell {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.title-text {
  color: #111827;
  font-weight: 700;
}

.muted-text {
  color: #667085;
  font-size: 12px;
}

.mono-text {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.metric-cell {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.metric-cell strong {
  color: #111827;
  font-weight: 700;
}

.metric-cell small {
  color: #667085;
  font-size: 12px;
  line-height: 1.35;
  white-space: normal;
}

.align-right {
  text-align: right;
}

@media (max-width: 720px) {
  .toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .metric-list {
    grid-template-columns: 1fr;
  }

  .bar-row {
    grid-template-columns: 84px 1fr;
  }

  .bar-row strong {
    grid-column: 2;
  }
}
</style>
