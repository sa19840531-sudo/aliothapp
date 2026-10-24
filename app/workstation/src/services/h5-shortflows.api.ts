import { downloadFile, requestJsonV2 } from './http'
import type { H5ShortflowSelection } from '../types/task'

export type { H5ShortflowSelection } from '../types/task'

export interface JourneyEvidenceCondition {
  checkpoint_id: string
  requirements: Array<{ role: string; artifact_types: string[]; minimum_count?: number; capture_phase?: string }>
}

export interface JourneyRuleContract {
  definition_id: string
  version: number
  content_hash: string
  workflow_skill: 'h5-player-workflow' | 'h5-payment-workflow'
  scenario_key: string
  evidence_conditions: JourneyEvidenceCondition[]
}

export interface H5ShortflowCatalog {
  definitions: Array<Omit<H5ShortflowSelection, 'scenario_key' | 'rule_ids'> & {
    name: string
    checkpoints: Array<{ checkpoint_id: string; depends_on: string[] }>
    evidence_conditions?: JourneyEvidenceCondition[]
  }>
  rules: Array<{ rule_id: string; rule_code: string; name: string; ruleset_code: string }>
}

export interface H5ShortflowArtifact {
  artifact_id: string
  artifact_type: string
  checksum: string
  content_type: string | null
  size_bytes: number
}

export interface H5ShortflowBinding {
  role: string
  capture_id: string
  action_execution_id: string
  manifest_id: string
  manifest_version: number
  latest_manifest_version: number | null
  quality_status: string
  integrity: string
  captured_at: string | null
  artifacts: H5ShortflowArtifact[]
  action_started_at?: string | null
  action_finished_at?: string | null
  manifest_versions?: EvidenceManifestReview[]
}

export interface EvidenceManifestReview {
  manifest_id: string
  manifest_version: number
  manifest_hash: string
  created_at: string
  quality_status: string
  quality_reason: string | null
  is_bound: boolean
  is_latest: boolean
  review_only: true
  integrity: 'verified' | 'artifact_mismatch'
  artifacts: H5ShortflowArtifact[]
}

export interface EvidenceTimelineEvent {
  event_id: string
  event_type: 'capture' | 'action_started' | 'action_finished'
  occurred_at: string | null
  checkpoint_id: string
  planned_order: number
  attempt_id: string
  session_id: string
  finalization_epoch: number
  role: string | null
  capture_id: string | null
  action_execution_id: string
}

export interface H5ShortflowAttempt {
  attempt_id: string
  session_id: string
  finalization_epoch: number
  status: string
  observation_status: string
  created_at: string
  bindings: H5ShortflowBinding[]
}

export interface H5ShortflowEvidenceRef {
  checkpoint_id: string
  attempt_id: string
  session_id: string
  role: string
  capture_id: string
  manifest_id: string
  manifest_version: number
  artifact_ids: string[]
}

export interface H5ShortflowCheckpointResult {
  checkpoint_id: string
  status: 'pass' | 'fail' | 'evidence_insufficient'
  reason: string | null
  missing: string[]
  checks: Array<{ name: string; passed: boolean }>
  evidence_refs: H5ShortflowEvidenceRef[]
}

export interface H5ShortflowRuleResult {
  rule_id: string
  rule_code: string
  rule_name: string
  rule_contract_hash: string
  execution_id: string | null
  input_set_id: string | null
  input_hash: string | null
  execution_status: string | null
  result_summary: string | null
  problem_desc: string | null
  review_status: string | null
  review_reason: string | null
  finalization_epoch: number
  linkage_status: 'linked' | 'missing_result' | 'input_mismatch' | 'evidence_changed'
  linkage_notice: string | null
  checkpoints: H5ShortflowCheckpointResult[]
}

export interface H5ShortflowPlan {
  execution_plan_id: string
  agent_run_id: string
  definition_id: string
  version: number
  content_hash: string
  scenario_key: string
  checkpoints: Array<{ checkpoint_id: string; attempts: H5ShortflowAttempt[] }>
  timeline?: EvidenceTimelineEvent[]
  // 兼容尚未提供规则关联的旧接口，缺少字段不能推断为质检通过。
  rule_results_status?: 'available' | 'partial' | 'unavailable'
  rule_results_notice?: string | null
  rule_results?: H5ShortflowRuleResult[]
}

export interface H5ShortflowSnapshot {
  task_id: string
  snapshot_hash: string
  finalization_epoch: number
  finalization_state: string
  plans: H5ShortflowPlan[]
}

export function getH5ShortflowCatalog() {
  return requestJsonV2<H5ShortflowCatalog>('/h5-shortflows/catalog')
}

export function getH5Shortflows(taskId: string) {
  return requestJsonV2<H5ShortflowSnapshot>(`/tasks/${encodeURIComponent(taskId)}/h5-shortflows`)
}

export function downloadH5ShortflowArchive(taskId: string, planId: string, snapshotHash: string, mode: 'original' | 'redacted' = 'original') {
  return downloadFile(
    `/tasks/${encodeURIComponent(taskId)}/h5-shortflows/${encodeURIComponent(planId)}/archive?snapshot_hash=${encodeURIComponent(snapshotHash)}&mode=${mode}`,
    `task_${taskId}_shortflow.zip`, 'v2',
  )
}

/** 逐份绑定已经固化的脱敏版本，服务端仍须核验完整闭集和当前权限。 */
export function downloadH5ShortflowRedactedArchive(taskId: string, planId: string, snapshotHash: string, redactionIds: Record<string, string>) {
  return downloadFile(
    `/tasks/${encodeURIComponent(taskId)}/h5-shortflows/${encodeURIComponent(planId)}/archive`,
    `task_${taskId}_reviewable_redacted.zip`, 'v2',
    { method: 'POST', body: JSON.stringify({ snapshot_hash: snapshotHash, redaction_ids: redactionIds }) },
  )
}
