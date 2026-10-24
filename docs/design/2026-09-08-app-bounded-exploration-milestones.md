# App 有限自主探索里程碑（M0–M7 依序）

> 日期：2026-09-08  
> 范围：仅 App（Android Official Runtime / ADB Adapter）；H5 Playwright CLI 不在本路线主责。  
> 依据：[有限自主探索完整方案](./2026-09-02-bounded-exploration-optimization-solution.html)、[专项文档集](./2026-09-03-exploration-optimization/README.md)。  
> 顺序约定：**M0 → M1 → M2 → M4 → M5 → M6 → M7**（本路线跳过 M3 产品/规则范围专项，待并行或后续单开）。

## 硬性不变量（全程）

1. Agent 不得直连 Driver / ADB / bash。
2. 命令成功 ≠ 页面到达；成功边必须有 stable after + postcondition。
3. 非 stable / 被遮挡帧不得冒充业务稳定证据。
4. 禁止跨 Capture epoch 重放历史坐标。
5. Coverage / Completion Gate 权威迁移前必须先影子，不得 silent cutover。
6. 外部 Package 落地不计业务覆盖。

## 现况对账（2026-09-08）

| 里程碑 | 现况 | 本轮目标 |
| --- | --- | --- |
| **M0** 契约与基线 | 部分：设备租约、Capture、Manifest、动态 Frontier、禁止直连有约定但缺统一里程碑旗标与 App 指标合同 | 冻结 App 里程碑旗标、指标键、禁止直连声明 |
| **M1** 动作／展示层／证据角色 | 部分：动作观察证明、弹层阶段、就绪诊断影子（`formal_settle_oracle=false`） | 升级 Settle **影子** Oracle：展示层、身份三明治、证据角色；Gate 仅影子记录 |
| **M2** 页面状态图影子 | 已有：图投影、对账、工作台只读图 API；Coverage 权威未迁图 | 巩固 App 双写与对账；图仍不参与调度／Gate |
| **M4** Command Broker | **已交＋加深**：observe 默认；Lab actions；单 Owner／前台 allowlist／后态回执 | 真机对账后再评估生产开闸 |
| **M5** 有限探索灰度 | **本轮已交**：图去重默认；**Lab 图调度灰度**（`schedules_from_graph`） | 真机后再评估生产开闸 |
| **M6** 图恢复与补采 | **本轮已交**：计划影子默认；**Lab runtime_ready 灰度**（仍不自动 Probe） | 真机 E2E 后再评估生产开闸 |
| **M7** 工作台／Gate／退役 | **本轮已交**：影子预览默认；**Lab Gate 裁决灰度**（不改 Gate SQL） | 真机后再评估生产开闸 |

## 本轮交付（M0 + M1 起步）

- `services/api/app/application/app_bounded_exploration_milestones.py`：里程碑旗标与指标键。
- `app_capture_readiness_diagnosis` → Settle 影子合同：`presentation_layer`、`identity_sandwich`、`evidence_role`；**正式 Oracle 仍关闭**。
- 探索 coverage metadata 写入影子字段，供后续 Gate shadow 消费。
- 回归：`test_app_p1_optimization_batch.py` 扩展；专项规则追加条目。

## M1 加深（2026-09-08）

- `classify_app_entry_layer`：APP_READY 后入口阻断层分型；恒 `app_ready_equals_business_home=false`、`underlying_home_fabricated=false`。
- 诊断 v3／settle v2：嵌入入口层；覆盖元数据写 `app_entry_layer` + `app_m1_settle_audit_bundle`。
- **正式 Oracle／Gate 仍不消费**；回归 `test_app_m1_entry_settle_deepen.py`。

## M2 交付（2026-09-08）

- `build_app_page_graph_shadow_snapshot`：Coverage 业务身份双写影子图；弹层／外跳仅诊断态。
- `attach_app_coverage_page_graph_shadow`：写入 `app_page_graph_snapshot` + 对账；`schedules_from_graph=false`、`gate_consumes_graph=false`；本批次禁止打开 M2 authority。
- 探索回合把当前帧传入双写；图仍不参与调度／Gate。
- 回归：`test_m2_shadow_dual_write_*`、`test_m2_forbids_opening_graph_authority_in_this_batch`。

## M2 加深（2026-09-08）

- 判别指南升至 `app-page-discrimination-guide.v2`（阻断层／底栏入口消歧 + 能力规则），仍只影子注入 Vision。
- `app_runtime_capability_shadow`：入口后暂定能力；静态画像 ≠ 运行时；未找到不得缩小冻结分母；`authority=false`。
- 身份继续 `app-business-page-identity.v6`；图权威位仍禁止打开。
- 回归：`pytest services/api/tests/test_app_m2_recognition_deepen.py -q`。

## M4 交付（2026-09-08）

- `app_command_broker`：生产默认 **observe**；Lab 用 `build_lab_m4_actions_milestone_policy()` 开低风险 `tap`／`swipe`／`back`。
- 坐标动作须 `capture_epoch` + Package allowlist；`shell` 等永拒；Receipt `arrival_not_implied`。
- 回归：`pytest services/api/tests/test_app_command_broker.py -q`

## M4 加深／对照页 M3（2026-09-08）

- Broker `app-command-broker.v3`：`executor_owner`（单写、禁双执行器、可回退 Driver）+ `timeout_policy`（禁无界挂起）。
- actions 须 `active_executor=broker`；前台包须落 allowlist；执行后采集后态且恒 `arrival_not_implied`。
- 生产仍 observe；影子不替换正式 Capture；对照页阶段标记 `comparison_page_stage=M3`。
- 回归：`pytest services/api/tests/test_app_m3_adb_broker_deepen.py -q`。

## M5 交付（2026-09-08）

- `app_exploration_grayscale`：70/20/10 三队列；**discovery** 图／Coverage 去重（不误伤 coverage_seek／底栏／关层）。
- 外跳旁路：browser／store 仅观察+回源；不计业务覆盖；复合动作禁跨 epoch 坐标重放。
- **Lab 图调度灰度**：`build_lab_m5_graph_schedule_milestone_policy()` → `schedules_from_graph=true`，对未访问目标／`graph_recovery` 候选优先排序。
- 恒 `gate_consumes_graph=false`、`coverage_authority_changed=false`；生产 `authority.M5=false`。
- `eligible_app_candidates` 可读元数据内里程碑覆盖；回归：`pytest services/api/tests/test_app_exploration_grayscale.py -q`

## M6 交付（2026-09-08）

- `app_graph_recapture`：Route Planner／等价门禁／CaptureSeries；生产默认 `runtime_ready=false`。
- **Lab 补采灰度**：`build_lab_m6_recapture_milestone_policy()` → 注入 Lab Supplemental；等价+路线+血缘通过时 `can_execute=true`。
- 恒 `executes_probe=false`（计划可执行 ≠ 自动派发）；fresh 不得填 exact；高风险拒 equivalent／fresh。
- 生产组合根不得默认 Lab 工厂；回归：`pytest services/api/tests/test_app_graph_recapture.py -q`

## M7 交付（2026-09-08）

- `app_gate_retirement_shadow`：生产默认影子预览（`would_block_if_enforced`）；Gate SQL 仍只读 `coverage_summary`。
- **Lab Gate 灰度**：`build_lab_m7_gate_enforcement_milestone_policy()` → `lab_gate_passed`／`lab_block_reasons`；可标退役评审就绪。
- 恒 `mutates_completion_gate_sql=false`、`writes_formal_block_reasons=false`、`deletion_allowed=false`。
- 工作台只读图面板仍不驱动正式 Gate；回归：`pytest services/api/tests/test_app_gate_retirement_shadow.py -q`

## 后续批次入口

Lab 开闸链 **M4→M5→M6→M7** 合同已齐；生产默认 authority 仍关。

### Lab 组合根开关（2026-09-08）

环境变量：`ALIOTH_APP_LAB_MILESTONE=off|M4|M5|M6|M7`（默认 `off`）。

| 值 | 效果 |
| --- | --- |
| `off` | 生产默认（Broker observe；补采 `runtime_ready=false`） |
| `M4` | Lab Broker actions |
| `M5` | + 图辅助调度 |
| `M6` | + **刀 A** `lab_plan_ready`（计划可声明可执行；默认仍不自动 Probe） |
| `M7` | + Lab Gate 裁决预览（**不**改 Completion Gate SQL） |

另：`ALIOTH_APP_LAB_EXECUTES_PROBE=true` 为 **刀 B**（仅 M6/M7），才打开关闭前排空／Probe；`lab_supplemental_ready` 现为刀 B 兼容别名。

约束：

1. 与任一 `ALIOTH_APP_MILESTONE_AUTHORITY_M4…M7=true` **互斥**（Worker 启动 fail-closed）。
2. 设备主路径为 **ONIRENT** 固定／WS ADB，不依赖 MoreLogin API。
3. Coverage 写入 `app_lab_composition` + `app_bounded_exploration_milestone_policy`。
4. 改 env 后必须重建／重启 API／Worker。

下一步只允许真机／指标达标后**单独评审**把任一生产 authority 打开，或改 Completion Gate SQL 读图；禁止 silent cutover，禁止未评审删除退役目录候选。

生产开闸评审合同（分层验收／回滚／禁止项）：[`2026-09-08-app-production-authority-rollout-review.md`](./2026-09-08-app-production-authority-rollout-review.md)。
