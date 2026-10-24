export type ReviewTriggerReason =
  | 'low_confidence'
  | 'high_risk'
  | 'evidence_insufficient'
  | 'loop_budget_exceeded'
  | 'no_progress'
  | 'manual_flag'

export type ReviewStatus = 'pending' | 'approved' | 'rejected' | 'needs_recheck'

export interface ReviewQueueItem {
  review_id: string
  execution_id: string
  task_id: string
  task_name: string
  rule_name: string
  trigger_reason: ReviewTriggerReason | string
  status: ReviewStatus | string
  reviewer: string | null
  review_reason: string | null
  created_at: string | null
}
