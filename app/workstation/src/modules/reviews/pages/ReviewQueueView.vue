<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { NButton, NSpin, useMessage } from 'naive-ui'
import ReviewStatusTag from '@/components/status/ReviewStatusTag.vue'
import { listReviews, performReviewAction } from '@/services/reviews.api'
import type { ReviewQueueItem, ReviewStatus, ReviewTriggerReason } from '@/types/review'

const nav = usePortalNav()
const message = useMessage()

const rows = ref<ReviewQueueItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(true)
const loadError = ref('')
const operatingId = ref('')

const statusFilter = ref<ReviewStatus | 'all'>('pending')
const reasonFilter = ref<ReviewTriggerReason | 'all'>('all')

const statusOptions: Array<{ label: string; value: ReviewStatus | 'all' }> = [
  { label: '全部状态', value: 'all' },
  { label: '待处理', value: 'pending' },
  { label: '已确认', value: 'approved' },
  { label: '已驳回', value: 'rejected' },
  { label: '需复核', value: 'needs_recheck' },
]

const reasonTextMap: Record<string, string> = {
  low_confidence: '置信度不足',
  high_risk: '高风险命中',
  evidence_insufficient: '证据不足',
  loop_budget_exceeded: '执行预算耗尽',
  no_progress: '执行无进展',
  manual_flag: '人工标记',
}

const reasonOptions: Array<{ label: string; value: ReviewTriggerReason | 'all' }> = [
  { label: '全部原因', value: 'all' },
  ...Object.entries(reasonTextMap).map(([value, label]) => ({
    label,
    value: value as ReviewTriggerReason,
  })),
]

async function fetchReviews(silent = false) {
  if (!silent) {
    loading.value = true
  }
  try {
    const result = await listReviews({
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      trigger_reason: reasonFilter.value === 'all' ? undefined : reasonFilter.value,
      page: page.value,
      page_size: pageSize,
    })
    rows.value = result.items
    total.value = result.total
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '审核队列加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => void fetchReviews())

watch([statusFilter, reasonFilter], () => {
  page.value = 1
  void fetchReviews()
})

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function goToPage(target: number) {
  const next = Math.min(Math.max(target, 1), totalPages.value)
  if (next !== page.value) {
    page.value = next
    void fetchReviews()
  }
}

const actionTextMap: Record<'approve' | 'reject' | 'recheck', string> = {
  approve: '确认',
  reject: '驳回',
  recheck: '转复核',
}

async function handleAction(item: ReviewQueueItem, action: 'approve' | 'reject' | 'recheck') {
  let reason: string | undefined
  if (action === 'reject' || action === 'recheck') {
    const input = window.prompt(`${actionTextMap[action]}意见（可留空）`, '')
    if (input === null) {
      return
    }
    reason = input.trim() || undefined
  }
  operatingId.value = item.review_id
  try {
    const result = await performReviewAction(item.review_id, action, reason)
    message.success(result.message || `${actionTextMap[action]}已受理`)
    await fetchReviews(true)
  } catch (error) {
    message.error(error instanceof Error ? error.message : `${actionTextMap[action]}失败`)
  } finally {
    operatingId.value = ''
  }
}

function reasonText(value: string) {
  return reasonTextMap[value] || value
}

function reviewComment(item: ReviewQueueItem) {
  if (!item.reviewer && !item.review_reason) {
    return '-'
  }
  const parts = []
  if (item.reviewer) {
    parts.push(item.reviewer)
  }
  if (item.review_reason) {
    parts.push(item.review_reason)
  }
  return parts.join('：')
}

function goTask(taskId: string) {
  void nav.push(`/tasks/${taskId}`)
}

function formatDate(value: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
</script>

<template>
  <section class="review-queue-shell">
    <div class="page-heading">
      <div>
        <h1>人工审核队列</h1>
        <p>处理规则执行中触发人工确认的项，最终决定由业务审核人做出</p>
      </div>
      <div class="filter-group">
        <label>
          <span>状态</span>
          <SearchSelect v-model="statusFilter" :bordered="false">
            <option v-for="option in statusOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </SearchSelect>
        </label>
        <label>
          <span>触发原因</span>
          <SearchSelect v-model="reasonFilter" :bordered="false">
            <option v-for="option in reasonOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </SearchSelect>
        </label>
        <n-button quaternary type="primary" @click="fetchReviews()">刷新</n-button>
      </div>
    </div>

    <div v-if="loading" class="state-panel workstation-glass-panel">
      <n-spin size="large" />
      <p>审核队列加载中…</p>
    </div>

    <div v-else-if="loadError" class="state-panel workstation-glass-panel">
      <h3>审核队列加载失败</h3>
      <p>{{ loadError }}</p>
      <n-button type="primary" @click="fetchReviews()">重试</n-button>
    </div>

    <template v-else>
      <div class="queue-table workstation-glass-panel">
        <table>
          <thead>
            <tr>
              <th>规则名称</th>
              <th>所属任务</th>
              <th>触发原因</th>
              <th>状态</th>
              <th>审核人 / 意见</th>
              <th>创建时间</th>
              <th class="actions-col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in rows" :key="item.review_id">
              <td>
                <div class="rule-name-cell">
                  <strong>{{ item.rule_name }}</strong>
                  <span>{{ item.review_id }}</span>
                </div>
              </td>
              <td>
                <button class="task-link" type="button" @click="goTask(item.task_id)">
                  {{ item.task_name || item.task_id }}
                </button>
              </td>
              <td>
                <span class="reason-badge">{{ reasonText(item.trigger_reason) }}</span>
              </td>
              <td><review-status-tag :status="item.status" /></td>
              <td class="comment-cell">{{ reviewComment(item) }}</td>
              <td class="time-cell">{{ formatDate(item.created_at) }}</td>
              <td>
                <div v-if="item.status === 'pending'" class="review-actions">
                  <button
                    type="button"
                    class="approve"
                    :disabled="operatingId === item.review_id"
                    @click="handleAction(item, 'approve')"
                  >
                    确认
                  </button>
                  <button
                    type="button"
                    class="reject"
                    :disabled="operatingId === item.review_id"
                    @click="handleAction(item, 'reject')"
                  >
                    驳回
                  </button>
                  <button
                    type="button"
                    :disabled="operatingId === item.review_id"
                    @click="handleAction(item, 'recheck')"
                  >
                    转复核
                  </button>
                </div>
                <span v-else class="done-hint">已处理</span>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td class="empty-cell" colspan="7">
                当前筛选下没有审核项；规则执行触发人工确认后会自动进入队列
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pagination-row">
        <span>共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页</span>
        <div>
          <button type="button" :disabled="page <= 1 || loading" @click="goToPage(page - 1)">上一页</button>
          <button type="button" :disabled="page >= totalPages || loading" @click="goToPage(page + 1)">下一页</button>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.review-queue-shell {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.page-heading,
.filter-group,
.rule-name-cell,
.review-actions,
.pagination-row {
  display: flex;
  align-items: center;
}

.page-heading {
  justify-content: space-between;
  gap: 20px;
  padding: 4px 8px 0;
}

.page-heading h1 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 24px;
  line-height: 1.2;
  font-weight: 900;
}

.page-heading p {
  margin: 8px 0 0;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.filter-group {
  gap: 12px;
  flex-wrap: wrap;
}

.filter-group label {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 40px;
  padding: 0 12px;
  border: 1px solid var(--workstation-border);
  border-radius: 12px;
  color: var(--workstation-text-faint);
  font-size: 10px;
  background: var(--workstation-panel-soft);
}

/* 胶囊里的下拉定宽；边框由胶囊提供，下拉本身不带边框（:bordered="false"） */
.filter-group .search-select {
  width: 140px;
}

.state-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  align-items: center;
  justify-content: center;
  min-height: 220px;
  padding: 24px;
  text-align: center;
}

.state-panel h3 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 800;
}

.state-panel p {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 11px;
}

.queue-table {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 15px 18px;
  border-bottom: 1px solid var(--workstation-border);
  color: var(--workstation-text);
  font-size: 11px;
  text-align: left;
  vertical-align: middle;
}

tbody tr:last-child td {
  border-bottom: 0;
}

th {
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-weight: 900;
  text-transform: uppercase;
  white-space: nowrap;
}

.actions-col {
  min-width: 200px;
}

.rule-name-cell {
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.rule-name-cell strong {
  color: var(--workstation-text-strong);
  font-size: 11px;
}

.rule-name-cell span {
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.task-link {
  padding: 0;
  border: 0;
  color: var(--workstation-primary);
  font: inherit;
  font-weight: 600;
  text-align: left;
  background: transparent;
  cursor: pointer;
}

.task-link:hover {
  text-decoration: underline;
}

.reason-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border: 1px solid rgba(167, 139, 250, 0.28);
  border-radius: 999px;
  color: var(--workstation-violet);
  font-size: 10px;
  font-weight: 800;
  white-space: nowrap;
  background: rgba(167, 139, 250, 0.1);
}

.comment-cell {
  max-width: 260px;
  overflow: hidden;
  color: var(--workstation-text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.time-cell {
  color: var(--workstation-text-muted);
  white-space: nowrap;
}

.review-actions {
  gap: 8px;
  flex-wrap: wrap;
}

.review-actions button {
  min-height: 30px;
  padding: 0 12px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  color: var(--workstation-text);
  font-size: 10px;
  font-weight: 800;
  background: var(--workstation-panel-soft);
  cursor: pointer;
}

.review-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.review-actions .approve {
  border-color: rgba(52, 211, 153, 0.3);
  color: var(--workstation-success);
  background: rgba(52, 211, 153, 0.08);
}

.review-actions .reject {
  border-color: rgba(248, 113, 113, 0.3);
  color: var(--workstation-danger);
  background: rgba(248, 113, 113, 0.08);
}

.done-hint {
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.empty-cell {
  padding: 26px;
  color: var(--workstation-text-faint);
  text-align: center;
}

.pagination-row {
  justify-content: space-between;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.pagination-row div {
  display: flex;
  gap: 10px;
}

.pagination-row button {
  min-height: 32px;
  padding: 0 14px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  color: var(--workstation-text-muted);
  font-size: 10px;
  font-weight: 700;
  background: var(--workstation-panel-soft);
  cursor: pointer;
}

.pagination-row button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

@media (max-width: 860px) {
  .page-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
