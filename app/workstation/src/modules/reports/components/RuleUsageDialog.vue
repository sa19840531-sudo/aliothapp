<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NModal } from 'naive-ui'
import type { ReportRuleItem } from '@/types/report-insight'
import {
  aiResultOf,
  confidenceText,
  confirmReason,
  finalResultOf,
  executionStateText,
  isUndecidableResult,
  isExecutionReportItem,
  machineExplanation,
  missingEvidenceText,
  rawResultText,
  humanConfirmationAuditText,
  severityChipClass,
  severityText,
} from './reportDisplay'
import { rulePageName, usageSummaryText, type RuleRow } from './ruleGroups'
import JourneyResultDetails from './JourneyResultDetails.vue'
import { issueEvidenceFileUrl } from '@/services/task-insight.api'
import GreenRuleResultDetails from '@/modules/tasks/components/GreenRuleResultDetails.vue'
import { isLinkedGreenResult } from '@/modules/tasks/components/greenRuleResultDetails'

/**
 * 规则详情弹窗：这条规则查什么，在这次任务里一共用了几次，每一次在哪一页、机器怎么判、
 * 人工怎么定、留了哪些截图。开关由父组件单独持有，关闭时只收开关、内容留着让关闭动画走完
 * （09-21 报告页「!」弹窗内容和开关一起清掉，页面锁死滚动）。
 */
const props = defineProps<{
  show: boolean
  rule: RuleRow | null
  targetPlatform: string | null
  shotIds: (item: ReportRuleItem) => string[]
  shotUrl: (artifactId: string) => string
  /** 截图还在加载：先说「截图加载中…」，不说「没有截图证据」 */
  shotsLoading?: boolean
  shotsError?: string
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'after-leave': []
  'open-shots': [artifactIds: string[], index: number]
  'green-view-evidence': [item: ReportRuleItem, artifactId: string]
  judge: [executionId: string]
}>()

const lead = computed(() => props.rule?.lead ?? null)
const isH5 = computed(() => props.targetPlatform === 'h5')
const materialError = ref('')
let materialGeneration = 0
watch(() => [props.show, props.rule], () => {
  materialGeneration += 1
  materialError.value = ''
}, { flush: 'sync' })
onBeforeUnmount(() => { materialGeneration += 1 })
async function openMaterial(item: ReportRuleItem, artifactId: string) {
  const generation = materialGeneration
  const current = () => generation === materialGeneration && props.show && props.rule?.items.includes(item)
  materialError.value = ''
  if (!current() || !item.evidence_artifact_ids.includes(artifactId)) return
  try {
    const url = await issueEvidenceFileUrl(artifactId, { download: true })
    if (current()) window.open(url, '_blank', 'noopener,noreferrer')
  } catch (cause) {
    if (current()) materialError.value = cause instanceof Error ? cause.message : '原材料不可读取'
  }
}

function knownFailure(item: ReportRuleItem) {
  return isLinkedGreenResult(item.green_result) && item.green_result.execution_status === 'fail'
}
const executionGroups = computed(() => {
  const entries = (props.rule?.items ?? []).map((item, index) => ({ item, index }))
  return [
    { key: 'executed', title: '已执行结果', entries: entries.filter(({ item }) => isExecutionReportItem(item) && (!isUndecidableResult(item) || knownFailure(item))) },
    { key: 'undecidable', title: '未执行与无法判定', entries: entries.filter(({ item }) => isExecutionReportItem(item) && isUndecidableResult(item) && !knownFailure(item)) },
    { key: 'candidate', title: '适用性核对与未执行处置', entries: entries.filter(({ item }) => !isExecutionReportItem(item)) },
  ].filter((group) => group.entries.length)
})

const metaText = computed(() => {
  const item = lead.value
  if (!item) return ''
  return [item.rule_code, item.ruleset_name, item.rule_package].filter(Boolean).join(' · ')
})

function usageTime(value: string | null) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function confirmText(item: ReportRuleItem) {
  if (item.confirm_status === 'pending') return '待确认'
  const note = item.human_verdict_note?.trim()
  const prefix = isUndecidableResult(item) ? '历史记录（不作为业务结论）' : '已确认为'
  return `${prefix}「${item.human_verdict_label || finalResultOf(item).label}」${note ? `，备注：${note}` : ''}`
}

/** 只说明当前截图目录缺少引用材料，不将缺失材料补成截图。 */
function missingShotIds(item: ReportRuleItem) {
  const available = new Set(props.shotIds(item))
  return [...new Set(item.evidence_artifact_ids)].filter((id) => !available.has(id))
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    :title="rule?.rule_name ?? ''"
    class="rule-usage-modal"
    :style="{ width: 'min(820px, 94vw)' }"
    :bordered="false"
    @update:show="emit('update:show', $event)"
    @after-leave="emit('after-leave')"
  >
    <div v-if="rule && lead" class="rule-usage" data-testid="rule-usage-dialog">
      <p v-if="materialError" role="alert">{{ materialError }}</p>
      <p class="usage-meta">
        <span class="severity-chip" :class="severityChipClass(lead)">{{ severityText(lead) }}</span>
        <span>{{ metaText }}</span>
      </p>

      <section v-if="lead.check_point || lead.trigger_display" class="usage-section">
        <h4>这条规则查什么</h4>
        <p v-if="lead.check_point">{{ lead.check_point }}</p>
        <p v-if="lead.trigger_display" class="usage-muted">触发条件：{{ lead.trigger_display }}</p>
      </section>

      <section class="usage-section">
        <h4>在这次任务里的记录与处置</h4>
        <p class="usage-summary" data-testid="rule-usage-summary">{{ usageSummaryText(rule) }}</p>
        <section v-for="group in executionGroups" :key="group.key" :data-testid="`rule-usage-group-${group.key}`">
        <h4 class="usage-group-title">{{ group.title }}（{{ group.entries.length }} {{ group.key === 'candidate' ? '条' : '次' }}）</h4>
        <p v-if="group.key === 'undecidable'" class="usage-muted">这些记录没有形成业务结论，不作为未通过结果。</p>
        <ol class="usage-list">
          <li
            v-for="{ item, index } in group.entries"
            :key="item.execution_id || `candidate-${item.rule_id}`"
            class="usage-card"
            :data-testid="`rule-usage-${item.execution_id}`"
          >
            <header class="usage-card-head">
              <strong>{{ isExecutionReportItem(item) ? `第 ${index + 1} 次 · ${rulePageName(item)}` : '规则适用性与未执行处置' }}</strong>
              <span v-if="item.finished_at" class="usage-muted">{{ usageTime(item.finished_at) }}</span>
            </header>
            <GreenRuleResultDetails v-if="item.green_result" :result="item.green_result"
              :allowed-screenshot-ids="shotIds(item)"
              @view-evidence="emit('green-view-evidence', item, $event)" />
            <div v-if="!item.green_result" class="usage-verdicts">
              <span>AI 判定</span>
              <span class="qc-chip" :class="aiResultOf(item).tone">{{ aiResultOf(item).label }}</span>
              <span>最终结果</span>
              <span class="qc-chip" :class="finalResultOf(item).tone">{{ finalResultOf(item).label }}</span>
              <span v-if="item.confirm_status || item.human_verdict" class="usage-muted">人工确认：{{ confirmText(item) }}</span>
            </div>
            <p v-if="!item.green_result && humanConfirmationAuditText(item)" class="usage-muted" :data-testid="`rule-confirm-audit-${item.execution_id}`">
              {{ humanConfirmationAuditText(item) }}
            </p>
            <div v-if="!item.green_result" class="usage-grid">
              <div>
                <span>执行状态</span>
                <p>{{ executionStateText(item) }}</p>
              </div>
              <div>
              <span>{{ isExecutionReportItem(item) ? '机器判定说明' : '处置依据' }}</span>
                <p>{{ machineExplanation(item) }}</p>
              </div>
              <div v-if="!isExecutionReportItem(item) && item.inventory_source">
                <span>规则清单依据</span>
                <p>{{ item.inventory_source === 'frozen' ? '任务提交时保存的规则清单' : item.inventory_source === 'legacy_verified' ? '已核验创建后未变更的历史规则清单' : '规则清单尚未核验' }}</p>
              </div>
              <div v-if="confirmReason(item)">
                <span>{{ item.display_status === 'not_applicable' ? '不适用原因' : '识别详情' }}</span>
                <p>{{ item.problem_desc }}</p>
              </div>
              <div>
                <span>原始执行态</span>
                <p>{{ rawResultText(item) }}</p>
              </div>
              <div>
                <span>识别把握度</span>
                <p>{{ confidenceText(item.confidence) }}</p>
              </div>
              <div v-if="isH5 && item.missing_evidence?.length">
                <span>缺失证据</span>
                <p>{{ missingEvidenceText(item) }}</p>
              </div>
              <div v-if="isH5 && item.evidence_input_set_id">
                <span>证据输入集</span>
                <p class="qc-mono" :title="item.evidence_input_set_id">{{ item.evidence_input_set_id }}</p>
              </div>
            </div>
            <div v-if="!item.green_result" class="usage-shots">
              <JourneyResultDetails v-if="isH5 && item.journey_result" :result="item.journey_result"
                :available-shot-ids="shotIds(item)" @open-shots="(ids, index) => emit('open-shots', ids, index)"
                @open-material="(id) => openMaterial(item, id)" />
              <button
                v-for="(artifactId, shotIndex) in shotIds(item)"
                :key="artifactId"
                type="button"
                class="usage-shot"
                title="看大图"
                :data-testid="`rule-usage-shot-${artifactId}`"
                @click="emit('open-shots', shotIds(item), shotIndex)"
              >
                <img :src="shotUrl(artifactId)" alt="截图证据" loading="lazy" />
              </button>
              <span v-if="!shotIds(item).length" class="usage-muted">{{ !isExecutionReportItem(item) ? '未进入规则执行，未生成判定截图' : shotsLoading ? '截图加载中…' : shotsError || (item.evidence_artifact_ids.length ? '关联材料未提供可查看截图' : '这一次没有截图证据') }}</span>
            </div>
            <p v-if="!item.green_result && !shotsLoading && !shotsError && missingShotIds(item).length" class="usage-muted"
              :data-testid="`rule-missing-shots-${item.execution_id}`">
              当前截图清单未提供以下判定材料：{{ missingShotIds(item).join('、') }}。可使用材料标识核对原始记录。
            </p>
            <button
              v-if="isExecutionReportItem(item) && item.confirmable && !isUndecidableResult(item)"
              type="button"
              class="qc-link usage-judge"
              :data-testid="`rule-usage-judge-${item.execution_id}`"
              @click="emit('judge', item.execution_id)"
            >
              {{ item.confirm_status === 'pending' ? '去人工判定' : '修改人工判定' }}
            </button>
          </li>
        </ol>
        </section>
      </section>
    </div>
  </n-modal>
</template>

<style scoped>
.rule-usage {
  display: grid;
  gap: 16px;
  max-height: 72vh;
  overflow-y: auto;
  padding-right: 4px;
}

.usage-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.usage-section h4 {
  margin: 0 0 8px;
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.usage-section p {
  margin: 0 0 4px;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
}

.usage-muted {
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.usage-summary {
  font-weight: 500;
}

.usage-group-title {
  margin-top: 14px !important;
}

.usage-list {
  display: grid;
  gap: 10px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.usage-card {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
}

.usage-card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  color: var(--workstation-text-strong);
  font-size: 13px;
}

.usage-verdicts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.usage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 10px 14px;
}

.usage-grid span {
  display: block;
  margin-bottom: 2px;
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.usage-grid p {
  margin: 0;
  color: var(--workstation-text);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.usage-shots {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.usage-shot {
  width: 48px;
  height: 64px;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--workstation-border);
  border-radius: 6px;
  background: var(--workstation-panel);
  cursor: zoom-in;
}

.usage-shot img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.usage-judge {
  justify-self: start;
}
</style>
