# 有限探索与动作后证明审查

日期：2026-09-07。范围：观星负责的有限探索调度，以及已有页面状态图所消费的真实动作结果。不实现新的 Probe 消费者、完整定向补采或新会话回放。

## 本批实现

- H5/App 共用三队列策略：必检页面、持久化证据缺口、自由发现。安全可达性和阻断处理仍在候选选择之前；App 全局队列先按三队列排序，再使用原有页型偏好，避免自由发现抢在补证前。
- 动作、耗时和重试分别累计到现有 Work/Frontier 数据，跨轮和失败重观察不清零。重复业务页、重复弹窗、相邻及非相邻返回循环分别计数；停止仍保留未完成目标，不改成“不适用”。
- 正式 AppV2/H5 组合根读取数据库中同任务、Run、Session、Epoch 的证据事实。删除或失效的事实不能继续沿用，Agent 自报缺口不成为优先级依据。
- App 工作流生成同一次动作的观察证明，统一供候选访问、覆盖累计和 Frontier 结算使用。证明绑定任务、会话、候选、动作 ID、前后 Capture、截图制品及视觉结果摘要，不能借用旧轮证明或页型对齐结果。
- 没有预设页型的点击仍允许发现新页面；必须有实际业务身份变化。同页 Tab/筛选等状态变化不当作新业务页，仅图片变化或执行成功也不够。
- 滚动需要同次截图中至少两个可信内容锚点一致移动，不接受模型自报的滚动数值。合法观察可以完成，只有业务底页可见时才提供覆盖事实。
- 返回允许回到已验证业务路径中的祖先，包括实际 pop-to-root；该路径不是 Android 原生返回栈。前态漂移后断开旧路径，不能拼出不存在的中间跳转。
- 关闭后仍是另一弹窗时保留中间状态。外跳恢复单独记为 `surface_restored`：只确认目标 App 当前重新可见，清空旧路径衔接，业务深度归零；不声明原状态恢复，也不增加业务覆盖或课纲进度。

## 回归要求

| 要求 | 测试证据 |
| --- | --- |
| 无预设页型的自由发现与同类型不同对象 | `test_untyped_free_click_requires_confirmed_business_change_and_round_trips_proof` |
| 同页无效果、局部状态与滚动 | `test_unchanged_business_with_only_image_difference_is_not_arrival`、`test_local_tab_change_completes_action_without_new_business_arrival`、`test_scroll_requires_coherent_bound_anchors_not_self_reported_offset` |
| 真实返回关系与外跳恢复 | `test_back_requires_recorded_predecessor_and_in_scope_after_page`、`test_package_recovery_only_confirms_current_app_without_business_arrival` |
| 跨轮/串帧证明拒绝 | `test_action_proof_rejects_mixed_capture_binding`、`test_frontier_rejects_replayed_or_tampered_action_proof` |
| 缺证先于自由发现 | `test_missing_goal_entry_includes_covered_page_type_with_evidence_gap` 的两个领取入口 |
| 时间、重试、循环与轮次恢复 | `test_state_keeps_per_queue_action_time_retry_and_loop_facts_after_roundtrip`、`test_non_adjacent_return_cycle_is_bounded_but_same_page_scroll_is_not_duplicate`、`test_h5_scheduler_state_survives_multi_round_failure_and_json_restore` |
| 真实事实范围与生产接线 | `test_h5_reader_uses_current_run_epoch_and_refreshes_missing_snapshot`、`test_app_reader_validates_capture_scope_and_refreshes_deleted_index`、`test_provider_wires_postgres_scheduler_facts_reader_into_workflow` |

## 交付边界

暂存区独立导出 `/tmp/alioth-d-index-IQlgrs`，验证树 `ad7afc3c12d649681585a5beeba6928f5ba847fa`（后续仅更新本文记录）。23 个测试文件组合为 **775 passed，10.43 秒，退出码 0**，无跳过；覆盖 App/H5 工作流、调度持久事实、页面图投影/权限/连续路线、真实隔离 PostgreSQL 和动作证明。导出代码不含未提交的 CLI 或秘密输入包装器；本批 Python Ruff 及暂存差异检查通过。

额外恢复消费回归 `test_package_boundary_recovery_is_surface_restored_without_business_coverage` 和 `test_runner_consumes_surface_restored_as_visited_without_business_arrival` 同时覆盖 BACK/WARM_RESTORE，断言后继继续、恢复操作结算与业务覆盖不混淆。

本批无新增数据库迁移，无前端菜单/路由变更，不改变 Coverage Snapshot、规则结果和正式报告的权威来源。纯函数、工作流和隔离 PostgreSQL 回归是代码证据；不冒充真实 Android 或完整正式 QC 任务通过。测试环境未安装 pytest-cov/coverage，不报告未经测量的代码覆盖率。
