<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  downloadH5ShortflowArchive, downloadH5ShortflowRedactedArchive, getH5Shortflows,
  type EvidenceManifestReview, type H5ShortflowArtifact, type H5ShortflowBinding,
  type H5ShortflowEvidenceRef, type H5ShortflowPlan, type H5ShortflowSnapshot,
} from '@/services/h5-shortflows.api'
import { issueEvidenceFileUrl } from '@/services/task-insight.api'
import { useEvidenceFileUrls } from '@/composables/useEvidenceFileUrls'
import ScreenshotEvidenceViewer from './ScreenshotEvidenceViewer.vue'
import H5ShortflowRuleResults from './H5ShortflowRuleResults.vue'
import EvidenceManifestVersions from './EvidenceManifestVersions.vue'
import EvidenceTextViewer from './EvidenceTextViewer.vue'
import { checkpointElementId, checkpointScreenshotEntries, referencedScreenshots } from './h5ShortflowRuleResults'
import type { ScreenshotViewerEntry } from './screenshotCollection'
import EvidenceRedactionPanel from '@/components/evidence/EvidenceRedactionPanel.vue'
import { shortflowRedactionArtifacts } from './shortflowRedactionArtifacts'

const props = defineProps<{ taskId: string; refreshKey?: string }>()
const snapshot = ref<H5ShortflowSnapshot | null>(null)
const loading = ref(false)
const error = ref('')
const exportError = ref('')
const exporting = ref(false)
const viewerEntries = ref<ScreenshotViewerEntry[]>([])
const initialArtifactId = ref('')
const evidenceFiles = useEvidenceFileUrls()
const viewerTitle = ref('检查点原始截图')
const textReview = ref<{ artifact: H5ShortflowArtifact; artifacts: H5ShortflowArtifact[]; title: string } | null>(null)
const reviewContext = ref<{ binding: H5ShortflowBinding; manifest?: EvidenceManifestReview; checkpointId: string; attemptId: string } | null>(null)
const selectedCheckpoint = ref('')
const redactionPlanId = ref('')
const redactionMaterials = computed(() => Object.fromEntries((snapshot.value?.plans ?? []).map((plan) => [plan.execution_plan_id, shortflowRedactionArtifacts(plan)])))
let version = 0
let viewerVersion = 0
const statusLabels: Record<string, string> = {
  reserved: '待绑定', bound: '证据已绑定（非质检通过）', gap: '证据缺口',
  not_evaluated: '未观测', observed: '已观测（非质检通过）', needs_review: '待复核',
  pending: '质量待定', pass: '材料质量合格（非规则通过）', fail: '材料质量不合格',
}
const label = (value: string) => statusLabels[value] ?? '状态待核对'
watch(() => [props.taskId, props.refreshKey], () => { void load() }, { immediate: true })
onBeforeUnmount(() => { version++; closeViewer() })

function closeViewer() {
  viewerVersion++
  viewerEntries.value = []
  initialArtifactId.value = ''
  textReview.value = null
  reviewContext.value = null
  evidenceFiles.clear()
}

async function load() {
  const current = ++version
  snapshot.value = null
  closeViewer()
  selectedCheckpoint.value = ''
  redactionPlanId.value = ''
  error.value = ''
  exportError.value = ''
  exporting.value = false
  loading.value = true
  try {
    const result = await getH5Shortflows(props.taskId)
    if (current === version && result.task_id === props.taskId) snapshot.value = result
  } catch (cause) {
    if (current === version) error.value = cause instanceof Error ? cause.message : '检查点加载失败'
  } finally {
    if (current === version) loading.value = false
  }
}

function locateCheckpoint(planId: string, checkpointId: string) {
  const id = checkpointElementId(planId, checkpointId)
  const element = document.getElementById(id)
  if (!element) return
  selectedCheckpoint.value = id
  element.scrollIntoView?.({ block: 'center' })
  element.focus({ preventScroll: true })
}

async function showRuleEvidence(plan: H5ShortflowPlan, reference: H5ShortflowEvidenceRef, artifactId: string) {
  const linked = plan.rule_results?.find((result) => result.linkage_status === 'linked'
    && result.checkpoints.some((checkpoint) => checkpoint.evidence_refs.includes(reference)))
  if (!linked || !referencedScreenshots(plan, reference).some((item) => item.artifact_id === artifactId)) {
    error.value = '规则证据关联已变化，请刷新台账后核对。'
    return
  }
  await showArtifact(plan.execution_plan_id, reference.checkpoint_id, reference.attempt_id, artifactId, linked.rule_name)
}

async function showArtifact(planId: string, checkpointId: string, attemptId: string, artifactId: string, ruleName?: string) {
  const current = version
  const request = ++viewerVersion
  textReview.value = null
  reviewContext.value = null
  const isCurrent = () => current === version && request === viewerVersion
  const plan = snapshot.value?.plans.find((item) => item.execution_plan_id === planId)
  const attempt = plan?.checkpoints.find((item) => item.checkpoint_id === checkpointId)
    ?.attempts.find((item) => item.attempt_id === attemptId)
  const selected = attempt ? checkpointScreenshotEntries(checkpointId, attempt) : []
  error.value = ''
  if (!selected.some((entry) => entry.artifact_id === artifactId)) {
    viewerEntries.value = []
    error.value = '当前尝试没有可用的对应截图，请核对材料关联。'
    return
  }
  try {
    for (const entry of selected) {
      await evidenceFiles.load([entry.artifact_id])
      if (!isCurrent()) return
      const failure = evidenceFiles.getError(entry.artifact_id)
      if (failure) throw new Error(failure)
    }
    viewerTitle.value = ruleName ? `规则证据 · ${ruleName}` : '检查点原始截图'
    initialArtifactId.value = artifactId
    viewerEntries.value = selected
  } catch (cause) {
    if (isCurrent()) {
      viewerEntries.value = []
      error.value = cause instanceof Error ? cause.message : '截图读取失败'
    }
  }
}

async function showMaterial(binding: H5ShortflowBinding, checkpointId: string, attemptId: string, artifact: H5ShortflowArtifact, manifest?: EvidenceManifestReview) {
  const artifacts = manifest ? manifest.artifacts : binding.integrity === 'current' ? binding.artifacts : []
  if (manifest && (!manifest.review_only || manifest.integrity !== 'verified')) return
  if (!artifacts.some((item) => item.artifact_id === artifact.artifact_id)) return
  closeViewer()
  const current = version
  const request = ++viewerVersion
  const title = `${checkpointId} · ${manifest && !manifest.is_latest ? '历史只读' : '材料只读'} · Manifest v${manifest?.manifest_version ?? binding.latest_manifest_version ?? binding.manifest_version}`
  reviewContext.value = { binding, manifest, checkpointId, attemptId }
  if (artifact.artifact_type !== 'screenshot') {
    textReview.value = { artifact, artifacts, title }
    return
  }
  const entries: ScreenshotViewerEntry[] = artifacts.filter((item) => item.artifact_type === 'screenshot').map((item) => ({
    artifact_id: item.artifact_id, page_name: `${checkpointId} · ${attemptId}`, captured_at: binding.captured_at,
    capture_role: binding.role, page_capture_id: binding.capture_id, duplicate_count: 1, related_rules: [], actions: [],
  }))
  try {
    await evidenceFiles.load(entries.map((item) => item.artifact_id))
    if (current !== version || request !== viewerVersion) return
    const failure = entries.map((item) => evidenceFiles.getError(item.artifact_id)).find(Boolean)
    if (failure) throw new Error(failure)
    viewerTitle.value = title
    initialArtifactId.value = artifact.artifact_id
    viewerEntries.value = entries
  } catch (cause) {
    if (current === version && request === viewerVersion) error.value = cause instanceof Error ? cause.message : '材料读取失败'
  }
}

function showSampleScreenshot(artifactId: string) {
  const context = reviewContext.value
  const artifact = textReview.value?.artifacts.find((item) => item.artifact_id === artifactId)
  if (context && artifact) void showMaterial(context.binding, context.checkpointId, context.attemptId, artifact, context.manifest)
}

async function exportPlan(planId: string, mode: 'original' | 'redacted' = 'original') {
  if (!snapshot.value || exporting.value) return
  const current = version
  exporting.value = true
  exportError.value = ''
  try {
    if (mode === 'original') await downloadH5ShortflowArchive(props.taskId, planId, snapshot.value.snapshot_hash)
    else await downloadH5ShortflowArchive(props.taskId, planId, snapshot.value.snapshot_hash, mode)
  } catch (cause) {
    if (current === version) exportError.value = cause instanceof Error ? cause.message : '整组导出失败'
  } finally { if (current === version) exporting.value = false }
}

async function exportRedactedPlan(planId: string, redactionIds: Record<string, string>) {
  if (!snapshot.value || exporting.value) return
  const materials = redactionMaterials.value[planId]
  if (!materials || materials.blockedReason || !materials.artifacts.length
    || materials.artifacts.some((artifact) => !redactionIds[artifact.artifact_id])) return
  const current = version
  const taskId = props.taskId
  exporting.value = true; exportError.value = ''
  try {
    await downloadH5ShortflowRedactedArchive(taskId, planId, snapshot.value.snapshot_hash, redactionIds)
  } catch (cause) {
    if (current === version) exportError.value = cause instanceof Error ? cause.message : '脱敏包导出失败'
  } finally { if (current === version) exporting.value = false }
}

async function downloadArtifact(artifactId: string) {
  if (!viewerEntries.value.some((entry) => entry.artifact_id === artifactId)
    && textReview.value?.artifact.artifact_id !== artifactId) return
  const current = version
  const request = viewerVersion
  try {
    const url = await issueEvidenceFileUrl(artifactId, { download: true })
    if (current === version && request === viewerVersion) window.open(`${url}${url.includes('?') ? '&' : '?'}representation=original`, '_blank', 'noopener,noreferrer')
  } catch (cause) {
    if (current === version && request === viewerVersion) error.value = cause instanceof Error ? cause.message : '材料下载失败'
  }
}
</script>

<template>
  <section class="workstation-glass-panel checkpoint-panel" data-testid="h5-checkpoint-panel">
    <h2>V2 检查点与流程材料</h2>
    <p class="qc-muted">检查点观测、材料质量、规则判定和最终 Gate 是不同结果；待复核不能算通过。</p>
    <button type="button" class="qc-btn qc-btn--secondary" :disabled="loading" @click="load">刷新台账</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="exportError" role="alert">{{ exportError }}</p>
    <p v-if="loading">读取检查点中…</p>
    <p v-else-if="snapshot && !snapshot.plans.length">当前任务没有 V2 短流程，旧步骤不会补算检查点证据。</p>
    <article v-for="plan in snapshot?.plans ?? []" :key="plan.execution_plan_id">
      <h3>{{ plan.definition_id }} · v{{ plan.version }} · {{ plan.scenario_key }}</h3>
      <p>Run：{{ plan.agent_run_id }} · 版本哈希：{{ plan.content_hash }}</p>
      <button type="button" class="qc-btn qc-btn--secondary" :disabled="exporting" data-testid="shortflow-export" @click="exportPlan(plan.execution_plan_id)">整组材料导出</button>
      <button type="button" class="qc-btn qc-btn--secondary" :disabled="exporting" data-testid="shortflow-export-redacted" @click="exportPlan(plan.execution_plan_id, 'redacted')">脱敏导出（整图遮盖）</button>
      <p>脱敏包仅供分享材料结构和追溯，整图遮盖，不可据此判定业务。</p>
      <button type="button" class="qc-btn qc-btn--secondary" data-testid="shortflow-redaction-open" @click="redactionPlanId = redactionPlanId === plan.execution_plan_id ? '' : plan.execution_plan_id">局部脱敏与业务复核</button>
      <EvidenceRedactionPanel v-if="redactionPlanId === plan.execution_plan_id" :key="`${taskId}:${snapshot?.snapshot_hash}:${plan.execution_plan_id}`"
        v-bind="redactionMaterials[plan.execution_plan_id]" :exporting="exporting" :export-error="exportError"
        @export="exportRedactedPlan(plan.execution_plan_id, $event)" />
      <H5ShortflowRuleResults :plan="plan" @locate-checkpoint="locateCheckpoint(plan.execution_plan_id, $event)"
        @view-evidence="(reference, artifactId) => showRuleEvidence(plan, reference, artifactId)" />
      <details v-if="plan.timeline?.length" data-testid="actual-evidence-timeline">
        <summary>实际发生时间线</summary>
        <p>按已记录的实际时间排序；相同时刻以稳定身份排列，不推断先后。未知时间置后，不以计划顺序补齐。</p>
        <ol><li v-for="event in plan.timeline" :key="event.event_id">
          {{ event.occurred_at || '时间未知' }} · {{ event.event_type === 'capture' ? '采样' : event.event_type === 'action_started' ? '动作开始' : '动作结束' }} · {{ event.checkpoint_id }}（计划第 {{ event.planned_order }} 项）
          <p>{{ event.event_id }} · 会话 {{ event.session_id }} · Epoch {{ event.finalization_epoch }} · {{ event.role }} · {{ event.capture_id }}</p>
        </li></ol>
      </details>
      <h4>计划检查点顺序</h4>
      <ol>
        <li v-for="checkpoint in plan.checkpoints" :id="checkpointElementId(plan.execution_plan_id, checkpoint.checkpoint_id)"
          :key="checkpoint.checkpoint_id" tabindex="-1" data-testid="shortflow-checkpoint-materials"
          :class="{ 'is-checkpoint-selected': selectedCheckpoint === checkpointElementId(plan.execution_plan_id, checkpoint.checkpoint_id) }">
          <strong>{{ checkpoint.checkpoint_id }}</strong>
          <p v-if="!checkpoint.attempts.length">尚无实际执行尝试</p>
          <div v-for="attempt in checkpoint.attempts" :key="attempt.attempt_id">
            <p>{{ label(attempt.status) }} · {{ label(attempt.observation_status) }} · Epoch {{ attempt.finalization_epoch }}</p>
            <p>尝试：{{ attempt.attempt_id }} · 会话：{{ attempt.session_id }} · {{ attempt.created_at }}</p>
            <p v-if="!attempt.bindings.length">尚无精确前后材料绑定</p>
            <div v-for="binding in attempt.bindings" :key="binding.role">
              <p>{{ binding.role }} · Capture {{ binding.capture_id }} · 动作 {{ binding.action_execution_id }}</p>
              <p>绑定 Manifest v{{ binding.manifest_version }} / 最新 v{{ binding.latest_manifest_version ?? '未知' }} · {{ label(binding.quality_status) }} · {{ binding.integrity === 'current' ? '身份一致' : '身份或材料已变化' }}</p>
              <button v-for="artifact in binding.artifacts.filter((item) => item.artifact_type === 'screenshot')" :key="artifact.artifact_id"
                type="button" class="qc-btn qc-btn--secondary" :disabled="binding.integrity !== 'current'"
                @click="showArtifact(plan.execution_plan_id, checkpoint.checkpoint_id, attempt.attempt_id, artifact.artifact_id)">查看截图 {{ artifact.artifact_id }}</button>
              <button v-for="artifact in binding.artifacts.filter((item) => item.artifact_type !== 'screenshot')" :key="artifact.artifact_id"
                type="button" class="qc-btn qc-btn--secondary" :disabled="binding.integrity !== 'current'"
                @click="showMaterial(binding, checkpoint.checkpoint_id, attempt.attempt_id, artifact)">查看材料 {{ artifact.artifact_type }} · {{ artifact.artifact_id }}</button>
              <EvidenceManifestVersions :versions="binding.manifest_versions ?? []"
                @view="(manifest, artifact) => showMaterial(binding, checkpoint.checkpoint_id, attempt.attempt_id, artifact, manifest)" />
            </div>
          </div>
        </li>
      </ol>
    </article>
    <ScreenshotEvidenceViewer v-if="viewerEntries.length" :entries="viewerEntries" :initial-artifact-id="initialArtifactId" :title="viewerTitle" :collection="true" :image-url="evidenceFiles.get" :image-error="evidenceFiles.getError" :format-time="(value) => value || '未知'" @close="closeViewer" @download="downloadArtifact" />
    <EvidenceTextViewer v-if="textReview" v-bind="textReview" @close="closeViewer" @download="downloadArtifact" @view-screenshot="showSampleScreenshot" />
  </section>
</template>

<style scoped>
.checkpoint-panel { padding: 20px; margin-top: 16px; overflow-wrap: anywhere; }
.qc-btn { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
article { border-top: 1px solid var(--qc-border, #d9e1e8); margin-top: 16px; }
li { margin-top: 14px; }
.is-checkpoint-selected { outline: 2px solid var(--workstation-primary, #3674cf); outline-offset: 5px; border-radius: 4px; }
</style>
