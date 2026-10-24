/// <reference types="node" />
import { webcrypto } from 'node:crypto'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { RedactionArtifact } from '@/services/evidence-redactions.api'

const mocks = vi.hoisted(() => ({ issueEvidenceFileUrl: vi.fn(), previewEvidenceRedaction: vi.fn(), createEvidenceRedaction: vi.fn(), readRedactedEvidence: vi.fn() }))
vi.mock('@/services/task-insight.api', () => ({ issueEvidenceFileUrl: mocks.issueEvidenceFileUrl }))
vi.mock('@/services/evidence-redactions.api', () => mocks)
import EvidenceRedactionEditor from './EvidenceRedactionEditor.vue'

const original = '姓名：张三；订单状态：已完成😀'
const derived = '姓名：██；订单状态：已完成😀'
const digest = async (value: string) => `sha256:${Buffer.from(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(value))).toString('hex')}`
let artifact: RedactionArtifact

beforeEach(async () => {
  vi.resetAllMocks()
  vi.stubGlobal('crypto', webcrypto)
  artifact = { artifact_id: 'a1', artifact_type: 'text', checksum: await digest(original), content_type: 'text/plain', size_bytes: new TextEncoder().encode(original).length, manifest_id: 'm1', manifest_version: 1 }
  mocks.issueEvidenceFileUrl.mockResolvedValue('/ticket?key=short')
  vi.stubGlobal('fetch', vi.fn(async () => new Response(original)))
  mocks.previewEvidenceRedaction.mockResolvedValue({ artifact_id: 'a1', business_reviewable: true,
    source: { original_checksum: artifact.checksum, manifest_id: 'm1', manifest_version: 1 },
    content_base64: Buffer.from(derived).toString('base64'), derived_checksum: await digest(derived), derived_size_bytes: new TextEncoder().encode(derived).length, derived_content_type: 'text/plain' })
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('只有脱敏复核权限时不申请或读取原件', async () => {
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact, canManage: false } })
  await flushPromises()
  expect(mocks.issueEvidenceFileUrl).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
  expect(wrapper.find('[data-testid="redaction-preview"]').exists()).toBe(false)
  wrapper.unmount()
})

it('以原件范围请求服务端预览，未遮盖业务内容保留并确认后才能保存', async () => {
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact, canManage: true } })
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('pre').text()).toBe(original))
  await wrapper.get('[data-testid="redaction-ranges"]').setValue('3,5')
  await wrapper.get('[data-testid="redaction-preview"]').trigger('click')
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.text()).toContain(derived))
  expect(mocks.previewEvidenceRedaction).toHaveBeenCalledWith('a1', expect.objectContaining({
    source_checksum: artifact.checksum, manifest_id: 'm1', manifest_version: 1,
    spec: { method: 'text_ranges', ranges: [{ start: 3, end: 5 }] }, review_confirmed: false,
  }), expect.any(AbortSignal))
  expect(wrapper.get('[data-testid="redaction-save"]').attributes('disabled')).toBeDefined()
  const previewResult = await mocks.previewEvidenceRedaction.mock.results[0]!.value
  mocks.createEvidenceRedaction.mockResolvedValue({ ...previewResult, redaction_id: 'r1' })
  await wrapper.get('[data-testid="redaction-confirm"]').setValue(true)
  await wrapper.get('[data-testid="redaction-save"]').trigger('click')
  await flushPromises()
  expect(mocks.createEvidenceRedaction).toHaveBeenCalledWith('a1', expect.objectContaining({ review_confirmed: true }), expect.any(AbortSignal))
  expect(wrapper.emitted('saved')?.[0]?.[0]).toMatchObject({ redaction_id: 'r1' })
  wrapper.unmount()
})

it('预览失败或范围越界不能保存，编辑变化清空预览确认', async () => {
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact, canManage: true } })
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('pre').exists()).toBe(true))
  await wrapper.get('[data-testid="redaction-ranges"]').setValue('0,999')
  await wrapper.get('[data-testid="redaction-preview"]').trigger('click')
  await flushPromises()
  expect(mocks.previewEvidenceRedaction).not.toHaveBeenCalled()
  await wrapper.get('[data-testid="redaction-ranges"]').setValue('3,5')
  await wrapper.get('[data-testid="redaction-preview"]').trigger('click')
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('[data-testid="redaction-confirm"]').exists()).toBe(true))
  await wrapper.get('[data-testid="redaction-confirm"]').setValue(true)
  await wrapper.get('[data-testid="redaction-ranges"]').setValue('4,5')
  expect(wrapper.find('[data-testid="redaction-confirm"]').exists()).toBe(false)
  mocks.previewEvidenceRedaction.mockRejectedValue(new Error('权限已撤销'))
  await wrapper.get('[data-testid="redaction-preview"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('权限已撤销')
  expect(mocks.createEvidenceRedaction).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('原样保留必须明确选择且两次确认', async () => {
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact, canManage: true } })
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('pre').exists()).toBe(true))
  await wrapper.get('[data-testid="redaction-method"]').setValue('reviewed_unchanged')
  await wrapper.get('[data-testid="redaction-preview"]').trigger('click')
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('[data-testid="redaction-confirm"]').exists()).toBe(true))
  await wrapper.get('[data-testid="redaction-confirm"]').setValue(true)
  expect(wrapper.get('[data-testid="redaction-save"]').attributes('disabled')).toBeDefined()
  await wrapper.get('[data-testid="redaction-unchanged-confirm"]').setValue(true)
  expect(wrapper.get('[data-testid="redaction-save"]').attributes('disabled')).toBeUndefined()
  wrapper.unmount()
})

it('卸载后迟到的原件票据不触发读取', async () => {
  let resolve!: (value: string) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact, canManage: true } })
  wrapper.unmount()
  resolve('/late')
  await flushPromises()
  expect(fetch).not.toHaveBeenCalled()
})

it('JSON 只提供精确路径和原样复核，不显示或提交文本范围策略', async () => {
  const json = JSON.stringify({ phone: '13800000000', order_status: 'success' })
  artifact = { ...artifact, content_type: 'application/json; charset=utf-8', checksum: await digest(json), size_bytes: new TextEncoder().encode(json).length }
  vi.stubGlobal('fetch', vi.fn(async () => new Response(json)))
  mocks.previewEvidenceRedaction.mockResolvedValue({ artifact_id: 'a1', business_reviewable: true,
    source: { original_checksum: artifact.checksum, manifest_id: 'm1', manifest_version: 1 },
    content_base64: Buffer.from(derived).toString('base64'), derived_checksum: await digest(derived), derived_size_bytes: new TextEncoder().encode(derived).length, derived_content_type: 'text/plain' })
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact, canManage: true } })
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('pre').text()).toBe(json))
  const options = wrapper.get('[data-testid="redaction-method"]').findAll('option').map((option) => option.attributes('value'))
  expect(options).toEqual(['json_pointers', 'reviewed_unchanged'])
  expect(wrapper.get<HTMLSelectElement>('[data-testid="redaction-method"]').element.value).toBe('json_pointers')
  await wrapper.get('[data-testid="redaction-ranges"]').setValue('/phone')
  await wrapper.get('[data-testid="redaction-preview"]').trigger('click')
  await flushPromises()
  expect(mocks.previewEvidenceRedaction).toHaveBeenCalledWith('a1', expect.objectContaining({ spec: { method: 'json_pointers', pointers: ['/phone'] } }), expect.any(AbortSignal))
  wrapper.unmount()
})

it.each(['image/svg+xml', 'application/xml', 'application/problem+json', null])('未注册格式 %s 明确阻断且不读取原件', async (contentType) => {
  const wrapper = mount(EvidenceRedactionEditor, { props: { artifact: { ...artifact, content_type: contentType }, canManage: true } })
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('尚未注册局部脱敏策略')
  expect(wrapper.find('[data-testid="redaction-method"]').exists()).toBe(false)
  expect(wrapper.find('[data-testid="redaction-preview"]').exists()).toBe(false)
  expect(mocks.issueEvidenceFileUrl).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
  wrapper.unmount()
})
