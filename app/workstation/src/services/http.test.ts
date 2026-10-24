import { afterEach, describe, expect, it, vi } from 'vitest'
import { AliothHttpError, configureHttpClient, downloadFile, requestJson } from './http'

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  configureHttpClient({})
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('工作台 HTTP 客户端', () => {
  it('自动注入登录令牌并保留调用方请求头', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    configureHttpClient({ getToken: () => 'workstation-token' })

    await requestJson('/tasks', {
      headers: { 'X-Request-Source': 'workstation-test' },
    })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toBe('Bearer workstation-token')
    expect(headers.get('X-Request-Source')).toBe('workstation-test')
  })

  it('遇到 401 时触发退出处理并保留后端错误信息', async () => {
    const onUnauthorized = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: 'UNAUTHORIZED', message: '登录状态已失效' } }, 401),
      ),
    )
    configureHttpClient({ onUnauthorized })

    const request = requestJson('/tasks')

    await expect(request).rejects.toBeInstanceOf(AliothHttpError)
    await expect(request).rejects.toMatchObject({
      status: 401,
      message: '登录状态已失效',
    })
    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('删除成功返回 204 时即使带着 JSON 头也按成功处理，不去解析空内容', async () => {
    // 后端框架给 204 也带 application/json 头但没有内容；以前这里会抛解析错误，页面把删除成功当成失败
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(null, { status: 204, headers: { 'Content-Type': 'application/json' } }),
      ),
    )

    await expect(requestJson('/users/user-1', { method: 'DELETE' })).resolves.toBeUndefined()
  })

  it('multipart 上传不强制 Content-Type 并注入令牌', async () => {
    const { requestForm } = await import('./http')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    configureHttpClient({ getToken: () => 'form-token' })

    const form = new FormData()
    form.append('file', new Blob(['apk']), 'demo.apk')
    await requestForm('/app-builds', form)

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toBe('Bearer form-token')
    expect(headers.get('Content-Type')).toBeNull()
    expect(init.body).toBe(form)
  })

  it('文件下载通过 Bearer 头授权且不把长期令牌写进 URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('report', {
        status: 200,
        headers: { 'Content-Disposition': "attachment; filename*=UTF-8''report.html" },
      }),
    )
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:test-report'),
      revokeObjectURL: vi.fn(),
    })
    configureHttpClient({ getToken: () => 'download-token' })

    await downloadFile('/workstation/reports/task-1/export', 'fallback.html')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = new Headers(init.headers)
    expect(url).toBe('/api/v1/workstation/reports/task-1/export')
    expect(url).not.toContain('download-token')
    expect(headers.get('Authorization')).toBe('Bearer download-token')
    expect(click).toHaveBeenCalledOnce()
  })

  it('取二进制内容（截图）走 Bearer 头，出错时带回后端原话', async () => {
    const { requestBlob } = await import('./http')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('png-bytes', { status: 200 }))
      .mockResolvedValueOnce(jsonResponse({ error: { message: '截图文件已经不在了' } }, 404))
    vi.stubGlobal('fetch', fetchMock)
    configureHttpClient({ getToken: () => 'blob-token' })

    const blob = await requestBlob('/rule-tests/records/r-1/screenshots/1')

    expect(await blob.text()).toBe('png-bytes')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/rule-tests/records/r-1/screenshots/1')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer blob-token')
    await expect(requestBlob('/rule-tests/records/r-1/screenshots/2')).rejects.toMatchObject({
      status: 404,
      message: '截图文件已经不在了',
    })
  })
})
