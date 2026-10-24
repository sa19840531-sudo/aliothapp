import type { ScreenshotRelatedRule, TaskScreenshots } from '@/types/task-insight'

export interface ReportScreenshot {
  artifact_id: string
  page_name: string
  captured_at: string | null
  related_rules: ScreenshotRelatedRule[]
}

/** 画廊只展示代表帧；规则证据仍可引用该任务清单里的被折叠帧。 */
export function indexReportScreenshots(shots: TaskScreenshots | null): Map<string, ReportScreenshot> {
  const entries = new Map<string, ReportScreenshot>()
  for (const group of shots?.groups ?? []) {
    for (const item of group.items) {
      entries.set(item.artifact_id, {
        artifact_id: item.artifact_id,
        page_name: group.page_type_name,
        captured_at: item.captured_at,
        related_rules: item.related_rules,
      })
    }
  }
  for (const group of shots?.groups ?? []) {
    for (const item of group.items) {
      for (const frame of item.related_frames ?? []) {
        if (entries.has(frame.artifact_id)) continue
        entries.set(frame.artifact_id, {
          artifact_id: frame.artifact_id,
          page_name: group.page_type_name,
          captured_at: frame.captured_at,
          // 代表帧关联的规则不能直接归到另一张截图。
          related_rules: [],
        })
      }
    }
  }
  return entries
}
