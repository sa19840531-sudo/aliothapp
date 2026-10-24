# App 截图探索单轮闭环设计

## 1. 背景与目标

App Workflow Runtime 已完成 Android APK 校验、安装、打开、首屏截图以及 `APP_READY` 交接，但当前生产 Worker 仍未消费 App 路由。下一阶段需要在不依赖正式星语 HTTP API 的前提下，使用 Fake Runtime 验证以下完整单轮：

```text
APP_READY
→ 截图理解
→ Frontier
→ Exploration Work
→ Explorer 选择 candidate_id
→ ActionIntent
→ before capture
→ App 动作
→ after capture
```

本阶段继续以截图作为 App 页面唯一事实，不引入 DOM、UI Tree、Visible Text、Appium locator、XPath 或 CSS selector。

## 2. 范围

### 2.1 本阶段包含

- 版本化 App 视觉理解输出合同。
- Screenshot Artifact 的可信加载、checksum 校验和有界模型输入。
- Fake Screenshot Understanding Provider。
- App 视觉事实、候选目标和 Frontier 构造。
- `APP_READY` 到 Exploration Work 的幂等发布。
- 公共 Explorer Work 仓储支持 `qc_capture`，同时接受 H5 与 App Capture。
- Explorer 的 Surface 级 Capture Profile 策略。
- 候选目标的确定性解析与陈旧候选拒绝。
- 公共 Capture Coordinator 从页面专属引用迁移到公共 Capture 引用。
- Fake Runtime 下完成一次 App 点击和 after screenshot。
- App V2 组合根与显式可调用入口。

### 2.2 本阶段不包含

- 正式星语 HTTP Adapter。
- OCR 全文索引、DOM 推断或 UI Tree 构造。
- 多轮自主探索、页面图谱收敛和任务最终收口。
- Inspection Worker、Rule Judge 或工作台 UI。
- 生产 `worker._process_one()` 的 App 路由启用。
- iOS 实现。

## 3. 方案选择

采用复用公共探索合同的方案：保留 `SurfaceObservation`、`CandidateAction`、`FrontierSnapshot`、`ExplorationWork` 和现有工作状态机，新增 App 专用截图理解与候选解析层。

未采用的方案：

1. 独立创建 `AppObservation/AppFrontier/AppExplorer`：会形成第二套探索协议和工作状态。
2. 将 App 截图直接并入 `ObservationPacketService`：该服务面向 Inspection/Judge，混用会模糊探索输入与规则证据的职责。

## 4. 架构与职责

### 4.1 AppScreenshotUnderstandingService

负责消费已持久化的 App Capture 引用，读取截图元数据与 Artifact 引用，校验截图归属、PNG 类型、大小和 checksum，然后调用 Screenshot Understanding Provider。服务不执行动作，不访问 SurfaceDriver。

图片字节仅允许在 Artifact Store 到受信任 Provider 的进程内边界出现。数据库、Outbox、Redis、Work Item 和 Agent JSON 只传 Artifact ID、Capture ID、hash 和有界语义结果。

### 4.2 ScreenshotUnderstandingProvider

Provider 输入包含：

- 任务、Session、Capture 与 Artifact 身份。
- PNG 图片字节。
- 图片尺寸、方向和 image signature。
- 允许的动作集合与候选数量上限。

Provider 输出 `AppVisualAnalysis`，包含：

- `visual_page_type`
- `visual_summary`
- `key_elements`
- `candidate_targets`
- `provider_id`
- `model_id`
- `contract_version`

Fake Provider 返回确定性结果；正式 Provider 后续实现同一端口。

### 4.3 AppVisualCandidate

每个候选包含：

- `candidate_id`
- `semantic_name`
- `action_type`
- `normalized_bounds`
- `confidence`
- `risk_level`
- `purpose`
- `expected_state_hint`
- `source_capture_id`
- `source_image_signature`
- `source_width`
- `source_height`
- `source_orientation`

`normalized_bounds` 使用 `[left, top, right, bottom]`，所有值必须处于 `[0, 1]`，且 `left < right`、`top < bottom`。候选 ID 由 Capture、动作、语义名称和标准化边界生成稳定 hash，不能由模型随意指定最终身份。

### 4.4 AppScreenshotExplorationWorkflow

消费 `AppReadyHandoff`，按以下顺序执行：

1. 校验 Handoff 指向 Android App Capture。
2. 查找该 Capture 已存在的视觉事实。
3. 不存在时读取截图并调用 Provider。
4. 校验、标准化并持久化视觉事实。
5. 映射为公共 `SurfaceObservation` 和 `FrontierSnapshot`。
6. 使用公共 Orchestrator 发布 `ExplorationWork`。
7. 返回只含事实与 Work 引用的结果。

重复消费相同 Handoff 必须返回相同视觉事实和 Work，不得重复调用 Provider 或创建重复 Outbox。

### 4.5 Explorer Agent

继续使用现有 MAF Explorer，但 Capture Profile 必须按 Surface 选择：

- Web/H5：保留 screenshot、dom、visible_text、ui_snapshot 与 action_trace。
- App：只允许 screenshot 与 action_trace。

Explorer 只读取已有候选并选择 `candidate_id`，不得生成坐标、locator、设备句柄或新的候选。第一阶段 App 动作 allowlist 为 `click`、`scroll`、`back`、`observe`；完整闭环验收使用 `click`。

### 4.6 AppCandidateResolver

Resolver 位于 Interaction Runtime 内部，不属于 Agent。它根据 `ActionIntent.source_capture_id` 和 `candidate_id` 加载持久化候选，并在 Session Command Gate 内完成以下校验：

- 候选属于当前任务和 Session。
- 候选绑定的 Capture 与 ActionIntent 一致。
- 动作类型和语义目标与候选一致。
- before screenshot 的尺寸、方向和 image signature 与候选源截图一致。
- 候选未被使用于冲突的 Action Execution。

校验通过后，Resolver 将 normalized bounds 转换为像素中心点。派生坐标只存在于 Runtime 内部传输 DTO；持久化的原始 Agent ActionIntent 保持语义目标和 candidate ID，不把模型生成坐标伪装成 Agent 决策。

### 4.7 Capture Coordinator

现有 Coordinator 使用页面专属 `CaptureReference.page_capture_id`。本阶段将其公共边界迁移为 `CaptureRef.capture_id`，让 Web 与 App 使用相同的 before/action/after 顺序、Session Command Permit、Action Execution 和 Evidence Exit Gate。

Web Capture Writer 通过兼容映射继续写入 `qc_capture + qc_page_capture`；App Capture Writer 写入 `qc_capture + qc_app_capture`。禁止复制一个独立 App Coordinator。

## 5. 数据模型

### 5.1 qc_app_visual_observation

新增 App 视觉事实表：

```text
observation_id uuid primary key
task_id uuid not null
session_id text not null
capture_id uuid not null unique
screenshot_artifact_id uuid not null
contract_version text not null
provider_id text not null
model_id text not null
result_hash text not null
visual_page_type text
visual_summary text not null
key_elements jsonb not null
candidate_targets jsonb not null
created_at timestamptz not null
```

该表是不可变视觉事实，不是任务状态或规则执行状态。相同 Capture 的 payload hash 不一致时拒绝覆盖。

### 5.2 qc_v2_work_item

不新增 App 专属 Work 表。`PostgresExplorerWorkRepository.publish()` 的 Capture 归属校验从 `qc_page_capture` 调整为公共 `qc_capture`，仍校验：

- Capture 属于任务。
- Capture 状态为 captured。
- 任务处于可运行状态。
- finalization state 仍为 open。
- Work ID、payload hash 和 finalization epoch 一致。

### 5.3 App 动作 Capture

扩展 App Capture Persistence，使非 Bootstrap Capture 可以保存：

- `capture_role=before_action|after_action`
- `parent_capture_id`
- `action_execution_id`
- `session_command_sequence`
- App Screenshot Artifact
- package、activity、width、height、orientation、image signature 和 device snapshot

App Capture 不允许 DOM、UI Tree、Visible Text 或 OCR 全文进入 metadata。

## 6. 单轮数据流

```text
AppReadyHandoff
  → AppScreenshotUnderstandingService
  → qc_app_visual_observation
  → SurfaceObservation + FrontierSnapshot
  → qc_v2_work_item + Outbox
  → AppExplorationTurnRunner claim
  → MAF Explorer.decide_next
  → semantic ActionIntent(candidate_id)
  → Session Command Gate
  → before App Capture
  → AppCandidateResolver
  → Runtime 内已解析坐标
  → SurfaceDriver.execute
  → after App Capture
  → Action Execution evidence complete
  → Exploration Work succeeded
```

`observe` 不改变页面，但仍产生 before/after 证据；`back` 和 `scroll` 不要求点目标，仍必须绑定源 Capture。第一阶段 Fake 成功路径固定选择一个低风险 click 候选。

## 7. 并发、幂等与恢复

- 视觉事实以 `capture_id` 唯一，结果由标准化 payload hash 锁定。
- Exploration Work 继续使用公共稳定身份和 payload hash；同一 Handoff 重放不重复发布。
- App Turn 只按指定 `task_id` 领取 Work，领取后必须确认 `surface_kind=android`。
- Work 状态使用既有 `pending/leased/running/...`，不增加 App 终态。
- 动作前必须持有 Session Command Permit。
- 动作使用已持久化 Action Execution 和稳定 `action_execution_id`；外部超时后重放同一 ID。
- before Capture 与候选源截图不一致时，在 Driver 调用前返回 `STALE_VISUAL_CANDIDATE`。
- after Capture 失败时 Action Execution 进入 evidence failed，Work 不得记普通成功。
- 旧 lease、旧 row version 或旧 finalization epoch 的写回必须失败。
- Redis 只负责唤醒；PostgreSQL 是最终事实源。

## 8. 错误合同

稳定错误至少包括：

- `SCREENSHOT_ARTIFACT_MISSING`
- `SCREENSHOT_CHECKSUM_MISMATCH`
- `VISUAL_UNDERSTANDING_FAILED`
- `INVALID_VISUAL_OUTPUT`
- `STALE_VISUAL_CANDIDATE`
- `CANDIDATE_NOT_FOUND`
- `ACTION_UNSUPPORTED`
- `BEFORE_CAPTURE_FAILED`
- `AFTER_CAPTURE_FAILED`

错误分类规则：

- Artifact 缺失、checksum 不一致和非法 Provider 输出不得重试模型后伪装成功。
- 明确网络超时和 retryable 5xx 可按预算重试。
- 陈旧候选需要重新截图理解，不得在原 Work 内盲目重试动作。
- Driver 已可能执行但返回未知时，保留 Action Execution 未决/失败审计，不创建第二个 Action Execution。
- 所有外部错误规范化后持久化；原始鉴权信息和设备句柄禁止进入错误正文。

## 9. Fake Runtime 与组合根

Fake Screenshot Understanding Provider 根据图片签名和预设场景生成确定性页面摘要及候选。Fake App Runtime 支持截图序列：Bootstrap、before 和 after 可以返回不同 PNG，从而验证页面变化和陈旧候选检测。

新增 App V2 组合根，共享同一个 `AppRuntimeBundle`：

- Bootstrap Port 与 SurfaceDriver 必须来自同一 Runtime 实例。
- Screenshot Understanding Provider 独立注入。
- Artifact Store、视觉事实仓储、Explorer Work 仓储、Session Gate、Action Execution 和 Evidence Exit Gate 使用既有端口。
- Fake Provider 仅允许非生产环境。
- Official Provider 仍显式返回未实现错误。

本阶段不修改生产 `worker._process_one()`。组合根提供显式的 Bootstrap、Publish Exploration 和 Run One Turn 调用入口，供集成测试及后续正式 Worker 路由使用。

## 10. 安全与所有权

- 青云拥有 Workflow、Capture、视觉理解、Frontier、ActionIntent、Work Item 和审计状态。
- 星语只负责未来正式 Adapter 中的设备、安装、打开、动作和截图执行。
- Explorer、Judge 和规则代码禁止导入 SurfaceDriver、App Runtime、星语 Client 或 Artifact 字节读取器。
- Token、Cookie、密码、签名密钥、APK 内容和设备句柄不得进入 Agent 上下文或 Redis。
- Agent 输入只包含有界页面摘要、候选语义、风险、置信度和稳定引用。

## 11. 测试与验收

### 11.1 合同测试

- App 视觉合同 round-trip。
- 未知未来字段兼容。
- 非法 normalized bounds、confidence、动作类型和身份拒绝。
- 候选稳定 ID 和结果 hash 可重放。

### 11.2 服务测试

- Screenshot Artifact 只从受信 Artifact Store 加载。
- checksum 不一致时 Provider 调用次数为零。
- Provider 输出标准化后构造公共 Observation 与 Frontier。
- Work/Agent JSON 不出现图片字节、Base64、DOM、UI Tree 或 locator。

### 11.3 持久化测试

- 迁移创建 `qc_app_visual_observation` 并保持公共终态不变。
- 相同 Capture 幂等，相同 Capture 不同 hash 拒绝覆盖。
- `qc_v2_work_item` 可以引用 App Capture，H5 路径继续通过。
- App before/after Capture 正确关联父 Capture 和 Action Execution。

### 11.4 运行时测试

- Explorer 只能选择当前 Frontier 的 candidate ID。
- App Capture Profile 只包含 screenshot/action_trace。
- before screenshot 与候选不一致时不调用 Driver。
- 相同 Action Execution 重放不重复点击。
- after screenshot 失败产生 evidence failed。
- Fake Runtime 完成 `APP_READY → Work → click → after capture`。

### 11.5 架构与回归测试

- Explorer/Judge 不得导入 Driver、App Runtime 或 Artifact Reader。
- 不在生产 Worker 中直接构造 Fake Runtime。
- Ruff 通过。
- App 定向测试通过。
- 完整 API 测试通过。

## 12. 完成标准

1. 相同 APP_READY Handoff 重放不会重复理解或发布 Work。
2. App Exploration Work 使用公共工作状态和 Outbox。
3. Explorer 只选择候选，不生成坐标或 locator。
4. Runtime 在 Session Gate 内拒绝陈旧候选。
5. 一次成功点击具有 before/action/after 完整审计链。
6. after screenshot 写入公共 Capture 与 App 子事实表。
7. Fake Runtime 可稳定复现成功、陈旧候选、动作超时和 after capture 失败。
8. H5 Capture Coordinator 与 Explorer 回归不变。
9. 生产 Worker 的 App 路由继续关闭。
10. 不引入第二套任务、工作或规则终态。
