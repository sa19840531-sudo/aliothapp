# 附件审查建议全量整改设计

## 目标与边界

本设计针对 `2026-08-06-qingyunke-sa19840531-commit-review.md` 中的两个阻断项、七个 P1 问题和工程质量问题，目标是让当前 `dev_1.5` 的代码在不调用远程 API 的前提下具备可本地验证的安全、数据一致性、可靠性和完成语义。

本次执行范围包括：

- 默认关闭并隔离 `mobile-demo`，收紧上传、设备、停止和 Artifact 读取能力。
- 统一 Action Execution、Evidence Exit Gate 和 App Capture 使用 `qc_capture` 公共事实表，保留 Web/App 专属子表边界。
- 修复 `CaptureRef` 协议消费者、动作轮次幂等、Capture 重试幂等、非法 App Build 引用和矩阵结果伪造。
- 让 App Explorer 终态只代表探索终态，Task/Run 必须经过 Inspection、Rule Judge、Review 和 Completion Gate 收口。
- 补齐 Session 重启后的 reattach/reacquire 入口、旧租约拒写和故障恢复测试。
- 让 Android Adapter 的截图、动作、坐标校验和 Artifact 引用经过统一 Runtime 边界，并收紧 Ruff 与可重复测试依赖。

不调用真实 Android、MoreLogin、官方 App API、云对象存储或其他远程服务；这些验收只保留本地适配器合同和显式未执行记录，不把模拟测试宣称为外部环境验收。

## 总体架构

```text
API 请求
  ├─ mobile-demo：显式开发开关 + loopback/隔离网络守卫
  └─ 正式任务：现有登录、租约和任务归属边界

ActionIntent
  → CaptureCoordinator
  → qc_capture 公共事实
       ├─ Web → qc_page_capture
       └─ App/Android → qc_app_capture
  → Interaction Runtime / SurfaceDriver
  → after Capture / Artifact
  → Inspection Work Item
  → Observation Packet / Semantic Rule Judge
  → Completion Gate(finalization_epoch + gate_snapshot_id)
  → Task/Run 完成
```

公共 Capture 查询只按 `surface_kind` 和 `capture_kind` 约束；Web 专属字段才连接 `qc_page_capture`，App 专属字段才连接 `qc_app_capture`。所有跨层响应使用 `capture_id`，旧 `page_capture_id` 仅限历史 Web 协议反序列化。

## 关键设计决策

### 1. mobile-demo 访问控制

`WorkflowSettings` 新增 Demo 访问配置，默认 `mobile_demo_enabled=False`。`create_app()` 不在默认 `api_router` 中挂载 Demo 路由，只有显式开启且通过 loopback/隔离网络检查时才挂载。已开启的开发路由仍经过统一依赖守卫；生产环境不允许以匿名方式开启。

上传先流式写入隔离 staging 文件，再做大小、ZIP 结构、Manifest、签名文件和包名校验；失败时删除 staging。响应只包含不透明的 Run/Artifact 标识和短期授权引用，不暴露本机或对象存储路径。

### 2. Capture 与幂等

动作截图命令 ID 固定由 `action_execution_id + phase` 派生；同一次重试复用同一命令 ID，不同动作轮次必然生成新 ID。App Capture 按 `action_execution_id + capture_role` 建立业务幂等身份；相同身份相同内容返回已提交事实，相同身份不同内容返回冲突。

Action Execution 和 Evidence Exit Gate 的 PostgreSQL SQL 从 `qc_capture` 开始查询，并通过 `surface_kind`、`capture_role`、Session 归属和状态过滤。迁移补充必要的唯一约束/索引和 App 全链路集成入口；没有显式本地数据库时测试只能报告跳过，不改变其验收语义。

### 3. App 完成语义与恢复

Explorer 的 terminal 只更新探索 Work 的状态。App Worker 在 Explorer 收口后发布或排空 Inspection Work Item，消费 Observation Packet 并完成规则结果，再在同一任务完成事务中评估 Completion Gate；只有所有必需事实终结并绑定 `finalization_epoch`、`gate_snapshot_id`、报告和人审策略后，才写入 Task/Run completed。

Session 恢复不依赖进程内集合：Workflow Resume 先调用 Runtime 的 list/validate/reattach 合同，在数据库租约保护下重新取得可执行会话；无法恢复时进入可重试的恢复状态，而不是不可重试终止。

### 4. Android/Demo 真实性

矩阵模式逐 profile 执行、逐 profile 保存状态和证据；不支持的路由显式返回 `unsupported`，禁止复制第一台设备结果。Agent 任务只有明确 `done` 且所有必需证据提交成功才是 succeeded；异常、超时、步数上限和 after Capture 失败进入 failed/incomplete/review。

Android Capture Writer 通过统一 Coordinator 获取命令序列、Epoch 和唯一截图命令；ADB 临时远端文件按会话和动作命名，并检查命令返回码；坐标必须通过屏幕边界、禁区和坐标空间校验。Artifact 本体和索引以可补偿方式落库，外部读接口不返回 `storage_path`。

### 5. 工程质量

将 Pillow 作为测试依赖显式声明，Ruff 作为本地/CI 强制门禁；新增协议消费者静态搜索测试和关键回归测试。历史提交不重写，当前整改提交统一使用 `[AI编程]` 标题并在正文说明模块、数据库迁移、前端菜单/路由同步和未覆盖测试风险。

## 验收策略

每个任务先添加能够复现问题的失败测试，再实现最小修复并运行针对性测试。最终本地验收包括：后端全量 pytest、公共 Capture/安全/完成门禁/恢复定向测试、Ruff、Python 编译、Workstation 前端测试与构建、`git diff --check`，以及本地代码审核。真实 PostgreSQL、Android、MoreLogin、Official API 和对象存储不在本次执行范围内，最终报告单独列出。
