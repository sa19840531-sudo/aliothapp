import { requestJson } from './http'

/** 创建任务草稿（/workstation/task-drafts）：表单原样存 JSON，只对本账号可见。 */

export interface TaskDraft {
  id: string
  name: string
  target_platform: 'h5' | 'web' | 'app'
  payload: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface TaskDraftPayload {
  name: string
  target_platform: 'h5' | 'web' | 'app'
  payload: Record<string, unknown>
}

export function listTaskDrafts(): Promise<{ items: TaskDraft[] }> {
  return requestJson<{ items: TaskDraft[] }>('/workstation/task-drafts')
}

export function createTaskDraft(payload: TaskDraftPayload): Promise<TaskDraft> {
  return requestJson<TaskDraft>('/workstation/task-drafts', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateTaskDraft(draftId: string, payload: TaskDraftPayload): Promise<TaskDraft> {
  return requestJson<TaskDraft>(`/workstation/task-drafts/${encodeURIComponent(draftId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function deleteTaskDraft(draftId: string): Promise<void> {
  return requestJson<void>(`/workstation/task-drafts/${encodeURIComponent(draftId)}`, {
    method: 'DELETE',
  })
}
