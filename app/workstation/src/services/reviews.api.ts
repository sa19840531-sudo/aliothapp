import { requestJson } from './http'
import type { ReviewQueueItem, ReviewStatus, ReviewTriggerReason } from '@/types/review'

export interface WorkstationReviewListParams {
  status?: ReviewStatus
  trigger_reason?: ReviewTriggerReason
  page?: number
  page_size?: number
}

export interface WorkstationReviewListResponse {
  items: ReviewQueueItem[]
  total: number
  page: number
  page_size: number
}

export interface WorkstationReviewActionResponse {
  review_id: string
  status: ReviewStatus | string
  message: string
}

export function listReviews(params: WorkstationReviewListParams = {}): Promise<WorkstationReviewListResponse> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') {
      query.set(key, String(value))
    }
  })
  const queryString = query.toString()
  return requestJson<WorkstationReviewListResponse>(
    queryString ? `/workstation/reviews?${queryString}` : '/workstation/reviews',
  )
}

export function performReviewAction(
  reviewId: string,
  action: 'approve' | 'reject' | 'recheck',
  reason?: string,
): Promise<WorkstationReviewActionResponse> {
  return requestJson<WorkstationReviewActionResponse>(`/workstation/reviews/${reviewId}/${action}`, {
    method: 'POST',
    body: JSON.stringify({ reason: reason ?? null }),
  })
}

export function listReviewCandidates(): Promise<WorkstationReviewListResponse> {
  return listReviews({ status: 'pending' })
}
