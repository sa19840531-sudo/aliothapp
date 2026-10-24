# H5 页面探索审计整改最终复审

复审日期：2026-08-13
原始审计：`C:/Users/40389/Downloads/Telegram Desktop/2026-08-12-h5-app-page-exploration-audit-report.html`
原始审计基线：`dev_1.5@3e0b470b9776f186926b88e2fd139803b9f53da6`
整改计划基线：`5950b70`；实施 ledger 起点：`2ac4346`；代码复审截止：`a445f23`
范围：仅 H5 与直接影响 H5 的共享控制面；不包含原报告的 App 包名、视觉观察、最终观察和 App 入口整改。

## 1. 复审结论

原报告 H5 4.2、4.3 以及共享控制面中直接影响 H5 的 14 项问题均有当前代码与自动化测试证据，代码级结论均为“已闭环”。整改将 H5 从单后继、内存评估和不完整证据交接，升级为版本化任务 Frontier、锁定运行预算、协作式取消、有界浏览器恢复、Assessment/Inventory 长期事实、正式工作台投影及 H5-only 收口门禁。

本结论不等于生产放量验收。真实 PostgreSQL、Redis、MinIO 与 Playwright 组合环境未提供，故最终发布判断为：**代码复审通过；仅允许继续集成或受控灰度准备，生产放量仍须通过第 5 节全部环境门禁。**

## 2. 附件问题到代码证据矩阵

| 审计项 | 原问题 | 代码证据 | 测试证据 | 结论 | 环境门禁 |
|---|---|---|---|---|---|
| H5-P1-01 | `goto` / `navigate` / `destination_url` 合同断裂 | `services/api/app/agents/tools/page_detection_tools.py:825, 847, 861, 2636-2666`；`services/api/app/application/h5_exploration_observation.py:210-261` | `services/api/tests/test_page_detection_tools.py:46-52`；`services/api/tests/test_h5_exploration_observation.py:87-164` | 已闭环 | 真实 Playwright 同源导航、跨源/越界拒绝待环境执行 |
| H5-P1-02 | 单后继导致父页 sibling 分支丢失 | `services/api/app/domain/exploration/h5_frontier.py:60-404, 603`；`services/api/app/application/h5_exploration_workflow.py:852-876` | `services/api/tests/test_h5_task_frontier.py:53-319`；`services/api/tests/test_h5_exploration_workflow.py:577-671` | 已闭环 | 根页/二级页多分支真实站点耗尽待 Playwright 执行 |
| H5-P1-03 | 提交预算与抽样设置未被 Runtime 消费 | `services/api/app/application/h5_task_runtime_settings.py:18-71, 91-101`；`services/api/app/application/h5_runtime_composition.py:1313-1400, 1602-1605` | `services/api/tests/test_h5_task_runtime_settings.py:25-84`；`services/api/tests/test_h5_runtime_composition.py:143-195` | 已闭环 | quick/full/custom 长时任务、真实 deadline 与页面上限待环境执行 |
| H5-P1-04 | 运行中取消不生效且完成态可覆盖取消 | `services/api/app/application/h5_exploration_cancellation.py:8-94`；`services/api/app/application/h5_runtime_composition.py:1196, 1232-1259`；`services/api/app/application/h5_exploration_turn.py:160-214, 411-416, 592-630, 729-740` | `services/api/tests/test_explorer_turn.py:151-173`；`services/api/tests/test_h5_exploration_cancellation.py:23-58`；`services/api/tests/test_h5_exploration_recovery.py:128-142`；`services/api/tests/test_h5_exploration_turn.py:340-360`；`services/api/tests/test_h5_runtime_composition.py:1344-1493`；`services/api/tests/test_worker_h5_v2_finalization.py:374-445` | 已闭环 | Redis/PG 多连接取消与完成竞争注入待环境执行 |
| H5-P1-05 | H5 V2 Work/Capture 事实无法由工作台正式回读 | `services/api/app/infrastructure/db/task_insight_repository.py:462-744`；`services/api/app/application/task_insight.py:123-209, 332` | `services/api/tests/test_task_insight_repository.py:302-410`；`services/api/tests/test_task_insight_service.py:530-617` | 已闭环 | 大任务 SQL 计划、索引命中与延迟待真实 PG 执行 |
| H5-P1-06 | 证据查询内连接丢项且单项不足拖垮整批 | `services/api/app/infrastructure/db/h5_evidence_handoff_repository.py:55-132`；`services/api/app/application/h5_evidence_handoff.py:145-277, 344-451` | `services/api/tests/test_h5_evidence_handoff.py:168-181, 463-534, 576-700`；`services/api/tests/test_h5_evidence_inventory.py:24-125` | 已闭环 | 真实 PG 库存与对象存储混合 pass/insufficient/failed 批次待执行 |
| H5-P1-07 | H5-only 未执行 Completion Gate 启动校验 | `services/api/app/worker.py:2123-2149, 2419-2449` | `services/api/tests/test_task_completion_gate.py:30-62` | 已闭环 | H5 on/App off 容器在缺项与完整配置下的真实启动待执行 |
| H5-P2-01 | PageTypeAssessment 仅存在于内存 | `services/api/migrations/versions/20260812_0049_h5_exploration_audit_remediation.py:46-87, 153-200`；`services/api/app/infrastructure/db/h5_page_type_assessment_repository.py:45-257` | `services/api/tests/test_h5_exploration_audit_remediation_migration.py:38-77`；`services/api/tests/test_h5_exploration_workflow.py:193-475` | 已闭环 | 真实 PostgreSQL upgrade/downgrade、约束与 Repository round-trip 待执行 |
| H5-P2-02 | ACTION / AFTER_CAPTURE 浏览器崩溃无恢复 | `services/api/app/application/h5_exploration_recovery.py:45-168, 688-845, 932-945` | `services/api/tests/test_h5_exploration_recovery.py:99-231, 336-414` | 已闭环 | Playwright 进程崩溃、历史栈恢复、跨 Worker reclaim 待故障注入 |
| H5-P2-03 | 交接未重读对象字节并校验 checksum | `services/api/app/infrastructure/storage/h5_evidence_object_verifier.py:22-63`；`services/api/app/application/h5_evidence_handoff.py:317-362`；`services/api/app/worker.py:1897-1906` | `services/api/tests/test_h5_evidence_inventory.py:24-125`；`services/api/tests/test_h5_evidence_handoff.py:463-534` | 已闭环 | MinIO 真实 bytes 重读、丢对象、size/checksum 篡改待注入 |
| H5-SHARED-01 | H5 Explorer/Probe 写无人消费的唤醒事件 | `services/api/app/infrastructure/db/explorer_work_repository.py:82-248, 499-609, 794-825`；`services/api/app/application/h5_runtime_composition.py:1213-1259` | `services/api/tests/test_v2_control_plane_repositories.py:391-502, 939-965`；`services/api/tests/test_h5_runtime_composition.py:291-293`；`services/api/tests/test_app_runtime_composition.py:125-126` | 已闭环 | 真实 PG Outbox + Redis relay 与 App/H5 混合 Worker 部署待执行 |
| H5-SHARED-02 | exploration-only 报告状态与入口矛盾 | `services/api/app/application/workstation_tasks.py:339-350`；`app/workstation/src/modules/tasks/pages/TaskDetailView.vue:230-238` | `services/api/tests/test_workstation_tasks_api.py:238-250`；`app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts:294, 358-406` | 已闭环 | 浏览器首屏、Timeline 延迟/失败与不同分辨率视觉检查待执行 |
| H5-SHARED-03 | 平台识别未使用正式 `target_platform` | `services/api/app/infrastructure/db/task_insight_repository.py:42-80`；`services/api/app/application/task_insight.py:123-157, 425-435, 1215-1234`；`services/api/app/infrastructure/db/workstation_query_repository.py:300-302, 420-431` | `services/api/tests/test_task_insight_service.py:530-617, 1002-1013`；`services/api/tests/test_workstation_query_repository.py:74-115, 118-164`；`services/api/tests/test_workstation_tasks_api.py:130-188` | 已闭环 | 历史 `target_platform IS NULL` 数据抽查与正式值优先级待真实 PG 验证 |
| H5-SHARED-04 | Inspection metadata 写读 schema 不一致 | `services/api/app/application/app_inspection_publisher.py:82-117, 148-173, 341-367`；`services/api/app/infrastructure/db/task_insight_repository.py:368-397, 608-622` | `services/api/tests/test_task_insight_repository.py:302-350, 413-510` | 已闭环 | 历史 nested/flat metadata 与正式列混合数据待真实 PG 抽查 |

## 3. 自动化验证记录

### 3.1 后端定向回归

执行 Task 12 brief 列出的 21 个 H5/共享后端测试文件：

```text
528 passed, 3 warnings in 67.82s
```

退出码为 0，无失败、无跳过。3 条警告为 Agent Framework experimental 警告 2 条及 Starlette/httpx 弃用警告 1 条。

### 3.2 后端全量与迁移链

首次全量执行暴露部署文档仍声明旧 head：

```text
3148 passed, 23 skipped, 1 failed, 30 warnings
FAILED services/api/tests/test_production_deployment_files.py::test_deployment_guide_uses_current_single_migration_head
```

根因是 `docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md:849` 仍写 `20260812_0048 (head)`；同步为 `20260812_0049 (head)` 后，原失败节点定点复跑为 `1 passed`。随后重新执行完整 API 测试：

```text
3149 passed, 23 skipped, 30 warnings in 253.08s
```

最终 full-suite 退出码为 0。23 个 skipped 保持为既有跳过项，未计入通过数；其中真实依赖集成验证在第 5 节单独列为未执行门禁。30 条 warning 为既有第三方 experimental/deprecation 与测试 JWT 密钥长度警告，没有测试失败。

其他门禁：

```text
python -m compileall -q services/api/app       -> exit 0
python -m alembic -c services/api/migrations/alembic.ini heads
20260812_0049 (head)
```

### 3.3 前端

```text
pnpm --dir app/workstation test
Test Files 10 passed (10); Tests 35 passed (35)

pnpm --dir app/workstation exec vue-tsc --noEmit -p tsconfig.app.json
exit 0

pnpm --dir app/workstation build
4194 modules transformed; built in 3.49s; exit 0
```

### 3.4 静态合同与差异卫生

- `rg -n '"goto"|action\s*=\s*"goto"' ...`：4 处生产命中，均位于具名历史兼容规范化或兼容候选接纳逻辑；没有新写入路径产出 `goto`。
- `rg -n '_DEFAULT_MAX_ACTIONS|_DEFAULT_MAX_DEPTH|600_000|900' services/api/app/application/h5_runtime_composition.py`：0 命中，确认旧 H5 私有硬编码预算已移除。
- `git diff --check`：退出码 0，无空白错误。
- Alembic 仅有 `20260812_0049` 一个 head；部署指南同步使用当前 head。

## 4. 变更范围与 App 非回归

`5950b70` 是本整改计划之前的有效祖先；`2ac4346` 是 ledger 记录的实施起点，二者之间仅新增实施计划。`git merge-base 5950b70 HEAD` 返回 `5950b70`，`git merge-base 2ac4346 HEAD` 返回 `2ac4346`。

对 `git diff --name-only 5950b70..HEAD` 逐项核查：

- 生产变更集中在 H5 导航、Runtime、Frontier、取消、恢复、Assessment、Inventory、Handoff、Task Insight、Workstation 与 Completion/Outbox 的 H5 分支。
- 共享扩展点均保持默认行为：`emit_wakeup` 默认 `True`，App Explorer/Probe 继续产生唤醒；取消和恢复钩子为 H5 组合根显式注入；正式平台字段优先且只对 NULL 历史数据兜底。
- 未修改 App 包名边界、APK/设备、App Visual Observation、App Final Observation 或 App-only 入口实现；未新增或修改前端菜单与路由。
- `app_inspection_publisher.py` 的共享修改只统一 H5 正式列与兼容 metadata，并保留 App 无 `qc_page_capture` 子类型的既有兼容路径。
- 全量前后端自动化用于证明默认共享路径未出现代码级回归；真实混合部署仍列为环境门禁。

## 5. 生产环境验收门禁

以下门禁本次均为**未执行**，不得把代码级“已闭环”表述为生产验收通过：

| 环境门禁 | 最低验收内容 | 当前状态 |
|---|---|---|
| PostgreSQL migration | 隔离库 upgrade 到 0049；Assessment/Inventory 约束、索引、Repository round-trip；有事实时 downgrade 拒绝、清空后 downgrade | 未执行 |
| Redis 取消竞态 | decision 后、动作边界、successor 前与 Completion 竞争；取消胜出后无新动作且 completed 不覆盖 canceled | 未执行 |
| MinIO 对象完整性 | 重读真实 bytes，验证 task/run ownership、size、SHA-256；缺对象和篡改分别落 insufficient/failed | 未执行 |
| Playwright 多分支/恢复 | 同源 navigate、多 sibling 耗尽、BACK/同源导航恢复、ACTION/AFTER crash 零重放、rebaseline 与恢复耗尽 | 未执行 |
| H5-only Completion 启动 | App off + H5 on，在 Noop/缺 URL/缺 Service 下启动失败；PostgreSQL + Service 完整时启动并收口 | 未执行 |

## 6. 提交与任务追踪

实施主线从 `2ac4346` 到 `a445f23` 共 34 个提交。各任务审查通过后的截止提交：Task 1 `b14322b`、Task 2 `600815c`、Task 3 `299a3a7`、Task 4 `6490cd9`、Task 5 `9b9676f`、Task 6 `b2e4ac2`、Task 7 `56baa5a`、Task 8 `923a7df`、Task 9 `85016b1`、Task 10 `b855eee`、Task 11 `a445f23`。各轮 RED/GREEN、独立复审与遗留环境门禁记录在 `.superpowers/sdd/2026-08-12-h5-page-exploration-audit-remediation/progress.md` 及对应 Task 报告。

Task 12 文档变更包括：本复审报告、`docs/专项能力与高频回归规则.md` 的十项 H5 耐久回归清单，以及全量测试暴露后同步的部署指南 Alembic head。Task 12 自身提交 ID 以最终 Git 历史为准。

## 7. 最终判断

- 原附件中的 H5 P1：7/7 代码已闭环。
- 原附件中的 H5 P2：3/3 代码已闭环。
- 直接影响 H5 的共享项：4/4 代码已闭环。
- App 专属问题：按用户授权明确不处理。
- 生产放量：在第 5 节门禁全部通过前保持暂缓；当前不宣称真实环境或生产闭环。

## 8. 2026-08-14 独立全分支终审补强

在 Task 12 首轮 CLEAN 之后，又以 `f9696c6` 为基线对附件、设计、迁移、生产调用链和工作台消费做了一次独立全分支审查。该审查未发现 Critical，但重新打开 11 个 Important；本轮均以最小生产修复和杀旧行为测试收口：

| 终审问题 | 修复结论 | 主要实现与回归 |
|---|---|---|
| 深层叶页无法由任务级 Frontier 耗尽形成完成 | 已修复 | `h5_exploration_workflow.py` 只在无 selected/pending/blocked/failed/auth 时提升完成；workflow 正向叶页及四类负例回归 |
| Assessment 跨 schema 覆写历史 | 已修复 | 0049 与 Repository 改为 `(page_capture_id, schema_version)`；同 Capture 多版本共存，读侧选最强且最新有效投影 |
| sibling 来源恢复的 Assessment 仅存在内存 | 已修复 | Frontier recovery 显式注入正式 Assessment Repository；upsert 先于 recovered Work，写入失败阻断恢复 |
| Redis 记录/key 丢失后已接受取消不可见 | 已修复 | PostgreSQL marker 为长期事实，App/H5 检查点读取 Redis OR PG，长期来源异常失败关闭 |
| App、H5 V1/Shadow 与早退终态竞争可遗留 RUNNING | 已修复 | 所有通用 finish site 统一进入 cancellation-aware finalizer；启动失败和 Agent 终态同样受保护 |
| orphan 取消先改 Redis、对账又只读 Redis | 已修复 | TaskSubmission 先持久化 marker；reconciler 同时读取长期事实并一次收敛 canceled |
| exploration-only rerun/copy 丢失锁定配置 | 已修复 | Workstation 详情正式读取并复制 `execution_mode + workflow_version` |
| 正常取消被读模型/UI 显示为技术失败 | 已修复 | 后端投影 `needs_review + work_cancelled`，前端显示“页面探索已取消/已取消”，取消原因优先于历史失败库存 |
| H5 V1 被误分流到 V2 读模型 | 已修复 | 仅正式 H5/Web 且 `workflow_version=v2` 使用 V2；V1/缺失版本继续 legacy |
| 恢复耗尽被记为 infrastructure ERROR | 已修复 | 三类耗尽统一落 `bounded` 并映射 `INCOMPLETE/needs_review`，unknown Action 保持零重放 |
| PostgreSQL 已接受但 Redis 未命中的取消 API 语义不稳定 | 已修复 | 正式 Store 以 durable acceptance 为准；Noop Store 保持历史 Redis 兼容且不误报不存在/已终态 Run |

本轮进一步自审又补出两项同根边界：Worker 启动状态写入失败时必须先读取长期取消再决定是否重新入队；任何 Workflow 的 `CANCELED` 记录在统一收口层都必须剥离报告、Inspection 与 Completion Gate。两项均先记录失败测试，再完成 GREEN。

### 8.1 阶段验证

- 全部受影响后端测试：`358 passed, 1 skipped, 3 warnings in 109.17s`；跳过项为未提供 `ALIOTH_TEST_DATABASE_URL` 的真实 PostgreSQL 集成用例。
- Frontier 新增正反例明确覆盖：深层叶页可完成；pending sibling、blocked、failed、requires_auth 均不得完成。
- 取消新增回归覆盖：Redis false + PG true、App/H5 检查点、启动失败、V1/Shadow/通用收口、orphan 对账、Completion Gate 禁入和 Noop 兼容。
- 最终 full API suite：`3168 passed, 23 skipped, 30 warnings in 289.35s`，退出码 0；30 条 warning 仍为既有第三方 experimental/deprecation 与测试 JWT 密钥长度提示。
- 前端 fresh 门禁：Vitest `10 files / 36 tests`、`vue-tsc` exit 0、Vite 生产构建 `4194 modules`，exit 0。
- 编译/静态/迁移门禁：compileall、Ruff check/format-check、`git diff --check` 均通过；Alembic 唯一 `20260812_0049 (head)`；0049 合同 `2 passed`。
- 第 5 节真实 PostgreSQL/Redis/MinIO/Playwright/H5-only 启动环境门禁保持未执行，代码级全绿不等同生产放量验收。

### 8.2 迁移说明

本轮没有新增 Alembic revision，而是在尚未发布的 0049 中修正 Assessment 复合唯一约束。若任何外部数据库已经执行旧版 0049，不得直接依赖同 revision 重放，必须先按部署变更流程创建后续迁移；本次交付仍以隔离库 fresh upgrade 到 0049 作为生产前门禁。

## 8.3 取消耐久竞态与跨 sibling 认证历史复审

在 8.1 的全分支审计后，又以故障注入和真实调用顺序复核了取消收口与 Frontier 历史，关闭以下 7 个 Important：

| 复审问题 | 最终修复 |
|---|---|
| 取消到达时仍可能继续 Inspection、规则、报告或 Completion Gate | App/H5 V2 在 publish、handoff、drain、rules、formal inspection 与 finalization 的相邻边界逐步读取长期事实；命中后立即走无报告、无 Gate 的统一终态 |
| 非终态 Work 清理异常阻断 CANCELED | 先保存 `CANCELED + needs_review` 短期投影，再尽力清理；失败保留 `cancellation_cleanup_completed=false`，对账器幂等重试，取消终态不回滚 |
| CANCELED 写入覆盖 `cancel_requested` | PostgreSQL 输出上下文保留 marker，长期检查同时采信 `status=canceled`，finished scan 明确排除 canceled |
| orphan durable cancel 未清理 H5 Exploration/Probe | CANCELED 对账统一调用平台限域清理；H5 仅取消 Web Exploration/Probe，App 保持原范围 |
| `record_started` 与取消终态写入连续故障后 Run 卡在 RUNNING | Redis 先稳定投影 CANCELED 并解除 owner，长期写入失败保留未完成标志，恢复后的 reconciler 继续终态写入而不重放任务 |
| queued 取消在 Redis 已终态后 PostgreSQL 短断无法收敛 | 已取消但未 finalized 的 Redis 记录允许取消 API 幂等重试长期终态；queued/orphan 成功后记录 finalized，失败时继续保留可恢复状态 |
| auth 叶页切换 sibling 后被遗忘并错误完成 | `requires_auth/login_required` 落点把对应 Frontier entry 持久化为 `BLOCKED(authentication_required)`；后续 sibling 的普通页面不得清除历史阻断或提升 objective |

本轮 RED 均先复现旧行为：App/H5 正式收口中途取消仍调用后续步骤、清理异常向外冒泡、CANCELED marker 丢失、启动/终态连续故障无法收敛、queued 二次收口失败，以及 auth→sibling→普通叶页错误完成。对应 GREEN 后的最终门禁：

- 最终受影响聚合：`143 passed, 3 warnings in 59.17s`。
- 全量 API 基线：`3180 passed, 23 skipped, 30 warnings in 255.74s`；其运行期间发现并修正最后一个 App 清理先后顺序，修正后单点 `1 passed`，并以上述最终 143 项再次覆盖全部受影响路径。
- Workstation：`10 files / 36 tests`；`vue-tsc --noEmit` 与 Vite build 均为 exit 0，构建转换 `4194 modules`。
- 本分支全部变更 Python 文件 Ruff 通过；`compileall`、`git diff --check` 均为 exit 0；Alembic 唯一 head 为 `20260812_0049`。
结论仍遵守第 5 节环境门禁：代码级审计闭环不等同于真实 PostgreSQL/Redis/Playwright/MinIO 混合故障验收。

## 8.4 PostgreSQL 取消恢复与终态 CAS 最终复审

最终冻结复审仅收口两项，不扩展功能范围：

- PostgreSQL 的取消恢复扫描以 `cancel_requested/status=canceled` 为长期游标，并持久化 `cancellation_cleanup_completed/cancellation_finalized`。即使 Redis 记录过期，`cleanup=false/finalized=true` 的部分状态也会在清理成功后通过同终态 CANCELED 回写为 `true/true`，下一轮扫描不再重复清理。
- Run 终态写入只允许非终态进入终态或同终态幂等刷新。`UPDATE 0` 后会读取权威 Run marker/status 与 Task 状态：只有真实取消事实才抛取消冲突；已有不同终态则抛 `AgentRunTerminalStateConflict`，不得把 Redis 误投影为 CANCELED。

本轮两项旧行为先得到 `2 failed / 1 passed`，最小修复后为 `3 passed`；最终受影响后端集合为 `186 passed, 3 warnings in 57.96s`。冻结只读复审结论为 **CLEAN（0 Critical / 0 Important）**。Ruff、`compileall` 与 `git diff --check` 继续通过。真实 PostgreSQL 多连接竞争、Redis TTL/驱逐和 Playwright/MinIO 混合故障仍属于第 5 节部署环境门禁。
