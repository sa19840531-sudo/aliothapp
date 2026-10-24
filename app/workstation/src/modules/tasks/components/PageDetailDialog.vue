<script setup lang="ts">
import { computed } from 'vue'
import { NModal } from 'naive-ui'
import type { RuleResultItem } from '@/types/task-insight'
import { severityClass } from '@/utils/status'
import { failureExplanation, groupNotApplicableItems, groupReviewItems, ruleBucket } from './pageStory'
import GreenRuleResultDetails from './GreenRuleResultDetails.vue'
import { isLinkedGreenResult } from './greenRuleResultDetails'

/**
 * 页面详情弹窗：这一页是怎么走的（大白话），检查了哪些规则，没通过的和要人工确认的各自为什么。
 * 开关由父组件单独持有，关闭时只收开关、内容留着（NModal 关闭动画要靠内容收尾，
 * 09-21 报告页「!」弹窗就是内容和开关一起清掉导致页面锁死滚动）。
 */
const props = defineProps<{
  show: boolean
  title: string
  subtitle?: string | null
  /** 经过那一段的小标题，默认「这一页是怎么走的」。 */
  storyTitle?: string
  story: string[]
  rules: RuleResultItem[]
  explorationOnly: boolean
  screenshotIds: (item: RuleResultItem) => string[]
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'view-evidence': [item: RuleResultItem, artifactId: string]
  confirm: [item: RuleResultItem]
}>()

const greenItems = computed(() => props.rules.filter((item) => item.green_result))
const ordinaryItems = computed(() => props.rules.filter((item) => !item.green_result))
const failedItems = computed(() => ordinaryItems.value.filter((item) => ruleBucket(item) === 'failed'))
const reviewItems = computed(() => ordinaryItems.value.filter((item) => ruleBucket(item) === 'review'))
const reviewGroups = computed(() => groupReviewItems(reviewItems.value))
const passedItems = computed(() => ordinaryItems.value.filter((item) => ruleBucket(item) === 'passed'))
const notApplicableItems = computed(() =>
  ordinaryItems.value.filter((item) => ruleBucket(item) === 'not_applicable'),
)
const notApplicableGroups = computed(() => groupNotApplicableItems(notApplicableItems.value))
const pendingItems = computed(() => ordinaryItems.value.filter((item) => ruleBucket(item) === 'pending'))

function rowKey(item: RuleResultItem) {
  return item.execution_id || `${item.rule_id}:${item.page_capture_id || 'none'}`
}

function firstScreenshot(item: RuleResultItem): string | null {
  return props.screenshotIds(item)[0] ?? null
}

function names(items: RuleResultItem[]) {
  return items.map((item) => item.rule_name)
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    :title="title"
    class="page-detail-dialog"
    :style="{ width: 'min(760px, 94vw)' }"
    :bordered="false"
    @update:show="emit('update:show', $event)"
  >
    <div class="page-detail-body" data-testid="page-detail-dialog">
      <p v-if="subtitle" class="dialog-subtitle">{{ subtitle }}</p>

      <section class="dialog-section">
        <h4>{{ storyTitle || '这一页是怎么走的' }}</h4>
        <ol class="story-list" data-testid="page-story">
          <li v-for="(line, index) in story" :key="index">{{ line }}</li>
        </ol>
        <template v-for="item in rules" :key="`coverage:${rowKey(item)}`">
          <p v-if="item.inventory_coverage_reason" class="review-reason" data-testid="inventory-coverage-reason">
            {{ item.rule_name }}：{{ item.inventory_coverage_reason }}
          </p>
        </template>
      </section>

      <template v-if="!explorationOnly">
        <section v-if="greenItems.length" class="dialog-section" data-testid="green-rule-results">
          <h4>规则分项结果</h4>
          <p class="muted">分项结论、尚缺材料和人工复核分别保留，局部分项通过不代表整条规则通过。</p>
          <article v-for="item in greenItems" :key="rowKey(item)" class="rule-card green-rule-card">
            <header><strong>{{ item.rule_name }}</strong></header>
            <GreenRuleResultDetails :result="item.green_result"
              :allowed-screenshot-ids="screenshotIds(item)"
              @view-evidence="emit('view-evidence', item, $event)" />
            <button v-if="isLinkedGreenResult(item.green_result) && item.confirmable
                && item.execution_id && item.item_type === 'execution'"
              type="button" class="dialog-link" @click="emit('confirm', item)">
              去报告页人工确认
            </button>
          </article>
        </section>
        <section v-if="failedItems.length" class="dialog-section" data-testid="page-failed-rules">
          <h4>没通过的规则（{{ failedItems.length }} 条）</h4>
          <article v-for="item in failedItems" :key="rowKey(item)" class="rule-card is-failed">
            <header>
              <span v-if="item.severity_display" class="severity-chip" :class="severityClass(item.severity || item.severity_display)">
                {{ item.severity_display }}
              </span>
              <strong>{{ item.rule_name }}</strong>
              <em v-if="item.human_verdict_label">已人工确认：{{ item.human_verdict_label }}</em>
            </header>
            <p>{{ failureExplanation(item).problem }}</p>
            <p v-if="failureExplanation(item).observed" class="muted">
              机器看到的：{{ failureExplanation(item).observed }}
            </p>
            <button
              v-if="firstScreenshot(item)"
              type="button"
              class="dialog-link"
              @click="emit('view-evidence', item, firstScreenshot(item)!)"
            >
              看截图
            </button>
          </article>
        </section>

        <section v-if="reviewGroups.length" class="dialog-section" data-testid="page-review-rules">
          <h4>未完成判定的规则（{{ reviewItems.length }} 条）</h4>
          <article v-for="group in reviewGroups" :key="group.reason" class="rule-card is-review">
            <p class="review-reason">
              <b v-if="group.items.length > 1">这 {{ group.items.length }} 条是同一个原因：</b>{{ group.reason }}
            </p>
            <div class="rule-names">
              <button
                v-for="item in group.items"
                :key="rowKey(item)"
                type="button"
                class="rule-chip"
                :disabled="!item.confirmable || !item.execution_id || item.item_type !== 'execution'"
                :title="item.confirmable && item.execution_id && item.item_type === 'execution' ? '去报告页人工确认这条规则' : '未执行或缺少证据，不能通过人工确认补成业务结论'"
                @click="emit('confirm', item)"
              >
                {{ item.rule_name }}
              </button>
            </div>
          </article>
        </section>

        <details v-if="passedItems.length" class="dialog-section compact">
          <summary>通过的规则（{{ passedItems.length }} 条）</summary>
          <p class="rule-name-line">{{ names(passedItems).join('、') }}</p>
        </details>
        <details v-if="notApplicableItems.length" class="dialog-section compact" data-testid="page-na-rules">
          <summary>不适用的规则（{{ notApplicableItems.length }} 条）</summary>
          <div v-for="group in notApplicableGroups" :key="group.reason" class="na-group">
            <p class="na-reason">
              <b>{{ group.items.length > 1 ? `这 ${group.items.length} 条不适用的原因相同：` : '不适用原因：' }}</b>{{ group.reason }}
            </p>
            <p class="rule-name-line">{{ names(group.items).join('、') }}</p>
          </div>
        </details>
        <details v-if="pendingItems.length" class="dialog-section compact">
          <summary>还在检查的规则（{{ pendingItems.length }} 条）</summary>
          <p class="rule-name-line">{{ names(pendingItems).join('、') }}</p>
        </details>
      </template>
    </div>
  </n-modal>
</template>

<style scoped>
.page-detail-body {
  display: grid;
  gap: 16px;
  max-height: 70vh;
  overflow-y: auto;
  padding-right: 4px;
}

.dialog-subtitle {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  word-break: break-all;
}

.dialog-section h4 {
  margin: 0 0 8px;
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.story-list {
  margin: 0;
  padding-left: 20px;
  display: grid;
  gap: 6px;
  color: var(--workstation-text-strong);
  font-size: 13px;
  line-height: 1.7;
}

.rule-card {
  display: grid;
  gap: 4px;
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
  font-size: 13px;
  line-height: 1.7;
}

.rule-card.is-failed {
  border-left: 3px solid var(--workstation-danger);
}

.rule-card.is-review {
  border-left: 3px solid var(--workstation-orange);
}

.rule-card header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rule-card header em {
  margin-left: auto;
  color: var(--workstation-text-muted);
  font-size: 12px;
  font-style: normal;
}

.rule-card p {
  margin: 0;
}

.muted {
  color: var(--workstation-text-muted);
}

.review-reason b {
  color: var(--workstation-text-strong);
}

.rule-names {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rule-chip {
  padding: 2px 8px;
  border: 1px solid var(--workstation-border);
  border-radius: 999px;
  background: transparent;
  color: var(--workstation-primary);
  font-size: 12px;
  cursor: pointer;
}

.rule-chip:disabled {
  color: var(--workstation-text-muted);
  cursor: default;
}

.dialog-link {
  justify-self: start;
  padding: 0;
  border: 0;
  background: none;
  color: var(--workstation-primary);
  font-size: 12px;
  cursor: pointer;
}

.dialog-section.compact summary {
  cursor: pointer;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.rule-name-line {
  margin: 6px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.8;
}

.na-group {
  margin-top: 8px;
  padding: 8px 12px;
  border-left: 3px solid var(--workstation-border);
  background: var(--workstation-panel-soft);
  border-radius: var(--workstation-radius-sm);
}

.na-group .rule-name-line {
  margin-top: 2px;
}

.na-reason {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
}

.na-reason b {
  color: var(--workstation-text-strong);
}
</style>
