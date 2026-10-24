# Alioth Workstation Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first runnable `app/workstation` Vue 3 + TypeScript + Naive UI front-end with route/menu structure, task API boundaries, and first-pass workbench pages.

**Architecture:** Implement `app/workstation` as an independent Vite application. Keep pages grouped by business module, keep API access inside `src/services`, and keep backend response contracts inside `src/types`.

**Tech Stack:** Vite 8.1.1, Vue 3.5.39, TypeScript 6.0.2, vue-router 4.6.4, Naive UI 2.44.1, @vicons/ionicons5 0.13.0.

## Global Constraints

- Workstation lives under `app/workstation`.
- Use Vue 3 + TypeScript + Naive UI.
- Treat `docs/ui/workstation` HTML files as visual and interaction references only.
- Do not copy large static HTML blocks, Tailwind classes, or inline SVGs into production Vue pages.
- Configuration pages remain in `app/admin`; workstation only covers task submission, task execution viewing, reports, and review queue.
- Page development order follows domain entity, interface contract, route/menu model, then Vue components.
- Frontend pages must call backend through service files, not direct `fetch` calls in view files.
- No database migration is introduced by this frontend work.
- All code comments must be Chinese and UTF-8 encoded.

---

## File Structure

Create the workstation application with these responsibilities:

- `app/workstation/package.json`: npm scripts and dependencies aligned with `app/admin`.
- `app/workstation/vite.config.ts`: Vite Vue plugin and `@` alias.
- `app/workstation/tsconfig*.json`: TypeScript project configuration aligned with `app/admin`.
- `app/workstation/index.html`: workstation HTML entry.
- `app/workstation/src/main.ts`: Vue app bootstrap with Naive UI provider.
- `app/workstation/src/App.vue`: top-level app shell.
- `app/workstation/src/router/routes.ts`: route table and menu metadata.
- `app/workstation/src/router/index.ts`: router instance.
- `app/workstation/src/layouts/WorkstationLayout.vue`: top navigation and content layout.
- `app/workstation/src/types/*.ts`: task, rule execution, agent run, review, and evidence contracts.
- `app/workstation/src/services/*.ts`: HTTP and API wrappers.
- `app/workstation/src/components/status/*.vue`: reusable status presentation.
- `app/workstation/src/modules/*/pages/*.vue`: first-pass pages for dashboard, tasks, reports, and reviews.
- `app/workstation/src/styles/*.css`: workstation design tokens and global styling.

---

### Task 1: Workstation Vite Application Skeleton

**Files:**
- Create: `app/workstation/package.json`
- Create: `app/workstation/index.html`
- Create: `app/workstation/vite.config.ts`
- Create: `app/workstation/tsconfig.json`
- Create: `app/workstation/tsconfig.app.json`
- Create: `app/workstation/tsconfig.node.json`
- Create: `app/workstation/src/main.ts`
- Create: `app/workstation/src/App.vue`
- Create: `app/workstation/src/styles/tokens.css`
- Create: `app/workstation/src/styles/workstation.css`
- Modify: `app/workstation/README.md`

**Interfaces:**
- Produces: a buildable Vite Vue application rooted at `app/workstation/src/main.ts`.
- Produces: global CSS variables used by layout and pages.

- [ ] **Step 1: Create package metadata**

Use the same dependency versions as `app/admin`:

```json
{
  "name": "workstation",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc -b && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "@vicons/ionicons5": "^0.13.0",
    "naive-ui": "^2.44.1",
    "vue": "^3.5.39",
    "vue-router": "^4.6.4"
  },
  "devDependencies": {
    "@types/node": "^24.13.2",
    "@vitejs/plugin-vue": "^6.0.7",
    "@vue/tsconfig": "^0.9.1",
    "typescript": "~6.0.2",
    "vite": "^8.1.1",
    "vue-tsc": "^3.3.5"
  }
}
```

- [ ] **Step 2: Add Vite and TypeScript configuration**

`app/workstation/vite.config.ts`:

```ts
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})
```

`app/workstation/tsconfig.json`:

```json
{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ]
}
```

`app/workstation/tsconfig.app.json`:

```json
{
  "extends": "@vue/tsconfig/tsconfig.dom.json",
  "include": ["env.d.ts", "src/**/*", "src/**/*.vue"],
  "exclude": ["src/**/__tests__/*"],
  "compilerOptions": {
    "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.app.tsbuildinfo",
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  }
}
```

`app/workstation/tsconfig.node.json`:

```json
{
  "extends": "@tsconfig/node24/tsconfig.json",
  "include": ["vite.config.*"],
  "compilerOptions": {
    "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.node.tsbuildinfo",
    "composite": true,
    "types": ["node"]
  }
}
```

- [ ] **Step 3: Add application entry files**

`app/workstation/index.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Alioth 质检工作台</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

`app/workstation/src/main.ts`:

```ts
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './styles/tokens.css'
import './styles/workstation.css'

createApp(App).use(router).mount('#app')
```

`app/workstation/src/App.vue`:

```vue
<template>
  <n-config-provider>
    <n-message-provider>
      <router-view />
    </n-message-provider>
  </n-config-provider>
</template>
```

- [ ] **Step 4: Add workstation design tokens**

Define `tokens.css` and `workstation.css` with dark workbench colors, readable text, fixed header sizing, and responsive content width. Use CSS variables prefixed with `--workstation-`.

- [ ] **Step 5: Update README**

Update `app/workstation/README.md` to describe the initialized Vite project, local startup, build command, current route scope, and the rule that HTML designs remain references.

- [ ] **Step 6: Verify skeleton**

Run:

```bash
npm install
npm run build
```

Expected: build exits with code 0 from `app/workstation`.

- [ ] **Step 7: Commit**

```bash
git add app/workstation
git commit -m "[AI编程] feat: 初始化工作台前端工程骨架" -m "涉及模块：app/workstation。" -m "数据库迁移：否。" -m "前端菜单/路由：仅建立工程入口，后续任务接入。" -m "测试风险：已运行工作台前端构建。"
```

---

### Task 2: Route and Layout Baseline

**Files:**
- Create: `app/workstation/src/router/routes.ts`
- Create: `app/workstation/src/router/index.ts`
- Create: `app/workstation/src/layouts/WorkstationLayout.vue`
- Create: `app/workstation/src/modules/dashboard/pages/DashboardView.vue`
- Create: `app/workstation/src/modules/tasks/pages/TaskListView.vue`
- Create: `app/workstation/src/modules/tasks/pages/TaskCreateView.vue`
- Create: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Create: `app/workstation/src/modules/reports/pages/ReportCenterView.vue`
- Create: `app/workstation/src/modules/reports/pages/ReportDetailView.vue`
- Create: `app/workstation/src/modules/reviews/pages/ReviewQueueView.vue`

**Interfaces:**
- Consumes: Vue app entry from Task 1.
- Produces: exported `routes` array and default router.
- Produces: menu metadata with route names `dashboard`, `taskList`, `taskCreate`, `taskDetail`, `reportCenter`, `reportDetail`, `reviewQueue`.

- [ ] **Step 1: Define route metadata**

`app/workstation/src/router/routes.ts`:

```ts
import type { RouteRecordRaw } from 'vue-router'
import WorkstationLayout from '@/layouts/WorkstationLayout.vue'

export interface WorkstationMenuItem {
  key: string
  label: string
  path: string
}

export const workstationMenuItems: WorkstationMenuItem[] = [
  { key: 'dashboard', label: '工作台', path: '/' },
  { key: 'taskList', label: '质检任务', path: '/tasks' },
  { key: 'reportCenter', label: '报告中心', path: '/reports' },
  { key: 'reviewQueue', label: '人工审核', path: '/reviews' },
]

export const routes: RouteRecordRaw[] = [
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
        path: 'tasks',
        name: 'taskList',
        component: () => import('@/modules/tasks/pages/TaskListView.vue'),
        meta: { title: '质检任务' },
      },
      {
        path: 'tasks/new',
        name: 'taskCreate',
        component: () => import('@/modules/tasks/pages/TaskCreateView.vue'),
        meta: { title: '创建质检任务' },
      },
      {
        path: 'tasks/:taskId',
        name: 'taskDetail',
        component: () => import('@/modules/tasks/pages/TaskDetailView.vue'),
        meta: { title: '任务详情' },
      },
      {
        path: 'reports',
        name: 'reportCenter',
        component: () => import('@/modules/reports/pages/ReportCenterView.vue'),
        meta: { title: '报告中心' },
      },
      {
        path: 'reports/:taskId',
        name: 'reportDetail',
        component: () => import('@/modules/reports/pages/ReportDetailView.vue'),
        meta: { title: '报告详情' },
      },
      {
        path: 'reviews',
        name: 'reviewQueue',
        component: () => import('@/modules/reviews/pages/ReviewQueueView.vue'),
        meta: { title: '人工审核' },
      },
    ],
  },
]
```

- [ ] **Step 2: Create router instance**

`app/workstation/src/router/index.ts`:

```ts
import { createRouter, createWebHistory } from 'vue-router'
import { routes } from './routes'

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
```

- [ ] **Step 3: Build the layout**

`WorkstationLayout.vue` must render a fixed top bar, four menu entries from `workstationMenuItems`, current route title, "未接入鉴权" status tag, and `<router-view />`. Use Naive UI `NLayout`, `NLayoutHeader`, `NLayoutContent`, `NMenu`, `NTag`, `NButton`, and Ionicons.

- [ ] **Step 4: Add placeholder pages**

Each page must render a concise page title, a one-sentence scope statement, and a visible empty state using Naive UI. Do not include direct API calls.

- [ ] **Step 5: Verify routes compile**

Run:

```bash
npm run build
```

Expected: build exits with code 0 from `app/workstation`.

- [ ] **Step 6: Commit**

```bash
git add app/workstation/src
git commit -m "[AI编程] feat: 建立工作台路由与布局基线" -m "涉及模块：app/workstation 路由、布局、首批页面。" -m "数据库迁移：否。" -m "前端菜单/路由：新增工作台、质检任务、报告中心、人工审核菜单。" -m "测试风险：已运行工作台前端构建。"
```

---

### Task 3: Type and Service Contracts

**Files:**
- Create: `app/workstation/src/types/task.ts`
- Create: `app/workstation/src/types/rule-execution.ts`
- Create: `app/workstation/src/types/agent-run.ts`
- Create: `app/workstation/src/types/review.ts`
- Create: `app/workstation/src/types/evidence.ts`
- Create: `app/workstation/src/services/http.ts`
- Create: `app/workstation/src/services/tasks.api.ts`
- Create: `app/workstation/src/services/agents.api.ts`
- Create: `app/workstation/src/services/reports.api.ts`
- Create: `app/workstation/src/services/reviews.api.ts`

**Interfaces:**
- Produces: `requestJson<T>(path: string, init?: RequestInit): Promise<T>`.
- Produces: `submitTask(payload: SubmitTaskPayload): Promise<SubmitTaskResponse>`.
- Produces: `listTasks(params?: ListTasksParams): Promise<TaskListResponse>`.
- Produces: `getTask(taskId: string): Promise<TaskDetail>`.
- Produces: `listRuleExecutions(taskId: string): Promise<RuleExecutionListResponse>`.
- Produces: `listCandidateRules(taskId: string, pageType?: string): Promise<CandidateRuleListResponse>`.
- Produces: `getAgentRun(runId: string): Promise<AgentRunStatus>`.

- [ ] **Step 1: Define task contracts**

Mirror the backend response fields from `services/api/app/api/v1/tasks.py`. Use union types for task statuses and channels. Use `string` for datetime fields at the frontend boundary.

- [ ] **Step 2: Define rule execution and candidate rule contracts**

Create explicit interfaces for `RuleExecutionSummary`, `RuleExecutionListResponse`, `CandidateRule`, and `CandidateRuleListResponse`.

- [ ] **Step 3: Define agent run contracts**

Create interfaces for `ActionTraceEntry` and `AgentRunStatus` aligned with `services/api/app/api/v1/agents.py`.

- [ ] **Step 4: Define review and evidence contracts**

Create narrow placeholder-ready types for review status, trigger reason, evidence artifact type, and evidence preview item. These types allow page composition without pretending the write APIs exist.

- [ ] **Step 5: Implement HTTP wrapper**

`http.ts` must read `import.meta.env.VITE_ALIOTH_API_BASE_URL`, default to `/api/v1`, parse JSON, and throw `AliothHttpError` with `status`, `message`, and optional `details`.

- [ ] **Step 6: Implement task and agent service wrappers**

Implement all task functions using `requestJson`. Implement `reports.api.ts` and `reviews.api.ts` as read-oriented adapters over task contracts so report and review pages do not import task API files directly.

- [ ] **Step 7: Verify service contracts compile**

Run:

```bash
npm run build
```

Expected: build exits with code 0 from `app/workstation`.

- [ ] **Step 8: Commit**

```bash
git add app/workstation/src/types app/workstation/src/services
git commit -m "[AI编程] feat: 建立工作台接口类型与服务边界" -m "涉及模块：app/workstation 类型与 API service。" -m "数据库迁移：否。" -m "前端菜单/路由：无变更。" -m "测试风险：已运行工作台前端构建；真实接口联调需后端服务启动。"
```

---

### Task 4: Reusable Status Components and Mock Data

**Files:**
- Create: `app/workstation/src/components/status/TaskStatusTag.vue`
- Create: `app/workstation/src/components/status/RuleExecutionStatusTag.vue`
- Create: `app/workstation/src/components/status/ReviewStatusTag.vue`
- Create: `app/workstation/src/components/status/SeverityBadge.vue`
- Create: `app/workstation/src/modules/tasks/mock/task-fixtures.ts`

**Interfaces:**
- Consumes: status union types from Task 3.
- Produces: reusable components with props `status` or `severity`.
- Produces: `mockTasks`, `mockRuleExecutions`, `mockCandidateRules`, and `mockAgentRun`.

- [ ] **Step 1: Create status mapping helpers inside components**

Each tag component must map known statuses to Chinese labels and Naive UI tag types. Unknown strings render as `未知状态` with default type.

- [ ] **Step 2: Create task fixtures**

Create realistic static records for task list, detail, rule execution, candidate rules, and one agent run. Fixtures must use fields from Task 3 contracts.

- [ ] **Step 3: Verify status components compile**

Run:

```bash
npm run build
```

Expected: build exits with code 0 from `app/workstation`.

- [ ] **Step 4: Commit**

```bash
git add app/workstation/src/components/status app/workstation/src/modules/tasks/mock
git commit -m "[AI编程] feat: 增加工作台状态展示组件" -m "涉及模块：app/workstation 状态组件与页面 mock 数据。" -m "数据库迁移：否。" -m "前端菜单/路由：无变更。" -m "测试风险：已运行工作台前端构建；mock 数据仅用于首屏展示。"
```

---

### Task 5: First Workstation Pages

**Files:**
- Modify: `app/workstation/src/modules/dashboard/pages/DashboardView.vue`
- Modify: `app/workstation/src/modules/tasks/pages/TaskListView.vue`
- Modify: `app/workstation/src/modules/tasks/pages/TaskCreateView.vue`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Modify: `app/workstation/src/modules/reports/pages/ReportCenterView.vue`
- Modify: `app/workstation/src/modules/reports/pages/ReportDetailView.vue`
- Modify: `app/workstation/src/modules/reviews/pages/ReviewQueueView.vue`

**Interfaces:**
- Consumes: routes from Task 2.
- Consumes: types, services, status components, and fixtures from Tasks 3 and 4.
- Produces: navigable first-pass pages using Naive UI components.

- [ ] **Step 1: Build dashboard page**

Render four metric cards, recent task table, and "创建任务" entry. Use `mockTasks` and route to `/tasks/new`.

- [ ] **Step 2: Build task list page**

Render status filter buttons, task table, and actions to view task detail. Use `TaskStatusTag`.

- [ ] **Step 3: Build create task page**

Render form fields `target_url`, `ruleset_code`, `ruleset_version`, `channel`, `priority`, and `task_config`. On submit, show a success message with a mock `task_id` when backend is unavailable. Keep the `submitTask` service import so real integration is one replacement point.

- [ ] **Step 4: Build task detail page**

Render task summary, rule execution table, candidate rule list, and agent action trace timeline. Use route param `taskId` and fixtures for first-pass display.

- [ ] **Step 5: Build report center and report detail pages**

Render report list from tasks and derive risk summary from rule executions. Report detail reuses the task detail data shape and shows evidence empty states.

- [ ] **Step 6: Build review queue page**

Render read-only review queue items derived from manual review or low-confidence rule executions. Show operation buttons disabled with explanatory text that write APIs are not connected in this phase.

- [ ] **Step 7: Verify pages compile**

Run:

```bash
npm run build
```

Expected: build exits with code 0 from `app/workstation`.

- [ ] **Step 8: Commit**

```bash
git add app/workstation/src
git commit -m "[AI编程] feat: 落地工作台首批页面" -m "涉及模块：app/workstation 工作台、任务、报告、人审页面。" -m "数据库迁移：否。" -m "前端菜单/路由：使用既有工作台路由并补齐页面内容。" -m "测试风险：已运行工作台前端构建；真实接口联调和浏览器视觉验收另行执行。"
```

---

## Self-Review

- Spec coverage: Tasks 1-5 cover independent Vite project, route/menu model, service layer, frontend types, first-pass pages, status mapping, and HTML-as-reference workflow.
- Scope control: Backend evidence storage, report export, review write-back, authentication, and shared package extraction are intentionally outside this first implementation slice.
- Type consistency: Service names and type names in page tasks match the contracts introduced in Task 3.
- Verification: Each task ends with `npm run build` from `app/workstation`.
