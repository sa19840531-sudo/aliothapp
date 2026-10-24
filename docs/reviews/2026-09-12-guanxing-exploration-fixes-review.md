# 2026-09-12 guanxing 探索问题修复与分批审查

## 范围与基线

依据 `docs/2026-09-11-探索优化方案完成度与偏离审查报告.md`，按原始实现、后续修复和共享接线区分责任。开始于 `a69a5036`，先快进同步至 `69a4e50c`；本轮修复以 `69a4e50c` 为比较基线。工作区原有 `.gitignore` 改动及未跟踪文档保留，不纳入提交。

| 报告项 | 基线实际状态 | 本轮处理 |
| --- | --- | --- |
| P0-01 App 适用性未闭合仍可完成 | `7fe67dbe` 已阻断 unresolved applicability；已知适用但未覆盖的页型仍可能完成 | 终态、覆盖摘要和真实 Gate SQL 同时检查已知未覆盖集合 |
| P1-06 App Command Broker | `7fe67dbe` 已让主交互调用 Broker，但适配器未实际复核授权，正式恢复仍有直调路径，失败回执会丢失 | 补齐正式 Driver 适配器、恢复接线和失败回执 |
| P1-07 H5 Coverage 与报告 | 报告未冻结 Coverage 身份，洞察允许旧 Epoch 回退 | 分两批修复 Gate 权威身份、洞察精确读取及报告事务绑定 |
| P1-09 Web/H5 生产补采执行端 | 原报告明确判定 guanxing 的失败关闭策略正确；专项规则“补采接上后重新封存输入集合”明确执行端归天枢、青云客 | 保留 `enabled + runtime_ready` 边界，不将关闸本身当作缺陷，也不声明生产补采完成 |
| P2-12 Finalization 测试合同 | 已由 `5fad00dc` 修复 | 当前主工作区重新验证 48 项单测和 14 项 PostgreSQL 恢复回归，不重复提交已有修复 |

其他报告项仍属于原报告各自责任范围，本轮不将整份探索优化方案标为完成。

## 第一批：Gate 冻结 H5 Coverage 身份

提交：`c5256e9b`。

- Gate 保存实际消费的 Coverage id、hash 和来源 Epoch，三项必须完整出现，并进入 Gate 事实哈希。
- 新迁移 `20260912_0110` 添加三个可空字段、任务范围外键及完整性约束。历史 Gate 不推测回填。
- 当前 Gate 有绑定时按其精确身份读取；没有 Gate 绑定时只读当前 Task/Run/Epoch。缺失时展示覆盖不可用，不用历史摘要重建完成态。
- H5 原子封存使任务和 Gate 的 Epoch 加一，Coverage 仍保留事实产生时的 Epoch，因此用明确绑定关联二者。

主工作区相关回归 133 项通过，其中包含 3 项 H5 Gate PostgreSQL 回归和 14 项恢复回归；洞察 H5 子集 46 项通过。完整洞察套件另有一个截图去重旧失败，已在基线上复现。

## 第二批：H5 正式报告与真实 Coverage 事务绑定

提交：`a3146bdb`。

- Worker 报告使用经领域哈希验证的完整 Coverage，并要求时间线 Run 与报告 Run 一致。
- 正文保存 Task/Run、Coverage id/hash/来源 Epoch、Gate id/封存 Epoch；完成结论由 Coverage 决定。
- `finalize_task_with_report` 与 `bind_report` 均比对 Gate 实际选择的 Coverage，同时检查正文 Task、Run、Gate、报告版本及最终状态。
- 生成后发生 Coverage 修订变化会使原子封存回滚；Gate 已封存后的迟到高修订不能替换它的报告输入。
- blocked 预览不携带“本次 Gate 已接纳此 Coverage”的嵌套标记。

主工作区相关回归 **228 项通过**，包含 5 项真实 PostgreSQL 报告绑定测试。数据库回读覆盖 Run 最终报告、Gate、报告绑定和不可变正文，两条入口均有正反例。新增模块与测试 Ruff 通过，并完成独立只读审查。

## 第三批：App 已知未覆盖页型阻止完成

提交：`63bd6847`。

- 动态适用性有结论不等于实际页面已覆盖，终态和摘要复用同一完成条件。
- `entry_seen` 和 `capability_blocked` 的已知适用页缺口阻止完成；有定向无入口证据的合法不适用页继续允许排除。
- Gate SQL 独立复核已知未覆盖集合，拒绝旧版本已经误标 `coverage_complete=true` 的摘要。

新增 PostgreSQL 反例修复前为 1 failed、2 passed，修复后 3 passed。主工作区终态、探索轮次、Gate 和 Finalization 组合回归 **176 passed、4 failed**；4 项均为修改前已经存在的到达证明失败，见后文。修改的 App 领域与测试 Ruff 通过。

## 第四批：正式 Driver 与恢复共用 Broker

提交：`7c0f08af`。

- Driver 适配器放在单一职责模块中，原 Broker 保留兼容入口与 Receipt builder。
- 主交互和正式恢复均复用任务命令校验器，检查执行身份、来源、上下文与 Epoch，正式请求缺校验器不能调用 Driver。
- 回执覆盖成功、返回失败、已声明异常和未知异常，包含 Task/Run/Session/Action/Source/Epoch 及参数哈希；不复制输入值、租约令牌或异常正文。
- 正式恢复 Driver 不支持上下文时也返回有回执的明确拒绝，避免协调器将未执行动作误记为结果未知。
- 候选解析、运行身份和目标策略等原有前置校验保留。ADB M4 off/observe 不作为正式 App 操作禁令；Fake 无租约路径继续兼容。
- 成功仍只进入 `EVIDENCE_PENDING`，后态证据和到达证明继续由现有流程判断，Broker receipt 不替代业务覆盖。

复核已追踪 `InteractionResult.state_summary` → Coordinator `record_interaction` → PostgreSQL Action Ledger `result_payload` 的保存路径。新增 4 项应用层协调器回归，验证回执保留到 terminal evidence，实际未执行不会被改写为未知结果；此处账本使用记录型测试仓储，PostgreSQL 序列化路径为代码复核。6 组旧 ADB 回执与基线逐项比较，正文和哈希完全一致。全部本轮涉及 Python 文件的 Ruff 增量比较没有新增诊断；旧诊断保留。

## 合并后主工作区汇总验证

以下三组没有重复测试文件，共 **27 个测试文件、726 项：717 passed、6 failed、3 skipped**。6 项失败均已在本轮修改前基线复现；新增回归全部通过。

| 组合 | 结果 | 说明 |
| --- | --- | --- |
| Worker、H5 Coverage、Gate、Finalization、洞察及 PostgreSQL | 416 passed、1 failed | 12 个文件；唯一失败为既有截图去重用例；其中 25 项为隔离 PostgreSQL 实跑 |
| App Broker、上下文、恢复、架构边界、终态与探索轮次 | 258 passed、5 failed、3 skipped | 13 个文件；旧页面漂移失败 1 项、旧到达证明失败 4 项；跳过两项退役 Demo 和一项缺少 cryptography 的用例 |
| 远端 H5 验收脚本与 ONIRENT vendor | 43 passed | 2 个文件，验证合并后的兼容性 |

数据库组使用 `ALIOTH_TEST_DATABASE_URL` 显式指向独立测试库，所有组均由主工作区 `.venv/bin/pytest` 执行。新模块/测试 Ruff 和本轮差异检查通过。分批结果与本节汇总存在重复，不能再次相加。

## 验证边界与已存在失败

本轮数据库验证使用独立本机 PostgreSQL 测试容器，将空库升级到当前迁移头。没有修改业务数据库配置，也没有将模拟结果当成真实站点或设备验收。

以下失败已经在干净基线 `69a4e50c` 复现：

- `test_app_exploration_turn.py` 的 4 项到达证明断言：缺少有效 After Capture 证明时实际为 FAILED，测试仍期待 VISITED。
- App arrival/cycle 相关 3 项旧用例因 `bottom_tabs_incomplete` 未满足而失败。
- `test_task_insight_service.py` 的 1 项截图去重旧用例失败。
- `test_app_interaction_runtime.py::test_app_navigation_rejects_after_source_page_type_drifts` 期望 FAILED、实际 EVIDENCE_PENDING；基线为 73 passed、1 failed。
- `test_android_surface_driver.py` 的两项弹窗 alias 用例期望 `uiautomator.popup_close_alias`、实际 `uiautomator.semantic_unique`，坐标断言均正确；基线定向运行 2 failed、33 deselected。

这些失败未混入本轮修复。实际设备/远端运行面、真实 H5 站点任务、生产 Completion Gate 与完整补采链尚未验收。前端菜单和路由无需同步。部署本轮代码前必须先应用 `20260912_0110`，否则新增 Gate 字段查询不可用。

## 远端同步

执行期间远端新增 5 个提交，更新 ONIRENT 连接、跳板配置和 H5 验收脚本。合并提交 `2a427acb` 纳入 `origin/dev_2.0@7ac2b577`；唯一冲突是专项规则文档相邻段落，已同时保留远端业务验收口径与本轮修复规则。H5 验收脚本与 ONIRENT vendor 组合测试 43 项通过。远端 ADB 文档已有 Markdown 双空格换行保留，无新增数据库迁移。
