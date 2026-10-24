import { afterEach, expect, it, vi } from 'vitest'
import { configureHttpClient } from './http'
import { listEvidenceRedactions, readRedactedEvidence } from './evidence-redactions.api'
import { downloadH5ShortflowRedactedArchive } from './h5-shortflows.api'

afterEach(() => { configureHttpClient({}); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('派生件文件和列表走当前 Bearer 身份，不签原件票据', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ items: [], can_manage: false }), { headers: { 'Content-Type': 'application/json' } }))
    .mockResolvedValueOnce(new Response('reviewable'))
  vi.stubGlobal('fetch', fetcher)
  configureHttpClient({ getToken: () => 'current-token' })
  await listEvidenceRedactions('a/1')
  await readRedactedEvidence('r/1')
  expect(fetcher.mock.calls[0]?.[0]).toBe('/api/v2/evidence/a%2F1/redactions')
  expect(fetcher.mock.calls[1]?.[0]).toBe('/api/v2/evidence-redactions/r%2F1/file')
  for (const [url, init] of fetcher.mock.calls) {
    expect(url).not.toContain('ticket')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer current-token')
  }
})

it('可复核导出 POST 明确的快照与每份材料版本，不在 URL 放令牌或策略', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('zip'))
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:zip'), revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  configureHttpClient({ getToken: () => 'current-token' })
  await downloadH5ShortflowRedactedArchive('t1', 'p1', 'snapshot', { a1: 'r1', a2: 'r2' })
  const [url, init] = fetcher.mock.calls[0]!
  expect(url).toBe('/api/v2/tasks/t1/h5-shortflows/p1/archive')
  expect(init.method).toBe('POST')
  expect(new Headers(init.headers).get('Authorization')).toBe('Bearer current-token')
  expect(JSON.parse(init.body)).toEqual({ snapshot_hash: 'snapshot', redaction_ids: { a1: 'r1', a2: 'r2' } })
})
