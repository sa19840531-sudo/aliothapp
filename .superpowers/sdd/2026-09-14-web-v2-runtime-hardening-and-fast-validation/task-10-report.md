# Task 10：分层实况验收报告器

## 审查修复第 2 轮

仅修复 Important #3 的生产形态，待复审，无 production app、数据库迁移或前端变更。

- 生产 `WebStageCursor` 的 `last_error_detail` 可为空，阶段推进后仍保留错误；source 增读真实 `gaps/stage_generation`、Action 的 intent/幂等身份/命令序列/epoch/结果 Capture，以及菜单账本的 `menu_intent_id/menu_action_count`。
- 候选导航实际位于 `bootstrap_navigation`，`candidate_navigation` 是后续详情/调度，不能混淆。以最后失败 gap 的阶段与代次（重试等待使用当前阶段）和唯一账本 intent 关联 Action；错误码只作一致性检查，不作关联键。根入口还核验 `web-bootstrap:<run>`、`web-entry:<run>`、首条命令、入口标记、Capture 阶段与 epoch。无法唯一关联保留独立阻塞失败。
- 同一 checkpoint/Action 的失败只计一次，保留 logical_page_key、action_execution_id 及原动作/Artifact 审计。自身 Action 的 state_summary 或正式关联 after_action 结果 Capture 才能证明响应；任何自身正式事实有 HTTP 响应均不得宣称无响应。根入口无 Capture，但自身 Action 明确 reachable=false/status_code=None 时，生成 home root_failure，可解释终态外部失败。
- 新增 `test_web_v2_live_acceptance_failures.py`：使用生产领域游标和行序列化生成三次重试、空 detail、gap 后推进 session_close 的事实；TDD 首组 8 failed/11 passed → 19 passed，再补 5 项防误配/冲突响应守卫，最终 24 passed。覆盖相同错误不同 Action、多义 intent、缺账本、错误阶段/代次/入口身份以及无响应事实缺失，保持保守阻塞。

最终验证（全部串行）：精确 **74 passed**，preflight 选定子进程集合 **102 passed**，扩大 **139 passed / 1 skipped**；唯一跳过仍为缺少显式隔离 `ALIOTH_TEST_DATABASE_URL` 的原始 Artifact PG 测试。真实 Chrome 两项均执行，另逐项独立重跑分别 1 passed/exit 0（双 origin 2.49s；清障与长图 9.98s）。scoped Ruff、工作树/暂存 diffcheck、CLI 独立入口通过。实际部署 API/PG/Redis 与真实 SQL 执行仍留 Task12 显式环境，不以行投影代替数据库验证。

接管后的独立复验使用三个全新 `--basetemp`：精确集再次 **74 passed**，preflight 集再次 **102 passed**，扩大集再次 **139 passed / 1 skipped**；两项 Chrome 又逐项通过（2.71s、10.08s）。这些是本轮未提交差异上的新结果，不复用前一次缓存或只读报告。

本轮环境记录：默认受限环境首次精确为 72 passed/2 failed（exit 1），失败堆栈为本地 `Page.goto` 5000ms 超时及 `WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT`，并非 0x5AF；两个浏览器关闭挂起。只读验证 pytest→driver→Chrome 临时 profile 后，仅清理对应 LD3skP/PID 34948、qPWGmD/PID 18416 进程树，未操作用户浏览器。正常宿主权限独立 Chrome 重跑通过；随后默认 pytest 临时根 ACL 导致 16 项 setup errors（58 passed），改用三个事先确认不存在的专用 basetemp，得到上述最终通过结果，未更改旧临时根 ACL。失败与权限诊断均不计作通过，不修改 Chrome 测试或生产逻辑以掩盖问题。

两分钟面向用户的展示继续由 Codex 会话/heartbeat 承担，scripts 仅采集验收事实与本地证据。用户原未跟踪计划、设计及四个输出目录不变。

## 审查修复第 1 轮

审查四项均已修复，待复审：

1. report 每站最低见证/Artifact 引用成为无条件硬退出门，省略 `--verify-artifacts` 也执行。真实 `main(argv)` 入口覆盖缺最低见证、缺引用与完整成功，失败仍生成可检查报告并 exit 2。
2. source 读取真实 capture_purpose、父子 Capture、角色、epoch/command sequence、PNG content_type、Phase evidence_artifact_ids 和 pipeline_metrics.obstruction_chain。独立 `web_acceptance_evidence.py` 关联正式长图，拒绝仅 Phase 完成、视口用途、错误父级/epoch、非 PNG、缺失 sequence；输出 full_page_screenshot_artifact_id。popup 基于多层关闭且 stable/无剩余阻断的持久指标，不依赖不存在的页型。真实 Chrome 调用生产清障函数关闭两层标准对话框，再调用生产长页稳定/截图函数，指标和正式关系投影通过。
3. 每个失败独立取响应证据：checkpoint 使用 schema 允许的 last_error_detail.capture_id；Action 使用真实 result_payload.state_summary。首页成功+候选 TLS 无响应正确归外部传输；响应事实缺失为未知，已收到 HTTP 状态码不会误列外部无响应。root_failure 只选 home checkpoint。
4. 共享 TERMINAL 加入迁移约束中的 aborted，最低见证与 CLI 活动槽位同步正确；真实 main monitor 测试验证前站 aborted 后下一站获提交。

TDD：首组 11 failed / 6 passed → 17 passed；再补缺序列和缺响应字段两个边界，均先 RED 后修复。审查测试最终 21 项。仅验证脚本/测试/专项规则改动，生产 app 未修改。

本轮最终验证：精确集 50 passed / 0 skipped（含 2 项真实 Chrome）；preflight 选定子进程集合 102 passed / 0 skipped；扩大集 115 passed / 1 skipped / 0 failed。唯一跳过仍为 `test_real_pg_endpoint_exports_stored_original_checksum`，没有显式隔离 `ALIOTH_TEST_DATABASE_URL`。精确/preflight 各 2 项、扩大集 4 项既有第三方 warnings。修改 Python 的 Ruff、工作树 diffcheck 及独立 CLI `--help` 均通过。

资源事件：并行 preflight/扩大回归时宿主 Chrome 报 Windows 0x5AF 页面文件不足，产生 setup errors；扩大 pytest 的专属 Playwright driver 随后无 Chrome 子进程但未退出。只读核对 PID/命令行后仅终止该测试的 driver 43004，未操作用户浏览器。此失败没有当作代码通过；改为串行重跑。串行 preflight 102 passed/0 skipped，真实 Chrome 两项通过。

边界澄清：scripts 仅为 Codex 验收数据采集和本地产物，用户可见的每两分钟进度由 Codex 会话/heartbeat 负责，不添加平台生产功能。实际部署 API/PG/Redis 和真实存储读取仍依赖 Task12 显式环境，不能用投影测试代替真实数据库验证。

## 交付范围

- `scripts/validation/web_v2_live_acceptance.py`：preflight/start/monitor/close-complete/extend-incomplete/report。
- `web_acceptance_reporter.py`：白名单投影、版本游标、幂等增量写入、单站最低见证、合并能力矩阵。
- `web_acceptance_source.py`：显式凭据 HTTP 边界、只读 PG 快照、真实 Redis 心跳解析、当前冻结策略核验。
- 两个新增测试文件与专项规则。未修改生产 App/H5/Web 执行实现，没有数据库迁移与前端菜单/路由变动。

## RED → GREEN

1. Reporter 11 项缺少实现时明确断言失败，随后 11 passed。
2. Capture 置信度更新重复 Artifact、布尔 root_failure 伪证据、数据库事实映射缺失三项 RED，修复后 Reporter/Chrome 合计 16 passed。
3. Web Worker 角色过滤、非终态收口、checkpoint Artifact 引用三项 RED；真实 RedisQueue 构造缺少 TTL 与冻结策略核验两项 RED；增加对应实现后通过。
4. 本地证据路径不存在时原验证错误通过，新增测试观测 RED 后改为拒绝。
5. CLI 使用持久 manifest 测试 monitor/close/extend/report，检查无 cancel 请求、无凭据文件输出；start 测试预先持久幂等键和单 Worker 队列；preflight 真实入口测试只读事务与部署/本地合同分离。独立 `--help` 命令 exit 0。

## 受控真实 Chrome

两个随机 `.test` 域名、两个随机本地 HTTP 端口；Chrome DNS 映射为 loopback，真实 HTTP 302 导向外站。首次 route.fulfill 夹具在重定向后无法映射第二次请求而 5 秒超时，改为真实 DNS/HTTP 后通过。广告仅替换 DNS 与固定 socket 连接两个网络 I/O 边界；生产 AdSafeTransport、地址审核、路由拦截、独立 Context、禁脚本/业务 Cookie 隔离与底层 click 拒绝保持真实实现。跨域 Capture 在 anchor reader 前拒绝，来源重新 goto 后签发新 Capture 并成功发现新菜单。浏览器、两服务器及线程由 finally 回收；最终测试无跳过。

## CLI 约定与裁决

需要显式 `ALIOTH_LIVE_TOKEN` 或 `ALIOTH_LIVE_USERNAME` + `ALIOTH_LIVE_PASSWORD`，以及 `ALIOTH_LIVE_DATABASE_URL`；preflight/start/monitor 另需 `ALIOTH_LIVE_REDIS_URL`。没有默认登录账号/密码。站点只来自 `start --site`。

主 agent 裁决：不扩展生产提前成功收口 API。close-complete 只登记已自然收口站点，拒绝非终态 checkpoint；extend-incomplete 仅一次最长 30 分钟的本地观察延长，不修改生产 deadline。受控测试允许公共随机测试域映射到本地源，只替换传输 I/O。

每站生成 events.jsonl、report.md、site-summary.json、cursor.json；全局保存 validation-state.json、suite-capability-matrix.json，report 命令再生成 final-report.json/md。未满足矩阵时报告退出码 2，不伪造完成。外部 TLS 分类必须有无响应证据；无法证明的错误保守归入代码/证据待核实。

## 验证与限制

- 新增精确与受控 Chrome：28 passed / 0 skipped，包含 1 个真实双 origin Chrome 测试。
- preflight 选定子进程集合：101 passed / 0 skipped。首次 100 passed / 1 failed 是原测试固定 Catalog v9，与已发布 v10 冲突；主 agent 批准最小调整既有测试，对比 current_body.guide_version，重跑通过。
- 相关扩大回归：93 passed / 1 skipped / 0 failed。跳过为 `test_real_pg_endpoint_exports_stored_original_checksum`，缺少显式 `ALIOTH_TEST_DATABASE_URL`，原因“只允许显式隔离 PG”；4 项既有第三方 warnings。
- 额外覆盖 home checkpoint 引用证明 observation 类型首页 Capture，不要求底层 Capture 角色一定命名 bootstrap；已观测 RED 后修复。
- 真实 API/PG/Redis 部署连接和 Task12 三站实况未执行；PG 查询按现有 schema 核对并以只读事务封装，仍须 Task12 显式环境完成实际查询验证。
- preflight 本地代码合同通过不代表部署进程就是当前源码；真正 Run 的冻结策略在 collect 时重新验签物化并核对菜单上限。
- 未出现的弹窗/年龄门/结构簇保持缺口；未知原始网络错误不根据错误自由文本猜测 TLS。原始 Artifact 字节及校验和不下载，本次验证检查数据库归属与本地路径存在性。
