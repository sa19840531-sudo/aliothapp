# H5 浏览器丢失恢复 Task 8 报告

## 实现结论

- 新增 `H5FailureStage`、不可变 `H5RecoveryContext/H5RecoveryOutcome` 与 `H5ExplorationRecovery.recover`。BEFORE_CAPTURE 只发布一个原子 retry Work；ACTION/AFTER_CAPTURE 保留动作事实、终结 unknown ledger、写 0049 insufficient 库存并从持久化 Frontier 继续，原 Action 重放次数为零。
- 浏览器丢失先把旧 Session 永久标记 LOST 并关闭残余句柄；恢复使用严格内容派生的新 Session `h5:{run}:recovery:{20hex}`。成功 rebaseline 后新 Session ACTIVE，最终由 lineage tracker 关闭；失败/取消的新 Session 标 LOST，旧行不清除 `lost_at/failure_reason`。
- Restoration、State Equivalence 和 recovery attempt 均为 frozen/versioned typed facts，使用 canonical JSON SHA-256 与 Work row_version 作为持久化权威。attempt 绑定 task/run/source session/failed Work/source state/frontier snapshot/deadline/last-safe-location/预算/interaction；失败事实进入不可变、连续的 history，跨 Worker reclaim 不会先执行 Explorer。
- 同源恢复使用 scheme、lowercase hostname 与 effective port；忽略 fragment，拒绝 userinfo/非法端口。State Equivalence 同时要求规范化实际 location 与 last-safe location 一致及 state_version 一致。
- recovery initial NAVIGATE 保留 CaptureCoordinator、Action ledger 和双采链。只有严格 marker、purpose、LOW risk、HTTP(S) destination、exact parameters、intent/work/stage/attempt/session family 全部闭合时，才绕过已 LOST source 的 Evidence Exit Gate；ActionExecution PostgreSQL 使用同一 helper 并校验 source capture/run/session owner。
- Work queue 默认 parent target 仍为 SUCCEEDED；SUPERSEDED 只能由同事务 successor commit 产生，并强制成功 typed recovery facts、failed parent、source/target Session 和 rebaseline after Capture 完整匹配。普通 App/既有调用默认行为不变。

## RED → GREEN 证据

- 初始 RED：recovery domain API 导入失败；queue/repository atomic supersede 3 项失败；runner BEFORE/unknown replay 与 composition lifecycle/inventory 共 5 项失败。
- 收紧所有 SUPERSEDED 必须携带完整 facts 后：5 failed / 45 passed；升级 fake 为真实 typed recovery identity 后通过。
- typed attempt/work/hash、严格 recovery action、origin/equivalence、unknown ledger、失败 history 与 N→N+1 分别先以缺字段、错误授权或断言失败确认旧实现无法通过，再完成 GREEN。
- 当前 Task8 focused（含 recovery/turn/runtime/evidence gate/handoff/action repo/session gate/work repo）除一次 Windows `--basetemp` 父目录被测试清理造成 setup error 外，拆分重跑均通过；最终总数见交付验证。

## 直接依赖扩展与理由

- `exploration_work_queue.py` / `explorer_work_repository.py`：需要在同一 lease、row_version、事务中原子 SUPERSEDE + publish，并在 PG 回读时拒绝篡改 facts/attempt/history。
- `action_execution_repository.py` / `evidence_exit_gate.py` / `capture_coordinator.py`：新 Session 导航必须继续走 action/capture ledger，同时旧 LOST Session 不能通过普通 evidence-exit/current-capture 约束；因此增加窄授权恢复入口和 unknown ledger 终结。
- `session_command_gate.py`：共享 Web gate 必须沿用已登记的 canonical/recovery owner，避免真实 PG `register_active` owner 冲突。
- `h5_evidence_handoff.py` / repository：Task7 reader 必须接受严格 recovery Session family，同时继续拒绝宽松 prefix、错误 owner/task/run。
- `surface_sessions.py` / repository 未保留 same-row resurrection；工作树显示为 line-ending/index metadata，blob 与 index 相同。

以上均不新增数据库迁移；正式事实存放在既有版本化 Work JSONB、0049 inventory 与 Action/Session 表中。前端菜单和路由无需同步。

## 风险

- 本地 Fake PostgreSQL 合同覆盖 SQL 参数、owner、状态与事务顺序，但未在本轮连接真实 PostgreSQL 执行恢复故障注入。
- 未运行真实 Playwright browser process crash、连接中断和跨 Worker 多进程 E2E；这些仍是部署验收项。
- Action ledger、0049 inventory 与 Work successor 分属多个事务；生产顺序为 `source LOST → ledger terminal → inventory → reopen`，任一步失败均不 reopen/不发布，重放依赖各自幂等键补齐。该边界无法在无迁移前提下合并为单个数据库事务。

## 最终验证

- Task8 focused：162 passed、2 个依赖实验 API warning。
- Task3–7 H5 回归：87 passed、2 个依赖实验 API warning。
- Session command gate 隔离回归：6 passed、2 个依赖实验 API warning。
- Ruff 首轮发现并修复 6 个 import/unused 问题；复跑 `All checks passed!`。
- `python -m compileall -q services/api/app/application services/api/app/infrastructure/db services/api/tests`：通过。
- `git diff --check`：通过，仅输出 Git 的 LF→CRLF 工作树提示。
- 工作树本地 `task8-*` pytest 临时目录已清理。

## Fix round 1/5（2026-08-13）

- 成功恢复 successor 将父 Work 已完成 attempt 链转入不可变 `h5_browser_recovery_parent_attempt_audit`，并删除仍绑定父 Work 的 active/history。生产链测试覆盖 reserve attempt 1 失败 → reserve attempt 2 → prepare recovery → PostgreSQL `commit_successor` 序列化 → repository `_parse_work` 回读。
- 非法 IPv6、非法端口和 userinfo 使 trusted recovery identity 返回 `None`，Evidence Exit Gate 保持生效，interaction executor 不会执行。
- reopen 后 assessment、state-equivalence、recovery-work preparation 异常均把目标 Session 标记 LOST；N+1 使用新 Session，不复活 N。
- repository parse/claim 强制 history 从 attempt 1 genesis 开始、同 stage 严格连续，BEFORE_CAPTURE 仅1次，ACTION/AFTER_CAPTURE 最多2次。
- AFTER_CAPTURE 在 source LOST 且身份完整时，可把 `evidence_pending` 幂等终结为 `evidence_failed / h5_browser_loss_after_capture_unknown`；ACTION 仍只允许 `executing`。

RED：非法 identity 3 failed；AFTER_CAPTURE ledger 1 failed；successor/post-reopen lifecycle 4 failed；history genesis/bounds 2 failed。GREEN：Task8 focused `145 passed, 2 warnings`；Task3–7 `106 passed, 2 warnings`；Ruff `All checks passed!`；compileall exit 0。warning 均为 Agent Framework 第三方 experimental warning。本轮无数据库迁移、无前端变更；真实 PostgreSQL + Playwright 崩溃注入仍为部署验收风险。

## Fix round 2/5（2026-08-13）

迁移 0029 要求 `evidence_failed` 的 `evidence_finished_at IS NOT NULL`。RED 使用 `evidence_pending + evidence_finished_at=None` 行，在领域回读时失败，证明旧 fake 预填时间掩盖了真实 PostgreSQL CHECK 冲突。UPDATE 现在仅对 AFTER_CAPTURE 写 `evidence_finished_at=now()`，ACTION 保留原值，幂等 fallback 仍为只读。RED `2 failed, 2 passed`；GREEN 直接 repository `4 passed`，Task8 最小生产链 `71 passed`。无迁移、无前端变更。
