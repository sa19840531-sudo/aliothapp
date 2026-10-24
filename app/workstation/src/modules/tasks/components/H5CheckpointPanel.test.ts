import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { shortflowPlan, shortflowSnapshot } from '@/test/shortflowFixtures'

const mocks = vi.hoisted(() => ({ getH5Shortflows: vi.fn(), downloadH5ShortflowArchive: vi.fn(), downloadH5ShortflowRedactedArchive: vi.fn(), issueEvidenceFileUrl: vi.fn() }))
vi.mock('@/services/h5-shortflows.api', () => mocks)
vi.mock('@/services/task-insight.api', () => ({
  issueEvidenceFileUrl: mocks.issueEvidenceFileUrl,
  issueEvidenceFileTicket: async (id: string) => ({
    url: await mocks.issueEvidenceFileUrl(id), expires_at: new Date(Date.now() + 60000).toISOString(),
  }),
}))
import H5CheckpointPanel from './H5CheckpointPanel.vue'

beforeEach(() => vi.resetAllMocks())

it('局部脱敏包必须完整选择当前成员，以 POST 下载方法提交精确快照', async () => {
  mocks.getH5Shortflows.mockResolvedValue(evidenceSnapshot())
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' }, global: { stubs: { EvidenceRedactionPanel: { name: 'EvidenceRedactionPanel', props: ['artifacts'], emits: ['export'], template: '<div />' } } } })
  await flushPromises()
  await wrapper.get('[data-testid="shortflow-redaction-open"]').trigger('click')
  const panel = wrapper.getComponent({ name: 'EvidenceRedactionPanel' })
  expect(panel.props('artifacts').map((item: { artifact_id: string }) => item.artifact_id)).toEqual(['first', 'second'])
  panel.vm.$emit('export', { first: 'r-first' })
  await flushPromises()
  expect(mocks.downloadH5ShortflowRedactedArchive).not.toHaveBeenCalled()
  mocks.downloadH5ShortflowRedactedArchive.mockRejectedValue(new Error('导出时权限已撤销'))
  panel.vm.$emit('export', { first: 'r-first', second: 'r-second' })
  await flushPromises()
  expect(mocks.downloadH5ShortflowRedactedArchive).toHaveBeenCalledWith('t1', 'plan', 'a'.repeat(64), { first: 'r-first', second: 'r-second' })
  expect(wrapper.text()).toContain('导出时权限已撤销')
  wrapper.unmount()
})

it('V2台账保留精确版本和待复核，导出整组携带快照身份', async () => {
  mocks.getH5Shortflows.mockResolvedValue({ task_id: 't1', snapshot_hash: 'a'.repeat(64), plans: [{
    execution_plan_id: 'plan', agent_run_id: 'run', definition_id: 'player', version: 3,
    scenario_key: 'pause', content_hash: 'f'.repeat(64), checkpoints: [{ checkpoint_id: 'pause', attempts: [{
      attempt_id: 'attempt', status: 'bound', observation_status: 'needs_review', finalization_epoch: 2,
      session_id: 'session', created_at: '2026-10-17T01:00:00Z', bindings: [],
    }] }],
  }] })
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' } })
  await flushPromises()
  expect(wrapper.text()).toContain('V2 检查点')
  expect(wrapper.text()).toContain('待复核')
  expect(wrapper.text()).toContain('非质检通过')
  expect(wrapper.text()).toContain('v3')
  await wrapper.get('[data-testid="shortflow-export"]').trigger('click')
  expect(mocks.downloadH5ShortflowArchive).toHaveBeenCalledWith('t1', 'plan', 'a'.repeat(64))
})

it('任务切换清空旧台账且忽略迟到结果', async () => {
  let resolve!: (value: unknown) => void
  mocks.getH5Shortflows.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  mocks.getH5Shortflows.mockResolvedValueOnce({ task_id: 't2', snapshot_hash: 'b'.repeat(64), plans: [] })
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' } })
  await wrapper.setProps({ taskId: 't2' })
  await flushPromises()
  resolve({ task_id: 't1', plans: [{ execution_plan_id: 'old-canary' }] })
  await flushPromises()
  expect(wrapper.text()).not.toContain('old-canary')
  expect(wrapper.text()).toContain('没有 V2 短流程')
})

function evidenceSnapshot() {
  return { task_id: 't1', snapshot_hash: 'a'.repeat(64), plans: [{
    execution_plan_id: 'plan', definition_id: 'player', version: 1, checkpoints: [{
      checkpoint_id: 'pause', attempts: ['first', 'second'].map((id) => ({
        attempt_id: id, status: 'bound', observation_status: 'needs_review', bindings: [{
          role: 'after_action', integrity: 'current', capture_id: `capture-${id}`,
          artifacts: [{ artifact_id: id, artifact_type: 'screenshot' }],
        }],
      })),
    }],
  }] }
}

async function evidencePanel() {
  mocks.getH5Shortflows.mockResolvedValue(evidenceSnapshot())
  const wrapper = mount(H5CheckpointPanel, {
    props: { taskId: 't1' }, global: { stubs: { ScreenshotEvidenceViewer: true } },
  })
  await flushPromises()
  const buttons = wrapper.findAll('button').filter((button) => button.text().startsWith('查看截图'))
  return { wrapper, buttons }
}

it('后点的证据组不被先点组的迟到鉴权覆盖', async () => {
  let resolve!: (value: string) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  mocks.issueEvidenceFileUrl.mockResolvedValueOnce('/second')
  const { wrapper, buttons } = await evidencePanel()
  await buttons[0]!.trigger('click')
  await buttons[1]!.trigger('click')
  await flushPromises()
  expect(wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('initialArtifactId')).toBe('second')
  resolve('/first')
  await flushPromises()
  expect(wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('initialArtifactId')).toBe('second')
  wrapper.unmount()
})

it('关闭查看器后待返回的另一组鉴权不能重新打开', async () => {
  mocks.issueEvidenceFileUrl.mockResolvedValueOnce('/first')
  let resolve!: (value: string) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  const { wrapper, buttons } = await evidencePanel()
  await buttons[0]!.trigger('click')
  await flushPromises()
  await buttons[1]!.trigger('click')
  wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).vm.$emit('close')
  await flushPromises()
  expect(wrapper.findComponent({ name: 'ScreenshotEvidenceViewer' }).exists()).toBe(false)
  resolve('/second')
  await flushPromises()
  expect(wrapper.findComponent({ name: 'ScreenshotEvidenceViewer' }).exists()).toBe(false)
  wrapper.unmount()
})

it('先点组的迟到错误不覆盖后点组成功或显示旧告警', async () => {
  let reject!: (value: Error) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((_, fail) => { reject = fail }))
  mocks.issueEvidenceFileUrl.mockResolvedValueOnce('/second')
  const { wrapper, buttons } = await evidencePanel()
  await buttons[0]!.trigger('click')
  await buttons[1]!.trigger('click')
  await flushPromises()
  reject(new Error('stale-canary'))
  await flushPromises()
  expect(wrapper.text()).not.toContain('stale-canary')
  expect(wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('initialArtifactId')).toBe('second')
  wrapper.unmount()
})

it('组件卸载后待返回鉴权不再挂载查看器', async () => {
  let resolve!: (value: string) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  const { wrapper, buttons } = await evidencePanel()
  await buttons[0]!.trigger('click')
  wrapper.unmount()
  resolve('/first')
  await flushPromises()
  expect(wrapper.findComponent({ name: 'ScreenshotEvidenceViewer' }).exists()).toBe(false)
})

it('检查点查看器停留267秒后不能继续复用已过期地址', async () => {
  vi.useFakeTimers()
  const start = Date.now()
  mocks.issueEvidenceFileUrl.mockResolvedValueOnce('/first').mockResolvedValueOnce('/renewed')
  const { wrapper, buttons } = await evidencePanel()
  try {
    await buttons[0]!.trigger('click')
    await flushPromises()
    const imageUrl = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('imageUrl')
    expect(imageUrl('first')).toBe('/first')
    vi.setSystemTime(start + 267000)
    expect(imageUrl('first')).toBe('')
    await flushPromises()
    expect(imageUrl('first')).toBe('/renewed')
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})

const evidenceViewerStub = {
  name: 'ScreenshotEvidenceViewer',
  props: ['entries', 'initialArtifactId', 'imageUrl', 'imageError'],
  template: '<div data-testid="rule-evidence-viewer">{{ initialArtifactId }}<span v-for="entry in entries" :key="entry.artifact_id">{{ entry.artifact_id }}</span></div>',
}

it('规则结论定位实际检查点，原图复用出票并限定为同一尝试', async () => {
  mocks.getH5Shortflows.mockResolvedValue(shortflowSnapshot())
  mocks.issueEvidenceFileUrl.mockImplementation(async (id: string) => `/evidence/${id}`)
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' }, attachTo: document.body,
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } } })
  await flushPromises()
  await wrapper.getComponent({ name: 'H5ShortflowRuleResults' }).get('button').trigger('click')
  const checkpoint = wrapper.get('[data-testid="shortflow-checkpoint-materials"]')
  expect(checkpoint.classes()).toContain('is-checkpoint-selected')
  expect(document.activeElement).toBe(checkpoint.element)
  await wrapper.get('[data-testid="shortflow-rule-evidence"]').trigger('click')
  await flushPromises()
  expect(mocks.issueEvidenceFileUrl.mock.calls.map(([id]) => id)).toEqual(['frame-0', 'frame-1'])
  expect(wrapper.get('[data-testid="rule-evidence-viewer"]').text()).toContain('frame-1')
  const entries = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('entries')
  expect(entries[0].actions[0]).toMatchObject({ id: 'action-1', source_capture_id: 'capture-0', target_capture_id: 'capture-1' })
  wrapper.unmount()
})

it('同图在其他流程出现时仍只打开结论所指尝试的前后原图', async () => {
  const plan = shortflowPlan()
  const other = shortflowPlan()
  other.execution_plan_id = 'plan-2'
  other.checkpoints[0]!.attempts[0]!.attempt_id = 'attempt-2'
  other.checkpoints[0]!.attempts[0]!.bindings[1]!.artifacts[0]!.artifact_id = 'other-after'
  other.rule_results![0]!.checkpoints[0]!.evidence_refs.forEach((reference, index) => {
    reference.attempt_id = 'attempt-2'
    if (index) reference.artifact_ids = ['other-after']
  })
  mocks.getH5Shortflows.mockResolvedValue({ ...shortflowSnapshot(plan), plans: [plan, other] })
  mocks.issueEvidenceFileUrl.mockImplementation(async (id: string) => `/evidence/${id}`)
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' },
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } } })
  await flushPromises()
  await wrapper.findAllComponents({ name: 'H5ShortflowRuleResults' })[1]!
    .get('[data-testid="shortflow-rule-evidence"]').trigger('click')
  await flushPromises()
  expect(mocks.issueEvidenceFileUrl.mock.calls.map(([id]) => id)).toEqual(['frame-0', 'other-after'])
  expect(wrapper.get('[data-testid="rule-evidence-viewer"]').text()).not.toContain('frame-1')
})

it('切换任务后迟到的证据票据不打开旧任务原图', async () => {
  let resolve!: (value: string) => void
  mocks.getH5Shortflows.mockResolvedValue(shortflowSnapshot())
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  mocks.issueEvidenceFileUrl.mockResolvedValue('/evidence/second')
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' },
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } } })
  await flushPromises()
  await wrapper.get('[data-testid="shortflow-rule-evidence"]').trigger('click')
  mocks.getH5Shortflows.mockResolvedValue({ ...shortflowSnapshot(), task_id: 't2', plans: [] })
  await wrapper.setProps({ taskId: 't2' })
  await flushPromises()
  resolve('/evidence/old-task')
  await flushPromises()
  expect(wrapper.find('[data-testid="rule-evidence-viewer"]').exists()).toBe(false)
})

it('证据权限拒绝时显示原错误，不以其他截图回退', async () => {
  mocks.getH5Shortflows.mockResolvedValue(shortflowSnapshot())
  mocks.issueEvidenceFileUrl.mockRejectedValue(new Error('无权查看此原始证据'))
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' },
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } } })
  await flushPromises()
  await wrapper.get('[data-testid="shortflow-rule-evidence"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('无权查看此原始证据')
  expect(wrapper.find('[data-testid="rule-evidence-viewer"]').exists()).toBe(false)
})

it('精确规则尝试原图到期后续签，不借用其他流程的同图材料', async () => {
  vi.useFakeTimers()
  const start = Date.now()
  const plan = shortflowPlan()
  const other = shortflowPlan()
  other.execution_plan_id = 'other-plan'
  other.checkpoints[0]!.attempts[0]!.bindings[1]!.artifacts[0]!.artifact_id = 'unrelated-frame'
  mocks.getH5Shortflows.mockResolvedValue({ ...shortflowSnapshot(plan), plans: [plan, other] })
  mocks.issueEvidenceFileUrl.mockImplementation(async (id: string) => `/evidence/${id}/${Date.now()}`)
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' },
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } } })
  try {
    await flushPromises()
    await wrapper.findAllComponents({ name: 'H5ShortflowRuleResults' })[0]!
      .get('[data-testid="shortflow-rule-evidence"]').trigger('click')
    await flushPromises()
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    const imageUrl = viewer.props('imageUrl')
    expect(viewer.props('entries').map((entry: { artifact_id: string }) => entry.artifact_id))
      .toEqual(['frame-0', 'frame-1'])
    expect(imageUrl('frame-1')).toBe(`/evidence/frame-1/${start}`)
    vi.setSystemTime(start + 267000)
    expect(imageUrl('frame-1')).toBe('')
    await flushPromises()
    expect(imageUrl('frame-1')).toBe(`/evidence/frame-1/${start + 267000}`)
    expect(mocks.issueEvidenceFileUrl.mock.calls.map(([id]) => id)).toEqual(['frame-0', 'frame-1', 'frame-1'])
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})

it('精确历史 Manifest 只读审阅不改当前证据组，实际时间线与计划顺序分开', async () => {
  const plan = shortflowPlan()
  const binding = plan.checkpoints[0]!.attempts[0]!.bindings[1]!
  const historical = { ...binding.artifacts[0]!, artifact_id: 'historical-only' }
  binding.manifest_versions = [{ manifest_id: 'history-manifest', manifest_version: 1,
    manifest_hash: 'h'.repeat(64), created_at: '2026-10-18T00:00:00Z', quality_status: 'pending',
    quality_reason: null, is_bound: true, is_latest: false, review_only: true,
    integrity: 'verified', artifacts: [historical] }]
  plan.timeline = [{ event_id: 'actual-capture', event_type: 'capture', occurred_at: '2026-10-19T00:00:01Z',
    checkpoint_id: 'pause', planned_order: 1, attempt_id: 'attempt-1', session_id: 'session-1',
    finalization_epoch: 0, role: 'after_action', capture_id: 'capture-1', action_execution_id: 'action-1' }]
  mocks.getH5Shortflows.mockResolvedValue(shortflowSnapshot(plan))
  mocks.issueEvidenceFileUrl.mockResolvedValue('/history-only')
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' },
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } } })
  await flushPromises()
  expect(wrapper.get('[data-testid="actual-evidence-timeline"]').text()).toContain('计划第 1 项')
  expect(wrapper.text()).toContain('计划检查点顺序')
  const historyButton = wrapper.findAll('button').find((button) => button.text() === '查看历史截图 historical-only')!
  await historyButton.trigger('click')
  await flushPromises()
  expect(wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('entries').map((item: { artifact_id: string }) => item.artifact_id)).toEqual(['historical-only'])
  expect(plan.checkpoints[0]!.attempts[0]!.bindings[1]!.artifacts.map((item) => item.artifact_id)).toEqual(['frame-1'])
  expect(mocks.issueEvidenceFileUrl).toHaveBeenCalledWith('historical-only')
  wrapper.unmount()
})

it('脱敏导出显式携带模式并说明整图遮盖的判读边界', async () => {
  mocks.getH5Shortflows.mockResolvedValue(shortflowSnapshot())
  const wrapper = mount(H5CheckpointPanel, { props: { taskId: 't1' } })
  await flushPromises()
  expect(wrapper.text()).toContain('整图遮盖，不可据此判定业务')
  await wrapper.get('[data-testid="shortflow-export-redacted"]').trigger('click')
  expect(mocks.downloadH5ShortflowArchive).toHaveBeenCalledWith('t1', 'plan-1', 'e'.repeat(64), 'redacted')
  wrapper.unmount()
})

it('关闭材料后迟到的原件下载票据不能打开旧材料', async () => {
  mocks.issueEvidenceFileUrl.mockResolvedValueOnce('/first')
  const { wrapper, buttons } = await evidencePanel()
  await buttons[0]!.trigger('click')
  await flushPromises()
  let resolve!: (value: string) => void
  mocks.issueEvidenceFileUrl.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  try {
    const viewer = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' })
    viewer.vm.$emit('download', 'first')
    viewer.vm.$emit('close')
    resolve('/late-original')
    await flushPromises()
    expect(open).not.toHaveBeenCalled()
    expect(wrapper.findComponent({ name: 'ScreenshotEvidenceViewer' }).exists()).toBe(false)
  } finally {
    open.mockRestore()
    wrapper.unmount()
  }
})

it('规则只选第二个检查点时仍定位原检查点并仅打开该尝试的原图', async () => {
  const plan = shortflowPlan()
  plan.checkpoints.unshift({ checkpoint_id: 'unselected', attempts: [] })
  mocks.getH5Shortflows.mockResolvedValue(shortflowSnapshot(plan))
  mocks.issueEvidenceFileUrl.mockImplementation(async (id: string) => `/evidence/${id}`)
  const wrapper = mount(H5CheckpointPanel, {
    props: { taskId: 't1' }, attachTo: document.body,
    global: { stubs: { ScreenshotEvidenceViewer: evidenceViewerStub } },
  })
  try {
    await flushPromises()
    const checkpoints = wrapper.findAll('[data-testid="shortflow-checkpoint-materials"]')
    await wrapper.getComponent({ name: 'H5ShortflowRuleResults' }).get('button').trigger('click')
    expect(checkpoints[0]!.classes()).not.toContain('is-checkpoint-selected')
    expect(checkpoints[1]!.classes()).toContain('is-checkpoint-selected')
    expect(document.activeElement).toBe(checkpoints[1]!.element)
    await wrapper.get('[data-testid="shortflow-rule-evidence"]').trigger('click')
    await flushPromises()
    expect(mocks.issueEvidenceFileUrl.mock.calls.map(([id]) => id)).toEqual(['frame-0', 'frame-1'])
    const entries = wrapper.getComponent({ name: 'ScreenshotEvidenceViewer' }).props('entries')
    expect(entries.map((entry: { artifact_id: string }) => entry.artifact_id))
      .toEqual(['frame-0', 'frame-1'])
    expect(entries.every((entry: { page_capture_id: string }) =>
      ['capture-0', 'capture-1'].includes(entry.page_capture_id))).toBe(true)
  } finally { wrapper.unmount() }
})
