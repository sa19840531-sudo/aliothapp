<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { useMessage } from 'naive-ui'
import QuickStartCard from '@/modules/quick/components/QuickStartCard.vue'
import { getDashboardOverview } from '@/services/dashboard.api'
import { listReports } from '@/services/reports.api'
import { listTasks } from '@/services/tasks.api'
import type { WorkstationDashboardOverview } from '@/types/dashboard'
import type { WorkstationReportListItem } from '@/types/report'
import type { WorkstationTaskListItem } from '@/types/task'
import { reportStateLabel, reportStateOf, taskStatusLabel } from '@/utils/status'

/**
 * 部门工作台（对齐 2.1 原型）：问候 + 快速开始质检 + 今日概览 + 任务与队列 + 最近报告。
 * 数据范围由账号自动确定，页面里不选公司和部门。
 * 执行服务心跳和全站队列属于运维数据，后端只对平台管理员（systemOperations）开放，
 * 而管理类账号进不了部门平台，所以这里不请求系统状态接口。
 */
const nav = usePortalNav()
const message = useMessage()

const overview = ref<WorkstationDashboardOverview | null>(null)
const tasks = ref<WorkstationTaskListItem[]>([])
const reports = ref<WorkstationReportListItem[]>([])
const loading = ref(true)
const loadError = ref('')
/** 列表请求失败时面板说「加载失败」，不说「还没有…」 */
const tasksFailed = ref(false)
const reportsFailed = ref(false)
let pollTimer: number | undefined

const PLATFORM_LABELS: Record<string, string> = {
  h5: 'H5',
  web: 'Web',
  app: 'Android',
}

/** 原型问候语：6–12 点上午好，12–19 点下午好，其余时间晚上好。 */
const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour >= 6 && hour < 12) return '上午好'
  if (hour >= 12 && hour < 19) return '下午好'
  return '晚上好'
})

function statusCount(status: string): number {
  return overview.value?.status_distribution.find((item) => item.status === status)?.count ?? 0
}

const queuedCount = computed(() => statusCount('queued'))
const runningCount = computed(() => statusCount('running'))
const todayCount = computed(() => overview.value?.today_metrics.executed_tasks_today ?? 0)

/** 本月 Token（原型卡片）：按公司任务的模型用量汇总，36.8M 这种写法。 */
const tokenUsage = computed(() => overview.value?.token_usage ?? null)

function formatTokens(value: number) {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  return String(value)
}

const tokenUsageText = computed(() =>
  tokenUsage.value ? formatTokens(tokenUsage.value.total_tokens) : '—',
)
const tokenUsageHint = computed(() =>
  tokenUsage.value
    ? `${tokenUsage.value.task_count} 个任务 · 费用 ${tokenUsage.value.total_cost.toFixed(2)} ${tokenUsage.value.currency}`
    : '用量统计暂不可用',
)
const doneCount = computed(
  () => overview.value?.summary_cards.find((item) => item.key === 'done_tasks')?.value ?? 0,
)

/** 报告已生成即可在线查看（不代表每条规则都通过）。 */
function isViewable(item: WorkstationReportListItem) {
  return reportStateOf(item.report_status) === 'success'
}

const viewableReports = computed(() => reports.value.filter(isViewable))
const pendingReviewReportCount = computed(
  () => viewableReports.value.filter((item) => (item.pending_confirm_count ?? 0) > 0).length,
)

const queueHint = computed(() => (queuedCount.value ? '等待执行服务领取' : '当前没有排队任务'))

const runningHint = computed(() =>
  runningCount.value ? '进度见下方任务与队列' : '当前没有执行中任务',
)

const queueTasks = computed(() =>
  tasks.value.filter((item) => item.status === 'queued' || item.status === 'running').slice(0, 6),
)

const recentTasks = computed(() => tasks.value.slice(0, 8))

function platformLabel(platform: string) {
  return PLATFORM_LABELS[platform] ?? platform
}

/** 最近报告的一行摘要：通过率（有才显示）· 问题数 · 待确认数。 */
function reportSummary(item: WorkstationReportListItem) {
  const parts: string[] = []
  if (item.pass_rate != null) parts.push(`通过率 ${item.pass_rate}%`)
  const issues = item.issue_count ?? item.attention_required
  if (issues != null) parts.push(`${issues} 个问题`)
  if (item.pending_confirm_count) parts.push(`待确认 ${item.pending_confirm_count}`)
  return parts.join(' · ') || '已生成'
}

function taskProgressText(item: WorkstationTaskListItem) {
  if (item.status === 'running') return `${Math.round(item.progress ?? 0)}%`
  if (item.status === 'queued') return queueHint.value
  return item.failure_reason || '—'
}

async function load(silent = false) {
  if (!silent) loading.value = true
  const results = await Promise.allSettled([
    getDashboardOverview(),
    listTasks({ page: 1, page_size: 12 }),
    listReports({ page: 1, page_size: 8 }),
  ])
  const [overviewResult, tasksResult, reportsResult] = results
  if (overviewResult.status === 'fulfilled') overview.value = overviewResult.value
  if (tasksResult.status === 'fulfilled') tasks.value = tasksResult.value.items
  if (reportsResult.status === 'fulfilled') reports.value = reportsResult.value.items
  tasksFailed.value = tasksResult.status === 'rejected'
  reportsFailed.value = reportsResult.status === 'rejected'
  const failed = results.filter((item) => item.status === 'rejected')
  loadError.value =
    failed.length === results.length
      ? '工作台数据加载失败，请稍后刷新'
      : failed.length
        ? `${failed.length} 个模块加载失败，可稍后刷新`
        : ''
  loading.value = false
  scheduleNextPoll()
}

function scheduleNextPoll() {
  window.clearTimeout(pollTimer)
  const active = tasks.value.some((item) => item.status === 'queued' || item.status === 'running')
  if (active) {
    pollTimer = window.setTimeout(() => void load(true), 8000)
  }
}

function handleSubmitted(payload: { task_id: string; task_name: string }) {
  message.success(`「${payload.task_name}」已进入队列`)
  void load(true)
}

onMounted(() => void load())
onBeforeUnmount(() => window.clearTimeout(pollTimer))
</script>

<template>
  <div class="workstation-page dashboard-page">
    <header class="hero">
      <h1 data-testid="dashboard-greeting">{{ greeting }}</h1>
    </header>

    <p v-if="loadError" class="load-error" data-testid="dashboard-load-error">{{ loadError }}</p>

    <QuickStartCard embedded @submitted="handleSubmitted" />

    <section class="stat-grid" data-testid="stat-grid">
      <!-- 加载中数字先显示「…」，不让 0 和「当前没有…」看起来像最终结果 -->
      <article class="stat-card">
        <span>今日任务</span>
        <strong>{{ loading ? '…' : todayCount }}</strong>
        <em>{{ loading ? '加载中…' : `累计完成 ${doneCount}` }}</em>
      </article>
      <article class="stat-card">
        <span>排队中</span>
        <strong class="tone-warn">{{ loading ? '…' : queuedCount }}</strong>
        <em>{{ loading ? '加载中…' : queueHint }}</em>
      </article>
      <article class="stat-card">
        <span>执行中</span>
        <strong class="tone-primary">{{ loading ? '…' : runningCount }}</strong>
        <em>{{ loading ? '加载中…' : runningHint }}</em>
      </article>
      <article class="stat-card">
        <span>待查看报告</span>
        <strong>{{ loading ? '…' : viewableReports.length }}</strong>
        <em>{{ loading ? '加载中…' : pendingReviewReportCount ? `其中 ${pendingReviewReportCount} 份待人工确认` : '已生成的报告均可在线查看' }}</em>
      </article>
      <article class="stat-card">
        <span>本月 Token</span>
        <strong>{{ loading ? '…' : tokenUsageText }}</strong>
        <em>{{ loading ? '加载中…' : tokenUsageHint }}</em>
      </article>
    </section>

    <div class="lower-grid">
      <section class="workstation-glass-panel panel" data-testid="queue-panel">
        <header class="panel-head">
          <strong>任务与队列</strong>
          <button type="button" class="link-btn" @click="nav.push('/tasks')">查看全部</button>
        </header>
        <p v-if="loading" class="empty">加载中…</p>
        <p v-else-if="tasksFailed && !recentTasks.length" class="empty">任务列表加载失败，可稍后刷新</p>
        <p v-else-if="!recentTasks.length" class="empty">还没有任务，先在上方快速开始一个质检。</p>
        <table v-else class="list-table">
          <thead>
            <tr>
              <th>任务</th>
              <th>平台</th>
              <th>状态</th>
              <th>排队 / 进度</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in queueTasks.length ? queueTasks : recentTasks"
              :key="item.task_id"
              class="row-link"
              @click="nav.push(`/tasks/${item.task_id}`)"
            >
              <td class="name">{{ item.task_name }}</td>
              <td><span class="chip">{{ platformLabel(item.target_platform) }}</span></td>
              <td><span class="status" :class="`status-${item.status}`">{{ taskStatusLabel(item.status) }}</span></td>
              <td class="muted-cell">{{ taskProgressText(item) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="workstation-glass-panel panel" data-testid="reports-panel">
        <header class="panel-head">
          <strong>最近报告</strong>
          <button type="button" class="link-btn" @click="nav.push('/reports')">查看全部</button>
        </header>
        <p v-if="loading" class="empty">加载中…</p>
        <p v-else-if="reportsFailed && !reports.length" class="empty">报告列表加载失败，可稍后刷新</p>
        <p v-else-if="!reports.length" class="empty">还没有报告</p>
        <ul v-else class="report-list">
          <li v-for="item in reports.slice(0, 5)" :key="item.task_id">
            <div>
              <strong>{{ item.task_name }}</strong>
              <span>{{ isViewable(item) ? reportSummary(item) : reportStateLabel(item.report_status) }}</span>
            </div>
            <button
              v-if="isViewable(item)"
              type="button"
              class="link-btn"
              @click="nav.push(`/reports/${item.task_id}`)"
            >
              在线查看
            </button>
            <span v-else class="muted-cell">稍后查看</span>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dashboard-page {
  gap: 16px;
}

.hero h1 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 20px;
  font-weight: 600;
}

.load-error {
  margin: 0;
  padding: 10px 14px;
  border: 1px solid rgba(220, 38, 38, 0.3);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-danger);
  background: var(--tint-danger);
  font-size: 13px;
}

/* 五张统计卡：bg-white rounded-xl border p-4；数字 text-2xl font-semibold */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.stat-card {
  display: flex;
  flex-direction: column;
  padding: 16px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel);
}

.stat-card > span {
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.stat-card > strong {
  margin-top: 4px;
  color: var(--workstation-text-strong);
  font-size: 24px;
  font-weight: 600;
  line-height: 1.3;
}

.stat-card.muted > strong {
  font-size: 20px;
}

.stat-card > em {
  margin-top: 4px;
  color: var(--workstation-text-faint);
  font-size: 12px;
  font-style: normal;
}

.tone-warn {
  color: var(--workstation-warning) !important;
}

.tone-primary {
  color: var(--workstation-primary) !important;
}

.lower-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.panel {
  overflow: hidden;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--workstation-border);
}

.panel-head strong {
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 600;
}

.link-btn {
  border: 0;
  padding: 0;
  color: var(--workstation-primary);
  background: transparent;
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}

.empty {
  margin: 0;
  padding: 24px 20px;
  color: var(--workstation-text-muted);
  font-size: 14px;
}

.list-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.list-table thead {
  background: var(--workstation-table-head-bg);
  color: var(--workstation-text-muted);
}

.list-table th {
  padding: 12px 20px;
  font-weight: 500;
  text-align: left;
}

.list-table td {
  padding: 12px 20px;
  border-top: 1px solid var(--workstation-border);
  color: var(--workstation-text);
}

.row-link {
  cursor: pointer;
}

.row-link:hover td {
  background: var(--workstation-table-hover-bg);
}

.name {
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.chip {
  padding: 2px 8px;
  border-radius: 4px;
  color: var(--workstation-text-muted);
  background: var(--tint-neutral);
  font-size: 12px;
}

.status {
  font-weight: 500;
}

.status-running {
  color: var(--workstation-primary);
}

.status-queued {
  color: var(--workstation-warning);
}

.status-done,
.status-review {
  color: var(--workstation-success);
}

.status-failed,
.status-canceled,
.status-canceling {
  color: var(--workstation-text-muted);
}

.muted-cell {
  color: var(--workstation-text-muted);
}

.report-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.report-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 20px;
  border-top: 1px solid var(--workstation-border);
}

.report-list li:first-child {
  border-top: 0;
}

.report-list strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 500;
}

.report-list span {
  display: block;
  margin-top: 4px;
  color: var(--workstation-text-faint);
  font-size: 12px;
}

@media (max-width: 1080px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .lower-grid {
    grid-template-columns: 1fr;
  }
}
</style>
