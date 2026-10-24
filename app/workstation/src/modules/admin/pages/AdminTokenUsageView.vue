<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import {
  exportDepartmentCostReport,
  getDepartmentCostDetail,
  getDepartmentCostReport,
  type DepartmentCostReport,
  type DepartmentCostRow,
  type DepartmentCostTaskRow,
} from '../api/tokenUsage'
import SearchSelect from '@/components/pickers/SearchSelect.vue'

/**
 * 部门 Token 消耗与费用（对齐 2.1 原型 token-usage.html）：月份/供应商筛选 → 五个指标卡 →
 * 部门费用明细表（带预算进度）+ 当前价格版本 / 费用口径待确认 / 预警规则。
 * 金额来自用量事件落库时按当时单价算出的费用，单价没配置时按 0 计并明确提示。
 */
const router = useRouter()
const message = useMessage()

const filters = reactive({ month: '', provider: '' })
const report = ref<DepartmentCostReport | null>(null)
const loading = ref(true)
/** 首次还没拿到报表：数字先显示「…」，不让 0 和「上月无数据」看起来像最终结果 */
const pending = computed(() => loading.value && !report.value)
const loadError = ref('')
const keyword = ref('')
const showPriceHistory = ref(false)

async function load() {
  loading.value = true
  try {
    report.value = await getDepartmentCostReport({
      month: filters.month || undefined,
      provider: filters.provider || undefined,
    })
    if (!filters.month) filters.month = report.value.month
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Token 费用加载失败'
  } finally {
    loading.value = false
  }
}

function formatTokens(value: number | null | undefined) {
  const amount = value ?? 0
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1)}M`
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1)}K`
  return String(amount)
}

function formatMoney(value: number | null | undefined, currency = 'CNY') {
  const symbol = currency === 'CNY' ? '¥' : `${currency} `
  return `${symbol}${(value ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatPrice(value: number) {
  return `¥${value % 1 === 0 ? value : value.toFixed(2)} / 1M`
}

function formatMonthLabel(key: string) {
  const [year, month] = key.split('-')
  return `${year} 年 ${Number(month)} 月`
}

function formatTime(value: string | null | undefined, withDate = false) {
  if (!value) return '—'
  const date = new Date(value)
  return withDate
    ? date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function changeText(percent: number | null) {
  if (percent === null) return '上月无数据'
  const sign = percent > 0 ? '+' : ''
  return `较上月 ${sign}${percent}%`
}

function changeTone(percent: number | null) {
  if (percent === null || percent === 0) return ''
  return percent > 0 ? 'is-down' : 'is-up'
}

const rows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  const list = report.value?.rows ?? []
  if (!needle) return list
  return list.filter(
    (row) => row.tenant_name.toLowerCase().includes(needle) || row.department_name.toLowerCase().includes(needle),
  )
})

const BUDGET_TONE: Record<DepartmentCostRow['budget_level'], string> = {
  none: '',
  normal: '',
  warning: 'is-warning',
  critical: 'is-warning',
  over: 'is-danger',
}

function budgetPercentText(row: DepartmentCostRow) {
  return row.budget_used_percent === null ? '未设预算' : `${row.budget_used_percent}%`
}

async function exportReport() {
  try {
    await exportDepartmentCostReport({ month: filters.month || undefined, provider: filters.provider || undefined })
  } catch (error) {
    message.error(error instanceof Error ? error.message : '导出失败')
  }
}

// ---------- 部门明细弹层 ----------

const detailTarget = ref<DepartmentCostRow | null>(null)
const detailRows = ref<DepartmentCostTaskRow[]>([])
const detailLoading = ref(false)

async function openDetail(row: DepartmentCostRow) {
  detailTarget.value = row
  // 先清掉上一个部门的明细，免得加载期间显示别的部门的数据
  detailRows.value = []
  detailLoading.value = true
  try {
    detailRows.value = await getDepartmentCostDetail({
      month: filters.month || undefined,
      provider: filters.provider || undefined,
      tenant_id: row.tenant_id,
      department_name: row.department_name,
    })
  } catch (error) {
    message.error(error instanceof Error ? error.message : '明细加载失败')
    detailRows.value = []
  } finally {
    detailLoading.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="ap-page" data-testid="admin-token-usage">
    <p class="qc-muted stamp">统计口径更新于 {{ pending ? '加载中…' : formatTime(report?.generated_at) }}</p>

    <div class="ap-head">
      <div>
        <h1 class="title-sm">部门 Token 消耗与费用</h1>
        <p>按部门、任务、模型和供应商核算资源消耗；历史费用按当时生效的价格版本保存。</p>
      </div>
      <div class="ap-head-actions">
        <SearchSelect v-model="filters.month" class="control" data-testid="token-month" @change="load">
          <option v-for="month in report?.months ?? [filters.month]" :key="month" :value="month">{{ month ? formatMonthLabel(month) : pending ? '加载中…' : '—' }}</option>
        </SearchSelect>
        <SearchSelect v-model="filters.provider" class="control" data-testid="token-provider" @change="load">
          <option value="">全部供应商</option>
          <option v-for="provider in report?.providers ?? []" :key="provider" :value="provider">{{ provider }}</option>
        </SearchSelect>
        <button type="button" class="qc-btn" data-testid="token-export" @click="exportReport">导出费用明细</button>
      </div>
    </div>

    <p v-if="loadError" class="qc-error" data-testid="token-error">{{ loadError }}</p>

    <section class="ap-kpis kpi-five" data-testid="token-kpis">
      <div class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">总 Token</div>
        <div class="ap-kpi-value">{{ pending ? '…' : formatTokens(report?.totals.total_tokens) }}</div>
        <div v-if="!pending" class="ap-kpi-hint" :class="changeTone(report?.token_change_percent ?? null)">{{ changeText(report?.token_change_percent ?? null) }}</div>
      </div>
      <div class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">输入 Token</div>
        <div class="ap-kpi-value">{{ pending ? '…' : formatTokens(report?.totals.input_tokens) }}</div>
        <div v-if="!pending" class="ap-kpi-hint">缓存命中 {{ formatTokens(report?.totals.cache_hit_tokens) }}</div>
      </div>
      <div class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">输出 Token</div>
        <div class="ap-kpi-value">{{ pending ? '…' : formatTokens(report?.totals.output_tokens) }}</div>
        <div v-if="!pending" class="ap-kpi-hint">{{ (report?.totals.ai_calls ?? 0).toLocaleString('zh-CN') }} 次调用</div>
      </div>
      <div class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">本月总金额</div>
        <div class="ap-kpi-value is-primary">{{ pending ? '…' : formatMoney(report?.totals.total_cost, report?.totals.currency) }}</div>
        <div v-if="!pending" class="ap-kpi-hint" :class="report?.totals.unpriced_calls ? 'is-down' : ''">
          <template v-if="report?.totals.unpriced_calls">{{ report.totals.unpriced_calls }} 次调用单价未配置，按 0 计</template>
          <template v-else>{{ changeText(report?.cost_change_percent ?? null) }}</template>
        </div>
      </div>
      <div class="workstation-glass-panel ap-kpi">
        <div class="ap-kpi-label">预算预警部门</div>
        <div class="ap-kpi-value is-warning">{{ pending ? '…' : (report?.alert_department_count ?? 0) }}</div>
        <div v-if="!pending" class="ap-kpi-hint">{{ report?.over_90_department_count ?? 0 }} 个已超过 90%</div>
      </div>
    </section>

    <div class="usage-grid">
      <section class="workstation-glass-panel" data-testid="token-table">
        <div class="qc-card-head">
          <div>
            <h2 class="card-title">部门费用明细</h2>
            <p class="card-sub">金额 = 输入 Token × 输入单价 + 输出 Token × 输出单价 + 缓存命中 × 缓存单价</p>
          </div>
          <input v-model="keyword" class="qc-input search" type="search" placeholder="搜索公司或部门" />
        </div>
        <div class="ap-table-wrap">
          <table class="qc-table">
            <thead>
              <tr>
                <th>公司 / 部门</th>
                <th class="is-right">输入 Token</th>
                <th class="is-right">输出 Token</th>
                <th class="is-right">调用次数</th>
                <th class="is-right">金额</th>
                <th>预算</th>
                <th class="is-right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!rows.length">
                <td colspan="7" class="qc-empty">{{ loading ? '加载中…' : '本月还没有模型调用记录' }}</td>
              </tr>
              <tr v-for="row in rows" :key="`${row.tenant_id}-${row.department_name}`">
                <td>
                  <div class="dept-name">{{ row.tenant_name }} · {{ row.department_name }}</div>
                  <div class="qc-muted dept-sub">主要使用 {{ row.main_provider || '—' }}<template v-if="row.main_model"> · {{ row.main_model }}</template></div>
                </td>
                <td class="is-right">{{ formatTokens(row.input_tokens) }}</td>
                <td class="is-right">{{ formatTokens(row.output_tokens) }}</td>
                <td class="is-right">{{ row.ai_calls.toLocaleString('zh-CN') }}</td>
                <td class="is-right"><strong>{{ formatMoney(row.total_cost, report?.totals.currency) }}</strong></td>
                <td class="budget-cell">
                  <div class="budget-meta" :class="BUDGET_TONE[row.budget_level]">
                    <span>{{ budgetPercentText(row) }}</span>
                    <span v-if="row.monthly_budget !== null">{{ formatMoney(row.monthly_budget, report?.totals.currency) }}</span>
                  </div>
                  <div class="ap-progress" :class="BUDGET_TONE[row.budget_level]">
                    <i :style="{ width: `${Math.min(100, row.budget_used_percent ?? 0)}%` }"></i>
                  </div>
                </td>
                <td class="is-right">
                  <button type="button" class="qc-link" @click="openDetail(row)">查看明细</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="qc-pager">
          <span>{{ pending ? '加载中…' : `共 ${rows.length} 个部门 · ${report?.totals.task_count ?? 0} 个任务` }}</span>
          <button type="button" class="qc-link" @click="router.push('/admin/cost-reports')">查看模型调用明细（按任务 / 模型）</button>
        </div>
      </section>

      <div class="side-column">
        <section class="workstation-glass-panel side-card" data-testid="token-price">
          <div class="side-head">
            <h2 class="card-title">当前价格版本</h2>
            <span v-if="report?.current_price" class="qc-chip tone-success">{{ formatTime(report.current_price.effective_from, true) }} 生效</span>
          </div>
          <template v-if="report?.current_price">
            <div class="price-rows">
              <div><span class="qc-muted">输入 Token</span><strong>{{ formatPrice(report.current_price.input_price) }}</strong></div>
              <div><span class="qc-muted">输出 Token</span><strong>{{ formatPrice(report.current_price.output_price) }}</strong></div>
              <div><span class="qc-muted">缓存命中</span><strong>{{ formatPrice(report.current_price.cache_hit_price) }}</strong></div>
            </div>
            <p v-if="!report.current_price.pricing_configured" class="qc-muted price-note">
              单价还没配置（来源：{{ report.current_price.pricing_source }}），金额暂按 0 计；配置后只影响之后的调用。
            </p>
          </template>
          <p v-else-if="pending" class="qc-muted price-note">加载中…</p>
          <p v-else class="qc-muted price-note">还没有模型调用记录，暂无价格版本。</p>
          <button type="button" class="qc-btn full" @click="showPriceHistory = !showPriceHistory">
            {{ showPriceHistory ? '收起价格历史' : '查看价格历史' }}
          </button>
          <ul v-if="showPriceHistory" class="price-history" data-testid="token-price-history">
            <li v-for="item in report?.price_history ?? []" :key="item.effective_from">
              <span>{{ formatTime(item.effective_from, true) }} 起</span>
              <span>输入 {{ formatPrice(item.input_price) }} · 输出 {{ formatPrice(item.output_price) }} · 缓存 {{ formatPrice(item.cache_hit_price) }}</span>
              <small class="qc-muted">{{ item.event_count }} 次调用</small>
            </li>
            <li v-if="!report?.price_history.length" class="qc-muted">{{ loading ? '加载中…' : '暂无记录' }}</li>
          </ul>
        </section>

        <section class="ap-note is-warning">
          <strong>费用口径待确认</strong>
          当前按「Token 消耗量 × 单价」计算。是否包含供应商折扣、汇率、固定服务费和部门结算加价，需要财务与业务负责人确认。
        </section>

        <section class="workstation-glass-panel side-card">
          <h2 class="card-title">预警规则</h2>
          <div class="rules">
            <div>预算达到 80%：提醒部门管理员</div>
            <div>预算达到 90%：提醒体验中心</div>
            <div>预算达到 100%：禁止新任务或申请追加</div>
          </div>
          <p class="qc-muted price-note">部门预算在「组织管理 → 部门管理」维护；达到 100% 后的停止策略待业务确认。</p>
        </section>
      </div>
    </div>

    <div v-if="detailTarget" class="ap-modal-backdrop" data-testid="token-detail-modal" @click.self="detailTarget = null">
      <div class="ap-modal is-wide">
        <div class="ap-modal-head">
          <h3>{{ detailTarget.tenant_name }} · {{ detailTarget.department_name }} · {{ report?.month_label }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="detailTarget = null">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="ap-table-wrap">
            <table class="qc-table">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>供应商 / 模型</th>
                  <th class="is-right">调用</th>
                  <th class="is-right">输入 Token</th>
                  <th class="is-right">输出 Token</th>
                  <th class="is-right">金额</th>
                  <th>最近调用</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!detailRows.length">
                  <td colspan="7" class="qc-empty">{{ detailLoading ? '加载中…' : '暂无明细' }}</td>
                </tr>
                <tr v-for="item in detailRows" :key="item.task_id">
                  <td>
                    <button type="button" class="qc-link" @click="router.push(`/admin/tasks/${item.task_id}`)">{{ item.task_name }}</button>
                  </td>
                  <td class="qc-muted">{{ item.provider || '—' }} / {{ item.model || '—' }}</td>
                  <td class="is-right">{{ item.ai_calls }}</td>
                  <td class="is-right">{{ formatTokens(item.input_tokens) }}</td>
                  <td class="is-right">{{ formatTokens(item.output_tokens) }}</td>
                  <td class="is-right">{{ formatMoney(item.total_cost, report?.totals.currency) }}</td>
                  <td class="qc-muted">{{ formatTime(item.last_call_at, true) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="detailTarget = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stamp {
  margin: 0 0 -12px;
  font-size: 12px;
}

.title-sm {
  font-size: 20px;
}

.control {
  width: auto;
  min-width: 150px;
}

.kpi-five {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.usage-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.65fr);
  gap: 16px;
}

.card-title {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 15px;
  font-weight: 600;
}

.card-sub {
  margin: 4px 0 0;
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.search {
  width: 200px;
}

.dept-name {
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.dept-sub {
  font-size: 12px;
}

.budget-cell {
  min-width: 150px;
}

.budget-meta {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
  font-size: 12px;
}

.budget-meta.is-warning {
  color: var(--workstation-warning);
}

.budget-meta.is-danger {
  color: var(--workstation-danger);
}

.side-column {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.side-card {
  padding: 20px;
}

.side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.price-rows {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 16px;
  font-size: 14px;
}

.price-rows div {
  display: flex;
  justify-content: space-between;
}

.price-note {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.6;
}

.full {
  justify-content: center;
  width: 100%;
  margin-top: 16px;
}

.price-history {
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
}

.price-history li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 0;
  border-top: 1px solid var(--workstation-border);
}

.rules {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  color: var(--workstation-text-muted);
  font-size: 14px;
}

@media (max-width: 1100px) {
  .usage-grid {
    grid-template-columns: 1fr;
  }
}
</style>
