# app/workstation 质检工作台

面向质检人员的工作台前端，技术栈为 Vite + Vue 3 + TypeScript + Naive UI。对应设计文档中的“质检工作台 Web”接入层。

## 职责范围

- **任务提交**：提交质检 URL、选择规则集、渠道、优先级和巡检配置。
- **执行进度查看**：查看任务状态、规则执行结果、候选规则和 Agent 执行留痕。
- **质检报告查看**：展示结构化问题清单、严重级别、置信度和证据入口。
- **人工审核队列**：展示待确认、驳回、需复核的审核项。

## 页面范围

- `/`：工作台首页
- `/tasks`：质检任务列表
- `/tasks/new`：创建质检任务
- `/tasks/:taskId`：任务详情
- `/reports`：报告中心
- `/reports/:taskId`：报告详情
- `/reviews`：人工审核队列

## 本地启动

```bash
npm install
npm run dev
```

## 构建

```bash
npm run build
```

## 约定

- `docs/ui/workstation` 下的 HTML 文件仅作为视觉与交互参考，不直接搬运为生产页面。
- 页面不直接调用 `fetch`，后端请求统一放在 `src/services`。
- 接口响应类型统一放在 `src/types`，保持任务状态、规则执行状态和审核状态稳定。
