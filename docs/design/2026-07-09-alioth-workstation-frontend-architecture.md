# Alioth 工作台前端开发架构设计

## 1. 背景

`app/workstation` 是面向质检人员的工作台前端，对应任务提交、执行进度查看、质检报告查看和人工审核队列。当前仓库中 `app/workstation` 仍是占位目录，`docs/ui/workstation` 已有工作台、创建质检任务、任务详情、报告中心等 HTML 设计稿；后端 `services/api/app/api/v1/tasks.py` 已提供任务提交、任务列表、任务详情、规则执行结果和候选规则查询接口雏形。

本设计的目标是先建立稳定的工作台前端开发架构，再按页面逐步迁移 HTML 设计稿，避免直接搬运静态页面导致实体、接口、状态和审计链路后续返工。

## 2. 设计目标

- 工作台作为独立 Vite + Vue 3 + TypeScript 前端项目，位于 `app/workstation`。
- 页面开发遵循“领域实体 -> 接口契约 -> 路由菜单 -> 页面组件”的顺序。
- 复用 Naive UI 和管理后台已有工程习惯，但工作台布局、信息密度和交互重点面向质检作业场景。
- `docs/ui/workstation` 仅作为视觉与交互参考，不把 HTML 原样复制为生产页面。
- 可复用的状态枚举、接口类型、HTTP client 和通用展示组件后续下沉到 `app/shared`，避免 `admin` 与 `workstation` 重复实现。

## 3. 范围边界

本轮工作台前端只覆盖质检作业链路：

- 工作台首页：任务概览、执行状态、快捷入口。
- 创建质检任务：提交目标 URL、规则集、渠道、优先级和巡检配置。
- 任务列表与任务详情：查看任务状态、规则执行结果、候选规则和 Agent 执行留痕。
- 报告中心：以任务和规则执行结果为主线展示质检结论。
- 人工审核队列：展示待审核项和三态处理入口。

不纳入工作台的内容：

- 用户、角色、权限、审计日志等管理后台能力。
- 规则库、敏感词库、品牌词库、知识库、Agent 配置等配置类页面。
- 后端尚未落地的证据对象存储、报告导出、人审写回接口，前端只保留可替换的 service 边界与空状态。

## 4. 推荐架构

采用“契约优先的模块化工作台前端”：

1. 先用后端已落地的 Pydantic 响应模型定义前端 TypeScript 类型。
2. 再建立 `services` 层封装 API 调用，页面不直接拼接请求。
3. 以业务模块拆分页面、组件和组合逻辑。
4. 最后按 HTML 设计稿迁移视觉结构，优先使用 Naive UI 组件和局部 CSS 变量。

不推荐直接 HTML 转 Vue。该方式虽然快，但容易把静态样式、假数据、硬编码状态和页面结构耦合在一起，不利于后续接入真实任务状态、Agent 留痕和人工审核流转。

## 5. 目录结构

```text
app/workstation/
  index.html
  package.json
  vite.config.ts
  tsconfig.json
  src/
    main.ts
    App.vue
    router/
      index.ts
      routes.ts
    layouts/
      WorkstationLayout.vue
    modules/
      dashboard/
        pages/
        components/
      tasks/
        pages/
        components/
        composables/
      reports/
        pages/
        components/
      reviews/
        pages/
        components/
    services/
      http.ts
      tasks.api.ts
      agents.api.ts
      reports.api.ts
      reviews.api.ts
    types/
      task.ts
      rule-execution.ts
      agent-run.ts
      evidence.ts
      review.ts
    components/
      status/
      evidence/
      task/
    styles/
      tokens.css
      workstation.css
```

模块职责：

- `router`：维护路由表、页面标题和菜单元信息。
- `layouts`：工作台整体布局，包含顶部导航、用户区、内容容器和移动端适配。
- `modules`：按业务模块承载页面和模块内组件。
- `services`：封装后端接口调用和错误归一化。
- `types`：承接后端接口契约，保持状态枚举稳定。
- `components`：放跨模块复用的小组件，例如状态标签、严重级别标记、证据预览面板。
- `styles`：放工作台设计令牌和全局样式。

## 6. 路由与菜单模型

首批路由建议如下：

| 路由 | 页面 | 来源设计稿 | 说明 |
| --- | --- | --- | --- |
| `/` | 工作台首页 | `自动化质检平台-工作台.html` | 概览、指标、最近任务、快捷入口 |
| `/tasks` | 任务列表 | `自动化质检平台-工作台.html` | 任务查询、状态筛选、进入详情 |
| `/tasks/new` | 创建质检任务 | `自动化质检平台-创建质检任务0.html` | 提交质检 URL 与任务配置 |
| `/tasks/:taskId` | 任务详情 | `自动化质检平台-任务详情.html` | 任务状态、规则执行、Agent 留痕 |
| `/reports` | 报告中心 | `自动化质检平台-报告中心 1.0.html` | 报告列表、风险概览 |
| `/reports/:taskId` | 报告详情 | `自动化质检平台-任务详情.html` | 问题清单与证据详情 |
| `/reviews` | 人工审核队列 | 无独立原型，参考任务详情 | 待审核项、确认、驳回、需复核 |

菜单首批保持四项：工作台、质检任务、报告中心、人工审核。配置类入口不出现在工作台菜单中。

## 7. 数据契约与 API 边界

首批页面以现有任务接口为主：

- `POST /tasks`：创建质检任务。
- `GET /tasks`：查询任务列表。
- `GET /tasks/{task_id}`：查询任务详情。
- `GET /tasks/{task_id}/rule-executions`：查询规则执行结果。
- `GET /tasks/{task_id}/candidate-rules`：查询候选规则。
- `GET /agents/runs/{run_id}`：短期用于查询异步 Agent Run 状态和 action trace。

`/agents/runs/{run_id}` 只作为短期适配层。页面通过 `agents.api.ts` 调用，不直接绑定到视图组件；后续如果任务域提供正式留痕查询接口，只替换 service 层。

前端状态模型需稳定映射：

- 任务状态：`queued`、`running`、`review`、`done`、`failed`、`canceled`。
- 规则执行状态：`pass`、`fail`、`warning`、`manual_review`、`evidence_insufficient`、`skipped`、`error`。
- 审核状态：`pending`、`approved`、`rejected`、`needs_recheck`。

## 8. 页面组件拆分

任务模块：

- `TaskCreateView.vue`：创建质检任务页面。
- `TaskListView.vue`：任务列表页面。
- `TaskDetailView.vue`：任务详情页面。
- `TaskSubmitForm.vue`：任务提交表单。
- `TaskStatusTag.vue`：任务状态标签。
- `RuleExecutionTable.vue`：规则执行结果表格。
- `CandidateRuleList.vue`：候选规则列表。
- `AgentTraceTimeline.vue`：Agent 执行留痕时间线。

报告模块：

- `ReportCenterView.vue`：报告中心列表。
- `ReportDetailView.vue`：报告详情。
- `FindingSummaryPanel.vue`：问题摘要。
- `EvidencePreviewPanel.vue`：证据预览。
- `SeverityBadge.vue`：严重级别标记。

审核模块：

- `ReviewQueueView.vue`：人工审核队列。
- `ReviewDecisionPanel.vue`：确认、驳回、需复核操作区。
- `ReviewReasonModal.vue`：驳回原因填写。

## 9. 状态管理与错误处理

首阶段不引入 Pinia。任务创建、详情轮询和列表查询先通过 Composition API 局部管理，避免过早扩大全局状态面。

统一处理：

- `services/http.ts` 负责基础 URL、请求头、JSON 解析和错误归一化。
- 页面组件负责展示 loading、empty、error、retry 状态。
- 轮询逻辑封装在 composable 中，并在组件卸载时停止。
- 所有用户可见错误使用 Naive UI message 或 alert，不暴露后端堆栈。

后续当跨页面缓存、用户会话、权限菜单或全局通知变复杂时，再引入 Pinia。

## 10. 视觉迁移策略

HTML 设计稿的处理原则：

- 保留信息结构、页面层级、指标卡片、任务列表、时间线、报告面板等交互意图。
- 不直接复用 Tailwind class 和内联 SVG。
- 优先使用 Naive UI 的表格、表单、标签、步骤、时间线、抽屉和弹窗。
- 工作台可以保留较深色、作业感更强的视觉方向，但通过 `tokens.css` 管理颜色、间距、阴影和状态色。
- 移动端先保证不破版，首阶段以桌面质检作业效率为主。

## 11. 迭代顺序

第一阶段：工程骨架

- 初始化 `app/workstation` Vite + Vue + TypeScript 项目。
- 接入 Naive UI、vue-router、基础布局、菜单和全局样式。
- 建立 `types` 与 `services` 基线。

第二阶段：主链路

- 实现创建质检任务页面。
- 实现任务列表与任务详情页面。
- 接入任务提交、任务列表、任务详情、规则执行结果接口。
- 用短期适配层展示 Agent Run 状态和 action trace。

第三阶段：报告与审核

- 实现报告中心和报告详情。
- 实现人工审核队列的只读列表与操作入口。
- 等后端审核写回接口稳定后接入确认、驳回、需复核操作。

第四阶段：共享下沉

- 将任务状态、规则执行状态、严重级别颜色、HTTP client 和通用状态组件评估下沉到 `app/shared`。
- 对齐 `admin` 与 `workstation` 的基础工程约定。

## 12. 验收标准

- 工作台工程可独立安装、启动和构建。
- 路由与菜单覆盖首批工作台页面。
- 创建任务到任务详情的主链路可以使用真实接口或明确 mock 数据跑通。
- 页面组件不直接调用 `fetch`，统一经过 service 层。
- HTML 设计稿被拆解为 Vue 页面和组件，不出现大段静态 HTML 搬运。
- 任务状态、规则执行状态、审核状态有统一类型定义和展示映射。
- 不引入数据库迁移。
- 不影响 `app/admin` 现有页面、菜单和路由。

## 13. 风险与后续确认

- 证据域和人工审核写回接口尚未完全落地，报告详情和审核队列首阶段需要保留空状态和 service 替换点。
- `/agents/runs/{run_id}` 当前偏调试/异步运行查询语义，作为工作台执行留痕来源需在后续任务域接口稳定后替换。
- `app/shared` 目前只有说明文档，是否在首阶段就初始化为可被 `admin` 与 `workstation` 共同引用的包，需要结合工程成本单独确认。
