import { downloadFile, requestForm, requestJson } from './http'

/** iOS 只代表产品资料能力（登记、导入），自动质检仍待接入。 */
export type ProductPlatform = 'h5' | 'web' | 'app' | 'ios'
export type ProductStatus = 'enabled' | 'disabled'

export const PRODUCT_PLATFORM_LABELS: Record<ProductPlatform, string> = {
  h5: 'H5',
  web: 'Web',
  app: 'Android',
  ios: 'iOS',
}

/** 工作台产品清单项（创建任务下拉与产品管理页共用）。 */
export interface WorkstationProductOption {
  id?: string | null
  project_code: string
  product_name: string
  h5_url: string | null
  department_name?: string | null
  owner_name?: string | null
  platforms?: ProductPlatform[] | string[]
  status?: ProductStatus | string
  /** 所属公司；空 = 运营中心维护的共享产品 */
  tenant_id?: string | null
  tenant_name?: string | null
}

export interface WorkstationProductListResponse {
  items: WorkstationProductOption[]
}

/** 产品登记表单（走运营中心同一套 /products 接口）。 */
export interface ProductPayload {
  project_code: string
  product_name: string
  h5_url: string | null
  department_name: string | null
  owner_name: string | null
  platforms: ProductPlatform[]
  status: ProductStatus
  /** 只有总部能指定；留空 = 共享产品 */
  tenant_id?: string | null
}

export interface ProductRecord extends ProductPayload {
  id: string
  tenant_name?: string | null
  created_at: string
  updated_at: string
}

export interface ProductListParams {
  keyword?: string
  status?: ProductStatus | ''
  tenant_id?: string
  department_name?: string
  platform?: ProductPlatform | ''
  page?: number
  page_size?: number
}

export interface PagedProducts {
  items: ProductRecord[]
  total: number
  page: number
  page_size: number
}

export interface ProductImportError {
  row: number
  message: string
}

export interface ProductImportResult {
  id: string | null
  batch_no: string | null
  filename: string
  total_rows: number
  created_count: number
  updated_count: number
  failed_count: number
  errors: ProductImportError[]
}

export interface ProductImportRecord {
  id: string
  batch_no: string | null
  tenant_id: string | null
  tenant_name: string | null
  operator_name: string
  filename: string
  total_rows: number
  created_count: number
  updated_count: number
  failed_count: number
  errors: ProductImportError[]
  created_at: string
}

export function listWorkstationProducts(): Promise<WorkstationProductListResponse> {
  return requestJson<WorkstationProductListResponse>('/workstation/products')
}

/** 产品管理页：服务端分页 + 筛选（含停用产品）。 */
export function listProducts(params: ProductListParams = {}): Promise<PagedProducts> {
  const query = new URLSearchParams()
  if (params.keyword?.trim()) query.set('keyword', params.keyword.trim())
  if (params.status) query.set('status', params.status)
  if (params.tenant_id) query.set('tenant_id', params.tenant_id)
  if (params.department_name) query.set('department_name', params.department_name)
  if (params.platform) query.set('platform', params.platform)
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 10))
  return requestJson<PagedProducts>(`/products?${query.toString()}`)
}

export function createProduct(payload: ProductPayload): Promise<ProductRecord> {
  return requestJson<ProductRecord>('/products', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateProduct(productId: string, payload: ProductPayload): Promise<ProductRecord> {
  return requestJson<ProductRecord>(`/products/${encodeURIComponent(productId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function importProducts(file: File): Promise<ProductImportResult> {
  const form = new FormData()
  form.append('file', file)
  return requestForm<ProductImportResult>('/products/import', form)
}

export function listProductImportRecords(): Promise<{ items: ProductImportRecord[] }> {
  return requestJson<{ items: ProductImportRecord[] }>('/products/import-records')
}

/** 导入模板（xlsx）由后端生成，表头与导入别名一致。 */
export function downloadProductImportTemplate(): Promise<void> {
  return downloadFile('/products/import-template', '产品导入模板.xlsx')
}

/** 某次导入的结果明细（含每行错误原因）。 */
export function downloadProductImportResult(recordId: string, batchNo?: string | null): Promise<void> {
  return downloadFile(
    `/products/import-records/${encodeURIComponent(recordId)}/result`,
    `导入结果-${batchNo || recordId}.csv`,
  )
}

export { downloadFile }
