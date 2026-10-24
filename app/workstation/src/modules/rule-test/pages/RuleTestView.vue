<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage } from 'naive-ui'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { listWorkstationProducts, type WorkstationProductOption } from '@/services/products.api'
import { getTask, listCandidateRules, listTasks } from '@/services/tasks.api'
import {
  getRuleEvaluationRun,
  listRuleEvaluationRuns,
  submitRuleEvaluationRun,
  type RuleEvaluationRunDetail,
  type RuleEvaluationRunSummary,
} from '@/services/rule-test.api'
import type { WorkstationTaskListItem } from '@/types/task'
import type { CandidateRule } from '@/types/rule-execution'
import { RULE_EXECUTION_STATUS_LABELS, SEVERITY_META, TASK_STATUS_LABELS } from '@/utils/status'

/** embedded：嵌在「规则配置 → 规则测试」页签里时不再显示自己的页头。 */
const props = withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })

const message = useMessage()
const route = useRoute()
const requestedTaskId = computed(() => {
  const value = route.query.task
  return typeof value === 'string' ? value : ''
})

const RUN_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '测试中',
  succeeded: '已完成',
  failed: '失败',
  evidence_insufficient: '证据不足',
  cancelled: '已取消',
}

const products = ref<WorkstationProductOption[]>([])
const productsLoading = ref(true)
const productCode = ref('')

// 各列表失败时说「加载失败」，不说「还没有…」；请求序号防止旧请求晚回来覆盖新选择的数据
const tasks = ref<WorkstationTaskListItem[]>([])
const tasksLoading = ref(false)
const tasksError = ref('')
const selectedTaskId = ref('')

const rules = ref<CandidateRule[]>([])
const rulesLoading = ref(false)
const rulesError = ref('')
const checkedCodes = ref<Set<string>>(new Set())

const runs = ref<RuleEvaluationRunSummary[]>([])
const runsLoading = ref(false)
const runsError = ref('')
let tasksSequence = 0
let rulesSequence = 0
let runsSequence = 0
const expandedRunId = ref('')
const runDetails = ref<Record<string, RuleEvaluationRunDetail>>({})
const detailLoadingId = ref('')
const submitting = ref(false)

let pollTimer: number | undefined

const allChecked = computed(
  () => rules.value.length > 0 && checkedCodes.value.size === rules.value.length,
)
const hasActiveRun = computed(() =>
  runs.value.some((run) => run.status === 'pending' || run.status === 'running'),
)

onMounted(() => {
  void loadProducts()
  void loadTasks()
})

onBeforeUnmount(() => window.clearTimeout(pollTimer))

watch(productCode, () => {
  void loadTasks()
})

watch(selectedTaskId, () => {
  rules.value = []
  rulesError.value = ''
  checkedCodes.value = new Set()
  runs.value = []
  runsError.value = ''
  expandedRunId.value = ''
  window.clearTimeout(pollTimer)
  if (selectedTaskId.value) {
    void loadRules()
    void loadRuns()
  }
})

async function loadProducts() {
  try {
    const response = await listWorkstationProducts()
    products.value = response.items
  } catch (error) {
    message.error(error instanceof Error ? error.message : '产品列表加载失败')
  } finally {
    productsLoading.value = false
  }
}

async function loadTasks() {
  const sequence = ++tasksSequence
  tasksLoading.value = true
  tasksError.value = ''
  selectedTaskId.value = ''
  try {
    const response = await listTasks({
      product_code: productCode.value || undefined,
      page_size: 20,
    })
    let taskItems = response.items
    let requestedTask = taskItems.find((item) => item.task_id === requestedTaskId.value)
    if (requestedTaskId.value && !requestedTask) {
      requestedTask = await getTask(requestedTaskId.value)
      taskItems = [requestedTask, ...taskItems]
    }
    if (sequence !== tasksSequence) return
    tasks.value = taskItems
    if (requestedTask) {
      selectedTaskId.value = requestedTask.task_id
    }
  } catch (error) {
    if (sequence !== tasksSequence) return
    tasksError.value = error instanceof Error ? error.message : '任务列表加载失败'
    message.error(tasksError.value)
  } finally {
    if (sequence === tasksSequence) tasksLoading.value = false
  }
}

async function loadRules() {
  const sequence = ++rulesSequence
  rulesLoading.value = true
  rulesError.value = ''
  try {
    const response = await listCandidateRules(selectedTaskId.value)
    if (sequence !== rulesSequence) return
    rules.value = response.items
    checkedCodes.value = new Set(response.items.map((item) => item.rule_code))
  } catch (error) {
    if (sequence !== rulesSequence) return
    rulesError.value = error instanceof Error ? error.message : '规则列表加载失败'
    message.error(rulesError.value)
  } finally {
    if (sequence === rulesSequence) rulesLoading.value = false
  }
}

async function loadRuns(silent = false) {
  if (!selectedTaskId.value) {
    return
  }
  const sequence = ++runsSequence
  if (!silent) {
    runsLoading.value = true
    runsError.value = ''
  }
  try {
    const response = await listRuleEvaluationRuns(selectedTaskId.value)
    if (sequence !== runsSequence) return
    runs.value = response.runs
    runsError.value = ''
  } catch (error) {
    if (sequence !== runsSequence) return
    if (!silent) {
      runsError.value = error instanceof Error ? error.message : '测试记录加载失败'
      message.error(runsError.value)
    }
  } finally {
    if (sequence === runsSequence) {
      runsLoading.value = false
      scheduleNextPoll()
    }
  }
}

function scheduleNextPoll() {
  window.clearTimeout(pollTimer)
  if (selectedTaskId.value && hasActiveRun.value) {
    pollTimer = window.setTimeout(() => void loadRuns(true), 5000)
  }
}

function toggleRule(ruleCode: string) {
  const next = new Set(checkedCodes.value)
  if (next.has(ruleCode)) {
    next.delete(ruleCode)
  } else {
    next.add(ruleCode)
  }
  checkedCodes.value = next
}

function checkAll() {
  checkedCodes.value = new Set(rules.value.map((item) => item.rule_code))
}

function clearAll() {
  checkedCodes.value = new Set()
}

async function handleSubmit() {
  if (!selectedTaskId.value) {
    message.error('请先选择任务')
    return
  }
  if (checkedCodes.value.size === 0) {
    message.error('请至少勾选一条规则')
    return
  }
  submitting.value = true
  try {
    const codes = allChecked.value ? undefined : Array.from(checkedCodes.value)
    const response = await submitRuleEvaluationRun(selectedTaskId.value, codes)
    message.success(`已提交测试，运行编号 …${response.rule_evaluation_run_id.slice(-8)}`)
    await loadRuns()
  } catch (error) {
    message.error(
      `提交测试失败：${error instanceof Error ? error.message : '请稍后重试'}`,
    )
  } finally {
    submitting.value = false
  }
}

function isTerminal(status: string) {
  return status !== 'pending' && status !== 'running'
}

async function toggleRunDetail(run: RuleEvaluationRunSummary) {
  if (expandedRunId.value === run.run_id) {
    expandedRunId.value = ''
    return
  }
  expandedRunId.value = run.run_id
  if (isTerminal(run.status) && !runDetails.value[run.run_id]) {
    detailLoadingId.value = run.run_id
    try {
      runDetails.value[run.run_id] = await getRuleEvaluationRun(
        selectedTaskId.value,
        run.run_id,
      )
    } catch (error) {
      message.error(error instanceof Error ? error.message : '测试结果加载失败')
      expandedRunId.value = ''
    } finally {
      detailLoadingId.value = ''
    }
  }
}

function formatTime(value: string | null) {
  if (!value) {
    return '—'
  }
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function shortText(value: unknown, limit = 80) {
  const text = String(value ?? '').trim()
  if (!text) {
    return '—'
  }
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}
</script>

<template>
  <section class="workstation-page rule-test-page">
    <header v-if="!props.embedded" class="page-head">
      <h1>规则测试</h1>
      <p>用任务已留存的页面证据单独重测规则，不重新跑页面，也不影响任务的正式结果和报告</p>
    </header>

    <div class="workstation-glass-panel panel">
      <h2 class="panel-title">1 选择证据来源</h2>
      <div class="picker-row">
        <label class="picker-field">
          <span>产品</span>
          <SearchSelect v-model="productCode" data-test="product-select">
            <option value="">{{ productsLoading ? '产品加载中…' : '全部产品' }}</option>
            <option
              v-for="product in products"
              :key="product.project_code"
              :value="product.project_code"
            >
              {{ product.project_code }} · {{ product.product_name }}
            </option>
          </SearchSelect>
        </label>
        <label class="picker-field task-field">
          <span>任务</span>
          <SearchSelect
            v-model="selectedTaskId"
            data-test="task-select"
            :disabled="tasksLoading"
          >
            <option value="">{{ tasksLoading ? '任务加载中…' : '请选择任务' }}</option>
            <option v-for="task in tasks" :key="task.task_id" :value="task.task_id">
              {{ task.task_name }} · {{ TASK_STATUS_LABELS[task.status] ?? task.status }} ·
              {{ formatTime(task.created_at) }}
            </option>
          </SearchSelect>
        </label>
      </div>
      <p v-if="!tasksLoading && tasksError" class="empty-block">{{ tasksError }}</p>
      <p v-else-if="!tasksLoading && tasks.length === 0" class="empty-block">
        {{ productCode ? '该产品下还没有任务，先去「任务中心」跑一次质检' : '还没有任务，先去「任务中心」创建一条' }}
      </p>
      <small class="hint">选一条跑过的任务当证据来源；「仅页面探索」任务开放后也能在这里选</small>
    </div>

    <div v-if="selectedTaskId" class="workstation-glass-panel panel">
      <div class="panel-head-row">
        <h2 class="panel-title">2 选择要测的规则</h2>
        <div class="rule-actions">
          <span class="count-chip">{{ rulesLoading ? '规则加载中…' : `已选 ${checkedCodes.size} / ${rules.length}` }}</span>
          <button class="ghost-button" type="button" @click="checkAll">全选</button>
          <button class="ghost-button" type="button" @click="clearAll">清空</button>
        </div>
      </div>
      <p v-if="rulesLoading" class="empty-block">规则加载中…</p>
      <p v-else-if="rulesError" class="empty-block">{{ rulesError }}</p>
      <p v-else-if="rules.length === 0" class="empty-block">
        该任务还没有识别出页面，暂无可测规则（任务需要先完成一次页面探索）
      </p>
      <div v-else class="table-wrap">
        <table class="data-table rules-table">
          <thead>
            <tr>
              <th class="check-col"></th>
              <th>规则编码</th>
              <th>规则名称</th>
              <th>分类</th>
              <th>严重度</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="rule in rules"
              :key="rule.rule_code"
              class="rule-row"
              @click="toggleRule(rule.rule_code)"
            >
              <td class="check-col">
                <input
                  type="checkbox"
                  :checked="checkedCodes.has(rule.rule_code)"
                  @click.stop="toggleRule(rule.rule_code)"
                />
              </td>
              <td class="mono">{{ rule.rule_code }}</td>
              <td>{{ rule.name }}</td>
              <td>{{ rule.category }}</td>
              <td>
                <span class="severity-chip" :class="SEVERITY_META[rule.severity]?.cls">
                  {{ SEVERITY_META[rule.severity]?.label ?? rule.severity }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="submit-row">
        <button
          class="primary-button submit-run-button"
          type="button"
          :disabled="submitting || checkedCodes.size === 0"
          @click="handleSubmit"
        >
          {{ submitting ? '提交中…' : '开始测试' }}
        </button>
        <small class="hint">测试在后台运行，结果只记录在下方列表，不会写进任务的正式结果</small>
      </div>
    </div>

    <div v-if="selectedTaskId" class="workstation-glass-panel panel">
      <div class="panel-head-row">
        <h2 class="panel-title">3 测试记录</h2>
        <button class="ghost-button" type="button" @click="() => void loadRuns()">刷新</button>
      </div>
      <p v-if="runsLoading" class="empty-block">测试记录加载中…</p>
      <p v-else-if="runsError" class="empty-block">{{ runsError }}</p>
      <p v-else-if="runs.length === 0" class="empty-block">
        还没有测试记录，选好规则点「开始测试」
      </p>
      <div v-else class="table-wrap">
        <table class="data-table runs-table">
          <thead>
            <tr>
              <th>状态</th>
              <th>测的规则</th>
              <th>页面数</th>
              <th>结果数</th>
              <th>提交时间</th>
              <th>完成时间</th>
              <th>说明</th>
              <th class="action-col"></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="run in runs" :key="run.run_id">
              <tr>
                <td>
                  <span class="status-chip" :class="`run-${run.status}`">
                    {{ RUN_STATUS_LABELS[run.status] ?? run.status }}
                  </span>
                </td>
                <td>
                  {{ run.requested_rule_codes.length === 0 ? '全部命中规则' : `指定 ${run.requested_rule_codes.length} 条` }}
                </td>
                <td>{{ run.page_count }}</td>
                <td>{{ run.rule_count }}</td>
                <td>{{ formatTime(run.created_at) }}</td>
                <td>{{ formatTime(run.finished_at) }}</td>
                <td class="reason-cell">{{ shortText(run.error_message, 40) }}</td>
                <td class="action-col">
                  <button
                    v-if="isTerminal(run.status)"
                    class="ghost-button"
                    type="button"
                    @click="toggleRunDetail(run)"
                  >
                    {{ expandedRunId === run.run_id ? '收起' : '查看结果' }}
                  </button>
                </td>
              </tr>
              <tr v-if="expandedRunId === run.run_id" class="detail-row">
                <td colspan="8">
                  <p v-if="detailLoadingId === run.run_id" class="empty-block">结果加载中…</p>
                  <template v-else-if="runDetails[run.run_id]">
                    <p v-if="runDetails[run.run_id].results.length === 0" class="empty-block">
                      本次没有产生规则结果
                    </p>
                    <div v-else class="table-wrap">
                      <table class="data-table result-table">
                        <thead>
                          <tr>
                            <th>规则编码</th>
                            <th>判定结果</th>
                            <th>结论摘要</th>
                            <th>问题描述</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            v-for="(item, index) in runDetails[run.run_id].results"
                            :key="`${run.run_id}-${index}`"
                          >
                            <td class="mono">{{ item.rule_code }}</td>
                            <td>
                              {{
                                RULE_EXECUTION_STATUS_LABELS[String(item.execution_status)] ??
                                item.execution_status
                              }}
                            </td>
                            <td>{{ shortText(item.result_summary) }}</td>
                            <td>{{ shortText(item.problem_desc) }}</td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </template>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 颜色一律取主题令牌：浅色 / 暗黑两套皮肤下都可读 */
.rule-test-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head h1 {
  margin: 0 0 4px;
  color: var(--workstation-text-strong);
  font-size: 20px;
}

.page-head p {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.panel {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-title {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.panel-head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.picker-row {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.picker-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--workstation-text-muted);
  min-width: 260px;
}

.task-field {
  flex: 1;
}

.hint {
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.count-chip {
  font-size: 11px;
  color: var(--workstation-text-muted);
  margin-right: 4px;
}

.rule-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ghost-button {
  background: transparent;
  border: 1px solid var(--workstation-border);
  border-radius: 6px;
  color: var(--workstation-text);
  padding: 4px 12px;
  font-size: 11px;
  cursor: pointer;
}

.ghost-button:hover {
  border-color: var(--workstation-primary);
  color: var(--workstation-primary);
}

.primary-button {
  background: var(--workstation-primary);
  border: 1px solid var(--workstation-primary);
  border-radius: 6px;
  color: #ffffff;
  padding: 8px 22px;
  font-size: 12px;
  cursor: pointer;
}

.primary-button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.submit-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.table-wrap {
  overflow-x: auto;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  color: var(--workstation-text);
}

.data-table th,
.data-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--workstation-border);
  white-space: nowrap;
}

.data-table th {
  color: var(--workstation-text-muted);
  background: var(--workstation-table-head-bg);
}

.data-table td.reason-cell,
.result-table td {
  white-space: normal;
}

.rule-row {
  cursor: pointer;
}

.rule-row:hover {
  background: var(--workstation-table-hover-bg);
}

.check-col {
  width: 32px;
}

.action-col {
  width: 88px;
  text-align: right;
}

.mono {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.status-chip {
  display: inline-block;
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 11px;
  border: 1px solid var(--workstation-border);
  color: var(--workstation-text-muted);
}

.run-succeeded {
  color: var(--workstation-success-strong);
  border-color: var(--workstation-success);
}

.run-failed {
  color: var(--workstation-danger);
  border-color: var(--workstation-danger);
}

.run-evidence_insufficient {
  color: var(--workstation-orange);
  border-color: var(--workstation-orange);
}

.run-pending,
.run-running {
  color: var(--workstation-primary);
  border-color: var(--workstation-primary);
}

.detail-row td {
  background: var(--workstation-panel-soft);
}

.empty-block {
  margin: 0;
  padding: 18px 12px;
  text-align: center;
  color: var(--workstation-text-muted);
  font-size: 12px;
}
</style>
