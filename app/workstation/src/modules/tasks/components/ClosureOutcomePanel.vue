<script setup lang="ts">
/**
 * 任务详情「收口结论」面板：页面探索、证据充分、规则结果、最终完成检查分别给出结果。
 *
 * 只展示后端算好的四维结论，不在前端推断任务是否结束；没有结论时明说「还没有结论」，
 * 不用空白冒充达标。完成检查被挡住时逐条列出原因和该怎么办。
 */
import { computed, ref } from 'vue'
import type { ClosureOutcome } from '@/types/task-insight'

const props = withDefaults(
  defineProps<{
    outcome: ClosureOutcome | null
    defaultExpanded?: boolean
  }>(),
  { defaultExpanded: true },
)

const expanded = ref(props.defaultExpanded)

const STATE_TONES: Record<string, string> = {
  satisfied: 'tone-good',
  incomplete: 'tone-warn',
  blocked: 'tone-bad',
  unknown: 'tone-muted',
}

const dimensions = computed(() => props.outcome?.dimensions ?? [])
const blockedCount = computed(
  () => dimensions.value.filter((item) => item.state === 'blocked').length,
)

function toneOf(state: string): string {
  return STATE_TONES[state] ?? 'tone-muted'
}

/** 维度里值得摆出来的数字，没有就不摆，避免一排 0。 */
function metricChips(key: string, metrics: Record<string, unknown>): string[] {
  const chips: string[] = []
  const num = (name: string): number => {
    const value = metrics[name]
    return typeof value === 'number' ? value : 0
  }
  if (key === 'exploration') {
    if (num('visited_page_type_count')) chips.push(`走到 ${num('visited_page_type_count')} 类页面`)
    if (num('pending_action_count')) chips.push(`动作未完成 ${num('pending_action_count')}`)
    if (num('pending_capture_commit_count')) {
      chips.push(`截图未存 ${num('pending_capture_commit_count')}`)
    }
    if (num('background_execution_count')) chips.push(`后台任务 ${num('background_execution_count')}`)
  }
  if (key === 'evidence') {
    if (num('evidence_gap_count')) chips.push(`证据缺口 ${num('evidence_gap_count')}`)
    if (num('presentation_violation_count')) {
      chips.push(`材料不可用 ${num('presentation_violation_count')}`)
    }
    if (num('open_supplement_request_count')) {
      chips.push(`补采在途 ${num('open_supplement_request_count')}`)
    }
    if (num('settled_supplement_request_count')) {
      chips.push(`补采已结 ${num('settled_supplement_request_count')}`)
    }
  }
  if (key === 'rules') {
    const counts = metrics.group_counts
    const labels = metrics.group_labels
    if (counts && typeof counts === 'object' && labels && typeof labels === 'object') {
      for (const [code, count] of Object.entries(counts as Record<string, number>)) {
        if (!count) continue
        const label = (labels as Record<string, string>)[code] ?? code
        chips.push(`${label} ${count}`)
      }
    }
  }
  if (key === 'completion_gate') {
    if (num('nonterminal_work_item_count')) {
      chips.push(`规则检查未结算 ${num('nonterminal_work_item_count')}`)
    }
    if (num('open_review_count')) chips.push(`待人工确认 ${num('open_review_count')}`)
  }
  return chips
}
</script>

<template>
  <section class="closure-outcome-panel" data-testid="closure-outcome-panel">
    <button type="button" class="closure-head" @click="expanded = !expanded">
      <span class="closure-title">收口结论</span>
      <span v-if="outcome" class="closure-summary">{{ outcome.summary }}</span>
      <span v-else class="closure-summary tone-muted">收口结论尚未加载</span>
      <span v-if="blockedCount" class="tone-bad">被挡住 {{ blockedCount }}</span>
      <span class="closure-toggle">{{ expanded ? '收起' : '展开' }}</span>
    </button>

    <div v-if="expanded && outcome" class="closure-body">
      <article
        v-for="dimension in dimensions"
        :key="dimension.key"
        class="closure-card"
        :data-testid="`closure-${dimension.key}`"
      >
        <header>
          <strong>{{ dimension.title }}</strong>
          <span :class="toneOf(dimension.state)">{{ dimension.state_label }}</span>
        </header>
        <p class="closure-text">{{ dimension.summary }}</p>
        <div v-if="metricChips(dimension.key, dimension.metrics).length" class="chip-row">
          <span v-for="chip in metricChips(dimension.key, dimension.metrics)" :key="chip">
            {{ chip }}
          </span>
        </div>
        <ul v-if="dimension.blockers.length" class="closure-blockers">
          <li v-for="blocker in dimension.blockers" :key="blocker.code">
            <span :class="blocker.waiting_for_people ? 'tone-warn' : 'tone-bad'">
              {{ blocker.label }}
            </span>
            <span class="closure-advice">{{ blocker.advice }}</span>
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>

<style scoped>
.closure-outcome-panel {
  margin: 0 20px 16px;
  padding: 14px 16px;
  border: 1px solid rgba(148, 163, 184, 0.24);
  border-radius: 12px;
  background: rgba(148, 163, 184, 0.06);
}

.closure-head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.closure-title {
  font-weight: 600;
  font-size: 15px;
}

.closure-summary {
  flex: 1;
  min-width: 12ch;
  font-size: 13px;
  opacity: 0.85;
}

.closure-toggle {
  font-size: 12px;
  opacity: 0.7;
}

.closure-body {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.closure-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.04);
}

.closure-card header {
  display: flex;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
}

.closure-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
}

.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  opacity: 0.8;
}

.closure-blockers {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
}

.closure-blockers li {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.closure-advice {
  opacity: 0.75;
}

.tone-good {
  color: #16a34a;
}

.tone-warn {
  color: #d97706;
}

.tone-bad {
  color: #dc2626;
}

.tone-muted {
  opacity: 0.6;
}
</style>
