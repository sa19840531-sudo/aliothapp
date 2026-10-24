<script setup lang="ts">
import { computed, nextTick, ref, watch, type ComponentPublicInstance } from 'vue'
import { useMessage } from 'naive-ui'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { confirmReportRule } from '@/services/reports.api'
import { AliothHttpError } from '@/services/http'
import type { HumanVerdict, ReportExecutionItem as ReportRuleItem, ReportRuleItem as AnyReportRuleItem } from '@/types/report-insight'
import { HUMAN_VERDICT_LABELS } from '@/utils/status'
import {
  confirmReason,
  machineExplanation,
  rawResultText,
  humanConfirmationAuditText,
  severityChipClass,
  severityText,
  isExecutionReportItem,
} from './reportDisplay'

/**
 * 人工确认规则：仅已执行且业务结论待复核的规则逐条判定，选完立即提交并通知页面刷新。
 * 检测缺口不通过人工结论补成完成；截图缩略图与规则表共用大图查看器。
 * 截图缩略图由页面通过 thumb 插槽提供，和规则表共用同一个大图查看器。
 *
 * 同一条规则落在多个页面时合成一组：一行汇总 + 整组判定，需要时再展开逐条改（一个任务
 * 动辄几百条待确认，平铺没法看）。只有一条的规则照旧单独一行。
 */
const props = defineProps<{
  taskId: string
  items: AnyReportRuleItem[]
}>()

const emit = defineEmits<{ confirmed: [item: ReportRuleItem] }>()

defineSlots<{ thumb?: (props: { item: ReportRuleItem }) => unknown }>()

const message = useMessage()

const VERDICT_OPTIONS: HumanVerdict[] = ['pass', 'fail', 'ignored']

/** 已提交、页面还没刷新到的判定：先显示用户的选择，刷新后以接口数据为准，提交失败则撤回。 */
const drafts = ref<Record<string, HumanVerdict>>({})
const submittingId = ref<string | null>(null)
/** 逐条的人工判定下拉（SearchSelect 实例）：聚焦用它暴露的 focus，滚动用它的根元素。 */
type VerdictSelect = InstanceType<typeof SearchSelect>
const selectElements = new Map<string, VerdictSelect>()

const executionItems = computed(() => props.items.filter(isExecutionReportItem))
const pendingCount = computed(() => executionItems.value.filter((item) => item.confirm_status === 'pending').length)

interface ConfirmGroup {
  key: string
  first: ReportRuleItem
  items: ReportRuleItem[]
  pending: number
  pages: string[]
}

const groups = computed<ConfirmGroup[]>(() => {
  const byRule = new Map<string, ConfirmGroup>()
  for (const item of executionItems.value) {
    let group = byRule.get(item.rule_code)
    if (!group) {
      group = { key: item.rule_code, first: item, items: [], pending: 0, pages: [] }
      byRule.set(item.rule_code, group)
    }
    group.items.push(item)
    if (item.confirm_status === 'pending') {
      group.pending += 1
    }
    const page = item.bound_page_type_name || '未识别页面'
    if (!group.pages.includes(page)) {
      group.pages.push(page)
    }
  }
  return [...byRule.values()]
})

const expandedGroups = ref<Set<string>>(new Set())
const submittingGroup = ref<string | null>(null)

function isGroupOpen(group: ConfirmGroup): boolean {
  return group.items.length === 1 || expandedGroups.value.has(group.key)
}

function toggleGroup(key: string) {
  const next = new Set(expandedGroups.value)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  expandedGroups.value = next
}

function groupPagesText(group: ConfirmGroup): string {
  const shown = group.pages.slice(0, 3).join('、')
  return group.pages.length > 3 ? `${shown} 等 ${group.pages.length} 类页面` : shown
}

/** 整组下拉：组内判定一致时显示该判定，不一致或还没判时留空。 */
function groupVerdict(group: ConfirmGroup): HumanVerdict | '' {
  const verdicts = new Set(group.items.map((item) => selectedVerdict(item)))
  if (verdicts.size !== 1) {
    return ''
  }
  const [only] = [...verdicts]
  return only ?? ''
}

async function onGroupSelect(group: ConfirmGroup, verdict: HumanVerdict | '') {
  if (!verdict) {
    return
  }
  const targets = group.items.filter((item) => selectedVerdict(item) !== verdict)
  if (!targets.length) {
    return
  }
  const next = { ...drafts.value }
  for (const item of targets) {
    next[item.execution_id] = verdict
  }
  drafts.value = next
  submittingGroup.value = group.key
  let failed = 0
  let lastError: unknown = null
  for (const item of targets) {
    try {
      await confirmReportRule(props.taskId, item.execution_id, { verdict })
    } catch (error) {
      failed += 1
      lastError = error
      dropDraft(item.execution_id)
    }
  }
  submittingGroup.value = null
  const succeeded = targets.length - failed
  if (succeeded > 0) {
    message.success(
      `已提交人工判定：${group.first.rule_name} · ${HUMAN_VERDICT_LABELS[verdict]}（${succeeded} 条）`,
    )
    emit('confirmed', group.first)
  }
  if (failed > 0) {
    const detail =
      lastError instanceof AliothHttpError && lastError.message ? `：${lastError.message}` : '，请稍后重试'
    message.error(`有 ${failed} 条人工判定提交失败${detail}`)
  }
}

const progress = computed(() => {
  if (!executionItems.value.length) {
    return { text: '无需人工确认', tone: 'is-done' }
  }
  return pendingCount.value > 0
    ? { text: `待确认 ${pendingCount.value} 条`, tone: 'is-pending' }
    : { text: '已全部确认，可生成导出报告', tone: 'is-done' }
})

watch(
  () => executionItems.value,
  (items) => {
    const next = { ...drafts.value }
    for (const item of items) {
      if (next[item.execution_id] === item.human_verdict) {
        delete next[item.execution_id]
      }
    }
    drafts.value = next
  },
)

function selectedVerdict(item: ReportRuleItem): HumanVerdict | '' {
  return drafts.value[item.execution_id] ?? item.human_verdict ?? ''
}

function dropDraft(executionId: string) {
  const next = { ...drafts.value }
  delete next[executionId]
  drafts.value = next
}

async function onSelect(item: ReportRuleItem, verdict: HumanVerdict | '') {
  if (!verdict || verdict === selectedVerdict(item)) {
    return
  }
  drafts.value = { ...drafts.value, [item.execution_id]: verdict }
  submittingId.value = item.execution_id
  try {
    await confirmReportRule(props.taskId, item.execution_id, { verdict })
    message.success(`已提交人工判定：${item.rule_name} · ${HUMAN_VERDICT_LABELS[verdict]}`)
    emit('confirmed', item)
  } catch (error) {
    dropDraft(item.execution_id)
    message.error(
      error instanceof AliothHttpError && error.message ? error.message : '人工判定提交失败，请稍后重试',
    )
  } finally {
    submittingId.value = null
  }
}

function bindSelect(executionId: string, element: Element | ComponentPublicInstance | null) {
  if (element && !(element instanceof Element)) {
    selectElements.set(executionId, element as VerdictSelect)
  } else {
    selectElements.delete(executionId)
  }
}

function focusSelect(select: VerdictSelect) {
  select.focus({ preventScroll: true })
  const root = select.$el as HTMLElement | null
  if (root && typeof root.scrollIntoView === 'function') {
    root.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

/** 规则表「人工判定」：滚到该规则的判定下拉并聚焦（所在的组收着就先展开）；找不到返回 false。 */
function focusItem(executionId: string): boolean {
  const select = selectElements.get(executionId)
  if (select) {
    focusSelect(select)
    return true
  }
  const group = groups.value.find((entry) =>
    entry.items.some((item) => item.execution_id === executionId),
  )
  if (!group) {
    return false
  }
  expandedGroups.value = new Set(expandedGroups.value).add(group.key)
  void nextTick(() => {
    const opened = selectElements.get(executionId)
    if (opened) {
      focusSelect(opened)
    }
  })
  return true
}

defineExpose({ focusItem })
</script>

<template>
  <section class="workstation-glass-panel confirm-card" data-testid="confirm-section">
    <div class="confirm-head">
      <div>
        <h2>人工确认规则</h2>
        <p>仅已执行且业务结论待复核的规则可人工确认。未执行、证据不足和检测异常保留原始状态，不能人工改为通过或未通过。</p>
      </div>
      <span class="confirm-progress" :class="progress.tone" data-testid="confirm-progress">{{ progress.text }}</span>
    </div>

    <div v-if="items.length" class="confirm-list">
      <template v-for="group in groups" :key="group.key">
      <div
        v-if="group.items.length > 1"
        class="confirm-item confirm-group-head"
        :class="{ 'is-confirmed': group.pending === 0 }"
        :data-testid="`confirm-group-${group.key}`"
      >
        <div class="confirm-main">
          <slot name="thumb" :item="group.first" />
          <div class="confirm-text">
            <div class="confirm-title">
              <span class="severity-chip" :class="severityChipClass(group.first)">{{ severityText(group.first) }}</span>
              <strong>{{ group.first.rule_name }}</strong>
              <span class="confirm-count">{{ group.items.length }} 条</span>
            </div>
            <p class="confirm-meta">
              {{ group.key }} · {{ groupPagesText(group) }} · {{ rawResultText(group.first) }} · 待确认 {{ group.pending }} 条
            </p>
            <p class="confirm-desc">{{ machineExplanation(group.first) }}</p>
            <p v-if="confirmReason(group.first)" class="confirm-reason">原因：{{ confirmReason(group.first) }}</p>
            <button
              type="button"
              class="confirm-toggle"
              :data-testid="`confirm-group-toggle-${group.key}`"
              @click="toggleGroup(group.key)"
            >
              {{ expandedGroups.has(group.key) ? '收起逐条' : `逐条查看 ${group.items.length} 条` }}
            </button>
          </div>
        </div>
        <SearchSelect
          class="confirm-select"
          :model-value="groupVerdict(group)"
          :disabled="submittingGroup === group.key"
          :aria-label="`${group.first.rule_name} 整组人工判定`"
          :data-testid="`confirm-group-select-${group.key}`"
          @update:model-value="onGroupSelect(group, $event as HumanVerdict | '')"
        >
          <option value="" disabled>整组判定</option>
          <option v-for="verdict in VERDICT_OPTIONS" :key="verdict" :value="verdict">
            {{ HUMAN_VERDICT_LABELS[verdict] }}
          </option>
        </SearchSelect>
      </div>
      <template v-if="isGroupOpen(group)">
      <div
        v-for="item in group.items"
        :key="item.execution_id"
        class="confirm-item"
        :class="{ 'is-confirmed': item.confirm_status === 'confirmed', 'is-grouped': group.items.length > 1 }"
        :data-testid="`confirm-item-${item.rule_code}`"
      >
        <div class="confirm-main">
          <slot name="thumb" :item="item" />
          <div class="confirm-text">
            <div class="confirm-title">
              <span class="severity-chip" :class="severityChipClass(item)">{{ severityText(item) }}</span>
              <strong>{{ item.rule_name }}</strong>
            </div>
            <p class="confirm-meta">
              {{ item.rule_code }} · {{ item.bound_page_type_name || '未识别页面' }} · {{ rawResultText(item) }}
            </p>
            <p class="confirm-desc">{{ machineExplanation(item) }}</p>
            <p v-if="confirmReason(item)" class="confirm-reason">原因：{{ confirmReason(item) }}</p>
            <p v-if="humanConfirmationAuditText(item)" class="confirm-meta" :data-testid="`confirm-audit-${item.execution_id}`">
              {{ humanConfirmationAuditText(item) }}
            </p>
          </div>
        </div>
        <SearchSelect
          :ref="(element) => bindSelect(item.execution_id, element)"
          class="confirm-select"
          :model-value="selectedVerdict(item)"
          :disabled="submittingId === item.execution_id"
          :aria-label="`${item.rule_name} 的人工判定`"
          :data-testid="`confirm-select-${item.rule_code}`"
          @update:model-value="onSelect(item, $event as HumanVerdict | '')"
        >
          <option value="" disabled>请选择人工判定</option>
          <option v-for="verdict in VERDICT_OPTIONS" :key="verdict" :value="verdict">
            {{ HUMAN_VERDICT_LABELS[verdict] }}
          </option>
        </SearchSelect>
      </div>
      </template>
      </template>
    </div>
    <p v-else class="confirm-empty">本报告没有需要人工确认的规则</p>
  </section>
</template>

<style scoped>
.confirm-card {
  padding: 20px;
}

.confirm-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.confirm-head h2 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 600;
}

.confirm-head p {
  margin: 4px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.confirm-progress {
  flex-shrink: 0;
  font-size: 12px;
  white-space: nowrap;
}

.confirm-progress.is-pending {
  color: var(--workstation-warning);
}

.confirm-progress.is-done {
  color: var(--workstation-success);
}

/* 待确认规则可能有几十条：列表内部滚动，避免把规则执行结果列表挤到很远 */
.confirm-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 640px;
  margin-top: 16px;
  padding: 8px 8px 2px 2px;
  overflow-y: auto;
}

.confirm-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px;
  border: 1px solid var(--workstation-warning);
  border-color: color-mix(in srgb, var(--workstation-warning) 25%, transparent);
  border-radius: var(--workstation-radius-sm);
  background: var(--tint-warning);
}

.confirm-item.is-confirmed {
  border-color: var(--workstation-border);
  background: var(--workstation-panel-soft);
}

.confirm-main {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
}

.confirm-text {
  min-width: 0;
}

.confirm-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.confirm-title strong {
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 500;
}

.confirm-meta {
  margin: 4px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.confirm-desc {
  margin: 8px 0 0;
  color: var(--workstation-text);
  font-size: 12px;
  line-height: 1.6;
}

.confirm-count {
  margin-left: 8px;
  color: var(--workstation-text-muted);
  font-size: 12px;
  font-weight: 400;
}

.confirm-toggle {
  margin-top: 6px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--workstation-accent, #2563eb);
  font-size: 12px;
  cursor: pointer;
}

.confirm-item.is-grouped {
  margin-left: 24px;
}

.confirm-reason {
  margin: 4px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.confirm-select {
  flex-shrink: 0;
  width: 180px;
}

.confirm-empty {
  margin: 16px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

@media (max-width: 1080px) {
  .confirm-item {
    align-items: stretch;
    flex-direction: column;
  }

  .confirm-select {
    width: 100%;
  }
}
</style>
