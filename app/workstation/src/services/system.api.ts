import { requestJson } from './http'

export interface SystemWorkerStatus {
  alive: boolean
  active_instances: number
  last_heartbeat_at: string | null
}

export interface SystemQueueStatus {
  depth: number | null
  redis_available: boolean
}

export interface SystemQueuedTasks {
  count: number
  oldest_created_at: string | null
  oldest_waiting_seconds: number | null
}

export interface SystemStatus {
  worker: SystemWorkerStatus
  queue: SystemQueueStatus
  queued_tasks: SystemQueuedTasks
  generated_at: string
}

/**
 * H5/App 主证据规则发布的统一投递事故投影。
 *
 * 该类型只描述 Publication 及其主链上的 Outbox、Inbox、Work；补采不属于
 * 当前版本的工作台运维范围，也不会通过此接口返回。
 */
export type DeliveryPlatform = 'h5' | 'app'

export type DeliveryOperationAction = 'reconcile' | 'requeue' | 'mark_needs_review'

export interface DeliveryOperationAudit {
  operation_id: string
  action: DeliveryOperationAction
  operator_user_id: string
  reason: string
  result: string
  before_state: Record<string, unknown>
  after_state: Record<string, unknown>
  created_at: string
}

export interface DeliveryIncident {
  platform: DeliveryPlatform
  publication_id: string
  task_id: string
  status: string
  row_version: number
  finalization_epoch: number
  ruleset_id: string
  ruleset_version: number
  ruleset_snapshot_hash: string | null
  source_identity: Record<string, unknown>
  outbox_pending_count: number
  outbox_dead_letter_count: number
  inbox_pending_count: number
  inbox_dead_letter_count: number
  linked_work_item_count: number
  linked_work_active_count: number
  linked_work_failed_count: number
  failure_code: string | null
  failure_message: string | null
  allowed_actions: DeliveryOperationAction[]
  operations: DeliveryOperationAudit[]
  updated_at: string | null
}

export interface DeliveryIncidentListResponse {
  task_id?: string
  incidents?: DeliveryIncident[]
  /** 工作台列表接口的通用字段名兼容位；新接口优先使用 incidents。 */
  items?: DeliveryIncident[]
  generated_at?: string
}

export interface DeliveryOperationCommandPayload {
  action: DeliveryOperationAction
  reason: string
  expected_row_version: number
}

export interface DeliveryOperationResult {
  operation_id: string
  action: DeliveryOperationAction
  platform: DeliveryPlatform
  publication_id: string
  task_id: string
  result: string
  affected_counts: Record<string, number>
  incident: DeliveryIncident
}

export function getSystemStatus(): Promise<SystemStatus> {
  return requestJson<SystemStatus>('/workstation/system/status')
}

function normalizeDeliveryIncidentList(
  payload: DeliveryIncidentListResponse | DeliveryIncident[],
): DeliveryIncident[] {
  if (Array.isArray(payload)) {
    return payload
  }
  if (Array.isArray(payload.incidents)) {
    return payload.incidents
  }
  return Array.isArray(payload.items) ? payload.items : []
}

/** 查询当前任务的 H5/App 主证据投递事故；不包含补采。 */
export async function listDeliveryIncidents(
  taskId: string,
  limit = 100,
): Promise<DeliveryIncident[]> {
  const query = new URLSearchParams({ task_id: taskId, limit: String(limit) })
  const payload = await requestJson<DeliveryIncidentListResponse | DeliveryIncident[]>(
    `/workstation/system/delivery-incidents?${query.toString()}`,
  )
  return normalizeDeliveryIncidentList(payload)
}

/** 执行带版本围栏和理由的 H5/App 主证据投递处置命令。 */
export function executeDeliveryIncidentCommand(
  platform: DeliveryPlatform,
  publicationId: string,
  payload: DeliveryOperationCommandPayload,
): Promise<DeliveryOperationResult> {
  return requestJson<DeliveryOperationResult>(
    `/workstation/system/delivery-incidents/${encodeURIComponent(platform)}/${encodeURIComponent(publicationId)}/commands`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
}
