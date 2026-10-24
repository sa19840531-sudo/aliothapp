<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { listEvidenceRedactions, type RedactionArtifact, type RedactionPlanSummary } from '@/services/evidence-redactions.api'
import EvidenceRedactionEditor from './EvidenceRedactionEditor.vue'
import EvidenceRedactionReview from './EvidenceRedactionReview.vue'
import { matchesSource } from './redactionContent'

const props = defineProps<{ artifacts: RedactionArtifact[]; exporting?: boolean; exportError?: string; blockedReason?: string }>()
const emit = defineEmits<{ export: [redactionIds: Record<string, string>] }>()
const activeId = ref('')
const listed = ref<Record<string, { items: RedactionPlanSummary[]; can_manage: boolean }>>({})
const chosen = ref<Record<string, string>>({})
const reviewId = ref('')
const readyId = ref('')
const editing = ref(false)
const error = ref('')
const loading = ref(false)
let generation = 0
let controller: AbortController | undefined
const active = computed(() => props.artifacts.find((item) => item.artifact_id === activeId.value))
const plans = computed(() => (listed.value[activeId.value]?.items ?? []).filter((plan) => active.value && matchesSource(plan, active.value)))
const reviewed = computed(() => plans.value.find((plan) => plan.redaction_id === reviewId.value))
const complete = computed(() => props.artifacts.length > 0 && !props.blockedReason && props.artifacts.every((item) => chosen.value[item.artifact_id]))

watch(() => props.artifacts, () => { void load() }, { immediate: true })
watch(activeId, () => { editing.value = false; reviewId.value = ''; readyId.value = '' })
watch(reviewId, () => { readyId.value = '' })
onBeforeUnmount(() => { generation++; controller?.abort() })

async function load() {
  const current = ++generation
  controller?.abort(); controller = new AbortController()
  listed.value = {}; chosen.value = {}; error.value = ''; editing.value = false; reviewId.value = ''; readyId.value = ''
  activeId.value = props.artifacts[0]?.artifact_id ?? ''; loading.value = true
  try {
    const result = await Promise.all(props.artifacts.map(async (artifact) => [artifact.artifact_id, await listEvidenceRedactions(artifact.artifact_id, controller?.signal)] as const))
    if (current === generation) listed.value = Object.fromEntries(result)
  } catch (cause) { if (current === generation) error.value = cause instanceof Error ? cause.message : '脱敏版本加载失败' }
  finally { if (current === generation) loading.value = false }
}

function saved(plan: RedactionPlanSummary) {
  if (!active.value || !matchesSource(plan, active.value)) return
  const previous = listed.value[activeId.value]
  if (!previous?.can_manage) return
  listed.value = { ...listed.value, [activeId.value]: { ...previous, items: [...previous.items, plan] } }
  editing.value = false; reviewId.value = plan.redaction_id
}

function choose() {
  if (!reviewed.value || readyId.value !== reviewed.value.redaction_id || !active.value) return
  chosen.value = { ...chosen.value, [activeId.value]: reviewed.value.redaction_id }
}

function exportPackage() {
  if (!complete.value || props.exporting || loading.value || error.value) return
  emit('export', { ...chosen.value })
}
</script>

<template>
  <section class="redaction-panel" data-testid="redaction-panel">
    <h4>局部脱敏与业务复核</h4>
    <p>逐份复核并选择不可变脱敏版本，全部 {{ artifacts.length }} 份材料就绪后导出；无需遮盖的材料也须人工确认。原件及当前判定输入保持不变。</p>
    <p v-if="blockedReason" role="alert">{{ blockedReason }}</p>
    <p v-if="loading">读取脱敏版本中…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="exportError" role="alert">{{ exportError }}</p>
    <label>当前材料 <select v-model="activeId" :disabled="loading || exporting" data-testid="redaction-artifact">
      <option v-for="artifact in artifacts" :key="artifact.artifact_id" :value="artifact.artifact_id">{{ chosen[artifact.artifact_id] ? '已选' : '待选' }} · {{ artifact.artifact_type }} · {{ artifact.artifact_id }}</option>
    </select></label>
    <template v-if="active && !loading && !error">
      <details><summary>当前材料来源</summary><p>Manifest {{ active.manifest_id || '无' }} · v{{ active.manifest_version ?? '无' }} · 原件摘要 {{ active.checksum }}</p></details>
      <button v-if="listed[activeId]?.can_manage" class="qc-btn qc-btn--secondary" type="button" data-testid="redaction-edit" @click="editing = !editing">{{ editing ? '收起编辑' : '创建局部脱敏版本' }}</button>
      <p v-else>当前身份仅能复核已生成的脱敏件，不读取原件。</p>
      <EvidenceRedactionEditor v-if="editing && listed[activeId]?.can_manage" :key="activeId" :artifact="active" :can-manage="true" @saved="saved" @close="editing = false" />
      <label>已有脱敏版本 <select v-model="reviewId" data-testid="redaction-version">
        <option value="">请选择并查看</option>
        <option v-for="plan in plans" :key="plan.redaction_id" :value="plan.redaction_id">{{ plan.created_at }} · {{ plan.redaction_id }}</option>
      </select></label>
      <p v-if="!plans.length">尚无与当前原件和 Manifest 一致的脱敏版本。</p>
      <EvidenceRedactionReview v-if="reviewed" :key="reviewed.redaction_id" :plan="reviewed" @ready="readyId = $event" @failed="readyId = ''" />
      <button class="qc-btn qc-btn--secondary" type="button" data-testid="redaction-choose" :disabled="!reviewed || readyId !== reviewed.redaction_id" @click="choose">已复核，选入本次导出</button>
      <p v-if="chosen[activeId]">本次已选版本 {{ chosen[activeId] }}</p>
    </template>
    <p>已选 {{ Object.keys(chosen).length }} / {{ artifacts.length }} 份材料。</p>
    <button class="qc-btn qc-btn--primary" type="button" data-testid="redaction-export" :disabled="!complete || exporting || loading || !!error" @click="exportPackage">{{ exporting ? '导出中…' : '导出可复核脱敏包' }}</button>
  </section>
</template>

<style scoped>
.redaction-panel { margin: 14px 0; padding: 14px; border: 1px solid var(--workstation-border); border-radius: 8px; overflow-wrap: anywhere; }
label { display: block; margin: 10px 0; }
select { max-width: 100%; }
button { margin: 6px 8px 6px 0; max-width: 100%; white-space: normal; }
</style>
