import { requestJson, requestJsonV2 } from './http'
import type {
  AgentRunEnvelope,
  AppTaskCreatePayload,
  ListTasksParams,
  SubmitTaskResponse,
  V2ExplorationTaskCreatePayload,
  V2TaskCreatePayload,
  WorkstationTaskCreatePayload,
  WorkstationTaskDetail,
  WorkstationTaskListResponse,
  WorkstationTaskOperationResponse,
} from '@/types/task'
import type {
  CandidateRuleListResponse,
  RuleExecutionListResponse,
} from '@/types/rule-execution'

const pendingMutationKeys = new Map<string, string>()
const pendingMutationStoragePrefix = 'alioth:pending-mutation:'

function newMutationKey() {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto)
  return randomUUID ? randomUUID() : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function stablePayload(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stablePayload(item)).join(',')}]`
  }
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stablePayload(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value) ?? 'undefined'
}

function pendingMutationStorageKey(fingerprint: string): string {
  let left = 0x811c9dc5
  let right = 0x9e3779b9
  for (let index = 0; index < fingerprint.length; index += 1) {
    const code = fingerprint.charCodeAt(index)
    left = Math.imul(left ^ code, 0x01000193)
    right = Math.imul(right ^ code, 0x85ebca6b)
  }
  return `${pendingMutationStoragePrefix}${(left >>> 0).toString(16)}${(right >>> 0).toString(16)}:${fingerprint.length}`
}

function readPendingMutationKey(fingerprint: string): string | undefined {
  const memoryKey = pendingMutationKeys.get(fingerprint)
  if (memoryKey) return memoryKey
  try {
    return globalThis.sessionStorage?.getItem(pendingMutationStorageKey(fingerprint)) ?? undefined
  } catch {
    return undefined
  }
}

function rememberPendingMutationKey(fingerprint: string, idempotencyKey: string) {
  pendingMutationKeys.set(fingerprint, idempotencyKey)
  try {
    globalThis.sessionStorage?.setItem(pendingMutationStorageKey(fingerprint), idempotencyKey)
  } catch {
    // 浏览器禁用会话存储时退化为当前页面生命周期内复用。
  }
}

function forgetPendingMutationKey(fingerprint: string) {
  pendingMutationKeys.delete(fingerprint)
  try {
    globalThis.sessionStorage?.removeItem(pendingMutationStorageKey(fingerprint))
  } catch {
    // 会话存储不可用不影响服务端幂等。
  }
}

async function submitWithStableKey<T>(
  requestIdentity: string,
  payload: unknown,
  submit: (idempotencyKey: string) => Promise<T>,
): Promise<T> {
  const fingerprint = `${requestIdentity}:${stablePayload(payload)}`
  const idempotencyKey = readPendingMutationKey(fingerprint) ?? newMutationKey()
  rememberPendingMutationKey(fingerprint, idempotencyKey)
  try {
    const result = await submit(idempotencyKey)
    forgetPendingMutationKey(fingerprint)
    return result
  } catch (error) {
    // 请求超时或响应丢失时保留同一键；用户用相同表单重试不会创建第二个任务。
    throw error
  }
}

function buildTaskListQuery(params: ListTasksParams = {}) {
  const query = new URLSearchParams()
  const entries: Array<[string, string | number | undefined]> = [
    ['task_name', params.task_name],
    ['product_name', params.product_name],
    ['product_code', params.product_code],
    ['target_url', params.target_url],
    ['status', params.status],
    ['report_status', params.report_status],
    ['created_from', params.created_from],
    ['created_to', params.created_to],
    ['department_name', params.department_name],
    ['submitter_name', params.submitter_name],
    ['target_platform', params.target_platform],
    ['page', params.page],
    ['page_size', params.page_size ?? params.limit],
  ]
  entries.forEach(([key, value]) => {
    if (value !== undefined && value !== '') {
      query.set(key, String(value))
    }
  })
  const queryString = query.toString()
  return queryString ? `/workstation/tasks?${queryString}` : '/workstation/tasks'
}

export function submitTask(payload: WorkstationTaskCreatePayload): Promise<SubmitTaskResponse> {
  return submitWithStableKey('v1:workstation:create', payload, (idempotencyKey) =>
    requestJson<SubmitTaskResponse>('/workstation/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(payload),
    }),
  )
}

/** V2 统一任务入口；目标平台和 App 制品来源由判别字段明确区分。 */
export function submitV2Task(payload: V2TaskCreatePayload): Promise<SubmitTaskResponse> {
  return submitWithStableKey('v2:task:create', payload, (idempotencyKey) =>
    requestJsonV2<SubmitTaskResponse>('/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(payload),
    }),
  )
}

/** H5、Web 与 App 仅页面探索：留存证据并完成页面类型判定，不触发正式规则质检。 */
export function submitExplorationTask(
  payload: V2ExplorationTaskCreatePayload,
): Promise<SubmitTaskResponse> {
  return submitWithStableKey('v2:exploration:create', payload, (idempotencyKey) =>
    requestJsonV2<SubmitTaskResponse>('/exploration-tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(payload),
    }),
  )
}

/** 正式 App 质检：直连 /tasks，不走 H5 workstation create。 */
export function submitAppTask(payload: AppTaskCreatePayload): Promise<SubmitTaskResponse> {
  const packageName = payload.package_name?.trim() || null
  const requestPayload = {
    target_platform: 'app',
    target_url: null,
    ruleset_code: payload.ruleset_code ?? 'alioth_first_ruleset',
    ruleset_version: payload.ruleset_version ?? 1,
    channel: payload.channel ?? 'workbench',
    priority: payload.priority ?? 100,
    task_config: {
      task_name: payload.task_name.trim(),
      workflow_version: 'v2',
      remark: payload.remark?.trim() || null,
      app: {
        platform_build_ref: payload.platform_build_ref,
        package_name: packageName,
        device: {
          backend: payload.device_backend ?? 'cloud_phone',
          profile_id: payload.device_profile_id ?? 'default',
        },
      },
      product: {
        department_name: '鼎丰',
        product_code: 'app-qc',
        product_name: 'App质检',
      },
    },
    page_sampling_policy: {},
    loop_guard: {},
  }
  return submitWithStableKey('v1:app:create', requestPayload, (idempotencyKey) =>
    requestJson<SubmitTaskResponse>('/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(requestPayload),
    }),
  )
}

export function listTasks(params?: ListTasksParams): Promise<WorkstationTaskListResponse> {
  return requestJson<WorkstationTaskListResponse>(buildTaskListQuery(params))
}

export function getTask(taskId: string): Promise<WorkstationTaskDetail> {
  return requestJson<WorkstationTaskDetail>(`/workstation/tasks/${taskId}`)
}

/** 管理端删除已结束任务，仅移除任务可见性，保留已有审计和证据。 */
export function deleteTask(taskId: string): Promise<WorkstationTaskOperationResponse> {
  return requestJson<WorkstationTaskOperationResponse>(`/workstation/tasks/${taskId}`, { method: 'DELETE' })
}

export function performTaskOperation(
  taskId: string,
  operation: 'start' | 'cancel' | 'rerun' | 'copy' | 'retry-finalization',
): Promise<WorkstationTaskOperationResponse> {
  const path = `/workstation/tasks/${taskId}/${operation}`
  return submitWithStableKey(`v1:workstation:operation:${operation}`, { taskId }, (idempotencyKey) =>
    requestJson<WorkstationTaskOperationResponse>(path, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  )
}

export function listRuleExecutions(taskId: string): Promise<RuleExecutionListResponse> {
  return requestJson<RuleExecutionListResponse>(`/tasks/${taskId}/rule-executions`)
}

export function listCandidateRules(
  taskId: string,
  pageType?: string,
): Promise<CandidateRuleListResponse> {
  const query = pageType ? `?page_type=${encodeURIComponent(pageType)}` : ''
  return requestJson<CandidateRuleListResponse>(`/tasks/${taskId}/candidate-rules${query}`)
}

export function getAgentRun(taskId: string): Promise<AgentRunEnvelope> {
  return requestJson<AgentRunEnvelope>(`/tasks/${taskId}/agent-run`)
}
