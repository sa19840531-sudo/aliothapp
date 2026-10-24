<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  deleteRuleTestRecords,
  listRuleTestRecords,
  type RuleTestPrefill,
  type RuleTestRecordSummary,
} from '../api/ruleTests'
import RuleTestRecordDrawer from './RuleTestRecordDrawer.vue'
import {
  RULE_TEST_OUTCOMES,
  formatRuleTestTime,
  ruleTestOutcomeLabel,
  ruleTestOutcomeTone,
  ruleTestPlatformLabel,
} from './ruleTestOutcome'
import SearchSelect from '@/components/pickers/SearchSelect.vue'

/**
 * 规则测试 · 测试记录：每次测试的证据和结论都留在这里，体验中心管理员都能看。
 * 不自动清理，按勾选或按时间段批量删除（连同截图，删了找不回来）。
 */
const props = defineProps<{
  /** 板块当前是否可见；每次切回来重新拉，刚测完的记录立刻出现 */
  active: boolean
}>()
const emit = defineEmits<{ retest: [prefill: RuleTestPrefill] }>()

const PAGE_SIZE = 20

const message = useMessage()
const loading = ref(false)
const items = ref<RuleTestRecordSummary[]>([])
const total = ref(0)
const filters = reactive({ keyword: '', outcome: '', dateFrom: '', dateTo: '', page: 1 })
const selected = ref<Set<string>>(new Set())
const viewingId = ref<string | null>(null)

const confirmSelectedOpen = ref(false)
const timeDelete = reactive({ open: false, dateFrom: '', dateTo: '', count: null as number | null })
let countSeq = 0
const deleting = ref(false)

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const allOnPageSelected = computed(
  () => items.value.length > 0 && items.value.every((item) => selected.value.has(item.id)),
)
const timeRangeProblem = computed(() => rangeProblem(timeDelete.dateFrom, timeDelete.dateTo))

watch(
  () => props.active,
  (active) => {
    if (active) void load()
  },
  { immediate: true },
)
watch(
  () => filters.outcome,
  () => search(),
)
watch(
  () => [timeDelete.open, timeDelete.dateFrom, timeDelete.dateTo],
  () => {
    if (timeDelete.open) void countTimeRange()
  },
)

/** 日期框给的是本地日期：开始日 0 点（含）到结束日次日 0 点（不含） */
function dayStart(date: string) {
  return date ? new Date(`${date}T00:00:00`).toISOString() : undefined
}

function dayEnd(date: string) {
  if (!date) return undefined
  const end = new Date(`${date}T00:00:00`)
  end.setDate(end.getDate() + 1)
  return end.toISOString()
}

function rangeProblem(dateFrom: string, dateTo: string): string | null {
  if (dateFrom && dateTo && dateFrom > dateTo) return '开始日期不能晚于结束日期'
  return null
}

async function load() {
  const problem = rangeProblem(filters.dateFrom, filters.dateTo)
  if (problem) {
    message.error(problem)
    return
  }
  loading.value = true
  try {
    const page = await listRuleTestRecords({
      keyword: filters.keyword.trim() || undefined,
      outcome: filters.outcome || undefined,
      createdFrom: dayStart(filters.dateFrom),
      createdTo: dayEnd(filters.dateTo),
      page: filters.page,
      pageSize: PAGE_SIZE,
    })
    items.value = page.items
    total.value = page.total
    // 只保留还在本页的勾选，免得删到看不见的记录
    const visible = new Set(page.items.map((item) => item.id))
    selected.value = new Set([...selected.value].filter((id) => visible.has(id)))
  } catch (error) {
    message.error(error instanceof Error ? error.message : '测试记录加载失败')
  } finally {
    loading.value = false
  }
}

function search() {
  filters.page = 1
  void load()
}

function goPage(page: number) {
  filters.page = Math.min(Math.max(1, page), pageCount.value)
  void load()
}

function toggleRow(id: string, checked: boolean) {
  const next = new Set(selected.value)
  if (checked) next.add(id)
  else next.delete(id)
  selected.value = next
}

function toggleAll(checked: boolean) {
  selected.value = checked ? new Set(items.value.map((item) => item.id)) : new Set()
}

function evidenceText(item: RuleTestRecordSummary) {
  const parts: string[] = []
  if (item.screenshot_count) parts.push(`截图 ${item.screenshot_count} 张`)
  if (item.has_visible_text) parts.push('文字')
  if (item.has_dom) parts.push('源码')
  if (item.has_ui_snapshot) parts.push('界面快照')
  if (item.has_judge_input) parts.push('补充输入')
  return parts.join(' · ') || '—'
}

function openTimeDelete() {
  timeDelete.dateFrom = filters.dateFrom
  timeDelete.dateTo = filters.dateTo
  timeDelete.count = null
  timeDelete.open = true
}

/** 删之前先数一下这段时间有几条，让人心里有数 */
async function countTimeRange() {
  countSeq += 1
  const seq = countSeq
  timeDelete.count = null
  if ((!timeDelete.dateFrom && !timeDelete.dateTo) || timeRangeProblem.value) return
  try {
    const page = await listRuleTestRecords({
      createdFrom: dayStart(timeDelete.dateFrom),
      createdTo: dayEnd(timeDelete.dateTo),
      page: 1,
      pageSize: 1,
    })
    if (seq === countSeq) timeDelete.count = page.total
  } catch (error) {
    if (seq === countSeq) message.error(error instanceof Error ? error.message : '统计条数失败')
  }
}

async function confirmDeleteSelected() {
  await runDelete({ recordIds: [...selected.value] })
  confirmSelectedOpen.value = false
}

async function confirmDeleteByTime() {
  await runDelete({
    createdFrom: dayStart(timeDelete.dateFrom),
    createdTo: dayEnd(timeDelete.dateTo),
  })
  timeDelete.open = false
}

async function runDelete(input: Parameters<typeof deleteRuleTestRecords>[0]) {
  deleting.value = true
  try {
    const { deleted } = await deleteRuleTestRecords(input)
    message.success(`已删除 ${deleted} 条测试记录`)
    selected.value = new Set()
    await load()
    // 删完这一页空了就退到最后一页
    if (filters.page > pageCount.value) goPage(pageCount.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '删除失败')
  } finally {
    deleting.value = false
  }
}

function handleRetest(prefill: RuleTestPrefill) {
  viewingId.value = null
  emit('retest', prefill)
}
</script>

<template>
  <section class="workstation-glass-panel rr-panel" data-testid="rule-test-records">
    <div class="rr-filters">
      <input
        v-model="filters.keyword"
        class="qc-input"
        placeholder="搜索规则编码或名称"
        data-testid="rule-test-records-keyword"
        @keyup.enter="search"
      />
      <SearchSelect v-model="filters.outcome" data-testid="rule-test-records-outcome">
        <option value="">全部结论</option>
        <option v-for="outcome in RULE_TEST_OUTCOMES" :key="outcome" :value="outcome">
          {{ ruleTestOutcomeLabel(outcome) }}
        </option>
      </SearchSelect>
      <div class="rr-dates">
        <input v-model="filters.dateFrom" class="qc-input" type="date" aria-label="开始日期" />
        <span>至</span>
        <input v-model="filters.dateTo" class="qc-input" type="date" aria-label="结束日期" />
      </div>
      <button type="button" class="qc-btn" data-testid="rule-test-records-search" @click="search">
        搜索
      </button>
    </div>

    <div class="rr-toolbar">
      <span class="rr-muted">
        每次测试都会自动存一条，不会自动清理；不需要的可以勾选删除，或者按时间段删除。
      </span>
      <div class="rr-toolbar-actions">
        <button
          type="button"
          class="qc-btn qc-btn-sm rr-danger"
          :disabled="selected.size === 0 || deleting"
          data-testid="rule-test-records-delete-selected"
          @click="confirmSelectedOpen = true"
        >
          删除选中（{{ selected.size }}）
        </button>
        <button
          type="button"
          class="qc-btn qc-btn-sm rr-danger"
          :disabled="deleting"
          data-testid="rule-test-records-delete-by-time"
          @click="openTimeDelete"
        >
          按时间删除
        </button>
      </div>
    </div>

    <div class="rr-table-scroll">
      <table class="qc-table" data-testid="rule-test-records-table">
        <thead>
          <tr>
            <th class="rr-check">
              <input
                type="checkbox"
                :checked="allOnPageSelected"
                :disabled="items.length === 0"
                aria-label="全选本页"
                data-testid="rule-test-records-select-all"
                @change="toggleAll(($event.target as HTMLInputElement).checked)"
              />
            </th>
            <th>测试时间</th>
            <th>规则</th>
            <th>结论</th>
            <th>证据</th>
            <th>页面</th>
            <th>测试人</th>
            <th class="is-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="8" class="rr-empty">测试记录加载中…</td>
          </tr>
          <tr v-else-if="items.length === 0">
            <td colspan="8" class="rr-empty">还没有测试记录；在「人工上传证据」里测一次就会出现在这里</td>
          </tr>
          <tr v-for="item in items" v-else :key="item.id" :data-testid="`rule-test-record-${item.id}`">
            <td class="rr-check">
              <input
                type="checkbox"
                :checked="selected.has(item.id)"
                :aria-label="`选中 ${item.rule_code} 的测试记录`"
                @change="toggleRow(item.id, ($event.target as HTMLInputElement).checked)"
              />
            </td>
            <td class="rr-nowrap">{{ formatRuleTestTime(item.created_at) }}</td>
            <td class="rr-rule">
              <span class="rr-mono">{{ item.rule_code }}</span>
              <span>{{ item.rule_name }}</span>
            </td>
            <td>
              <span class="qc-chip" :class="`tone-${ruleTestOutcomeTone(item.outcome)}`">
                {{ ruleTestOutcomeLabel(item.outcome) }}
              </span>
              <p class="rr-summary" :title="item.result_summary">{{ item.result_summary }}</p>
            </td>
            <td class="rr-nowrap">{{ evidenceText(item) }}</td>
            <td class="rr-page" :title="item.page_url">
              <span>{{ ruleTestPlatformLabel(item.platform) }}</span>
              <span v-if="item.page_title || item.page_url" class="rr-ellipsis">
                {{ item.page_title || item.page_url }}
              </span>
            </td>
            <td class="rr-nowrap">{{ item.tester_name }}</td>
            <td class="is-right">
              <button type="button" class="qc-link" @click="viewingId = item.id">查看</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="qc-pager">
      <span>{{ loading && !items.length ? '加载中…' : `共 ${total} 条测试记录` }}</span>
      <div class="rr-pager-actions">
        <button
          type="button"
          class="qc-btn qc-btn-sm"
          :disabled="filters.page <= 1"
          @click="goPage(filters.page - 1)"
        >
          上一页
        </button>
        <span>{{ filters.page }} / {{ pageCount }}</span>
        <button
          type="button"
          class="qc-btn qc-btn-sm"
          :disabled="filters.page >= pageCount"
          @click="goPage(filters.page + 1)"
        >
          下一页
        </button>
      </div>
    </div>

    <RuleTestRecordDrawer
      :record-id="viewingId"
      @close="viewingId = null"
      @retest="handleRetest"
    />

    <div
      v-if="confirmSelectedOpen"
      class="ap-modal-backdrop"
      data-testid="rule-test-records-confirm-selected"
      @click.self="confirmSelectedOpen = false"
    >
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>删除选中的测试记录</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="confirmSelectedOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <p class="rr-confirm">
            确认删除选中的 {{ selected.size }} 条测试记录？证据截图会一起删掉，删了找不回来。
          </p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="confirmSelectedOpen = false">取消</button>
          <button
            type="button"
            class="qc-btn qc-btn-primary"
            :disabled="deleting"
            data-testid="rule-test-records-confirm-selected-ok"
            @click="confirmDeleteSelected"
          >
            {{ deleting ? '删除中…' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="timeDelete.open"
      class="ap-modal-backdrop"
      data-testid="rule-test-records-confirm-time"
      @click.self="timeDelete.open = false"
    >
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>按时间删除测试记录</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="timeDelete.open = false">×</button>
        </div>
        <div class="ap-modal-body">
          <p class="rr-confirm">
            选一个时间段，这段时间里的测试记录全部删除（不管上面筛了什么规则和结论）。只填结束日期就是删掉这天及以前的全部记录。
          </p>
          <div class="rr-dates">
            <input
              v-model="timeDelete.dateFrom"
              class="qc-input"
              type="date"
              aria-label="开始日期"
              data-testid="rule-test-records-time-from"
            />
            <span>至</span>
            <input
              v-model="timeDelete.dateTo"
              class="qc-input"
              type="date"
              aria-label="结束日期"
              data-testid="rule-test-records-time-to"
            />
          </div>
          <p v-if="timeRangeProblem" class="ap-note is-warning">{{ timeRangeProblem }}</p>
          <p v-else-if="!timeDelete.dateFrom && !timeDelete.dateTo" class="rr-muted">
            请至少选一个日期
          </p>
          <p v-else-if="timeDelete.count === null" class="rr-muted">正在统计这段时间的记录…</p>
          <p v-else class="rr-confirm" data-testid="rule-test-records-time-count">
            这段时间共有 {{ timeDelete.count }} 条测试记录，证据截图会一起删掉，删了找不回来。
          </p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="timeDelete.open = false">取消</button>
          <button
            type="button"
            class="qc-btn qc-btn-primary"
            :disabled="deleting || !timeDelete.count"
            data-testid="rule-test-records-confirm-time-ok"
            @click="confirmDeleteByTime"
          >
            {{ deleting ? '删除中…' : timeDelete.count === null ? '正在统计…' : `删除这 ${timeDelete.count} 条` }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.rr-panel {
  padding: 0;
  overflow: hidden;
}

.rr-filters {
  display: grid;
  grid-template-columns: minmax(200px, 1fr) 180px auto auto;
  gap: 12px;
  padding: 16px 20px 8px;
}

.rr-dates {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.rr-dates .qc-input {
  width: 150px;
}

.rr-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 20px 14px;
}

.rr-toolbar-actions {
  display: flex;
  gap: 8px;
}

.rr-danger:not(:disabled) {
  border-color: rgba(220, 38, 38, 0.35);
  color: var(--workstation-danger);
}

.rr-table-scroll {
  overflow-x: auto;
}

.rr-check {
  width: 36px;
  padding-right: 0 !important;
}

.rr-empty {
  padding: 32px 20px;
  color: var(--workstation-text-muted);
  text-align: center;
}

.rr-nowrap {
  white-space: nowrap;
}

.rr-rule {
  min-width: 200px;
}

.rr-rule span {
  display: block;
}

.rr-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px;
  color: var(--workstation-text-muted);
}

.rr-summary {
  max-width: 280px;
  margin: 4px 0 0;
  overflow: hidden;
  color: var(--workstation-text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rr-page {
  max-width: 240px;
}

.rr-page span {
  display: block;
}

.rr-ellipsis {
  overflow: hidden;
  color: var(--workstation-text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rr-pager-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rr-confirm {
  margin: 0;
  color: var(--workstation-text);
  font-size: 14px;
  line-height: 1.7;
}

.rr-muted {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 1100px) {
  .rr-filters {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
