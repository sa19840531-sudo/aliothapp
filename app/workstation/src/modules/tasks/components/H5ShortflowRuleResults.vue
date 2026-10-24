<script setup lang="ts">
import type { H5ShortflowEvidenceRef, H5ShortflowPlan } from '@/services/h5-shortflows.api'
import { REVIEW_STATUS_LABELS, RULE_EXECUTION_STATUS_LABELS } from '@/utils/status'
import {
  checkpointCheckLabel, checkpointMissingLabel, referencedScreenshots, shortflowRuleStatus, shortflowRuleStatusClass,
} from './h5ShortflowRuleResults'

defineProps<{ plan: H5ShortflowPlan }>()
const emit = defineEmits<{
  'locate-checkpoint': [checkpointId: string]
  'view-evidence': [reference: H5ShortflowEvidenceRef, artifactId: string]
}>()
</script>

<template>
  <section class="shortflow-rule-results" data-testid="shortflow-rule-results" aria-label="流程规则判定结果">
    <h4>规则判定结果</h4>
    <p class="qc-muted">以下展示已关联的后端判定；观测完成和材料质量合格不代表规则通过。</p>
    <p v-if="plan.rule_results_notice" class="result-notice" role="status">{{ plan.rule_results_notice }}</p>
    <p v-else-if="!plan.rule_results?.length" class="result-notice" role="status">尚无已关联的规则判定结果，请等待判定或核对规则输入关联。</p>
    <article v-for="result in plan.rule_results ?? []" :key="`${result.rule_id}:${result.input_set_id || result.rule_contract_hash}`"
      class="shortflow-rule-card" data-testid="shortflow-rule-result">
      <header>
        <strong>{{ result.rule_name }}</strong>
        <span class="rule-result-badge" :class="shortflowRuleStatusClass(result)"
          data-testid="shortflow-rule-verdict">{{ shortflowRuleStatus(result) }}</span>
      </header>
      <p v-if="result.linkage_status !== 'linked'" class="result-notice" role="status">
        {{ result.linkage_notice || '判定与当前流程的关联尚未确认，暂不能作为当前结论或定位原图。' }}
      </p>
      <template v-else>
        <p v-if="result.result_summary">{{ result.result_summary }}</p>
        <p v-if="result.problem_desc && result.problem_desc !== result.result_summary">{{ result.problem_desc }}</p>
        <p v-if="result.review_status || result.review_reason" class="result-notice" data-testid="shortflow-rule-review">
          人工复核：{{ REVIEW_STATUS_LABELS[result.review_status || ''] || '状态待核对' }}<template v-if="result.review_reason"> · {{ result.review_reason }}</template>
        </p>
        <ol v-if="result.checkpoints.length" class="rule-checkpoints">
          <li v-for="checkpoint in result.checkpoints" :key="checkpoint.checkpoint_id" data-testid="shortflow-checkpoint-result">
            <header>
              <strong>检查点 {{ checkpoint.checkpoint_id }}</strong>
              <span>{{ RULE_EXECUTION_STATUS_LABELS[checkpoint.status] || '结论待核对' }}</span>
              <button v-if="plan.checkpoints.some((item) => item.checkpoint_id === checkpoint.checkpoint_id)"
                type="button" class="qc-btn qc-btn--secondary" @click="emit('locate-checkpoint', checkpoint.checkpoint_id)">定位检查点</button>
            </header>
            <p v-if="checkpoint.reason">{{ checkpoint.reason }}</p>
            <ul v-if="checkpoint.checks.length" class="checkpoint-checks">
              <li v-for="check in checkpoint.checks" :key="check.name">{{ checkpointCheckLabel(check.name) }}：{{ check.passed ? '符合' : '不符合' }}</li>
            </ul>
            <ul v-if="checkpoint.missing.length" class="checkpoint-missing" aria-label="证据缺口">
              <li v-for="missing in checkpoint.missing" :key="missing">{{ checkpointMissingLabel(missing) }}</li>
            </ul>
            <div v-for="reference in checkpoint.evidence_refs" :key="`${reference.attempt_id}:${reference.role}:${reference.capture_id}`" class="rule-evidence">
              <button v-for="artifact in referencedScreenshots(plan, reference)" :key="artifact.artifact_id" type="button"
                class="qc-btn qc-btn--secondary" data-testid="shortflow-rule-evidence"
                @click="emit('view-evidence', reference, artifact.artifact_id)">
                查看{{ reference.role === 'before_action' ? '操作前' : reference.role === 'after_action' ? '操作后' : '关联' }}原图
              </button>
            </div>
          </li>
        </ol>
        <p v-else class="qc-muted">后端尚未提供分项结论，不根据材料状态推算。</p>
      </template>
    </article>
  </section>
</template>

<style scoped>
.shortflow-rule-results { margin: 18px 0; overflow-wrap: anywhere; }
h4 { margin: 0 0 8px; }
.shortflow-rule-card { padding: 14px; margin-top: 12px; border: 1px solid var(--workstation-border); border-radius: var(--workstation-radius-sm, 8px); background: var(--workstation-panel-soft); }
header { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
p { margin: 8px 0; line-height: 1.6; }
.rule-result-badge { border-radius: 12px; padding: 2px 9px; font-size: 12px; background: var(--workstation-panel); }
.is-pass { color: var(--workstation-success, #177245); }
.is-fail, .is-error { color: var(--workstation-danger, #b73333); }
.is-evidence_insufficient, .is-manual_review, .is-unlinked, .result-notice, .checkpoint-missing { color: var(--workstation-orange, #9b5a13); }
.rule-checkpoints { padding-left: 20px; }
.rule-checkpoints > li { margin: 14px 0; }
.checkpoint-checks, .checkpoint-missing { padding-left: 20px; line-height: 1.7; }
.rule-evidence { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.qc-btn { max-width: 100%; white-space: normal; }
@media (max-width: 600px) { .shortflow-rule-card { padding: 10px; } .rule-checkpoints { padding-left: 16px; } }
</style>
