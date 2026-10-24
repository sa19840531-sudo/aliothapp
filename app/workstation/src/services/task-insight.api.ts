import { downloadFile, requestJson } from './http'
import type {
  PageTimeline,
  RuleResults,
  TaskLogs,
  TaskScreenshots,
  TaskStats,
  AppStageProjection,
  InspectionWorkItems,
  PageGraphProjection,
  EvidenceGaps,
  PageTypeJudgements,
  RuleReuse,
  PageGraph,
  RecoveryRoutes,
  ClosureOutcome,
} from '@/types/task-insight'

const apiBaseUrl = import.meta.env.VITE_ALIOTH_API_BASE_URL || '/api/v1'

/** 拼接可直接用于 <img src> / window.open 的绝对 API 路径。 */
export function resolveApiUrl(path: string) {
  const base = apiBaseUrl.endsWith('/') ? apiBaseUrl.slice(0, -1) : apiBaseUrl
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

export function getPageTimeline(taskId: string): Promise<PageTimeline> {
  return requestJson<PageTimeline>(`/workstation/tasks/${taskId}/page-timeline`)
}

export function getRuleResults(taskId: string): Promise<RuleResults> {
  return requestJson<RuleResults>(`/workstation/tasks/${taskId}/rule-results`)
}

export function getTaskStats(taskId: string): Promise<TaskStats> {
  return requestJson<TaskStats>(`/workstation/tasks/${taskId}/stats`)
}

export function getTaskLogs(
  taskId: string,
  params: { level?: string; page?: number; pageSize?: number } = {},
): Promise<TaskLogs> {
  const query = new URLSearchParams()
  if (params.level) {
    query.set('level', params.level)
  }
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.pageSize ?? 100))
  return requestJson<TaskLogs>(`/workstation/tasks/${taskId}/logs?${query.toString()}`)
}

export function getTaskScreenshots(taskId: string): Promise<TaskScreenshots> {
  return requestJson<TaskScreenshots>(`/workstation/tasks/${taskId}/screenshots`)
}

export function getAppStage(taskId: string): Promise<AppStageProjection> {
  return requestJson<AppStageProjection>(`/workstation/tasks/${taskId}/app-stage`)
}

/** 只读页面状态图（App 页面图面板用的精简投影）；不可用于驱动 Gate／调度。 */
export function getPageGraphProjection(taskId: string): Promise<PageGraphProjection> {
  return requestJson<PageGraphProjection>(`/workstation/tasks/${taskId}/page-graph`)
}

export function getPageGraphRecoveryRoute(
  taskId: string,
  params: { sourceStateId?: string; targetIdentity?: string; limit?: number } = {},
): Promise<Record<string, unknown>> {
  const query = new URLSearchParams()
  if (params.sourceStateId) query.set('source_state_id', params.sourceStateId)
  if (params.targetIdentity) query.set('target_identity', params.targetIdentity)
  if (params.limit != null) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return requestJson(`/workstation/tasks/${taskId}/page-graph/recovery-route${suffix}`)
}

export function getInspectionWorkItems(taskId: string): Promise<InspectionWorkItems> {
  return requestJson<InspectionWorkItems>(`/workstation/tasks/${taskId}/inspection-work-items`)
}

export interface EvidenceDownloadTicket {
  url: string
  expires_at: string
}

/** 先以 Bearer 完成对象授权，再取得只绑定单个 artifact 的短时地址。 */
export async function issueEvidenceFileUrl(
  artifactId: string,
  options: { download?: boolean } = {},
) {
  return (await issueEvidenceFileTicket(artifactId, options)).url
}

/** 保留服务端到期时间，不由前端猜测票据时效或扩大有效期。 */
export async function issueEvidenceFileTicket(
  artifactId: string,
  options: { download?: boolean } = {},
): Promise<EvidenceDownloadTicket> {
  const ticket = await requestJson<EvidenceDownloadTicket>(
    `/evidence/${encodeURIComponent(artifactId)}/ticket`,
    { method: 'POST' },
  )
  const separator = ticket.url.includes('?') ? '&' : '?'
  const suffix = options.download ? `${separator}download=true` : ''
  return { ...ticket, url: resolveApiUrl(`${ticket.url}${suffix}`) }
}

export function downloadScreenshotsArchive(taskId: string) {
  return downloadFile(
    `/workstation/tasks/${taskId}/screenshots/archive`,
    `task_${taskId}_screenshots.zip`,
  )
}

export function downloadLogsExport(taskId: string) {
  return downloadFile(`/workstation/tasks/${taskId}/logs/export`, `task_${taskId}_logs.txt`)
}

export function downloadPlatformProgressExport(taskId: string) {
  return downloadFile(
    `/workstation/tasks/${taskId}/platform-progress/export`,
    `task_${taskId}_platform_progress.xlsx`,
  )
}

/** 页面状态图：页面、状态、实际转场与探索树（只读投影）。 */
export function getPageGraph(taskId: string): Promise<PageGraph> {
  return requestJson<PageGraph>(`/workstation/tasks/${taskId}/page-graph`)
}

/** 页面图只读恢复候选路线：只比较可信历史路线，不执行补采。 */
export function getPageRecoveryRoutes(taskId: string, limit = 100): Promise<RecoveryRoutes> {
  return requestJson<RecoveryRoutes>(
    `/workstation/tasks/${taskId}/page-graph/recovery-route?limit=${limit}`,
  )
}

/** 规则证据缺口台账：判定为证据不足时声明的缺什么、怎么补，只记录不执行。 */
export function getEvidenceGaps(taskId: string): Promise<EvidenceGaps> {
  return requestJson<EvidenceGaps>(`/workstation/tasks/${taskId}/evidence-gaps`)
}

/** 规则结论复用记录：只列沿用来的结论，真实执行看规则结果列表。 */
export function getRuleReuse(taskId: string): Promise<RuleReuse> {
  return requestJson<RuleReuse>(`/workstation/tasks/${taskId}/rule-reuse`)
}

/** 收口结论：页面探索、证据充分、规则结果、最终完成检查分别给出结果。 */
export function getClosureOutcome(taskId: string): Promise<ClosureOutcome> {
  return requestJson<ClosureOutcome>(`/workstation/tasks/${taskId}/closure-outcome`)
}

/** 页型判断与改判记录：各来源判断、裁定与正式识别差异，只记录不改正式页型。 */
export function getPageTypeJudgements(taskId: string): Promise<PageTypeJudgements> {
  return requestJson<PageTypeJudgements>(`/workstation/tasks/${taskId}/page-type-judgements`)
}
