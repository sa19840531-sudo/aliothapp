<script setup lang="ts">
import { computed } from 'vue'
import type { GreenRuleResultProjection, GreenRuleSubitemResult } from '@/types/task-insight'
import {
  greenConfidence, greenMissingLabel, greenReadableText, greenResultLinked, greenResultStatus,
  greenResultTitle, greenReviewReason, greenReviewStatus, greenSubitemLabel, greenSubitemScreenshots, greenSubitemStatus,
} from './greenRuleResultDetails'

const props = defineProps<{
  result?: GreenRuleResultProjection | null
  allowedScreenshotIds?: string[]
}>()
const emit = defineEmits<{ 'view-evidence': [artifactId: string] }>()
const linked = computed(() => greenResultLinked(props.result))
const status = computed(() => props.result ? greenResultStatus(props.result) : null)

function screenshots(item: GreenRuleSubitemResult) {
  return props.result ? greenSubitemScreenshots(props.result, item, props.allowedScreenshotIds) : []
}

function viewEvidence(item: GreenRuleSubitemResult, artifactId: string) {
  // 点击时重读当前属性，旧卡片或旧截图集合不能在切换结果后继续开图。
  if (screenshots(item).includes(artifactId)) emit('view-evidence', artifactId)
}
</script>

<template>
  <section v-if="result" class="green-rule-details" data-testid="green-rule-details" aria-label="绿色规则分项判定">
    <header class="result-header">
      <strong>{{ greenResultTitle(result) }}</strong>
      <span class="result-badge" :class="status?.className" data-testid="green-rule-status">{{ status?.label }}</span>
    </header>
    <p v-if="!linked" class="result-notice" role="status" data-testid="green-linkage-notice">
      {{ greenReadableText(result.linkage_notice, '判定与当前规则的关联尚未确认，暂不显示结论和原图。') }}
    </p>
    <template v-else>
      <p class="result-explanation">分项按已有判定分别展示；尚未判定表示材料、条件或判读能力尚不完整，不代表产品不符合。</p>
      <p v-if="result.review_status || result.review_reason" class="result-notice" data-testid="green-review-status">
        人工复核：{{ greenReviewStatus(result) }}<template v-if="result.review_reason"> · {{ greenReviewReason(result) }}</template>
      </p>
      <div v-if="result.missing_evidence.length" class="missing-block" data-testid="green-overall-gaps">
        <strong>仍需补齐的材料与判读</strong>
        <ul><li v-for="(code, index) in result.missing_evidence" :key="index">{{ greenMissingLabel(code) }}</li></ul>
      </div>
      <ol v-if="result.subitems.length" class="subitems">
        <li v-for="(item, index) in result.subitems" :key="`${item.key}:${index}`" class="subitem" data-testid="green-subitem">
          <header class="result-header">
            <strong>{{ greenSubitemLabel(item.key) }}</strong>
            <span class="result-badge" :class="greenSubitemStatus(item, result).className" data-testid="green-subitem-status">{{ greenSubitemStatus(item, result).label }}</span>
            <span v-if="greenConfidence(item)" class="confidence">置信度 {{ greenConfidence(item) }}</span>
          </header>
          <p v-if="item.reason">{{ greenReadableText(item.reason, '分项原因待核对') }}</p>
          <ul v-if="item.missing_evidence.length" class="missing-list" aria-label="分项材料与判读缺口">
            <li v-for="(code, gapIndex) in item.missing_evidence" :key="gapIndex">{{ greenMissingLabel(code) }}</li>
          </ul>
          <div v-if="screenshots(item).length" class="evidence-actions">
            <button v-for="(artifactId, imageIndex) in screenshots(item)" :key="artifactId" type="button"
              class="dialog-link" data-testid="green-view-evidence" @click="viewEvidence(item, artifactId)">
              查看{{ screenshots(item).length > 1 ? `关联原图 ${imageIndex + 1}` : '关联原图' }}
            </button>
          </div>
          <p v-else class="no-image" data-testid="green-no-image">{{ greenReadableText(item.evidence_notice, '当前分项暂无可查看的关联原图。') }}</p>
        </li>
      </ol>
      <p v-else class="result-explanation">后端尚未提供已关联的分项判定。</p>
    </template>
  </section>
</template>

<style scoped>
.green-rule-details { min-width: 0; margin-top: 10px; padding: 12px; border: 1px solid var(--workstation-border); border-radius: var(--workstation-radius-sm, 8px); background: var(--workstation-panel-soft); font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.result-header { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px; min-width: 0; }
.result-header strong { min-width: 0; color: var(--workstation-text-strong); }
.result-badge { max-width: 100%; padding: 1px 8px; border-radius: 999px; background: var(--workstation-panel); font-size: 12px; white-space: normal; }
.is-pass { color: var(--workstation-success, #177245); }
.is-fail { color: var(--workstation-danger, #b73333); }
.is-review, .result-notice, .missing-block, .missing-list { color: var(--workstation-orange, #9b5a13); }
.is-neutral, .result-explanation, .confidence, .no-image { color: var(--workstation-text-muted); }
p { margin: 6px 0; }
ul { margin: 4px 0; padding-left: 20px; }
.subitems { margin: 10px 0 0; padding-left: 22px; }
.subitem { margin: 10px 0; min-width: 0; }
.evidence-actions { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 6px; }
.dialog-link { max-width: 100%; padding: 0; border: 0; background: none; color: var(--workstation-primary); font: inherit; font-size: 12px; white-space: normal; cursor: pointer; }
.confidence, .no-image { font-size: 12px; }
@media (max-width: 600px) { .green-rule-details { padding: 10px; } .subitems { padding-left: 18px; } }
</style>
