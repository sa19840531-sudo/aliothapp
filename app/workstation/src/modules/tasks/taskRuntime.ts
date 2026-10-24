/** 完整质检默认运行预算为 150 分钟；这里只作时长提示，不变更任务终态。 */
export const LONG_EXECUTION_MS = 2.5 * 60 * 60 * 1000

export function executionDurationMs(started: string | null | undefined, finished: string | null | undefined, now: number): number | null {
  if (!started) return null
  const start = Date.parse(started)
  const end = finished ? Date.parse(finished) : now
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : null
}

export function isLongExecution(status: string, duration: number | null | undefined, finished?: string | null): boolean {
  return ['running', 'canceling'].includes(status) && !finished && duration != null && Number.isFinite(duration) && duration >= LONG_EXECUTION_MS
}
