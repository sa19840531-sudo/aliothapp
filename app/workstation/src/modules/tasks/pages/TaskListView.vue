<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { isNavigationFailure, NavigationFailureType, onBeforeRouteLeave, useRoute, useRouter, type RouteLocationNormalized } from 'vue-router'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { NPopconfirm, useMessage } from 'naive-ui'
import { deleteTask, listTasks, performTaskOperation } from '@/services/tasks.api'
import { getSystemStatus, type SystemStatus } from '@/services/system.api'
import { listWorkstationProducts, type WorkstationProductOption } from '@/services/products.api'
import { downloadScreenshotsArchive } from '@/services/task-insight.api'
import { authStore } from '@/stores/auth'
import type { ListTasksParams, TaskStatus, WorkstationTaskListItem } from '@/types/task'
import { reportStateOf, taskStatusLabel } from '@/utils/status'
import { readTaskListState, taskListQuery } from '../taskListState'
import { isLongExecution } from '../taskRuntime'

/**
 * 任务中心 · 任务列表（对齐 2.0 原型 task.html）：筛选卡 → 队列/服务卡片 → 列表卡
 * （创建任务 / 批量操作 / 表格 / 分页）。所属公司锁定为登录账号；部门与平台由服务端筛选。
 * 部门平台（2.1 原型）不展示公司 / 部门筛选和列，组织范围由登录身份确定；产品按下拉精确筛选。
 * 执行服务心跳和全站队列属于运维数据，后端只对平台管理员（systemOperations）开放：
 * 读不到的账号不请求，也不显示只靠它的「预计等待」「服务状态」两张卡。
 */
const nav = usePortalNav()
const route = useRoute()
const router = useRouter()
const listPath = route.path
const initialState = readTaskListState(route.query)
const message = useMessage()
const selectedIds = ref<string[]>([])

const workerOffline = ref(false)
const systemStatus = ref<SystemStatus | null>(null)
const rows = ref<WorkstationTaskListItem[]>([])
const total = ref(0)
const page = ref(initialState.page)
const pageSize = ref(initialState.pageSize)
const loading = ref(true)
const loadError = ref('')
const operatingTaskId = ref('')

const workspaceName = computed(() => authStore.user.value?.tenant_name || '本公司')
const canReadSystemStatus = computed(
  () => authStore.user.value?.permissions.includes('systemOperations') ?? false,
)
const showOrg = computed(() => nav.isAdmin.value)
const columnCount = computed(() => (showOrg.value ? 12 : 10))
const productOptions = ref<WorkstationProductOption[]>([])
/** 产品下拉的选项还在加载：下拉里放一条禁用的「产品加载中…」 */
const productsLoading = ref(true)

const filters = reactive(initialState.filters)

let pollTimer: number | undefined
let filterTimer: number | undefined
let systemStatusLoading = false
let active = true
let requestVersion = 0
let leavingFor: RouteLocationNormalized | null = null

function clearRefreshTimers() {
  window.clearTimeout(pollTimer)
  window.clearTimeout(filterTimer)
}

// 离开守卫早于详情懒加载；仅在卸载时停刷新已来不及，旧筛选会取消用户导航。
onBeforeRouteLeave((to) => {
  leavingFor = to
  requestVersion += 1
  clearRefreshTimers()
})
function resumeList(destination: RouteLocationNormalized) {
  if (!active || leavingFor !== destination || route.path !== listPath) return false
  leavingFor = null
  void fetchTasks()
  return true
}
const removeAfterEach = router.afterEach((to, _, failure) => {
  // 被更新导航取代的旧失败不能恢复旧列表，只有当前被拒绝的离开意图可恢复。
  if (failure && !isNavigationFailure(failure, NavigationFailureType.cancelled)) resumeList(to)
})
const removeOnError = router.onError((_, to) => {
  if (resumeList(to)) message.error('页面加载失败，未离开任务列表，请稍后重试')
})
function navigate(target: Parameters<typeof nav.push>[0]) {
  // 路由错误由上述作用域内监听明确提示；不重载、不自动重试，也不遗留未处理拒绝。
  void Promise.resolve(nav.push(target)).catch(() => {})
}

function buildParams(): ListTasksParams {
  return {
    task_name: filters.taskName.trim() || undefined,
    product_code: filters.productCode || undefined,
    department_name: filters.department.trim() || undefined,
    target_platform: filters.platform || undefined,
    submitter_name: filters.submitterName.trim() || undefined,
    status: filters.status === 'all' ? undefined : filters.status,
    created_from: filters.createdStart ? `${filters.createdStart}T00:00:00` : undefined,
    created_to: filters.createdEnd ? `${filters.createdEnd}T23:59:59` : undefined,
    page: page.value,
    page_size: pageSize.value,
  }
}

async function loadSystemStatus() {
  if (!canReadSystemStatus.value || systemStatusLoading) return
  systemStatusLoading = true
  try {
    const status = await getSystemStatus()
    if (!active) return
    systemStatus.value = status ?? null
    workerOffline.value = status ? !status.worker.alive : false
  } catch {
    if (!active) return
    systemStatus.value = null
    workerOffline.value = false
  } finally {
    systemStatusLoading = false
  }
}

async function fetchTasks(silent = false) {
  if (!active || leavingFor) return
  const version = ++requestVersion
  const current = () => active && !leavingFor && version === requestVersion
  window.clearTimeout(pollTimer)
  if (!silent) {
    loading.value = true
    void nav.replace({ path: '/tasks', query: taskListQuery(filters, page.value, pageSize.value) })
  }
  // 服务心跳独立刷新，运维接口延迟或失败不能阻塞任务列表。
  void loadSystemStatus()
  try {
    const result = await listTasks(buildParams())
    if (!current()) return
    rows.value = result.items
    total.value = result.total
    loadError.value = ''
    for (const row of result.items) {
      const department = row.product.department_name
      if (department && !knownDepartments.value.includes(department)) {
        knownDepartments.value.push(department)
      }
    }
  } catch (error) {
    if (!current()) return
    loadError.value = error instanceof Error ? error.message : '任务列表加载失败'
  } finally {
    if (current()) {
      loading.value = false
      scheduleNextPoll()
    }
  }
}

function scheduleNextPoll() {
  window.clearTimeout(pollTimer)
  if (!active || leavingFor) return
  const hasActiveTask = rows.value.some((row) => row.status === 'queued' || row.status === 'running')
  if (!loadError.value && hasActiveTask) {
    pollTimer = window.setTimeout(() => void fetchTasks(true), 8000)
  }
}

async function loadProductOptions() {
  try {
    const result = await listWorkstationProducts()
    if (active) productOptions.value = result.items
  } catch {
    if (active) productOptions.value = []
  } finally {
    if (active) productsLoading.value = false
  }
}

onMounted(() => {
  void fetchTasks()
  void loadProductOptions()
})
onBeforeUnmount(() => {
  active = false
  requestVersion += 1
  clearRefreshTimers()
  removeAfterEach()
  removeOnError()
})

watch(
  () => [
    filters.taskName,
    filters.productCode,
    filters.department,
    filters.platform,
    filters.submitterName,
    filters.status,
    filters.createdStart,
    filters.createdEnd,
    pageSize.value,
  ],
  () => {
    window.clearTimeout(filterTimer)
    if (!active || leavingFor) return
    filterTimer = window.setTimeout(() => {
      page.value = 1
      void fetchTasks()
    }, 300)
  },
)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const pageNumbers = computed(() => {
  const count = totalPages.value
  const current = page.value
  const start = Math.max(1, Math.min(current - 2, count - 4))
  return Array.from({ length: Math.min(5, count) }, (_, index) => start + index)
})

function goToPage(target: number) {
  const next = Math.min(Math.max(target, 1), totalPages.value)
  if (next !== page.value) {
    page.value = next
    void fetchTasks()
  }
}

const statusOptions: Array<{ label: string; value: TaskStatus | 'all' }> = [
  { label: '全部', value: 'all' },
  { label: '排队中', value: 'queued' },
  { label: '执行中', value: 'running' },
  { label: '取消中', value: 'canceling' },
  { label: '待复核', value: 'review' },
  { label: '已完成', value: 'done' },
  { label: '执行失败', value: 'failed' },
  { label: '已取消', value: 'canceled' },
]

const PLATFORM_LABELS: Record<string, string> = { h5: 'H5', web: 'Web', app: 'Android' }

function platformLabel(code: string) {
  return PLATFORM_LABELS[code] ?? code
}

/** 部门下拉的候选来自已加载过的任务；筛选本身由服务端完成。 */
const knownDepartments = ref<string[]>([])
const departments = computed(() => knownDepartments.value)

const visibleRows = computed(() => rows.value)

function formatDuration(ms: number | null | undefined) {
  if (ms == null) return '—'
  const seconds = Math.round(ms / 1000)
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return minutes ? `${minutes}分${rest}秒` : `${rest}秒`
}

const selectedVisibleCount = computed(
  () => visibleRows.value.filter((row) => selectedIds.value.includes(row.task_id)).length,
)
const allVisibleSelected = computed(
  () => visibleRows.value.length > 0 && selectedVisibleCount.value === visibleRows.value.length,
)

function getStatusMeta(status: string) {
  const map: Record<string, { label: string; className: string }> = {
    queued: { label: taskStatusLabel('queued'), className: 'tone-warning' },
    running: { label: taskStatusLabel('running'), className: 'tone-primary' },
    canceling: { label: taskStatusLabel('canceling'), className: 'tone-warning' },
    review: { label: taskStatusLabel('review'), className: 'tone-warning' },
    done: { label: taskStatusLabel('done'), className: 'tone-success' },
    failed: { label: taskStatusLabel('failed'), className: 'tone-danger' },
    canceled: { label: taskStatusLabel('canceled'), className: 'tone-muted' },
  }
  return map[status] || { label: taskStatusLabel(status), className: 'tone-muted' }
}

function getReportMeta(
  status: string,
  availability: WorkstationTaskListItem['report_availability'],
  taskStatus: string,
) {
  if (availability === 'preview') {
    return { label: '报告预览', className: 'tone-warning' }
  }
  if (availability === 'final') {
    return { label: '正式报告', className: 'tone-success' }
  }
  const map: Record<string, { label: string; className: string }> = {
    not_started: { label: '未生成', className: 'tone-muted' },
    generating: { label: '生成中', className: 'tone-primary' },
    ready: { label: '已生成', className: 'tone-success' },
    generated: { label: '已生成', className: 'tone-success' },
    failed: { label: '生成失败', className: 'tone-danger' },
    not_applicable: { label: '不适用', className: 'tone-muted' },
  }
  if (taskStatus === 'canceled' && reportStateOf(status, taskStatus) === 'none') {
    return map.not_started
  }
  return map[status] || map.not_started
}

const actionTextMap: Record<string, string> = {
  start: '开始执行',
  edit: '编辑',
  cancel: '取消',
  detail: '查看详情',
  report: '查看报告',
  exploration: '查看探索结果',
  evidence: '下载证据',
  rerun: '重新执行',
  copy: '复制任务',
  failure: '查看失败原因',
  'retry-finalization': '恢复报告收口',
  delete: '删除',
}

function getActions(row: WorkstationTaskListItem) {
  const actions = Array.from(new Set(row.available_actions))
    .filter((value) => value !== 'delete' || (nav.isAdmin.value && ['done', 'review', 'failed', 'canceled'].includes(row.status)))
    .sort((left, right) => {
      const order = (value: string) => value === 'delete' ? 2 : value === 'cancel' ? 1 : 0
      return order(left) - order(right)
    })
    .map((value) => ({
    value,
    label:
      value === 'report' && row.report_availability === 'preview'
        ? row.target_platform === 'h5'
          ? '查看报告（复核）'
          : '查看报告（预览）'
        : actionTextMap[value] || value,
    emphasis: value === 'start' || value === 'detail' || value === 'failure',
    danger: value === 'cancel' || value === 'delete',
    disabled: false,
    title: undefined as string | undefined,
  }))
  const needsExplicitNoneAction =
    row.target_platform === 'web' &&
    row.execution_mode === 'full_inspection' &&
    row.report_availability === 'none' &&
    !row.available_actions.includes('report')
  if (needsExplicitNoneAction) {
    actions.push({
      value: 'report-none',
      label: '查看报告',
      emphasis: false,
      danger: false,
      disabled: true,
      title: '尚未产生可信采集，暂时没有可预览的报告',
    })
  }
  return actions
}

function toggleRow(taskId: string) {
  selectedIds.value = selectedIds.value.includes(taskId)
    ? selectedIds.value.filter((id) => id !== taskId)
    : [...selectedIds.value, taskId]
}

function toggleAllVisible() {
  const visibleIds = visibleRows.value.map((row) => row.task_id)
  if (allVisibleSelected.value) {
    selectedIds.value = selectedIds.value.filter((id) => !visibleIds.includes(id))
    return
  }
  selectedIds.value = Array.from(new Set([...selectedIds.value, ...visibleIds]))
}

function resetFilters() {
  filters.taskName = ''
  filters.department = ''
  filters.productCode = ''
  filters.platform = ''
  filters.submitterName = ''
  filters.status = 'all'
  filters.createdStart = ''
  filters.createdEnd = ''
}

function queryTasks() {
  window.clearTimeout(filterTimer)
  page.value = 1
  void fetchTasks()
}

function createTask() {
  navigate('/tasks/new')
}

async function confirmDelete(row: WorkstationTaskListItem) {
  if (operatingTaskId.value) return
  operatingTaskId.value = row.task_id
  try {
    const result = await deleteTask(row.task_id)
    selectedIds.value = selectedIds.value.filter((id) => id !== row.task_id)
    message.success(result.message || '任务已删除')
    if (rows.value.length === 1 && page.value > 1) page.value -= 1
    await fetchTasks()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '任务删除失败')
  } finally {
    operatingTaskId.value = ''
  }
}

async function handleAction(action: string, row: WorkstationTaskListItem) {
  if (action === 'detail') {
    navigate({ path: `/tasks/${row.task_id}`, query: taskListQuery(filters, page.value, pageSize.value) })
    return
  }
  if (action === 'report') {
    navigate({ path: '/reports', query: { ...taskListQuery(filters, page.value, pageSize.value), task: row.task_id } })
    return
  }
  if (action === 'exploration') {
    navigate({ path: `/tasks/${row.task_id}`, query: taskListQuery(filters, page.value, pageSize.value) })
    return
  }
  if (action === 'evidence') {
    try {
      await downloadScreenshotsArchive(row.task_id)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '证据包下载失败')
    }
    return
  }
  if (action === 'failure') {
    message.error(`${row.task_name}：${row.failure_reason || '执行失败原因待补充'}`)
    return
  }
  if (action === 'edit') {
    message.info('编辑功能暂未开放')
    return
  }
  if (
    action === 'start' ||
    action === 'cancel' ||
    action === 'rerun' ||
    action === 'copy' ||
    action === 'retry-finalization'
  ) {
    operatingTaskId.value = row.task_id
    try {
      const result = await performTaskOperation(row.task_id, action)
      message.success(result.message || '任务操作已受理')
      await fetchTasks(true)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '任务操作失败')
      if (action === 'cancel') await fetchTasks(true)
    } finally {
      operatingTaskId.value = ''
    }
    return
  }
  message.info(`暂不支持的操作：${action}`)
}

function formatDate(value: string) {
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

const batchRunning = ref(false)

async function batchAction(action: 'cancel' | 'rerun' | 'copy', label: string) {
  if (selectedIds.value.length === 0) {
    message.warning('请先勾选任务')
    return
  }
  const selectedRows = rows.value.filter((row) => selectedIds.value.includes(row.task_id))
  const targets = selectedRows.filter((row) => row.available_actions.includes(action))
  const skipped = selectedRows.length - targets.length
  if (targets.length === 0) {
    message.warning('勾选的任务当前状态都不支持该操作')
    return
  }
  batchRunning.value = true
  let succeeded = 0
  let failed = 0
  try {
    for (const row of targets) {
      try {
        await performTaskOperation(row.task_id, action)
        succeeded += 1
      } catch {
        failed += 1
      }
    }
  } finally {
    batchRunning.value = false
  }
  const parts = [`${label}成功 ${succeeded} 个`]
  if (failed > 0) {
    parts.push(`失败 ${failed} 个`)
  }
  if (skipped > 0) {
    parts.push(`${skipped} 个状态不支持已跳过`)
  }
  if (failed > 0) {
    message.warning(parts.join('，'))
  } else {
    message.success(parts.join('，'))
  }
  selectedIds.value = []
  await fetchTasks(true)
}

// ---------- 队列与服务卡片 ----------

const queueCount = computed(() => systemStatus.value?.queued_tasks?.count ?? systemStatus.value?.queue?.depth ?? null)
const runningCount = computed(() => rows.value.filter((row) => row.status === 'running').length)
const queuedInPage = computed(() => rows.value.filter((row) => row.status === 'queued').length)

const waitEstimate = computed(() => {
  const seconds = systemStatus.value?.queued_tasks?.oldest_waiting_seconds
  if (seconds == null) return '—'
  const minutes = Math.max(1, Math.round(seconds / 60))
  return `${minutes}–${minutes + 4} 分钟`
})

const serviceLabel = computed(() => {
  if (!systemStatus.value) return '状态未知'
  return systemStatus.value.worker.alive ? '运行正常' : '执行服务离线'
})

const rangeText = computed(() => {
  if (!total.value) return loading.value ? '加载中…' : '暂无数据'
  const start = (page.value - 1) * pageSize.value + 1
  const end = Math.min(page.value * pageSize.value, total.value)
  return `显示 ${start}-${end} 条，共 ${total.value} 条`
})
</script>

<template>
  <div class="workstation-page task-list-page">
    <div v-if="workerOffline" class="offline-banner">
      <strong>质检 Worker 未运行：</strong>
      排队中的任务不会被执行。请检查 alioth-worker 容器状态与启动日志（启动自检失败会导致进程反复崩溃重启）。
    </div>

    <!-- 筛选卡 -->
    <section class="workstation-glass-panel filter-card" data-testid="task-filters">
      <div class="filter-grid" :class="{ 'is-compact': !showOrg }">
        <label class="span-2">
          <span class="qc-label">任务名称</span>
          <input v-model="filters.taskName" class="qc-input" type="text" placeholder="请输入任务名称" />
        </label>
        <template v-if="showOrg">
          <label>
            <span class="qc-label">所属公司</span>
            <SearchSelect disabled>
              <option>{{ workspaceName }}</option>
            </SearchSelect>
          </label>
          <label>
            <span class="qc-label">部门</span>
            <SearchSelect v-model="filters.department">
              <option value="">全部</option>
              <option v-for="name in departments" :key="name" :value="name">{{ name }}</option>
            </SearchSelect>
          </label>
        </template>
        <label>
          <span class="qc-label">产品</span>
          <SearchSelect v-model="filters.productCode" data-testid="task-filter-product">
            <option value="">全部</option>
            <option v-if="productsLoading" value="__loading__" disabled>产品加载中…</option>
            <option v-for="item in productOptions" :key="item.project_code" :value="item.project_code">
              {{ item.product_name }}
            </option>
          </SearchSelect>
        </label>
        <label>
          <span class="qc-label">目标平台</span>
          <SearchSelect v-model="filters.platform">
            <option value="">全部</option>
            <option value="h5">H5</option>
            <option value="web">Web</option>
            <option value="app">Android</option>
          </SearchSelect>
        </label>
        <label>
          <span class="qc-label">任务状态</span>
          <SearchSelect v-model="filters.status">
            <option v-for="option in statusOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </SearchSelect>
        </label>
      </div>
      <div class="filter-grid filter-grid-bottom" :class="{ 'is-compact': !showOrg }">
        <div class="span-2">
          <span class="qc-label">创建时间</span>
          <div class="date-range">
            <input v-model="filters.createdStart" class="qc-input" type="date" />
            <span class="qc-muted">-</span>
            <input v-model="filters.createdEnd" class="qc-input" type="date" />
          </div>
        </div>
        <label>
          <span class="qc-label">发起人</span>
          <input v-model="filters.submitterName" class="qc-input" type="text" placeholder="请输入发起人姓名" data-testid="task-filter-submitter" />
        </label>
        <div class="filter-actions span-4">
          <button type="button" class="qc-btn qc-btn-primary" @click="queryTasks">查询</button>
          <button type="button" class="qc-btn" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <!-- 队列 / 服务卡片：读不到服务状态的账号只看排队和执行中两张 -->
    <section class="stat-grid" :class="{ 'is-compact': !canReadSystemStatus }" data-testid="task-stats">
      <!-- 首次加载时数字先显示「…」，不让 0 和「暂无」看起来像最终结果 -->
      <article class="stat-card">
        <span>当前排队</span>
        <strong>{{ loading ? '…' : (queueCount ?? queuedInPage) }}</strong>
        <em>{{ loading ? '加载中…' : queuedInPage ? `本页 ${queuedInPage} 个排队任务` : '本部门暂无排队任务' }}</em>
      </article>
      <article class="stat-card">
        <span>执行中</span>
        <strong class="tone-primary">{{ loading ? '…' : runningCount }}</strong>
        <em>{{ loading ? '加载中…' : systemStatus ? `执行实例 ${systemStatus.worker.active_instances ?? 0} 个` : '按本页任务统计' }}</em>
      </article>
      <article v-if="canReadSystemStatus" class="stat-card">
        <span>预计等待</span>
        <strong class="tone-warning">{{ loading ? '…' : waitEstimate }}</strong>
        <em>根据任务复杂度动态变化</em>
      </article>
      <article v-if="canReadSystemStatus" class="stat-card">
        <span>服务状态</span>
        <strong :class="loading ? '' : systemStatus?.worker.alive ? 'tone-success' : 'tone-danger'">{{ loading ? '…' : serviceLabel }}</strong>
        <em>{{ loading ? '加载中…' : !systemStatus ? '无法读取服务状态' : systemStatus.queue?.redis_available === false ? '队列服务不可用' : '备用资源可用' }}</em>
      </article>
    </section>

    <!-- 列表卡 -->
    <section class="workstation-glass-panel table-card" data-testid="task-table">
      <div class="qc-card-head">
        <div class="head-left">
          <button type="button" class="qc-btn qc-btn-primary" @click="createTask">+ 创建任务</button>
          <button
            type="button"
            class="qc-btn"
            :disabled="selectedIds.length === 0 || batchRunning"
            @click="batchAction('rerun', '批量重新执行')"
          >
            批量重新执行
          </button>
          <button
            type="button"
            class="qc-btn"
            :disabled="selectedIds.length === 0 || batchRunning"
            @click="batchAction('cancel', '批量取消')"
          >
            批量取消
          </button>
          <span v-if="selectedIds.length" class="qc-muted small">已勾选 {{ selectedIds.length }} 项</span>
        </div>
        <span class="qc-muted small">{{ loading && !rows.length ? '加载中…' : `共 ${total} 条任务` }}</span>
      </div>

      <div class="table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th class="check-col">
                <input type="checkbox" :checked="allVisibleSelected" @change="toggleAllVisible" />
              </th>
              <th class="name-col">任务名称</th>
              <th v-if="showOrg">所属公司</th>
              <th v-if="showOrg">部门</th>
              <th>产品</th>
              <th>目标平台</th>
              <th>发起人</th>
              <th>创建时间</th>
              <th>执行时长</th>
              <th>任务状态</th>
              <th>报告状态</th>
              <th class="is-right action-col">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-if="!loading && !loadError">
              <tr v-for="row in visibleRows" :key="row.task_id">
                <td class="check-col">
                  <input type="checkbox" :checked="selectedIds.includes(row.task_id)" @change="toggleRow(row.task_id)" />
                </td>
                <td class="name-col name-cell">
                  <button type="button" class="task-name-link" @click="handleAction('detail', row)">{{ row.task_name }}</button>
                  <a v-if="row.target_url" :href="row.target_url" target="_blank" rel="noreferrer">{{ row.target_url }}</a>
                  <span v-else class="qc-muted">App 质检</span>
                </td>
                <td v-if="showOrg">{{ row.tenant_name || workspaceName }}</td>
                <td v-if="showOrg">{{ row.product.department_name || '—' }}</td>
                <td>{{ row.product.product_name }}</td>
                <td><span class="qc-chip">{{ platformLabel(row.target_platform) }}</span></td>
                <td>{{ row.submitter_name || '—' }}</td>
                <td class="qc-muted">{{ formatDate(row.created_at) }}</td>
                <td class="qc-muted">{{ formatDuration(row.duration_ms) }}</td>
                <td>
                  <span class="qc-chip" :class="getStatusMeta(row.status).className">
                    <i></i>{{ getStatusMeta(row.status).label }}
                    <template v-if="row.status === 'running'"> {{ row.progress }}%</template>
                  </span>
                  <small v-if="row.status === 'failed' && row.failure_reason" class="failure-text">{{ row.failure_reason }}</small>
                  <small v-if="isLongExecution(row.status, row.duration_ms, row.execution_finished_at)" class="failure-text">执行超过 2.5 小时，请核查进展</small>
                </td>
                <td>
                  <span
                    class="qc-chip"
                    :class="getReportMeta(row.report_status, row.report_availability, row.status).className"
                  >
                    {{ getReportMeta(row.report_status, row.report_availability, row.status).label }}
                  </span>
                </td>
                <td class="is-right action-col">
                  <div class="actions">
                    <div class="general-actions">
                    <template v-for="action in getActions(row).filter((item) => item.value !== 'cancel')" :key="action.value">
                      <n-popconfirm
                        v-if="action.value === 'delete'"
                        positive-text="确认删除"
                        negative-text="保留任务"
                        :content-style="{ maxWidth: 'min(360px, calc(100vw - 32px))' }"
                        :positive-button-props="{ loading: operatingTaskId === row.task_id }"
                        @positive-click="confirmDelete(row)"
                      >
                        <template #trigger>
                          <button type="button" class="qc-link danger" :disabled="operatingTaskId === row.task_id">删除</button>
                        </template>
                        确认删除任务「{{ row.task_name }}」？删除后将从列表移除，已有报告、证据和审计记录仍会保留。
                      </n-popconfirm>
                      <button
                        v-else
                        type="button"
                        class="qc-link"
                        :class="[{ emphasis: action.emphasis, danger: action.danger }]"
                        :disabled="action.disabled || operatingTaskId === row.task_id"
                        :title="action.title"
                        @click="handleAction(action.value, row)"
                      >
                        {{ action.label }}
                      </button>
                    </template>
                    </div>
                    <div class="cancel-slot">
                      <button v-if="row.available_actions.includes('cancel')" type="button" class="qc-link danger" :disabled="operatingTaskId === row.task_id" @click="handleAction('cancel', row)">取消</button>
                    </div>
                  </div>
                </td>
              </tr>
              <tr v-if="visibleRows.length === 0">
                <td class="qc-empty" :colspan="columnCount">暂无任务，点击「创建任务」发起第一条质检</td>
              </tr>
            </template>
            <tr v-else-if="loading">
              <td class="qc-empty" :colspan="columnCount">加载中…</td>
            </tr>
            <tr v-else>
              <td class="qc-empty qc-error" :colspan="columnCount">{{ loadError }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="qc-pager">
        <div class="pager-left">
          <SearchSelect v-model="pageSize" class="page-size" size="small">
            <option :value="10">10 条/页</option>
            <option :value="20">20 条/页</option>
            <option :value="50">50 条/页</option>
          </SearchSelect>
          <span>{{ rangeText }}</span>
        </div>
        <div class="qc-pager-pages">
          <button type="button" :disabled="page <= 1" @click="goToPage(page - 1)">上一页</button>
          <button
            v-for="number in pageNumbers"
            :key="number"
            type="button"
            :class="{ 'is-active': number === page }"
            @click="goToPage(number)"
          >
            {{ number }}
          </button>
          <button type="button" :disabled="page >= totalPages" @click="goToPage(page + 1)">下一页</button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.task-list-page {
  gap: 24px;
}

.offline-banner {
  padding: 12px 16px;
  border: 1px solid rgba(217, 119, 6, 0.35);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-warning-strong);
  background: var(--tint-warning);
  font-size: 13px;
}

.filter-card {
  padding: 16px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 16px;
}

.filter-grid-bottom {
  margin-top: 16px;
}

.filter-grid label {
  display: block;
}

.span-2 {
  grid-column: span 2;
}

.span-4 {
  grid-column: span 4;
}

/* 部门平台少了公司 / 部门两项，按原型收成 5 列 */
.filter-grid.is-compact {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}

.filter-grid.is-compact .span-4 {
  grid-column: span 2;
}

.date-range {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-actions {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.stat-grid.is-compact {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.stat-card {
  display: flex;
  flex-direction: column;
  padding: 16px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel);
  box-shadow: var(--workstation-shadow);
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

.stat-card > em {
  margin-top: 4px;
  color: var(--workstation-text-faint);
  font-size: 12px;
  font-style: normal;
}

.tone-primary {
  color: var(--workstation-primary) !important;
}

.tone-warning {
  color: var(--workstation-warning) !important;
}

.tone-success {
  color: var(--workstation-success) !important;
}

.tone-danger {
  color: var(--workstation-danger) !important;
}

.stat-card .qc-chip,
.table-card .qc-chip.tone-primary,
.table-card .qc-chip.tone-warning,
.table-card .qc-chip.tone-success,
.table-card .qc-chip.tone-danger {
  color: inherit;
}

.table-card {
  overflow: hidden;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.small {
  font-size: 12px;
}

.table-wrap {
  overflow-x: auto;
}

.qc-table {
  min-width: 1140px;
  border-collapse: separate;
  border-spacing: 0;
}

.qc-table th,
.qc-table td {
  padding: 12px;
}

.check-col,
.name-col,
.action-col {
  position: sticky;
  z-index: 1;
  background: var(--workstation-panel);
}

thead .check-col,
thead .name-col,
thead .action-col {
  z-index: 2;
  background: var(--workstation-table-head-bg);
}

.check-col {
  left: 0;
  min-width: 40px;
  max-width: 40px;
  width: 40px;
  box-sizing: border-box;
}

.name-col {
  left: 40px;
  min-width: 220px;
  max-width: 220px;
  width: 220px;
  box-sizing: border-box;
  box-shadow: 2px 0 0 var(--workstation-border);
}

.action-col {
  right: 0;
  min-width: 240px;
  max-width: 240px;
  width: 240px;
  box-sizing: border-box;
  box-shadow: -2px 0 0 var(--workstation-border);
}

.name-cell .task-name-link {
  display: block;
  color: var(--workstation-text-strong);
  font-weight: 500;
  white-space: normal;
  overflow-wrap: anywhere;
}

.task-name-link { background: transparent; border: 0; padding: 0; font-family: inherit; font-size: inherit; text-align: left; cursor: pointer; }
.task-name-link:hover { text-decoration: underline; }

.name-cell a,
.name-cell .qc-muted {
  display: block;
  max-width: 196px;
  overflow: hidden;
  color: var(--workstation-text-faint);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.failure-text {
  display: block;
  margin-top: 4px;
  color: var(--workstation-danger);
  font-size: 12px;
}

.actions {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 28px;
  gap: 12px;
  align-items: start;
}

.general-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  white-space: nowrap;
}

.cancel-slot { min-width: 28px; text-align: right; }

.actions .qc-link.danger {
  color: var(--workstation-danger);
}

.pager-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.page-size {
  width: 112px;
}

@media (max-width: 1200px) {
  .filter-grid,
  .filter-grid.is-compact {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .span-4,
  .filter-grid.is-compact .span-4 {
    grid-column: span 1;
  }

  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
