import type { WorkstationReportListItem } from '@/types/report'

/** 生成成功只代表有报告可读，不能替代人工确认和正式封存状态。 */
export function reportListFlow(item: WorkstationReportListItem) {
  const pending = Math.max(0, item.pending_confirm_count ?? 0)
  const statusReady = ['ready', 'generated'].includes(item.report_status)
  // 显式的报告可用性优先于生成状态，所有平台的 none 都不能开放报告操作。
  const readable = item.report_availability !== 'none' && (item.report_availability === 'preview' || item.report_availability === 'final' ||
    statusReady)
  if (item.report_status === 'failed') {
    return { label: '生成失败', tone: 'tone-danger', readable, exportable: false, reason: '报告生成失败' }
  }
  if (item.report_status === 'generating' && !readable) {
    return { label: '生成中', tone: 'tone-primary', readable: false, exportable: false, reason: '报告仍在生成中' }
  }
  if (pending > 0 && readable) {
    return { label: `待人工确认（${pending}）`, tone: 'tone-warning', readable: true, exportable: false, reason: `尚有 ${pending} 项待人工确认` }
  }
  if (item.report_availability === 'preview') {
    return { label: '质检未完整完成', tone: 'tone-warning', readable: true, exportable: false, reason: '当前只有复核或预览报告，尚未形成正式报告' }
  }
  if (readable && item.report_availability === 'final') {
    return { label: '已完成', tone: 'tone-success', readable: true, exportable: true, reason: '' }
  }
  return { label: readable ? '报告状态待核实' : '未生成', tone: 'tone-muted', readable, exportable: false, reason: readable ? '正式报告状态未知，请进入详情核实' : '尚未产生可查看的报告' }
}
