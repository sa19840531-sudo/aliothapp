import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authStore } from '@/stores/auth'
import ProductImportPanel from './ProductImportPanel.vue'

const mocks = vi.hoisted(() => ({
  importProducts: vi.fn(),
  listProductImportRecords: vi.fn(),
  downloadProductImportTemplate: vi.fn(),
  downloadProductImportResult: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  route: { path: '/admin/products/import' },
}))

vi.mock('vue-router', () => ({ useRoute: () => mocks.route, useRouter: () => ({ push: vi.fn() }) }))
vi.mock('naive-ui', () => ({ useMessage: () => mocks.message }))
vi.mock('@/services/products.api', () => ({
  importProducts: mocks.importProducts,
  listProductImportRecords: mocks.listProductImportRecords,
  downloadProductImportTemplate: mocks.downloadProductImportTemplate,
  downloadProductImportResult: mocks.downloadProductImportResult,
}))

function signIn(permissions: string[]) {
  authStore.session.value = {
    token: 'token',
    expires_at: null,
    user: { id: 'u1', username: 'admin', full_name: '管理员', role: '平台管理员', status: 'enabled', permissions, tenant_id: 't1', tenant_name: '体验中心' },
  }
}

function pickFile(input: ReturnType<typeof mount>['find'] extends (...args: never[]) => infer R ? R : never, file: File) {
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  return input.trigger('change')
}

describe('ProductImportPanel', () => {
  beforeEach(() => {
    mocks.importProducts.mockReset().mockResolvedValue({
      id: 'r1', batch_no: 'IMP-20260908-001', filename: '产品导入.xlsx', total_rows: 3, created_count: 2, updated_count: 0, failed_count: 1,
      errors: [{ row: 4, message: '所属公司编码不存在：c-9999' }],
    })
    mocks.listProductImportRecords.mockReset().mockResolvedValue({
      items: [{ id: 'r1', batch_no: 'IMP-20260908-001', tenant_id: 't1', tenant_name: '体验中心', operator_name: 'admin', filename: '产品导入.xlsx', total_rows: 3, created_count: 2, updated_count: 0, failed_count: 1, errors: [], created_at: '2026-09-08T09:30:00Z' }],
    })
    mocks.downloadProductImportResult.mockReset().mockResolvedValue(undefined)
    mocks.message.warning.mockReset()
    mocks.route.path = '/admin/products/import'
    signIn(['qcRules', 'taskObjects:read:any'])
  })

  it('tells department accounts that company and department are filled in automatically', async () => {
    mocks.route.path = '/products'
    signIn(['qcRules', 'taskObjects:read:owner'])
    const wrapper = mount(ProductImportPanel, { props: { tab: 'import' } })
    await flushPromises()
    const notes = wrapper.find('[data-testid="import-panel"]').text()
    expect(notes).toContain('所属公司、所属部门不用填，导入后自动归本部门')
    expect(notes).not.toContain('所属公司编码')
  })

  it('accepts xlsx, rejects legacy xls and shows the import result with per-row errors', async () => {
    const wrapper = mount(ProductImportPanel, { props: { tab: 'import' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="import-panel"]').text()).toContain('所属公司编码（运营中心导入时填写')

    const input = wrapper.find('[data-testid="import-file"]')
    await pickFile(input, new File(['x'], 'legacy.xls'))
    expect(mocks.message.warning).toHaveBeenCalledWith(expect.stringContaining('.xlsx'))
    expect(wrapper.find('[data-testid="import-progress"]').exists()).toBe(false)

    const file = new File(['PK'], '产品导入.xlsx')
    await pickFile(input, file)
    expect(wrapper.find('[data-testid="import-progress"]').text()).toContain('产品导入.xlsx')
    await wrapper.find('[data-testid="import-run"]').trigger('click')
    await flushPromises()
    expect(mocks.importProducts).toHaveBeenCalledWith(file)
    const result = wrapper.find('[data-testid="import-result"]').text()
    expect(result).toContain('IMP-20260908-001')
    expect(result).toContain('第 4 行')
    expect(result).toContain('c-9999')
    expect(wrapper.emitted('imported')).toHaveLength(1)
  })

  it('lists import records with batch numbers and downloads the result file', async () => {
    const wrapper = mount(ProductImportPanel, { props: { tab: 'record' } })
    await flushPromises()
    const records = wrapper.find('[data-testid="import-records"]').text()
    expect(records).toContain('IMP-20260908-001')
    expect(records).toContain('已完成')
    await wrapper.find('[data-testid="import-record-download-r1"]').trigger('click')
    expect(mocks.downloadProductImportResult).toHaveBeenCalledWith('r1', 'IMP-20260908-001')
    await wrapper.find('[data-testid="import-tab-import"]').trigger('click')
    expect(wrapper.emitted('update:tab')?.[0]).toEqual(['import'])
  })

  it('loads the records when the parent switches the tab without remounting the panel', async () => {
    // 产品页顶部页签、管理后台侧栏「导入记录」都只改 tab，原来不会去拉记录，一直显示「还没有导入记录」
    const wrapper = mount(ProductImportPanel, { props: { tab: 'import' } })
    await flushPromises()
    expect(mocks.listProductImportRecords).not.toHaveBeenCalled()

    await wrapper.setProps({ tab: 'record' })
    expect(wrapper.find('[data-testid="import-records"]').text()).toContain('加载中…')
    await flushPromises()
    expect(mocks.listProductImportRecords).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="import-records"]').text()).toContain('IMP-20260908-001')
  })
})
