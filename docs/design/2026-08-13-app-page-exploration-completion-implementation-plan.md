# Alioth App 页面探索业务闭环完善实施计划

> 日期：2026-08-13
>
> 基线分支：`dev_1.5`
>
> 基线提交：`ccbf695`
>
> 范围：仅 App 页面探索业务逻辑，从已经锁定的 App Task / `APP_READY` handoff 开始，到 Evidence Index、`AppExplorationResult` 与 Surface Session 终态结束。

## 1. 入口口径与范围纠正

本计划不改入口。入口事实固定为：

- 新的完整质检入口只有 `POST /api/v2/tasks`。
- 单独页面探索使用独立页面探索 API：`POST /api/v2/exploration-tasks`。
- 两个入口的路由、请求模型、前端页面、菜单、兼容策略和是否开放 App 平台，均不在本轮实施范围内。
- 本轮只要求入口交付的版本化 App Task、Run、Target 和执行模式能够被 App Runtime 正确消费；不在探索模块里反向改造入口。

业务闭环边界如下：

```text
已受理 App Task / Agent Run
  -> APP_READY handoff
  -> Screenshot Understanding / Visual Observation
  -> App Goal / Budget / Frontier
  -> 当前截图 candidate_id
  -> package / activity / risk gate
  -> before Capture -> 单动作 -> after Capture / Timed Observation
  -> Coverage + 全局 Frontier 收敛
  -> Final Observation
  -> Manifest finalize / Evidence Gap
  -> Evidence Index create / seal
  -> AppExplorationResult
  -> Surface Session CLOSED / LOST
```

本轮终点是“页面探索证据与终态已经正确、完整、可恢复地落库”。以下内容全部排除：

- `/api/v2/tasks` 与 `/api/v2/exploration-tasks` 的接口改造或接线。
- V1/V2 入口收敛、旧入口兼容、前端菜单和创建页。
- H5/Web 页面探索及其预算、Frontier、恢复和工作台投影。
- Rule Judge、Observation Packet、Inspection Work、Completion Gate、正式报告和人工复核。
- App 重跑/复制、工作台 Timeline、AppStage 和 Report Center。
- Probe、自动补采、规则取证副作用。
- App `download_url` Resolver。

## 2. 计划目标

完成本计划后，App 页面探索必须满足：

1. 任意进入 Evidence Index 的 PASS Capture 都有可信、可重载、与 Manifest 绑定的视觉事实。
2. 含 click/scroll/back 等动作的正常探索可以得到 `exploration_completed`，不再因 ACTION_BEFORE 缺视觉事实被确定性降级。
3. Manifest FAIL/PENDING/缺失、视觉理解失败、取消或覆盖不足可以封存为带明确 gap 的 `evidence_insufficient`。
4. PostgreSQL 触发器、应用层 Closure 和 Domain outcome 使用同一封存合同。
5. Closure 任何阶段失败后都不会遗留没有恢复事实的 ACTIVE/DRAINING Session。
6. App Task 已保存的 `scan_mode`、`loop_guard`、Journey 和目标配置真正约束 Runtime；本轮不改变这些入口字段。
7. Playbook 阶段、动作预算和导航深度彼此可达，不再出现必达 Tab 超出最大深度。
8. 未选择的安全业务候选不会随“唯一后继”丢失；全局 Frontier 排空才允许正常完成。
9. Worker/设备/Redis 故障不会造成重复动作、永久租约或不可恢复的 App Run。
10. 真实 PostgreSQL、对象存储、视觉 Provider 和 Official 设备完成 `APP_READY -> Result -> Session terminal` 的部署级验收。

## 3. 已完成基线

以下能力已经落地，本轮不重复实现：

- APP_READY handoff 使用安装成功后的真实 `installed_package`；Goal、边界和动作执行器均锁定该身份。
- `AppVisualAnalysis.page_state/overlay_signature/surface_facts` 已由 0049 migration 持久化并参与 hash 校验。
- Final Observation 已在封存前执行 Screenshot Understanding 并保存视觉事实。
- Runtime 已区分动作预算与 turn 数，并在 Bootstrap、每轮动作前后检查取消。
- Coverage pending Action/Evidence 已读取 PostgreSQL 事实，不再硬编码为零。
- 缺视觉事实时已有防御性 fail-closed，不再伪造探索成功。

当前必须处理的核心缺口：

- P0：ACTION_BEFORE Capture 会进入 Evidence Index，但正常链路没有为它生产视觉事实。
- P0：0046 trigger 无条件拒绝存在最新非 PASS/缺失 Manifest 的 sealed Index，与应用层允许 `evidence_insufficient + gaps` 冲突。
- P1：Closure 启动后异常可能跳过 Session LOST 收口。
- P1：App Runtime 没有真正消费任务已保存的 `scan_mode/loop_guard`。
- P1：Playbook phase 使用导航深度当游标，8/9 专用/默认课纲存在必达阶段不可达风险。
- P1：当前只提交一个 successor，未选 sibling 候选会丢失，不能证明全局覆盖。
- P1：进程崩溃、Redis 丢唤醒或设备断连后，缺少同一 App Run 的完整自动恢复闭环。

## 4. 批次依赖与交付节奏

```mermaid
flowchart LR
    B0["批次 0<br/>失败基线"] --> B1["批次 1<br/>Capture 视觉事实完整性"]
    B1 --> B2["批次 2<br/>Evidence 封存与 Session 收口"]
    B2 --> B3["批次 3<br/>Goal / Budget / Playbook"]
    B3 --> B4["批次 4<br/>App 全局 Frontier"]
    B4 --> B5["批次 5<br/>App 恢复闭环"]
    B5 --> B6["批次 6<br/>App 部署级验收"]
```

| 里程碑 | 批次 | 粗略工程量 | 出口条件 |
|---|---|---:|---|
| M1 主链解阻 | 0～2 | 6～9 工程日 | 两个 P0 关闭，真实 PG 可封存 completed/insufficient |
| M2 探索完整性 | 3～4 | 10～16 工程日 | 预算可达、全局 Frontier 不丢业务分支 |
| M3 可恢复运行 | 5 | 5～8 工程日 | 崩溃、断连、重复唤醒不产生重复动作或孤儿 Session |
| M4 部署验收 | 6 | 4～7 工程日以上 | 真实设备与基础设施完成闭环验收 |

总工程量粗估 25～40 工程日，不含供应商排期、生产审批和外部凭据等待。该估算只用于排期，不作为验收结果。

## 5. 分批实施方案

### 批次 0：固化 App 业务闭环失败基线

目标：用真实生产组合和 PostgreSQL 场景锁定当前两个 P0，避免“单测全绿但组合必失败”。

实施项：

1. 设计一次真实动作的组合回归：APP_READY → decision → before/after Capture → Closure → sealed Index。
2. 在修复前验证 ACTION_BEFORE 缺视觉事实，且正常动作型结果无法满足完整 Index 事实。
3. 设计 PostgreSQL trigger 场景：
   - completed + 全部最新 Manifest PASS；
   - completed + FAIL/PENDING/missing；
   - insufficient + FAIL/PENDING/missing + 对应 gap；
   - insufficient + 非 PASS 但无对应 gap。
4. 设计 Closure 在 index create/seal、result write、session close 各阶段失败的终态矩阵。
5. 红灯测试分别随批次 1、2 的修复提交，不单独提交一个让主分支持续失败的测试批次。

预计文件边界：App 生产组合测试、Evidence Index PostgreSQL integration、Closure/Runtime fault tests、本计划文档。

验收：测试能精确证明失败来自 ACTION_BEFORE visual gap 和 0046 trigger，不用 fake Repository 绕过数据库行为。

建议提交：计划文档可独立提交；红灯测试分别与对应修复一起提交。

数据库迁移：无。前端：无。复杂度：S。

### 批次 1：补齐 App Capture 视觉事实完整性

目标：任何 PASS Evidence Index Entry 在封存前都已经具备可信视觉事实。

设计原则：不通过排除 ACTION_BEFORE 来掩盖缺口。ACTION_BEFORE 是动作语义证据的一部分，继续保留在 Index 中。

实施项：

1. App 动作协调完成后，对 before/after Capture 都生产 `AppVisualObservation`，不再只理解 after Capture。
2. APP_READY、ACTION_BEFORE、ACTION_AFTER、SURFACE_BEFORE、SURFACE_AFTER、REOBSERVE、FINAL_OBSERVATION 使用同一视觉事实身份合同。
3. Closure 在 seal 前执行统一完整性检查，作为异常恢复和历史数据补漏：
   - 按 `capture_id` 查已有 Observation；
   - 缺失时只从该 Capture 的可信 Screenshot Artifact 理解；
   - 校验 task/run/session、capture、artifact ownership、PNG、SHA-256 和截图身份；
   - 幂等保存并验证 result hash。
4. 已有事实必须复用；Closure 重放不能再次调用 Provider 或创建第二条 Observation。
5. 若基于相同截图内容复用理解结果，仍必须生成绑定新 capture/artifact 身份、可验证 provenance 的 Observation，禁止直接借用另一个 Capture 的记录。
6. 视觉失败生成带 `capture_id + semantic_role + error_code` 的 gap，并使探索 outcome 为 `evidence_insufficient`；Provider/Artifact 技术错误不得伪装为业务不合格。
7. 保留 sealed consumer 的缺事实防御门禁，但正常路径不应等到下游消费者才发现缺口。

关键测试：

- click、scroll、back 的 before/after/final 均有视觉事实。
- 动作失败后只有 before Capture 时可以形成可审计 insufficient 结果。
- Surface Phase before/after 与普通 action 使用相同身份合同。
- Provider 超时、坏 PNG、artifact hash 不匹配、Observation 冲突和 Closure 重放。
- 一次含动作的完整 App 探索可以保持 `exploration_completed`。

预计文件边界：

- `app_screenshot_exploration.py`
- `app_exploration_closure.py`
- `app_v2_runtime.py`
- App Visual Observation Port/Repository
- App exploration cycle/closure/组合测试

验收：Evidence Index 每个 PASS Entry 均能按 capture_id 读取哈希一致且属于同一 Manifest 的视觉事实；ACTION_BEFORE 不再导致确定性降级。

建议提交：`[AI编程] fix(app): 补齐动作证据视觉事实`

数据库迁移：无。前端：无。复杂度：M。

### 批次 2：统一 Evidence Index 封存与 Session 终态

目标：让 Domain、Closure、Repository 和 PostgreSQL 对 completed/insufficient 使用同一合同。

实施项：

1. 从实现时的当前单一 Alembic head 追加 migration；基线 head 为 0049，当前预期新 revision 为 0050。禁止修改已发布的 0046。
2. 替换 Evidence Index header trigger：
   - `exploration_completed`：所有 captured Capture 的最新 Manifest 均为 PASS、无 evidence gaps、存在 FINAL_OBSERVATION；
   - `evidence_insufficient`：允许 FAIL/PENDING/缺失 Manifest，但每个非 PASS Capture 必须有对应 gap；
   - Index Entry 始终只能引用最新 PASS Manifest；
   - sealed Index 的 outcome、terminal reason、gaps、content hash 与 entry 集合不可变。
3. 对 gap 的 capture_id、semantic role 和 reason code 建立一致性校验；取消等没有单一 Capture 的 run-level gap 使用显式类型，禁止用模糊 gap 绕过门禁。
4. 同一 Run 的 Final Observation、Index、Result 和 Session close 使用跨 Worker 的整段 Closure 锁，等待锁的取消者不能误伤正在排空的赢家 Session。
5. Closure 在 capture finalization、index create/seal、result create 任一阶段异常时，必须 best-effort 标记 Session LOST 或持久化明确的可恢复 DRAINING 状态。
6. 去掉 `closure_started=True` 后不再 mark LOST 的语义漏洞。
7. building Index 绑定 `coverage_summary + started_at` 的结果上下文摘要并纳入封存 hash；迁移前遗留的 building Index 只允许一次性绑定同一 Run 的上下文，并在重试时按最新 Manifest 对账 Entry；sealed Index 缺 Result 时只允许同一上下文恢复，禁止结果漂移。
8. Index/Result 已封存而 readiness、seal-for-close、Driver close 或 mark-closed 失败时，不回滚或篡改证据；Session 进入 LOST、保存关闭错误并转人工复核。
9. Migration downgrade 必须阻止静默丢失新语义。

关键测试：

- 在隔离 PostgreSQL 真正执行 0049 → 新 head migration。
- completed/insufficient 与 PASS/FAIL/PENDING/missing Manifest 的完整矩阵。
- gap 与 Capture 对不上、缺 gap、重复 gap、run-level gap。
- index create、seal、result write、session close 故障矩阵。
- 重放幂等、历史 building 上下文补绑、旧 Entry 对账、缺 Result 的同上下文恢复、上下文漂移拒绝、封存后 Capture/Manifest/Session 身份修改被拒绝、并发 Closure 只有一次 Final Observation 和一个结果。

预计文件边界：

- 新 Alembic migration
- `app_evidence_index_repository.py`
- `app_exploration_closure.py`
- `app_runtime_composition.py`
- Evidence Index/Closure/PostgreSQL integration tests
- deployment migration head 说明

验收：真实 PostgreSQL 下 completed 和 insufficient 都能按合同封存；任何失败路径都得到 Result 或明确的可恢复/LOST Session 事实。

建议提交：`[AI编程] fix(app): 对齐探索证据封存与会话终态`

数据库迁移：有。前端：无。复杂度：L。

### 批次 3：统一 App Goal、Budget 与 Playbook 推进

目标：现有入口已经保存的 App 任务配置被 Runtime 正确消费，且所有接受的覆盖目标在预算内可达。

设计原则：只消费现有 Task/Run 配置，不修改 `/api/v2/tasks` 或独立页面探索 API 的请求合同。

实施项：

1. App Runtime 从当前 task record 读取并校验现有 `scan_mode`、`loop_guard`、Journey 和目标配置。
2. 定义这些现有字段到 `ExplorationBudget`、deadline、最大动作数、最大业务页面层级和分支上限的唯一映射。
3. `navigation_depth` 只描述业务页面层级，不再兼任 Playbook phase 游标。
4. 增加独立、可持久化的 `phase_cursor/completed_phase_ids`；Tab 切换和同页滚动不虚增页面层级。
5. Goal required milestones、Playbook phases、tab swipe plan 与 Runtime budget 在启动时做可达性验证；不可达配置 fail closed 并保存原因。
6. `minimum_business_pages` 从 App Goal/任务目标派生，不再对所有 App 固定为 2。
7. Coverage summary 保存预算来源、phase 进度、missing/blocked milestone 和耗尽原因。
8. 取消、deadline 和 action budget 使用同一时间/计数事实，禁止多套互相覆盖的常量。

关键测试：

- 9 套当前 Playbook 的 required Tab/phase 全部在预算内可达。
- quick/full/custom 等现有配置确实产生不同 Runtime 预算；如果某模式并非当前 App 合同允许值，应在消费层拒绝而不是猜测。
- scroll、Tab、详情下钻、BACK 分别更新正确的 depth/phase。
- APP_READY 恢复后 phase 和预算继续单调推进。
- 任务配置从 record 到 Runtime 的集成断言，不只验证入口写入。

预计文件边界：

- `app_runtime_composition.py`
- `app_script_curriculum.py`
- `app_capture_playbook.py`
- App Goal/Budget/Coverage domain
- phase progress Repository/migration（若需要）
- App Runtime/terminal/playbook tests

验收：任何接受的 App 任务都能证明 required milestones 在预算内可达；终态摘要能解释完成、阻断或预算耗尽原因。

建议提交：`[AI编程] fix(app): 统一探索目标预算与课纲推进`

数据库迁移：可能有。前端：无。复杂度：L。

### 批次 4：实现 App 跨页面全局 Frontier

目标：未选择的安全业务候选不再随单一 successor 丢失，`frontier_exhausted` 表示整个 App Run 的可访问业务分支已收敛。

实施项：

1. 定义 App Frontier Item：run/session、source capture/page identity、candidate fingerprint、导航路径、priority、status、blocked reason、lease 和 row version。
2. 当前截图生成的全部安全候选进入 PostgreSQL backlog；按规范化 fingerprint 在同一 Run 去重。
3. 单设备 Session 仍串行动作，但未选 sibling 保持 pending，不被当前 successor 覆盖。
4. 执行 sibling 前恢复并校验 source page identity；页面不一致时必须重新识别候选，旧坐标/candidate_id 不可盲用。
5. 只允许通过受控 BACK、重新打开当前视觉确认的安全 Tab 或可审计导航路径恢复来源页面。
6. pending/leased/retry/blocked Frontier 进入 Coverage 与 terminal 判定。
7. 必达 milestone 对应分支被 blocked 时只能 evidence-insufficient；非必达风险分支可按明确政策收口。
8. 取消必须原子取消剩余 App Frontier，不再生成后继。

关键测试：

- 首页同时发现 A/B/C 三个安全 Tab，选择 A 后 B/C 仍可恢复执行。
- sibling 来源页面变化后重新识别，不重放旧坐标。
- 重复候选、环路、BACK、外部包跳转和不可恢复页面。
- 多 Worker claim/lease expiry/row version/outbox 重放。
- 全局 pending 存在时任何 terminal proposal 都不能完成。
- 全部候选最终为 visited/blocked/cancelled，不存在静默丢失。

预计文件边界：

- App exploration domain/queue/turn/runtime
- `explorer_work_repository.py` 或独立 App Frontier Repository
- 新 Frontier migration
- App terminal/Coverage
- App multi-branch/concurrency tests

验收：多分支场景中，安全候选库存完整、状态可审计；正常完成必须建立在全局 Frontier 真正排空之上。

建议提交：`[AI编程] feat(app): 持久化跨页面全局探索Frontier`

数据库迁移：预计有。前端：无。复杂度：XL。

### 批次 5：完善 App Runtime 崩溃恢复与唤醒

目标：进程退出、Redis 丢消息或设备短暂断连后，从 PostgreSQL 事实恢复同一 App Run，且绝不重复设备动作。

实施项：

1. 只处理 App exploration Work 的唤醒合同：为 App exploration Stream 增加消费者，或删除无消费者路由并采用有指标的 PostgreSQL poller；不得顺带接入 H5/Probe。
2. Redis 仅携带 work identity；Consumer 每次重新进入 PostgreSQL claim/lease。
3. Worker 启动和 orphan reconcile 扫描可恢复 App Run：
   - APP_READY 且 Session 可 reattach：续领 pending/retry Frontier；
   - Session 不可 reattach：标 LOST，并按已有事实封存 insufficient；
   - action 尚未发送：安全重试；
   - action 已发送但 after Capture 未提交：根据 ActionExecution/设备事实恢复或阻断，禁止再次执行动作；
   - Capture 已提交但 successor 未创建：幂等补交。
4. Redis wakeup 丢失时，低频 PostgreSQL reconciliation 能重新唤醒 pending App Work。
5. ACK 只能发生在数据库确认终态、重试时间未到或 Work 已被其它有效租约领取之后。
6. Closure 中途崩溃从 sealed/building Index、Result 和 Session 事实恢复，不重复 Final Observation。

关键测试：

- crash before action。
- crash after driver/before after-Capture。
- crash after Capture/before successor commit。
- crash during Final Observation、index seal、result write、session close。
- Redis 重启、重复消息、Outbox 重放、Consumer 崩溃。
- 两 Worker 同时恢复时只有一个有效租约和一次设备动作。
- Session reattach 成功、失败和前台包不一致。

预计文件边界：

- `worker.py`
- `redis_stream.py`
- App Runtime/Bootstrap reattach
- App Work/ActionExecution/Outbox Repository
- App recovery/fault-injection tests

验收：任何故障点都不会产生重复动作、丢失 Frontier、永久租约或无终态 Session；恢复全过程可从 PostgreSQL 审计。

建议提交：`[AI编程] feat(app): 完善页面探索恢复与唤醒闭环`

数据库迁移：可能有。前端：无。复杂度：XL。

### 批次 6：App 页面探索部署级闭环验收

目标：证明仓库实现能够在真实基础设施和设备上完成本计划定义的闭环；不扩展到规则质检和报告。

验收起点与终点：

- 起点：已受理的 App Task/Run 已进入现有 App Runtime，并形成有效 APP_READY handoff。
- 终点：Evidence Index 与 `AppExplorationResult` 已封存，Surface Session 为 CLOSED 或带明确原因的 LOST。
- `/api/v2/tasks` 与独立页面探索 API 只作为现有上游调用，不做接口修改或入口验收扩展。

实施项：

1. 在隔离环境从历史 head 升级到最新 Alembic head，验证 schema、trigger、索引和 downgrade guard。
2. 使用至少一个通用 APK 和一个现有专用 Playbook APK，验证 APP_READY 后的多页面、多动作、多 sibling 探索。
3. 验证 completed、coverage insufficient、Manifest insufficient、cancel、technical failure 和 close failure。
4. 故障注入：PostgreSQL 瞬断、Redis 丢唤醒、对象存储 missing/checksum、视觉 Provider 503、设备 crash、Worker kill、Driver close failure。
5. 核对 Work/lease、ActionExecution、Capture/Artifact、Manifest、Visual Observation、Frontier、Evidence Index、Result、Session 的 ownership 与 hash。
6. 增加 App 探索专项指标：visual fact missing、closure failure、stale session、Frontier age、outbox lag、recovery outcome 和 evidence gap reason。
7. 更新 `docs/专项能力与高频回归规则.md`，记录本轮可复用的证据不变量、封存合同、Frontier 和恢复门禁。

验收：

- 含动作的正常任务能够得到 `exploration_completed`。
- 证据不足任务能够封存并关闭/标 LOST，原因可审计。
- 多分支 App 不丢安全业务候选。
- Worker/Redis/设备故障不产生重复动作或孤儿 Session。
- PostgreSQL 与对象存储中的身份、hash 和终态一致。

建议提交：`[AI编程] test(app): 完成页面探索部署级闭环验收`

数据库迁移：取决于前序批次。前端：无。复杂度：L～XL，取决于外部环境。

## 6. 每批提审与 Git 门禁

每批严格执行：

1. `git status --short`，保护现有 `.testagent/`、设计文档和其它用户未跟踪文件。
2. 只修改本批 App 探索责任文件；发现 H5、入口、规则质检或工作台问题只记录，不顺手修改。
3. 新增测试先确认能暴露问题，再实施修复并转绿。
4. 执行本批后端测试、相关 App 探索回归、Ruff 和 `git diff --check`。
5. 迁移批次必须检查 Alembic 单一 head，并在显式隔离 PostgreSQL 执行 upgrade、trigger、round-trip 和 downgrade guard；SQL 字符串测试不能替代真实数据库。
6. 使用显式文件列表暂存，检查：
   - `git diff --cached --name-status`
   - `git diff --cached --check`
   - `git diff --cached`
7. 修复具有复用价值的问题时，同批更新 `docs/专项能力与高频回归规则.md`。
8. 本地提交必须以 `[AI编程]` 开头，并明确写出：
   - 模块：App 页面探索；
   - 数据库迁移：有/无；
   - 前端菜单/路由：无变更；
   - 未覆盖风险。
9. 提交后再次确认工作区只剩用户原有文件和下一批未提交改动。
10. 未经用户明确要求不 push、不创建 PR。

## 7. 最终验收矩阵

| App 探索场景 | 预期终态 | 必须验证的事实 |
|---|---|---|
| 含 click/scroll/back 的正常多页面探索 | `exploration_completed` | before/after/final visual、全局 Frontier 排空、Index/Result sealed、Session CLOSED |
| Manifest FAIL/PENDING/missing | `evidence_insufficient` | gap 对应 Capture、Index 可封存、Result 落库、Session CLOSED/LOST |
| 视觉 Provider/Artifact 失败 | insufficient 或明确 technical failure | 不伪造 visual/stable/pass，错误码可审计 |
| 任务现有不同预算配置 | Runtime 按配置执行 | task record、Budget、Coverage summary 一致 |
| 多 sibling 业务分支 | 全部 visited/blocked/cancelled | 未选择候选不会因唯一 successor 丢失 |
| 运行中取消 | insufficient/canceled 语义一致 | 不再发动作、不重复 Final Observation、剩余 Frontier 终止 |
| Worker kill/Redis 丢唤醒 | 同一 Run 恢复或安全终结 | 无重复动作、租约唯一、PG 可审计 |
| 设备不可 reattach | `LOST` | 已有证据保留、无永久 ACTIVE Session |
| Closure/close failure | sealed result 或明确失败事实 | 不覆盖 sealed 证据、不遗留无原因 DRAINING |

## 8. 发布判断

- 批次 1、2 未完成：两个主链阻断仍存在，App 页面探索禁止灰度。
- 批次 1～5 完成但批次 6 未完成：只能称“App 页面探索仓库级闭环完成”。
- 批次 6 在真实环境通过：可以进入受控 App 页面探索灰度，但不代表 Rule Judge、报告或完整质检下游已经验收。

以下任一情况出现，立即阻断 App 页面探索发布：

- PASS Evidence Index Entry 缺可信视觉事实。
- `evidence_insufficient` 无法封存，或被覆盖成无原因技术失败。
- 全局 Frontier 仍有 pending/leased/retry Work 却产生 completed。
- Worker 恢复可能重复设备动作。
- Session 留在没有恢复事实的 ACTIVE/DRAINING。
- 任何结论只能由 fake/in-memory 测试证明，尚未在真实 PostgreSQL 和对象存储验证。
