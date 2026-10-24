import { expect, test } from '@playwright/test'
import {
  blockUnmockedApi,
  departmentUser,
  expectBearerToken,
  fulfillJson,
  mockLogin,
} from '../../fixtures/api'

test('登录后返回工作台并展示任务与规则概览', async ({ page }) => {
  const unmocked = await blockUnmockedApi(page)
  await mockLogin(page, departmentUser)
  await page.route('**/api/v1/workstation/dashboard/overview', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      summary_cards: [
        { key: 'total_tasks', label: '任务总数', value: 27 },
        { key: 'running_tasks', label: '执行中', value: 1 },
        { key: 'done_tasks', label: '已完成', value: 26 },
      ],
      today_metrics: {
        executed_tasks_today: 3,
        executed_tasks_yesterday: 2,
        ignored_rules_today: 1,
        abnormal_rules_today: 1,
        qc_success_rate_today: 92.5,
        qc_success_rate_yesterday: 90,
      },
      recent_tasks: [],
      status_distribution: [
        { status: 'running', count: 1 },
        { status: 'done', count: 26 },
      ],
      risk_distribution: [],
      execution_status_distribution: [
        { execution_status: 'pass', count: 8 },
        { execution_status: 'fail', count: 1 },
      ],
      latest_rule_executions: [],
      token_usage: {
        month: '2026-09',
        total_tokens: 36_800_000,
        total_cost: 128.5,
        currency: 'CNY',
        task_count: 12,
      },
    })
  })
  await page.route('**/api/v1/workstation/tasks?*', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      items: [
        {
          task_id: '00000000-0000-0000-0000-000000000101',
          task_name: '前端自动化验收任务',
          product: {
            department_name: '质量平台',
            product_code: 'E2E-001',
            product_name: '测试产品',
          },
          target_url: 'https://example.test/',
          target_platform: 'h5',
          execution_mode: 'full_inspection',
          journey_steps: [],
          status: 'running',
          progress: 45,
          report_status: 'not_started',
          failure_reason: null,
          available_actions: ['detail'],
          created_at: '2026-08-05T08:00:00Z',
        },
      ],
      total: 1,
      page: 1,
      page_size: 12,
    })
  })
  await page.route('**/api/v1/workstation/reports?*', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      items: [
        {
          task_id: '00000000-0000-0000-0000-000000000102',
          task_name: '上周巡检任务',
          report_status: 'ready',
          report_availability: 'final',
          overall_status: 'fail',
          attention_required: 2,
          generated_at: '2026-09-17T08:00:00Z',
          pending_confirm_count: 1,
          department_name: '质量平台',
          product_code: 'E2E-001',
          product_name: '测试产品',
          target_platform: 'h5',
          issue_count: 2,
          pass_rate: 92.5,
        },
      ],
      total: 1,
      page: 1,
      page_size: 8,
    })
  })
  // 系统状态接口只对平台管理员开放，部门工作台不请求它（请求了会被兜底拦截记下）。
  // 快速开始质检卡片：产品、规则目录、部署能力、设备库存
  await page.route('**/api/v1/workstation/products', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      items: [
        {
          project_code: 'E2E-001',
          product_name: '测试产品',
          h5_url: 'https://example.test/',
          department_name: '质量平台',
          platforms: ['h5', 'app'],
          status: 'enabled',
        },
      ],
    })
  })
  await page.route('**/api/v1/workstation/rule-catalog', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      rulesets: [
        {
          ruleset_id: '00000000-0000-0000-0000-000000000201',
          ruleset_code: 'e2e_phase1',
          version: 1,
          name: '1期质检规则集',
          description: '',
          status: 'enabled',
          enabled_rule_count: 40,
          rule_codes: [],
        },
      ],
      rules: [],
      page_types: [
        {
          code: 'home',
          name: '首页',
          category: 'business',
          rule_matchable: true,
          coverage_required: true,
          description: '',
          related_rule_codes: [],
        },
      ],
      page_scope_options: { h5: ['home'], web: ['home'], app: ['home'] },
    })
  })
  await page.route('**/api/v2/capabilities', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      web: {
        enabled: true,
        execution_modes: ['full_inspection'],
        default_browser_profile_id: 'desktop-1080p',
        browser_profiles: [],
      },
    })
  })
  await page.route('**/api/v1/workstation/devices', async (route) => {
    expectBearerToken(route)
    await fulfillJson(route, {
      items: [
        {
          device_ref: 'cloud_phone:qa-d',
          backend: 'cloud_phone',
          profile_id: 'qa-d',
          enabled: true,
          health_state: 'healthy',
          occupied: false,
          occupancy_status: 'available',
          lease_task_id: null,
          lease_session_id: null,
          lease_state: null,
          lease_expires_at: null,
          morelogin_env_id: 'e2e-env',
          queued_preferred_count: 0,
        },
      ],
    })
  })

  await page.goto('/')
  await expect(page).toHaveURL(/\/login\?redirect=\/$/)

  await page.getByPlaceholder('请输入账号').fill('e2e-user')
  await page.getByPlaceholder('请输入密码').fill('e2e-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page).toHaveURL(/\/$/)

  // 部门账号只有部门平台六项菜单，没有管理后台入口；数据范围按账号所属公司和部门。
  const sidebar = page.getByRole('complementary', { name: '部门质检平台导航' })
  await expect(sidebar.getByRole('navigation').getByRole('button')).toHaveText([
    /工作台$/,
    /产品管理$/,
    /任务中心$/,
    /报告中心$/,
    /规则中心$/,
    /我的反馈$/,
  ])
  await expect(sidebar).toContainText('华东分公司 · 质量平台')

  // 今日概览：所有模块都加载成功，数字来自各自接口。
  await expect(page.getByTestId('dashboard-load-error')).toHaveCount(0)
  const statCard = (label: string) =>
    page.getByTestId('stat-grid').locator('article').filter({ hasText: label })
  await expect(statCard('今日任务').locator('strong')).toHaveText('3')
  await expect(statCard('今日任务')).toContainText('累计完成 26')
  await expect(statCard('执行中').locator('strong')).toHaveText('1')
  await expect(statCard('执行中')).toContainText('进度见下方任务与队列')
  await expect(statCard('待查看报告').locator('strong')).toHaveText('1')
  await expect(statCard('待查看报告')).toContainText('其中 1 份待人工确认')
  await expect(statCard('本月 Token').locator('strong')).toHaveText('36.8M')
  await expect(statCard('本月 Token')).toContainText('12 个任务 · 费用 128.50 CNY')

  const taskRow = page.getByTestId('queue-panel').getByRole('row', { name: /前端自动化验收任务/ })
  await expect(taskRow.getByRole('cell')).toHaveText(['前端自动化验收任务', 'H5', '执行中', '45%'])

  const reportsPanel = page.getByTestId('reports-panel')
  await expect(reportsPanel).toContainText('上周巡检任务')
  await expect(reportsPanel).toContainText('通过率 92.5% · 2 个问题 · 待确认 1')
  await expect(reportsPanel.getByRole('button', { name: '在线查看' })).toBeVisible()

  // 快速开始质检：产品、设备、部署能力和规则集都来自接口。
  const quickStart = page.getByTestId('quick-start-card')
  await expect(quickStart.getByTestId('product-select')).toContainText('测试产品（E2E-001）')
  await expect(quickStart).toContainText('将使用空闲设备 qa-d')
  await quickStart.getByRole('tab', { name: 'H5 / Web 地址' }).click()
  await expect(quickStart.getByRole('radio', { name: /Web（桌面视口）/ })).toBeEnabled()
  // 单选框保持原生尺寸：套上输入框的 100% 宽会把旁边的「H5（手机视口）」挤成逐字换行
  for (const name of [/H5（手机视口）/, /Web（桌面视口）/]) {
    const box = await quickStart.getByRole('radio', { name }).boundingBox()
    expect(box?.width).toBeLessThan(24)
  }
  await quickStart.getByTestId('target-url').fill('https://example.test/home')
  await expect(quickStart.getByTestId('quick-hint')).toHaveText(
    '已选择 1期质检规则集 · 40 条规则，页面范围 全部可匹配页型；提交后进入执行队列',
  )

  await page.getByRole('button', { name: '当前用户菜单' }).click()
  await expect(page.getByText('自动化验收员 · 华东分公司 · 质量平台')).toBeVisible()

  expect(unmocked).toEqual([])
})
