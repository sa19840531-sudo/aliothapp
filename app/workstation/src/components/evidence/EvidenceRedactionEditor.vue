<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { createEvidenceRedaction, previewEvidenceRedaction, type RedactionArtifact, type RedactionPlanSummary, type RedactionRequest, type RedactionSpec } from '@/services/evidence-redactions.api'
import { issueEvidenceFileUrl } from '@/services/task-insight.api'
import EvidenceRedactionReview from './EvidenceRedactionReview.vue'
import EvidenceRedactionImage from './EvidenceRedactionImage.vue'
import { matchesSource, previewBytes, readableText, readOriginalBytes } from './redactionContent'

const props = defineProps<{ artifact: RedactionArtifact; canManage: boolean }>()
const emit = defineEmits<{ saved: [plan: RedactionPlanSummary]; close: [] }>()
const method = ref<RedactionSpec['method']>('image_regions')
const rows = ref('')
const width = ref(0)
const height = ref(0)
const originalUrl = ref('')
const originalText = ref('')
const loaded = ref(false)
const busy = ref(false)
const error = ref('')
const confirmed = ref(false)
const unchangedConfirmed = ref(false)
const derived = ref<Uint8Array>()
const derivedType = ref('')
const previewKey = ref('')
const previewIdentity = ref({ planHash: '', checksum: '', size: 0 })
const media = computed(() => (props.artifact.content_type ?? '').split(';')[0]!.trim().toLowerCase())
const isImage = computed(() => ['image/png', 'image/jpeg', 'image/webp'].includes(media.value))
const isText = computed(() => ['text/plain', 'text/html'].includes(media.value))
const isJson = computed(() => media.value === 'application/json')
const supported = computed(() => isImage.value || isText.value || isJson.value)
const draftKey = computed(() => JSON.stringify([props.artifact, method.value, rows.value, width.value, height.value]))
const imageRegions = computed(() => method.value === 'image_regions' ? rows.value.split('\n').flatMap((line) => {
  const values = line.trim().split(/[,，\s]+/).map(Number)
  return values.length === 4 && values.every(Number.isFinite) ? [{ x: values[0]!, y: values[1]!, width: values[2]!, height: values[3]! }] : []
}) : [])
let generation = 0
let controller: AbortController | undefined

watch(draftKey, () => { derived.value = undefined; previewKey.value = ''; confirmed.value = false; unchangedConfirmed.value = false })
watch(() => [props.artifact, props.canManage], () => { void load() }, { immediate: true })
onBeforeUnmount(() => { generation++; controller?.abort(); revokeOriginal() })

function revokeOriginal() {
  if (originalUrl.value) URL.revokeObjectURL(originalUrl.value)
  originalUrl.value = ''
}

async function load() {
  const current = ++generation
  controller?.abort(); controller = new AbortController()
  revokeOriginal(); loaded.value = false; originalText.value = ''; error.value = ''; rows.value = ''
  derived.value = undefined; confirmed.value = false; previewKey.value = ''; width.value = 0; height.value = 0
  method.value = isImage.value ? 'image_regions' : isJson.value ? 'json_pointers' : 'text_ranges'
  if (!props.canManage) return
  if (!supported.value) {
    busy.value = false
    error.value = `材料格式 ${media.value || '未声明'} 尚未注册局部脱敏策略，不能生成可复核脱敏件。`
    return
  }
  busy.value = true
  try {
    const url = await issueEvidenceFileUrl(props.artifact.artifact_id)
    if (current !== generation) return
    const bytes = await readOriginalBytes(`${url}${url.includes('?') ? '&' : '?'}representation=original`, props.artifact, controller.signal)
    if (current !== generation) return
    if (isImage.value) originalUrl.value = URL.createObjectURL(new Blob([bytes as BlobPart], { type: props.artifact.content_type ?? 'image/png' }))
    else originalText.value = readableText(bytes, props.artifact.content_type ?? '')
    loaded.value = true
  } catch (cause) { if (current === generation) error.value = cause instanceof Error ? cause.message : '原件读取失败' }
  finally { if (current === generation) busy.value = false }
}

function imageLoaded(imageWidth: number, imageHeight: number) {
  width.value = imageWidth; height.value = imageHeight
}

function addRegion(region: { x: number; y: number; width: number; height: number }) {
  if (busy.value || method.value !== 'image_regions') return
  rows.value = `${rows.value.trim()}${rows.value.trim() ? '\n' : ''}${region.x},${region.y},${region.width},${region.height}`
}

function spec(): RedactionSpec {
  if (!supported.value) throw new Error('此材料格式尚未注册局部脱敏策略')
  if (method.value === 'reviewed_unchanged') return { method: method.value }
  const lines = rows.value.trim().split('\n').map((line) => line.trim()).filter(Boolean)
  if (!lines.length) throw new Error('请填写需要遮盖的区域、范围或路径')
  if (method.value === 'json_pointers') return { method: method.value, pointers: lines }
  const values = lines.map((line) => line.split(/[,，\s]+/).map(Number))
  if (values.some((line) => line.some((value) => !Number.isSafeInteger(value)))) throw new Error('坐标与范围须为整数')
  if (method.value === 'image_regions') {
    if (!width.value || !height.value) throw new Error('原图尺寸尚未就绪')
    if (values.some((line) => line.length !== 4 || line[0]! < 0 || line[1]! < 0 || line[2]! <= 0 || line[3]! <= 0 || line[0]! + line[2]! > width.value || line[1]! + line[3]! > height.value)) throw new Error('矩形须位于原图范围内且宽高大于零')
    return { method: method.value, image_width: width.value, image_height: height.value,
      regions: values.map(([x, y, w, h]) => ({ x: x!, y: y!, width: w!, height: h! })) }
  }
  const length = Array.from(originalText.value).length
  if (values.some((line) => line.length !== 2 || line[0]! < 0 || line[1]! <= line[0]! || line[1]! > length)) throw new Error('文本范围须位于原文代码点范围内且起点小于终点')
  return { method: method.value, ranges: values.map(([start, end]) => ({ start: start!, end: end! })) }
}

function request(reviewConfirmed: boolean): RedactionRequest {
  return { source_checksum: props.artifact.checksum, manifest_id: props.artifact.manifest_id,
    manifest_version: props.artifact.manifest_version, spec: spec(), review_confirmed: reviewConfirmed }
}

async function preview() {
  if (!props.canManage || !loaded.value || busy.value) return
  const current = generation
  const key = draftKey.value
  busy.value = true; error.value = ''; derived.value = undefined; confirmed.value = false
  try {
    const result = await previewEvidenceRedaction(props.artifact.artifact_id, request(false), controller?.signal)
    if (!matchesSource(result, props.artifact)) throw new Error('派生证据与所选原件或 Manifest 不一致')
    const bytes = await previewBytes(result)
    if (current !== generation || key !== draftKey.value) return
    derived.value = bytes; derivedType.value = result.derived_content_type; previewKey.value = key
    previewIdentity.value = { planHash: result.plan_hash, checksum: result.derived_checksum, size: result.derived_size_bytes }
  } catch (cause) { if (current === generation) error.value = cause instanceof Error ? cause.message : '预览失败' }
  finally { if (current === generation) busy.value = false }
}

async function save() {
  if (!props.canManage || busy.value || !confirmed.value || !derived.value || previewKey.value !== draftKey.value
    || (method.value === 'reviewed_unchanged' && !unchangedConfirmed.value)) return
  const current = generation
  busy.value = true; error.value = ''
  try {
    const result = await createEvidenceRedaction(props.artifact.artifact_id, request(true), controller?.signal)
    if (current !== generation) return
    if (!matchesSource(result, props.artifact)) throw new Error('保存的派生证据与原件不一致')
    if (result.plan_hash !== previewIdentity.value.planHash || result.derived_checksum !== previewIdentity.value.checksum
      || result.derived_size_bytes !== previewIdentity.value.size) throw new Error('保存版本与已确认的预览不一致，请重新核对')
    emit('saved', result)
  } catch (cause) { if (current === generation) error.value = cause instanceof Error ? cause.message : '保存失败' }
  finally { if (current === generation) busy.value = false }
}
</script>

<template>
  <section class="redaction-editor" data-testid="redaction-editor">
    <h4>局部脱敏 · {{ artifact.artifact_id }}</h4>
    <p>原件保持不变。按人工确认的范围生成派生件；不自动识别敏感信息，请核对截图、文本和相关材料。</p>
    <p v-if="!canManage">当前身份不能查看原件或管理脱敏方案。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="canManage && supported">
      <details v-if="loaded" open><summary>原件（仅编辑者可见）</summary>
        <EvidenceRedactionImage v-if="originalUrl" :src="originalUrl" :regions="imageRegions" @size="imageLoaded" @region="addRegion" />
        <pre v-else tabindex="0">{{ originalText }}</pre>
      </details>
      <label>处理方式 <select v-model="method" :disabled="busy" data-testid="redaction-method">
        <option v-if="isImage" value="image_regions">图片矩形遮盖</option>
        <option v-if="isText" value="text_ranges">文本范围遮盖</option>
        <option v-if="isJson" value="json_pointers">JSON 路径遮盖</option>
        <option value="reviewed_unchanged">已人工核对，无需遮盖</option>
      </select></label>
      <p v-if="method === 'image_regions'">原图 {{ width }} × {{ height }} 像素（按显示方向）。可拖动框选，或每行填写 x,y,宽,高。原件上的框仅辅助定位，请以服务端派生预览核对实际遮盖。</p>
      <p v-if="method === 'text_ranges'">每行填写 起点,终点，按 Unicode 代码点从 0 计数，含起点、不含终点；原文共 {{ Array.from(originalText).length }} 个代码点。</p>
      <p v-if="method === 'json_pointers'">每行填写一个精确 JSON Pointer，例如 /customer/phone；特殊键的 ~ 和 / 分别写为 ~0 和 ~1。</p>
      <textarea v-if="method !== 'reviewed_unchanged'" v-model="rows" :disabled="busy" rows="4" aria-label="脱敏区域或范围" data-testid="redaction-ranges" />
      <p v-else>此选择会保留全部内容。请确认本材料无需遮盖，不能用于跳过敏感信息核对。</p>
      <button class="qc-btn qc-btn--secondary" type="button" :disabled="busy || !loaded" data-testid="redaction-preview" @click="preview">生成服务端预览</button>
      <EvidenceRedactionReview v-if="derived" :bytes="derived" :content-type="derivedType" />
      <label v-if="derived"><input v-model="confirmed" type="checkbox" data-testid="redaction-confirm" />已检查派生件：隐私已处理，保留的业务内容可用于复核。</label>
      <label v-if="derived && method === 'reviewed_unchanged'"><input v-model="unchangedConfirmed" type="checkbox" data-testid="redaction-unchanged-confirm" />再次确认：本材料不含需要遮盖的信息，允许完整内容进入复核包。</label>
      <button class="qc-btn qc-btn--primary" type="button" :disabled="busy || !confirmed || !derived || (method === 'reviewed_unchanged' && !unchangedConfirmed)" data-testid="redaction-save" @click="save">保存不可变脱敏版本</button>
    </template>
    <button class="qc-btn qc-btn--secondary" type="button" @click="emit('close')">关闭编辑</button>
  </section>
</template>

<style scoped>
.redaction-editor { margin-top: 12px; padding: 14px; border: 1px solid var(--workstation-border); overflow-wrap: anywhere; }
img { display: block; max-width: 100%; max-height: 60vh; object-fit: contain; }
pre { max-height: 45vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
label { display: block; margin: 10px 0; }
textarea { width: 100%; box-sizing: border-box; }
button { margin: 6px 8px 6px 0; max-width: 100%; white-space: normal; }
</style>
