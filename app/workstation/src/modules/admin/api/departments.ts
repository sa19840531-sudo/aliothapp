import { request } from './http'

/** 部门管理（/departments）：分公司 → 部门 → 产品 的中间层。 */

export type DepartmentStatus = 'enabled' | 'disabled'

export interface DepartmentItem {
  id: string
  tenant_id: string
  tenant_name: string
  name: string
  owner_name: string | null
  status: DepartmentStatus
  monthly_token_budget: number | null
  remark: string | null
  product_count: number
  user_count: number
  created_at: string
  updated_at: string
}

export interface DepartmentPayload {
  name: string
  owner_name?: string | null
  status?: DepartmentStatus
  monthly_token_budget?: number | null
  remark?: string | null
  tenant_id?: string | null
}

export interface DepartmentQuery {
  tenant_id?: string
  keyword?: string
  status?: DepartmentStatus | ''
}

export async function listDepartments(query: DepartmentQuery = {}) {
  const search = new URLSearchParams()
  if (query.tenant_id) search.set('tenant_id', query.tenant_id)
  if (query.keyword?.trim()) search.set('keyword', query.keyword.trim())
  if (query.status) search.set('status', query.status)
  const text = search.toString()
  const result = await request<{ items: DepartmentItem[] }>(`/departments${text ? `?${text}` : ''}`)
  return result.items
}

export function createDepartment(payload: DepartmentPayload) {
  return request<DepartmentItem>('/departments', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateDepartment(departmentId: string, payload: DepartmentPayload) {
  return request<DepartmentItem>(`/departments/${encodeURIComponent(departmentId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

/** 服务端仅删除无关联记录，有业务或历史关联时返回明确说明。 */
export function deleteDepartment(departmentId: string) {
  return request<void>(`/departments/${encodeURIComponent(departmentId)}`, { method: 'DELETE' })
}
