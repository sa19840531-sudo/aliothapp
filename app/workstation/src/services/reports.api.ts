import { downloadFile, requestJson } from './http'
import type { WorkstationReportDetail, WorkstationReportListResponse } from '@/types/report'
import type {
  HumanVerdict,
  ReportConfirmResult,
  ReportInsight,
} from '@/types/report-insight'

/** 服务端筛选生成状态：已生成（含待确认）/ 生成中 / 生成失败；完成流程另按已有数量与报告可用性展示。 */
export type ReportListStatusFilter = 'ready' | 'generating' | 'failed'

export interface ListReportsParams {
  page?: number
  page_size?: number
  task_name?: string
  product_name?: string
  department_name?: string
  target_platform?: string
  created_from?: string
  created_to?: string
  report_status?: ReportListStatusFilter
}

export function listReports(params: ListReportsParams = {}): Promise<WorkstationReportListResponse> {
  const query = new URLSearchParams()
  const entries: Array<[string, string | number | undefined]> = [
    ['page', params.page],
    ['page_size', params.page_size],
    ['task_name', params.task_name],
    ['product_name', params.product_name],
    ['department_name', params.department_name],
    ['target_platform', params.target_platform],
    ['created_from', params.created_from],
    ['created_to', params.created_to],
    ['report_status', params.report_status],
  ]
  entries.forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      query.set(key, String(value))
    }
  })
  const queryString = query.toString()
  return requestJson<WorkstationReportListResponse>(
    queryString ? `/workstation/reports?${queryString}` : '/workstation/reports',
  )
}

export function getReport(taskId: string): Promise<WorkstationReportDetail> {
  return requestJson<WorkstationReportDetail>(`/workstation/reports/${taskId}`)
}

export function getReportInsight(taskId: string): Promise<ReportInsight> {
  return requestJson<ReportInsight>(`/workstation/reports/${taskId}/insight`)
}

export function confirmReportRule(
  taskId: string,
  executionId: string,
  payload: { verdict: HumanVerdict; note?: string | null },
): Promise<ReportConfirmResult> {
  return requestJson<ReportConfirmResult>(
    `/workstation/reports/${taskId}/confirmations/${executionId}`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
}

export function downloadReportExport(taskId: string) {
  return downloadFile(`/workstation/reports/${taskId}/export`, `task_${taskId}_report.html`)
}

/** 批量导出：服务端打成一个 zip。 */
export function downloadReportExportBatch(taskIds: string[]) {
  const query = new URLSearchParams()
  taskIds.forEach((id) => query.append('task_ids', id))
  return downloadFile(`/workstation/reports/export-batch?${query.toString()}`, '质检报告.zip')
}

export const listReportTasks = listReports
export const getReportRuleExecutions = getReport
