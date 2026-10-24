/// <reference types="node" />
import { webcrypto } from 'node:crypto'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import type { H5ShortflowArtifact } from '@/services/h5-shortflows.api'
const mocks = vi.hoisted(() => ({ issueEvidenceFileUrl: vi.fn() }))
vi.mock('@/services/task-insight.api', () => mocks)
import EvidenceTextViewer from './EvidenceTextViewer.vue'

afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks() })

it('DOM 原件通过当前对象票据加载并作为纯文本显示，下载仍指向原件', async () => {
  vi.stubGlobal('crypto', webcrypto)
  const content = '<img src=x onerror="window.canary=1"><script>window.canary=1</script>'
  const bytes = new TextEncoder().encode(content)
  const checksum = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex')
  const artifact: H5ShortflowArtifact = { artifact_id: 'dom-1', artifact_type: 'dom', content_type: 'text/html', size_bytes: bytes.length, checksum: `sha256:${checksum}` }
  mocks.issueEvidenceFileUrl.mockResolvedValue('/signed?ticket=one')
  const fetcher = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(bytes))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(EvidenceTextViewer, { props: { artifact, artifacts: [artifact], title: '历史 v1' } })
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('pre').text()).toBe(content))
  expect(wrapper.find('script').exists()).toBe(false)
  expect(wrapper.find('img').exists()).toBe(false)
  expect(fetcher.mock.calls[0]?.[0]).toBe('/signed?ticket=one&representation=original')
  await wrapper.findAll('button')[1]!.trigger('click')
  expect(wrapper.emitted('download')).toEqual([['dom-1']])
  wrapper.unmount()
})

it('切换材料或卸载后旧票据不会取回旧原件', async () => {
  let resolve!: (url: string) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  const artifact = { artifact_id: 'old', content_type: 'text/plain', size_bytes: 1 } as H5ShortflowArtifact
  const wrapper = mount(EvidenceTextViewer, { props: { artifact, artifacts: [artifact], title: '历史' } })
  wrapper.unmount()
  resolve('/old')
  await flushPromises()
  expect(fetcher).not.toHaveBeenCalled()
})
