# App 统一执行边界分批审查

日期：2026-09-07。分支：`dev_2.0`。基线：`07788be6`。

本批属于观星的“统一页面操作执行流程”和“实现受限命令与风险控制”。只提交 App/ADB/Official 执行边界与公共协调上下文，不包含未提交的 CLI、调度和动作后业务到达证明。

## 本批改动

- AppV2 组合根共享服务端命令范围、Coordinator、动作账本和 Capture 恢复读取。当前包名、绑定视觉目标分别在设备动作前复验。
- Coordinator 把同一命令的 Epoch、序号和租约透传到 Official/Xingyu。Xingyu dispatch 前再次读取真实数据库范围，不在下游重复申请租约。
- 正式序列/租约请求遇到不支持完整上下文的 Driver 时，在任何设备原语前拒绝；不退回裸 `execute`、`warm_restore` 或 `reset_app`。历史仅 Epoch 的兼容请求不冒充正式 App 请求。
- 正式恢复要求 Coordinator 和实际 After Capture 的快照读取器；缺少真实后态不能用预期状态拼造。目标新会话缺少父 Capture 时保持明确不支持，不扩展为完整新会话补采。
- Android 默认策略不再开放 HIGH 风险或通配包名；UI Hierarchy 中实际目标参与风险判断。允许安全留证的失败仍进入后态采集。
- 租约令牌不进入请求 repr；API 从同步设备调用改为异步边界内的受控派发。
- Official 动作请求不使用通用 HTTP 自动重试；502/500、网络中断、读取超时和成功响应合同损坏统一记录为不可重试的未知结果。Runtime 缓存同 action 的未知终态，持久恢复仍由既有 Coordinator 账本负责；App 失败后态继续保留。

## 独立提交验证

暂存区单独导出到 `/tmp/alioth-a2-index-baSuFh`。验证暂存树：`0eea9f28a23d2e7ff2b862ea52baf2bda50aa4fa`（后续仅更新审查记录及规则文档）。AppV2 的调度 reader 接线留在工作区，导出目录没有未提交的调度或 CLI 模块。

18 个测试文件组合运行：**338 passed、5 skipped，32.60 秒，退出码 0**。使用隔离 PostgreSQL `127.0.0.1:51309/alioth_dev20_merge_20260907`，与主数据库 URL 明确区分；未连接业务数据库。

| 验证要求 | 证据 |
| --- | --- |
| 正式请求不能降级到裸 Driver | `test_app_interaction_rejects_formal_request_before_bare_driver`、`test_restoration_interaction_rejects_formal_request_before_bare_driver` |
| Official 透传完整上下文，dispatch 前重新检查范围 | `test_official_runtime_passes_complete_execution_context_to_api`、`test_xingyu_rejects_stale_scope_between_validation_and_dispatch` |
| 高风险目标不得仅凭 Agent 自报 LOW 放行 | `test_xingyu_rejects_low_risk_payment_intent_at_official_boundary`、`test_android_resolved_payment_target_is_blocked_before_adb_driver` |
| 恢复必须有真实协调器和后态来源 | `test_formal_restore_rejects_missing_coordinator_before_any_driver_call`、`test_new_session_replay_fails_before_start_without_target_capture_seed` |
| 真实任务/会话/租约/Epoch 约束 | `test_scope_reader_uses_real_task_session_and_verified_build_facts` |
| 默认策略保留普通低风险操作 | `test_factory_default_policy_keeps_low_risk_observe_and_click_available` |
| 设备结果未知不得自动重放 | `test_execute_does_not_retry_unknown_server_failure` 的五类故障、`test_unknown_action_outcome_is_cached_and_not_replayed` |

组合范围：`test_app_execution_context_boundaries`、`test_app_formal_restoration_coordinator_boundary`、`test_app_interaction_runtime`、`test_app_proxy_and_restoration_loop`、`test_app_v2_runtime`、`test_app_runtime_composition`、`test_capture_coordinator`、`test_command_scope_reader_postgres_integration`、`test_official_app_runtime`、`test_official_app_api_contract`、`test_official_app_star_contract`、`test_xingyu_official`、`test_xingyu_official_openapi`、`test_agent_ad_capture_uses_runtime`、`test_android_capture_runtime`、`test_android_surface_driver`、`test_command_scope_and_policy`。

组合范围另包含 `test_app_screenshot_exploration_cycle`：测试专用 Fake adapter 显式接收并断言正式 task/session/sequence/epoch 上下文，动作账本替身实现终态附后态证据协议，不给生产 Driver 添加例外。

独立导出中的本批 Python 文件 Ruff 通过；暂存差异检查通过。测试夹具修正了 Capture source/current-before 区分和截图坐标基准，没有放宽生产验证。

## 验证边界

- 5 个跳过：1 个缺少 `cryptography` 的 MITM 证书测试、1 个未显式配置真实星语服务的合同测试、3 个已经下线的 Mobile Demo Agent 路径测试。
- 未执行真实 Android 设备、真实星语服务和完整正式 QC 任务；不以本批测试宣称 Completion Gate 通过。
- 本批无数据库迁移，无前端菜单/路由变更。页面覆盖的动作后实际到达证明仍在后续批次，不由命令成功或本批测试结果替代。
