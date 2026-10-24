<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { readRedactedEvidence, type RedactionPlanSummary } from '@/services/evidence-redactions.api'
import { readableText, verifyEvidenceBytes } from './redactionContent'

const props = defineProps<{ plan?: RedactionPlanSummary; bytes?: Uint8Array; contentType?: string }>()
const emit = defineEmits<{ ready: [redactionId: string]; failed: [redactionId: string] }>()
const imageUrl = ref('')
const text = ref('')
const error = ref('')
const loading = ref(false)
let generation = 0
let controller: AbortController | undefined

function clear() {
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ''; text.value = ''; error.value = ''
}

watch(() => [props.plan, props.bytes, props.contentType], () => { void load() }, { immediate: true })
onBeforeUnmount(() => { generation++; controller?.abort(); clear() })

async function load() {
  const current = ++generation
  controller?.abort(); controller = new AbortController(); clear(); loading.value = true
  try {
    let bytes = props.bytes
    const contentType = props.plan?.derived_content_type ?? props.contentType ?? ''
    if (props.plan) {
      if (!props.plan.business_reviewable) throw new Error('此派生证据尚未确认可用于业务复核')
      const blob = await readRedactedEvidence(props.plan.redaction_id, controller.signal)
      bytes = new Uint8Array(await blob.arrayBuffer())
      await verifyEvidenceBytes(bytes, props.plan.derived_size_bytes, props.plan.derived_checksum)
    }
    if (current !== generation) return
    if (!bytes) throw new Error('尚无可读取的派生证据')
    if (contentType.startsWith('image/')) imageUrl.value = URL.createObjectURL(new Blob([bytes as BlobPart], { type: contentType }))
    else text.value = readableText(bytes, contentType)
    if (props.plan) emit('ready', props.plan.redaction_id)
  } catch (cause) {
    if (current === generation) {
      error.value = cause instanceof Error ? cause.message : '脱敏证据读取失败'
      if (props.plan) emit('failed', props.plan.redaction_id)
    }
  } finally { if (current === generation) loading.value = false }
}
</script>

<template>
  <section class="redaction-review" data-testid="redaction-review">
    <p>脱敏派生证据用于业务复核；被遮盖内容不能推断，复核材料不代表规则或 Gate 通过。</p>
    <template v-if="plan">
      <p>复核人 {{ plan.reviewed_by }} · {{ plan.created_at }}</p>
      <details><summary>来源与版本追溯</summary>
        <p>版本 {{ plan.redaction_id }} · 策略 {{ plan.policy_version }}</p>
        <p>原件摘要 {{ plan.source.original_checksum }} · 派生摘要 {{ plan.derived_checksum }}</p>
        <p>Manifest {{ plan.source.manifest_id || '无' }} · v{{ plan.source.manifest_version ?? '无' }}</p>
      </details>
    </template>
    <p v-if="loading">读取并校验脱敏件中…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <img v-if="imageUrl" :src="imageUrl" alt="服务端生成的局部脱敏证据" />
    <pre v-if="text" tabindex="0">{{ text }}</pre>
  </section>
</template>

<style scoped>
.redaction-review { overflow-wrap: anywhere; }
img { display: block; max-width: 100%; max-height: 65vh; object-fit: contain; }
pre { max-height: 60vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
