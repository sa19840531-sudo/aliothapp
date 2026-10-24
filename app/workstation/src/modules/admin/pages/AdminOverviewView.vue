<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import BarChart from '@/components/charts/BarChart.vue'
import DonutChart from '@/components/charts/DonutChart.vue'
import LineChart from '@/components/charts/LineChart.vue'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { authStore } from '@/stores/auth'
import { getOperationsOverview, type OperationsOverview, type OperationsRange } from '../api/operations'
import { listTenants, type TenantItem } from '../api/tenants'

/**
 * 运营中心工作台（对齐 2.1 原型 index.html）：筛选卡 → 六个指标卡 → 四张趋势/分布图 → 分公司汇总表。
 * 只展示汇总数字；运营中心看全部公司，分公司管理员只看本公司（服务端按登录态收口）。
 */
const router = useRouter()
const message = useMessage()

const RANGE_OPTIONS: Array<{ value: OperationsRange; label: string }> = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: 'quarter', label: '本季度' },
  { value: 'year', label: '本年度' },
]
const PLATFORM_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'app', label: 'Android' },
  { value: 'ios', label: 'iOS' },
  { value: 'web', label: 'Web' },
  { value: 'h5', label: 'H5' },
]

const permissions = computed(() => new Set(authStore.user.value?.permissions ?? []))
const canPickCompany = computed(() => permissions.value.has('taskObjects:read:any'))
const workspaceName = computed(() => authStore.user.value?.tenant_name || '本公司')

const filters = reactive({ range: '7d' as OperationsRange, tenant_id: '', platform: '' })
const tenants = ref<TenantItem[]>([])
const overview = ref<OperationsOverview | null>(null)
const loading = ref(true)
const loadError = ref('')

async function load() {
  loading.value = true
  try {
    overview.value = await getOperationsOverview({
      range: filters.range,
      tenant_id: filters.tenant_id || undefined,
      platform: filters.platform || undefined,
    })
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '运营总览加载失败'
  } finally {
    loading.value = false
  }
}

async function loadTenants() {
  if (!permissions.value.has('tenants')) return
  try {
    tenants.value = await listTenants()
  } catch {
    tenants.value = []
  }
}

function resetFilters() {
  filters.range = '7d'
  filters.tenant_id = ''
  filters.platform = ''
  void load()
}

function formatNumber(value: number | null | undefined) {
  return (value ?? 0).toLocaleString('zh-CN')
}

function formatPercent(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `${value}%`
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '—'
  const totalSeconds = Math.round(seconds)
  const minutes = Math.floor(totalSeconds / 60)
  const rest = totalSeconds % 60
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

const rangeLabel = computed(() => RANGE_OPTIONS.find((item) => item.value === filters.range)?.label ?? '')

const kpis = computed(() => {
  const summary = overview.value?.summary
  return [
    { label: '质检任务总数', value: formatNumber(summary?.task_count), tone: '' },
    { label: '已生成报告数', value: formatNumber(summary?.report_count), tone: '' },
    { label: '报告生成成功率', value: formatPercent(summary?.report_success_rate), tone: 'is-success' },
    { label: '活跃分公司数', value: formatNumber(summary?.active_tenant_count), tone: 'is-primary' },
    { label: '活跃账号数', value: formatNumber(summary?.active_account_count), tone: '' },
    {
      label: '平均执行时长',
      value: formatDuration(summary?.avg_duration_seconds),
      tone: '',
      title: '统计已生成报告的任务（已完成、待复核）的执行起止时间，不含排队；失败、取消及无有效时间的任务不计入',
    },
  ]
})

const trendLabels = computed(() => overview.value?.trend.map((point) => point.date) ?? [])
const taskTrend = computed(() => [
  { name: '任务数', color: '#2563eb', values: overview.value?.trend.map((point) => point.task_count) ?? [] },
])
const reportTrend = computed(() => [
  { name: '报告数', color: '#059669', values: overview.value?.trend.map((point) => point.report_count) ?? [] },
  { name: '生成失败', color: '#dc2626', values: overview.value?.trend.map((point) => point.failed_count) ?? [] },
])

const companyBars = computed(() =>
  [...(overview.value?.tenants ?? [])]
    .sort((a, b) => b.report_count - a.report_count)
    .slice(0, 10)
    .map((row) => ({ label: row.tenant_name, value: row.report_count, hint: `${row.task_count} 个任务` })),
)

const outcomeItems = computed(() => {
  const summary = overview.value?.summary
  if (!summary) return []
  const other = Math.max(0, summary.task_count - summary.done_count - summary.failed_count - summary.review_count)
  return [
    { label: '成功', value: summary.done_count, color: '#059669' },
    { label: '失败', value: summary.failed_count, color: '#dc2626' },
    { label: '待复核', value: summary.review_count, color: '#d97706' },
    { label: '进行中 / 其他', value: other, color: '#94a3b8' },
  ]
})

function usageLabel(status: 'active' | 'idle') {
  return status === 'active' ? '活跃' : '沉默'
}

function viewCompany(tenantId: string) {
  if (permissions.value.has('analytics')) {
    void router.push({ path: '/admin/analytics', query: { tenant_id: tenantId } })
    return
  }
  void router.push('/admin/tasks')
}

function exportTable() {
  const rows = overview.value?.tenants ?? []
  if (!rows.length) {
    message.warning('没有可导出的数据')
    return
  }
  const header = ['公司名称', '产品数量', '活跃账号', '任务数量', '报告数量', '报告成功率', '异常率', '最近使用时间', '使用状态']
  const lines = rows.map((row) =>
    [
      row.tenant_name,
      row.product_count,
      row.active_account_count,
      row.task_count,
      row.report_count,
      formatPercent(row.report_success_rate),
      formatPercent(row.failure_rate),
      formatDate(row.last_used_at),
      usageLabel(row.usage_status),
    ].join(','),
  )
  const blob = new Blob([`﻿${[header.join(','), ...lines].join('\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `分公司汇总-${filters.range}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

onMounted(() => {
  void loadTenants()
  void load()
})
</script>

<template>
  <div class="ap-page" data-testid="admin-overview">
    <section class="workstation-glass-panel ap-filter">
      <div class="ap-filter-grid">
        <div class="ap-field">
          <label>时间范围</label>
          <SearchSelect v-model="filters.range" data-testid="overview-range">
            <option v-for="item in RANGE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>分公司</label>
          <SearchSelect v-if="canPickCompany" v-model="filters.tenant_id" data-testid="overview-tenant">
            <option value="">全部</option>
            <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
          </SearchSelect>
          <SearchSelect v-else disabled>
            <option>{{ workspaceName }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>平台类型</label>
          <SearchSelect v-model="filters.platform" data-testid="overview-platform">
            <option v-for="item in PLATFORM_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
          </SearchSelect>
        </div>
        <div class="ap-filter-actions">
          <button type="button" class="qc-btn qc-btn-primary" data-testid="overview-search" @click="load">查询</button>
          <button type="button" class="qc-btn" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <p v-if="loadError" class="qc-error" data-testid="overview-error">{{ loadError }}</p>

    <section class="ap-kpis" data-testid="overview-kpis">
      <div v-for="kpi in kpis" :key="kpi.label" class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label" :title="kpi.title">{{ kpi.label }}</div>
        <div class="ap-kpi-value" :class="kpi.tone">{{ loading && !overview ? '…' : kpi.value }}</div>
      </div>
    </section>

    <section class="ap-charts cols-2">
      <div class="workstation-glass-panel ap-chart">
        <div class="ap-chart-head"><h3>任务数量趋势</h3><span>{{ rangeLabel }}</span></div>
        <LineChart :loading="loading" :labels="trendLabels" :series="taskTrend" :height="260" />
      </div>
      <div class="workstation-glass-panel ap-chart">
        <div class="ap-chart-head"><h3>报告生成趋势</h3><span>{{ rangeLabel }}</span></div>
        <LineChart :loading="loading" :labels="trendLabels" :series="reportTrend" :height="260" />
      </div>
    </section>

    <section class="ap-charts cols-2-1">
      <div class="workstation-glass-panel ap-chart">
        <div class="ap-chart-head"><h3>各分公司报告数量对比</h3><span>TOP10</span></div>
        <BarChart :loading="loading" :items="companyBars" unit=" 份" />
      </div>
      <div class="workstation-glass-panel ap-chart">
        <div class="ap-chart-head"><h3>任务成功 / 失败 / 待复核分布</h3></div>
        <DonutChart :loading="loading" :items="outcomeItems" center-label="任务" />
      </div>
    </section>

    <section class="workstation-glass-panel" data-testid="overview-tenants">
      <div class="qc-card-head">
        <h3>分公司汇总</h3>
        <button type="button" class="qc-btn qc-btn-sm" @click="exportTable">导出</button>
      </div>
      <div class="ap-table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th>公司名称</th>
              <th>产品数量</th>
              <th>活跃账号</th>
              <th>任务数量</th>
              <th>报告数量</th>
              <th>报告成功率</th>
              <th>异常率</th>
              <th>最近使用时间</th>
              <th>使用状态</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!overview?.tenants.length">
              <td colspan="10" class="qc-empty">{{ loading ? '加载中…' : '暂无公司数据' }}</td>
            </tr>
            <tr v-for="row in overview?.tenants ?? []" :key="row.tenant_id">
              <td><strong>{{ row.tenant_name }}</strong></td>
              <td>{{ row.product_count }}</td>
              <td>{{ row.active_account_count }}</td>
              <td>{{ row.task_count }}</td>
              <td>{{ row.report_count }}</td>
              <td>{{ formatPercent(row.report_success_rate) }}</td>
              <td>{{ formatPercent(row.failure_rate) }}</td>
              <td class="qc-muted">{{ formatDate(row.last_used_at) }}</td>
              <td>
                <span class="qc-chip" :class="row.usage_status === 'active' ? 'tone-success' : 'tone-muted'">
                  {{ usageLabel(row.usage_status) }}
                </span>
              </td>
              <td class="is-right">
                <button type="button" class="qc-link" @click="viewCompany(row.tenant_id)">查看</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
