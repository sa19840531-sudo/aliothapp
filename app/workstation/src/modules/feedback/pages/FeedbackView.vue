<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { useMessage } from 'naive-ui'
import {
  FEEDBACK_KIND_LABELS,
  FEEDBACK_PRIORITY_LABELS,
  FEEDBACK_STATUS_LABELS,
  createFeedback,
  handleFeedback,
  listFeedback,
  type FeedbackKind,
  type FeedbackPriority,
  type FeedbackStatus,
  type FeedbackTicket,
} from '@/services/feedback.api'
import { authStore } from '@/stores/auth'

/**
 * 我的反馈 / 反馈中心（对齐 2.0 原型 feedback.html?tab=my）：提示条 → 筛选卡 → 反馈列表卡。
 * 部门账号提交误报、漏报、规则建议、平台问题并跟踪处理进度；公司管理员与总部在同一页处理。
 * 可见范围由服务端按登录态决定。
 */
const route = useRoute()
const nav = usePortalNav()
const message = useMessage()

const items = ref<FeedbackTicket[]>([])
const total = ref(0)
const page = ref(1)
const PAGE_SIZE = 20
const loading = ref(true)
const loadError = ref('')
const canHandle = ref(false)
const visibility = ref<'all' | 'tenant' | 'own'>('own')
/** 数据范围以服务端返回为准：第一次返回前不显示范围说明，免得先说「只显示我提交的」 */
const scopeKnown = ref(false)
const expandedId = ref<string | null>(null)

const workspaceName = computed(() => authStore.user.value?.tenant_name || '本公司')

const filters = reactive({
  status: '' as FeedbackStatus | '',
  kind: '' as FeedbackKind | '',
  priority: '' as FeedbackPriority | '',
  keyword: '',
})

const KIND_OPTIONS = Object.entries(FEEDBACK_KIND_LABELS) as Array<[FeedbackKind, string]>
const PRIORITY_OPTIONS = Object.entries(FEEDBACK_PRIORITY_LABELS) as Array<[FeedbackPriority, string]>
const STATUS_OPTIONS = Object.entries(FEEDBACK_STATUS_LABELS) as Array<[FeedbackStatus, string]>

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pageNumbers = computed(() => Array.from({ length: pageCount.value }, (_, index) => index + 1))

/** 菜单入口：我的反馈（只看自己提交的）/ 反馈处理（有处理权限的账号看全部待处理）。 */
const activeTab = computed<'my' | 'handle' | ''>(() => {
  const tab = route.query.tab?.toString()
  return tab === 'my' || tab === 'handle' ? tab : ''
})
const tabTitle = computed(() => (activeTab.value === 'handle' ? '反馈处理' : activeTab.value === 'my' ? '我的反馈' : ''))

/** 优先级在当前页内筛选（接口暂不支持该条件）；「我的反馈」只留本人提交的。 */
const visibleItems = computed(() => {
  let list = filters.priority ? items.value.filter((item) => item.priority === filters.priority) : items.value
  if (activeTab.value === 'my') {
    const me = authStore.user.value?.id
    list = me ? list.filter((item) => item.submitter_id === me) : list
  }
  return list
})

const scopeHint = computed(() => {
  if (visibility.value === 'all') return '总部视角：显示全部公司的反馈'
  if (visibility.value === 'tenant') return '显示本公司全部账号提交的反馈'
  return '只显示我提交的反馈'
})

async function load() {
  loading.value = true
  try {
    const result = await listFeedback({
      status: filters.status,
      kind: filters.kind,
      keyword: filters.keyword,
      page: page.value,
      page_size: PAGE_SIZE,
    })
    items.value = result.items
    total.value = result.total
    canHandle.value = result.can_handle
    visibility.value = result.visibility
    scopeKnown.value = true
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '反馈列表加载失败'
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  void load()
}

function resetFilters() {
  filters.status = ''
  filters.kind = ''
  filters.priority = ''
  filters.keyword = ''
  search()
}

function goPage(next: number) {
  page.value = Math.min(Math.max(1, next), pageCount.value)
  void load()
}

function toggleExpanded(id: string) {
  expandedId.value = expandedId.value === id ? null : id
}

// ---------- 提交反馈 ----------

const submitOpen = ref(false)
const submitting = ref(false)
const form = reactive({
  kind: 'false_positive' as FeedbackKind,
  priority: 'medium' as FeedbackPriority,
  title: '',
  description: '',
  task_id: '',
  rule_code: '',
  contact: '',
})

function openSubmit(
  preset: {
    task_id?: string
    rule_code?: string
    kind?: FeedbackKind
    title?: string
    description?: string
  } = {},
) {
  form.task_id = preset.task_id ?? form.task_id
  form.rule_code = preset.rule_code ?? form.rule_code
  if (preset.kind) form.kind = preset.kind
  if (preset.title !== undefined) form.title = preset.title
  if (preset.description !== undefined) form.description = preset.description
  submitOpen.value = true
}

/**
 * 规则中心带过来的反馈（2.1 原型 feedback.html?source=…&name=…）：
 * department-rules = 从规则中心反馈规则问题；custom-rule / custom-page = 申请开启新增的规则 / 页面。
 */
function presetFromSource(): Parameters<typeof openSubmit>[0] | null {
  const source = route.query.source?.toString()
  const name = route.query.name?.toString().trim() ?? ''
  const rule = route.query.rule?.toString() ?? ''
  if (source === 'custom-rule' || source === 'custom-page') {
    const noun = source === 'custom-rule' ? '规则' : '页面'
    return {
      kind: 'rule_suggestion',
      title: name ? `申请开启新增${noun}：${name}` : `申请开启新增${noun}`,
      description: `用户新增${noun}“${name}”，需要管理后台优化识别模型覆盖后再开启。请评估并补充覆盖。`,
    }
  }
  if (source === 'department-rules') {
    return {
      kind: 'rule_suggestion',
      rule_code: rule,
      title: name ? `规则问题：${name}` : '',
      description: name ? `规则「${name}」：` : '',
    }
  }
  return null
}

function closeSubmit() {
  submitOpen.value = false
}

async function submit() {
  if (!form.title.trim()) {
    message.warning('请填写反馈标题')
    return
  }
  if (!form.description.trim()) {
    message.warning('请描述问题或建议')
    return
  }
  submitting.value = true
  try {
    const ticket = await createFeedback({
      kind: form.kind,
      priority: form.priority,
      title: form.title.trim(),
      description: form.description.trim(),
      task_id: form.task_id.trim() || null,
      rule_code: form.rule_code.trim() || null,
      contact: form.contact.trim() || null,
    })
    message.success(`反馈已提交，编号 ${ticket.ticket_no}`)
    submitOpen.value = false
    form.title = ''
    form.description = ''
    form.task_id = ''
    form.rule_code = ''
    page.value = 1
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '反馈提交失败')
  } finally {
    submitting.value = false
  }
}

// ---------- 处理反馈 ----------

const handleTarget = ref<FeedbackTicket | null>(null)
const handling = ref(false)
const handleForm = reactive({
  status: 'processing' as FeedbackStatus,
  reply: '',
})

function openHandle(ticket: FeedbackTicket) {
  handleTarget.value = ticket
  handleForm.status = ticket.status === 'pending' ? 'processing' : ticket.status
  handleForm.reply = ticket.reply ?? ''
}

async function submitHandle() {
  const target = handleTarget.value
  if (!target) return
  handling.value = true
  try {
    await handleFeedback(target.id, {
      status: handleForm.status,
      reply: handleForm.reply.trim() || null,
    })
    message.success(`已更新 ${target.ticket_no}`)
    handleTarget.value = null
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '处理失败')
  } finally {
    handling.value = false
  }
}

function openTask(taskId: string) {
  void nav.push(`/tasks/${taskId}`)
}

function formatDate(value: string | null | undefined) {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

const PRIORITY_TONE: Record<string, string> = {
  urgent: 'tone-danger',
  high: 'tone-orange',
  medium: 'tone-warning',
  low: 'tone-muted',
}

const STATUS_TONE: Record<string, string> = {
  pending: 'tone-warning',
  processing: 'tone-primary',
  resolved: 'tone-success',
  closed: 'tone-muted',
}

onMounted(() => {
  const task = route.query.task?.toString()
  const rule = route.query.rule?.toString()
  const sourcePreset = presetFromSource()
  if (sourcePreset) {
    openSubmit(sourcePreset)
  } else if (task || rule || route.query.submit === '1') {
    openSubmit({ task_id: task ?? '', rule_code: rule ?? '' })
  }
  if (activeTab.value === 'handle') {
    filters.status = 'pending'
  }
  void load()
})

watch(activeTab, (tab) => {
  filters.status = tab === 'handle' ? 'pending' : ''
  search()
})
</script>

<template>
  <div class="workstation-page feedback-page">
    <div class="tip-bar">
      <span class="tip-icon">!</span>
      <strong v-if="tabTitle" class="tab-title" data-testid="feedback-tab-title">{{ tabTitle }}：</strong>
      如遇紧急问题，建议同步在工作群沟通，以便更快响应。<span v-if="scopeKnown" class="qc-muted">{{ scopeHint }}。</span>
    </div>

    <section class="workstation-glass-panel filter-card">
      <!-- 部门平台不展示公司 / 部门，组织范围由登录身份确定（2.1 原型） -->
      <div class="filter-grid" :class="{ 'filter-grid-3': !nav.isAdmin.value }">
        <SearchSelect v-if="nav.isAdmin.value" disabled :title="workspaceName">
          <option>{{ workspaceName }}</option>
        </SearchSelect>
        <SearchSelect v-model="filters.kind" data-testid="filter-kind">
          <option value="">全部类型</option>
          <option v-for="[value, label] in KIND_OPTIONS" :key="value" :value="value">{{ label }}</option>
        </SearchSelect>
        <SearchSelect v-model="filters.status" data-testid="filter-status">
          <option value="">全部状态</option>
          <option v-for="[value, label] in STATUS_OPTIONS" :key="value" :value="value">{{ label }}</option>
        </SearchSelect>
        <SearchSelect v-model="filters.priority">
          <option value="">全部优先级</option>
          <option v-for="[value, label] in PRIORITY_OPTIONS" :key="value" :value="value">{{ label }}</option>
        </SearchSelect>
      </div>
      <div class="filter-row">
        <input
          v-model="filters.keyword"
          class="qc-input keyword"
          type="search"
          placeholder="问题描述 / 关联任务 / 提交账号"
          data-testid="filter-keyword"
          @keyup.enter="search"
        />
        <button type="button" class="qc-btn qc-btn-primary" @click="search">查询</button>
        <button type="button" class="qc-btn" @click="resetFilters">重置</button>
      </div>
    </section>

    <section class="workstation-glass-panel table-card">
      <div class="qc-card-head">
        <h2>反馈列表 <span class="qc-muted small">{{ loading && !items.length ? '加载中…' : `共 ${total} 条` }}</span></h2>
        <button type="button" class="qc-btn qc-btn-primary" data-testid="open-submit" @click="openSubmit()">+ 提交反馈</button>
      </div>

      <p v-if="loading" class="qc-empty">加载中…</p>
      <p v-else-if="loadError" class="qc-empty qc-error">{{ loadError }}</p>
      <p v-else-if="!visibleItems.length" class="qc-empty">还没有反馈记录，点击右上角「提交反馈」</p>
      <div v-else class="table-wrap">
        <table class="qc-table" data-testid="feedback-table">
          <thead>
            <tr>
              <th>反馈编号</th>
              <th>问题描述</th>
              <th v-if="nav.isAdmin.value">所属公司/部门</th>
              <th>产品/关联</th>
              <th>优先级</th>
              <th>状态</th>
              <th>提交时间</th>
              <th v-if="visibility !== 'own'">提交账号</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in visibleItems" :key="item.id">
              <tr>
                <td class="qc-mono ticket-no">{{ item.ticket_no }}</td>
                <td class="title-cell">
                  <strong>{{ item.title }}</strong>
                  <span class="qc-chip tone-muted">{{ FEEDBACK_KIND_LABELS[item.kind] ?? item.kind }}</span>
                </td>
                <td v-if="nav.isAdmin.value">{{ item.tenant_name || workspaceName }}</td>
                <td class="relation">
                  <button v-if="item.task_id" type="button" class="qc-link" @click="openTask(item.task_id)">
                    任务 {{ item.task_id.slice(0, 8) }}
                  </button>
                  <span v-if="item.rule_code" class="qc-mono">{{ item.rule_code }}</span>
                  <span v-if="!item.task_id && !item.rule_code" class="qc-muted">-</span>
                </td>
                <td>
                  <span class="qc-chip" :class="PRIORITY_TONE[item.priority] ?? 'tone-muted'">
                    {{ FEEDBACK_PRIORITY_LABELS[item.priority] ?? item.priority }}
                  </span>
                </td>
                <td>
                  <span class="qc-chip" :class="STATUS_TONE[item.status] ?? 'tone-muted'">
                    {{ FEEDBACK_STATUS_LABELS[item.status] ?? item.status }}
                  </span>
                </td>
                <td class="qc-muted">{{ formatDate(item.created_at) }}</td>
                <td v-if="visibility !== 'own'">{{ item.submitter_name }}</td>
                <td class="is-right actions">
                  <button type="button" class="qc-link" @click="toggleExpanded(item.id)">
                    {{ expandedId === item.id ? '收起' : '详情' }}
                  </button>
                  <button
                    v-if="canHandle"
                    type="button"
                    class="qc-link"
                    :data-testid="`handle-${item.ticket_no}`"
                    @click="openHandle(item)"
                  >
                    处理
                  </button>
                </td>
              </tr>
              <tr v-if="expandedId === item.id" class="detail-row">
                <td :colspan="(visibility !== 'own' ? 9 : 8) - (nav.isAdmin.value ? 0 : 1)">
                  <div class="detail-grid">
                    <div>
                      <span>问题描述</span>
                      <p>{{ item.description }}</p>
                    </div>
                    <div>
                      <span>处理回复</span>
                      <p v-if="item.reply">{{ item.reply }}</p>
                      <p v-else class="qc-muted">尚未回复</p>
                      <small v-if="item.handled_by" class="qc-muted">
                        {{ item.handled_by }} · {{ formatDate(item.handled_at) }}
                      </small>
                    </div>
                    <div>
                      <span>联系方式</span>
                      <p>{{ item.contact || '未留' }}</p>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <div v-if="pageCount > 1" class="qc-pager">
        <span>共 {{ total }} 条</span>
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

    <div v-if="submitOpen" class="modal-backdrop" data-testid="submit-modal" @click.self="closeSubmit">
      <div class="modal-card">
        <h3>提交反馈</h3>
        <div class="form-grid">
          <label>
            <span>反馈类型</span>
            <SearchSelect v-model="form.kind" data-testid="submit-kind">
              <option v-for="[value, label] in KIND_OPTIONS" :key="value" :value="value">{{ label }}</option>
            </SearchSelect>
          </label>
          <label>
            <span>优先级</span>
            <SearchSelect v-model="form.priority" data-testid="submit-priority">
              <option v-for="[value, label] in PRIORITY_OPTIONS" :key="value" :value="value">{{ label }}</option>
            </SearchSelect>
          </label>
          <label class="full">
            <span>标题 <b>*</b></span>
            <input v-model="form.title" class="qc-input" type="text" maxlength="200" placeholder="一句话说清问题" data-testid="submit-title" />
          </label>
          <label class="full">
            <span>问题描述 <b>*</b></span>
            <textarea
              v-model="form.description"
              class="qc-textarea"
              rows="4"
              maxlength="4000"
              placeholder="发生在哪个页面、期望结果是什么；能附上报告里的规则名称更好"
              data-testid="submit-description"
            ></textarea>
          </label>
          <label>
            <span>关联任务</span>
            <input v-model="form.task_id" class="qc-input" type="text" placeholder="任务 ID（选填）" data-testid="submit-task" />
          </label>
          <label>
            <span>关联规则</span>
            <input v-model="form.rule_code" class="qc-input" type="text" placeholder="规则编号（选填）" data-testid="submit-rule" />
          </label>
          <label class="full">
            <span>联系方式</span>
            <input v-model="form.contact" class="qc-input" type="text" placeholder="便于处理人联系你（选填）" data-testid="submit-contact" />
          </label>
        </div>
        <div class="modal-actions">
          <button type="button" class="qc-btn" @click="closeSubmit">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="submitting" data-testid="submit-confirm" @click="submit">
            {{ submitting ? '提交中…' : '提交' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="handleTarget" class="modal-backdrop" data-testid="handle-modal" @click.self="handleTarget = null">
      <div class="modal-card">
        <h3>处理反馈 · {{ handleTarget.ticket_no }}</h3>
        <p class="modal-meta">{{ handleTarget.title }} · {{ handleTarget.submitter_name }}</p>
        <div class="form-grid">
          <label>
            <span>处理状态</span>
            <SearchSelect v-model="handleForm.status" data-testid="handle-status">
              <option v-for="[value, label] in STATUS_OPTIONS" :key="value" :value="value">{{ label }}</option>
            </SearchSelect>
          </label>
          <label class="full">
            <span>回复</span>
            <textarea v-model="handleForm.reply" class="qc-textarea" rows="4" maxlength="4000" placeholder="告诉提交人处理结论" data-testid="handle-reply"></textarea>
          </label>
        </div>
        <div class="modal-actions">
          <button type="button" class="qc-btn" @click="handleTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="handling" data-testid="handle-confirm" @click="submitHandle">
            {{ handling ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.feedback-page {
  gap: 24px;
}

/* 原型：amber-50 提示条 */
.tip-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border: 1px solid rgba(217, 119, 6, 0.3);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-warning-strong);
  background: var(--tint-warning);
  font-size: 13px;
}

.tip-icon {
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  border: 1px solid currentColor;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 700;
}

.filter-card {
  padding: 16px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.filter-grid.filter-grid-3 {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.filter-row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.keyword {
  width: min(360px, 100%);
}

.table-card {
  overflow: hidden;
}

.small {
  font-size: 12px;
  font-weight: 400;
}

.table-wrap {
  overflow-x: auto;
}

.ticket-no {
  color: var(--workstation-text-strong);
  font-weight: 600;
}

.title-cell strong {
  display: block;
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.title-cell .qc-chip {
  margin-top: 4px;
}

.relation {
  white-space: nowrap;
}

.relation .qc-mono {
  margin-left: 8px;
}

.actions {
  white-space: nowrap;
}

.actions .qc-link + .qc-link {
  margin-left: 12px;
}

.detail-row td {
  background: var(--workstation-panel-soft);
}

.detail-grid {
  display: grid;
  grid-template-columns: 2fr 2fr 1fr;
  gap: 16px;
}

.detail-grid span {
  display: block;
  margin-bottom: 4px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.detail-grid p {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(15, 23, 42, 0.45);
}

.modal-card {
  width: min(640px, 100%);
  padding: 22px 24px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel-strong);
  box-shadow: var(--workstation-shadow-lg);
}

.modal-card h3 {
  margin: 0 0 12px;
  color: var(--workstation-text-strong);
  font-size: 16px;
}

.modal-meta {
  margin: -6px 0 12px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 14px;
}

.form-grid label {
  display: grid;
  gap: 6px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.form-grid label.full {
  grid-column: 1 / -1;
}

.form-grid b {
  color: var(--workstation-danger);
}

.qc-textarea {
  resize: vertical;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}

@media (max-width: 900px) {
  .filter-grid,
  .detail-grid,
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
