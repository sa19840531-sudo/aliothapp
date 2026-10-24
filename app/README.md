# app/ 前端应用

存放平台的前端项目，均基于 **Vite + Vue + TypeScript** 构建。

## 子项目

| 目录 | 说明 |
| --- | --- |
| `workstation/src/modules/admin/` | 体验中心管理后台（与工作台同一个应用，路由前缀 `/admin`，树形菜单见 `router/routes.ts` 的 `adminMenuTree`）：运营总览、使用分析、分公司/部门/账号与权限、规则配置、Excel 产品导入、Token 与费用、AI 服务管理、系统管理；任务/报告/反馈等业务页复用部门平台页面 |
| [workstation/](workstation/README.md) | 质检工作台：任务提交、执行进度查看、质检报告查看、人工审核队列 |
| [shared/](shared/README.md) | 前端共享包：通用 UI 组件、API client、TypeScript 类型定义 |

## 约定

- 每个子项目是独立的 Vite 项目，可独立开发、独立部署。
- 公共能力（UI 组件、请求封装、后端接口类型）下沉到 `shared/`，避免管理后台模块与部门平台页面间出现重复实现。
- 新增页面前，先确认对应的核心实体、接口契约、路由与菜单模型（见 [AGENTS.md](../AGENTS.md) 架构基线），禁止先堆页面再反推数据结构。
- 前端调用的后端接口见 [services/api/](../services/api/README.md)。
