import { request } from './http'

export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export type UserStatus = 'enabled' | 'disabled'
export type UserRole = '平台管理员' | '公司管理员' | '审计员' | '规则配置员'

export interface UserItem {
  id: string
  username: string
  full_name: string
  role: UserRole
  status: UserStatus
  tenant_id: string
  tenant_name: string
  department_name: string | null
  last_login_at: string | null
  /** 数据权限范围说明（由角色和部门推出） */
  data_scope: string
  created_at: string
  updated_at: string
}

export interface UserQuery {
  keyword?: string
  status?: UserStatus | 'all'
  tenant_id?: string | 'all'
  role?: UserRole | 'all'
  department_name?: string
  page?: number
  page_size?: number
}

export interface CreateUserPayload {
  username: string
  full_name: string
  password: string
  role: UserRole
  status: UserStatus
  tenant_id?: string
  department_name?: string | null
}

export interface UpdateUserPayload {
  full_name: string
  role: UserRole
  status: UserStatus
  password?: string
  tenant_id?: string
  department_name?: string | null
}

function toSearchParams(params: UserQuery): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === '' || value === 'all') {
      return
    }
    search.set(key, String(value))
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

export async function listUsers(query: UserQuery = {}) {
  const url = `/users${toSearchParams(query)}`
  return request<PageResult<UserItem>>(url)
}

export async function createUser(payload: CreateUserPayload) {
  return request<UserItem>('/users', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function updateUser(userId: string, payload: UpdateUserPayload) {
  return request<UserItem>(`/users/${userId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export async function deleteUser(userId: string) {
  return request<void>(`/users/${userId}`, {
    method: 'DELETE',
  })
}
