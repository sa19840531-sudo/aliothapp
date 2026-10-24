import { request } from './http'

export type TenantStatus = 'enabled' | 'disabled'

export interface TenantItem {
  id: string
  tenant_key: string
  display_name: string
  status: TenantStatus
  contact_name: string | null
  remark: string | null
  /** 总管理员账号（公司管理员；总部退回第一个平台管理员） */
  admin_username: string | null
  user_count: number
  department_count: number
  product_count: number
  is_headquarters: boolean
  created_at: string
  updated_at: string
}

export interface CreateTenantPayload {
  display_name: string
  tenant_key?: string
  contact_name?: string | null
  remark?: string | null
  /** 顺手建总管理员账号：填了账号就必须给初始密码 */
  admin_username?: string
  admin_full_name?: string
  admin_password?: string
}

export interface UpdateTenantPayload {
  display_name: string
  status: TenantStatus
  contact_name?: string | null
  remark?: string | null
}

export async function listTenants() {
  const result = await request<{ items: TenantItem[] }>('/tenants')
  return result.items
}

export async function createTenant(payload: CreateTenantPayload) {
  return request<TenantItem>('/tenants', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function updateTenant(tenantId: string, payload: UpdateTenantPayload) {
  return request<TenantItem>(`/tenants/${tenantId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

/** 服务端仅删除无关联记录，有业务或历史关联时返回明确说明。 */
export function deleteTenant(tenantId: string) {
  return request<void>(`/tenants/${encodeURIComponent(tenantId)}`, { method: 'DELETE' })
}
