import { requestJson } from './http'

/** 反馈工单（/workstation/feedback）。 */

export type FeedbackKind = 'false_positive' | 'missed' | 'rule_suggestion' | 'platform_issue' | 'other'
export type FeedbackPriority = 'urgent' | 'high' | 'medium' | 'low'
export type FeedbackStatus = 'pending' | 'processing' | 'resolved' | 'closed'

export interface FeedbackTicket {
  id: string
  ticket_no: string
  tenant_id: string
  tenant_name: string | null
  submitter_id: string
  submitter_name: string
  kind: FeedbackKind
  priority: FeedbackPriority
  title: string
  description: string
  task_id: string | null
  rule_code: string | null
  contact: string | null
  status: FeedbackStatus
  reply: string | null
  handled_by: string | null
  handled_at: string | null
  created_at: string
  updated_at: string
}

export interface FeedbackListResponse {
  items: FeedbackTicket[]
  total: number
  page: number
  page_size: number
  can_handle: boolean
  visibility: 'all' | 'tenant' | 'own'
}

export interface FeedbackCreatePayload {
  kind: FeedbackKind
  priority: FeedbackPriority
  title: string
  description: string
  task_id?: string | null
  rule_code?: string | null
  contact?: string | null
}

export interface FeedbackHandlePayload {
  status: FeedbackStatus
  reply?: string | null
}

export const FEEDBACK_KIND_LABELS: Record<FeedbackKind, string> = {
  false_positive: '误报',
  missed: '漏报',
  rule_suggestion: '规则建议',
  platform_issue: '平台问题',
  other: '其他',
}

export const FEEDBACK_PRIORITY_LABELS: Record<FeedbackPriority, string> = {
  urgent: '紧急',
  high: '高',
  medium: '中',
  low: '低',
}

export const FEEDBACK_STATUS_LABELS: Record<FeedbackStatus, string> = {
  pending: '待处理',
  processing: '处理中',
  resolved: '已解决',
  closed: '已关闭',
}

export function listFeedback(
  params: {
    status?: FeedbackStatus | ''
    kind?: FeedbackKind | ''
    keyword?: string
    page?: number
    page_size?: number
  } = {},
): Promise<FeedbackListResponse> {
  const query = new URLSearchParams()
  if (params.status) query.set('status', params.status)
  if (params.kind) query.set('kind', params.kind)
  if (params.keyword?.trim()) query.set('keyword', params.keyword.trim())
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  return requestJson<FeedbackListResponse>(`/workstation/feedback?${query.toString()}`)
}

export function createFeedback(payload: FeedbackCreatePayload): Promise<FeedbackTicket> {
  return requestJson<FeedbackTicket>('/workstation/feedback', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function handleFeedback(ticketId: string, payload: FeedbackHandlePayload): Promise<FeedbackTicket> {
  return requestJson<FeedbackTicket>(`/workstation/feedback/${encodeURIComponent(ticketId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}
