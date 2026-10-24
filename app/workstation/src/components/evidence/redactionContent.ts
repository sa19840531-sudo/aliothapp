import type { RedactionArtifact, RedactionPlanSummary, RedactionPreview } from '@/services/evidence-redactions.api'

export const REDACTION_CONTENT_LIMIT = 16 * 1024 * 1024

export async function verifyEvidenceBytes(bytes: Uint8Array, size: number, checksum: string) {
  if (bytes.byteLength !== size || size > REDACTION_CONTENT_LIMIT) throw new Error('证据大小与清单不一致或超过 16 MiB')
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes as BufferSource)))
    .map((byte) => byte.toString(16).padStart(2, '0')).join('')
  if (`sha256:${hash}` !== checksum) throw new Error('证据摘要与清单不一致')
}

export async function readOriginalBytes(url: string, artifact: RedactionArtifact, signal: AbortSignal) {
  if (artifact.size_bytes > REDACTION_CONTENT_LIMIT) throw new Error('局部脱敏编辑上限为 16 MiB')
  const response = await fetch(url, { signal, cache: 'no-store' })
  if (!response.ok || !response.body) throw new Error('原件读取失败')
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    while (true) {
      const item = await reader.read()
      if (item.done) break
      size += item.value.byteLength
      if (size > artifact.size_bytes || size > REDACTION_CONTENT_LIMIT) throw new Error('原件大小与清单不一致')
      chunks.push(item.value)
    }
  } finally { await reader.cancel() }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  await verifyEvidenceBytes(bytes, artifact.size_bytes, artifact.checksum)
  return bytes
}

export async function previewBytes(preview: RedactionPreview) {
  if (preview.content_base64.length > REDACTION_CONTENT_LIMIT * 1.4) throw new Error('派生证据超过预览上限')
  const bytes = Uint8Array.from(atob(preview.content_base64), (value) => value.charCodeAt(0))
  await verifyEvidenceBytes(bytes, preview.derived_size_bytes, preview.derived_checksum)
  return bytes
}

export function matchesSource(plan: RedactionPlanSummary | RedactionPreview, artifact: RedactionArtifact) {
  return plan.artifact_id === artifact.artifact_id && plan.business_reviewable === true
    && plan.source.original_checksum === artifact.checksum
    && (!artifact.manifest_id || plan.source.manifest_id === artifact.manifest_id)
    && (artifact.manifest_version === undefined || plan.source.manifest_version === artifact.manifest_version)
}

export function readableText(bytes: Uint8Array, contentType: string) {
  if (!(contentType.startsWith('text/') || /(?:json|xml)/.test(contentType))) throw new Error('此材料格式不支持文本预览')
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
}
