# H5 预期动作证据库存 Task 7 报告

## 实现结论

- H5 库存以当前 task/run 的 `qc_action_execution` 为预期集合，严格绑定 `h5:{agent_run_id}`、task/run/session/web/role/action identity，并 LEFT JOIN before/after；初始 NAVIGATE 和缺任一 side 的 Action 均不会丢失。
- 每项均先持久化 0049 库存事实，再仅向 full-inspection 的持久化 `pass` 项发布。`snapshot.status` 是汇总、`evidence_ready`、发布资格及 pass/insufficient/failed 计数的唯一权威；failed sticky 重放不会发布，也不会允许 sealed report 或 Completion Gate。
- 逐侧重读 screenshot/DOM 对象字节并校验 ownership、Capture identity、size、checksum。缺一侧时仍审计存在侧；存在侧任一完整性失败优先为 `failed/evidence_integrity_failed`，否则缺 Capture/制品为 `insufficient`。摘要只保存 side、身份、声明大小/校验和、校验结果和 reason，不保存正文。
- PostgreSQL 仓储把同一份 canonical、严格 JSON 文本用于 payload hash 和 `$3::jsonb` 绑定，避免依赖 asyncpg 默认 JSON codec。非字符串键、NaN、Infinity 和不可编码值 fail-closed；同 hash 不升版本，变更原子升版本，failed sticky，发布计数单调。
- CTE 并发首写无行时用新 statement 按 action/task/run/session 严格重读 winner，避免 PostgreSQL statement snapshot 窗口误报身份冲突。
- 发布器每次 prepare 都运行既有唯一约束/outbox 幂等路径，`items[*].created` 才计为本轮新 Work；成功后才 mark published。insufficient、failed、excluded active-session rules 都阻止正式收口，但已通过项事实保留。

## Fix round 2：RED → GREEN

- C1 RED：fake connection 证明仓储直接绑定 Python dict 到 `$3::jsonb`；GREEN：pass/insufficient/failed 都绑定 canonical JSON string，hash 与落库 payload 同源。新增 `test_h5_evidence_inventory_postgres_integration.py`，在隔离 `ALIOTH_TEST_DATABASE_URL` 中使用默认 asyncpg codec 对三态 `upsert_inventory_item` 做 round-trip。
- C2 RED：upsert 返回 historical sticky `failed` snapshot 时，handoff 仍按局部 pass 发布。GREEN：所有汇总、valid 集合、发布资格和 evidence_ready 均由 persisted snapshot 派生；`test_handoff_uses_persisted_failed_snapshot_as_the_only_publish_authority` 用两个新 service 实例重放，断言 failed_count=1、不发布且不 ready。Worker 的 nonpass 门禁测试确认不 seal、不进入 Completion Gate。
- I1 RED：missing-before/after 时结构校验提前短路，存在侧制品未重读。GREEN：缺 side 仅记录该侧 side-qualified 缺失事实，存在侧仍逐制品调用 verifier；三组测试覆盖 missing-after + before 完整、missing-after + before 损坏、missing-before + after 损坏，且断言不存在侧不读取伪对象。

## Fix round 3：RED → GREEN

- A RED：`evidence_ready=False` 但计数全为零时，Worker 仅依赖 insufficient/failed/excluded 计数，仍会封存正式报告并进入 Completion Gate。GREEN：`formal_closure_blocked` 首先检查 `not evidence_ready`；空且未就绪库存被投影为 `needs_review/incomplete`，`test_empty_not_ready_inventory_blocks_formal_report_and_gate` 断言不注入 finalization、不启用 legacy sealed report。正常 pass 路径继续覆盖正式收口。
- B RED：expected Action 的 after capture 生命周期 `superseded/abandoned` 在 SQL `WHERE` 中过滤，导致库存集合遗漏动作。GREEN：before/after 各自的生命周期条件移入严格 identity 的 `LEFT JOIN qc_capture ... ON`，以 `NOT EXISTS qc_page_capture` 使不合格 side 投影为 NULL；Action 仍由 `qc_action_execution` 驱动保留，后续作为缺侧审计。`test_postgres_reader_keeps_action_when_after_capture_is_superseded` 断言动作和 before 身份仍返回、生命周期谓词不再出现在 WHERE。既有两个 service sticky-failed 重放测试和 Worker nonpass 门禁共同覆盖 failed 不发布、不封存、不进入 Completion Gate。

## 验证

- Task7 矩阵：95 passed、1 skipped、2 warnings。
- Task3–6 H5 回归：29 passed、2 warnings。
- Ruff、`compileall`、`git diff --check`：通过。

## 环境风险

- 当前未设置 `ALIOTH_TEST_DATABASE_URL`，真实 asyncpg round-trip 集成测试安全跳过；fake 合同测试不替代真实 PostgreSQL 验收。
- 未新建平行对象存储，生产继续复用既有 local/MinIO read adapter；真实 MinIO、PostgreSQL 并发和浏览器 E2E 仍需在隔离环境执行。
