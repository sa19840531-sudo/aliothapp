/** 任务详情过程可视化读模型（对应 /workstation/tasks/{id}/* 聚合端点）。 */

export type RuleDisplayStatus =
  | 'passed'
  | 'failed'
  | 'not_detected'
  | 'not_applicable'
  | 'coverage_gap'
  | 'not_executed'
  | 'detect_error'
  | 'evidence_insufficient'
  | 'manual_review'
  | 'running'
  | 'pending'

export type PageNodeStatus =
  | 'pending'
  | 'running'
  | 'visiting'
  | 'recognized'
  | 'failed'
  | 'skipped'

export type PageInspectionStatus =
  | 'captured'
  | 'detecting'
  | 'matching'
  | 'evaluating'
  | 'completed'
  | 'degraded'
  | 'failed'

export interface PageExplorationResult {
  page_capture_id: string | null
  parent_page_capture_id: string | null
  supersedes_page_capture_id: string | null
  page_seq: number | null
  page_type: string | null
  page_url: string | null
  page_title: string | null
  captured_at: string | null
  detection_status: string | null
  page_state?: string
  artifact_ids?: string[]
  lifecycle_status:
    | 'active'
    | 'superseded'
    | 'abandoned'
    | 'evidence_only'
    | 'unknown'
  inspection_status: PageInspectionStatus | 'unknown'
  failure_stage: string | null
  failure_code: string | null
  failure_message: string | null
  failure_retryable: boolean | null
  abandon_reason: string | null
}

export type PageTypeAssessmentStatus =
  | 'covered'
  | 'requires_auth'
  | 'blocked_by_safety'
  | 'not_found'
  | 'evidence_insufficient'
  | 'not_applicable'

export type H5JsonValue = string | number | boolean | null | H5JsonValue[] | { [key: string]: H5JsonValue }

export interface H5FrontierCandidateProjection {
  protocol_version: string
  candidate_id: string
  semantic_target: string
  action_type: string
  risk_level: string
  exploration_value: number
  navigation_depth: number
  expected_surface_kind: string | null
  prior_failure_count: number
  metadata: { [key: string]: H5JsonValue }
}

export interface H5FrontierEntryProjection {
  entry_id: string
  candidate: H5FrontierCandidateProjection
  source_capture_id: string
  source_state_version: string
  source_location: string
  navigation_depth: number
  parent_state_version: string | null
  lineage: string[]
  created_at: string
  status: 'pending' | 'selected' | 'executed' | 'blocked' | 'failed'
  restoration_attempt_count: number
  last_failure_reason: string | null
  selected_at: string | null
  finished_at: string | null
}

export interface H5FrontierProjection {
  schema_version: string
  snapshot_id: string
  entries: H5FrontierEntryProjection[]
  active_state_version: string | null
  visited_state_ids: string[]
  executed_candidate_ids: string[]
  action_fingerprints: string[]
  covered_page_count: number
  covered_page_types: string[]
  max_pages: number | null
  pending_count: number
  blocked_count: number
  failed_count: number
  sampling_policy_summary: { [key: string]: H5JsonValue }
  payload_hash: string
}

export interface H5AssessmentProjection {
  assessment_id: string
  page_capture_id: string | null
  page_capture_ids: string[]
  page_type: string
  page_state: string | null
  status: PageTypeAssessmentStatus
  reason_codes: string[]
  reason_code: string
  catalog_sha256?: string
  evidence_artifact_ids?: string[]
  exploration_action_ids?: string[]
}

export interface H5EvidenceInventoryItemProjection {
  inventory_item_id: string
  session_id: string
  action_execution_id: string
  before_capture_id: string | null
  after_capture_id: string | null
  page_type: string | null
  page_state: string | null
  status: 'pass' | 'insufficient' | 'failed'
  reason_codes: string[]
  required_artifacts: string[]
  found_artifacts: string[]
  verification_summary: { [key: string]: H5JsonValue }
  before_manifest_id?: string | null
  before_manifest_hash?: string | null
  after_manifest_id?: string | null
  after_manifest_hash?: string | null
  authority_source?: 'pass_manifest' | 'manifest_unverified' | 'legacy_object_verified'
  published_inspection_count: number
  schema_version: string
  payload_hash: string
  row_version: number
  verified_at: string | null
  created_at: string
  updated_at: string
}

export interface H5CoverageProjection {
  finalization_epoch?: number
  revision: number | null
  authority_source: 'h5_page_type_assessment' | 'legacy_frontier_derived'
  required_page_types: string[]
  covered_page_types: string[]
  missing_page_types: string[]
  scopes: Array<{ [key: string]: H5JsonValue }>
  assessment_scope_ready: boolean
  frontier_terminal: boolean
  completion_ready: boolean
  schema_version: string
  payload_hash: string | null
}

export interface H5EvidenceInventoryProjection {
  item_count: number
  pass_count: number
  insufficient_count: number
  failed_count: number
  items: H5EvidenceInventoryItemProjection[]
}

export type H5PublicationStage =
  | 'not_started'
  | 'publication_persisted'
  | 'wakeup_delivered'
  | 'work_claimed'
  | 'inspection_completed'

export interface H5InspectionPublicationProjectionItem {
  publication_id: string
  mode: 'shadow' | 'enforced'
  status: string
  current_stage: Exclude<H5PublicationStage, 'not_started'>
  page_capture_id: string
  page_type: string | null
  row_version: number
  attempt_count: number
  max_attempts: number
  candidate_rule_count: number | null
  expected_batch_count: number | null
  published_work_item_count: number
  excluded_active_session_rule_count: number
  outbox_count: number
  outbox_published_count: number
  outbox_pending_count: number
  outbox_dead_letter_count: number
  inbox_count: number
  inbox_completed_count: number
  inbox_pending_count: number
  inbox_dead_letter_count: number
  linked_work_count: number
  claimed_work_count: number
  terminal_work_count: number
  failed_work_count: number
  inspection_attempt_count: number
  failure_code: string | null
  failure_message: string | null
  next_attempt_at: string | null
  created_at: string
  updated_at: string
  finished_at: string | null
}

export interface H5InspectionPublicationProjection {
  current_stage: H5PublicationStage
  publication_count: number
  status_counts: Record<string, number>
  stage_counts: Record<Exclude<H5PublicationStage, 'not_started'>, number>
  publication_persisted_count: number
  wakeup_delivered_count: number
  work_claimed_count: number
  inspection_completed_count: number
  outbox_pending_count: number
  outbox_dead_letter_count: number
  inbox_pending_count: number
  inbox_dead_letter_count: number
  failed_count: number
  items: H5InspectionPublicationProjectionItem[]
}

export interface H5FullInspectionClosure {
  inspection_count: number
  nonterminal_inspection_count: number
  failed_inspection_count: number
  gate_snapshot_id: string | null
  gate_passed: boolean | null
  gate_block_reasons: string[]
  evaluated_at?: string | null
  publication_projection: H5InspectionPublicationProjection
}

export interface PageCoverageGap {
  page_type: string
  status: PageTypeAssessmentStatus
  reason_code: string
  page_capture_ids: string[]
  evidence_artifact_ids: string[]
  exploration_action_ids: string[]
}

export interface PageCoverageSummary {
  catalog_observation: {
    catalog_page_types: string[]
    catalog_page_type_total: number
    observed_page_types: string[]
    observed_page_type_count: number
    business_observed_page_types: string[]
    missing_catalog_page_types: string[]
    raw_coverage_rate: number
  }
  lifecycle_shells: {
    observed_page_types: string[]
    strict_line_loading_observed: boolean
    startup_ad_observed: boolean
    loading_state_observed: boolean
  }
  product_business: {
    assessment_available: boolean
    assessment_scope_complete: boolean
    assessment_scope_verified: boolean
    assessment_complete: boolean
    missing_assessment_page_types: string[]
    applicable_page_types: string[] | null
    covered_page_types: string[]
    known_uncovered_page_types: string[]
    unresolved_page_types: string[]
    not_applicable_page_types: string[]
    covered_count: number
    applicable_count: number | null
    coverage_rate: number | null
  }
  exploration: {
    action_total: number
    terminal_count: number
    pending_count: number
    explored_count: number
    blocked_count: number
    skipped_count: number
    failed_count: number
    terminal_rate: number | null
    explored_rate: number | null
  }
  gaps: PageCoverageGap[]
  acceptance_complete: boolean | null
}

export interface PopupWallFinding {
  kind?: 'popup_layer_limit_exceeded' | 'popup_dismissal_stalled'
  closed_count: number
  limit: number
  note: string
}

export interface WebPageApplicabilitySnapshot {
  required_page_types: string[]
  optional_page_types: string[]
  not_applicable_page_types: string[]
  applicability_mode: string
  catalog_version: string
  catalog_hash: string
  resolution_reason?: string
  snapshot_protocol_version?: string
}

export interface WebCoverageReportSnapshot {
  applicability_snapshot?: WebPageApplicabilitySnapshot
  [key: string]: unknown
}

export interface PageExplorationSummary {
  web_report_explanation?: import('./web-report-explanation').WebReportExplanation | null
  web_coverage?: WebCoverageReportSnapshot
  popup_wall?: PopupWallFinding
  tracking_available: boolean
  complete: boolean | null
  quality_status: 'unknown' | 'in_progress' | 'complete' | 'needs_review' | 'technical_failed'
  requires_review: boolean
  page_total: number
  active_page_count: number
  terminal_page_count: number
  completed_page_count: number
  degraded_page_count: number
  failed_page_count: number
  pending_page_count: number
  superseded_page_count: number
  abandoned_page_count: number
  evidence_only_page_count: number
  page_status_counts: Record<string, number>
  page_lifecycle_counts: Record<string, number>
  action_total: number
  pending_action_count: number
  explored_action_count: number
  inactive_action_count: number
  failed_action_count: number
  blocked_action_count: number
  skipped_action_count: number
  action_status_counts: Record<string, number>
  pending_work_count: number
  coverage: PageCoverageSummary
  page_results: PageExplorationResult[]
}

export interface ExplorationActionItem {
  exploration_action_id: string
  source_page_capture_id: string | null
  source_page_type: string | null
  action: string
  origin: 'entry' | 'discovered' | 'recovery'
  target: string | null
  target_keywords: string[]
  risk_level: string | null
  purpose: string | null
  expected_next_page_type: string | null
  navigation_depth: number | null
  status: 'candidate' | 'selected' | 'explored' | 'blocked' | 'skipped' | 'failed'
  result_page_capture_id: string | null
  failure_reason: string | null
  failure_code: string | null
  failure_message: string | null
  failure_retryable: boolean | null
  attempt_count: number
  created_at: string
  updated_at: string
}

export interface PageAttentionItem {
  execution_id: string | null
  rule_code: string
  rule_name: string
  display_status: RuleDisplayStatus
  display_status_label: string
  result_summary: string | null
  problem_desc: string | null
  review_reason: string | null
  error_message: string | null
  diagnostic_code: string | null
  diagnostic_message: string
}

export interface PageTimelineNode {
  node_seq: number
  source: 'journey' | 'discovered'
  page_role?: 'home' | 'menu' | 'detail' | null
  step_name: string | null
  page_type: string | null
  page_type_name: string | null
  page_state: string | null
  status: PageNodeStatus
  status_label: string
  is_current: boolean
  page_capture_id: string | null
  logical_page_id?: string | null
  canonical_url?: string | null
  capture_ids?: string[]
  capture_count?: number
  primary_capture_id?: string | null
  classification_status?: 'classified' | 'needs_review' | null
  raw_page_type?: string | null
  proposed_page_type?: string | null
  observed_page_type?: string | null
  expected_page_type?: string | null
  conflict_reason_code?: string | null
  diagnostic_message?: string | null
  classification_confidence?: number | null
  classification_review_reason?: string | null
  capture_versions?: Array<{
    page_capture_id: string | null
    capture_purpose: string | null
    captured_at: string | null
    page_type: string | null
    inspection_status: string | null
    screenshot_artifact_id: string | null
  }>
  url: string | null
  screenshot_artifact_id: string | null
  detection: {
    status: string | null
    confidence: number | null
    confidence_source: string | null
    model_page_type: string | null
    model_confidence: number | null
    duration_ms: number | null
  }
  inspection: {
    lifecycle_status: 'active' | 'superseded' | 'abandoned' | 'evidence_only' | null
    status: PageInspectionStatus | null
    status_label: string | null
    failure_stage: string | null
    failure_code: string | null
    failure_message: string | null
    failure_retryable: boolean | null
    abandon_reason: string | null
  }
  parent_page_capture_id: string | null
  element_count: number | null
  rule_progress: {
    executed: number
    total: number | null
  }
  result_rollup: RuleDisplayStatus | null
  result_rollup_label: string | null
  result_counts: Record<string, number>
  attention_items: PageAttentionItem[]
  current_rule_code: string | null
  first_visited_at: string | null
  captured_at: string | null
  finished_at: string | null
}

export interface PageTimeline {
  web_report_explanation?: import('./web-report-explanation').WebReportExplanation | null
  task_id: string
  task_status: string
  run_id: string | null
  journey_steps: string[]
  current_node_seq: number | null
  exploration_summary: PageExplorationSummary
  exploration_actions: ExplorationActionItem[]
  nodes: PageTimelineNode[]
  frontier?: H5FrontierProjection
  h5_assessments?: H5AssessmentProjection[]
  h5_evidence_inventory?: H5EvidenceInventoryProjection
  h5_coverage?: H5CoverageProjection
  terminal_reason?: string | null
  execution_mode?: 'full_inspection' | 'exploration_only'
  full_inspection_closure?: H5FullInspectionClosure
}

/** 绿色共享判定的白名单分项；截图引用与实际判据分别保留。 */
export interface GreenRuleSubitemResult {
  key: string
  status: string
  evaluated: boolean
  confidence: number | null
  reason: string | null
  missing_evidence: string[]
  evidence_artifact_ids: string[]
  screenshot_artifact_ids: string[]
  evidence_notice?: string | null
  capture_ids: string[]
}

/** 仅已核验关联的第4版绿色结果用于分项展示，不消费原始 judge_output。 */
export interface GreenRuleResultProjection {
  schema_version: 'green-rule-result.v1'
  rule_key: '102' | 's72' | '41' | '62' | 's35' | 's49' | 's73' | '32'
  rule_contract_hash: string | null
  linkage_status: string
  linkage_notice: string | null
  execution_status: string | null
  review_status: string | null
  review_reason: string | null
  missing_evidence: string[]
  subitems: GreenRuleSubitemResult[]
}

export interface RuleResultItem {
  green_result?: GreenRuleResultProjection | null
  inventory_source?: string | null
  inventory_coverage_reason?: string | null
  item_type: 'execution' | 'candidate'
  execution_id: string | null
  rule_id: string
  rule_code: string
  rule_name: string
  category: string | null
  rule_package: string
  trigger_display: string
  bound_page_type: string | null
  bound_page_type_name: string | null
  applicable_page_types: string[]
  display_status: RuleDisplayStatus
  display_status_label: string
  machine_display_status: RuleDisplayStatus
  machine_display_status_label: string
  execution_status: string | null
  judge_strategy: string | null
  confidence: number | null
  severity: string | null
  severity_display: string | null
  result_summary: string | null
  problem_desc: string | null
  review_status: string | null
  human_verdict: 'pass' | 'fail' | 'ignored' | null
  human_verdict_label: string | null
  human_verdict_note: string | null
  human_verdict_at: string | null
  confirmable: boolean
  confirm_status: 'pending' | 'confirmed' | null
  evidence_artifact_ids: string[]
  page_capture_id: string | null
  is_bootstrap: boolean
  finished_at: string | null
}

export interface RuleResults {
  task_id: string
  task_status: string
  run_id: string | null
  total: number
  status_counts: Partial<Record<RuleDisplayStatus, number>>
  items: RuleResultItem[]
}

export interface RecentAnomaly {
  rule_code: string
  rule_name: string
  display_status: RuleDisplayStatus
  display_status_label: string
  severity_display: string | null
  bound_page_type: string | null
  bound_page_type_name: string | null
  problem_desc: string | null
  occurred_at: string | null
}

export interface TaskStats {
  task_id: string
  task_status: string
  total: number
  triggered: number
  /** 已有真实业务结论或确认不适用的规则数量，技术缺口不计入。 */
  finished: number
  frontier_pending: number
  progress_percent: number
  status_counts: Partial<Record<RuleDisplayStatus, number>>
  status_labels: Record<RuleDisplayStatus, string>
  recent_anomalies: RecentAnomaly[]
}

export interface TaskLogItem {
  id: number
  ts: string
  level: 'INFO' | 'WARN' | 'ERROR' | 'EXEC'
  source: string
  message: string
  page_capture_id: string | null
  rule_execution_id: string | null
  context: Record<string, unknown>
}

export interface TaskLogs {
  task_id: string
  total: number
  page: number
  page_size: number
  items: TaskLogItem[]
}

export interface ScreenshotRelatedRule {
  rule_code: string
  rule_name: string
  display_status: RuleDisplayStatus
  display_status_label: string
}

/** 采集元数据里持久化的展示层评估（工作台只取判断字段）。 */
export interface CapturePresentation {
  protocol_version?: string
  topmost_layer_kind?: string | null
  base_page_visibility?: string | null
  blocking_scope?: string | null
  mutation_mode?: string | null
  overlay_semantic_kind?: string | null
  hit_test_status?: string | null
  diagnostic_operations?: Array<{ kind: string; source: string }>
  uncertainty?: string[]
}

/** 采集管线的页面稳定性判定。 */
export interface CaptureSettle {
  outcome: string | null
  blocking_reasons: string[]
  dynamic_reasons: string[]
}

export interface ScreenshotRelatedFrame {
  artifact_id: string
  page_capture_id: string | null
  captured_at: string | null
  capture_role?: string | null
  page_state?: string | null
  duplicate_count: number
}

export interface ScreenshotItem {
  artifact_id: string
  page_capture_id: string | null
  captured_at: string | null
  content_type: string | null
  size_bytes: number | null
  duplicate_count: number
  related_rules: ScreenshotRelatedRule[]
  capture_role?: string | null
  capture_purpose?: string | null
  gallery_visible?: boolean
  duplicate_of_artifact_id?: string | null
  page_state?: string | null
  occlusion_kind?: 'startup' | 'overlay' | 'stable' | string | null
  related_frames?: ScreenshotRelatedFrame[]
  presentation?: CapturePresentation | null
  settle?: CaptureSettle | null
}

export interface ScreenshotGroup {
  page_group_key: string
  page_title: string | null
  page_display_name: string
  page_type: string | null
  page_type_name: string
  total: number
  items: ScreenshotItem[]
}

export interface TaskScreenshots {
  task_id: string
  run_id: string | null
  groups: ScreenshotGroup[]
}

export interface AppStageBootstrap {
  session_id?: string
  stage?: string
  installed_package?: string | null
  install_attempt?: number | null
  open_attempt?: number | null
  error_code?: string | null
  error_message?: string | null
  ready_at?: string | null
  updated_at?: string | null
}

export interface AppStageGate {
  gate_passed?: boolean
  block_reasons?: string[]
  require_nonempty_candidate_inventory?: boolean
  no_rules_applicable?: boolean
  candidate_inventory_satisfied?: boolean
  app_coverage_satisfied?: boolean
  evaluated_at?: string | null
}

export type AppExplorationStatus =
  | 'created'
  | 'bootstrapping'
  | 'surface_resolving'
  | 'app_ready'
  | 'exploring'
  | 'final_observing'
  | 'evidence_validating'
  | 'evidence_sealing'
  | 'session_draining'
  | 'exploration_completed'
  | 'evidence_insufficient'
  | 'technical_failed'
  | 'close_failed'

export interface AppBusinessPageProjection {
  protocol_version: string
  page_group_key: string
  page_display_name: string
  page_name: string | null
  page_name_source: string | null
  page_name_confidence: number | null
  page_type: string
  capture_count: number
  first_capture_id: string
  last_capture_id: string
}

export interface AppExplorationProjection {
  status: AppExplorationStatus
  surface_phase: string | null
  current_page_identity: string | null
  completed_work_count: number
  pending_work_count: number
  action_count: number
  reobservation_count: number
  coverage_summary: Record<string, unknown>
  terminal_reason_code: string | null
  outcome: string | null
  evidence_index_id: string | null
  evidence_index_hash: string | null
  evidence_gap_count: number
  surface_session_status: string | null
  started_at: string | null
  completed_at: string | null
  evidence_preserved?: boolean
  recovery_state?: 'pending' | 'evidence_sealed' | 'no_evidence' | 'reconciled' | null
  technical_failure?: {
    phase: string
    reason_code: string
    error_type?: string | null
    retryable?: boolean
    observed_at?: string | null
    recovery_state: 'pending' | 'evidence_sealed' | 'no_evidence' | 'reconciled'
    reconciled_at?: string | null
  } | null
}

export type AppPublicationStage = 'not_started' | 'sealed' | 'delivered' | 'claimed' | 'judged'

export interface AppInspectionPublicationEntryProjection {
  entry_id: number
  sequence_no: number
  capture_id: string
  evidence_manifest_id: string
  semantic_role: string
  page_type: string | null
  page_state: string
  status: 'pending' | 'linked' | 'not_applicable' | 'needs_review' | string
  current_stage: Exclude<AppPublicationStage, 'not_started' | 'delivered'>
  candidate_rule_count: number | null
  linked_work_item_count: number
  explicit_exclusion_count: number
  claimed_work_count: number
  terminal_work_count: number
  failure_code: string | null
  failure_message: string | null
}

export interface AppInspectionPublicationProjectionItem {
  publication_id: string
  status: string
  current_stage: Exclude<AppPublicationStage, 'not_started'>
  evidence_index_id: string
  evidence_index_hash: string
  row_version: number
  attempt_count: number
  max_attempts: number
  expected_entry_count: number
  terminal_entry_count: number
  expected_rule_count: number | null
  linked_work_item_count: number
  explicit_exclusion_count: number
  outbox_pending_count: number
  outbox_dead_letter_count: number
  inbox_pending_count: number
  inbox_dead_letter_count: number
  failure_code: string | null
  failure_message: string | null
  entries: AppInspectionPublicationEntryProjection[]
}

export interface AppInspectionPublicationProjection {
  current_stage: AppPublicationStage
  publication_count: number
  status_counts: Record<string, number>
  stage_counts: Record<Exclude<AppPublicationStage, 'not_started'>, number>
  sealed_count: number
  delivered_count: number
  claimed_count: number
  judged_count: number
  outbox_pending_count: number
  outbox_dead_letter_count: number
  inbox_pending_count: number
  inbox_dead_letter_count: number
  needs_review_count: number
  failed_count: number
  items: AppInspectionPublicationProjectionItem[]
}

export interface AppStageProjection {
  applicable: boolean
  task_id: string
  task_status?: string
  bootstrap?: AppStageBootstrap | null
  exploration_work?: Record<string, number>
  app_exploration?: AppExplorationProjection | null
  gate?: AppStageGate | null
  inspection_summary?: {
    work_item_count?: number
    enforced_count?: number
    nonterminal_count?: number
    failed_count?: number
  }
  publication_projection?: AppInspectionPublicationProjection
}

export interface InspectionWorkItemRule {
  rule_id: string
  rule_code: string
  ordinal: number
  contract_hash: string
  status: string
  observation_packet_id: string | null
  rule_execution_id: string | null
  last_error_code: string | null
  last_error_message: string | null
  updated_at: string | null
}

export interface InspectionAttempt {
  attempt_id: string
  consumer_agent_run_id: string | null
  attempt_no: number
  status: string
  input_hash: string
  has_result_payload: boolean
  error_code: string | null
  error_message: string | null
  started_at: string | null
  judged_at: string | null
  finished_at: string | null
  updated_at: string | null
}

export interface InspectionWorkItem {
  work_item_id: string
  protocol_version: number
  control_mode: 'enforced' | 'shadow' | string
  status: string
  page_capture_id: string | null
  producer_agent_run_id: string | null
  consumer_agent_run_id: string | null
  session_owner_agent_run_id: string | null
  batch_key: string
  strategy: string
  category: string | null
  session_dependency: string
  input_hash: string
  priority: number
  attempt_count: number
  max_attempts: number
  lease_owner: string | null
  lease_expires_at: string | null
  next_attempt_at: string | null
  last_error_code: string | null
  last_error_message: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  updated_at: string
  rules: InspectionWorkItemRule[]
  attempts: InspectionAttempt[]
}

export interface InspectionWorkItemSummary {
  work_item_count: number
  enforced_count: number
  shadow_count: number
  nonterminal_count: number
  failed_count: number
  active_session_failed_count: number
  status_counts: Record<string, number>
  rule_status_counts: Record<string, number>
  attempt_status_counts: Record<string, number>
}

export interface InspectionWorkItems {
  task_id: string
  task_status: string
  run_id: string | null
  control_mode: string | null
  summary: InspectionWorkItemSummary
  items: InspectionWorkItem[]
}

/** 工作台只读页面状态图（对应 /workstation/tasks/{id}/page-graph）。 */
export interface PageGraphStateProjection {
  state_id?: string | null
  logical_page_key?: string | null
  page_type?: string | null
  label?: string | null
  identity_status?: string | null
  counts_as_business_page?: boolean | null
  is_overlay?: boolean | null
  is_external?: boolean | null
}

export interface PageGraphTransitionProjection {
  transition_id?: string | null
  source_state_id?: string | null
  target_state_id?: string | null
  status?: string | null
  proof?: string | null
  counts_as_business_arrival?: boolean | null
}

export interface PageGraphTreeNode {
  state_id?: string | null
  logical_page_key?: string | null
  page_type?: string | null
  label?: string | null
  children?: PageGraphTreeNode[]
}

export interface PageGraphProjection {
  status?: string | null
  reason?: string | null
  task_id?: string | null
  agent_run_id?: string | null
  finalization_epoch?: number | null
  states?: PageGraphStateProjection[]
  transitions?: PageGraphTransitionProjection[]
  exploration_tree?: {
    roots?: PageGraphTreeNode[]
  } | null
}

// ---- 探索过程展示：页面状态图 / 只读恢复路线 / 证据缺口台账 ----------------------

export interface PageGraphState {
  state_id: string
  task_id?: string
  agent_run_id?: string | null
  surface_kind?: string | null
  surface_session_id?: string | null
  finalization_epoch?: number | null
  command_sequence?: number | null
  capture_role?: string | null
  logical_page_key: string
  identity_status: string
  identity_source?: string | null
  page_type: string | null
  page_state: string | null
  state_fingerprint?: string | null
  is_overlay: boolean
  is_external: boolean
  counts_as_business_page: boolean
  state_payload?: Record<string, unknown> | string | null
  captured_at?: string | null
}

export interface PageGraphTransition {
  transition_id: string
  action_execution_id?: string | null
  source_state_id: string | null
  target_state_id: string | null
  action_kind: string | null
  semantic_target: string | null
  risk_level: string | null
  status: string
  terminal?: boolean
  proof: string | null
  counts_as_business_arrival: boolean
  replay_eligible: boolean
  reason_codes: string[]
  outcome_payload?: Record<string, unknown> | string | null
}

export interface ExplorationTreeStateVersion {
  state_fingerprint: string
  capture_ids: string[]
  latest_capture_id: string | null
  status: string
  reason_codes: string[]
  evidence_status?: string
  inspection_status?: string
}

export interface ExplorationTreeOverlay {
  capture_id: string
  page_state: string | null
  state_payload?: Record<string, unknown> | string | null
}

export interface ExplorationTreeTarget {
  candidate_id?: string
  source_capture_id?: string | null
  semantic_target?: string | null
  status?: string | null
  reason_code?: string | null
  progress_status?: string | null
}

export interface ExplorationTreeNode {
  page_key: string
  parent_page_key: string | null
  children: string[]
  states: ExplorationTreeStateVersion[]
  overlays: ExplorationTreeOverlay[]
  targets: ExplorationTreeTarget[]
  transition_ids: string[]
  status: string
}

export interface ExplorationTree {
  protocol_version: string
  source?: string
  roots: string[]
  nodes: ExplorationTreeNode[]
  cross_transition_ids: string[]
  unattached_overlays: ExplorationTreeOverlay[]
  unknown_observation_ids: string[]
  unassigned_targets: ExplorationTreeTarget[]
  coverage_authority_changed?: boolean
}

export interface PageGraph {
  protocol_version: string
  status: string
  reason_code?: string | null
  task_id: string
  agent_run_id?: string | null
  finalization_epoch?: number | null
  source?: string
  counts?: Record<string, number>
  states: PageGraphState[]
  transitions: PageGraphTransition[]
  progress_source?: string
  recovery_policy?: Record<string, unknown>
  exploration_tree?: ExplorationTree | null
}

export interface RecoveryRoute {
  source_state_id: string
  target_state_id: string
  target_page_key: string
  steps: PageGraphTransition[]
}

export interface RecoveryRoutes {
  protocol_version: string
  task_id?: string
  agent_run_id?: string | null
  finalization_epoch?: number | null
  status: string
  reason_code?: string | null
  graph_status?: string | null
  routes: RecoveryRoute[]
  requires_stepwise_reverification?: boolean
  route_policy?: string
}

export interface EvidenceGapMissing {
  page_states: string[]
  presentation: { code: string; reason: string } | null
  capture_roles: string[]
  artifact_types: string[]
  judge_statements: string[]
  quality_gate_sources: string[]
}

export interface EvidenceGapTarget {
  region: { kind: string; label: string; layer_target: string }
  page_url: string | null
  page_type: string | null
  page_title: string | null
  surface_kind: string | null
  page_capture_id: string
  observed_page_state: string | null
  required_page_states: string[]
}

export interface EvidenceGap {
  gap_id: string
  protocol_version?: string
  task_id?: string
  rule_id?: string
  rule_code: string
  rule_name: string
  category: string
  target: EvidenceGapTarget
  missing: EvidenceGapMissing
  disposition: string
  reason_code: string
  evidence_goal: string
  overlay_phase: string
  recapture_policy: string
  recapture_fallback?: string | null
  requested_request?: Record<string, unknown> | null
  diagnostics?: Record<string, unknown>
}

export interface EvidenceGapRoutePlan {
  state: string
  request_status?: string
  reason_code?: string
  reason_label?: string
  advice?: string
  blocked_action_kinds?: string[]
}

export interface EvidenceGapItem {
  execution_id: string | null
  rule_id: string | null
  rule_code: string
  rule_name: string | null
  category: string | null
  severity: string | null
  execution_status: string | null
  page_capture_id: string | null
  logical_page_key: string | null
  state_fingerprint: string | null
  created_at: string | null
  gap: EvidenceGap
  route_plan?: EvidenceGapRoutePlan
}

export interface EvidenceGaps {
  protocol_version: string
  task_id: string
  task_status: string
  run_id: string | null
  total: number
  phase_counts: Record<string, number>
  policy_counts: Record<string, number>
  disposition_counts: Record<string, number>
  route_plan_counts?: Record<string, number>
  route_blocked_reasons?: Record<string, number>
  execution_policy: string
  items: EvidenceGapItem[]
}

/** 规则结论复用：一条沿用了别处结论的规则判定，带来源、依据与失效条件（只读）。 */
export interface RuleReuseInvalidation {
  code: string
  label: string
}

export interface RuleReuseSource {
  execution_id: string | null
  page_capture_id: string | null
  page_type: string
  page_url: string
  execution_status: string | null
}

export interface RuleReuseItem {
  execution_id: string | null
  rule_id: string | null
  rule_code: string
  rule_name: string | null
  category: string | null
  severity: string | null
  execution_status: string | null
  result_summary: string | null
  page_capture_id: string | null
  page_type: string
  page_url: string
  scope: string
  scope_label: string
  reuse_key: string
  reason: string
  invalidate_on: RuleReuseInvalidation[]
  basis: Record<string, unknown>
  source: RuleReuseSource
  created_at: string | null
}

export interface RuleReuse {
  protocol_version: string
  task_id: string
  task_status: string
  run_id: string | null
  total: number
  scope_counts: Record<string, number>
  status_counts: Record<string, number>
  presentation: string
  items: RuleReuseItem[]
}

/** 页面类型判断记录：每页各来源的判断、按记录算出的裁定，以及与正式识别的差异（只读）。 */
export interface PageTypeJudgementRecord {
  judgement_id: string
  sequence_no: number
  source: string
  source_label: string
  page_type: string | null
  page_state: string
  confidence: number | null
  kind: string
  revises_judgement_id: string | null
  basis: Record<string, unknown>
  arrived_after_seal: boolean
  created_at: string | null
}

export interface PageTypeRevisionRecord {
  source: string
  source_label: string
  judgement_id: string
  from_page_type: string | null
  to_page_type: string | null
  sequence_no: number
}

export interface PageTypeResolution {
  status: string
  status_label: string
  page_type: string | null
  reason: string
  current_by_source: Record<string, string | null>
  conflicting_page_types: string[]
  revisions: PageTypeRevisionRecord[]
  adopted_judgement_id: string | null
  judgement_count: number
  after_seal_count: number
}

export interface PageTypeJudgementPage {
  page_capture_id: string
  page_seq: number | null
  page_slug: string | null
  page_url: string
  logical_page_key: string | null
  formal_page_type: string | null
  resolution: PageTypeResolution
  differs_from_formal: boolean
  judgements: PageTypeJudgementRecord[]
}

export interface PageTypeJudgements {
  protocol_version: string
  task_id: string
  task_status: string
  run_id: string | null
  total: number
  status_counts: Record<string, number>
  conflicting_count: number
  revised_count: number
  differs_count: number
  resolution_policy: string
  items: PageTypeJudgementPage[]
}

/** 任务收口结论：页面探索、证据充分、规则结果、最终完成检查各自一个结果（只读）。 */
export interface ClosureBlocker {
  code: string
  label: string
  advice: string
  waiting_for_people: boolean
}

export interface ClosureDimension {
  key: string
  title: string
  state: string
  state_label: string
  summary: string
  metrics: Record<string, unknown>
  blockers: ClosureBlocker[]
}

export interface ClosureOutcome {
  protocol_version: string
  task_id: string
  task_status: string
  run_id: string | null
  summary: string
  supplement_request_counts: Record<string, number>
  dimensions: ClosureDimension[]
}
