import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listFeedback: vi.fn(),
  createFeedback: vi.fn(),
  handleFeedback: vi.fn(),
  push: vi.fn(),
  query: {} as Record<string, string>,
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: mocks.query }),
  useRouter: () => ({ push: mocks.push }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => mocks.message,
}))

vi.mock('@/services/feedback.api', async () => {
  const actual = await vi.importActual<typeof import('@/services/feedback.api')>(
    '@/services/feedback.api',
  )
  return {
    ...actual,
    listFeedback: mocks.listFeedback,
    createFeedback: mocks.createFeedback,
    handleFeedback: mocks.handleFeedback,
  }
})

import FeedbackView from './FeedbackView.vue'

function ticket(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ticket-1',
    ticket_no: 'FB202609080001',
    tenant_id: 'tenant-a',
    tenant_name: '北京分公司',
    submitter_id: 'user-1',
    submitter_name: '刘一',
    kind: 'missed',
    priority: 'high',
    title: '首页倒计时广告没被报出来',
    description: '首页进入后 3 秒有倒计时广告，报告里未通过项没有它',
    task_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    rule_code: 'alioth_3',
    contact: null,
    status: 'pending',
    reply: null,
    handled_by: null,
    handled_at: null,
    created_at: '2026-09-08T08:00:00Z',
    updated_at: '2026-09-08T08:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.query = {}
  mocks.listFeedback.mockResolvedValue({
    items: [ticket()],
    total: 1,
    page: 1,
    page_size: 20,
    can_handle: false,
    visibility: 'own',
  })
  mocks.createFeedback.mockResolvedValue(ticket({ id: 'ticket-2', ticket_no: 'FB202609080002' }))
  mocks.handleFeedback.mockResolvedValue(ticket({ status: 'processing', reply: '已转规则负责人' }))
})

describe('FeedbackView（我的反馈）', () => {
  it('列表用中文展示类型、优先级与状态，展开可见描述', async () => {
    const wrapper = mount(FeedbackView)
    await flushPromises()

    const table = wrapper.find('[data-testid="feedback-table"]')
    expect(table.text()).toContain('FB202609080001')
    expect(table.text()).toContain('漏报')
    expect(table.text()).toContain('高')
    expect(table.text()).toContain('待处理')
    expect(table.text()).toContain('任务 aaaaaaaa')
    expect(table.text()).not.toContain('倒计时广告，报告里')
    // 只看自己的反馈时不显示提交人列，也没有处理按钮
    expect(table.text()).not.toContain('提交人')
    expect(wrapper.find('[data-testid="handle-FB202609080001"]').exists()).toBe(false)

    const viewButton = table.findAll('button').find((button) => button.text() === '详情')
    await viewButton!.trigger('click')
    expect(wrapper.text()).toContain('倒计时广告，报告里未通过项没有它')
    expect(wrapper.text()).toContain('尚未回复')
  })

  it('带 task 与 rule 参数进入时自动打开提交弹窗并预填，提交后回列表', async () => {
    mocks.query = { task: 'task-9', rule: 'alioth_5' }
    const wrapper = mount(FeedbackView)
    await flushPromises()

    expect(wrapper.find('[data-testid="submit-modal"]').exists()).toBe(true)
    expect((wrapper.get('[data-testid="submit-task"]').element as HTMLInputElement).value).toBe('task-9')
    expect((wrapper.get('[data-testid="submit-rule"]').element as HTMLInputElement).value).toBe('alioth_5')

    await wrapper.get('[data-testid="submit-confirm"]').trigger('click')
    expect(mocks.message.warning).toHaveBeenCalledWith('请填写反馈标题')
    expect(mocks.createFeedback).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="submit-kind"]').setValue('false_positive')
    await wrapper.get('[data-testid="submit-priority"]').setValue('urgent')
    await wrapper.get('[data-testid="submit-title"]').setValue('这条规则误报了')
    await wrapper.get('[data-testid="submit-description"]').setValue('页面上并没有弹窗')
    await wrapper.get('[data-testid="submit-confirm"]').trigger('click')
    await flushPromises()

    expect(mocks.createFeedback).toHaveBeenCalledWith({
      kind: 'false_positive',
      priority: 'urgent',
      title: '这条规则误报了',
      description: '页面上并没有弹窗',
      task_id: 'task-9',
      rule_code: 'alioth_5',
      contact: null,
    })
    expect(wrapper.find('[data-testid="submit-modal"]').exists()).toBe(false)
    expect(mocks.listFeedback).toHaveBeenCalledTimes(2)
  })

  it('从部门规则中心反馈规则问题时预填类型、标题和规则编号', async () => {
    mocks.query = { tab: 'my', source: 'department-rules', rule: 'alioth_1', name: '首页弹窗可以关闭' }
    const wrapper = mount(FeedbackView)
    await flushPromises()

    expect(wrapper.find('[data-testid="submit-modal"]').exists()).toBe(true)
    expect((wrapper.get('[data-testid="submit-kind"]').element as HTMLSelectElement).value).toBe('rule_suggestion')
    expect((wrapper.get('[data-testid="submit-title"]').element as HTMLInputElement).value).toBe('规则问题：首页弹窗可以关闭')
    expect((wrapper.get('[data-testid="submit-rule"]').element as HTMLInputElement).value).toBe('alioth_1')
  })

  it('有处理权限时显示提交人并可改状态回复', async () => {
    mocks.listFeedback.mockResolvedValue({
      items: [ticket()],
      total: 1,
      page: 1,
      page_size: 20,
      can_handle: true,
      visibility: 'tenant',
    })
    const wrapper = mount(FeedbackView)
    await flushPromises()

    expect(wrapper.find('[data-testid="feedback-table"]').text()).toContain('刘一')
    await wrapper.get('[data-testid="handle-FB202609080001"]').trigger('click')
    expect(wrapper.find('[data-testid="handle-modal"]').exists()).toBe(true)
    await wrapper.get('[data-testid="handle-status"]').setValue('resolved')
    await wrapper.get('[data-testid="handle-reply"]').setValue('规则已调整阈值')
    await wrapper.get('[data-testid="handle-confirm"]').trigger('click')
    await flushPromises()

    expect(mocks.handleFeedback).toHaveBeenCalledWith('ticket-1', {
      status: 'resolved',
      reply: '规则已调整阈值',
    })
    expect(wrapper.find('[data-testid="handle-modal"]').exists()).toBe(false)
  })
})
