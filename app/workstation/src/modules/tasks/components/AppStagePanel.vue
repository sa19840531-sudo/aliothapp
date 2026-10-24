<script setup lang="ts">
import { computed } from 'vue'
import type {
  AppBusinessPageProjection,
  AppExplorationProjection,
  AppExplorationStatus,
  AppStageProjection,
} from '@/types/task-insight'
import type { V2TaskExecutionMode } from '@/types/task'

const props = defineProps<{
  stage: AppStageProjection | null
  loading?: boolean
  executionMode?: V2TaskExecutionMode | string | null
  /** 只有体验中心管理员能用规则测试，其他账号不提这个入口 */
  canTestRules?: boolean
}>()

const exploration = computed(() => props.stage?.app_exploration ?? null)
const publication = computed(() => props.stage?.publication_projection ?? null)
const isExplorationOnly = computed(() => props.executionMode === 'exploration_only')

const statusLabels: Record<AppExplorationStatus, string> = {
  created: '已创建',
  bootstrapping: '启动准备中',
  surface_resolving: '界面准备中',
  app_ready: 'App 已就绪',
  exploring: '页面探索中',
  final_observing: '最终观察中',
  evidence_validating: '证据校验中',
  evidence_sealing: '证据封存中',
  session_draining: '会话关闭中',
  exploration_completed: '探索完成',
  evidence_insufficient: '证据不足',
  technical_failed: '技术失败',
  close_failed: '会话关闭失败',
}

const explorationStatusText = computed(() => {
  const status = exploration.value?.status
  return status ? statusLabels[status] : '暂无探索封存事实'
})

const bootstrapLabel = computed(() => {
  const bootstrap = props.stage?.bootstrap
  if (!bootstrap) return '尚未开始'
  if (bootstrap.error_code) return '技术失败'
  if (bootstrap.stage === 'app_ready') return '已就绪'
  return bootstrap.stage || '进行中'
})

const buildValidationLabel = computed(() => {
  const bootstrap = props.stage?.bootstrap
  if (bootstrap?.error_code) return '校验失败'
  if (bootstrap) return '已通过'
  return '待校验'
})

const surfaceReadinessText = computed(() => {
  const value = exploration.value
  if (!value) return '暂无新流程数据'
  if (value.status === 'technical_failed') return '技术失败'
  if (value.surface_phase) return value.surface_phase
  if (value.status === 'bootstrapping' || value.status === 'surface_resolving') return '解析中'
  return '已就绪'
})

const evidenceValidationText = computed(() => {
  const value = exploration.value
  if (!value) return '待校验'
  if (value.status === 'evidence_insufficient') return `证据不足 · 缺口 ${value.evidence_gap_count}`
  if (value.status === 'technical_failed') {
    return value.evidence_preserved ? '技术失败 · 证据已保留' : '因技术失败未完成'
  }
  if (value.status === 'evidence_validating') return '校验中'
  if (value.evidence_index_id) return `已校验 · 缺口 ${value.evidence_gap_count}`
  return '等待最终观察'
})

const evidenceSealingText = computed(() => {
  const value = exploration.value
  if (!value) return '待封存'
  if (value.status === 'evidence_sealing') return '封存中'
  if (value.evidence_index_hash) return '已封存'
  if (value.status === 'technical_failed' && value.evidence_preserved) {
    return value.recovery_state === 'pending' ? '证据待对账' : '部分证据已保留'
  }
  if (value.status === 'technical_failed') return '未封存'
  return '等待封存'
})

const sessionClosureText = computed(() => {
  const value = exploration.value
  if (!value) return '待关闭'
  const sessionStatus = value.surface_session_status || 'unknown'
  if (value.status === 'close_failed' || ['lost', 'failed'].includes(sessionStatus)) {
    return `会话关闭失败 · ${sessionStatus}`
  }
  if (sessionStatus === 'closed') return `已关闭 · ${sessionStatus}`
  if (value.status === 'session_draining') return `关闭中 · ${sessionStatus}`
  return `待关闭 · ${sessionStatus}`
})

const inspectionText = computed(() => {
  if (isExplorationOnly.value) return '不适用'
  const summary = props.stage?.inspection_summary
  const projection = publication.value
  if (!projection || projection.current_stage === 'not_started') {
    return `尚未持久发布 · 工作项 ${summary?.work_item_count ?? 0}`
  }
  const labels = { sealed: '已封存', delivered: '已投递', claimed: '已领取', judged: '已判定' }
  return `${labels[projection.current_stage]} · 工作项 ${summary?.work_item_count ?? 0} · 未终态 ${summary?.nonterminal_count ?? 0}`
})

const publicationEntries = computed(() =>
  publication.value?.items.flatMap((item) => item.entries) ?? [],
)

const reportReviewText = computed(() => {
  if (isExplorationOnly.value) return '不适用'
  const gate = props.stage?.gate
  if (!gate) return '尚未评估'
  if (gate.gate_passed) return '已通过'
  return gate.block_reasons?.length ? gate.block_reasons.join(', ') : '未通过'
})

function coverageCount(value: AppExplorationProjection | null, key: string): number {
  const raw = value?.coverage_summary[key]
  return typeof raw === 'number' ? raw : 0
}

function coverageList(key: string): string[] {
  const raw = exploration.value?.coverage_summary[key]
  return Array.isArray(raw) ? raw.map(String) : []
}

const appTarget = computed(() => {
  const value = exploration.value?.coverage_summary.app_target_snapshot
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
})

const degradedProviderFacts = computed(() => {
  const value = exploration.value?.coverage_summary.degraded_provider_facts
  return Array.isArray(value) ? value : []
})

const coveredBusinessPages = computed<AppBusinessPageProjection[]>(() => {
  const value = exploration.value?.coverage_summary.covered_business_pages
  if (!Array.isArray(value)) return []
  return value.filter(
    (item): item is AppBusinessPageProjection =>
      Boolean(
        item &&
          typeof item === 'object' &&
          !Array.isArray(item) &&
          typeof (item as Record<string, unknown>).page_group_key === 'string' &&
          typeof (item as Record<string, unknown>).page_display_name === 'string',
      ),
  )
})
</script>

<template>
  <section class="app-stage-panel" data-testid="app-stage-panel">
    <header class="panel-header">
      <div>
        <h3>App 探索闭环</h3>
        <p>从构建校验到报告复核的可审计阶段投影</p>
      </div>
      <span
        v-if="stage?.applicable"
        class="overall-status"
        :class="exploration?.status"
        data-testid="exploration-status"
      >
        {{ explorationStatusText }}
      </span>
    </header>

    <div v-if="loading" class="app-stage-muted">加载中…</div>
    <template v-else-if="stage?.applicable">
      <div v-if="!exploration" class="legacy-notice">
        旧任务暂无 App Exploration 封存事实，仅展示可兼容读取的启动与质检摘要。
      </div>

      <div class="app-stage-grid">
        <article class="stage-card">
          <span class="stage-index">01</span>
          <span class="label">Build Validation</span>
          <strong>{{ buildValidationLabel }}</strong>
          <p class="hint">构建解析与目标包校验</p>
        </article>

        <article class="stage-card">
          <span class="stage-index">02</span>
          <span class="label">Bootstrap</span>
          <strong>{{ bootstrapLabel }}</strong>
          <p v-if="stage.bootstrap?.installed_package" class="hint mono">
            {{ stage.bootstrap.installed_package }}
          </p>
          <p v-if="stage.bootstrap?.error_code" class="hint error">
            {{ stage.bootstrap.error_code }}：{{ stage.bootstrap.error_message }}
          </p>
        </article>

        <article class="stage-card">
          <span class="stage-index">03</span>
          <span class="label">Surface Readiness</span>
          <strong>{{ surfaceReadinessText }}</strong>
          <p v-if="exploration?.current_page_identity" class="hint mono">
            {{ exploration.current_page_identity }}
          </p>
        </article>

        <article class="stage-card">
          <span class="stage-index">04</span>
          <span class="label">Page Exploration</span>
          <strong>{{ explorationStatusText }}</strong>
          <p class="hint">
            完成 {{ exploration?.completed_work_count ?? 0 }} · 待处理
            {{ exploration?.pending_work_count ?? 0 }} · 动作
            {{ exploration?.action_count ?? 0 }} · 重观察
            {{ exploration?.reobservation_count ?? 0 }}
          </p>
          <p v-if="exploration" class="hint">
            业务页面 {{ coverageCount(exploration, 'covered_business_page_count') }} · 页型
            {{ coverageCount(exploration, 'covered_page_type_count') }} · 证据
            {{ coverageCount(exploration, 'indexed_capture_count') }}
          </p>
          <div
            v-if="coveredBusinessPages.length"
            class="business-page-list"
            data-testid="covered-business-pages"
          >
            <span
              v-for="page in coveredBusinessPages"
              :key="page.page_group_key"
              :title="`${page.page_display_name} · Capture ${page.capture_count}`"
            >
              {{ page.page_display_name }} ×{{ page.capture_count }}
            </span>
          </div>
          <p v-if="exploration" class="hint">
            目标页型 {{ coverageList('required_page_types').join('、') || '未锁定' }} ·
            已覆盖 {{ coverageList('covered_page_types').join('、') || '无' }}
          </p>
          <p v-if="coverageList('missing_page_types').length" class="hint error">
            缺失页型 {{ coverageList('missing_page_types').join('、') }}
          </p>
          <p v-if="coverageList('missing_milestones').length" class="hint error">
            缺失里程碑 {{ coverageList('missing_milestones').join('、') }}
          </p>
          <p v-if="appTarget" class="hash mono">
            target {{ String(appTarget.target_hash || 'hash 缺失') }}
          </p>
          <p v-if="degradedProviderFacts.length" class="hint error">
            视觉 Provider 已降级 {{ degradedProviderFacts.length }} 次，任务只能进入证据不足/复核
          </p>
        </article>

        <article class="stage-card">
          <span class="stage-index">05</span>
          <span class="label">Evidence Validation</span>
          <strong>{{ evidenceValidationText }}</strong>
          <p v-if="exploration?.terminal_reason_code" class="hint mono">
            {{ exploration.terminal_reason_code }}
          </p>
          <p v-if="exploration?.technical_failure" class="hint error">
            {{ exploration.technical_failure.phase }} ·
            {{ exploration.technical_failure.error_type || exploration.technical_failure.reason_code }} ·
            {{ exploration.technical_failure.recovery_state }}
          </p>
        </article>

        <article class="stage-card">
          <span class="stage-index">06</span>
          <span class="label">Evidence Sealing</span>
          <strong>{{ evidenceSealingText }}</strong>
          <p v-if="exploration?.evidence_index_id" class="hint mono">
            {{ exploration.evidence_index_id }}
          </p>
          <p v-if="exploration?.evidence_index_hash" class="hash mono">
            {{ exploration.evidence_index_hash }}
          </p>
        </article>

        <article class="stage-card" data-testid="session-closure-stage">
          <span class="stage-index">07</span>
          <span class="label">Session Closure</span>
          <strong>{{ sessionClosureText }}</strong>
          <p v-if="exploration?.completed_at" class="hint">{{ exploration.completed_at }}</p>
        </article>

        <article class="stage-card publication-stage" data-testid="app-publication-stage">
          <span class="stage-index">08</span>
          <span class="label">Formal Inspection</span>
          <strong>{{ inspectionText }}</strong>
          <p class="hint">{{ isExplorationOnly ? '仅页面探索不创建正式质检工作项' : '仅消费已封存证据快照' }}</p>
          <template v-if="!isExplorationOnly && publication">
            <div class="publication-flow" aria-label="App Publication 四阶段">
              <span :class="{ reached: publication.sealed_count > 0 }">封存</span>
              <span :class="{ reached: publication.delivered_count > 0 }">投递</span>
              <span :class="{ reached: publication.claimed_count > 0 }">领取</span>
              <span :class="{ reached: publication.judged_count > 0 }">判定</span>
            </div>
            <p
              v-if="publication.outbox_dead_letter_count || publication.inbox_dead_letter_count"
              class="hint error"
            >
              死信：Outbox {{ publication.outbox_dead_letter_count }} · Inbox
              {{ publication.inbox_dead_letter_count }}
            </p>
            <p v-if="publication.needs_review_count || publication.failed_count" class="hint error">
              Publication 需复核 {{ publication.needs_review_count }} · 失败
              {{ publication.failed_count }}
            </p>
            <div v-if="publicationEntries.length" class="publication-entries">
              <div v-for="entry in publicationEntries" :key="entry.entry_id" class="publication-entry">
                <span>#{{ entry.sequence_no }} · {{ entry.semantic_role }}</span>
                <strong>{{ entry.page_type || '页面类型未识别' }} · {{ entry.status }}</strong>
                <small>
                  {{ entry.current_stage }} · Work {{ entry.linked_work_item_count }} · 活会话排除
                  {{ entry.explicit_exclusion_count }}
                </small>
              </div>
            </div>
          </template>
        </article>

        <article class="stage-card report-stage">
          <span class="stage-index">09</span>
          <span class="label">Report / Review</span>
          <strong :class="{ ok: !isExplorationOnly && stage.gate?.gate_passed }">
            {{ reportReviewText }}
          </strong>
          <p v-if="isExplorationOnly" class="hint">
            {{ props.canTestRules ? '完成后可从独立规则测试入口复用已封存证据' : '只探索页面，不执行正式规则' }}
          </p>
          <p v-else-if="exploration?.outcome" class="hint mono">{{ exploration.outcome }}</p>
        </article>
      </div>
    </template>
    <div v-else-if="stage" class="app-stage-muted">当前任务不是 App 质检任务。</div>
    <div v-else class="app-stage-muted">App 阶段数据尚未加载</div>
  </section>
</template>

<style scoped>
.app-stage-panel {
  margin: 12px 0 4px;
  padding: 14px;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  background: var(--workstation-panel-soft);
  color: var(--workstation-text, #e2e8f0);
}
.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.panel-header h3,
.panel-header p {
  margin: 0;
}
.panel-header h3 {
  font-size: 14px;
}
.panel-header p,
.hint {
  margin-top: 4px;
  color: var(--workstation-text-faint, #94a3b8);
  font-size: 12px;
}
.overall-status {
  flex: 0 0 auto;
  padding: 3px 9px;
  border-radius: 999px;
  background: rgba(56, 189, 248, 0.12);
  color: var(--workstation-primary);
  font-size: 12px;
  font-weight: 600;
}
.overall-status.exploration_completed {
  background: rgba(52, 211, 153, 0.12);
  color: var(--workstation-success, #34d399);
}
.overall-status.evidence_insufficient,
.overall-status.close_failed {
  background: rgba(251, 191, 36, 0.12);
  color: var(--workstation-warning);
}
.overall-status.technical_failed {
  background: rgba(248, 113, 113, 0.12);
  color: var(--workstation-danger, #f87171);
}
.legacy-notice {
  margin-bottom: 12px;
  padding: 8px 10px;
  border-left: 3px solid #fbbf24;
  background: rgba(251, 191, 36, 0.08);
  color: var(--workstation-warning);
  font-size: 12px;
}
.app-stage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px;
}
.stage-card {
  position: relative;
  min-width: 0;
  min-height: 94px;
  padding: 11px 12px;
  overflow: hidden;
  border: 1px solid var(--workstation-border);
  border-radius: 8px;
  background: var(--workstation-panel-soft);
}
.publication-stage {
  grid-column: span 2;
}
.publication-flow {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 5px;
  margin-top: 10px;
}
.publication-flow span {
  padding: 4px 5px;
  border: 1px solid var(--workstation-border);
  border-radius: 5px;
  color: var(--workstation-text-faint, #94a3b8);
  font-size: 11px;
  text-align: center;
}
.publication-flow span.reached {
  border-color: rgba(52, 211, 153, 0.35);
  background: rgba(52, 211, 153, 0.08);
  color: var(--workstation-success, #34d399);
}
.publication-entries {
  display: grid;
  gap: 5px;
  margin-top: 8px;
}
.publication-entry {
  display: grid;
  grid-template-columns: minmax(120px, 0.8fr) minmax(150px, 1fr) minmax(180px, 1.2fr);
  gap: 8px;
  align-items: center;
  padding-top: 5px;
  border-top: 1px solid var(--workstation-border);
  font-size: 11px;
}
.publication-entry strong {
  padding: 0;
  font-size: 11px;
}
.publication-entry small {
  color: var(--workstation-text-faint, #94a3b8);
}
.business-page-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 7px;
}
.business-page-list span {
  max-width: 100%;
  padding: 2px 6px;
  overflow: hidden;
  border: 1px solid rgba(56, 189, 248, 0.24);
  border-radius: 999px;
  background: rgba(56, 189, 248, 0.08);
  color: var(--workstation-primary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 720px) {
  .publication-stage {
    grid-column: span 1;
  }
  .publication-entry {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
.stage-index {
  position: absolute;
  top: 7px;
  right: 9px;
  color: var(--workstation-text-faint);
  font: 600 18px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.label {
  display: block;
  margin-bottom: 7px;
  color: var(--workstation-text-faint, #94a3b8);
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.stage-card strong {
  display: block;
  padding-right: 22px;
  color: var(--workstation-text, #e2e8f0);
  font-size: 13px;
  line-height: 1.45;
  word-break: break-word;
}
.hint,
.hash {
  margin-bottom: 0;
  word-break: break-all;
}
.hash {
  margin-top: 5px;
  color: var(--workstation-primary);
  font-size: 11px;
}
.error {
  color: var(--workstation-danger, #f87171);
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.report-stage strong.ok {
  color: var(--workstation-success, #34d399);
}
.app-stage-muted {
  font-size: 13px;
  color: var(--workstation-text-faint, #94a3b8);
}
</style>
