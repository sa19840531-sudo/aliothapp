import { requestBlobV2, requestJsonV2 } from './http'

export interface RedactionArtifact {
  artifact_id: string
  artifact_type: string
  checksum: string
  content_type: string | null
  size_bytes: number
  manifest_id?: string
  manifest_version?: number
}

export interface RedactionSpec {
  method: 'image_regions' | 'text_ranges' | 'json_pointers' | 'reviewed_unchanged'
  image_width?: number
  image_height?: number
  regions?: Array<{ x: number; y: number; width: number; height: number }>
  ranges?: Array<{ start: number; end: number }>
  pointers?: string[]
}

export interface RedactionSource {
  artifact_id: string
  artifact_type: string
  source_hash: string
  original_checksum: string
  original_size_bytes: number
  original_content_type: string | null
  task_id: string
  agent_run_id: string | null
  session_id: string | null
  capture_id: string | null
  manifest_id: string | null
  manifest_version: number | null
  manifest_hash: string | null
}

export interface RedactionPlanSummary {
  redaction_id: string
  artifact_id: string
  source: RedactionSource
  policy_version: string
  plan_hash: string
  derived_checksum: string
  derived_size_bytes: number
  derived_content_type: string
  business_reviewable: true
  reviewed_by: string
  created_at: string
}

export interface RedactionPreview extends Omit<RedactionPlanSummary, 'redaction_id' | 'reviewed_by' | 'created_at'> {
  content_base64: string
}

export interface RedactionRequest {
  source_checksum: string
  manifest_id?: string
  manifest_version?: number
  spec: RedactionSpec
  review_confirmed: boolean
}

export function listEvidenceRedactions(artifactId: string, signal?: AbortSignal) {
  return requestJsonV2<{ items: RedactionPlanSummary[]; can_manage: boolean }>(`/evidence/${encodeURIComponent(artifactId)}/redactions`, { signal, cache: 'no-store' })
}

export function previewEvidenceRedaction(artifactId: string, body: RedactionRequest, signal?: AbortSignal) {
  return requestJsonV2<RedactionPreview>(`/evidence/${encodeURIComponent(artifactId)}/redactions/preview`, { method: 'POST', body: JSON.stringify(body), signal })
}

export function createEvidenceRedaction(artifactId: string, body: RedactionRequest, signal?: AbortSignal) {
  return requestJsonV2<RedactionPlanSummary>(`/evidence/${encodeURIComponent(artifactId)}/redactions`, { method: 'POST', body: JSON.stringify(body), signal })
}

export function readRedactedEvidence(redactionId: string, signal?: AbortSignal) {
  return requestBlobV2(`/evidence-redactions/${encodeURIComponent(redactionId)}/file`, { signal, cache: 'no-store' })
}
