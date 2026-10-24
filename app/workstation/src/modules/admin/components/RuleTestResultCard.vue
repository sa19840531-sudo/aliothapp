<script setup lang="ts">
import { computed } from 'vue'
import type { RuleTestRunResult } from '../api/ruleTests'
import { ruleTestOutcomeLabel, ruleTestOutcomeTone } from './ruleTestOutcome'

/** 一次规则测试的结论：结论 → 原因 → 具体发现 → 缺的证据 → 和正式任务的差别 → 技术细节。 */
const props = defineProps<{
  result: RuleTestRunResult
  ranAt: string
  /** 点得开的证据名（父组件手里有图才给）；不给就还是纯文字 */
  previewableEvidence?: readonly string[]
}>()
const emit = defineEmits<{
  previewEvidence: [name: string]
}>()

const previewable = computed(() => new Set(props.previewableEvidence ?? []))

const REASON_TITLES: Record<string, string> = {
  pass: '说明',
  fail: '不通过原因',
  warning: '风险原因',
  manual_review: '为什么转人工',
  evidence_insufficient: '为什么判不了',
  skipped: '为什么不适用',
  error: '出错原因',
  not_executable: '原因',
}

const tone = computed(() => ruleTestOutcomeTone(props.result.outcome))
const outcomeLabel = computed(() => ruleTestOutcomeLabel(props.result.outcome))
const reasonTitle = computed(() => REASON_TITLES[props.result.outcome] ?? '说明')
const modeLabel = computed(() =>
  props.result.check_mode === 'strict' ? '严格按正式证据要求' : '按已给证据判定',
)
const seconds = computed(() => (props.result.duration_ms / 1000).toFixed(1))
const confidenceText = computed(() => {
  const { confidence, confidence_threshold: threshold } = props.result
  if (confidence === null || confidence === undefined) return ''
  const text = `${Math.round(confidence * 100)}%`
  return threshold === null || threshold === undefined
    ? text
    : `${text}（规则要求至少 ${Math.round(threshold * 100)}%）`
})
const technicalJson = computed(() =>
  JSON.stringify(
    { judge_output: props.result.judge_output, judge_input: props.result.judge_input },
    null,
    2,
  ),
)
</script>

<template>
  <article class="rt-result" data-testid="rule-test-result">
    <header class="rt-result-head">
      <span class="rt-outcome" :class="`tone-${tone}`" data-testid="rule-test-outcome">
        {{ outcomeLabel }}
      </span>
      <div class="rt-result-title">
        <strong>{{ result.rule_code }} · {{ result.rule_name }}</strong>
        <small>{{ modeLabel }} · 用时 {{ seconds }} 秒 · {{ ranAt }}</small>
      </div>
    </header>

    <p class="rt-summary">{{ result.result_summary }}</p>

    <section v-if="result.problem_desc" class="rt-block">
      <h4>{{ reasonTitle }}</h4>
      <p>{{ result.problem_desc }}</p>
    </section>

    <section v-if="result.findings.length" class="rt-block">
      <h4>具体发现</h4>
      <ul class="rt-findings">
        <li v-for="(finding, index) in result.findings" :key="index">
          <p>{{ finding.summary }}</p>
          <div v-if="finding.evidence.length" class="rt-chips">
            <button
              v-for="name in finding.evidence"
              :key="name"
              type="button"
              class="qc-chip tone-primary"
              :class="{ 'rt-chip-openable': previewable.has(name) }"
              :disabled="!previewable.has(name)"
              :title="previewable.has(name) ? '点击看这张截图' : ''"
              @click="emit('previewEvidence', name)"
            >
              {{ name }}
            </button>
          </div>
        </li>
      </ul>
    </section>

    <section v-if="result.missing_evidence.length" class="rt-block">
      <h4>缺少的证据</h4>
      <div class="rt-chips">
        <span v-for="item in result.missing_evidence" :key="item" class="qc-chip tone-orange">
          {{ item }}
        </span>
      </div>
    </section>

    <section v-if="confidenceText" class="rt-block rt-inline">
      <h4>AI 判断把握度</h4>
      <span>{{ confidenceText }}</span>
    </section>

    <section v-if="result.used_evidence.length" class="rt-block">
      <h4>本次用到的证据</h4>
      <div class="rt-chips">
        <button
          v-for="name in result.used_evidence"
          :key="name"
          type="button"
          class="qc-chip tone-muted"
          :class="{ 'rt-chip-openable': previewable.has(name) }"
          :disabled="!previewable.has(name)"
          :title="previewable.has(name) ? '点击看这张截图' : ''"
          @click="emit('previewEvidence', name)"
        >
          {{ name }}
        </button>
      </div>
    </section>

    <section v-if="result.notices.length" class="rt-notices" data-testid="rule-test-notices">
      <h4>注意</h4>
      <ul>
        <li v-for="notice in result.notices" :key="notice">{{ notice }}</li>
      </ul>
    </section>

    <details class="rt-tech">
      <summary>技术细节（排查规则配置时看）</summary>
      <pre>{{ technicalJson }}</pre>
    </details>
  </article>
</template>

<style scoped>
.rt-result {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.rt-result-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.rt-outcome {
  flex-shrink: 0;
  padding: 6px 14px;
  border-radius: 999px;
  font-size: 15px;
  font-weight: 600;
  background: var(--tint-neutral);
  color: var(--workstation-text-muted);
}

.rt-outcome.tone-success {
  background: var(--tint-success);
  color: var(--workstation-success-strong);
}

.rt-outcome.tone-danger {
  background: var(--tint-danger);
  color: var(--workstation-danger);
}

.rt-outcome.tone-warning {
  background: var(--tint-warning);
  color: var(--workstation-warning-strong);
}

.rt-outcome.tone-orange {
  background: var(--tint-orange);
  color: var(--workstation-orange-strong);
}

.rt-result-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.rt-result-title strong {
  color: var(--workstation-text-strong);
  font-size: 14px;
  overflow-wrap: anywhere;
}

.rt-result-title small {
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.rt-summary {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 14px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.rt-block h4,
.rt-notices h4 {
  margin: 0 0 6px;
  color: var(--workstation-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.rt-block p {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.rt-inline {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.rt-inline h4 {
  margin: 0;
}

.rt-inline span {
  color: var(--workstation-text-strong);
  font-size: 13px;
}

.rt-findings {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rt-findings li {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
}

.rt-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* 证据引用平时是标签；父组件手里有这张图时才变成能点的 */
.rt-chips button.qc-chip {
  font: inherit;
  cursor: default;
}

.rt-chips button.qc-chip:disabled {
  opacity: 1;
}

.rt-chips button.qc-chip.rt-chip-openable {
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.rt-chips button.qc-chip.rt-chip-openable:hover,
.rt-chips button.qc-chip.rt-chip-openable:focus-visible {
  filter: brightness(0.94);
}

.rt-notices {
  padding: 10px 14px;
  border: 1px solid rgba(217, 119, 6, 0.35);
  border-radius: var(--workstation-radius-sm);
  background: var(--tint-warning);
}

.rt-notices ul {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding-left: 18px;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.6;
}

.rt-tech summary {
  color: var(--workstation-text-muted);
  font-size: 12px;
  cursor: pointer;
}

.rt-tech pre {
  max-height: 360px;
  margin: 8px 0 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
  color: var(--workstation-text);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
