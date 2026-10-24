export class AliothHttpError extends Error {
  status: number
  details: unknown

  constructor(status: number, message: string, details?: unknown) {
    super(message)
    this.name = 'AliothHttpError'
    this.status = status
    this.details = details
  }
}

const apiBaseUrl = import.meta.env.VITE_ALIOTH_API_BASE_URL || '/api/v1'
const apiV2BaseUrl =
  import.meta.env.VITE_ALIOTH_API_V2_BASE_URL || apiBaseUrl.replace(/\/v1\/?$/, '/v2')

let tokenProvider: (() => string | null) | null = null
let unauthorizedHandler: (() => void | Promise<void>) | null = null
let handlingUnauthorized = false

export function configureHttpClient(options: {
  getToken?: () => string | null
  onUnauthorized?: () => void | Promise<void>
}) {
  tokenProvider = options.getToken ?? null
  unauthorizedHandler = options.onUnauthorized ?? null
}

async function handleUnauthorized() {
  if (!unauthorizedHandler || handlingUnauthorized) {
    return
  }
  handlingUnauthorized = true
  try {
    await unauthorizedHandler()
  } finally {
    handlingUnauthorized = false
  }
}

function buildUrl(path: string, baseUrl = apiBaseUrl) {
  const normalizedBase = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${normalizedBase}${normalizedPath}`
}

async function parseResponse(response: Response) {
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) {
    return undefined
  }
  // 204 这类没有内容的返回也会带 application/json 头（后端框架默认如此），空内容按「没有返回值」处理
  const text = await response.text()
  return text ? JSON.parse(text) : undefined
}

export async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  return requestJsonFromBase<T>(apiBaseUrl, path, init)
}

/** 调用独立挂载的 `/api/v2` 业务入口。 */
export async function requestJsonV2<T>(path: string, init: RequestInit = {}): Promise<T> {
  return requestJsonFromBase<T>(apiV2BaseUrl, path, init)
}

async function requestJsonFromBase<T>(
  baseUrl: string,
  path: string,
  init: RequestInit,
): Promise<T> {
  const headers = new Headers(init.headers)
  if (!headers.has('Content-Type') && init.body) {
    headers.set('Content-Type', 'application/json')
  }
  const token = tokenProvider?.()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(buildUrl(path, baseUrl), {
    ...init,
    headers,
  })
  const payload = await parseResponse(response)

  if (response.status === 401) {
    await handleUnauthorized()
  }

  if (!response.ok) {
    throw new AliothHttpError(response.status, extractErrorMessage(payload, response.status), payload)
  }

  return payload as T
}

/** multipart / FormData 上传；勿手动设置 Content-Type，交由浏览器带 boundary。 */
export async function requestForm<T>(path: string, formData: FormData, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.delete('Content-Type')
  const token = tokenProvider?.()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(buildUrl(path), {
    ...init,
    method: init.method ?? 'POST',
    headers,
    body: formData,
  })
  const payload = await parseResponse(response)

  if (response.status === 401) {
    await handleUnauthorized()
  }

  if (!response.ok) {
    throw new AliothHttpError(response.status, extractErrorMessage(payload, response.status), payload)
  }

  return payload as T
}

/** 以 Bearer 头下载文件，避免把长期登录令牌写入 URL、历史记录或代理日志。 */
export async function downloadFile(path: string, fallbackFilename: string, version: 'v1' | 'v2' = 'v1', init: RequestInit = {}): Promise<void> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const token = tokenProvider?.()
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  const response = await fetch(buildUrl(path, version === 'v2' ? apiV2BaseUrl : apiBaseUrl), { ...init, headers })
  if (response.status === 401) {
    await handleUnauthorized()
  }
  if (!response.ok) {
    const payload = await parseResponse(response)
    throw new AliothHttpError(
      response.status,
      extractErrorMessage(payload, response.status),
      payload,
    )
  }
  const blobUrl = URL.createObjectURL(await response.blob())
  try {
    const anchor = document.createElement('a')
    anchor.href = blobUrl
    anchor.download = responseFilename(response) || fallbackFilename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(blobUrl), 0)
  }
}

/** 以 Bearer 头取二进制内容（如截图），交给调用方做成页面内的临时地址。 */
export async function requestBlob(path: string): Promise<Blob> {
  return requestBlobFromBase(path, apiBaseUrl)
}

/** 脱敏派生件每次以当前登录身份授权，不复用原件票据。 */
export async function requestBlobV2(path: string, init: RequestInit = {}): Promise<Blob> {
  return requestBlobFromBase(path, apiV2BaseUrl, init)
}

async function requestBlobFromBase(path: string, baseUrl: string, init: RequestInit = {}): Promise<Blob> {
  const headers = new Headers(init.headers)
  const token = tokenProvider?.()
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  const response = await fetch(buildUrl(path, baseUrl), { ...init, headers })
  if (response.status === 401) {
    await handleUnauthorized()
  }
  if (!response.ok) {
    const payload = await parseResponse(response)
    throw new AliothHttpError(
      response.status,
      extractErrorMessage(payload, response.status),
      payload,
    )
  }
  return response.blob()
}

function responseFilename(response: Response): string | null {
  const disposition = response.headers.get('content-disposition') || ''
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      return utf8Match[1]
    }
  }
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i)
  return plainMatch?.[1] || null
}

function extractErrorMessage(payload: unknown, status: number) {
  if (typeof payload === 'object' && payload !== null) {
    const record = payload as Record<string, unknown>
    // 后端统一错误信封：{"error": {"code": "...", "message": "..."}}
    const nested = record.error
    if (typeof nested === 'object' && nested !== null) {
      const nestedMessage = (nested as Record<string, unknown>).message
      if (typeof nestedMessage === 'string' && nestedMessage) {
        return nestedMessage
      }
    }
    if (typeof record.message === 'string' && record.message) {
      return record.message
    }
    // FastAPI / Pydantic 校验：{"detail": "..."} 或 {"detail":[{"msg":"..."}]}
    const detailMessage = extractFastApiDetail(record.detail)
    if (detailMessage) {
      return detailMessage
    }
  }
  return `请求失败：${status}`
}

function extractFastApiDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim()
  }
  if (!Array.isArray(detail) || detail.length === 0) {
    return null
  }
  const parts: string[] = []
  for (const item of detail) {
    if (typeof item === 'string' && item.trim()) {
      parts.push(item.trim())
      continue
    }
    if (typeof item !== 'object' || item === null) {
      continue
    }
    const row = item as Record<string, unknown>
    const msg = typeof row.msg === 'string' ? row.msg.trim() : ''
    if (msg) {
      parts.push(msg.replace(/^Value error,\s*/i, ''))
    }
  }
  return parts.length ? parts.join('；') : null
}
