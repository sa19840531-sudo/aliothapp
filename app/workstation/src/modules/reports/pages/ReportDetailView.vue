<script setup lang="ts">
import WebReportExplanationPanel from '@/components/WebReportExplanationPanel.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { usePortalNav } from '@/composables/usePortalNav'
import { NButton, NIcon, NSpin, useMessage } from 'naive-ui'
import {
  ChevronForwardOutline,
  DocumentTextOutline,
  FolderOpenOutline,
  ImageOutline,
} from '@vicons/ionicons5'
import PageExplorationPanel from '@/components/PageExplorationPanel.vue'
import WebStageDurationPanel from '@/components/WebStageDurationPanel.vue'
import { useEvidenceFileUrls } from '@/composables/useEvidenceFileUrls'
import RuleExecutionStatusTag from '@/components/status/RuleExecutionStatusTag.vue'
import SeverityBadge from '@/components/status/SeverityBadge.vue'
import { getReport } from '@/services/reports.api'
import { getTask } from '@/services/tasks.api'
import type { WorkstationReportDetail } from '@/types/report'

const route = useRoute()
const nav = usePortalNav()
const message = useMessage()
const evidenceFiles = useEvidenceFileUrls()

const taskId = computed(() => route.params.taskId?.toString() || '')
const report = ref<WorkstationReportDetail | null>(null)
const taskName = ref('')
const loading = ref(true)
const loadError = ref('')

async function loadAll() {
  if (!taskId.value) {
    loadError.value = '缺少任务编号'
    loading.value = false
    return
  }
  loading.value = true
  evidenceFiles.clear()
  try {
    const [reportResult, taskResult] = await Promise.all([
      getReport(taskId.value),
      getTask(taskId.value),
    ])
    report.value = reportResult
    void evidenceFiles.load(
      reportResult.evidence_entries
        .map((item) => String(item.artifact_id || ''))
        .filter(Boolean),
    )
    taskName.value = taskResult.task_name
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '报告加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => void loadAll())
watch(taskId, () => void loadAll())

const summary = computed(() => report.value?.summary ?? null)
const pageExploration = computed(() => report.value?.page_exploration ?? null)

const reportStageMeta = computed(() => {
  if (report.value?.incomplete_reasons?.includes('H5_REPORT_NO_BUSINESS_RESULTS')) {
    return {
      stage: 'final_review',
      label: '尚无有效质检结果',
      description: '本次未完成规则质检，诊断证据保留在任务过程；不能作为业务质检报告或导出。',
    }
  }
  const stage = report.value?.report_stage ?? (report.value?.is_final ? 'final' : 'preview')
  if (stage === 'final') {
    return {
      stage,
      label: '已封存终版',
      description: '报告已通过 Completion Gate，并与当前 Run 的不可变快照完成绑定。',
    }
  }
  if (stage === 'final_review') {
    return {
      stage,
      label: '待复核报告（未封存）',
      description: '任务已结束，但报告尚未通过 Completion Gate，不能作为正式终版结论。',
    }
  }
  return {
    stage,
    label: '运行中预览',
    description: '任务仍在运行，页面覆盖、规则判定和证据可能继续变化。',
  }
})

const incompleteReasonText = computed(() => {
  const labels: Record<string, string> = {
    WEB_REPORT_NO_PERSISTED_FACTS: '尚未产生可信采集',
    WEB_REPORT_NOT_FINALIZED: '报告尚未封存',
    WEB_COMPLETION_GATE_NOT_PASSED: 'Completion Gate 尚未通过',
    WEB_REPORT_BINDING_PENDING: '正式报告尚未绑定',
    WEB_REQUIRED_PAGE_TYPES_MISSING: '仍有必检页面类型缺口',
    WEB_PAGE_TYPE_REVIEW_PENDING: '仍有页面类型待复核',
    WEB_MANUAL_CONFIRMATION_PENDING: '仍有规则待人工确认',
    H5_REPORT_NO_BUSINESS_RESULTS: '本次未完成规则质检，不能作为业务质检报告或导出',
    H5_REPORT_NOT_FINALIZED: '质检尚未完成正式收口，当前内容不能作为正式报告',
    COMMAND_HOST_BOUNDARY_VIOLATION: '页面跳转超出本次任务允许的网站范围，质检已停止',
  }
  return (report.value?.incomplete_reasons ?? []).map((reason) => labels[reason] ?? '质检流程尚未完成，请查看任务过程中的诊断说明')
})

const stats = computed(() => {
  const s = summary.value
  if (!s) {
    return { total: 0, pass: 0, fail: 0, other: 0 }
  }
  return {
    total: s.total_rules,
    pass: s.pass,
    fail: s.fail,
    other: s.total_rules - s.pass - s.fail,
  }
})

const passRate = computed(() => {
  if (typeof summary.value?.pass_rate === 'number') {
    return summary.value.pass_rate
  }
  const conclusive = stats.value.pass + stats.value.fail
  return conclusive > 0 ? Math.round((stats.value.pass / conclusive) * 100) : 0
})

const qcSuccessRate = computed(() => {
  if (typeof summary.value?.qc_success_rate === 'number') {
    return summary.value.qc_success_rate
  }
  const s = summary.value
  if (!s) {
    return 0
  }
  const successful = s.pass + s.fail + s.warning + (s.confirmed_not_applicable_rules ?? 0)
  return s.total_rules > 0 ? Math.round((successful / s.total_rules) * 100) : 0
})

const donutStyle = computed(() => {
  const { total } = stats.value
  if (total === 0) {
    return { background: 'conic-gradient(rgba(148, 163, 184, 0.25) 0 360deg)' }
  }
  const successDeg = Math.round((qcSuccessRate.value / 100) * 360)
  return {
    background: `conic-gradient(#34d399 0 ${successDeg}deg, #f59e0b ${successDeg}deg 360deg)`,
  }
})

const completionGateReasonText: Record<string, string> = {
  exploration_not_terminal: '页面探索尚未进入终态',
  pending_action: '仍有页面动作未完成',
  pending_capture_commit: '仍有采集证据未提交完成',
  nonterminal_enforced_work_item: '仍有强制工作项未进入终态',
  active_session_request: '仍有浏览器或设备会话请求在执行',
  candidate_rule_coverage_nonterminal: '候选规则尚未全部完成判定',
  background_execution: '仍有后台执行未完成',
  review_policy_unsatisfied: '人工复核策略尚未满足',
  app_candidate_inventory_empty: 'App 候选规则清单为空',
  app_coverage_or_visual_quality_incomplete: 'App 页面覆盖或视觉质量未达标',
  h5_coverage_scope_incomplete: 'H5 业务页面覆盖范围未达标',
}

const completionGateReasons = computed(() =>
  (report.value?.completion_gate?.block_reasons ?? []).map((reason) => ({
    code: reason,
    label: completionGateReasonText[reason] ?? reason,
  })),
)

const taskStatusMeta = computed(() => {
  const map: Record<string, { label: string; tone: string }> = {
    queued: { label: '待执行', tone: 'pending' },
    running: { label: '执行中', tone: 'running' },
    canceling: { label: '取消中', tone: 'warning' },
    review: { label: '待复核', tone: 'warning' },
    done: { label: '已完成', tone: 'pass' },
    failed: { label: '执行失败', tone: 'error' },
    canceled: { label: '已取消', tone: 'pending' },
  }
  const status = report.value?.task.status || ''
  return map[status] || { label: status || '-', tone: 'warning' }
})

const conclusionText = computed(() => {
  const s = summary.value
  if (!s) {
    return ''
  }
  const attention = s.manual_review + s.evidence_insufficient + s.warning
  const parts = [`共 ${s.total_rules} 条规则，${s.evaluated_rules} 条完成判定`, `${s.pass} 项通过`]
  if (s.fail > 0) {
    parts.push(`${s.fail} 项不通过`)
  }
  if (attention > 0) {
    parts.push(`${attention} 项待确认`)
  }
  if (s.skipped > 0) {
    const notApplicable = s.confirmed_not_applicable_rules ?? 0
    const coverageGap = s.coverage_gap_rules ?? 0
    if (notApplicable > 0) {
      parts.push(`${notApplicable} 条经判定不适用`)
    }
    if (coverageGap > 0) {
      parts.push(`${coverageGap} 条未被本次运行覆盖`)
    }
  }
  if (s.error > 0) {
    parts.push(`${s.error} 项执行异常`)
  }
  const pages = pageExploration.value
  if (pages?.tracking_available) {
    parts.push(
      `动态发现 ${pages.page_total} 个页面，完成 ${pages.completed_page_count} 个，需复核 ${pages.degraded_page_count} 个，失败 ${pages.failed_page_count} 个`,
    )
    if (pages.complete === true) {
      parts.push('页面探索已收敛')
    }
  }
  return parts.join('，') + '。'
})

const overviewItems = computed(() => {
  if (!report.value) {
    return []
  }
  const detail = report.value
  const coverage = summary.value?.candidate_execution_coverage
  return [
    { label: '任务编号', value: detail.task.task_id },
    { label: '规则集版本', value: `${detail.task.ruleset_code} v${detail.task.ruleset_version}` },
    { label: '报告版本', value: detail.report_version },
    { label: 'Run ID', value: detail.task.run_id || '-' },
    { label: '报告生成时间', value: formatDate(detail.generated_at) },
    {
      label: '有效判定完成率',
      value: `${qcSuccessRate.value}%`,
    },
    { label: '通过率', value: `${passRate.value}%` },
    { label: '候选执行覆盖率', value: coverage === null || coverage === undefined ? '-' : `${coverage}%` },
    { label: '确认不适用', value: `${detail.summary.confirmed_not_applicable_rules ?? 0} 条` },
    { label: '运行覆盖缺口', value: `${detail.summary.coverage_gap_rules ?? 0} 条` },
  ]
})

const statusRank: Record<string, number> = {
  fail: 0,
  manual_review: 1,
  warning: 2,
  evidence_insufficient: 3,
  error: 4,
  skipped: 5,
  pass: 6,
}

const severityRank: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
}

const sortedFindings = computed(() =>
  [...(report.value?.rule_results ?? [])].sort((a, b) => {
    const statusDiff = (statusRank[a.execution_status] ?? 9) - (statusRank[b.execution_status] ?? 9)
    if (statusDiff !== 0) {
      return statusDiff
    }
    return (severityRank[a.severity || 'info'] ?? 9) - (severityRank[b.severity || 'info'] ?? 9)
  }),
)

type FindingFilter = 'all' | 'attention' | 'coverage_gap' | 'judged'
const findingFilter = ref<FindingFilter>('all')

const findingFilters = computed(() => {
  const items = sortedFindings.value
  const attentionStatuses = new Set([
    'fail',
    'manual_review',
    'warning',
    'evidence_insufficient',
    'error',
  ])
  return [
    { key: 'all' as const, label: '全部', count: items.length },
    {
      key: 'attention' as const,
      label: '需关注',
      count: items.filter((item) => attentionStatuses.has(item.execution_status)).length,
    },
    {
      key: 'coverage_gap' as const,
      label: '未覆盖',
      count: items.filter((item) => item.outcome_class === 'coverage_gap').length,
    },
    {
      key: 'judged' as const,
      label: '已判定',
      count: items.filter((item) => item.outcome_class === 'judged').length,
    },
  ]
})

const filteredFindings = computed(() => {
  if (findingFilter.value === 'all') {
    return sortedFindings.value
  }
  if (findingFilter.value === 'attention') {
    return sortedFindings.value.filter((item) =>
      ['fail', 'manual_review', 'warning', 'evidence_insufficient', 'error'].includes(
        item.execution_status,
      ),
    )
  }
  return sortedFindings.value.filter(
    (item) => item.outcome_class === findingFilter.value,
  )
})

const evidenceEntries = computed(() => report.value?.evidence_entries ?? [])

const evidenceTypeText: Record<string, string> = {
  screenshot: '截图',
  dom: 'DOM 快照',
  ocr: 'OCR 文本',
  network: '网络日志',
}

function evidenceType(value: string | undefined) {
  return value ? evidenceTypeText[value] || value : '-'
}

function evidenceUrl(artifactId: string) {
  return evidenceFiles.get(artifactId)
}

async function openEvidence(artifactId: string) {
  try {
    await evidenceFiles.open(artifactId)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '证据打开失败')
  }
}

function findingReason(item: { problem_desc: string | null; result_summary: string | null }) {
  return item.problem_desc || item.result_summary || '-'
}

function sourceMeta(source: string) {
  return source === 'bootstrap'
    ? { label: '兜底补记', className: 'source-bootstrap' }
    : { label: 'Agent 判定', className: 'source-agent' }
}

function confidenceText(value: number | null) {
  return value === null ? '-' : `${Math.round(value * 100)}%`
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
  <section class="report-detail-shell">
    <div class="breadcrumb-row">
      <button class="breadcrumb-link" type="button" @click="nav.push('/reports')">报告中心</button>
      <n-icon size="15">
        <ChevronForwardOutline />
      </n-icon>
      <span>报告详情</span>
    </div>

    <div v-if="loading" class="state-panel workstation-glass-panel">
      <n-spin size="large" />
      <p>报告加载中…</p>
    </div>

    <div v-else-if="loadError" class="state-panel workstation-glass-panel">
      <h3>报告加载失败</h3>
      <p>{{ loadError }}</p>
      <n-button type="primary" @click="loadAll()">重试</n-button>
    </div>

    <template v-else-if="report">
      <div class="report-hero workstation-glass-panel workstation-card-hover">
        <div class="hero-main">
          <div class="hero-icon">
            <n-icon size="24">
              <DocumentTextOutline />
            </n-icon>
          </div>
          <div class="hero-text">
            <div class="title-line">
              <h2>{{ taskName || report.task.target_url }}</h2>
              <span class="status-pill" :class="`tone-${taskStatusMeta.tone}`">
                <span class="status-dot"></span>
                {{ taskStatusMeta.label }}
              </span>
            </div>
            <p class="hero-url">{{ report.task.target_url }}</p>
            <p class="hero-conclusion">{{ conclusionText }}</p>
          </div>
        </div>

        <div class="hero-rate">
          <div class="rate-donut" :style="donutStyle">
            <div class="donut-hole">
              <strong>{{ qcSuccessRate }}<span>%</span></strong>
              <em>有效判定完成率</em>
            </div>
          </div>
          <div class="rate-figures">
            <div>
              <span>通过</span>
              <strong class="figure-pass">{{ stats.pass }}</strong>
            </div>
            <div>
              <span>不通过</span>
              <strong class="figure-fail">{{ stats.fail }}</strong>
            </div>
            <div>
              <span>待确认/不适用/未覆盖</span>
              <strong class="figure-other">{{ stats.other }}</strong>
            </div>
          </div>
        </div>
      </div>

      <div class="report-stage-banner" :class="`report-stage-${reportStageMeta.stage}`">
        <strong>{{ reportStageMeta.label }}</strong>
        <span>{{ reportStageMeta.description }}</span>
      </div>

      <div v-if="incompleteReasonText.length" class="report-stage-banner report-stage-final_review">
        <strong>当前报告缺口</strong>
        <span>{{ incompleteReasonText.join('；') }}</span>
      </div>

      <section v-if="report.completion_gate" class="completion-gate workstation-glass-panel">
        <div class="completion-gate-heading">
          <div>
            <span class="eyebrow">COMPLETION GATE</span>
            <h2>报告完成门禁</h2>
          </div>
          <span
            class="gate-status"
            :class="report.completion_gate.gate_passed ? 'gate-passed' : 'gate-blocked'"
          >
            {{ report.completion_gate.gate_passed ? '已通过并可封存' : '未通过，禁止作为终版' }}
          </span>
        </div>
        <div v-if="completionGateReasons.length" class="gate-reason-grid">
          <div v-for="reason in completionGateReasons" :key="reason.code" class="gate-reason-item">
            <span></span>
            <div>
              <strong>{{ reason.label }}</strong>
              <code>{{ reason.code }}</code>
            </div>
          </div>
        </div>
        <p v-else class="gate-empty-reason">
          {{ report.completion_gate.gate_passed ? '全部完成条件已经满足。' : '当前报告未返回具体阻断原因，请检查任务收口快照。' }}
        </p>
      </section>

      <WebStageDurationPanel
        v-if="report.stage_durations"
        :durations="report.stage_durations"
      />

      <WebReportExplanationPanel v-if="report.page_exploration?.web_report_explanation" :explanation="report.page_exploration.web_report_explanation" />
      <PageExplorationPanel
        v-if="pageExploration?.tracking_available"
        :summary="pageExploration"
      />

      <div class="overview-grid workstation-glass-panel">
        <div v-for="item in overviewItems" :key="item.label" class="overview-item">
          <span>{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
        </div>
      </div>

      <section class="findings-section">
        <div class="section-heading">
          <div class="section-title-wrap">
            <h2>规则结果明细</h2>
            <span>{{ summary?.attention_required ?? 0 }} 项需关注</span>
          </div>
          <p>按执行结果与严重级别排序，识别不通过与待确认项排在最前</p>
        </div>

        <div class="finding-filters" role="tablist" aria-label="规则结果筛选">
          <button
            v-for="filter in findingFilters"
            :key="filter.key"
            type="button"
            :class="{ active: findingFilter === filter.key }"
            @click="findingFilter = filter.key"
          >
            {{ filter.label }}
            <span>{{ filter.count }}</span>
          </button>
        </div>

        <div class="findings-table workstation-glass-panel">
          <table>
            <thead>
              <tr>
                <th>规则名称</th>
                <th>执行结果</th>
                <th>严重级别</th>
                <th>置信度</th>
                <th>判定来源</th>
                <th>判定说明 / 跳过原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in filteredFindings" :key="item.execution_id">
                <td>
                  <div class="rule-name-cell">
                    <strong>{{ item.rule_name }}</strong>
                    <span>{{ item.rule_code }}</span>
                  </div>
                </td>
                <td>
                  <rule-execution-status-tag
                    :status="item.execution_status"
                    :outcome-class="item.outcome_class"
                  />
                </td>
                <td><severity-badge :severity="item.severity" /></td>
                <td class="mono-cell">{{ confidenceText(item.confidence) }}</td>
                <td>
                  <span class="source-badge" :class="sourceMeta(item.source).className">
                    {{ sourceMeta(item.source).label }}
                  </span>
                </td>
                <td class="desc-cell">{{ findingReason(item) }}</td>
              </tr>
              <tr v-if="filteredFindings.length === 0">
                <td class="empty-cell" colspan="6">当前筛选条件下没有规则执行记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="evidence-section">
        <div class="section-heading">
          <div class="section-title-wrap">
            <h2>证据入口</h2>
            <span>{{ evidenceEntries.length }} 项</span>
          </div>
          <p>报告引用的截图、DOM 与 OCR 证据索引</p>
        </div>

        <div class="evidence-table workstation-glass-panel">
          <table v-if="evidenceEntries.length > 0">
            <thead>
              <tr>
                <th>证据名称</th>
                <th>类型</th>
                <th>来源</th>
                <th>预览 / 原文</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, index) in evidenceEntries" :key="item.artifact_id || index">
                <td>
                  <div class="evidence-name-cell">
                    <span class="evidence-mark">
                      <n-icon size="15">
                        <ImageOutline v-if="item.artifact_type === 'screenshot'" />
                        <FolderOpenOutline v-else />
                      </n-icon>
                    </span>
                    {{ item.title || item.artifact_id || '-' }}
                  </div>
                </td>
                <td>{{ evidenceType(item.artifact_type) }}</td>
                <td class="mono-cell">{{ item.source || '-' }}</td>
                <td>
                  <a
                    v-if="item.artifact_id"
                    class="evidence-open-link"
                    href="#"
                    @click.prevent="openEvidence(String(item.artifact_id))"
                  >
                    <img
                      v-if="item.artifact_type === 'screenshot'"
                      class="evidence-preview"
                      :src="evidenceUrl(String(item.artifact_id))"
                      alt="截图预览"
                      loading="lazy"
                    />
                    <span v-else>打开原文</span>
                  </a>
                  <span v-else class="mono-cell">-</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="evidenceEntries.length === 0" class="empty-block">
            本报告暂未附带证据索引
          </div>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.report-detail-shell {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.report-stage-banner {
  align-items: center;
  border: 1px solid currentColor;
  border-radius: 12px;
  display: flex;
  gap: 14px;
  padding: 13px 18px;
  color: var(--workstation-primary);
  background: rgba(96, 165, 250, 0.08);
}

.report-stage-banner strong {
  flex: 0 0 auto;
  font-size: 13px;
}

.report-stage-banner span {
  color: var(--workstation-text-muted);
  font-size: 13px;
  line-height: 1.6;
}

.report-stage-final {
  color: var(--workstation-success);
  background: rgba(52, 211, 153, 0.08);
}

.report-stage-final_review {
  color: var(--workstation-warning);
  background: rgba(245, 158, 11, 0.08);
}

.completion-gate {
  padding: 20px 24px;
}

.completion-gate-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}

.completion-gate-heading h2 {
  margin: 4px 0 0;
  color: var(--workstation-text-strong);
  font-size: 16px;
  font-weight: 900;
}

.eyebrow {
  color: var(--workstation-text-faint);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.16em;
}

.gate-status {
  padding: 7px 12px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 900;
  white-space: nowrap;
}

.gate-passed {
  color: var(--workstation-success);
  background: rgba(52, 211, 153, 0.08);
}

.gate-blocked {
  color: var(--workstation-warning);
  background: rgba(245, 158, 11, 0.08);
}

.gate-reason-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 18px;
}

.gate-reason-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid rgba(245, 158, 11, 0.18);
  border-radius: 10px;
  background: rgba(245, 158, 11, 0.05);
}

.gate-reason-item > span {
  width: 7px;
  height: 7px;
  margin-top: 5px;
  border-radius: 999px;
  flex: 0 0 auto;
  background: var(--workstation-warning);
  box-shadow: 0 0 10px rgba(245, 158, 11, 0.5);
}

.gate-reason-item strong,
.gate-reason-item code {
  display: block;
}

.gate-reason-item strong {
  color: var(--workstation-text);
  font-size: 12px;
  line-height: 1.5;
}

.gate-reason-item code {
  margin-top: 3px;
  color: var(--workstation-text-faint);
  font-size: 9px;
}

.gate-empty-reason {
  margin: 16px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.breadcrumb-row,
.hero-main,
.title-line,
.hero-rate,
.rate-figures,
.section-heading,
.section-title-wrap,
.evidence-name-cell {
  display: flex;
  align-items: center;
}

.breadcrumb-row {
  gap: 8px;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.breadcrumb-link {
  padding: 0;
  border: 0;
  color: var(--workstation-text-muted);
  font: inherit;
  background: transparent;
  cursor: pointer;
}

.breadcrumb-link:hover {
  color: var(--workstation-primary);
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

.report-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 28px;
  padding: 26px;
}

.hero-main {
  align-items: flex-start;
  gap: 16px;
  min-width: 0;
}

.hero-icon {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  color: var(--workstation-primary);
  background: rgba(56, 189, 248, 0.12);
}

.hero-text {
  min-width: 0;
}

.title-line {
  gap: 14px;
  flex-wrap: wrap;
}

.title-line h2 {
  margin: 0;
  overflow: hidden;
  color: var(--workstation-text-strong);
  font-size: 18px;
  font-weight: 900;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 800;
  background: color-mix(in srgb, currentColor 12%, transparent);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: currentColor;
}

.tone-pass {
  color: var(--workstation-success);
}

.tone-warning {
  color: var(--workstation-warning);
}

.tone-error {
  color: var(--workstation-danger);
}

.hero-url {
  margin: 8px 0 0;
  overflow: hidden;
  color: var(--workstation-text-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hero-conclusion {
  margin: 10px 0 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
}

.hero-rate {
  gap: 22px;
  flex: 0 0 auto;
}

.rate-donut {
  width: 116px;
  height: 116px;
  border-radius: 999px;
  display: grid;
  place-items: center;
}

.donut-hole {
  width: 84px;
  height: 84px;
  border-radius: 999px;
  display: grid;
  place-content: center;
  text-align: center;
  background: var(--workstation-panel-strong);
}

.donut-hole strong {
  color: var(--workstation-text-strong);
  font-size: 19px;
  line-height: 1;
  font-weight: 900;
  font-family: 'Space Grotesk', 'Segoe UI', sans-serif;
}

.donut-hole strong span {
  font-size: 11px;
}

.donut-hole em {
  margin-top: 4px;
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-style: normal;
}

.rate-figures {
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
}

.rate-figures div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
  min-width: 132px;
}

.rate-figures span {
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.rate-figures strong {
  font-size: 13px;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.figure-pass {
  color: var(--workstation-success);
}

.figure-fail {
  color: var(--workstation-danger);
}

.figure-other {
  color: var(--workstation-warning);
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px 28px;
  padding: 20px 24px;
}

.overview-item span {
  display: block;
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
}

.overview-item strong {
  display: block;
  margin-top: 7px;
  overflow: hidden;
  color: var(--workstation-text);
  font-size: 11px;
  font-weight: 600;
  font-family: 'JetBrains Mono', Consolas, monospace;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.section-heading {
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.section-title-wrap {
  gap: 12px;
}

.section-title-wrap h2 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 16px;
  font-weight: 900;
}

.section-title-wrap span {
  padding: 5px 12px;
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-radius: 999px;
  color: var(--workstation-primary);
  font-size: 11px;
  font-weight: 800;
  background: rgba(56, 189, 248, 0.1);
}

.section-heading p {
  margin: 0;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.finding-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: -2px 0 14px;
}

.finding-filters button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 11px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  color: var(--workstation-text-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 700;
  background: var(--workstation-panel-soft);
  cursor: pointer;
}

.finding-filters button:hover,
.finding-filters button.active {
  border-color: rgba(56, 189, 248, 0.46);
  color: var(--workstation-text-strong);
  background: rgba(56, 189, 248, 0.12);
}

.finding-filters button span {
  min-width: 20px;
  padding: 2px 6px;
  border-radius: 999px;
  color: var(--workstation-primary);
  font-size: 9px;
  text-align: center;
  background: rgba(56, 189, 248, 0.12);
}

.findings-table,
.evidence-table {
  overflow-x: auto;
}

.findings-table table {
  min-width: 980px;
}

.evidence-table table {
  min-width: 760px;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 15px 20px;
  border-bottom: 1px solid var(--workstation-border);
  color: var(--workstation-text);
  font-size: 13px;
  text-align: left;
  vertical-align: middle;
}

tbody tr:last-child td {
  border-bottom: 0;
}

th {
  color: var(--workstation-text-faint);
  font-size: 12px;
  font-weight: 900;
  text-transform: uppercase;
}

.rule-name-cell strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 13px;
}

.rule-name-cell span {
  display: block;
  margin-top: 4px;
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.evidence-preview {
  border: 1px solid var(--workstation-border);
  border-radius: 8px;
  display: block;
  height: 72px;
  object-fit: cover;
  object-position: top;
  width: 112px;
}

.evidence-open-link {
  color: var(--workstation-primary);
  font-size: 10px;
  text-decoration: none;
}

.evidence-open-link:hover {
  text-decoration: underline;
}

.mono-cell {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 10px;
}

.desc-cell {
  min-width: 260px;
  max-width: 420px;
  color: var(--workstation-text-muted);
  line-height: 1.65;
  white-space: normal;
}

.empty-cell {
  color: var(--workstation-text-faint);
  text-align: center;
}

.source-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 800;
  white-space: nowrap;
  background: color-mix(in srgb, currentColor 10%, transparent);
}

.source-agent {
  color: var(--workstation-primary);
}

.source-bootstrap {
  color: var(--workstation-text-muted);
}

.evidence-name-cell {
  gap: 10px;
  color: var(--workstation-text-strong);
  font-weight: 600;
}

.evidence-mark {
  width: 30px;
  height: 30px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  color: var(--workstation-primary);
  background: rgba(56, 189, 248, 0.1);
}

.empty-block {
  margin: 18px 20px;
  padding: 16px;
  border: 1px dashed var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text-faint);
  font-size: 10px;
  text-align: center;
}

@media (max-width: 1080px) {
  .report-hero {
    align-items: flex-start;
    flex-direction: column;
  }

  .overview-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .hero-rate {
    flex-wrap: wrap;
  }

  .overview-grid {
    grid-template-columns: 1fr;
  }

  .completion-gate-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .gate-reason-grid {
    grid-template-columns: 1fr;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .desc-cell {
    max-width: none;
  }
}
</style>
