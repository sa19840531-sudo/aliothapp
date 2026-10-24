import type { RouteRecordRaw } from 'vue-router'
import WorkstationLayout from '@/layouts/WorkstationLayout.vue'
import AdminLayout from '@/layouts/AdminLayout.vue'

export type WorkstationMenuGroup = 'main' | 'more'

export interface WorkstationMenuItem {
  key: string
  label: string
  path: string
  /** main=部门质检平台主菜单（对齐 2.1 原型）；more=运营/开发用的次级入口 */
  group: WorkstationMenuGroup
  /** 侧栏收起时显示的单字标识 */
  badge: string
}

/**
 * 部门质检平台侧栏（2.1 原型 renderDepartmentNavigation）：固定六项，按原型顺序；
 * 快速质检在工作台里，不再单独占菜单；配置中心与规则测试只在管理后台。
 */
export const workstationMenuItems: WorkstationMenuItem[] = [
  { key: 'dashboard', label: '工作台', path: '/', group: 'main', badge: '首' },
  { key: 'productList', label: '产品管理', path: '/products', group: 'main', badge: '产' },
  { key: 'taskList', label: '任务中心', path: '/tasks', group: 'main', badge: '任' },
  { key: 'reportCenter', label: '报告中心', path: '/reports', group: 'main', badge: '报' },
  { key: 'ruleCenter', label: '规则中心', path: '/rules', group: 'main', badge: '规' },
  { key: 'feedback', label: '我的反馈', path: '/feedback', group: 'main', badge: '馈' },
]

/**
 * 体验中心管理后台的树形菜单（对齐 2.1 原型 admin-navigation.js：一级带图标可展开，二级是具体页面）。
 * permission 与 /auth/me 返回的权限键一致；没写的项登录后都能看。
 */
export interface AdminMenuChild {
  key: string
  label: string
  /** 可带 ?tab=… 区分同一页面的不同入口 */
  path: string
  permission?: string
}

export interface AdminMenuNode {
  key: string
  label: string
  badge: string
  path?: string
  permission?: string
  children?: AdminMenuChild[]
}

export const adminMenuTree: AdminMenuNode[] = [
  { key: 'dashboard', label: '工作台', badge: '台', path: '/admin' },
  {
    key: 'org',
    label: '组织管理',
    badge: '组',
    children: [
      { key: 'tenants', label: '分公司管理', path: '/admin/tenants', permission: 'tenants' },
      { key: 'departments', label: '部门管理', path: '/admin/departments', permission: 'departments' },
      { key: 'users', label: '账号与权限', path: '/admin/users', permission: 'users' },
      { key: 'roles', label: '角色说明', path: '/admin/roles', permission: 'roles' },
    ],
  },
  {
    key: 'product',
    label: '产品管理',
    badge: '产',
    children: [
      { key: 'productList', label: '产品列表', path: '/admin/products' },
      { key: 'productImport', label: 'Excel 导入', path: '/admin/products/import' },
      { key: 'importRecords', label: '导入记录', path: '/admin/products/import?tab=record' },
    ],
  },
  {
    key: 'task',
    label: '任务中心',
    badge: '任',
    children: [
      { key: 'taskAll', label: '全部任务', path: '/admin/tasks' },
      { key: 'taskCreate', label: '创建任务', path: '/admin/tasks/new' },
    ],
  },
  {
    key: 'report',
    label: '报告中心',
    badge: '报',
    children: [{ key: 'reportList', label: '报告列表', path: '/admin/reports' }],
  },
  {
    key: 'config',
    label: '配置中心',
    badge: '配',
    // 2.1 原型：配置中心只留页面识别配置与规则配置，规则测试并入规则配置（?tab=test）
    children: [
      { key: 'pageConfig', label: '页面配置', path: '/admin/config' },
      { key: 'ruleConfig', label: '规则配置', path: '/admin/rules', permission: 'qcRules' },
    ],
  },
  {
    key: 'feedback',
    label: '反馈中心',
    badge: '馈',
    children: [
      { key: 'myFeedback', label: '我的反馈', path: '/admin/feedback?tab=my' },
      { key: 'feedbackHandle', label: '反馈处理', path: '/admin/feedback?tab=handle' },
    ],
  },
  {
    key: 'analytics',
    label: '使用分析',
    badge: '析',
    permission: 'analytics',
    children: [
      { key: 'analyticsOverview', label: '公司使用概览', path: '/admin/analytics' },
      { key: 'analyticsTask', label: '任务和报告统计', path: '/admin/analytics?tab=task' },
      { key: 'analyticsAccount', label: '活跃账号分析', path: '/admin/analytics?tab=account' },
    ],
  },
  {
    key: 'resource',
    label: '资源治理',
    badge: '资',
    permission: 'costReports',
    children: [
      { key: 'tokenUsage', label: 'Token 与费用', path: '/admin/token-usage', permission: 'costReports' },
      { key: 'providers', label: 'AI 服务管理', path: '/admin/providers', permission: 'systemOperations' },
    ],
  },
  { key: 'system', label: '系统管理', badge: '系', path: '/admin/system', permission: 'system' },
]

/**
 * 两套门户共用的业务页面：任务、报告、快速质检、反馈、人工审核。
 * 管理后台下路由名加 `admin:` 前缀，页面内跳转走 usePortalNav 自动带 /admin。
 * 配置中心与规则测试只属于管理后台（见 adminOnlyRoutes）。
 */
function businessRoutes(portal: 'workstation' | 'admin'): RouteRecordRaw[] {
  const name = (key: string) => (portal === 'admin' ? `admin:${key}` : key)
  return [
    {
      path: 'quick',
      name: name('quickInspect'),
      component: () => import('@/modules/quick/pages/QuickInspectView.vue'),
      meta: { title: '快速质检' },
    },
    {
      path: 'feedback',
      name: name('feedback'),
      component: () => import('@/modules/feedback/pages/FeedbackView.vue'),
      meta: { title: portal === 'admin' ? '反馈中心' : '我的反馈' },
    },
    {
      path: 'tasks',
      name: name('taskList'),
      component: () => import('@/modules/tasks/pages/TaskListView.vue'),
      meta: { title: portal === 'admin' ? '全部任务' : '质检任务' },
    },
    {
      path: 'tasks/new',
      name: name('taskCreate'),
      component: () => import('@/modules/tasks/pages/TaskCreateView.vue'),
      meta: { title: '创建质检任务' },
    },
    {
      path: 'app-qc',
      name: name('appQcCreate'),
      // 兼容旧书签，但统一落到 V2 创建合同，避免旧 AppQcCreateView 继续提交 V1。
      redirect: { name: name('taskCreate'), query: { platform: 'app' } },
      meta: { title: '创建 App 任务' },
    },
    {
      path: 'tasks/:taskId',
      name: name('taskDetail'),
      component: () => import('@/modules/tasks/pages/TaskDetailView.vue'),
      meta: { title: '任务详情' },
    },
    {
      path: 'reports',
      name: name('reportCenter'),
      component: () => import('@/modules/reports/pages/ReportListView.vue'),
      meta: { title: '报告列表' },
    },
    {
      path: 'reports/:taskId',
      name: name('reportDetail'),
      component: () => import('@/modules/reports/pages/ReportCenterView.vue'),
      meta: { title: '报告详情' },
    },
    {
      path: 'reviews',
      name: name('reviewQueue'),
      component: () => import('@/modules/reviews/pages/ReviewQueueView.vue'),
      meta: { title: '人工审核' },
    },
  ]
}

/** 只在管理后台的配置页面：页面识别配置；旧「规则测试」地址并入规则配置的测试页签。 */
const adminOnlyRoutes: RouteRecordRaw[] = [
  {
    path: 'config',
    name: 'admin:configCenter',
    component: () => import('@/modules/config/pages/ConfigCenterView.vue'),
    meta: { title: '页面识别配置' },
  },
  {
    path: 'rule-test',
    name: 'admin:ruleTest',
    redirect: (to) => ({ name: 'adminQcRules', query: { ...to.query, tab: 'test' } }),
  },
]

export const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/modules/auth/pages/LoginView.vue'),
    meta: { title: '登录' },
  },
  {
    path: '/admin/login',
    name: 'adminLogin',
    component: () => import('@/modules/admin/pages/AdminLoginView.vue'),
    meta: { title: '管理后台登录', portal: 'admin' },
  },
  {
    path: '/admin',
    component: AdminLayout,
    meta: { portal: 'admin' },
    children: [
      {
        path: '',
        name: 'adminDashboard',
        component: () => import('@/modules/admin/pages/AdminOverviewView.vue'),
        meta: { title: '工作台', permission: 'dashboard' },
      },
      {
        path: 'analytics',
        name: 'adminAnalytics',
        component: () => import('@/modules/admin/pages/AdminAnalyticsView.vue'),
        meta: { title: '使用分析', permission: 'analytics' },
      },
      {
        path: 'tenants',
        name: 'adminTenants',
        component: () => import('@/modules/admin/pages/AdminTenantsView.vue'),
        meta: { title: '分公司管理', permission: 'tenants' },
      },
      {
        path: 'departments',
        name: 'adminDepartments',
        component: () => import('@/modules/admin/pages/AdminDepartmentsView.vue'),
        meta: { title: '部门管理', permission: 'departments' },
      },
      {
        path: 'users',
        name: 'adminUsers',
        component: () => import('@/modules/admin/pages/AdminUsersView.vue'),
        meta: { title: '账号与权限', permission: 'users' },
      },
      {
        path: 'roles',
        name: 'adminRoles',
        component: () => import('@/modules/admin/pages/AdminRolesView.vue'),
        meta: { title: '角色说明', permission: 'roles' },
      },
      {
        path: 'audit-logs',
        name: 'adminAuditLogs',
        component: () => import('@/modules/admin/pages/AdminAuditLogsView.vue'),
        meta: { title: '操作日志', permission: 'auditLogs' },
      },
      {
        path: 'token-usage',
        name: 'adminTokenUsage',
        component: () => import('@/modules/admin/pages/AdminTokenUsageView.vue'),
        meta: { title: 'Token 与费用', permission: 'costReports' },
      },
      {
        path: 'cost-reports',
        name: 'adminCostReports',
        component: () => import('@/modules/admin/pages/AdminCostReportsView.vue'),
        meta: { title: '模型调用明细', permission: 'costReports' },
      },
      {
        path: 'providers',
        name: 'adminProviders',
        component: () => import('@/modules/admin/pages/AdminProvidersView.vue'),
        meta: { title: 'AI 服务管理', permission: 'systemOperations' },
      },
      {
        path: 'system',
        name: 'adminSystem',
        component: () => import('@/modules/admin/pages/AdminSystemView.vue'),
        meta: { title: '系统管理', permission: 'system' },
      },
      {
        path: 'rules',
        name: 'adminQcRules',
        component: () => import('@/modules/admin/pages/AdminQcRulesView.vue'),
        meta: { title: '规则配置', permission: 'qcRules' },
      },
      {
        path: 'products',
        name: 'adminProducts',
        component: () => import('@/modules/products/pages/ProductListView.vue'),
        meta: { title: '产品列表' },
      },
      {
        path: 'products/import',
        name: 'adminProductImport',
        component: () => import('@/modules/admin/pages/AdminProductImportView.vue'),
        meta: { title: 'Excel 导入' },
      },
      ...businessRoutes('admin'),
      ...adminOnlyRoutes,
      // 兼容旧管理后台链接
      { path: 'config-center', redirect: { name: 'adminQcRules' } },
      { path: 'config-center/qc-rules', redirect: { name: 'adminQcRules' } },
      { path: 'config-center/products', redirect: { name: 'adminProducts' } },
      { path: 'qc-rules', redirect: { name: 'adminQcRules' } },
    ],
  },
  {
    path: '/',
    component: WorkstationLayout,
    children: [
      {
        path: '',
        name: 'dashboard',
        component: () => import('@/modules/dashboard/pages/DashboardView.vue'),
        meta: { title: '工作台' },
      },
      {
        path: 'products',
        name: 'productList',
        component: () => import('@/modules/products/pages/ProductListView.vue'),
        meta: { title: '产品管理' },
      },
      {
        path: 'rules',
        name: 'ruleCenter',
        component: () => import('@/modules/rules/pages/RuleCenterView.vue'),
        meta: { title: '规则中心' },
      },
      ...businessRoutes('workstation'),
    ],
  },
]
