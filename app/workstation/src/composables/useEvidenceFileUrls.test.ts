import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useEvidenceFileUrls } from './useEvidenceFileUrls'

const mocks = vi.hoisted(() => ({ issue: vi.fn() }))
vi.mock('@/services/task-insight.api', () => ({
  issueEvidenceFileUrl: async (id: string) => (await mocks.issue(id)).url,
  issueEvidenceFileTicket: mocks.issue,
}))
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1792404180000); mocks.issue.mockReset() })
afterEach(() => vi.useRealTimers())

it('缺少到期时间的票据不能显示图片或触发重复申请', async () => {
  mocks.issue.mockResolvedValueOnce({ url: '/missing-expiry' })
  const files = useEvidenceFileUrls()
  await files.load(['missing-expiry'])
  for (let i = 0; i < 10; i++) expect(files.get('missing-expiry')).toBe('')
  expect(files.getError('missing-expiry')).toContain('失败')
  expect(mocks.issue).toHaveBeenCalledTimes(1)
})

it('同一原图267秒后不能复用已经过期的60秒票据', async () => {
  mocks.issue.mockResolvedValueOnce({ url: '/old', expires_at: new Date(1792404240000).toISOString() })
    .mockResolvedValueOnce({ url: '/fresh', expires_at: new Date(1792404507000).toISOString() })
  const files = useEvidenceFileUrls()
  await files.load(['01a6506a-ed01-4d79-9046-2a8d85ff6dc2'])
  expect(files.get('01a6506a-ed01-4d79-9046-2a8d85ff6dc2')).toBe('/old')
  vi.setSystemTime(1792404447000)
  expect(files.get('01a6506a-ed01-4d79-9046-2a8d85ff6dc2')).toBe('')
  await flushPromises()
  expect(files.get('01a6506a-ed01-4d79-9046-2a8d85ff6dc2')).toBe('/fresh')
  expect(mocks.issue).toHaveBeenCalledTimes(2)
})

it('并发读取同图只签发一次，不覆盖其他图的已完成结果', async () => {
  let resolve!: (value: unknown) => void
  mocks.issue.mockReturnValueOnce(new Promise((done) => { resolve = done }))
    .mockResolvedValueOnce({ url: '/b', expires_at: new Date(Date.now() + 60000).toISOString() })
  const files = useEvidenceFileUrls()
  const one = files.load(['a'])
  const two = files.load(['a', 'b'])
  await flushPromises()
  resolve({ url: '/a', expires_at: new Date(Date.now() + 60000).toISOString() })
  await Promise.all([one, two])
  expect(mocks.issue).toHaveBeenCalledTimes(2)
  expect(files.get('a')).toBe('/a')
  expect(files.get('b')).toBe('/b')
})

it('任务切换后晚到的旧票据不能重新写入缓存', async () => {
  let resolve!: (value: unknown) => void
  mocks.issue.mockReturnValueOnce(new Promise((done) => { resolve = done }))
  const files = useEvidenceFileUrls()
  const loading = files.load(['old'])
  files.clear()
  resolve({ url: '/old', expires_at: new Date(Date.now() + 60000).toISOString() })
  await loading
  expect(files.get('old')).toBe('')
})

it('错误或过期的签发结果不自动空转、不回退匿名或原图地址', async () => {
  mocks.issue.mockRejectedValue(new Error('404'))
  const files = useEvidenceFileUrls()
  await files.load(['denied'])
  for (let i = 0; i < 10; i++) expect(files.get('denied')).toBe('')
  expect(files.getError('denied')).toContain('失败')
  expect(mocks.issue).toHaveBeenCalledTimes(1)
  mocks.issue.mockResolvedValueOnce({ url: '/expired', expires_at: 'invalid' })
  await files.load(['expired'])
  expect(files.get('expired')).toBe('')
  expect(files.getError('expired')).toContain('失败')
})

it('权限拒绝保留服务端原因，非Error仍显示安全兜底且不自动重试', async () => {
  mocks.issue.mockRejectedValueOnce(new Error('无权查看此原始证据')).mockRejectedValueOnce(null)
  const files = useEvidenceFileUrls()
  await files.load(['denied', 'unknown'])
  expect(files.getError('denied')).toContain('无权查看此原始证据')
  expect(files.getError('unknown')).toBe('证据访问地址申请失败，请重新打开或下载原图')
  expect(files.get('denied')).toBe('')
  expect(files.get('unknown')).toBe('')
  expect(mocks.issue).toHaveBeenCalledTimes(2)
})
