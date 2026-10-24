# H5 V2 Explorer Workflow 迁移设计

日期：2026-08-11

状态：已批准设计固化稿

## 1. 目标

将 H5 自动质检从 V1 Main Agent 混合链路迁移到独立 V2 Explorer Agent 链路，同时保留现有 H5 页面识别、Playwright 采集、动态 Frontier、遮挡清理、详情下钻、来源恢复和风险门禁的成熟行为。

迁移完成后，H5 任务必须形成以下稳定边界：

1. Orchestrator 创建并发布 `ExplorationWork`，不直接执行页面动作。
2. H5 V2 Explorer 只读取 Observation、Frontier、预算和覆盖摘要，只输出 `ActionIntent` 或局部终态。
3. Web Interaction Runtime 独占浏览器会话并执行动作，Explorer 不接触 Playwright、Page、Locator 或 Driver 句柄。
4. 每次状态变更动作必须具备不可变的操作前后 Capture，并记录截图、DOM、URL、可见文本和 Action Trace。
5. 页面类型判定消费已持久化 Capture，输出 `TargetObservation` 和候选动作，不输出规则 Pass/Fail。
6. Transition 验真后才能更新状态图、Frontier 和 Coverage。
7. Rule Judge 独立消费证据工作项；Explorer 局部终态不得直接映射为任务完成。
8. 最终完成必须经过统一 Completion Gate。

## 2. 范围

### 2.1 本次包含

- H5 V2 Workflow 路由及 `off / shadow / on` 模式。
- H5 专用 V2 Explorer 生产装配，禁止加载 App 专用 Workflow Skill。
- Web Surface Session、首次 Observe、动作前后 Capture 和会话关闭接线。
- H5 Capture 到页面类型判定、`TargetObservation`、候选动作的适配。
- `ActionIntent` 执行、`InteractionResult`、Transition 验真、Frontier 和 Coverage 更新。
- H5 V2 探索结果到既有 Inspection Work Item、Rule Judge 和 Completion Gate 的交接。
- H5 V1/V2 Shadow 差异记录和回退开关。
- 新 H5 Workflow 纯文本说明和专项回归规则更新。

### 2.2 本次不包含

- 自动账号密码登录、验证码识别、短信或 MFA 处理。
- 将凭据、Cookie、Token 或浏览器存储状态暴露给 Explorer 或页面判定模型。
- App V2 Workflow 行为重构。
- Web 桌面端独立业务路由启用。
- 删除 V1 H5 表、V1 Main Agent 或历史查询投影。
- 修改规则业务内容或规则判定口径。

认证页仍按现有安全语义处理：可关闭、可选择游客入口时执行低风险动作；确需凭据时输出 `requires_auth` 并进入阻断或复核。

## 3. 方案比较

### 3.1 方案 A：直接将 H5 路由指向现有 App V2 Explorer

优点：改动最少。

缺点：现有默认 Explorer 强制加载 `app-workflow` 和 `app-script-rhythm`，会把 App 脚本课纲、截图理解和设备语义带入 H5；App Bootstrap 与 H5 URL 启动方式也不一致。

结论：不采用。

### 3.2 方案 B：为 H5 复制一套完整 V2 Agent 和运行时

优点：短期隔离明显。

缺点：复制 ExplorationWork、ActionIntent、Interaction Runtime、Evidence 和 Completion Gate，形成第二套 V2 协议与事实源，后续难以维护。

结论：不采用。

### 3.3 方案 C：复用通用 V2 合同，新增 H5/Web 组合根

复用 `MafExplorerAgent`、Exploration/Probe Work、Interaction Runtime、Evidence、Rule Judge 和 Completion Gate；新增 H5 Explorer Bootstrap、Web 启动适配、H5 Observation Adapter 和 H5 Workflow Executor。

优点：领域合同统一，平台差异停留在 Adapter 和组合根；符合 V2 架构目标并保留渐进迁移能力。

缺点：需要补齐 Web Interaction Runtime 的生产接线和 Shadow 对账。

结论：采用。

## 4. 目标架构

```text
Task Submission
  -> Workflow Router
  -> H5 V2 Runtime Composition
  -> QcOrchestrator.publish(ExplorationWork)
  -> H5 V2 Explorer.decide_next
  -> ActionIntent
  -> Policy / Risk / Evidence Exit Gate
  -> Web Interaction Runtime
  -> before Capture
  -> Playwright Action
  -> after Capture
  -> Capture Persistence
  -> H5 Page Detection
  -> TargetObservation + CandidateAction[]
  -> Transition Verification
  -> State Graph / Frontier / Coverage
  -> next ExplorationWork or Explorer Terminal
  -> Inspection Work Item
  -> Rule Judge
  -> Completion Gate
  -> sealed Report
```

### 4.1 H5 V2 Explorer Bootstrap

新增 H5 专用构建入口，要求：

- 使用通用 `MafExplorerAgent`。
- 加载 H5 页面探索和质检 Workflow 上下文，不加载 `app-workflow`、`app-script-rhythm`。
- `tools=None`，不注册浏览器 Tool、数据库 Tool 或规则写回 Tool。
- 候选动作必须来自当前 `ExplorationWork.frontier`。
- 只返回完整 `ActionIntent` 或受协议约束的 Explorer 局部终态。

### 4.2 H5 V2 Runtime Composition

新增独立组合根，注入：

- PostgreSQL Surface Session Repository；
- Web `SurfaceDriver` Adapter；
- Session Command Gate；
- Capture Coordinator 和 H5 Page Capture Persistence；
- H5 Page Detection Adapter；
- Exploration Work Repository/Publisher；
- H5 V2 Explorer；
- Transition Verifier、Coverage 更新端口；
- Inspection Work Item Publisher；
- Evidence Exit Gate；
- Task Finalization/Completion Gate。

组合根只负责装配，不承载探索策略或页面识别规则。

### 4.3 路由模式

引入 H5 V2 模式：

- `off`：H5 全部走 V1 Main Agent。
- `shadow`：V1 是正式执行和完成事实源；V2 只消费同一不可变 Observation/Frontier 投影并记录决策差异，不执行第二次页面动作，不进入正式完成门禁。
- `on`：H5 走 V2 Workflow；V1 仅作为显式回退路径，不双写正式事实。

任务锁定 `workflow_version=v1` 时始终走 V1。任务锁定 `workflow_version=v2` 时再受部署级 H5 V2 模式控制，避免全局开关覆盖已锁定任务语义。

## 5. 单轮数据流

### 5.1 首轮

1. 创建 H5 Surface Session，绑定 task、run、目标 URL、viewport 和安全边界。
2. Web Interaction Runtime 执行受信任的初始 Navigate。
3. 固化初始 Capture，至少保存截图、DOM、URL、可见文本和网络/控制台摘要引用。
4. 页面判定读取 Capture，输出页面类型、页面状态、风险、Overlay、关键地标和候选动作。
5. 将判定结果适配为 `TargetObservation` 和 Frontier Snapshot。
6. 发布第一条 `ExplorationWork`。

### 5.2 动作轮

1. Explorer 从当前 Frontier 中选择候选并输出 `ActionIntent`。
2. Runtime 校验工作身份、会话租约、来源 Capture、域名/路由、风险、预算和 Evidence Exit Gate。
3. 如果来源 Capture 已满足 before 证据要求则复用；否则先补充 before Capture。
4. Interaction Runtime 串行执行动作。
5. 动作后创建 result Capture，固化截图、DOM、URL、可见文本和 Action Trace。
6. 页面类型判定消费 result Capture。
7. Transition Verifier 区分：有效到达、无变化、错误跳转、遮挡、证据待提交、证据失败。
8. 只有有效 Transition 才更新状态图和 Coverage；失败或无变化更新候选生命周期和原因。
9. 生成新 Frontier，并发布下一条 `ExplorationWork` 或 Explorer 局部终态。

### 5.3 收口轮

1. Explorer 依据 Frontier、Coverage、预算、blocked、no_progress 和 pending 状态形成局部终态。
2. Inspection Work Item 必须引用不可变 Capture/Observation Packet。
3. Rule Judge 输出规则判定或 Evidence Request。
4. Evidence Request 经 Orchestrator 转为 Probe Work，不允许 Judge 直接操作页面。
5. 所有探索、证据、规则和会话事实进入终态后，Completion Gate 才允许生成并绑定 sealed Report。

## 6. 持久化与审计

### 6.1 必须持续入库的事实

- Surface Session：会话所有者、租约、viewport、目标环境、当前 Capture、关闭原因。
- Exploration Work：objective、预算、Frontier revision、局部终态和原因。
- Action Execution：Intent、来源/结果 Capture、执行状态、错误、风险决策、幂等身份。
- Surface/Page Capture：父 Capture、URL、标题、状态指纹、生命周期和 Artifact 引用。
- Artifact：截图、DOM、可见文本、Action Trace、网络/控制台摘要的校验和与存储引用。
- Page Detection/Target Observation：检测器版本、页面类型、状态、置信度、依据和候选动作。
- Transition：前后状态、实际落点、验真结果和失败原因。
- Coverage：已覆盖、未覆盖、阻断、unknown、预算和 Frontier 摘要。
- Inspection/Rule Execution：Observation Packet、规则合同、判定和证据请求。
- Shadow Reconciliation：V1/V2 决策、页面、阻断、覆盖、耗时和成本差异。

### 6.2 身份规则

- URL 只是元数据，不是页面或状态唯一身份。
- 页面事实以 `capture_id` 为锚点。
- 状态指纹至少组合页面类型、业务身份、关键地标、Overlay、结构摘要和环境身份。
- Action Execution 必须稳定关联 `source_capture_id` 与 `result_capture_id`。
- 同一逻辑候选跨 Capture 使用稳定候选身份去重。

### 6.3 事务边界

- Action Intent 领取、会话命令许可和 Action Execution 启动必须具备幂等保护。
- result Capture 和 Action Execution 结果必须保持可恢复关联。
- Exploration/Inspection Work 与 Outbox 在同一 PostgreSQL 事务中推进。
- Redis 只承担唤醒，不作为业务事实源。

## 7. 错误处理与安全

- 浏览器会话丢失：终结当前命令许可，创建 Restoration Work 或进入复核，不复用失效句柄。
- 页面不稳定：有界重新 Observe，不使用无限 sleep/refresh。
- stale 候选：按语义身份重绑定；不在错误页面执行旧 selector。
- 动作成功但页面无变化：记录 `no_change`，不更新 Coverage。
- 动作成功但证据失败：记录 `evidence_pending/evidence_failed`，不伪装普通成功。
- 登录墙：低风险关闭或游客入口可执行；需要凭据时输出 `requires_auth`。
- R3 动作：支付、下单、删除、发布、授权默认禁止。
- 页面文本、DOM 和下载内容一律视为非可信数据，不能提升权限或生成任意脚本。

## 8. 兼容与迁移

- V1 H5 行为在 `off` 和显式 V1 任务中保持不变。
- V2 主模型可向旧 `qc_page_capture`、页面节点和探索动作查询投影双写，但只有一个正式事实源。
- Shadow 数据不得进入正式 Rule Execution、Completion Gate 或报告。
- 切流后发现越权动作、证据链断裂、会话并发写、不可解释 Transition 或完成误判时，部署级模式立即回退 `off`。
- 本阶段不删除旧表；旧表移除必须另行设计并完成历史查询迁移。

## 9. 测试与验收

### 9.1 契约测试

- H5 Explorer 不注册 Driver、数据库或规则写回 Tool。
- H5 Bootstrap 不加载 App Workflow Skill。
- ActionIntent、InteractionResult、TargetObservation、ExplorationWork 版本和幂等身份稳定。

### 9.2 采集与判定测试

- 首轮 Navigate 后固化初始 Capture。
- 每个状态变更动作关联操作前后 Capture。
- 截图、DOM、URL、可见文本和 Action Trace 引用完整。
- 每个 result Capture 重新进行页面类型判定。
- URL 相同但 Tab/弹窗/业务地标不同的状态不会错误去重。

### 9.3 Explorer 测试

- 详情下钻、页内入口、全局导航、来源恢复和覆盖缺口优先级。
- Frontier 跨 Capture 去重和 stale 重绑定。
- 深度、动作数、时间、no_progress 和 blocked 终态。
- Frontier 为空但后台工作未完成时不得将 Task 标记 Done。

### 9.4 可靠性与安全测试

- 同一 Session 并发动作串行化或拒绝。
- 重复消息不重复执行页面动作。
- Redis 丢失、Worker 重启、会话丢失和证据提交失败具有稳定恢复或复核终态。
- 登录、支付、删除、发布、授权和 Prompt Injection 边界。

### 9.5 Shadow 与切流门禁

- 相同任务、预算、环境和不可变输入下，V1/V2 页面集合、候选选择、阻断原因和终态差异可解释。
- 无越权动作、无证据链断裂、无 Session 并发写、无不可解释 Transition。
- V2 不降低既有 H5 Profile、遮挡清理、底部导航、详情下钻、来源恢复和风险门禁回归结果。
- 切换到 `on` 前完成回退演练。

## 10. 交付物

1. H5 V2 Workflow 路由和部署模式配置。
2. H5 V2 Explorer Bootstrap。
3. H5 V2 Runtime Composition/Executor。
4. H5 Capture/Detection/Observation/Frontier 适配链。
5. 前后 Capture、Transition、Coverage 和 Shadow 持久化接线。
6. H5 V2 到 Inspection/Rule Judge/Completion Gate 的交接。
7. 单元、契约、集成和回归测试。
8. `docs/专项能力与高频回归规则.md` 更新。
9. 新 H5 Workflow 纯文本文件。

## 11. 成功标准

- `workflow_version=v2` 且部署模式为 `on` 的 H5 任务不再构建或运行 V1 Main Agent。
- H5 V2 Explorer 使用独立 Run，只输出 ActionIntent 或局部终态。
- 每次状态变更动作均可从 Action Execution 追溯到操作前后 Capture、Artifact、页面判定和 Transition。
- 页面类型判定在首轮及每次动作后执行，结果持续入库。
- Coverage 只由验真成功的 Transition 更新。
- Rule Judge、Evidence Request 和 Completion Gate 保持独立职责。
- `off` 可立即回退 V1；`shadow` 不污染正式事实；`on` 不混用两个事实源。
- 新增和既有相关测试全部通过，代码审核无高优先级问题。
