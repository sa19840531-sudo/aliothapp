<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BarChart from '@/components/charts/BarChart.vue'
import DonutChart from '@/components/charts/DonutChart.vue'
import LineChart from '@/components/charts/LineChart.vue'
import { getOperationsAnalytics, type OperationsOverview, type OperationsRange } from '../api/operations'
import { listTenants, type TenantItem } from '../api/tenants'
import SearchSelect from '@/components/pickers/SearchSelect.vue'

/**
 * 使用分析（对齐 2.1 原型 analytics.html）：只对运营中心开放。
 * 三个入口共用一页：公司使用概览 / 任务和报告统计（?tab=task）/ 活跃账号分析（?tab=account）。
 */
const route = useRoute()
const router = useRouter()

type TabKey = 'overview' | 'task' | 'account'
const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: '公司使用概览' },
  { key: 'task', label: '任务和报告统计' },
  { key: 'account', label: '活跃账号分析' },
]
const activeTab = computed<TabKey>(() => {
  const tab = route.query.tab?.toString()
  return tab === 'task' || tab === 'account' ? tab : 'overview'
})

function switchTab(key: TabKey) {
  const query = { ...route.query }
  if (key === 'overview') delete query.tab
  else query.tab = key
  void router.replace({ path: route.path, query })
}

const RANGES: Array<{ value: OperationsRange; label: string }> = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: '90d', label: '近 90 天' },
]
const PLATFORM_LABELS: Record<string, string> = { h5: 'H5', web: 'Web', app: 'Android', ios: 'iOS' }

const filters = reactive({
  range: '7d' as OperationsRange,
  tenant_id: route.query.tenant_id?.toString() ?? '',
  platform: '',
})
const tenants = ref<TenantItem[]>([])
const data = ref<OperationsOverview | null>(null)
const loading = ref(true)
const loadError = ref('')

async function load() {
  loading.value = true
  try {
    data.value = await getOperationsAnalytics({
      range: filters.range,
      tenant_id: filters.tenant_id || undefined,
      platform: filters.platform || undefined,
    })
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '使用分析加载失败'
  } finally {
    loading.value = false
  }
}

function setRange(value: OperationsRange) {
  filters.range = value
  void load()
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
  if (!seconds) return '0m'
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
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

/** 较上期变化：上期为 0 时没法算比例，直接说明。 */
function delta(current: number, previous: number): { text: string; tone: string } {
  if (!previous) return { text: previous === current ? '与上期持平' : '上期无数据', tone: '' }
  const percent = Math.round(((current - previous) / previous) * 1000) / 10
  if (percent > 0) return { text: `较上期 +${percent}%`, tone: 'is-up' }
  if (percent < 0) return { text: `较上期 ${percent}%`, tone: 'is-down' }
  return { text: '与上期持平', tone: '' }
}

const kpis = computed(() => {
  const summary = data.value?.summary
  const previous = data.value?.previous
  if (!summary || !previous) return []
  return [
    { label: '质检任务总数', value: formatNumber(summary.task_count), hint: delta(summary.task_count, previous.task_count) },
    { label: '已生成报告数', value: formatNumber(summary.report_count), hint: delta(summary.report_count, previous.report_count) },
    { label: '报告生成成功率', value: formatPercent(summary.report_success_rate), hint: { text: '目标 ≥ 95%', tone: '' } },
    { label: '活跃分公司数', value: formatNumber(summary.active_tenant_count), hint: { text: `共 ${data.value?.tenants.length ?? 0} 家分公司`, tone: '' } },
    { label: '活跃账号数', value: formatNumber(summary.active_account_count), hint: delta(summary.active_account_count, previous.active_account_count) },
    { label: '平均执行时长', value: formatDuration(summary.avg_duration_seconds), hint: { text: '任务平均耗时', tone: '' } },
  ]
})

const trendLabels = computed(() => data.value?.trend.map((point) => point.date) ?? [])
const taskSeries = computed(() => [
  { name: '任务数', color: '#2563eb', values: data.value?.trend.map((point) => point.task_count) ?? [] },
  { name: '报告数', color: '#059669', values: data.value?.trend.map((point) => point.report_count) ?? [] },
])
const reportSeries = computed(() => [
  { name: '报告数', color: '#059669', values: data.value?.trend.map((point) => point.report_count) ?? [] },
  { name: '生成失败', color: '#dc2626', values: data.value?.trend.map((point) => point.failed_count) ?? [] },
])

const PLATFORM_COLORS: Record<string, string> = { h5: '#2563eb', web: '#0891b2', app: '#059669', ios: '#7c3aed' }
const platformItems = computed(() =>
  (data.value?.platform_share ?? []).map((item) => ({
    label: PLATFORM_LABELS[item.platform] ?? item.platform,
    value: item.task_count,
    color: PLATFORM_COLORS[item.platform] ?? '#94a3b8',
  })),
)

const outcomeItems = computed(() => {
  const summary = data.value?.summary
  if (!summary) return []
  const other = Math.max(0, summary.task_count - summary.done_count - summary.failed_count - summary.review_count)
  return [
    { label: '成功', value: summary.done_count, color: '#059669' },
    { label: '失败', value: summary.failed_count, color: '#dc2626' },
    { label: '待复核', value: summary.review_count, color: '#d97706' },
    { label: '进行中 / 其他', value: other, color: '#94a3b8' },
  ]
})

const rankItems = computed(() =>
  [...(data.value?.tenants ?? [])]
    .sort((a, b) => b.task_count - a.task_count)
    .slice(0, 10)
    .map((row) => ({ label: row.tenant_name, value: row.task_count, hint: `${row.report_count} 份报告` })),
)

const accountItems = computed(() =>
  [...(data.value?.tenants ?? [])]
    .sort((a, b) => b.active_account_count - a.active_account_count)
    .map((row) => ({ label: row.tenant_name, value: row.active_account_count, color: '#7c3aed' })),
)

onMounted(async () => {
  // 公司列表只给筛选下拉用，和数据并行拉，不拖慢首屏
  const tenantsRequest = listTenants()
    .then((items) => {
      tenants.value = items
    })
    .catch(() => {
      tenants.value = []
    })
  await Promise.all([load(), tenantsRequest])
})

watch(
  () => route.query.tenant_id?.toString() ?? '',
  (tenantId) => {
    if (tenantId !== filters.tenant_id) {
      filters.tenant_id = tenantId
      void load()
    }
  },
)
</script>

<template>
  <div class="ap-page" data-testid="admin-analytics">
    <div class="workstation-glass-panel ap-tabs" data-testid="analytics-tabs">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        class="ap-tab"
        :class="{ 'is-active': activeTab === tab.key }"
        :data-testid="`analytics-tab-${tab.key}`"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <section class="workstation-glass-panel ap-filter">
      <div class="ap-filter-grid">
        <div class="ap-field">
          <label>时间范围</label>
          <div class="ap-range" data-testid="analytics-range">
            <button
              v-for="item in RANGES"
              :key="item.value"
              type="button"
              :class="{ 'is-active': filters.range === item.value }"
              @click="setRange(item.value)"
            >
              {{ item.label }}
            </button>
          </div>
        </div>
        <div class="ap-field">
          <label>公司</label>
          <SearchSelect v-model="filters.tenant_id" data-testid="analytics-tenant">
            <option value="">全部公司</option>
            <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>平台</label>
          <SearchSelect v-model="filters.platform">
            <option value="">全部平台</option>
            <option value="h5">H5</option>
            <option value="web">Web</option>
            <option value="app">Android</option>
            <option value="ios">iOS</option>
          </SearchSelect>
        </div>
        <div class="ap-filter-actions">
          <button type="button" class="qc-btn qc-btn-primary" @click="load">查询</button>
          <button type="button" class="qc-btn" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <p v-if="loadError" class="qc-error" data-testid="analytics-error">{{ loadError }}</p>

    <section v-if="activeTab !== 'account'" class="ap-kpis" data-testid="analytics-kpis">
      <div v-if="loading && !kpis.length" class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">加载中…</div>
      </div>
      <div v-for="kpi in kpis" :key="kpi.label" class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">{{ kpi.label }}</div>
        <div class="ap-kpi-value">{{ kpi.value }}</div>
        <div class="ap-kpi-hint" :class="kpi.hint.tone">{{ kpi.hint.text }}</div>
      </div>
    </section>

    <template v-if="activeTab === 'overview'">
      <section class="ap-charts cols-2-1">
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>任务趋势</h3><span>按日统计</span></div>
          <LineChart :loading="loading" :labels="trendLabels" :series="taskSeries" :height="256" />
        </div>
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>平台类型使用占比</h3></div>
          <DonutChart :loading="loading" :items="platformItems" center-label="任务" />
        </div>
      </section>
      <section class="ap-charts cols-2">
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>报告生成趋势</h3></div>
          <LineChart :loading="loading" :labels="trendLabels" :series="reportSeries" :height="224" />
        </div>
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>分公司使用排名</h3><span>按任务数</span></div>
          <BarChart :loading="loading" :items="rankItems" unit=" 个任务" />
        </div>
      </section>
    </template>

    <template v-else-if="activeTab === 'task'">
      <section class="ap-charts cols-2-1">
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>任务与报告趋势</h3><span>按日统计</span></div>
          <LineChart :loading="loading" :labels="trendLabels" :series="reportSeries" :height="256" />
        </div>
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>任务结果分布</h3></div>
          <DonutChart :loading="loading" :items="outcomeItems" center-label="任务" />
        </div>
      </section>
      <section class="workstation-glass-panel" data-testid="analytics-task-table">
        <div class="qc-card-head"><h3>分公司任务与报告</h3></div>
        <div class="ap-table-wrap">
          <table class="qc-table">
            <thead>
              <tr>
                <th>公司</th>
                <th>任务数</th>
                <th>报告数</th>
                <th>报告成功率</th>
                <th>异常率</th>
                <th>产品数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!data?.tenants.length"><td colspan="6" class="qc-empty">{{ loading ? '加载中…' : '暂无数据' }}</td></tr>
              <tr v-for="row in data?.tenants ?? []" :key="row.tenant_id">
                <td><strong>{{ row.tenant_name }}</strong></td>
                <td>{{ row.task_count }}</td>
                <td>{{ row.report_count }}</td>
                <td>{{ formatPercent(row.report_success_rate) }}</td>
                <td>{{ formatPercent(row.failure_rate) }}</td>
                <td>{{ row.product_count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="ap-kpis">
        <div class="workstation-glass-panel ap-kpi">
          <div class="ap-kpi-label">活跃账号数</div>
          <div class="ap-kpi-value">{{ loading && !data ? '…' : formatNumber(data?.summary.active_account_count) }}</div>
          <div class="ap-kpi-hint">统计周期内登录过或发起过任务</div>
        </div>
        <div class="workstation-glass-panel ap-kpi">
          <div class="ap-kpi-label">活跃分公司数</div>
          <div class="ap-kpi-value is-primary">{{ loading && !data ? '…' : formatNumber(data?.summary.active_tenant_count) }}</div>
          <div class="ap-kpi-hint">统计周期内有任务的公司</div>
        </div>
        <div class="workstation-glass-panel ap-kpi">
          <div class="ap-kpi-label">上期活跃账号</div>
          <div class="ap-kpi-value">{{ loading && !data ? '…' : formatNumber(data?.previous.active_account_count) }}</div>
          <div v-if="data" class="ap-kpi-hint" :class="delta(data?.summary.active_account_count ?? 0, data?.previous.active_account_count ?? 0).tone">
            {{ delta(data?.summary.active_account_count ?? 0, data?.previous.active_account_count ?? 0).text }}
          </div>
        </div>
      </section>
      <section class="ap-charts cols-2">
        <div class="workstation-glass-panel ap-chart">
          <div class="ap-chart-head"><h3>各分公司活跃账号</h3></div>
          <BarChart :loading="loading" :items="accountItems" unit=" 个" />
        </div>
        <div class="workstation-glass-panel" data-testid="analytics-account-table">
          <div class="qc-card-head"><h3>分公司使用状态</h3></div>
          <div class="ap-table-wrap">
            <table class="qc-table">
              <thead>
                <tr>
                  <th>公司</th>
                  <th>活跃账号</th>
                  <th>最近使用时间</th>
                  <th>使用状态</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!data?.tenants.length"><td colspan="4" class="qc-empty">{{ loading ? '加载中…' : '暂无数据' }}</td></tr>
                <tr v-for="row in data?.tenants ?? []" :key="row.tenant_id">
                  <td>{{ row.tenant_name }}</td>
                  <td>{{ row.active_account_count }}</td>
                  <td class="qc-muted">{{ formatDate(row.last_used_at) }}</td>
                  <td>
                    <span class="qc-chip" :class="row.usage_status === 'active' ? 'tone-success' : 'tone-muted'">
                      {{ row.usage_status === 'active' ? '活跃' : '沉默' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>
