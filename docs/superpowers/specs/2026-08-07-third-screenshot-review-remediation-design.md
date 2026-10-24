# 2026-08-07 第三轮截图审查整改设计

## 结论

当前 `827eef9` 仍不适合作为可部署版本。本轮截图反馈中的 5 个 P1 均落在收口确定性、安全点击和生产配置边界上，方向成立；次要质量问题中，代理地址、沙盒 token 与 Redis 断连恢复也需要在同批规则中收紧。最优方案不是继续补提示词，而是把每个放行点收回到独立事实源：PostgreSQL Finalization、真实 Android UI 层级和显式运行配置。

## 领域与边界

- Completion Gate 属于任务终态域。`qc_task.finalization_state`、`qc_agent_run.output_context.final_report` 与 `qc_report_gate_binding` 是事实源，Redis 只作为短期视图。
- Android 探索属于 Interaction Runtime 域。模型 JSON 只能解释意图，不能直接证明安全栏目耗尽、目标控件存在或关闭控件可信。
- Mobile Demo 生产代理属于 API 边界。生产环境必须由部署配置显式声明可信来源，示例文件必须可通过 `docker compose config`。
- 沙盒启动属于 Host Runner 边界。启动脚本和 Compose 必须在容器启动前拒绝占位 token。

## 方案

### 1. Completion Gate 失败保持可恢复

最终报告生成或原子收口失败时，不再把 Redis/长期 Run 快照写回成普通 `open`。如果 PostgreSQL 任务已经进入 `exploration_draining`、`session_draining` 或 `sealed`，恢复器必须能从数据库扫描到它；如果流程仍处于 `open`，也必须通过明确的可恢复扫描处理。失败路径只保留 preview 快照，禁止发布 final。

### 2. sealed 历史任务禁止 preview 直升 final

恢复器遇到 `sealed` 但缺少报告绑定时，只能绑定已经确认的正式报告。若 Redis 或 PostgreSQL 中只有 `report_stage=preview` 或 `is_final=false`，必须重新生成正式报告后再绑定；生成失败时保持可恢复，不得把旧 preview 改字段冒充 final。

### 3. 高风险 Tap 进入独立风险门禁

所有 Android 定向 Tap 继续标记为 `HIGH`。Agent 层新增语义阻断词覆盖 `purchase`、`subscribe`、`buy`、`立即购买` 等购买/订阅语义；普通坐标 Tap 只要带有高风险语义就阻断。关闭/跳过/确认类点击必须同时命中真实 UI 层级中的可见、启用、可点击关闭控件，模型提供的 `dismiss_evidence` 仅作说明。

### 4. UI 完成判定必须证明目标 App 内有效内容

运行时观察不再把“XML 可解析但只有 WebView 根节点”当成安全栏目耗尽。无底栏完成只在以下条件同时满足时成立：观察属于目标 package，仍在目标 App activity 内，并且存在可见的有效内容节点或明确无可探索安全栏目。Launcher、空 WebView 或崩溃后的外部界面不得触发 `done ok`。

### 5. 生产配置示例与必填项同步

`infra/env/compose.production.example` 必须包含 `ALIOTH_MOBILE_DEMO_TRUSTED_PROXY_ADDRESSES`，并保留 `ALIOTH_MOBILE_DEMO_TRUSTED_PROXY_NETWORKS` 的空默认；生产 Compose 的 `config` 以示例文件作为验收输入应能展开。

### 6. 次要质量项收口

沙盒 token 检查必须剥离引号和空白后识别 `change-me-sandbox`；代理网络配置不能依赖动态容器 IP 作为长期白名单；恢复器在 Redis 断连时应先尝试 PostgreSQL 可恢复扫描，不能因为 Redis 短断完全跳过 PG 事实读取。

## 数据流

1. Worker 生成 preview，仅作为诊断快照。
2. Worker 生成 final，调用 `finalize_task_with_report` 在事务内写 Agent Run、绑定报告、封存 Task。
3. 任一失败路径保留当前 Finalization 状态，恢复器从 PostgreSQL 扫描可恢复任务。
4. sealed 未绑定报告时，恢复器检查现有报告是否为 final；不是 final 则重新生成。
5. Android Agent 每次观察写入运行时事实；完成门禁只读取运行时事实，不读取模型自证。
6. Tap 经 Agent 语义阻断、Runtime 风险等级和真实 UI 控件命中三层校验。

## 错误处理

- 报告生成失败：保留 preview 或空报告，记录日志，等待恢复器重试。
- 原子收口失败：不发布 final，不把 Task 改回 open，不写 sealed 后的 Agent Run。
- sealed 缺 final 报告：拒绝绑定 preview，重生成失败时留给下一轮扫描。
- UI 层级不可用、包名不匹配、只有空根节点：完成门禁返回失败原因。
- Compose 示例缺变量：本地配置验证失败，阻断提交。

## 测试策略

- Worker 单测覆盖 open/exploration/session/sealed 恢复、最终报告失败、preview 不直升 final、Redis 列表失败仍扫描 PG。
- Repository 源码契约测试覆盖 `open`、`exploration_draining`、`session_draining`、`sealed 未绑定` 均在恢复扫描范围。
- Android 单测覆盖购买/订阅语义阻断、真实关闭控件放行、WebView-only 不算完成、包名不匹配不算完成。
- Compose 验证用生产示例执行 `docker compose config --quiet`。
- Ruff 与相关后端专项测试作为每批提交前门禁；最终执行后端全量和前端已有门禁。

## 不做

- 不引入新的数据库迁移；本轮只修复现有 Finalization 状态使用方式。
- 不扩大 Mobile Demo 生产可用范围；生产仍应默认禁用或只在受控链路启用。
- 不用模型提示词替代代码门禁。
