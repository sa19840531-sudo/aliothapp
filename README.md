# Alioth（玉衡）AI 自动化质检平台

Alioth 是一个 AI Agent 驱动的自动化质检平台，面向 Web 与 H5 产品做自动化质量检查：质检规则以自然语言描述"检查什么"，由 Agent 自主完成页面探索、证据采集与规则判定，命中问题产出结构化报告并流转人工审核。

> 当前仓库已推进到 **v1.5 运行闭环收口阶段**：FastAPI、Redis 任务队列、Worker、Agent Runtime、H5 V2 探索、App Runtime、页面识别、证据 Manifest、规则执行、Completion Gate、质检工作台、管理后台与成本报表均已落地。后续重点不再是骨架补齐，而是围绕真实产品验收、H5/App 探索稳定性、补证闭环、可观测解释和多人分工协作做生产化收口。

## 技术栈

| 层面 | 选型 |
| --- | --- |
| 后端 | Python + FastAPI |
| 数据库 | PostgreSQL（+ pgvector，向量检索） |
| 前端 | Vite + Vue + TypeScript |
| 浏览器自动化 | Playwright |
| AI 编排 | Microsoft Agent Framework（Python） |

完整的架构设计、数据模型、Agent Loop 编排细节见 [docs/design/](docs/design/README.md)。

## 仓库结构

| 目录 | 说明 |
| --- | --- |
| [app/](app/README.md) | 前端应用（管理后台 admin、质检工作台 workstation、共享包 shared） |
| [services/](services/README.md) | 后端服务（FastAPI 单体应用，内部分层：接口层 / 领域层 / 应用层 / AI Agent 层 / 基建层） |
| [docs/](docs/README.md) | 架构设计文档、规则数据模型、Agent Loop 编排方案 |
| [infra/](infra/README.md) | 部署与运维配置（Docker Compose、GitLab CI/CD、生产验收与回滚脚本） |
| [scripts/](scripts/README.md) | 开发、数据初始化、规则同步、运维辅助与验收脚本 |

## 架构原则（摘自 [AGENTS.md](AGENTS.md)）

1. 领域边界清晰
2. 权限模型可扩展
3. 接口命名一致
4. 数据结构稳定
5. 审计链路完整
6. 前端可维护性高

设计基线：先定义领域（核心实体、实体关系、权限模型、接口契约、前端路由与菜单模型），再写页面；默认采用"分层清晰的单体架构"，不过早微服务化。

## 演进路线

| 阶段 | 编排方式 | 目标 |
| --- | --- | --- |
| v1 | 单一 Main Agent 自主 Loop + 阶段/规则 Skill 分层 | 单 Agent 跑通全链路，验证 Skill/Tool 契约与规则模型 |
| v2+v3（合并） | Agent Orchestrator + Agent Loop Runtime + 专职 Agent 群 + 内建人审 | 多 Agent 生产级编排，人审内建为一等公民 |
| v4 | + 记忆引擎 | 分层记忆、反馈学习、规则与 Prompt 自进化 |

详见 [最终整合方案设计](docs/design/2026-07-03-alioth-integrated-solution-design.md)。

## 贡献约定

- 提交说明至少交代：涉及模块、是否引入数据库迁移、是否需前端同步菜单/路由、未覆盖的测试风险，并标注 `[AI编程]`（若由 AI 编写）。
- 注释统一使用中文（UTF-8）。
- 保证单文件职责单一，禁止单个文件堆砌臃肿代码。
- 完整规范见 [AGENTS.md](AGENTS.md)。
