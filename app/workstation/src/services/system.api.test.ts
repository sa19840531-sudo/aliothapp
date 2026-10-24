import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureHttpClient } from './http'
import {
  executeDeliveryIncidentCommand,
  listDeliveryIncidents,
  type DeliveryIncident,
} from './system.api'

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const incident: DeliveryIncident = {
  platform: 'app',
  publication_id: 'publication-1',
  task_id: 'task-1',
  status: 'failed',
  row_version: 4,
  finalization_epoch: 2,
  ruleset_id: 'ruleset-1',
  ruleset_version: 1,
  ruleset_snapshot_hash: 'sha256:rules',
  source_identity: { evidence_index_id: 'index-1', evidence_index_hash: 'sha256:index' },
  outbox_pending_count: 0,
  outbox_dead_letter_count: 1,
  inbox_pending_count: 1,
  inbox_dead_letter_count: 0,
  linked_work_item_count: 2,
  linked_work_active_count: 1,
  linked_work_failed_count: 1,
  failure_code: 'DELIVERY_DEAD_LETTER',
  failure_message: '投递重试耗尽',
  allowed_actions: ['reconcile', 'requeue', 'mark_needs_review'],
  operations: [],
  updated_at: '2026-08-18T08:00:00Z',
}

afterEach(() => {
  configureHttpClient({})
  vi.unstubAllGlobals()
})

describe('规则投递事故 API', () => {
  it('按 task_id 查询统一 H5/App 事故，并保留查询限制', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ task_id: 'task-1', incidents: [incident], generated_at: '2026-08-18T08:00:01Z' }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(listDeliveryIncidents('task-1', 50)).resolves.toEqual([incident])

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/workstation/system/delivery-incidents?task_id=task-1&limit=50')
    expect(init.method).toBeUndefined()
  })

  it('兼容后端直接返回事故数组', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([incident]))
    vi.stubGlobal('fetch', fetchMock)

    await expect(listDeliveryIncidents('task with space')).resolves.toEqual([incident])
    expect((fetchMock.mock.calls[0] as [string, RequestInit])[0]).toContain('task_id=task+with+space')
  })

  it('POST 发送动作、理由和 row_version，不把长 token 拼到 URL', async () => {
    const result = {
      operation_id: 'operation-1',
      action: 'requeue',
      platform: 'app',
      publication_id: 'publication/1',
      task_id: 'task-1',
      result: '已重新投递',
      affected_counts: { outbox: 1 },
      incident,
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(result))
    vi.stubGlobal('fetch', fetchMock)
    configureHttpClient({ getToken: () => 'long-lived-token' })

    await expect(
      executeDeliveryIncidentCommand('app', 'publication/1', {
        action: 'requeue',
        reason: '确认死信来自临时 Redis 故障',
        expected_row_version: 4,
      }),
    ).resolves.toEqual(result)

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/workstation/system/delivery-incidents/app/publication%2F1/commands')
    expect(url).not.toContain('access_token')
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer long-lived-token')
    expect(JSON.parse(String(init.body))).toEqual({
      action: 'requeue',
      reason: '确认死信来自临时 Redis 故障',
      expected_row_version: 4,
    })
  })
})
