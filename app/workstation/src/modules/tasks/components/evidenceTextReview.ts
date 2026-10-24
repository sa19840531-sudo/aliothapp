import type { H5ShortflowArtifact } from '@/services/h5-shortflows.api'

export const EVIDENCE_TEXT_LIMIT = 512 * 1024

export function isReadableEvidence(artifact: H5ShortflowArtifact) {
  const media = (artifact.content_type || '').split(';')[0]!.trim().toLowerCase()
  return artifact.size_bytes <= EVIDENCE_TEXT_LIMIT && (media.startsWith('text/')
    || media === 'application/json' || media.endsWith('+json') || media === 'application/xml')
}

export async function readEvidenceText(url: string, artifact: H5ShortflowArtifact, signal: AbortSignal) {
  if (!isReadableEvidence(artifact)) throw new Error('此材料仅支持下载原件，文本阅读上限为 512 KiB')
  const response = await fetch(url, { signal, cache: 'no-store' })
  if (!response.ok) throw new Error(`材料读取失败（${response.status}）`)
  if (!response.body) throw new Error('材料响应没有可读取内容')
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > EVIDENCE_TEXT_LIMIT || size > artifact.size_bytes) throw new Error('材料大小与清单不一致')
      chunks.push(value)
    }
  } finally { await reader.cancel() }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
    .map((byte) => byte.toString(16).padStart(2, '0')).join('')
  if (size !== artifact.size_bytes || `sha256:${hash}` !== artifact.checksum) throw new Error('材料摘要或大小与所选 Manifest 不一致')
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
}

export interface EvidenceReviewSample {
  id: string
  original_index: number
  observed_at_ms: number
  elapsed_ms: number
  screenshot_ids: string[]
}

export function observationSampleReview(text: string, packetId: string, artifacts: H5ShortflowArtifact[]) {
  let data: { schema_version?: unknown; samples?: unknown }
  const empty = { samples: [] as EvidenceReviewSample[], notice: '' }
  try { data = JSON.parse(text) } catch { return empty }
  if (!data || data.schema_version !== 'checkpoint-observation.v1') return empty
  if (!Array.isArray(data.samples)) return { ...empty, notice: '采样包缺少有效样本数组，不能还原实际时序；请核对原文。' }
  const total = data.samples.length
  const samples = data.samples.flatMap((sample, index) => {
    if (!sample || typeof sample !== 'object' || typeof sample.observed_at_ms !== 'number'
      || !Number.isFinite(sample.observed_at_ms) || typeof sample.screenshot_sha256 !== 'string') return []
    return [{ id: `${packetId}:sample:${index}`, original_index: index,
      observed_at_ms: sample.observed_at_ms, elapsed_ms: 0,
      screenshot_ids: artifacts.filter((artifact) => artifact.artifact_type === 'screenshot'
        && artifact.checksum === `sha256:${sample.screenshot_sha256}`).map((artifact) => artifact.artifact_id) }]
  }).sort((a, b) => a.observed_at_ms - b.observed_at_ms || a.original_index - b.original_index)
  const first = samples[0]?.observed_at_ms ?? 0
  const invalid = total - samples.length
  return { samples: samples.map((sample) => ({ ...sample, elapsed_ms: sample.observed_at_ms - first })),
    notice: invalid ? `采样包共 ${total} 项，其中 ${invalid} 项缺少有效时间或截图摘要，以下时序不完整；缺失项请核对原文，不能认定观测完整。`
      : !total ? '采样包没有实际样本，不能认定观测完整。' : '' }
}

export function observationSamples(text: string, packetId: string, artifacts: H5ShortflowArtifact[]): EvidenceReviewSample[] {
  return observationSampleReview(text, packetId, artifacts).samples
}
