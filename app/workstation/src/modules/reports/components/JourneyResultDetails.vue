<script setup lang="ts">
import type { JourneyRuleResult } from '@/types/report-insight'

const props = defineProps<{ result: JourneyRuleResult; availableShotIds: string[] }>()
const emit = defineEmits<{ 'open-shots': [artifactIds: string[], index: number]; 'open-material': [artifactId: string] }>()
const labels: Record<string, string> = { pass: '满足', fail: '未满足', evidence_insufficient: '证据不足', needs_review: '待复核' }
function shotIds(checkpoint: JourneyRuleResult['checkpoints'][number]) {
  if (checkpoint.integrity !== 'bound') return []
  return checkpoint.bindings.flatMap((item) => item.artifact_ids).filter((id) => props.availableShotIds.includes(id))
}
</script>

<template>
  <section class="journey-results" data-testid="journey-result-details">
    <h4>冻结流程分项判定</h4>
    <p>场景 {{ result.scenario_key }} · 流程 {{ result.execution_plan_id }}</p>
    <p>输入集 {{ result.input_set_id }}；材料是判定时的原绑定，不表示最新质量或整体通过。</p>
    <article v-for="checkpoint in result.checkpoints" :key="checkpoint.checkpoint_id">
      <h5>{{ checkpoint.checkpoint_id }} · {{ labels[checkpoint.status] || '结果未知' }}</h5>
      <ul><li v-for="(passed, measurement) in checkpoint.checks" :key="measurement">{{ measurement }}：{{ passed ? '满足' : '未满足' }}</li></ul>
      <p v-if="checkpoint.missing?.length">缺口：{{ checkpoint.missing.join('、') }}</p>
      <p v-if="checkpoint.integrity !== 'bound'">没有完整原始绑定，不以其他截图补齐。</p>
      <div v-for="binding in checkpoint.bindings" :key="binding.role">
        <p>{{ binding.role }} · Capture {{ binding.capture_id }} · 动作 {{ binding.action_execution_id }}</p>
        <p>Manifest {{ binding.manifest_id }} · v{{ binding.manifest_version }}</p>
        <p>原材料：{{ binding.artifact_ids.join('、') }}</p>
        <button v-for="id in binding.artifact_ids.filter((item) => shotIds(checkpoint).includes(item))" :key="id"
          type="button" class="qc-link" :data-testid="`checkpoint-shot-${id}`"
          @click="emit('open-shots', shotIds(checkpoint), shotIds(checkpoint).indexOf(id))">查看原截图 {{ id }}</button>
        <button v-for="id in binding.artifact_ids.filter((item) => !shotIds(checkpoint).includes(item))" v-show="checkpoint.integrity === 'bound'" :key="id"
          type="button" class="qc-link" :data-testid="`checkpoint-material-${id}`"
          @click="emit('open-material', id)">下载原材料 {{ id }}</button>
      </div>
      <p v-if="checkpoint.bindings.length && !shotIds(checkpoint).length">当前截图目录缺少这些原附件，不能按最新截图替代；其他材料可按标识核对。</p>
    </article>
  </section>
</template>

<style scoped>
.journey-results { width: 100%; overflow-wrap: anywhere; }
article { border-top: 1px solid var(--qc-border); margin-top: 12px; padding-top: 8px; }
button { margin-right: 12px; }
</style>
