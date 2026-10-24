/// <reference types="node" />
import { webcrypto } from 'node:crypto'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import type { RedactionPlanSummary } from '@/services/evidence-redactions.api'
const mocks = vi.hoisted(() => ({ readRedactedEvidence: vi.fn(), issueEvidenceFileUrl: vi.fn() }))
vi.mock('@/services/evidence-redactions.api', () => ({ readRedactedEvidence: mocks.readRedactedEvidence }))
vi.mock('@/services/task-insight.api', () => ({ issueEvidenceFileUrl: mocks.issueEvidenceFileUrl }))
import EvidenceRedactionReview from './EvidenceRedactionReview.vue'

afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })
it('只读取派生件并校验摘要，HTML 仍为纯文本', async () => {
  vi.stubGlobal('crypto', webcrypto)
  const text = '<b>订单成功</b><script>alert(1)</script>'
  const bytes = new TextEncoder().encode(text)
  const checksum = `sha256:${Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex')}`
  const plan = { redaction_id: 'r1', business_reviewable: true, derived_checksum: checksum, derived_size_bytes: bytes.length, derived_content_type: 'text/html', source: { original_checksum: 'sha256:original' } } as RedactionPlanSummary
  mocks.readRedactedEvidence.mockResolvedValue(new Blob([bytes]))
  const wrapper = mount(EvidenceRedactionReview, { props: { plan } })
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('pre').text()).toBe(text))
  expect(mocks.readRedactedEvidence).toHaveBeenCalledWith('r1', expect.any(AbortSignal))
  expect(mocks.issueEvidenceFileUrl).not.toHaveBeenCalled()
  expect(wrapper.find('script').exists()).toBe(false)
  expect(wrapper.emitted('ready')).toEqual([['r1']])
  wrapper.unmount()
})

it('卸载后迟到的派生件不会触发复核就绪', async () => {
  let resolve!: (value: Blob) => void
  mocks.readRedactedEvidence.mockImplementationOnce(() => new Promise<Blob>((done) => { resolve = done }))
  const plan = { redaction_id: 'r1', business_reviewable: true, source: {} } as RedactionPlanSummary
  const wrapper = mount(EvidenceRedactionReview, { props: { plan } })
  wrapper.unmount()
  resolve(new Blob(['late']))
  await flushPromises()
  expect(wrapper.emitted('ready')).toBeUndefined()
})
