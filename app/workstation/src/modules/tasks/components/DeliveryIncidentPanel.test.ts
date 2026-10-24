import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import DeliveryIncidentPanel from './DeliveryIncidentPanel.vue'
import type { DeliveryIncident } from '@/services/system.api'

const mocks = vi.hoisted(() => ({
  listDeliveryIncidents: vi.fn(),
  executeDeliveryIncidentCommand: vi.fn(),
  message: { success: vi.fn() },
}))

vi.mock('@/services/system.api', () => ({
  listDeliveryIncidents: mocks.listDeliveryIncidents,
  executeDeliveryIncidentCommand: mocks.executeDeliveryIncidentCommand,
}))

vi.mock('@/stores/auth', () => ({
  authStore: {
    user: {
      value: { permissions: ['systemOperations'] },
    },
  },
}))

vi.mock('naive-ui', () => ({
  useMessage: () => mocks.message,
}))

const incident: DeliveryIncident = {
  platform: 'h5',
  publication_id: 'publication-h5-1',
  task_id: 'task-1',
  status: 'failed',
  row_version: 6,
  finalization_epoch: 3,
  ruleset_id: 'ruleset-1',
  ruleset_version: 1,
  ruleset_snapshot_hash: 'sha256:rules',
  source_identity: { page_capture_id: 'capture-1' },
  outbox_pending_count: 1,
  outbox_dead_letter_count: 1,
  inbox_pending_count: 0,
  inbox_dead_letter_count: 1,
  linked_work_item_count: 2,
  linked_work_active_count: 1,
  linked_work_failed_count: 1,
  failure_code: 'OUTBOX_DEAD_LETTER',
  failure_message: '重试次数耗尽',
  allowed_actions: ['reconcile', 'requeue', 'mark_needs_review'],
  operations: [
    {
      operation_id: 'operation-1',
      action: 'requeue',
      operator_user_id: 'operator-1',
      reason: 'Redis 临时不可用',
      result: 'applied',
      before_state: { row_version: 5 },
      after_state: { row_version: 6 },
      created_at: '2026-08-18T08:00:00Z',
    },
  ],
  updated_at: '2026-08-18T08:00:00Z',
}

describe('DeliveryIncidentPanel', () => {
  it('展示 H5/App 投递状态、死信、失败原因和最近处置时间线', async () => {
    mocks.listDeliveryIncidents.mockResolvedValue([incident])
    const wrapper = mount(DeliveryIncidentPanel, {
      props: { taskId: 'task-1', targetPlatform: 'h5' },
    })
    await flushPromises()

    expect(mocks.listDeliveryIncidents).toHaveBeenCalledWith('task-1')
    expect(wrapper.get('[data-testid="delivery-incident-panel"]').text()).toContain('H5 · Publication')
    expect(wrapper.text()).toContain('Outbox：1 待处理 / 1 死信')
    expect(wrapper.text()).toContain('Inbox：0 待处理 / 1 死信')
    expect(wrapper.text()).toContain('OUTBOX_DEAD_LETTER')
    expect(wrapper.text()).toContain('Redis 临时不可用')
    expect(wrapper.text()).toContain('operator-1')
  })

  it('三个动作都要求理由，并用当前 row_version 提交后刷新', async () => {
    mocks.listDeliveryIncidents.mockResolvedValue([incident])
    mocks.executeDeliveryIncidentCommand.mockResolvedValue({
      operation_id: 'operation-2',
      action: 'requeue',
      platform: 'h5',
      publication_id: incident.publication_id,
      task_id: incident.task_id,
      result: '已重新投递',
      affected_counts: { outbox: 1 },
      incident,
    })
    const wrapper = mount(DeliveryIncidentPanel, {
      props: { taskId: 'task-1', targetPlatform: 'h5' },
    })
    await flushPromises()

    expect(wrapper.findAll('[data-testid="delivery-action-reconcile"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="delivery-action-requeue"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="delivery-action-mark_needs_review"]')).toHaveLength(1)

    await wrapper.get('[data-testid="delivery-action-requeue"]').trigger('click')
    await wrapper.get('[data-testid="confirm-delivery-action"]').trigger('click')
    expect(mocks.executeDeliveryIncidentCommand).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请填写处置理由后再确认')

    await wrapper.get('[data-testid="delivery-reason-input"]').setValue('确认 Redis 临时故障已恢复')
    await wrapper.get('[data-testid="confirm-delivery-action"]').trigger('click')
    await flushPromises()
    expect(mocks.executeDeliveryIncidentCommand).toHaveBeenCalledWith('h5', 'publication-h5-1', {
      action: 'requeue',
      reason: '确认 Redis 临时故障已恢复',
      expected_row_version: 6,
    })
    expect(mocks.listDeliveryIncidents).toHaveBeenCalledTimes(2)
    expect(mocks.message.success).toHaveBeenCalledWith('已重新投递')
  })

  it('Web 任务即使具备系统运维权限也不展示或请求 H5/App 投递事故', async () => {
    mocks.listDeliveryIncidents.mockResolvedValue([incident])

    const wrapper = mount(DeliveryIncidentPanel, {
      props: { taskId: 'task-web-1', targetPlatform: 'web' },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="delivery-incident-panel"]').exists()).toBe(false)
    expect(mocks.listDeliveryIncidents).not.toHaveBeenCalled()
  })
})
