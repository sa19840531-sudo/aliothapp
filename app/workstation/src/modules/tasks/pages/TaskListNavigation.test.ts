import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router'
import TaskListView from './TaskListView.vue'

const mocks = vi.hoisted(() => ({
  list: vi.fn(), products: vi.fn(), operation: vi.fn(),
  message: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
}))
vi.mock('@/services/tasks.api', () => ({
  listTasks: mocks.list, performTaskOperation: mocks.operation, deleteTask: vi.fn(),
}))
vi.mock('@/services/products.api', () => ({ listWorkstationProducts: mocks.products }))
vi.mock('naive-ui', () => ({ useMessage: () => mocks.message, NPopconfirm: { template: '<div><slot name="trigger" /></div>' } }))

const detail = { template: '<div data-testid="controlled-detail">受控详情</div>' }
function response(name = '受控任务') {
  return { items: [{
    task_id: 'controlled', task_name: name, target_platform: 'h5',
    execution_mode: 'full_inspection', target_url: 'https://controlled.example.test',
    product: { department_name: '受控部门', product_code: 'test', product_name: '受控产品' },
    status: 'running', progress: 55, report_status: 'not_started', report_availability: 'none',
    available_actions: ['detail', 'cancel'], created_at: '2026-10-17T00:00:00Z',
  }], total: 27, page: 2, page_size: 20 }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
let wrapper: VueWrapper | undefined
let router: Router
let events: Array<{ path: string; failed: boolean }>
async function setup(loader = () => Promise.resolve(detail), base = '/tasks') {
  router = createRouter({ history: createMemoryHistory(), routes: [
    { path: base, component: TaskListView },
    { path: `${base}/:taskId`, component: loader },
  ] })
  events = []
  router.afterEach((to, _, failure) => events.push({ path: to.path, failed: Boolean(failure) }))
  await router.push(`${base}?page=2&page_size=20&submitter_name=原筛选`)
  wrapper = mount(RouterView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  mocks.list.mockResolvedValue(response())
  mocks.products.mockResolvedValue({ items: [] })
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.clearAllTimers()
  vi.useRealTimers()
})

it.each(['/tasks', '/admin/tasks'])('稳定复现：%s详情懒加载等待期间，300ms筛选刷新不能取消用户导航', async (base) => {
  const module = deferred<typeof detail>()
  const loader = vi.fn(() => module.promise)
  const view = await setup(loader, base)
  await view.get('[data-testid="task-filter-submitter"]').setValue('新筛选')
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  expect(loader).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(300)
  module.resolve(detail)
  await flushPromises()
  expect(router.currentRoute.value.path).toBe(`${base}/controlled`)
  expect(router.currentRoute.value.query).toEqual({ page: '2', page_size: '20', submitter_name: '新筛选' })
  expect(events.filter((event) => event.path === `${base}/controlled` && event.failed)).toEqual([])
  expect(mocks.list).toHaveBeenCalledTimes(1)
})

it('稳定复现：卸载后迟到的轮询结果不能复活8秒轮询', async () => {
  const pending = deferred<ReturnType<typeof response>>()
  const view = await setup()
  mocks.list.mockReturnValueOnce(pending.promise)
  await vi.advanceTimersByTimeAsync(8000)
  expect(mocks.list).toHaveBeenCalledTimes(2)
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/tasks/controlled')
  pending.resolve(response('迟到的旧轮询'))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(16000)
  expect(mocks.list).toHaveBeenCalledTimes(2)
})

it('反例：留在列表时正常筛选仍刷新URL、回第一页并保留轮询', async () => {
  const view = await setup()
  await view.get('[data-testid="task-filter-submitter"]').setValue('新筛选')
  await vi.advanceTimersByTimeAsync(300)
  await flushPromises()
  expect(router.currentRoute.value.query).toEqual({ page: '1', page_size: '20', submitter_name: '新筛选' })
  expect(mocks.list).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, submitter_name: '新筛选' }))
  await vi.advanceTimersByTimeAsync(8000)
  expect(mocks.list).toHaveBeenCalledTimes(3)
})

it('反例：守卫拒绝详情导航后，列表筛选和轮询不能永久暂停', async () => {
  const view = await setup()
  router.beforeEach((to) => to.path === '/tasks/controlled' ? false : true)
  await view.get('[data-testid="task-filter-submitter"]').setValue('新筛选')
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  await vi.advanceTimersByTimeAsync(300)
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/tasks')
  expect(router.currentRoute.value.query.submitter_name).toBe('新筛选')
  expect(mocks.list).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(8000)
  expect(mocks.list).toHaveBeenCalledTimes(3)
})

it('反例：详情模块真实加载失败必须提示并恢复列表，不自动重试或留下未处理拒绝', async () => {
  const module = deferred<typeof detail>()
  const loader = vi.fn(() => module.promise)
  const view = await setup(loader)
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  module.reject(new Error('受控模块加载失败'))
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/tasks')
  expect(mocks.message.error).toHaveBeenCalledWith('页面加载失败，未离开任务列表，请稍后重试')
  expect(loader).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(8000)
  expect(mocks.list).toHaveBeenCalledTimes(3)
  expect(loader).toHaveBeenCalledTimes(1)
})

it('反例：旧导航被新导航取代后，旧失败不能恢复列表抢走最新目的地', async () => {
  const first = deferred<typeof detail>()
  const second = deferred<typeof detail>()
  const loader = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const view = await setup(loader)
  await view.get('[data-testid="task-filter-submitter"]').setValue('新筛选')
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  const newer = router.push('/tasks/latest?page=2&page_size=20&submitter_name=新筛选')
  await flushPromises()
  first.resolve(detail)
  await flushPromises()
  await vi.advanceTimersByTimeAsync(8000)
  expect(mocks.list).toHaveBeenCalledTimes(1)
  second.resolve(detail)
  await newer
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/tasks/latest')
  expect(mocks.message.error).not.toHaveBeenCalled()
})

it('反例：同一任务连续点击也须区分导航实例，旧模块错误不能解除新导航暂停', async () => {
  const first = deferred<typeof detail>()
  const second = deferred<typeof detail>()
  const loader = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const view = await setup(loader)
  await view.get('[data-testid="task-filter-submitter"]').setValue('新筛选')
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  await view.get('tbody .task-name-link').trigger('click')
  await flushPromises()
  expect(loader).toHaveBeenCalledTimes(2)
  first.reject(new Error('旧导航受控错误'))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(300)
  expect(mocks.list).toHaveBeenCalledTimes(1)
  expect(mocks.message.error).not.toHaveBeenCalled()
  second.resolve(detail)
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/tasks/controlled')
})

it('稳定复现：旧筛选迟到不能覆盖新查询', async () => {
  const old = deferred<ReturnType<typeof response>>()
  const view = await setup()
  mocks.list.mockReturnValueOnce(old.promise).mockResolvedValueOnce(response('新查询任务'))
  await view.get('[data-testid="task-filter-submitter"]').setValue('旧查询')
  await vi.advanceTimersByTimeAsync(300)
  await view.get('[data-testid="task-filter-submitter"]').setValue('新查询')
  await vi.advanceTimersByTimeAsync(300)
  await flushPromises()
  expect(view.text()).toContain('新查询任务')
  old.resolve(response('过时任务'))
  await flushPromises()
  expect(view.text()).toContain('新查询任务')
  expect(view.text()).not.toContain('过时任务')
})
