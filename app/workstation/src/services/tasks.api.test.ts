import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureHttpClient } from './http'
import { deleteTask, listTasks, submitExplorationTask, submitV2Task } from './tasks.api'

function jsonResponse(payload: unknown, status = 202) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const commonFields = {
  task_name: 'V2 任务入口映射',
  ruleset_code: 'alioth_first_ruleset',
  ruleset_version: 1,
  channel: 'workbench' as const,
  priority: 100,
  product: {
    department_name: '鼎丰',
    product_code: 'WEB-001',
    product_name: '统一入口样例',
  },
  scan_mode: 'full' as const,
  journey_steps: ['首页', '详情'],
  remark: 'V2 请求映射测试',
}

const appExplorationTarget = {
  required_page_types: ['home', 'content_detail'],
  milestones: ['journey:首页', 'journey:详情'],
  minimum_business_pages: 2,
  device_capability_template: 'android_visual_readonly_v1' as const,
}

afterEach(() => {
  configureHttpClient({})
  globalThis.sessionStorage?.clear()
  vi.unstubAllGlobals()
})

describe('V2 任务请求映射', () => {
  it.each([
    {
      name: 'H5',
      target: { platform: 'h5' as const, url: 'https://example.test/h5' },
    },
    {
      name: 'Web',
      target: {
        platform: 'web' as const,
        url: 'https://example.test/web',
        browser_profile_id: 'desktop_fhd_1920x1080_v1',
      },
    },
    {
      name: 'App build_ref',
      target: {
        platform: 'app' as const,
        source: {
          type: 'build_ref' as const,
          platform_build_ref: 'app-build:00000000-0000-0000-0000-000000000001',
        },
        package_name: 'com.example.alioth',
        device: { backend: 'cloud_phone', profile_id: 'pixel-7' },
        exploration_target: appExplorationTarget,
      },
    },
    {
      name: 'App download_url',
      target: {
        platform: 'app' as const,
        source: {
          type: 'download_url' as const,
          download_url: 'https://example.test/builds/alioth.apk',
        },
        package_name: 'com.example.alioth',
        device: { backend: 'cloud_phone', profile_id: 'pixel-7' },
        exploration_target: appExplorationTarget,
      },
    },
  ])('$name 只发送对应 target 字段并 POST 到 V2 入口', async ({ target }) => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        task_id: 'task-v2',
        run_id: 'run-v2',
        ruleset_id: 'ruleset-v2',
        status: 'queued',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const payload = { ...commonFields, target }
    await submitV2Task(payload)

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v2/tasks')
    expect(init.method).toBe('POST')
    const headers = new Headers(init.headers)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('Idempotency-Key')).toBeTruthy()
    expect(JSON.parse(String(init.body))).toEqual(payload)
    expect(Object.keys(JSON.parse(String(init.body)).target)).toEqual(Object.keys(target))
  })

  it('H5 仅探索任务 POST 到独立 exploration-tasks 入口', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        task_id: 'task-exploration',
        run_id: 'run-exploration',
        status: 'queued',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const payload = {
      ...commonFields,
      target: { platform: 'h5' as const, url: 'https://example.test/h5' },
    }

    await submitExplorationTask(payload)

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v2/exploration-tasks')
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBeTruthy()
    expect(JSON.parse(String(init.body))).toEqual(payload)
  })

  it('响应丢失并刷新模块后相同载荷重试复用 Idempotency-Key', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('network timeout'))
      .mockResolvedValueOnce(
        jsonResponse({
          task_id: 'task-replayed',
          run_id: 'run-replayed',
          ruleset_id: 'ruleset-v2',
          status: 'queued',
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'stable-request-key') })
    const payload = {
      ...commonFields,
      target: { platform: 'h5' as const, url: 'https://example.test/h5' },
    }

    await expect(submitV2Task(payload)).rejects.toThrow('network timeout')
    expect(Array.from({ length: sessionStorage.length }, (_, index) => sessionStorage.key(index)))
      .toContainEqual(expect.stringMatching(/^alioth:pending-mutation:/))
    vi.resetModules()
    const reloadedTasksApi = await import('./tasks.api')
    await reloadedTasksApi.submitV2Task(payload)

    const firstHeaders = new Headers((fetchMock.mock.calls[0] as [string, RequestInit])[1].headers)
    const secondHeaders = new Headers((fetchMock.mock.calls[1] as [string, RequestInit])[1].headers)
    expect(firstHeaders.get('Idempotency-Key')).toBe('stable-request-key')
    expect(secondHeaders.get('Idempotency-Key')).toBe('stable-request-key')
  })
})

describe('任务管理请求契约', () => {
  it('发起人筛选传给列表接口，中文与特殊字符正确编码', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], total: 0, page: 2, page_size: 10 }, 200))
    vi.stubGlobal('fetch', fetchMock)
    await listTasks({ submitter_name: '张三 & 李四', page: 2, page_size: 10 })
    const query = new URL(String(fetchMock.mock.calls[0]![0]), 'http://localhost').searchParams
    expect(query.get('submitter_name')).toBe('张三 & 李四')
    expect(query.get('page')).toBe('2')
  })

  it('单项任务删除使用现有任务资源 DELETE 路径', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ task_id: 'task-1', status: 'done', run_id: null, message: '任务已删除' }, 200))
    vi.stubGlobal('fetch', fetchMock)
    await expect(deleteTask('task-1')).resolves.toMatchObject({ task_id: 'task-1' })
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/workstation/tasks/task-1', expect.objectContaining({ method: 'DELETE' }))
  })
})
