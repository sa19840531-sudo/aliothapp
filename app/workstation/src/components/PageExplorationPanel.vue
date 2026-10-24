<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PageExplorationSummary } from '@/types/task-insight'

const props = withDefaults(
  defineProps<{
    summary: PageExplorationSummary
    variant?: 'embedded' | 'card'
    collapsible?: boolean
    defaultExpanded?: boolean
  }>(),
  {
    variant: 'card',
    collapsible: false,
    defaultExpanded: true,
  },
)

const expanded = ref(props.defaultExpanded)

const statusText = computed(() => {
  if (props.summary.complete === false) return '仍有动态工作待处理'
  if (props.summary.requires_review) return '探索已完成，存在局部失败待复核'
  return '动态页面探索已完成'
})

const badgeText = computed(() => {
  if (props.summary.complete === false) return '探索中'
  return props.summary.requires_review ? '已收敛 · 待复核' : '已收敛'
})

const inactivePageCount = computed(
  () => props.summary.superseded_page_count + props.summary.abandoned_page_count,
)

const productCoverage = computed(() => props.summary.coverage?.product_business ?? null)

const productCoverageStatus = computed(() => {
  const coverage = productCoverage.value
  if (!coverage?.assessment_available) return '尚未形成适用性评估'
  if (!coverage.assessment_scope_verified) return '评估范围未闭合'
  if (!coverage.assessment_complete) return '仍有页面类型待确认'
  if (props.summary.coverage?.acceptance_complete === false) return '覆盖未达验收线'
  return '产品覆盖已闭合'
})

const productCoverageTone = computed(() => {
  const coverage = productCoverage.value
  if (!coverage?.assessment_available || !coverage.assessment_scope_verified) return 'danger'
  if (!coverage.assessment_complete || props.summary.coverage?.acceptance_complete === false) {
    return 'warning'
  }
  return 'success'
})

const productCoverageRate = computed(() => {
  const rate = productCoverage.value?.coverage_rate
  return typeof rate === 'number' ? `${Math.round(rate * 100)}%` : '待确认'
})

const coverageGroups = computed(() => {
  const coverage = productCoverage.value
  if (!coverage) return []
  return [
    { label: '已覆盖', values: coverage.covered_page_types, tone: 'success' },
    { label: '确认不适用', values: coverage.not_applicable_page_types, tone: 'neutral' },
    { label: '已知未覆盖', values: coverage.known_uncovered_page_types, tone: 'warning' },
    { label: '未形成结论', values: coverage.unresolved_page_types, tone: 'danger' },
    { label: '缺少适用性评估', values: coverage.missing_assessment_page_types, tone: 'danger' },
  ].filter((group) => group.values.length > 0)
})

function pageResultText(status: string, lifecycleStatus: string) {
  if (lifecycleStatus === 'superseded') return '已被替代'
  if (lifecycleStatus === 'abandoned') return '已放弃'
  if (lifecycleStatus === 'evidence_only') return '旁路证据'
  const labels: Record<string, string> = {
    captured: '已采集',
    detecting: '识别中',
    matching: '规则匹配中',
    evaluating: '规则执行中',
    completed: '页面检查完成',
    degraded: '完成 · 需复核',
    failed: '质检失败',
    unknown: '状态未知',
  }
  return labels[status] || status
}

function pageResultClass(status: string, lifecycleStatus: string) {
  if (lifecycleStatus === 'evidence_only') return 'result-evidence'
  if (lifecycleStatus !== 'active') return 'result-inactive'
  if (status === 'completed') return 'result-completed'
  if (status === 'degraded') return 'result-degraded'
  if (status === 'failed') return 'result-failed'
  return 'result-pending'
}
</script>

<template>
  <section
    class="page-exploration-panel"
    :class="[variant, { 'workstation-glass-panel': variant === 'card' }]"
  >
    <button
      v-if="collapsible"
      type="button"
      class="panel-head collapsible-head"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <div>
        <strong>动态页面探索完成度</strong>
        <span>{{ statusText }}；页面总量以运行时实际发现为准</span>
      </div>
      <span class="panel-actions">
        <em :class="{ attention: summary.complete === false || summary.requires_review }">
          {{ badgeText }}
        </em>
        <span class="collapse-label">{{ expanded ? '收起' : '展开' }}</span>
      </span>
    </button>
    <div v-else class="panel-head">
      <div>
        <strong>动态页面探索完成度</strong>
        <span>{{ statusText }}；页面总量以运行时实际发现为准</span>
      </div>
      <em :class="{ attention: summary.complete === false || summary.requires_review }">
        {{ badgeText }}
      </em>
    </div>

    <template v-if="!collapsible || expanded">
      <div class="metrics">
        <span>发现页面 <b>{{ summary.page_total }}</b></span>
        <span>完成 <b>{{ summary.completed_page_count }}</b></span>
        <span>需复核 <b>{{ summary.degraded_page_count }}</b></span>
        <span>失败 <b>{{ summary.failed_page_count }}</b></span>
        <span>替代 / 放弃 <b>{{ inactivePageCount }}</b></span>
        <span>旁路证据 <b>{{ summary.evidence_only_page_count }}</b></span>
        <span>页面待处理 <b>{{ summary.pending_page_count }}</b></span>
        <span>Frontier 动作 <b>{{ summary.action_total }}</b></span>
        <span>动作待处理 <b>{{ summary.pending_action_count }}</b></span>
        <span>动作失败 <b>{{ summary.failed_action_count }}</b></span>
      </div>

      <section v-if="productCoverage" class="coverage-contract">
        <div class="coverage-contract-head">
          <div>
            <strong>产品业务页面覆盖合同</strong>
            <span>以产品适用页面为分母，不使用全局目录缺口代替产品漏测</span>
          </div>
          <em :class="`tone-${productCoverageTone}`">{{ productCoverageStatus }}</em>
        </div>
        <div class="coverage-overview">
          <div>
            <span>有效覆盖率</span>
            <strong>{{ productCoverageRate }}</strong>
          </div>
          <div>
            <span>适用页面</span>
            <strong>{{ productCoverage.applicable_count ?? '待确认' }}</strong>
          </div>
          <div>
            <span>已覆盖</span>
            <strong>{{ productCoverage.covered_count }}</strong>
          </div>
          <div>
            <span>评估作用域</span>
            <strong>{{ productCoverage.assessment_scope_verified ? '已验证' : '未验证' }}</strong>
          </div>
        </div>
        <div v-if="coverageGroups.length" class="coverage-groups">
          <div v-for="group in coverageGroups" :key="group.label" class="coverage-group">
            <span>{{ group.label }}</span>
            <ul>
              <li v-for="pageType in group.values" :key="pageType" :class="`tone-${group.tone}`">
                {{ pageType }}
              </li>
            </ul>
          </div>
        </div>
        <p v-else-if="!productCoverage.assessment_available" class="coverage-empty">
          当前 Run 没有页面类型适用性评估，不能据此声明产品页面覆盖完成。
        </p>
      </section>

      <div v-if="summary.page_results.length" class="page-results">
        <article
          v-for="page in summary.page_results"
          :key="page.page_capture_id || `${page.page_seq}-${page.page_url}`"
        >
          <div class="result-main">
            <span :class="pageResultClass(page.inspection_status, page.lifecycle_status)">
              {{ pageResultText(page.inspection_status, page.lifecycle_status) }}
            </span>
            <strong>{{ page.page_title || page.page_type || `动态页面 ${page.page_seq ?? ''}` }}</strong>
            <code>{{ page.page_capture_id || '无采集 ID' }}</code>
          </div>
          <p>{{ page.page_url || '未记录页面地址' }}</p>
          <p v-if="page.failure_message" class="failure">
            {{ page.failure_code || 'PAGE_INSPECTION_FAILED' }} · {{ page.failure_message }}
          </p>
          <p v-else-if="page.abandon_reason" class="failure">{{ page.abandon_reason }}</p>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.page-exploration-panel {
  padding: 14px 16px;
  border: 1px solid rgba(56, 189, 248, 0.16);
  border-radius: 12px;
  background: rgba(14, 165, 233, 0.06);
}

.page-exploration-panel.embedded {
  margin: 0 20px 16px;
}

.page-exploration-panel.card {
  padding: 18px 22px;
}

.panel-head,
.metrics,
.result-main {
  display: flex;
  align-items: center;
}

.panel-head {
  justify-content: space-between;
  gap: 16px;
}

.collapsible-head {
  width: 100%;
  padding: 0;
  border: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  background: transparent;
  cursor: pointer;
}

.panel-actions {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}

.collapse-label {
  color: var(--workstation-primary);
  font-size: 10px;
  font-weight: 800;
}

.panel-head strong,
.panel-head span {
  display: block;
}

.panel-head .panel-actions {
  display: inline-flex;
  margin-top: 0;
}

.panel-head .collapse-label {
  color: var(--workstation-primary);
  margin-top: 0;
}

.panel-head strong {
  color: var(--workstation-text-strong);
  font-size: 13px;
}

.panel-head span {
  margin-top: 4px;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.panel-head em,
.result-main > span {
  padding: 3px 9px;
  border-radius: 999px;
  color: var(--workstation-success);
  flex: 0 0 auto;
  font-size: 10px;
  font-style: normal;
  font-weight: 800;
  background: rgba(52, 211, 153, 0.12);
}

.panel-head em.attention,
.result-main > span.result-degraded,
.result-main > span.result-inactive {
  color: var(--workstation-warning);
  background: rgba(245, 158, 11, 0.12);
}

.metrics {
  gap: 8px 16px;
  margin-top: 12px;
  flex-wrap: wrap;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.metrics b {
  color: var(--workstation-text-strong);
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.coverage-contract {
  margin-top: 14px;
  padding: 13px 14px;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  background: var(--workstation-panel-soft);
}

.coverage-contract-head,
.coverage-overview,
.coverage-group,
.coverage-group ul {
  display: flex;
  align-items: center;
}

.coverage-contract-head {
  justify-content: space-between;
  gap: 14px;
}

.coverage-contract-head strong,
.coverage-contract-head span {
  display: block;
}

.coverage-contract-head strong {
  color: var(--workstation-text-strong);
  font-size: 12px;
}

.coverage-contract-head span {
  margin-top: 3px;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.coverage-contract-head em {
  padding: 3px 9px;
  border-radius: 999px;
  flex: 0 0 auto;
  font-size: 10px;
  font-style: normal;
  font-weight: 800;
}

.coverage-overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.coverage-overview > div {
  padding: 9px 10px;
  border-radius: 8px;
  background: rgba(148, 163, 184, 0.06);
}

.coverage-overview span,
.coverage-overview strong {
  display: block;
}

.coverage-overview span {
  color: var(--workstation-text-faint);
  font-size: 9px;
}

.coverage-overview strong {
  margin-top: 4px;
  color: var(--workstation-text-strong);
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 14px;
}

.coverage-groups {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.coverage-group {
  align-items: flex-start;
  gap: 10px;
}

.coverage-group > span {
  width: 88px;
  padding-top: 3px;
  flex: 0 0 auto;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.coverage-group ul {
  gap: 5px;
  margin: 0;
  padding: 0;
  flex-wrap: wrap;
  list-style: none;
}

.coverage-group li,
.coverage-contract-head em {
  color: var(--workstation-text-muted);
  background: rgba(148, 163, 184, 0.12);
}

.coverage-group li {
  padding: 3px 7px;
  border-radius: 6px;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 9px;
}

.tone-success {
  color: var(--workstation-success) !important;
  background: rgba(52, 211, 153, 0.12) !important;
}

.tone-warning {
  color: var(--workstation-warning) !important;
  background: rgba(245, 158, 11, 0.12) !important;
}

.tone-danger {
  color: #fb7185 !important;
  background: rgba(244, 63, 94, 0.12) !important;
}

.coverage-empty {
  margin: 10px 0 0;
  color: var(--workstation-danger);
  font-size: 10px;
}

.page-results {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.page-results article {
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  background: var(--workstation-panel-soft);
}

.result-main {
  gap: 7px;
  min-width: 0;
}

.result-main > span {
  padding: 2px 6px;
  font-size: 9px;
}

.result-main > span.result-failed {
  color: var(--workstation-danger);
  background: rgba(244, 63, 94, 0.12);
}

.result-main > span.result-evidence {
  color: var(--workstation-text-muted);
  background: rgba(148, 163, 184, 0.12);
}

.result-main > span.result-pending {
  color: var(--workstation-primary);
  background: rgba(14, 165, 233, 0.12);
}

.result-main strong,
.result-main code,
.page-results p {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-main strong {
  color: var(--workstation-text);
  font-size: 11px;
}

.result-main code {
  margin-left: auto;
  color: var(--workstation-text-faint);
  font-size: 9px;
}

.page-results p {
  margin: 5px 0 0;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.page-results p.failure {
  color: var(--workstation-danger);
}

@media (max-width: 760px) {
  .page-results {
    grid-template-columns: 1fr;
  }

  .panel-head {
    align-items: flex-start;
  }

  .coverage-contract-head,
  .coverage-group {
    align-items: flex-start;
    flex-direction: column;
  }

  .coverage-overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
