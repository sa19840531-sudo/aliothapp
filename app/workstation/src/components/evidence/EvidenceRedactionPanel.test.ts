import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import type { RedactionArtifact, RedactionPlanSummary } from '@/services/evidence-redactions.api'
const mocks = vi.hoisted(() => ({ listEvidenceRedactions: vi.fn(), issueEvidenceFileUrl: vi.fn() }))
vi.mock('@/services/evidence-redactions.api', () => ({ listEvidenceRedactions: mocks.listEvidenceRedactions }))
vi.mock('@/services/task-insight.api', () => ({ issueEvidenceFileUrl: mocks.issueEvidenceFileUrl }))
import EvidenceRedactionPanel from './EvidenceRedactionPanel.vue'

const artifacts: RedactionArtifact[] = ['a1', 'a2'].map((id) => ({ artifact_id: id, artifact_type: 'text', content_type: 'text/plain', size_bytes: 3, checksum: `sha256:${id}`, manifest_id: `m-${id}`, manifest_version: 1 }))
const plans = artifacts.map((artifact) => ({ redaction_id: `r-${artifact.artifact_id}`, artifact_id: artifact.artifact_id,
  business_reviewable: true, created_at: '2026-10-23', source: { original_checksum: artifact.checksum, manifest_id: artifact.manifest_id, manifest_version: 1 } }) as RedactionPlanSummary)
const reviewStub = { name: 'EvidenceRedactionReview', props: ['plan'], emits: ['ready', 'failed'], template: '<div />' }
beforeEach(() => {
  vi.resetAllMocks()
  mocks.listEvidenceRedactions.mockImplementation(async (id: string) => ({ can_manage: false, items: plans.filter((plan) => plan.artifact_id === id) }))
})

it('只复核脱敏件的身份不加载原件，全部成员复核选定后才导出', async () => {
  const wrapper = mount(EvidenceRedactionPanel, { props: { artifacts }, global: { stubs: { EvidenceRedactionReview: reviewStub } } })
  await flushPromises()
  expect(mocks.issueEvidenceFileUrl).not.toHaveBeenCalled()
  expect(wrapper.find('[data-testid="redaction-edit"]').exists()).toBe(false)
  expect(wrapper.get('[data-testid="redaction-export"]').attributes('disabled')).toBeDefined()
  await wrapper.get('[data-testid="redaction-version"]').setValue('r-a1')
  wrapper.getComponent({ name: 'EvidenceRedactionReview' }).vm.$emit('ready', 'r-a1')
  await flushPromises()
  await wrapper.get('[data-testid="redaction-choose"]').trigger('click')
  expect(wrapper.get('[data-testid="redaction-export"]').attributes('disabled')).toBeDefined()
  await wrapper.get('[data-testid="redaction-artifact"]').setValue('a2')
  await wrapper.get('[data-testid="redaction-version"]').setValue('r-a2')
  wrapper.getComponent({ name: 'EvidenceRedactionReview' }).vm.$emit('ready', 'r-a2')
  await flushPromises()
  await wrapper.get('[data-testid="redaction-choose"]').trigger('click')
  expect(wrapper.get('[data-testid="redaction-export"]').attributes('disabled')).toBeUndefined()
  await wrapper.get('[data-testid="redaction-export"]').trigger('click')
  expect(wrapper.emitted('export')).toEqual([[{ a1: 'r-a1', a2: 'r-a2' }]])
  wrapper.unmount()
})

it('材料身份不一致、读取失败或绑定冲突时不能选入导出', async () => {
  mocks.listEvidenceRedactions.mockResolvedValue({ can_manage: false, items: [{ ...plans[0], source: { original_checksum: 'sha256:wrong' } }] })
  const wrapper = mount(EvidenceRedactionPanel, { props: { artifacts: [artifacts[0]!] }, global: { stubs: { EvidenceRedactionReview: reviewStub } } })
  await flushPromises()
  expect(wrapper.text()).toContain('尚无与当前原件和 Manifest 一致')
  expect(wrapper.get('[data-testid="redaction-export"]').attributes('disabled')).toBeDefined()
  wrapper.unmount()
  mocks.listEvidenceRedactions.mockResolvedValue({ can_manage: false, items: [plans[0]] })
  const failed = mount(EvidenceRedactionPanel, { props: { artifacts: [artifacts[0]!] }, global: { stubs: { EvidenceRedactionReview: reviewStub } } })
  await flushPromises()
  await failed.get('[data-testid="redaction-version"]').setValue('r-a1')
  failed.getComponent({ name: 'EvidenceRedactionReview' }).vm.$emit('failed', 'r-a1')
  await flushPromises()
  expect(failed.get('[data-testid="redaction-choose"]').attributes('disabled')).toBeDefined()
  await failed.setProps({ blockedReason: '绑定冲突' })
  expect(failed.get('[data-testid="redaction-export"]').attributes('disabled')).toBeDefined()
  failed.unmount()
})

it('切换集合后忽略旧列表，不复活旧编辑权限', async () => {
  let resolve!: (value: unknown) => void
  mocks.listEvidenceRedactions.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const wrapper = mount(EvidenceRedactionPanel, { props: { artifacts: [artifacts[0]!] } })
  await wrapper.setProps({ artifacts: [artifacts[1]!] })
  await flushPromises()
  resolve({ can_manage: true, items: [plans[0]] })
  await flushPromises()
  expect(wrapper.find('[data-testid="redaction-edit"]').exists()).toBe(false)
  expect(wrapper.text()).toContain('m-a2')
  expect(wrapper.text()).not.toContain('m-a1')
  wrapper.unmount()
})
