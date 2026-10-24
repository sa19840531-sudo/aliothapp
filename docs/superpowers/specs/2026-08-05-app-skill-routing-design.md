# App Skill 路由与生产入口改造设计

## 背景

当前平台已经具备 `qc-workflow`、`app-workflow` 两份 Skill，`AppWorkflowRouter`、`AppV2Runtime` 和 App Bootstrap/截图探索领域服务也已落库，但三处入口仍未闭环：

1. 通用 Main Agent 的系统指令始终要求优先读取 `qc-workflow`，没有按任务目标区分 H5 与 App。
2. `ExplorerAgent` 的默认 MAF Agent 没有挂载 Skill Provider，因此 App Explorer 实际收不到 `app-workflow` 约束。
3. 生产 Worker 直接构造通用 Main Agent 并执行 `run_main_agent`，没有先经过 `AppWorkflowRouter`，也没有调用 `AppV2Runtime`。

如果不先修正入口，App 任务可能进入 H5 Agent，或者 Explorer 在没有 App 约束的情况下生成不符合协议的动作。官方 App Adapter 尚未交付，因此本阶段必须 fail-closed，不能用 Fake 或 H5 Driver 隐式兜底。

## 目标

- H5/Web 与 App 在 Agent 构建前完成确定性 Workflow 路由。
- H5/Web Main Agent 强制加载 `qc-workflow`；App 入口强制加载 `app-workflow`，并拒绝把 App 任务交给通用 H5 Agent。
- App Explorer 显式挂载 `app-workflow` Skill Provider。
- 生产 Worker 具备 `AppWorkflowRouter → AppV2Runtime` 的正式调用边界；Adapter 未配置时将 Run 显式标记失败。
- 保持既有 Redis Run、PostgreSQL 状态和取消/恢复链路，不新增第二套任务终态。
- 通过单元、架构和生产组合测试固定上述边界。

## 非目标

- 本阶段不实现星语 Official App Adapter，也不改变 APK 安装、打开、截图 API 的具体传输协议。
- 不允许 Fake App Runtime 在 production 环境启用。
- 不把 CSS/XPath/Appium locator、DOM 或 Driver 实例暴露给 Explorer 或 Agent 上下文。
- 不新增独立 Redis 队列或第二套 Worker 进程；先复用现有 Run 队列与状态持久化。

## 方案选择

### 方案 A：统一 Worker 路由 + App 专用处理器（采用）

Worker 读取 Run 后，先从锁定的 `task_config["_task_target"]` 还原 `TaskTarget`，调用 `AppWorkflowRouter`。H5 走现有 Main Agent；App 走 `AppWorkflowHandler`，由该处理器通过显式 `AppRuntimeProvider` 构造并调用 `AppV2Runtime`。Provider 未提供 Official Adapter 时抛出配置错误，Run 以现有 `failed/error` 终态收敛。

优点是改动集中、不会引入新队列，且可以用依赖注入替换 Fake/Official；缺点是需要在现有 Worker 中增加 App handler 的组合入口。

### 方案 B：独立 App Worker 队列

新增 App 队列、进程和运维配置，App Worker 独立构造 Runtime。边界最清晰，但会增加队列恢复、监控、部署和双 Worker 测试范围，本阶段不采用。

### 方案 C：只修 Skill，不接生产路由

只能解决前两个问题，第三个问题仍会让 App 任务进入通用 Agent，不满足上线阻断项，不采用。

## 设计

### 1. Workflow 与 Skill 选择

新增一个纯函数/策略对象，根据 `WorkflowRoute` 返回 Skill 子目录：

- `h5`：`("qc-workflow",)`；
- `app`：`("app-workflow",)`；
- 未知路由：抛出路由错误。

通用 Main Agent 不再把 App 任务当作 H5 任务处理。通用入口默认仍服务 H5，并在系统指令中明确“只接受 H5/Web Run”；App 入口由 App handler 单独构造，不复用该 Agent。

Skill Provider 的上下文必须在 Agent 创建时注入，而不是只把 Skill 文件存在磁盘。App Explorer 的默认构造函数传入 `build_skills_context_provider(sub_dirs=("app-workflow",))`；`MafExplorerAgent` 接受并转交 MAF `Agent` 的 `context_providers`。

### 2. App Worker 组合边界

新增 `AppRuntimeProvider` Protocol，职责是按当前 Run 返回已组合的 `AppV2Runtime`。Provider 不负责解析任务、不负责改变 Run 终态。

新增 `AppWorkflowHandler`：

- 校验路由必须为 `WorkflowRoute.APP`；
- 检查取消请求与 Run lease；
- 调用 `runtime.bootstrap(...)` 完成安装 → 打开 → 首次截图并等待 `APP_READY`；
- 按 Explorer 队列/持久化状态调用 `runtime.run_one_turn(...)`；
- 由现有 Worker 结果适配器写回 Run，不直接调用 Driver；
- Runtime/Adapter 未配置时抛出可识别的 `AppRuntimeConfigurationError`，由 Worker 使用既有失败收敛函数落库。

Worker 的顺序固定为：

```text
dequeue Run
  -> restore locked TaskTarget
  -> AppWorkflowRouter.route
  -> H5: build Main Agent(qc-workflow) -> run_main_agent
  -> APP: AppWorkflowHandler -> AppV2Runtime
```

任何 `target_platform=app` 的 Run 都不能调用 `build_default_main_agent` 或 `run_main_agent`。路由失败也不能静默改写为 H5。

### 3. 失败与恢复

- 未配置 Official Adapter：`FAILED` + `ErrorCategory.INFRASTRUCTURE`（或现有最贴近的配置错误分类），保留错误码与审计信息。
- App Bootstrap/Explorer 的可重试错误继续由 `AppV2Runtime` 的领域状态和现有 Run 状态承载，不新增 App 专属终态。
- 取消、lease 过期、旧 Worker 接管使用既有状态/租约校验；App handler 在每次外部调用前检查取消和 lease，避免旧 Worker 继续操作。

### 4. 测试门禁

必须新增或调整以下测试：

- Skill 路由：H5 只得到 `qc-workflow`，App 只得到 `app-workflow`。
- Explorer 组合：默认 App Explorer 的 framework Agent 挂载了 App Skill Provider。
- Worker 路由：App Run 不构造 Main Agent、不调用 `run_main_agent`，而调用注入的 App handler；H5 行为保持不变。
- Fail-closed：production + official 未注册时 Run 显式失败；不得回退 Fake/H5。
- 架构边界：Explorer 不导入 Driver，Worker 只通过 `AppV2Runtime`/handler 访问 App 领域。
- 真实 PostgreSQL 双 Worker 与星语接口集成测试留作 Official Adapter 接入阶段；本阶段先提供可替换 Provider seam 和 Fake/local 覆盖。

## 风险与回滚

- 风险：现有测试直接调用 `_process_one` 并假设所有 Run 都是 H5。通过测试 fixture 默认写入 H5 target，新增 App case 单独覆盖。
- 风险：MAF 版本对 `context_providers` 的构造参数不一致。先用现有依赖版本的构造签名测试锁定，必要时集中在 Explorer builder 适配。
- 回滚：删除 App handler/provider 组合并恢复 Worker 路由前的 Main Agent 路径即可；保留 `AppWorkflowRouter` 与 Skill 文件，不回滚领域模型或数据库迁移。

## 验收标准

- 代码搜索可证明生产 Worker 存在 `AppWorkflowRouter` 路由，且 App 分支不调用通用 Main Agent。
- App Explorer 的 MAF Agent 实例含 `app-workflow` 上下文 Provider。
- H5 现有测试全通过，新增 App 路由/失败闭环测试通过。
- Ruff、后端测试及架构边界测试通过。

