export interface WebMaterialReference {
  capture_id?: string
  capture_role?: string
  captured_at?: string
  sequence_id?: string
  purpose?: string
  readiness_status?: string | null
  readiness_exit_reason?: string | null
  page_state?: string | null
  rule_id?: string
  rule_code?: string
  work_item_id?: string
  work_status?: string
  attempt_id?: string
  evidence_input_set_id?: string
  contract_hash?: string
  observation_packet_id?: string
  status?: string
  execution_status?: string
  canonical_status?: string
  classification_status?: string
  reason_code?: string
  error_code?: string
  material_role?: 'stable' | 'sequence' | 'unknown'
  menu_entry_id?: string
  parent_capture_id?: string
  direct_parent_capture_id?: string
  source_capture_id?: string
  before_capture_id?: string
  after_capture_id?: string
  action_execution_id?: string
  stage?: string
  scope?: string
  predicate?: string
  account_state?: string
  frames?: WebMaterialReference[]
  attempts?: WebMaterialReference[]
  sources?: WebMaterialReference[]
  notification?: WebMaterialReference
  selection?: WebMaterialReference
  canonical?: WebMaterialReference
  capability_revision?: WebMaterialReference
  primary_content_capture?: WebMaterialReference
}

export interface WebReportExplanation {
  schema_version: string
  task_id: string
  agent_run_id: string
  finalization_epoch: number
  notifications: WebMaterialReference[]
  sequences: WebMaterialReference[]
  publication_rules: WebMaterialReference[]
  rule_materials: WebMaterialReference[]
  primary_content?: WebMaterialReference[]
  page_type_summary?: {
    business_page_types: string[]
    lifecycle_page_types: string[]
    unknown_pages: string[]
    ad_page_count: number
  }
  web_logical_pages?: WebLogicalEvidencePage[]
  web_ad_pages?: WebAdEvidencePage[]
  web_identity_diagnostics?: Array<{ capture_id: string; reason_code: string }>
}

export interface WebFullPageEvidence {
  status: 'captured' | 'failed' | 'not_attempted' | string
  capture_id?: string | null
  screenshot_artifact_id?: string | null
  reason_code?: string | null
}

export interface WebLogicalEvidencePage {
  logical_page_id: string
  canonical_url?: string
  primary_capture_id: string
  page_type?: string | null
  coverage_group?: 'business' | 'lifecycle' | 'neither' | string
  classification_status?: string | null
  screenshot_artifact_id?: string | null
  supplemental_full_page?: WebFullPageEvidence | null
}

export interface WebAdEvidencePage {
  capture_id: string
  parent_capture_id?: string | null
  capture_purpose: 'web_ad_page'
  page_type: 'ad_page'
  coverage_group: 'evidence_only'
  requested_url?: string | null
  final_url?: string | null
  screenshot_artifact_id?: string | null
  artifact_ids?: string[]
}
