import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listReports: vi.fn(),
  downloadReportExport: vi.fn(),
  downloadReportExportBatch: vi.fn(),
  listWorkstationProducts: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  route: { path: '/reports', query: {} as Record<string, string> },
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }),
}))

vi.mock('@/services/reports.api', () => ({
  listReports: mocks.listReports,
  downloadReportExport: mocks.downloadReportExport,
  downloadReportExportBatch: mocks.downloadReportExportBatch,
}))

vi.mock('@/services/products.api', () => ({
  listWorkstationProducts: mocks.listWorkstationProducts,
}))

vi.mock('@/stores/auth', () => ({
  authStore: { user: { value: { username: 'u', full_name: '刘一', role: '规则配置员', tenant_name: '北京分公司' } } },
}))

import ReportListView from './ReportListView.vue'

function report(overrides: Record<string, unknown> = {}) {
  return {
    task_id: 'task-1',
    task_name: '手机银行首页巡检',
    report_status: 'ready',
    report_availability: 'final',
    overall_status: 'fail',
    attention_required: 12,
    generated_at: '2026-08-31T01:30:00Z',
    pending_confirm_count: 0,
    department_name: '一部',
    product_code: 'MB-001',
    product_name: '手机银行 Android',
    target_platform: 'app',
    issue_count: 12,
    pass_rate: 86.4,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.route = { path: '/reports', query: {} }
  mocks.listReports.mockResolvedValue({
    items: [
      report(),
      report({
        task_id: 'task-2',
        task_name: 'Web 转账流程巡检',
        report_status: 'generating', report_availability: 'none',
        overall_status: 'pending',
        attention_required: null,
        product_name: '企业网银',
        target_platform: 'web',
        issue_count: null,
        pass_rate: null,
      }),
      report({
        task_id: 'task-3',
        task_name: 'Android 注册页质检',
        report_status: 'failed',
        overall_status: 'failed',
        attention_required: null,
        issue_count: null,
        pass_rate: null,
      }),
    ],
    total: 3,
    page: 1,
    page_size: 10,
  })
  mocks.listWorkstationProducts.mockResolvedValue({
    items: [
      { project_code: 'MB-001', product_name: '手机银行 Android', h5_url: null },
      { project_code: 'EB-001', product_name: '企业网银', h5_url: null },
      { project_code: 'EB-002', product_name: '企业网银', h5_url: null },
      { project_code: 'H5-009', product_name: 'H5 活动页', h5_url: 'https://h5.example.test' },
    ],
  })
  mocks.downloadReportExportBatch.mockResolvedValue(undefined)
})

function rowOf(wrapper: ReturnType<typeof mount>, taskName: string) {
  const row = wrapper.findAll('[data-testid="report-table"] tbody tr').find((item) => item.text().includes(taskName))
  if (!row) throw new Error(`找不到报告行：${taskName}`)
  return row
}

describe('ReportListView（报告列表）', () => {
  it.each(['/reports', '/admin/reports'])('%s 中 Android 预览展示真实结果且可查看，无事实报告继续禁用，正式报告才可导出', async (path) => {
    mocks.route = { path, query: {} }
    mocks.listReports.mockResolvedValueOnce({
      items: [
        report({
          task_id: 'android-preview',
          task_name: 'Android 待复核任务',
          report_availability: 'preview',
          pass_rate: 50,
          issue_count: 2,
          attention_required: 3,
        }),
        report({
          task_id: 'android-none',
          task_name: 'Android 无结果任务',
          report_availability: 'none',
          pass_rate: 66,
          issue_count: 8,
        }),
        report({ task_id: 'android-final', task_name: 'Android 正式报告任务' }),
      ],
      total: 3,
      page: 1,
      page_size: 10,
    })
    const wrapper = mount(ReportListView)
    await flushPromises()

    const preview = rowOf(wrapper, 'Android 待复核任务')
    const previewCells = preview.findAll('td')
    expect(previewCells[3]!.text()).toBe('Android')
    expect(preview.get('[data-testid="report-state"]').text()).toBe('质检未完整完成')
    expect(previewCells[5]!.text()).toBe('50%')
    expect(previewCells[6]!.text()).toBe('2')
    expect(preview.findAll('button')[0]!.attributes('disabled')).toBeUndefined()
    expect(preview.findAll('button')[1]!.attributes('disabled')).toBeDefined()
    expect(preview.get('input[type="checkbox"]').attributes('disabled')).toBeDefined()
    await preview.findAll('button')[0]!.trigger('click')
    expect(mocks.push).toHaveBeenCalledWith(`${path}/android-preview`)

    const none = rowOf(wrapper, 'Android 无结果任务')
    expect(none.get('[data-testid="report-state"]').text()).toBe('未生成')
    expect(none.findAll('td')[5]!.text()).toBe('-')
    expect(none.findAll('td')[6]!.text()).toBe('-')
    expect(none.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    expect(none.get('input[type="checkbox"]').attributes('disabled')).toBeDefined()

    const final = rowOf(wrapper, 'Android 正式报告任务')
    expect(final.get('[data-testid="report-state"]').text()).toBe('已完成')
    expect(final.findAll('button').every((button) => button.attributes('disabled') === undefined)).toBe(true)
    expect(final.get('input[type="checkbox"]').attributes('disabled')).toBeUndefined()
    await final.findAll('button')[1]!.trigger('click')
    await flushPromises()
    expect(mocks.downloadReportExport).toHaveBeenCalledWith('android-final')

    await wrapper.get('thead input[type="checkbox"]').setValue(true)
    await wrapper.get('[data-testid="report-export-batch"]').trigger('click')
    await flushPromises()
    expect(mocks.downloadReportExportBatch).toHaveBeenCalledWith(['android-final'])
    expect(preview.get<HTMLInputElement>('input[type="checkbox"]').element.checked).toBe(false)
    expect(none.get<HTMLInputElement>('input[type="checkbox"]').element.checked).toBe(false)
  })

  it('待确认21项和预览报告能查看但不能单条或批量导出，正式报告才可导出', async () => {
    mocks.listReports.mockResolvedValueOnce({ items: [
      report({ task_id: 'pending', task_name: '待人工复核', pending_confirm_count: 21, report_availability: 'preview' }),
      report({ task_id: 'preview', task_name: '有缺口的预览', report_availability: 'preview' }),
      report({ task_id: 'unknown', task_name: '旧报告状态未知', report_availability: undefined }),
      report({ task_id: 'final', task_name: '正式封存报告' }),
    ], total: 4, page: 1, page_size: 10 })
    const wrapper = mount(ReportListView)
    await flushPromises()
    for (const [name, label] of [['待人工复核', '待人工确认（21）'], ['有缺口的预览', '质检未完整完成'], ['旧报告状态未知', '报告状态待核实']]) {
      const row = rowOf(wrapper, name!)
      expect(row.get('[data-testid="report-state"]').text()).toBe(label)
      const buttons = row.findAll('button')
      expect(buttons.find((button) => button.text() === '在线查看')!.attributes('disabled')).toBeUndefined()
      expect(buttons.find((button) => button.text() === '导出')!.attributes('disabled')).toBeDefined()
      expect(row.get('input[type="checkbox"]').attributes('disabled')).toBeDefined()
    }
    await wrapper.get('thead input[type="checkbox"]').setValue(true)
    await wrapper.get('[data-testid="report-export-batch"]').trigger('click')
    await flushPromises()
    expect(mocks.downloadReportExportBatch).toHaveBeenCalledWith(['final'])
    await rowOf(wrapper, '待人工复核').findAll('button')[0]!.trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/reports/pending')
  })

  it.each([
    ['app', 'ready'], ['app', 'generated'],
    ['android', 'ready'], ['android', 'generated'],
  ])('%s 的 %s 状态不能覆盖 none 报告可用性', async (platform, status) => {
    mocks.listReports.mockResolvedValueOnce({
      items: [
        report({
          task_id: 'app-ready',
          task_name: 'App 无事实报告任务',
          target_platform: platform,
          report_status: status,
          report_availability: 'none',
          pass_rate: 99.5,
          issue_count: 1,
        }),
      ],
      total: 1,
      page: 1,
      page_size: 10,
    })
    const wrapper = mount(ReportListView)
    await flushPromises()
    const row = rowOf(wrapper, 'App 无事实报告任务')
    expect(row.findAll('td')[5]!.text()).toBe('-')
    expect(row.findAll('td')[6]!.text()).toBe('-')
    expect(row.get('[data-testid="report-state"]').text()).toBe('未生成')
    const view = row.findAll('button').find((button) => button.text() === '在线查看')
    const exportButton = row.findAll('button').find((button) => button.text() === '导出')
    expect(view!.attributes('disabled')).toBeDefined()
    expect(exportButton!.attributes('disabled')).toBeDefined()
    expect(row.get('input[type="checkbox"]').attributes('disabled')).toBeDefined()
    await view!.trigger('click')
    await exportButton!.trigger('click')
    await wrapper.get('thead input[type="checkbox"]').setValue(true)
    await wrapper.get('[data-testid="report-export-batch"]').trigger('click')
    await flushPromises()
    expect(mocks.push).not.toHaveBeenCalled()
    expect(mocks.downloadReportExport).not.toHaveBeenCalled()
    expect(mocks.downloadReportExportBatch).not.toHaveBeenCalled()
  })

  it('报告区分正式完成和生成状态，并展示产品、平台、通过率与问题数', async () => {
    const wrapper = mount(ReportListView)
    await flushPromises()

    const table = wrapper.find('[data-testid="report-table"]')
    expect(table.text()).toContain('共 3 条报告')
    expect(table.find('thead').text()).toContain('报告状态')
    expect(table.find('thead').text()).not.toContain('质检结果')

    const ready = rowOf(wrapper, '手机银行首页巡检')
    expect(ready.text()).toContain('手机银行 Android')
    expect(ready.text()).toContain('Android')
    expect(ready.text()).toContain('86.4%')
    expect(ready.text()).toContain('12')
    expect(ready.get('[data-testid="report-state"]').text()).toBe('已完成')
    expect(ready.get('[data-testid="report-state"]').classes()).toContain('tone-success')

    const generating = rowOf(wrapper, 'Web 转账流程巡检').get('[data-testid="report-state"]')
    expect(generating.text()).toBe('生成中')
    expect(generating.classes()).toContain('tone-primary')

    const failed = rowOf(wrapper, 'Android 注册页质检').get('[data-testid="report-state"]')
    expect(failed.text()).toBe('生成失败')
    expect(failed.classes()).toContain('tone-danger')

    // 单条规则的六类结果不再冒充报告状态
    expect(table.text()).not.toContain('未通过')
    expect(table.text()).not.toContain('待人工确认')
    // 行操作只剩在线查看与导出
    expect(table.text()).not.toContain('反馈问题')
    expect(mocks.listReports).toHaveBeenCalledWith({ page: 1, page_size: 10 })
  })

  it('部门平台不展示公司与部门筛选，保留日期筛选', async () => {
    const wrapper = mount(ReportListView)
    await flushPromises()

    const filters = wrapper.find('[data-testid="report-filters"]')
    expect(filters.text()).not.toContain('北京分公司')
    expect(filters.text()).not.toContain('全部部门')
    expect(filters.find('[data-testid="report-filter-department"]').exists()).toBe(false)
    expect(filters.find('input[type="date"]').exists()).toBe(true)
  })

  it('管理后台保留公司与部门筛选，部门条件走服务端', async () => {
    mocks.route = { path: '/admin/reports', query: {} }
    const wrapper = mount(ReportListView)
    await flushPromises()

    const filters = wrapper.find('[data-testid="report-filters"]')
    expect(filters.text()).toContain('北京分公司')
    expect(filters.text()).toContain('全部部门')
    expect(filters.find('input[type="date"]').exists()).toBe(true)

    await wrapper.get('[data-testid="report-filter-department"]').setValue('一部')
    await wrapper.get('[data-testid="report-search"]').trigger('click')
    await flushPromises()
    expect(mocks.listReports).toHaveBeenLastCalledWith({ page: 1, page_size: 10, department_name: '一部' })

    // 管理后台里在线查看跳到管理后台的报告详情
    await rowOf(wrapper, '手机银行首页巡检')
      .findAll('button')
      .find((button) => button.text() === '在线查看')!
      .trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/admin/reports/task-1')
  })

  it('报告状态筛选按三态传给服务端，不在当前页内过滤', async () => {
    const wrapper = mount(ReportListView)
    await flushPromises()

    const options = wrapper.get('[data-testid="report-filter-status"]').findAll('option')
    expect(options.map((option) => option.text())).toEqual(['全部报告状态', '已生成（含待确认）', '生成中', '生成失败'])

    mocks.listReports.mockResolvedValueOnce({
      items: [report({ task_id: 'task-3', task_name: 'Android 注册页质检', report_status: 'failed' })],
      total: 1,
      page: 1,
      page_size: 10,
    })
    await wrapper.get('[data-testid="report-filter-status"]').setValue('failed')
    await wrapper.get('[data-testid="report-search"]').trigger('click')
    await flushPromises()

    expect(mocks.listReports).toHaveBeenLastCalledWith({ page: 1, page_size: 10, report_status: 'failed' })
    const table = wrapper.find('[data-testid="report-table"]')
    expect(table.text()).toContain('共 1 条报告')
    expect(table.text()).toContain('生成失败')

    await wrapper.get('[data-testid="report-filter-status"]').setValue('ready')
    await wrapper.get('[data-testid="report-search"]').trigger('click')
    await flushPromises()
    expect(mocks.listReports).toHaveBeenLastCalledWith({ page: 1, page_size: 10, report_status: 'ready' })
  })

  it('产品下拉取工作台产品清单并按名称去重', async () => {
    const wrapper = mount(ReportListView)
    await flushPromises()

    expect(mocks.listWorkstationProducts).toHaveBeenCalledTimes(1)
    const options = wrapper.get('[data-testid="report-filter-product"]').findAll('option')
    expect(options.map((option) => option.text())).toEqual(['全部产品', '手机银行 Android', '企业网银', 'H5 活动页'])

    await wrapper.get('[data-testid="report-filter-product"]').setValue('H5 活动页')
    await wrapper.get('[data-testid="report-search"]').trigger('click')
    await flushPromises()
    expect(mocks.listReports).toHaveBeenLastCalledWith({ page: 1, page_size: 10, product_name: 'H5 活动页' })
  })

  it('产品清单取不到时只保留全部产品，列表照常加载', async () => {
    mocks.listWorkstationProducts.mockRejectedValueOnce(new Error('无权限'))
    const wrapper = mount(ReportListView)
    await flushPromises()

    const options = wrapper.get('[data-testid="report-filter-product"]').findAll('option')
    expect(options.map((option) => option.text())).toEqual(['全部产品'])
    expect(wrapper.find('[data-testid="report-table"]').text()).toContain('共 3 条报告')
  })

  it('任务名称筛选走服务端，在线查看跳转，勾选后批量导出', async () => {
    const wrapper = mount(ReportListView)
    await flushPromises()

    mocks.listReports.mockResolvedValueOnce({
      items: [report({ task_id: 'task-2', task_name: 'Web 转账流程巡检', report_status: 'generating', report_availability: 'none' })],
      total: 1,
      page: 1,
      page_size: 10,
    })
    await wrapper.get('[data-testid="report-filter-name"]').setValue('转账')
    await wrapper.get('[data-testid="report-search"]').trigger('click')
    await flushPromises()
    expect(mocks.listReports).toHaveBeenLastCalledWith({ page: 1, page_size: 10, task_name: '转账' })
    const table = wrapper.find('[data-testid="report-table"]')
    expect(table.text()).toContain('共 1 条报告')
    // 生成中的报告不能在线查看，也不能导出
    const buttons = table.findAll('button')
    expect(buttons.find((button) => button.text() === '在线查看')!.attributes('disabled')).toBeDefined()
    expect(buttons.find((button) => button.text() === '导出')!.attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="report-filter-name"]').setValue('')
    await wrapper.get('[data-testid="report-search"]').trigger('click')
    await flushPromises()
    await rowOf(wrapper, '手机银行首页巡检')
      .findAll('button')
      .find((button) => button.text() === '在线查看')!
      .trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/reports/task-1')

    const checkboxes = wrapper.find('[data-testid="report-table"]').findAll('tbody input[type="checkbox"]')
    await checkboxes[0].setValue(true)
    await checkboxes[1].setValue(true)
    await wrapper.get('[data-testid="report-export-batch"]').trigger('click')
    await flushPromises()
    expect(mocks.downloadReportExportBatch).toHaveBeenCalledWith(['task-1'])
  })

  it('旧链接 /reports?task=… 直接进详情，并带上要确认的规则', async () => {
    mocks.route = { path: '/reports', query: { task: 'task-9', execution: 'exec-7' } }
    mount(ReportListView)
    await flushPromises()

    expect(mocks.replace).toHaveBeenCalledWith({ path: '/reports/task-9', query: { execution: 'exec-7' } })
    expect(mocks.listReports).not.toHaveBeenCalled()
  })

  it.each(['/reports', '/admin/reports'])('任务列表经 %s 兼容跳转时保留第 3 页和所有筛选', async (path) => {
    const state = {
      page: '3', page_size: '10', task_name: '巡检', product_code: 'MB-001',
      department_name: '质检部', target_platform: 'h5', submitter_name: '张三',
      status: 'done', created_from: '2026-09-01', created_to: '2026-09-30',
    }
    mocks.route = { path, query: { ...state, task: 'task-9', execution: 'exec-7', unrelated: '忽略' } }
    mount(ReportListView)
    await flushPromises()

    expect(mocks.replace).toHaveBeenCalledWith({
      path: `${path}/task-9`, query: { ...state, execution: 'exec-7' },
    })
    expect(mocks.listReports).not.toHaveBeenCalled()
    expect(mocks.listWorkstationProducts).not.toHaveBeenCalled()
  })
})
