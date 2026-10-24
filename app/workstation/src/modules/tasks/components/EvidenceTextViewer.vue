<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { H5ShortflowArtifact } from '@/services/h5-shortflows.api'
import { issueEvidenceFileUrl } from '@/services/task-insight.api'
import { isReadableEvidence, observationSampleReview, readEvidenceText } from './evidenceTextReview'

const props = defineProps<{ artifact: H5ShortflowArtifact; artifacts: H5ShortflowArtifact[]; title: string }>()
const emit = defineEmits<{ close: []; download: [artifactId: string]; viewScreenshot: [artifactId: string] }>()
const text = ref('')
const error = ref('')
const loading = ref(false)
const observation = computed(() => observationSampleReview(text.value, props.artifact.artifact_id, props.artifacts))
const samples = computed(() => observation.value.samples)
let generation = 0
let controller: AbortController | undefined
watch(() => props.artifact, () => { void load() }, { immediate: true })
onBeforeUnmount(() => { generation++; controller?.abort() })

async function load() {
  const current = ++generation
  controller?.abort()
  controller = new AbortController()
  const signal = controller.signal
  text.value = ''; error.value = ''; loading.value = true
  try {
    if (!isReadableEvidence(props.artifact)) throw new Error('此材料仅支持下载原件，文本阅读上限为 512 KiB')
    const url = await issueEvidenceFileUrl(props.artifact.artifact_id)
    if (current !== generation) return
    const originalUrl = `${url}${url.includes('?') ? '&' : '?'}representation=original`
    const content = await readEvidenceText(originalUrl, props.artifact, signal)
    if (current === generation) text.value = content
  } catch (cause) {
    if (current === generation) error.value = cause instanceof Error ? cause.message : '材料读取失败'
  } finally { if (current === generation) loading.value = false }
}
</script>

<template>
  <section class="evidence-text-viewer" role="dialog" aria-modal="false" :aria-label="title" data-testid="evidence-text-viewer">
    <h3>{{ title }}</h3>
    <p>材料 {{ artifact.artifact_id }} · {{ artifact.artifact_type }} · {{ artifact.content_type }} · {{ artifact.size_bytes }} 字节</p>
    <p>只读审阅，历史版本不会成为当前判定输入。DOM 和 HTML 始终作为纯文本显示。</p>
    <button type="button" class="qc-btn qc-btn--secondary" @click="emit('close')">关闭材料</button>
    <button type="button" class="qc-btn qc-btn--secondary" @click="emit('download', artifact.artifact_id)">下载此材料原件</button>
    <p v-if="loading">读取并校验原件中…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="observation.notice" role="alert">{{ observation.notice }}</p>
    <div v-if="samples.length" data-testid="observation-timeline">
      <h4>连续样本实际时序</h4>
      <p>按本采样包单调时钟排序，不能跨包或跨会话换算墙钟；同刻不推断先后。重复摘要保留全部对应原件，不猜测唯一图片身份。</p>
      <ol><li v-for="sample in samples" :key="sample.id">
        <p>样本 {{ sample.original_index + 1 }} · 单调时刻 {{ sample.observed_at_ms }} ms · 距首样本 {{ sample.elapsed_ms }} ms · {{ sample.id }}</p>
        <p v-if="!sample.screenshot_ids.length">所选 Manifest 中未找到摘要一致的原始截图。</p>
        <p v-if="sample.screenshot_ids.length > 1">多份原件摘要相同，无法确定唯一制品身份，以下材料全部保留。</p>
        <button v-for="id in sample.screenshot_ids" :key="id" type="button" class="qc-btn qc-btn--secondary" @click="emit('viewScreenshot', id)">查看样本截图 {{ id }}</button>
      </li></ol>
    </div>
    <pre v-if="text" tabindex="0">{{ text }}</pre>
  </section>
</template>

<style scoped>
.evidence-text-viewer { margin-top: 16px; padding: 16px; border: 1px solid var(--qc-border, #d9e1e8); overflow-wrap: anywhere; }
pre { max-height: 60vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
button { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
</style>
