# 页面状态图与路线 API 批次审查

日期：2026-09-07。对应九项实施目标中的页面/状态记录、转场、树与进度、路线查询（E/F）。

## 1. 交付边界

- 新增页面图领域判定、可重建 PostgreSQL 投影、进度与连续路线查询；接入现有 Capture、Action Execution 和页面识别 Writer。
- 页面身份与页型、局部状态分开。已有视觉身份基础使用 `818abff9` 的生产输出合同，不以测试专用字段或页型名充当 App 业务身份。
- 失败、阻断、外跳和未知操作保留诊断记录。树仅展示可信业务页面；未访问入口和弹窗分别展示，不造假首页。
- API 沿用任务读取权限和权威 Run/Epoch；恢复路线是只读候选，不执行设备操作，也不实现完整补采或新会话重放。
- 新增迁移 `20260906_0085`，生产启用本批 Writer 前必须先完成迁移；没有迁移不能作为空图继续运行。未增加前端菜单、路由或页面。
- 不包含 CLI、App 正式执行上下文及 D 调度的未提交改动，不修改 Coverage Snapshot 或正式报告的权威来源。

## 2. 主审修正

1. App Capture 的包名、Activity、图片摘要和 Artifact 以实际采集列为准，不能被历史 metadata 覆盖；权威空值不能被旧字段补齐。
2. 图重建不读取 Session 当前阶段代替历史 Capture 阶段；无依据或冲突保持未知。
3. 动作 Run 按真实来源 Capture、真实目标 Capture、合法无 Capture 入口标记的顺序确定；状态能否投影不改变动作归属。跨 Run 对保留在来源运行的诊断中，不计覆盖或恢复。
4. 入口标记先严格验证 UUID 再转换，避免畸形历史内容导致整个重建失败。只有两端均无真实 Capture 引用时才允许标记兜底。
5. SQL 恢复约束显式拒绝空风险等级，不能利用 PostgreSQL CHECK 对 NULL 的语义绕过低风险条件。
6. 页面识别补全后重算关联动作；尚未加载、缺可见层或后置条件不足时不提升为可信到达。分页/筛选变化属于原页面状态，可形成状态路线而非新页面覆盖。

## 3. 需求与证据

| 用户要求 | 本批对应证据 |
| --- | --- |
| 建立页面与页面状态记录 | `test_page_graph.py::test_app_same_page_pagination_is_state_change_not_new_page_arrival`、`test_state_fingerprint_does_not_change_when_only_page_type_changes`、`test_same_visual_name_with_different_business_object_has_distinct_identity`；`test_app_visual_page_graph_postgres.py::test_saved_production_visual_output_projects_and_rebuilds_page_identity` 验证实际存储、生产映射、读回与重建 |
| 记录页面跳转路径和操作结果 | `test_page_graph.py::test_failed_action_keeps_terminal_edge_without_target_capture`、`test_completed_capture_pair_without_trusted_postcondition_is_not_arrival`；`test_page_graph_projection_scope_postgres.py::test_real_detection_writer_promotes_local_state_route_only_after_loaded_observation` |
| 生成页面探索树和进度 | `test_page_graph_projection.py::test_tree_keeps_cycles_and_multiple_entries_outside_parent_links`、`test_startup_overlay_does_not_create_home_and_nested_overlays_attach_by_path`、`test_progress_requires_evidence_inspection_and_exploration_completion`；真实 PG `test_real_graph_tree_reads_quality_and_inspection_without_changing_coverage` |
| 提供页面路线查询接口 | `test_page_graph_read_postgres.py::test_task_graph_query_enforces_tenant_scope_and_authoritative_run_pointer`、`test_real_recovery_query_builds_continuous_routes_and_excludes_failed_edges`、`test_real_recovery_denies_unprojected_graph_instead_of_claiming_full_path`；API `test_page_graph_routes_pass_scope_and_clamp_recovery_limit` |

## 4. 独立验证环境

暂存区独立导出：`/tmp/alioth-pagegraph-index-HwblVW`。通过实际模块 `__file__` 确认导入该目录，而非原工作区的未提交模块。

隔离 PostgreSQL：已有专用容器 `8ada9a87599e`，端口 `127.0.0.1:51309`；本批新建空库 `alioth_pagegraph_commit_20260907`，未连接业务 5432。

初次独立验证：14 个批次测试文件共 156 项通过；页面检测工具、任务详情服务/仓储与固定 Capture 阶段共 686 项通过。追加目标 Capture 不可投影及规范入口标记回归后，全部 18 文件在最终暂存导出合并运行：**846 passed，3487 warnings，11.40 秒**，退出码 0。警告来自既有依赖/asyncio 弃用，不作为功能通过依据。

迁移验证：从空库完整升级至 0085；随后在该隔离测试库执行 0085 → 0084 降级，删除的仅为可重建页面图测试投影，不涉及业务库。最终合并回归重新升级成功，数据库读回 `20260906_0085`。30 个代码/测试文件 Ruff 与暂存差异检查通过。

复验命令（在暂存导出目录执行）：

```bash
PYTHONPATH=/tmp/alioth-pagegraph-index-HwblVW \
ALIOTH_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:51309/alioth_pagegraph_commit_20260907 \
/Users/funshow/Desktop/0_foya/alioth/.venv/bin/python -m pytest -q --disable-warnings \
  services/api/tests/test_page_graph.py \
  services/api/tests/test_page_graph_progress.py \
  services/api/tests/test_page_graph_projection.py \
  services/api/tests/test_page_graph_projection_scope_postgres.py \
  services/api/tests/test_page_graph_read_postgres.py \
  services/api/tests/test_page_graph_run_scope_postgres.py \
  services/api/tests/test_app_visual_page_graph_postgres.py \
  services/api/tests/test_app_postgres_integration.py \
  services/api/tests/test_task_insight_api.py \
  services/api/tests/test_action_execution_repository.py \
  services/api/tests/test_app_capture_repository.py \
  services/api/tests/test_app_visual_observation_repository.py \
  services/api/tests/test_page_capture_persistence.py \
  services/api/tests/test_surface_action_persistence.py \
  services/api/tests/test_page_detection_tools.py \
  services/api/tests/test_task_insight_service.py \
  services/api/tests/test_task_insight_repository.py \
  services/api/tests/test_capture_epoch_postgres.py
```

## 5. 未覆盖风险与后续步骤

- PG 与受控视觉载荷证明合同及存储接线，不证明真实模型识别准确率或 Android 真机探索成功。
- App 同图片复用的新身份绑定、统一执行上下文与调度接线属于后续批次，不用本批图查询通过替代主链验收。
- 正式覆盖仍需沿原控制面验收其实际输入及最终完成检查；图接口的 `coverage_authority_changed=false` 本身不是正式任务通过证明。
- A2、B/C、D 的整体回归、真实浏览器与官方 Android 服务验证、分批提交尚未全部结束，九项目标保持实施中。
