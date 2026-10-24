import { requestJson } from './http'

export interface AuthUser {
  id: string
  username: string
  full_name: string
  role: string
  status: string
  permissions: string[]
  /** 所属公司（工作空间）；旧会话可能没有，展示时按缺省处理 */
  tenant_id?: string
  tenant_name?: string
  /** 账号所属部门；旧会话可能没有 */
  department_name?: string
}

export interface AuthSession {
  token: string
  user: AuthUser
  /** 后端签发的过期时间（ISO），前端据此在到期瞬间强制登出。 */
  expires_at?: string | null
}

export interface LoginPayload {
  username: string
  password: string
}

export function login(payload: LoginPayload): Promise<AuthSession> {
  return requestJson<AuthSession>('/auth/login', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function fetchCurrentUser(): Promise<AuthUser> {
  return requestJson<AuthUser>('/auth/me')
}
