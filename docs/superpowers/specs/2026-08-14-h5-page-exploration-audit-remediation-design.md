# H5 页面探索完善度审计整改设计

## 1. 目标

依据 `2026_08_14_h5_app_page_exploration_completeness_audit_report.html`，只收口 H5 页面探索链路中的四项仓内缺口：共享首跳父 Capture 合同、H5 入口 URL 凭据校验、`exploration_only` 对 Rule Engine 配置的错误依赖，以及切换脚本与任务快照路由的口径漂移。

本次不新增页面能力、不修改 App 产品合同、不搭建或伪造真实 PostgreSQL、Redis、Playwright、MinIO、LLM、认证联合验收环境。真实联合验收继续作为生产放量前的外部门禁，并在复审结论中明确保留。

## 2. 审计证据与根因

### 2.1 共享首跳父 Capture

`evidence_exit_gate.is_trusted_initial_surface_action()` 的注释和现有测试约定：

- H5 V2 组合根生成的 `h5-entry:*` 必须显式携带 `initial_surface_action=true`；
- 通用 Main Agent 生成的 `agent-entry:*` 与历史 `legacy-agent-entry` 本身具有入口语义，不指向已落库 Capture。

当前实现却要求所有入口哨兵都携带显式标记，导致未带标记的 `agent-entry:*` 返回 `False`。`CaptureCoordinator` 随后会把哨兵字符串作为 before Capture 的真实 `parent_capture_id`。同文件在提前 `return` 后还残留了不可达的旧判断分支，表明合并时保留了说明和测试，却截断了实际语义。

### 2.2 H5 入口 URL 凭据

`H5TargetRequest` 当前只校验 scheme 和 netloc。`https://user:password@example.test/path` 会通过 Schema，随后进入应用命令、任务持久化、Run 配置和队列消息；直到浏览器语义目标解析层才被拒绝。敏感信息因此可能在执行前已经进入审计链和消息链。

### 2.3 仅探索任务的 Rule Engine 依赖

H5 Worker 已在正式收口阶段按 `execution_mode` 分流：`exploration_only` 不执行 Judge、规则结果或 Completion Gate。问题发生在更早的 Worker 启动阶段：`main()` 无条件扫描数据库中所有已启用 Rule Engine 配置，所以一个只需要 Explorer、Capture 和 Evidence Inventory 的任务也会被无关规则配置阻断。

### 2.4 切换脚本路由口径

权威运行时 `AppWorkflowRouter` 按持久化在任务上的 `workflow_version` 选择 H5 V1、shadow 或 V2；V2 API 入口会为新任务锁定 `workflow_version=v2`。当前切换脚本却把全局 `ALIOTH_WORKFLOW_VERSION=v2` 对 `shadow` 和 `on` 都作为硬门槛，并宣称全局为 v1 时 H5 V2 模式完全不生效。该结论对锁定 v2 的 V2 API 任务不成立。

## 3. 方案选择

采用“定点合同收口”方案：在现有边界内修复四项问题并补齐回归，不抽取新的通用 URL 值对象，不增加新的 Worker 部署形态，也不修改 App、Web 或前端产品合同。

未采用的方案：

- 领域级 URL 安全模型与能力门禁重构：长期抽象更统一，但会扩大 Web、App 和共享基础设施影响面；
- 通过新增 exploration-only Worker 环境开关规避：保留共享合同根因并引入新的部署配置债务。

## 4. 详细设计

### 4.1 首跳哨兵判定

`is_trusted_initial_surface_action()` 继续只接受 `SurfaceKind.WEB + NAVIGATE`，随后按哨兵类型分流：

- `h5-entry:*`：仅在 `initial_surface_action is True` 时可信；
- `agent-entry:*`：哨兵身份即表示通用 Main Agent 的首跳；
- `legacy-agent-entry`：保留历史首跳兼容；
- 真实 Capture、未知前缀、非 Web 或非 Navigate：均不可信。

`CaptureCoordinator` 的新执行、计划恢复和已执行回放都继续调用同一 helper。可信首跳的 before Capture 必须写入 `parent_capture_id=None`，after Capture 仍以本轮 before Capture 为父级。删除 `trusted_h5_browser_recovery_identity()` 返回语句之后的不可达首跳代码，避免出现两个互相漂移的实现。

### 4.2 H5 URL 前移校验

只调整 `H5TargetRequest`：在现有 HTTP(S) 校验基础上拒绝 `urlsplit()` 解析出的 username 或 password，并读取 `parsed.port` 以拒绝非法端口。错误由 Pydantic/FastAPI 在请求模型阶段映射为 422。

安全边界要求：

- 校验失败时不得调用 `V2TaskEntryPort.submit()`；
- 因此不得创建应用命令、写任务表、写 Run 状态或发送 Redis 消息；
- 不修改 `WebTargetRequest` 和 App `download_url` 的既有合同，避免扩展本次范围。

### 4.3 Rule Engine 按任务模式延迟门禁

把 Rule Engine 配置校验从 Worker 进程启动期移动到按任务路由之后、任何浏览器或设备副作用之前：

- `full_inspection`：执行现有 `validate_enabled_rule_engine_configuration()`，失败时沿 Worker 的结构化失败路径 fail-closed；
- `exploration_only`：不调用 Rule Engine 配置仓储，不依赖规则配置即可进入 H5 Explorer；
- 成功校验结果在当前 Worker 进程内缓存，避免每个完整质检任务重复全表扫描；失败不缓存，允许配置修复后由后续任务重新验证。

门禁对象在生产 `main()` 中装配并传入 `_process_one()`；私有处理函数的测试可注入记录型门禁。App 与传统完整质检仍走 `full_inspection`，不会因为本次 H5 修复失去原有 fail-closed 保护。

### 4.4 切换脚本按目标档位解释路由

切换脚本对全局 workflow version 的解释改为：

- 目标为 `shadow`：当前旧入口必须能产生锁定 v2 的任务，因而全局默认 v1 仍是阻塞项；
- 目标为 `on`：全局默认 v1 只表示旧入口继续生成 V1 任务，锁定 v2 的 V2 API 任务仍可进入 H5 V2；该项降为非阻塞警告；
- 无论目标档位如何，都保留任务快照为权威路由事实的说明，禁止再宣称全局版本覆盖所有 H5 任务。

脚本仍保持只读，不连接数据库或外部依赖；`ready=true` 仍只表示可以开始切换动作，不代表真实联合验收通过。

## 5. 数据流与失败语义

### 5.1 提交链

H5 JSON 请求先经过 `H5TargetRequest`。安全 URL 才能转换成 `V2TaskSubmissionCommand` 并进入 Entry Service；带凭据或非法端口的 URL 在任何持久化和入队之前返回 422。

### 5.2 执行链

Worker 读取已锁定的任务目标、`workflow_version` 和 `execution_mode`，先完成路由，再执行模式门禁。仅探索任务直接进入 H5 Runtime；完整质检任务先确认 Rule Engine 配置可执行。首跳动作由统一哨兵 helper 决定 before Capture 是否有真实父级。

### 5.3 失败链

- 非法 H5 URL：API 422，提交端口零调用；
- 无效首跳身份：不能绕过离场门禁，也不能清除真实父 Capture；
- 完整质检规则配置错误：Run 在创建运行时副作用之前进入结构化失败；
- 仅探索任务：不因规则配置错误失败；
- 切换脚本：阻塞项返回退出码 2，警告项不改变 readiness。

## 6. 测试与复审

严格执行 RED-GREEN-REFACTOR，每个行为先观察测试按预期失败：

1. 参数化 `h5-entry:*`、`agent-entry:*`、`legacy-agent-entry` 从 ActionIntent 到 before/action/after Capture 的完整父级合同，并覆盖伪造标记、非 Navigate、非 Web 和真实 Capture。
2. 对 `/api/v2/tasks` 与 `/api/v2/exploration-tasks` 增加带 username、password 和非法端口的负例，断言 422 且提交服务零调用；保留普通 H5 URL 正例。
3. 增加记录型 Rule Engine 门禁，断言 `exploration_only` 零调用、`full_inspection` 调用一次、成功缓存、失败重试以及失败发生在 Runtime 分派之前。
4. 增加切换脚本矩阵：`shadow+global v1` 阻塞，`on+global v1` 警告但不阻塞，v2 任务快照路由说明与 CLI 退出码一致。
5. 运行相关 H5/共享后端回归、关键 Ruff、Python 编译、切换脚本 CLI、Alembic 单头检查；若改动触及工作站文件才运行工作站测试和构建。本设计不计划修改工作站文件。

复审按报告四项 H5 缺口逐条给出代码证据、测试证据和剩余风险，不把 Fake、Mock 或只读配置检查描述为真实联合验收。

## 7. 兼容性与交付影响

- 数据库迁移：无。
- 前端菜单或路由：无。
- App 产品合同：无变化。
- Web 公开请求合同：无变化。
- H5 V1：保留现有任务快照路由，不强制随全局开关迁移。
- 部署：不新增环境变量；Worker 内部门禁时点变化，完整质检仍保持 fail-closed。
- 剩余风险：真实 PostgreSQL、Redis、Playwright、MinIO、LLM、认证、浏览器崩溃和 Worker 重启联合验收未在本次环境执行。

## 8. 完成标准

- 报告列出的四项仓内 H5 缺口均有先失败后通过的自动化回归；
- 相关回归、静态检查、编译和脚本矩阵通过；
- 代码差异不包含 App 功能扩展、前端页面改造或数据库迁移；
- 高频回归规则记录本次根因和今后约束；
- 复审报告明确区分仓内闭环与真实环境发布门禁；
- 修复分支提交后合并到 `dev_1.5`，在合并结果上重新验证并推送远程。
