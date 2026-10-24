# H5 V2 API 业务闭环接入设计

## 1. 目标

将已经存在的 H5 V2 Explorer Runtime 接入 V2 控制面，使以下业务路径真实可用：

1. `POST /api/v2/tasks` 可提交 H5 完整质检任务；
2. `POST /api/v2/exploration-tasks` 可提交 H5 仅页面探索任务；
3. 仅探索任务完成截图、DOM、URL、动作轨迹、页面类型和探索谱系持久化后结束，不执行 Rule Judge、不生成正式报告；
4. 完整质检任务在探索证据通过交接门禁后发布正式质检工作、执行 Rule Judge、保存正式规则结果并通过 Completion Gate 生成封存报告；
5. 已留存证据可由 `POST /api/v2/tasks/{task_id}/rule-evaluation-runs` 重复执行独立规则测试，且不得覆盖正式结果。

本次不扩展自动登录能力。遇到必须输入账号密码、验证码、MFA 或扫码授权的页面时，任务保持 `requires_auth`/人工复核语义，不能伪装为探索完成或登录成功。

## 2. 已确认现状

- H5 V2 Runtime、Explorer Agent、受控浏览器交互、before/after Capture、页面判定、PostgreSQL Explorer Work 和 Worker 专用分流均已存在。
- `/api/v2/tasks` 当前只允许 App，H5 在应用层固定返回 501。
- `/api/v2/exploration-tasks` 当前是固定返回 501 的占位入口。
- 独立规则测试入口已接入 `qc_rule_evaluation_run`，但只能消费已经存在的页面证据。
- 工作站创建页默认选择 H5 并调用 `/api/v2/tasks`，因此当前用户路径必然失败。
- H5 V2 正式运行需要 `ALIOTH_H5_V2_MODE=on`；默认值仍为 `off`。

## 3. 方案选择

### 3.1 推荐方案：共享 Explorer Runtime，使用类型化执行模式分流

完整质检和仅探索共享任务提交、队列、Worker 路由与 H5 Explorer Runtime。新增稳定枚举 `TaskExecutionMode`：

- `full_inspection`：探索完成后进入证据交接、正式规则判定和报告收口；
- `exploration_only`：探索完成并确认核心证据已落库后直接结束。

执行模式锁定在任务配置和 Agent Run 快照中，由统一解析函数读取，禁止在 API、Worker 和前端散落字符串判断。

优点：只有一套深度/广度探索语义；仅探索与完整质检的证据格式完全一致；后续独立规则测试可直接复用证据。缺点：Worker 收口必须显式区分两种终态路径。

### 3.2 不采用：复制一套仅探索 Runtime

该方案短期改动直观，但会复制 Session、Capture、页面判定、Explorer Work 和失败恢复逻辑，后续极易产生两套行为和两套审计口径。

### 3.3 不采用：完整质检流水线中途强制退出

该方案只增加一个临时条件判断，但可能已经发布质检工作项或初始化正式规则执行，再强行跳过 Judge 会留下非终态库存，污染 Completion Gate 和报告状态。

## 4. 领域与接口边界

### 4.1 执行模式

新增领域枚举 `TaskExecutionMode`，并提供严格解析：

- 未声明模式的既有任务兼容为 `full_inspection`；
- 未知模式必须失败关闭，不能静默回退；
- 新 V2 完整任务显式锁定 `full_inspection`；
- 新仅探索任务显式锁定 `exploration_only`。

本次复用现有 `qc_task.task_config` JSONB 和 `qc_agent_run.input_context` 审计快照，不新增数据库列。原因是执行模式属于任务执行策略快照，现有表已经以不可变配置承载同类字段；通过领域枚举和唯一读写函数保证结构稳定。

实施审查同时发现既有 Web 父表触发器会把 `qc_page_capture` 的真实角色投影成公共 `qc_capture.capture_role=observation`。因此新增迁移 `20260812_0047`：扩展公共角色约束到冻结供给词汇、回填历史 Web Capture 角色，并让插入/更新触发器持续同步真实角色。该迁移修正既有数据一致性，不新增列。

### 4.2 V2 完整任务入口

`TaskSubmissionV2EntryService` 按平台构造稳定 `TaskTarget`：

- H5：要求非空 HTTP(S) URL，构造 `TaskTarget(platform=H5)`，锁定 `workflow_version=v2` 和 `execution_mode=full_inspection`；
- App：保持现有制品引用逻辑，锁定相同执行模式；
- Web：继续返回结构化 501，不得静默并入 H5。

H5 请求在提交时检查部署模式。只有 `ALIOTH_H5_V2_MODE=on` 才接受新 H5 V2 任务；`off`/`shadow` 返回结构化冲突错误，避免提交成功后实际落入 H5 V1 或只读 Shadow。

### 4.3 仅探索入口

用真实 `ExplorationTaskV2EntryService` 替换占位实现：

- 本次只支持 H5；App/Web 明确返回结构化未接入错误；
- 复用统一 H5 目标构造、扫描预算和任务提交服务；
- 锁定 `workflow_version=v2`、`execution_mode=exploration_only`；
- 返回真实 `task_id`、`run_id` 和排队状态。

## 5. Worker 与证据交接

### 5.1 共享探索阶段

两种模式均执行同一 H5 V2 Runtime：

```text
任务入队
  -> H5 V2 路由
  -> Surface Session
  -> 初始 before/action/after Capture
  -> 页面类型判定
  -> 有界深度优先 + 覆盖缺口广度补齐
  -> 每步 before/action/after Capture
  -> 页面类型与谱系持续入库
  -> Explorer 局部终态
  -> Probe 排空
  -> Session 有序关闭
```

Explorer 不获得浏览器、数据库、Rule Judge 或报告写权限。所有动作仍由 ActionIntent、边界、风险策略、预算与 Session Command Gate 控制。

### 5.2 证据交接门禁

H5 Runtime 返回且 Session 关闭后，Worker 通过独立的 H5 证据库存读取器检查当前 `task_id + agent_run_id`：

- 至少存在一条 `captured` 且未被废弃的 `after_action` 页面 Capture；
- Capture 归属当前任务和 Run；
- 页面判定状态为 `recognized`，且具有规范化 `page_type`；
- after 与其 parent before 的公共/页面 Capture 角色必须分别一致为 `after_action`/`before_action`；
- before/after 必须绑定同一 `action_execution_id`，两侧核心截图、DOM 和非空 URL 均存在；
- 需要登录但没有安全游客入口时，保留 `requires_auth`，不能将门禁包装为成功。

门禁失败时任务进入失败或人工复核，不发布正式质检工作，也不生成封存报告。

### 5.3 完整质检收口

门禁通过后，完整质检模式按已识别 Capture 顺序执行：

1. 根据 `page_type + page_state` 获取任务锁定规则集中的候选规则；
2. 只发布与不可变快照证据兼容的正式 Inspection Work Item；
3. 统计被排除的 `active_session` 规则；只要存在此类缺口，完整质检必须转 `needs_review/incomplete`，禁止声称全覆盖；
4. 幂等排空工作项并执行 Rule Judge；
5. 正式结果写入 `qc_rule_execution`；
6. 最终化缺失结果和非终态工作；
7. Completion Gate 通过后生成并绑定 sealed Report。

独立规则测试仍只写 `qc_rule_evaluation_run`，不得成为正式报告的数据源，也不得覆盖上述正式结果。

### 5.4 仅探索收口

门禁通过后，仅探索模式：

- 记录 `evidence_ready=true`、可判定 Capture 数、页面类型数、终止原因和阻断摘要；
- 不发布 Inspection Work Item；
- 不调用 Rule Judge；
- 不写 `qc_rule_execution`；
- 不运行 Completion Gate；
- 不生成正式或预览报告；
- 按普通任务终态持久化 Run 与 Task，使其可在规则测试页面被选择。

这样形成“先探索留证，再按需重复规则测试”的闭环，同时保持正式质检与实验性重测物理隔离。

## 6. 前端行为

创建任务页开放“仅页面探索”选项：

- 完整质检：调用 `POST /api/v2/tasks`；
- 仅页面探索：只允许 H5，调用 `POST /api/v2/exploration-tasks`；
- 选择仅探索后若切换到 App/Web，前端立即恢复完整质检并给出说明；后端仍执行最终校验；
- 提交成功进入现有任务详情；部署模式未开启时显示后端结构化错误；
- 规则测试页面继续使用独立规则评估接口，不把实验结果展示成正式报告。

## 7. 错误与安全语义

- H5 V2 未开启：请求不入库、不入队，返回可识别冲突错误；
- Web 或仅探索 App 未接入：返回结构化 501；
- Redis/PostgreSQL 不可用：保持现有 503；
- 页面无法判定或核心证据缺失：证据门禁失败，禁止 Rule Judge；
- `requires_auth`：保存已取得证据并进入人工复核/阻断，不自动输入凭据；
- Session 关闭失败：任务失败或人工复核，不能生成 sealed Report；
- 重复发布：依赖现有 Inspection Work Item 输入哈希和唯一约束幂等；
- 页面内容、DOM、URL 和截图始终视为不可信输入。

## 8. 测试与验收

实施必须按 TDD 完成以下验证：

1. V2 H5 完整任务从 API 到 `TaskSubmissionService` 的契约测试；
2. `off`/`shadow` 拒绝、`on` 接受的配置门禁测试；
3. H5 仅探索入口的成功、平台拒绝和下游失败测试；
4. 执行模式默认兼容、锁定和未知值失败关闭测试；
5. Worker 两种模式分流测试：仅探索不触发发布/Judge/报告，完整质检全部触发；
6. 证据门禁的归属、状态、页面类型、截图、DOM 和空证据测试；
7. 正式质检发布幂等、规则写回和 Completion Gate 测试；
8. 前端 API 与创建页两种任务类型测试；
9. 后端相关回归、前端全量测试和前端生产构建；
10. 条件允许时执行真实 PostgreSQL + Redis + Playwright 冒烟；条件不具备时必须明确记录为部署验收项，不能用单元测试冒充真实环境闭环。

最终审核必须逐项证明：API 可达、任务能入队、H5 V2 路由不会回退、证据完整入库、仅探索无规则副作用、完整质检有正式规则结果、报告门禁有效、独立规则重测与正式结果隔离、认证边界未被突破。

## 9. 发布要求

- 保持 `ALIOTH_H5_V2_MODE` 默认 `off`，不通过代码改默认值绕过灰度；
- 部署环境完成真实冒烟后显式设置 `ALIOTH_H5_V2_MODE=on`；
- 上线前确认 PostgreSQL 长期状态存储、Redis、Playwright/浏览器、证据存储和语义判定模型均已配置；
- 若任一关键依赖缺失，系统必须失败关闭，不得回退为“成功但未质检”。
