# docs/design/

设计文档存放目录，命名规范：`YYYY-MM-DD-<topic>.md`。

成组的专项设计可放入主题目录，优先维护现有文档。展示格式按阅读需要保留，避免重复内容和已完结的一次性计划长期堆积。

## 现有文档

| 文档 | 定位 |
| --- | --- |
| [2026-06-30-alioth-platform-design.md](2026-06-30-alioth-platform-design.md) | 总体设计文档：背景目标、分阶段路线图（v1~v4）、规则数据模型地基、技术选型 |
| [2026-07-01-alioth-qc-workflow-detail.md](2026-07-01-alioth-qc-workflow-detail.md) | 模块交互流程细化：数据源 CRUD 与 Skill 取数、Playwright 两级证据分类、Main Agent 批量质检循环、人工审核流程 |
| [2026-07-01-alioth-agent-loop-design.md](2026-07-01-alioth-agent-loop-design.md) | Agent Loop 编排设计：Skill/Tool 工具箱、思考-行动-观察循环、与传统编程模式的对比、Skill 化论证（2026-07-05 起 Skill/Tool 术语订正见文中提示） |
| [2026-07-03-alioth-integrated-solution-design.md](2026-07-03-alioth-integrated-solution-design.md) | **权威整合版本**：V1 单 Agent 的阶段 Skill / 规则 Skill / Tool 三层细化；V2+V3 合并后的 Agent Orchestrator + Agent Loop Runtime 多 Agent 架构，人审内建为 Review Agent（2026-07-05 起 Skill/Tool 术语订正见文中提示） |
| [2026-07-03-alioth-core-schema-design.md](2026-07-03-alioth-core-schema-design.md) | 当前版本核心表结构设计：规则集、规则、任务、页面采集、证据、Observation Packet、规则执行、人审、Agent 留痕、Agent Capability Registry（`capability_kind` 枚举值 2026-07-05 起术语订正见文中提示） |
| [implementation-roadmap.md](implementation-roadmap.md) | 工程落地路线：M0 文档契约、M1 后端基线、M2 核心表、M3 最小 API、M4 Runtime、M5 证据采集、M6 Workstation |
| [2026-07-05-alioth-agent-skill-tool-dev-guide.md](2026-07-05-alioth-agent-skill-tool-dev-guide.md) | **开发指南（非设计文档）**：现状 Agent Runtime/Agent Loop 逻辑回顾、架构审查结论与缺口清单、Skill/Tool 开发步骤与代码模板、集成方式、面向质检业务的开发路线建议 |
| [2026-07-08-alioth-native-app-qc-platform-branch.md](2026-07-08-alioth-native-app-qc-platform-branch.md) | **原生 APP 质检平台分支修订稿**：Web 与 APP 并列、不替换 Playwright；共用领域结果模型，分叉 `infrastructure/device` 与设备 Tool |
| [2026-07-10-alioth-app-automation-overall-solution.md](2026-07-10-alioth-app-automation-overall-solution.md) | **APP 自动化整体方案（含正式部署）**：当前 Demo 架构、dev/prod 双阶段、Host Runner 安全、正式机安装验收 SOP、真机/WiFi/Tool 演进 |
| [2026-07-10-alioth-app-qc-integrated-solution.md](2026-07-10-alioth-app-qc-integrated-solution.md) | **APP 质检整合方案（详细版）**：与平台 Agent/领域整合的目标态、Demo→正式迁移、Tool/Skill/装置后端、M-A0～A6 里程碑 |
| [2026-10-21-app-product-agent-orchestration.md](2026-10-21-app-product-agent-orchestration.md) | **产品专属编排交给 Agent**：Product Profile + Skill；默认关闭 `*_script_override`；`ALIOTH_APP_EXECUTION_MODE` 切流 |
| [2026-07-10-alioth-app-automation-detailed-design.md](2026-07-10-alioth-app-automation-detailed-design.md) | **APP 自动化方案详细设计**：模块职责、API/Host 协议、Run 状态机、MobileDevice、流水线阶段、Tool 契约、错误/锁/配置/安全/测试 |
| [2026-07-15-ldplayer-multi-device-matrix-sop.md](2026-07-15-ldplayer-multi-device-matrix-sop.md) | **雷电多机型矩阵 SOP**：同 APK 串行多 index、profiles 配置、verify 脚本、本阶段不做项 |
| [2026-07-20-docker-android-emulator-integration.md](2026-07-20-docker-android-emulator-integration.md) | **Docker 安卓模拟器整合（青云客方向）**：含跨服务器沙盒↔主站 sync 协议 |
| [2026-07-25-path-b-vmware-docker-redroid.md](2026-07-25-path-b-vmware-docker-redroid.md) | **Path B**：關 Hyper-V，VMware Ubuntu 跑 Docker Engine + ReDroid + 主站 |
| [2026-07-26-cloud-phone-ai-poc.md](2026-07-26-cloud-phone-ai-poc.md) | **雲手機 + AI 視覺 POC**：約 100 分鐘免費、其後計費；需另購 SOCKS5 |
| [2026-07-27-cloud-phone-technical-solution.md](2026-07-27-cloud-phone-technical-solution.md) | **雲手機裝置面技術方案**：為何不用模擬器、架構／費用（100 分鐘＋SOCKS5）／驗收 |
| [2026-08-17-app-cloud-phone-concurrency-scheduling.md](2026-08-17-app-cloud-phone-concurrency-scheduling.md) | **App 云手机并发调度设计（排期第 54 行）**：PG 租约边界、多机选路、池满等待、≥2 线验收；对接第 55～58 行落地 |
| [2026-09-02-bounded-exploration-optimization-solution.html](2026-09-02-bounded-exploration-optimization-solution.html) | **有限自主探索完整方案 v1.1（可视化评审版，2026-09-03 更新）**：七项问题与收益、14 条团队反馈裁决、产品/规则范围、展示层与弹窗、网络/外跳/长图、S0–S12 闭环与验收 |
| [2026-09-02-bounded-exploration-page-state-graph-design.md](2026-09-02-bounded-exploration-page-state-graph-design.md) | **有限自主探索实现级详细设计 v1.1（2026-09-03 更新）**：既有代码挂接、页状态图/动作事务、产品能力/展示层/规则频控、诊断隔离、证据角色、补采、14 条反馈逐条结论与全流程合同 |
| [七项探索优化专项设计：HTML 总览](2026-09-03-exploration-optimization/index.html) · [Markdown 索引](2026-09-03-exploration-optimization/README.md) | **七篇独立设计文档，文档集 v1.2（2026-09-04）**：每篇 HTML + Markdown，21 张组件/时序/决策图，加专项二第 5.10 节概念数据结构关系图，共 22 张。包含代码接线、算法、并发幂等、故障推演与验收；HTML 使用离线 Canvas，MD 保留 Mermaid 图源，不展开数据库表、字段类型或接口载荷 |
| [现有流程与优化方案：竖向 Canvas 对比](2026-09-03-exploration-flow-comparison.html) | **现有流程与优化方案：竖向 Canvas 对比**：H5/App 按阶段对齐，说明节点差异、闭环与代码依据 |

## 维护约定

- 新增或修改架构相关内容，优先更新 2026-07-03 整合文档（当前权威版本），而不是老的总体设计文档第 6/7 节。
- **平台执行分叉（Web vs 原生 APP）**以 [2026-07-08-alioth-native-app-qc-platform-branch.md](2026-07-08-alioth-native-app-qc-platform-branch.md) 为准；该文档定稿后的 schema/里程碑增量，再回写核心 schema 与落地路线。
- **APP 自动化工程落地与正式部署**以 [2026-07-10-alioth-app-automation-overall-solution.md](2026-07-10-alioth-app-automation-overall-solution.md) 为准；**APP 与平台整合收敛**以 [2026-07-10-alioth-app-qc-integrated-solution.md](2026-07-10-alioth-app-qc-integrated-solution.md) 为准；**模块/接口/状态机等实现级设计**以 [2026-07-10-alioth-app-automation-detailed-design.md](2026-07-10-alioth-app-automation-detailed-design.md) 为准。冲突时：领域/双轨服从 2026-07-08；Agent 编排服从 2026-07-03；整合里程碑服从整合方案；部署命令服从整体方案；实现细节服从详细设计。
- 修改任一文档涉及跨文档结论时，同步更新其余文档的"关联文档"引用与必要的勘误说明。
- mermaid 图表书写习惯：节点文本换行用 `<br/>`，避免在节点/判断框文本中使用括号和冒号。
