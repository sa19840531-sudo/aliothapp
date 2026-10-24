import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'
import type { SystemStatus } from '@/services/system.api'
import type { LocationQuery } from 'vue-router'

enableAutoUnmount(afterEach)

const mocks = vi.hoisted(() => ({
  listTasks: vi.fn(),
  performTaskOperation: vi.fn(),
  routerPush: vi.fn(),
  routerReplace: vi.fn(),
  deleteTask: vi.fn(),
  route: { path: '/tasks', query: {} as LocationQuery },
  listWorkstationProducts: vi.fn(),
  getSystemStatus: vi.fn(),
}))

vi.mock('vue-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('vue-router')>(),
  useRoute: () => mocks.route,
  onBeforeRouteLeave: vi.fn(),
  useRouter: () => ({
    push: mocks.routerPush, replace: mocks.routerReplace,
    afterEach: () => () => {}, onError: () => () => {},
  }),
}))
vi.mock('naive-ui', async () => {
  const { defineComponent, h, ref } = await import('vue')
  return {
    NPopconfirm: defineComponent({
      emits: ['positive-click'],
      setup(_, { slots, emit }) {
        const showing = ref(false)
        return () => h('div', [
          h('div', { onClick: () => { showing.value = true } }, slots.trigger?.()),
          showing.value ? h('div', [slots.default?.(), h('button', { onClick: () => emit('positive-click') }, '确认删除')]) : null,
        ])
      },
    }),
    useMessage: () => ({ error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() }),
  }
})
vi.mock('@/services/tasks.api', () => ({
  listTasks: mocks.listTasks,
  performTaskOperation: mocks.performTaskOperation,
  deleteTask: mocks.deleteTask,
}))
vi.mock('@/services/system.api', () => ({ getSystemStatus: mocks.getSystemStatus }))
vi.mock('@/services/products.api', () => ({ listWorkstationProducts: mocks.listWorkstationProducts }))

import TaskListView from './TaskListView.vue'

function grantSystemStatusPermission() {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: {
      id: 'u1',
      username: 'admin@group.com',
      full_name: '运营中心管理员',
      role: '平台管理员',
      status: 'enabled',
      permissions: ['systemOperations'],
      tenant_id: 't1',
      tenant_name: '体验中心',
    },
  }
}

function pendingSystemStatus() {
  let resolve!: (status: SystemStatus) => void
  let reject!: (error: Error) => void
  const promise = new Promise<SystemStatus>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

const healthySystemStatus: SystemStatus = {
  worker: { alive: true, active_instances: 2, last_heartbeat_at: null },
  queue: { depth: 0, redis_available: true },
  queued_tasks: { count: 0, oldest_created_at: null, oldest_waiting_seconds: null },
  generated_at: '2026-09-23T00:00:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getSystemStatus.mockReset()
  mocks.route.path = '/tasks'
  mocks.route.query = {}
  authStore.session.value = null
  mocks.listWorkstationProducts.mockResolvedValue({
    items: [{ project_code: 'H5-1', product_name: '移动站点', h5_url: 'https://example.test' }],
  })
  mocks.listTasks.mockResolvedValue({
    items: [
      {
        task_id: 'task-h5',
        task_name: 'H5 仅探索',
        product: { department_name: '质检部', product_code: 'H5-1', product_name: '移动站点' },
        target_url: 'https://example.test',
        target_platform: 'h5',
        execution_mode: 'exploration_only',
        journey_steps: [],
        status: 'done',
        progress: 100,
        report_status: 'not_applicable',
        failure_reason: null,
        available_actions: ['detail', 'exploration', 'evidence', 'rerun', 'copy'],
        created_at: '2026-08-13T00:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    page_size: 20,
  })
})

describe('任务列表固定操作入口', () => {
  it.each(['queued', 'failed', 'canceled'])('%s任务点击名称进入详情并保留分页', async (status) => {
    mocks.route.query = { page: '2', page_size: '20' }
    mocks.listTasks.mockResolvedValueOnce({ items: [{
      task_id: 'task-read', task_name: '异常任务入口', status, progress: 0,
      product: { department_name: '质检部', product_code: 'P', product_name: '产品' },
      target_platform: 'h5', execution_mode: 'full_inspection', available_actions: ['copy'],
      report_status: 'not_started', created_at: '2026-09-30T01:00:00Z',
    }], total: 21, page: 2, page_size: 20 })
    const wrapper = mount(TaskListView)
    await flushPromises()
    await wrapper.get('.task-name-link').trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith({ path: '/tasks/task-read', query: expect.objectContaining({ page: '2', page_size: '20' }) })
    expect(mocks.performTaskOperation).not.toHaveBeenCalled()
  })
})

describe('部门平台任务列表', () => {
  it('不展示公司和部门，产品按下拉的产品编码筛选', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(TaskListView)
      await flushPromises()

      const headers = wrapper.findAll('th').map((node) => node.text())
      expect(headers).not.toContain('所属公司')
      expect(headers).not.toContain('部门')
      expect(wrapper.find('[data-testid="task-filters"]').text()).not.toContain('所属公司')

      await wrapper.find('[data-testid="task-filter-product"]').setValue('H5-1')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(mocks.listTasks).toHaveBeenLastCalledWith(expect.objectContaining({ product_code: 'H5-1', page: 1 }))
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('任务列表布局和返回状态', () => {
  it.each(['h5', 'web', 'app'])('轮询到%s任务取消时，旧生成状态显示未生成', async (platform) => {
    vi.useFakeTimers()
    try {
      const response = await mocks.listTasks()
      Object.assign(response.items[0], {
        target_platform: platform,
        execution_mode: 'full_inspection',
        status: 'running',
        report_status: 'generating',
        report_availability: 'none',
      })
      const canceled = structuredClone(response)
      canceled.items[0].status = 'canceled'
      mocks.listTasks.mockClear()
      mocks.listTasks.mockResolvedValueOnce(response).mockResolvedValue(canceled)
      const wrapper = mount(TaskListView)
      await flushPromises()
      expect(wrapper.get('tbody tr').text()).toContain('生成中')

      await vi.advanceTimersByTimeAsync(8000)
      await flushPromises()
      const row = wrapper.get('tbody tr')
      expect(row.text()).toContain('已取消')
      expect(row.text()).toContain('未生成')
      expect(row.text()).not.toContain('生成中')
      expect(row.get('td:nth-last-child(2) .qc-chip').classes()).toContain('tone-muted')
    } finally {
      vi.useRealTimers()
    }
  })

  it.each([
    ['not_applicable', 'none', '未生成'],
    ['not_started', 'none', '未生成'],
    ['ready', 'none', '已生成'],
    ['failed', 'none', '生成失败'],
    ['generating', 'preview', '报告预览'],
    ['generating', 'final', '正式报告'],
  ])('取消任务的报告%s/%s显示%s', async (status, availability, label) => {
    const response = await mocks.listTasks()
    Object.assign(response.items[0], {
      execution_mode: 'full_inspection',
      status: 'canceled',
      report_status: status,
      report_availability: availability,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()
    expect(wrapper.get('tbody tr td:nth-last-child(2)').text()).toBe(label)
  })

  it('取消请求与任务终态并发时刷新列表，移除旧取消入口', async () => {
    const response = await mocks.listTasks()
    response.items[0].status = 'running'
    response.items[0].available_actions = ['detail', 'cancel']
    const stopped = structuredClone(response)
    stopped.items[0].status = 'failed'
    stopped.items[0].available_actions = ['failure', 'rerun', 'copy']
    mocks.listTasks.mockClear()
    mocks.listTasks.mockResolvedValueOnce(response).mockResolvedValue(stopped)
    mocks.performTaskOperation.mockRejectedValueOnce(new Error('任务运行记录不存在或已进入终态'))
    const wrapper = mount(TaskListView)
    await flushPromises()
    await wrapper.get('tbody .actions').findAll('button').find((button) => button.text() === '取消')!.trigger('click')
    await flushPromises()
    expect(mocks.performTaskOperation).toHaveBeenCalledWith('task-h5', 'cancel')
    expect(mocks.listTasks).toHaveBeenCalledTimes(2)
    expect(wrapper.get('tbody .actions').text()).not.toContain('取消')
    expect(wrapper.text()).toContain('执行失败')
  })

  it('列表从 URL 恢复第 3 页、每页数量和发起人筛选，进入详情继续携带状态', async () => {
    mocks.route.query = { page: '3', page_size: '10', product_code: 'H5-1', submitter_name: '张三', status: 'done' }
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(mocks.listTasks).toHaveBeenCalledWith(expect.objectContaining({
      page: 3, page_size: 10, product_code: 'H5-1', submitter_name: '张三', status: 'done',
    }))
    expect(wrapper.get<HTMLInputElement>('[data-testid="task-filter-submitter"]').element.value).toBe('张三')
    await wrapper.findAll('button').find((button) => button.text() === '查看详情')!.trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith({ path: '/tasks/task-h5', query: mocks.route.query })
  })

  it('选择、任务名和操作使用固定列，取消操作在其他动作之后', async () => {
    const response = await mocks.listTasks()
    response.items[0].status = 'running'
    response.items[0].available_actions = ['cancel', 'detail', 'report']
    mocks.listTasks.mockResolvedValue(response)
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(wrapper.findAll('th').filter((cell) => cell.classes().some((name) => ['check-col', 'name-col', 'action-col'].includes(name)))).toHaveLength(3)
    const buttons = wrapper.get('tbody .actions').findAll('button').map((button) => button.text())
    expect(buttons).toEqual(['查看详情', '查看报告', '取消'])
  })

  it('查询发起人时回到第一页并更新 URL，避免详情返回到旧筛选页码', async () => {
    vi.useFakeTimers()
    try {
      mocks.route.query = { page: '3' }
      const wrapper = mount(TaskListView)
      await flushPromises()
      await wrapper.get('[data-testid="task-filter-submitter"]').setValue(' 李四 ')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(mocks.listTasks).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, submitter_name: '李四' }))
      expect(mocks.routerReplace).toHaveBeenLastCalledWith({ path: '/tasks', query: { page: '1', page_size: '20', submitter_name: '李四' } })
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('管理端任务删除', () => {
  it('重复动作只渲染一次，保留原动作数组且删除仍须单项确认', async () => {
    mocks.route.path = '/admin/tasks'
    const response = await mocks.listTasks()
    const availableActions = ['delete', 'detail', 'delete', 'copy', 'detail', 'delete']
    response.items[0].available_actions = availableActions
    mocks.listTasks.mockResolvedValue(response)
    mocks.deleteTask.mockResolvedValue({ task_id: 'task-h5', status: 'done', run_id: null, message: '任务已删除' })
    const wrapper = mount(TaskListView)
    await flushPromises()

    const buttons = wrapper.get('tbody .actions').findAll('button')
    expect(buttons.map((button) => button.text())).toEqual(['查看详情', '复制任务', '删除'])
    expect(availableActions).toEqual(['delete', 'detail', 'delete', 'copy', 'detail', 'delete'])
    await buttons.find((button) => button.text() === '删除')!.trigger('click')
    expect(mocks.deleteTask).not.toHaveBeenCalled()
    expect(wrapper.findAll('button').filter((button) => button.text() === '确认删除')).toHaveLength(1)
    await wrapper.findAll('button').find((button) => button.text() === '确认删除')!.trigger('click')
    await flushPromises()
    expect(mocks.deleteTask).toHaveBeenCalledTimes(1)
    expect(mocks.deleteTask).toHaveBeenCalledWith('task-h5')
  })

  it('后端允许删除的终态任务经单项确认后才调用删除接口，并退回非空页', async () => {
    mocks.route.path = '/admin/tasks'
    mocks.route.query = { page: '2' }
    const response = await mocks.listTasks()
    response.items[0].available_actions = ['detail', 'delete']
    mocks.listTasks.mockResolvedValue(response)
    mocks.deleteTask.mockResolvedValue({ task_id: 'task-h5', status: 'done', run_id: null, message: '任务已删除' })
    const wrapper = mount(TaskListView)
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text() === '删除')!.trigger('click')
    expect(mocks.deleteTask).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('已有报告、证据和审计记录仍会保留')
    await wrapper.findAll('button').find((button) => button.text() === '确认删除')!.trigger('click')
    await flushPromises()
    expect(mocks.deleteTask).toHaveBeenCalledWith('task-h5')
    expect(mocks.listTasks).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1 }))
    expect(mocks.routerReplace).toHaveBeenLastCalledWith({ path: '/admin/tasks', query: { page: '1', page_size: '20' } })
  })

  it.each(['/tasks', '/admin/tasks'])('执行中任务在 %s 隐藏删除操作', async (path) => {
    mocks.route.path = path
    const response = await mocks.listTasks()
    response.items[0].status = 'running'
    response.items[0].available_actions = ['detail', 'delete', 'delete']
    mocks.listTasks.mockResolvedValue(response)
    const wrapper = mount(TaskListView)
    await flushPromises()
    expect(wrapper.findAll('button').some((button) => button.text() === '删除')).toBe(false)
  })

  it('工作台即便拿到 delete 动作也不显示管理端删除入口', async () => {
    const response = await mocks.listTasks()
    response.items[0].available_actions = ['detail', 'delete', 'delete']
    mocks.listTasks.mockResolvedValue(response)
    const wrapper = mount(TaskListView)
    await flushPromises()
    expect(wrapper.findAll('button').some((button) => button.text() === '删除')).toBe(false)
  })
})

describe('队列与服务卡片', () => {
  afterEach(() => {
    authStore.session.value = null
  })

  it('读不到服务状态的账号不请求，也不显示预计等待和服务状态两张卡', async () => {
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(mocks.getSystemStatus).not.toHaveBeenCalled()
    const stats = wrapper.get('[data-testid="task-stats"]')
    expect(stats.findAll('.stat-card')).toHaveLength(2)
    expect(stats.text()).toContain('当前排队')
    expect(stats.text()).toContain('执行中')
    expect(stats.text()).not.toContain('服务状态')
    expect(stats.text()).not.toContain('状态未知')
  })

  it('平台管理员照常看到四张卡和执行服务状态', async () => {
    grantSystemStatusPermission()
    mocks.getSystemStatus.mockResolvedValue(healthySystemStatus)
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(mocks.getSystemStatus).toHaveBeenCalled()
    const stats = wrapper.get('[data-testid="task-stats"]')
    expect(stats.findAll('.stat-card')).toHaveLength(4)
    expect(stats.text()).toContain('运行正常')
  })

  it('服务状态仍在等待时先渲染任务，随后更新离线提示', async () => {
    grantSystemStatusPermission()
    const status = pendingSystemStatus()
    mocks.getSystemStatus.mockReturnValue(status.promise)
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(mocks.getSystemStatus).toHaveBeenCalledTimes(1)
    expect(wrapper.get('tbody').text()).toContain('H5 仅探索')
    expect(wrapper.get('tbody').text()).not.toContain('加载中…')
    expect(wrapper.get('[data-testid="task-stats"]').text()).toContain('状态未知')
    expect(wrapper.find('.offline-banner').exists()).toBe(false)

    status.resolve({
      ...healthySystemStatus,
      worker: { alive: false, active_instances: 0, last_heartbeat_at: null },
    })
    await flushPromises()

    expect(wrapper.get('tbody').text()).toContain('H5 仅探索')
    expect(wrapper.get('[data-testid="task-stats"]').text()).toContain('执行服务离线')
    expect(wrapper.get('.offline-banner').text()).toContain('质检 Worker 未运行')
  })

  it('服务状态失败只影响服务卡片，任务列表仍可查看和操作', async () => {
    grantSystemStatusPermission()
    const status = pendingSystemStatus()
    mocks.getSystemStatus.mockReturnValue(status.promise)
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(wrapper.get('tbody').text()).toContain('H5 仅探索')
    status.reject(new Error('系统状态接口超时'))
    await flushPromises()

    expect(wrapper.get('tbody').text()).toContain('H5 仅探索')
    expect(wrapper.find('tbody .qc-error').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('系统状态接口超时')
    expect(wrapper.get('[data-testid="task-stats"]').text()).toContain('无法读取服务状态')
    await wrapper.findAll('button').find((button) => button.text() === '查看详情')!.trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith({
      path: '/tasks/task-h5', query: { page: '1', page_size: '20' },
    })
  })

  it('等待服务状态期间保持八秒任务轮询，不重复并发请求服务状态', async () => {
    vi.useFakeTimers()
    try {
      grantSystemStatusPermission()
      const response = await mocks.listTasks()
      response.items[0].status = 'running'
      mocks.listTasks.mockClear()
      mocks.listTasks.mockResolvedValue(response)
      const status = pendingSystemStatus()
      mocks.getSystemStatus.mockReturnValueOnce(status.promise).mockResolvedValue(healthySystemStatus)
      const wrapper = mount(TaskListView)
      await flushPromises()

      expect(mocks.listTasks).toHaveBeenCalledTimes(1)
      expect(wrapper.get('tbody').text()).toContain('H5 仅探索')
      await vi.advanceTimersByTimeAsync(8000)
      await flushPromises()
      expect(mocks.listTasks).toHaveBeenCalledTimes(2)
      expect(mocks.getSystemStatus).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(8000)
      await flushPromises()
      expect(mocks.listTasks).toHaveBeenCalledTimes(3)
      expect(mocks.getSystemStatus).toHaveBeenCalledTimes(1)

      status.resolve(healthySystemStatus)
      await flushPromises()
      expect(wrapper.get('[data-testid="task-stats"]').text()).toContain('运行正常')
      await vi.advanceTimersByTimeAsync(8000)
      await flushPromises()
      expect(mocks.listTasks).toHaveBeenCalledTimes(4)
      expect(mocks.getSystemStatus).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('H5 仅探索任务列表消费', () => {
  it('显示报告不适用并提供探索和证据动作', async () => {
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(wrapper.text()).toContain('不适用')
    expect(wrapper.text()).not.toContain('未生成')
    expect(wrapper.text()).toContain('查看探索结果')
    expect(wrapper.text()).toContain('下载证据')
  })

  it('Web V2 预览报告在任务列表显示入口并跳转到当前任务', async () => {
    mocks.listTasks.mockResolvedValue({
      items: [
        {
          task_id: 'task-web-preview',
          task_name: 'Web 预览报告',
          product: { department_name: '质检部', product_code: 'WEB-1', product_name: 'Web 站点' },
          target_url: 'https://example.test',
          target_platform: 'web',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'review',
          progress: 100,
          report_status: 'ready',
          report_availability: 'preview',
          failure_reason: null,
          available_actions: ['detail', 'report'],
          created_at: '2026-09-05T00:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()

    const buttons = wrapper.findAll('button')
    const target = buttons.find((button) => button.text().includes('查看报告（预览）'))
    expect(target).toBeDefined()
    expect(wrapper.text()).toContain('报告预览')
    expect(wrapper.text()).not.toContain('已生成')
    await target!.trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith({
      path: '/reports',
      query: { task: 'task-web-preview', page: '1', page_size: '20' },
    })
  })

  it('Web V2 尚无持久化事实时显示禁用的报告入口和原因', async () => {
    mocks.listTasks.mockResolvedValue({
      items: [
        {
          task_id: 'task-web-none',
          task_name: 'Web 尚无报告事实',
          product: { department_name: '质检部', product_code: 'WEB-0', product_name: 'Web 站点' },
          target_url: 'https://example.test',
          target_platform: 'web',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'queued',
          progress: 0,
          report_status: 'not_started',
          report_availability: 'none',
          failure_reason: null,
          available_actions: ['detail'],
          created_at: '2026-09-05T00:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()

    const target = wrapper.findAll('button').find((button) => button.text() === '查看报告')
    expect(target).toBeDefined()
    expect(target!.attributes('disabled')).toBeDefined()
    expect(target!.attributes('title')).toContain('尚未产生可信采集')
  })

  it('H5 V2 复核报告在任务列表使用复核文案而不是 Web 预览文案', async () => {
    mocks.listTasks.mockResolvedValue({
      items: [
        {
          task_id: 'task-h5-review',
          task_name: 'H5 复核报告',
          product: { department_name: '质检部', product_code: 'H5-1', product_name: 'H5 站点' },
          target_url: 'https://example.test/h5',
          target_platform: 'h5',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'review',
          progress: 100,
          report_status: 'ready',
          report_availability: 'preview',
          failure_reason: null,
          available_actions: ['detail', 'report'],
          created_at: '2026-09-05T00:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(wrapper.text()).toContain('查看报告（复核）')
    expect(wrapper.text()).toContain('报告预览')
    expect(wrapper.text()).not.toContain('查看报告（预览）')
  })

  it('正式报告在列表中与复核预览明确区分', async () => {
    mocks.listTasks.mockResolvedValue({
      items: [
        {
          task_id: 'task-h5-final',
          task_name: 'H5 正式报告',
          product: { department_name: '质检部', product_code: 'H5-2', product_name: 'H5 站点' },
          target_url: 'https://example.test/h5',
          target_platform: 'h5',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'done',
          progress: 100,
          report_status: 'ready',
          report_availability: 'final',
          failure_reason: null,
          available_actions: ['detail', 'report'],
          created_at: '2026-09-05T00:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(wrapper.text()).toContain('正式报告')
    expect(wrapper.text()).not.toContain('报告预览')
  })

  it('App 预览报告继续沿用原预览文案', async () => {
    mocks.listTasks.mockResolvedValue({
      items: [
        {
          task_id: 'task-app-preview',
          task_name: 'App 预览报告',
          product: { department_name: '质检部', product_code: 'APP-1', product_name: 'App' },
          target_url: 'app://example',
          target_platform: 'app',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'review',
          progress: 100,
          report_status: 'ready',
          report_availability: 'preview',
          failure_reason: null,
          available_actions: ['detail', 'report'],
          created_at: '2026-09-05T00:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()

    expect(wrapper.text()).toContain('查看报告（预览）')
    expect(wrapper.text()).not.toContain('查看报告（复核）')
  })

  it('管理员可从列表显式恢复被隔离的正式报告收口', async () => {
    mocks.performTaskOperation.mockResolvedValue({ message: '正式报告收口重试已恢复' })
    mocks.listTasks.mockResolvedValue({
      items: [
        {
          task_id: 'task-web-quarantined',
          task_name: 'Web 收口隔离',
          product: { department_name: '质检部', product_code: 'WEB-2', product_name: 'Web 站点' },
          target_url: 'https://example.test',
          target_platform: 'web',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'review',
          progress: 99,
          report_status: 'ready',
          report_availability: 'preview',
          failure_reason: 'RuntimeError: report binding failed',
          available_actions: ['detail', 'report', 'retry-finalization'],
          created_at: '2026-09-06T00:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(TaskListView)
    await flushPromises()

    const retry = wrapper.findAll('button').find((button) => button.text().includes('恢复报告收口'))
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await flushPromises()

    expect(mocks.performTaskOperation).toHaveBeenCalledWith(
      'task-web-quarantined',
      'retry-finalization',
    )
  })
})
