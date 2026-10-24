# Alioth App 页面探索整改实施计划

> 日期：2026-08-12
>
> 基线分支：`dev_1.5`
>
> 基线提交：`3e0b470b9776f186926b88e2fd139803b9f53da6`

## 1. 目标与结论

本轮仅处理 App 页面探索，不修改 H5 探索策略、H5 Runtime 或 H5 工作台投影。

目标是先关闭会阻断 App 正常探索或让成功终态失真的确定性缺口，再修正低风险的预算、覆盖口径和前端能力展示。所有缺少生产 Adapter 或外部验收的能力继续 fail-closed，禁止用日志、默认值或伪造结果冒充成功。

完成本计划后，仓库层面应满足：

1. APK 提交未提供包名时，Bootstrap 安装返回的真实包名成为后续探索的唯一身份边界。
2. App 视觉分析的完整 V2 字段可在 PostgreSQL 保存、重载和恢复后保持哈希一致。
3. Final Observation 必须形成可读取的视觉事实；缺失视觉事实时不能保持 `exploration_completed`。
4. 缺最终观察等证据不足情况保持 `INCOMPLETE / evidence_insufficient`，不被发布异常改写为通用 `FAILED`。
5. App 动作预算和 Coverage metadata 使用真实持久化事实；未接线的 App source / mode 不在 UI 中伪装为可用。

## 2. 本轮边界

### 2.1 纳入范围

- App Bootstrap → `AppReadyHandoff` → App Screenshot Exploration 的包身份传递。
- `AppVisualAnalysis` V2 字段、Repository、Alembic migration 和 round-trip 测试。
- Final Observation 的 Screenshot Understanding、视觉事实持久化、Evidence Index / Publisher 结果语义。
- App Runtime 的动作预算边界、Coverage pending 统计。
- App 创建页对当前后端能力的 fail-closed 展示。
- 与以上修复直接相关的单元测试、SQL 合同测试、前端测试和专项回归规则。

### 2.2 明确不纳入范围

- H5 页面探索的 Frontier、导航、预算、取消和工作台投影。
- 新建 App `download_url` 制品下载 Resolver。
- 在没有正式合同和真实运行时验收前开放 App exploration-only API。
- Official App / 云手机供应商接口改造和生产凭据配置。
- Explorer / Probe 独立 Worker、Outbox Stream 消费器等共享架构改造。
- 推送远端；本轮只做经审查的本地 Git 提交。

## 3. 分批实施与提交

### 批次 0：计划与验收基线

交付：

- 冻结 App-only 范围、批次依赖和验收口径。
- 记录每批是否包含数据库迁移、是否需要前端同步、未覆盖风险。

提交类型：文档；无数据库迁移；无前端菜单或路由变更。

### 批次 1：安装包身份闭环

问题：`package_name` 可空，Bootstrap 能取得 `installed_package`，但后续 `AppGoal` 和 `ExplorationBoundaries` 仍使用任务输入或字面值 `app`，真实候选会被 host gate 拒绝。

实施：

1. 将已验证的 `installed_package` 纳入版本化 `AppReadyHandoff`。
2. 新运行和 APP_READY 恢复均从 Bootstrap snapshot 构造相同身份。
3. App Runtime 使用 handoff 中的包名构造 goal 和 allowed hosts。
4. 对“任务包名为空”“任务包名与安装结果不一致”“handoff 序列化恢复”增加回归。

提交类型：App domain / application；无数据库迁移；无前端菜单或路由变更。

### 批次 2：视觉事实 PostgreSQL round-trip

问题：V2 `result_hash` 包含 `page_state`、`overlay_signature`、`surface_facts`，但表结构和 Repository 不保存这些字段，重载会触发哈希不一致。

实施：

1. 新增 Alembic migration，补齐三个字段及合法默认值/约束。
2. Repository 的 SELECT、INSERT、冲突幂等与 row parser 完整读写字段。
3. 增加 stable page、overlay 和非空 surface facts 的保存/重载测试。
4. 对旧行采用与旧合同一致的默认值，确保升级后可读；不改写已有 `result_hash`。

提交类型：App infrastructure + 数据库迁移；无前端菜单或路由变更。上线前必须在隔离 PostgreSQL 执行 migration 与 round-trip 验收。

### 批次 3：最终观察与证据不足终态

问题：Final Observation 只 Capture 不做 Screenshot Understanding；Publisher 缺视觉事实时只 skip。缺最终观察的 evidence-insufficient 结果又可能被 Publisher 异常改写为 FAILED。

实施：

1. Final Observation Capture 成功后执行与普通 Capture 相同的视觉理解和持久化。
2. Publisher 对任何 Index entry 缺可信视觉事实时 fail-closed，不允许保留成功 outcome。
3. 明确“已封存但证据不足”与“系统执行失败”的边界；Worker 保留 INCOMPLETE 原因。
4. 覆盖 final visual success、visual missing、final entry missing、publisher infrastructure failure 四类测试。

提交类型：App application / Worker；无数据库迁移；无前端菜单或路由变更。

### 批次 4：预算、覆盖口径与能力展示

问题：App loop 使用 `<= max_actions`，边界上可能额外 claim；Coverage pending 计数固定为 0；统一创建页展示后端稳定返回 501 的 App download URL。

实施：

1. 修正动作预算循环边界并补 0 / 1 / max actions 测试。
2. Coverage 从 PG readiness / work facts 投影 pending counts，无法取得事实时不得伪报 0。
3. App 创建页只展示当前已接线的 `build_ref`；未接线 source 明确禁用或隐藏。
4. 保持 API 对未接线能力的结构化 501，不接旧 Worker，不伪造成功。

提交类型：App application + Workstation；无数据库迁移；不新增菜单/路由，现有 App 入口兼容关系另行评审。

## 4. 每批审查与提交门禁

每一批必须按以下顺序执行，任一门禁失败不得提交：

1. 检查工作区，确认没有夹带用户原有未跟踪文件或其它模块改动。
2. 执行本批新增与受影响的定向测试。
3. 对 Python 变更执行 Ruff；对前端变更执行组件测试和生产构建。
4. 涉及迁移时，检查 Alembic 单一 head，并在可用的隔离 PostgreSQL 中执行 upgrade / round-trip。
5. 执行 `git diff --check`。
6. 只显式暂存本批文件，检查 `git diff --cached --name-status`、`git diff --cached --check` 和完整 staged diff。
7. 使用 `[AI编程]` 前缀创建本地提交；提交说明写明模块、迁移、前端同步和未覆盖风险。
8. 提交后确认工作区只剩用户原有文件和下一批尚未暂存的改动。

## 5. 最终回归矩阵

### 5.1 后端仓库回归

- App Workflow contracts / bootstrap / recovery。
- App Runtime composition / screenshot exploration / turn / terminal / closure。
- App Visual Observation Repository / migration contract。
- Sealed Inspection Publisher / Worker Completion Gate。
- App V2 task entry / App build submission。
- Ruff、Alembic head、`git diff --check`。

### 5.2 前端仓库回归

- `TaskCreateView` App source 选择和提交 payload。
- `AppQcCreateView` 兼容入口不受影响。
- Workstation 测试全量与 `npm run build`。

### 5.3 仍需部署级验收

- 真实 PostgreSQL migration、索引与重启恢复。
- Redis queue / cancel / lease。
- MinIO artifact bytes、checksum、Manifest 与 Evidence Index 一致性。
- Official App API / Android 云手机：start → install → open → capture → tap → capture → close。
- 真实视觉 Provider 与 Final Observation Understanding。
- 完整任务从 API 提交到 Completion Gate、报告和工作台 Timeline 的 E2E。

仓库测试通过只作为代码合同证据；部署级验收未完成前，`app_qc_enabled` 和生产 Runtime 开关保持受控。
