<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { taskListReturnQuery } from '@/modules/tasks/taskListState'
import { useMessage } from 'naive-ui'
import {
  downloadReportExport,
  downloadReportExportBatch,
  listReports,
  type ListReportsParams,
  type ReportListStatusFilter,
} from '@/services/reports.api'
import { listWorkstationProducts } from '@/services/products.api'
import { authStore } from '@/stores/auth'
import type { WorkstationReportListItem } from '@/types/report'
import { reportListFlow } from '@/modules/reports/components/reportListFlow'

/**
 * 报告中心 · 报告列表（对齐 2.1 原型 report.html）：筛选卡 + 列表卡（批量导出、
 * 任务名称/产品名称/平台/报告状态/通过率/问题数量/生成时间/操作）。
 * 列表区分生成、待确认、预览与正式完成；生成状态筛选仍由服务端分页。
 * 部门质检平台只看本部门报告，不展示公司、部门筛选；管理后台保留。
 */
const route = useRoute()
const nav = usePortalNav()
const message = useMessage()

const items = ref<WorkstationReportListItem[]>([])
const total = ref(0)
const loading = ref(true)
const loadError = ref('')
const selectedIds = ref<string[]>([])
const page = ref(1)
const PAGE_SIZE = 10

const workspaceName = computed(() => authStore.user.value?.tenant_name || '本公司')

const filters = reactive({
  department: '',
  product: '',
  platform: '',
  taskName: '',
  status: '' as ReportListStatusFilter | '',
  date: '',
})
const applied = reactive({ ...filters })

/** 部门下拉候选来自已加载过的报告；产品下拉取工作台产品清单。筛选本身都由服务端完成。 */
const knownDepartments = ref<string[]>([])
const productOptions = ref<string[]>([])
/** 产品下拉的选项还在加载：下拉里放一条禁用的「产品加载中…」 */
const productsLoading = ref(true)

const PLATFORM_LABELS: Record<string, string> = { h5: 'H5', web: 'Web', app: 'Android', android: 'Android', ios: 'iOS' }

function platformLabel(code: string | null | undefined) {
  if (!code) return '—'
  return PLATFORM_LABELS[code.toLowerCase()] ?? code
}

const STATUS_OPTIONS: Array<{ value: ReportListStatusFilter | ''; label: string }> = [
  { value: '', label: '全部报告状态' },
  { value: 'ready', label: '已生成（含待确认）' },
  { value: 'generating', label: '生成中' },
  { value: 'failed', label: '生成失败' },
]

function canView(item: WorkstationReportListItem) {
  return reportListFlow(item).readable
}

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pageNumbers = computed(() => {
  const count = pageCount.value
  const start = Math.max(1, Math.min(page.value - 2, count - 4))
  return Array.from({ length: Math.min(5, count) }, (_, index) => start + index)
})

const allPagedSelected = computed(
  () => exportableItems.value.length > 0 && exportableItems.value.every((item) => selectedIds.value.includes(item.task_id)),
)
const exportableItems = computed(() => items.value.filter((item) => reportListFlow(item).exportable))

function buildParams(): ListReportsParams {
  return {
    page: page.value,
    page_size: PAGE_SIZE,
    task_name: applied.taskName.trim() || undefined,
    product_name: applied.product || undefined,
    department_name: applied.department || undefined,
    target_platform: applied.platform || undefined,
    report_status: applied.status || undefined,
    created_from: applied.date ? `${applied.date}T00:00:00` : undefined,
    created_to: applied.date ? `${applied.date}T23:59:59` : undefined,
  }
}

async function load() {
  loading.value = true
  try {
    const result = await listReports(buildParams())
    items.value = result.items
    const blocked = new Set(result.items.filter((item) => !reportListFlow(item).exportable).map((item) => item.task_id))
    selectedIds.value = selectedIds.value.filter((id) => !blocked.has(id))
    total.value = result.total
    for (const item of result.items) {
      if (item.department_name && !knownDepartments.value.includes(item.department_name)) {
        knownDepartments.value.push(item.department_name)
      }
    }
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '报告列表加载失败'
  } finally {
    loading.value = false
  }
}

/** 产品候选取工作台产品清单（按名称去重）；取不到时只保留「全部产品」，不影响列表。 */
async function loadProductOptions() {
  try {
    const result = await listWorkstationProducts()
    productOptions.value = Array.from(
      new Set(result.items.map((item) => item.product_name).filter((name): name is string => !!name)),
    )
  } catch {
    productOptions.value = []
  } finally {
    productsLoading.value = false
  }
}

function search() {
  Object.assign(applied, filters)
  page.value = 1
  void load()
}

function resetFilters() {
  filters.department = ''
  filters.product = ''
  filters.platform = ''
  filters.taskName = ''
  filters.status = ''
  filters.date = ''
  search()
}

function goPage(next: number) {
  const target = Math.min(Math.max(1, next), pageCount.value)
  if (target !== page.value) {
    page.value = target
    void load()
  }
}

function toggleSelect(item: WorkstationReportListItem) {
  if (!reportListFlow(item).exportable) return
  const taskId = item.task_id
  selectedIds.value = selectedIds.value.includes(taskId)
    ? selectedIds.value.filter((id) => id !== taskId)
    : [...selectedIds.value, taskId]
}

function toggleSelectAll() {
  const ids = exportableItems.value.map((item) => item.task_id)
  if (allPagedSelected.value) {
    selectedIds.value = selectedIds.value.filter((id) => !ids.includes(id))
  } else {
    selectedIds.value = Array.from(new Set([...selectedIds.value, ...ids]))
  }
}

function openReport(item: WorkstationReportListItem) {
  void nav.push(`/reports/${item.task_id}`)
}

async function exportOne(item: WorkstationReportListItem) {
  if (!reportListFlow(item).exportable) return
  try {
    await downloadReportExport(item.task_id)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '报告导出失败')
  }
}

const exportingBatch = ref(false)

async function exportSelected() {
  if (!selectedIds.value.length) {
    message.warning('请先勾选报告')
    return
  }
  exportingBatch.value = true
  try {
    await downloadReportExportBatch(selectedIds.value)
    message.success(`已打包导出 ${selectedIds.value.length} 份报告`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '批量导出失败')
  } finally {
    exportingBatch.value = false
  }
}

function formatDate(value: string | null) {
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

onMounted(() => {
  // 兼容旧链接：保留任务列表返回状态和待确认执行编号，再进入报告详情。
  const legacyTask = route.query.task?.toString()
  if (legacyTask) {
    const execution = route.query.execution?.toString()
    const query = { ...taskListReturnQuery(route.query), ...(execution ? { execution } : {}) }
    void nav.replace({
      path: `/reports/${legacyTask}`,
      query: Object.keys(query).length ? query : undefined,
    })
    return
  }
  void load()
  void loadProductOptions()
})
</script>

<template>
  <div class="workstation-page report-list-page">
    <section class="workstation-glass-panel filter-card" data-testid="report-filters">
      <div class="filter-grid" :class="{ 'is-admin': nav.isAdmin.value }">
        <template v-if="nav.isAdmin.value">
          <SearchSelect disabled :title="workspaceName">
            <option>{{ workspaceName }}</option>
          </SearchSelect>
          <SearchSelect v-model="filters.department" data-testid="report-filter-department">
            <option value="">全部部门</option>
            <option v-for="name in knownDepartments" :key="name" :value="name">{{ name }}</option>
          </SearchSelect>
        </template>
        <SearchSelect v-model="filters.product" data-testid="report-filter-product">
          <option value="">全部产品</option>
          <option v-if="productsLoading" value="__loading__" disabled>产品加载中…</option>
          <option v-for="name in productOptions" :key="name" :value="name">{{ name }}</option>
        </SearchSelect>
        <SearchSelect v-model="filters.platform">
          <option value="">全部平台</option>
          <option value="h5">H5</option>
          <option value="web">Web</option>
          <option value="app">Android</option>
        </SearchSelect>
        <input
          v-model="filters.taskName"
          class="qc-input"
          type="text"
          placeholder="任务名称"
          data-testid="report-filter-name"
          @keyup.enter="search"
        />
        <SearchSelect v-model="filters.status" data-testid="report-filter-status">
          <option v-for="option in STATUS_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
        </SearchSelect>
        <input v-model="filters.date" class="qc-input" type="date" />
      </div>
      <div class="filter-actions">
        <button type="button" class="qc-btn qc-btn-primary" data-testid="report-search" @click="search">查询</button>
        <button type="button" class="qc-btn" @click="resetFilters">重置</button>
      </div>
    </section>

    <section class="workstation-glass-panel table-card" data-testid="report-table">
      <div class="qc-card-head">
        <div class="head-left">
          <button
            type="button"
            class="qc-btn"
            :disabled="!selectedIds.length || exportingBatch"
            data-testid="report-export-batch"
            @click="exportSelected"
          >
            {{ exportingBatch ? '打包中…' : '批量导出' }}
          </button>
          <span class="qc-muted small">已选 {{ selectedIds.length }} 项</span>
        </div>
        <span class="qc-muted small">{{ loading && !items.length ? '加载中…' : `共 ${total} 条报告` }}</span>
      </div>

      <p v-if="loading" class="qc-empty">加载中…</p>
      <p v-else-if="loadError" class="qc-empty qc-error">{{ loadError }}</p>
      <p v-else-if="!items.length" class="qc-empty">没有符合条件的报告</p>
      <div v-else class="table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th class="check-col"><input type="checkbox" :checked="allPagedSelected" :disabled="!exportableItems.length" @change="toggleSelectAll" /></th>
              <th>任务名称</th>
              <th>产品名称</th>
              <th>平台</th>
              <th>报告状态</th>
              <th>通过率</th>
              <th>问题数量</th>
              <th>生成时间</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.task_id">
              <td class="check-col">
                <input type="checkbox" :checked="selectedIds.includes(item.task_id)" :disabled="!reportListFlow(item).exportable" :title="reportListFlow(item).reason" @change="toggleSelect(item)" />
              </td>
              <td class="name-cell">{{ item.task_name }}</td>
              <td>{{ item.product_name || '—' }}</td>
              <td><span class="qc-chip">{{ platformLabel(item.target_platform) }}</span></td>
              <td>
                <span class="qc-chip" :class="reportListFlow(item).tone" :title="reportListFlow(item).reason" data-testid="report-state">
                  <i></i>{{ reportListFlow(item).label }}
                </span>
              </td>
              <td>{{ canView(item) && item.pass_rate != null ? `${item.pass_rate}%` : '-' }}</td>
              <td>{{ canView(item) ? (item.issue_count ?? item.attention_required ?? '-') : '-' }}</td>
              <td class="qc-muted">{{ formatDate(item.generated_at) }}</td>
              <td class="is-right actions">
                <button type="button" class="qc-link" :disabled="!canView(item)" @click="openReport(item)">在线查看</button>
                <button type="button" class="qc-link" :disabled="!reportListFlow(item).exportable" :title="reportListFlow(item).reason" @click="exportOne(item)">导出</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="!loading && total" class="qc-pager">
        <span>显示 {{ (page - 1) * PAGE_SIZE + 1 }}-{{ Math.min(page * PAGE_SIZE, total) }} 条，共 {{ total }} 条</span>
        <div class="qc-pager-pages">
          <button type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
          <button
            v-for="number in pageNumbers"
            :key="number"
            type="button"
            :class="{ 'is-active': number === page }"
            @click="goPage(number)"
          >
            {{ number }}
          </button>
          <button type="button" :disabled="page >= pageCount" @click="goPage(page + 1)">下一页</button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.filter-card {
  padding: 16px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.filter-grid.is-admin {
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

.filter-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
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

.check-col {
  width: 40px;
}

.name-cell {
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.actions {
  white-space: nowrap;
}

.actions .qc-link + .qc-link {
  margin-left: 12px;
}

@media (max-width: 1200px) {
  .filter-grid,
  .filter-grid.is-admin {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .filter-grid,
  .filter-grid.is-admin {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
