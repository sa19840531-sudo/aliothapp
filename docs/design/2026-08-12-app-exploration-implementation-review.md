# APP 页面探索阶段实施与审查报告

日期：2026-08-12
范围：仅 `APP_READY` 之后的 APP 页面探索，直到 `AppExplorationResult`、Evidence Index/Manifest 和 Surface Session `CLOSED/LOST`。

## 1. 结论

本轮已把 APP 页面探索从“组件基本齐全、完成条件偏弱、异常难收口”推进为可审计的目标驱动工作流：探索目标在 `APP_READY` 后锁定；真实截图事实驱动 Surface Phase、Frontier 和覆盖累计；动作只允许使用当前截图候选并受目标包边界约束；只有覆盖目标满足、无待办事实且 Frontier 耗尽时才能完成；证据缺失、取消、预算或循环会明确收口为 `evidence_insufficient`；Evidence Index 和结果均带稳定 hash，证据封存后才关闭设备 Session。

当前代码达到“仓库级实现和回归可提审”标准，但不能据此宣称生产真机已经验收。真实 PostgreSQL/Redis 并发、真实对象存储故障、Official 云手机和真实 APK 页面语义仍需部署环境验收。

## 2. 范围边界

本轮没有修改正式任务入口，正式入口继续统一使用 `/api/v2/tasks`。没有修改 APK 上传/校验、安装与 Bootstrap，也没有涉及 H5/Web、Probe、Rule Judge、Inspection Work、Completion Gate、报告、人工复核和前端页面。

探索阶段的输入是已形成的 `AppReadyHandoff`，输出是以下不可混淆的事实：

- `exploration_completed`：目标覆盖满足，探索工作排空，证据可封存。
- `evidence_insufficient`：覆盖不足、预算/循环、证据缺口或取消，但已有事实可以封存。
- `technical_failed`：尚未形成可封存业务结果的技术失败。
- `CLOSED/LOST`：Evidence Index 封存后的设备 Session 关闭结果。

## 3. 实施批次

| 批次 | 本地提交 | 实施结果 |
|---|---|---|
| 1 | `608599c` | 新增 AppExplorationGoal、CoverageSnapshot、AppExplorationResult、结果 hash 与 0047 迁移，锁定跨层合同。 |
| 2 | `e1116d1` | 将 `page_state`、overlay 和 Surface facts 从视觉提供方传入真实 Frontier，接通 Android Surface Phase。 |
| 3 | `97ee10e` | Playbook 降为提示；禁止历史固定坐标盲点；增加实时 package/activity 门禁和 ADB 前台身份读取。 |
| 4 | `ae783fc` | 覆盖事实随 Work 单调累计；完成判定改为目标、业务页数、待办数、Frontier 和预算共同决定。 |
| 5 | `57057ae` | PENDING/FAIL/missing Manifest 和 Final Observation 失败均形成可审计 gap，封存 `evidence_insufficient` 后关闭 Session。 |
| 6 | `f500d87` | 取消贯穿 claim、Driver、Timed Observation、后继提交和 Final Observation；重试与崩溃恢复回到 PostgreSQL 事实。 |
| 7 | 本文提交 | 补组合验收，修正动作里程碑在生产接线中被逐字拆分的问题，并完成全量 APP 探索相关回归。 |

## 4. 当前工作流

```text
APP_READY
  -> Screenshot Understanding
  -> Visual Observation(page_state/overlay/surface facts)
  -> Surface Phase 或普通 Frontier
  -> PostgreSQL claim/lease/renew
  -> Explorer 选择当前截图 candidate_id
  -> package/activity/risk gate
  -> before Capture -> 单动作 -> after Capture / Timed Observation
  -> CoverageSnapshot 单调累计 -> 后继 Work
  -> 目标满足 + 无待办 + Frontier 耗尽
  -> Final Observation
  -> Manifest PASS 校验或 gap 汇总
  -> Evidence Index create/seal(hash)
  -> AppExplorationResult(hash/index binding)
  -> Session DRAINING -> CLOSING -> CLOSED/LOST
```

取消路径不会再产生新的 Final Observation。系统先把非终态普通 Exploration Work 记为 `cancelled`，再以 cancellation gap 封存取消前已有证据；封存失败时将 Session 收敛到 `LOST`。`retry_wait` 不再由进程内固定重试次数决定，`next_attempt_at`、`attempt_count/max_attempts`、deadline、lease expiry 和 row version 均以 PostgreSQL 为准。

## 5. 分项审查结果

### 5.1 目标与完成条件

通过。目标在进入探索时生成并随 Work 原样传播，后继不得扩大或替换目标。稳定业务页、页面类型、Journey/Tab 里程碑、待办 Action/Evidence、候选库存和预算共同进入 CoverageSnapshot。模型提出 terminal 不能绕过缺页、缺里程碑、开放 Frontier、预算耗尽或 deadline。

### 5.2 Surface Phase 与视觉事实

通过仓库级验收。Android 视觉输出能够保留 `startup_ad`、popup、auth gate、system dialog 和 overlay facts，未知/非法协议 fail closed。Surface Phase 结束后才回到普通 Explorer。真实视觉模型对目标 APK 的识别质量仍需设备联调。

### 5.3 动作与包边界

通过。普通点击必须引用当前截图产生的候选并通过截图签名、尺寸、package、activity 和实时前台身份校验。跳到浏览器、设置或其他 App 后，普通动作被拒绝；只允许受控 BACK 或符合 Surface Phase 政策的系统对话框动作。Playbook 不再生成无当前视觉事实支撑的历史固定坐标点击。

### 5.4 证据封存与 Session

通过仓库级故障矩阵。完整 PASS Manifest 可以封存完成结果；Manifest 缺失、PENDING、FAIL、校验和/读取错误以及 Final Observation 失败都会形成 gap 并降级为 `evidence_insufficient`。Index/Result 的 outcome、gap、index id/hash 和 result hash 保持一致。封存完成后才关闭 Driver；关闭失败不撤销证据，Session 进入 `LOST`。

### 5.5 取消、重试与恢复

通过仓库级并发合同。取消在外部动作运行期间由协作式 watcher 触发，本地协程停止后不再生成后继 Work；数据库终态和证据收口可以审计。过期 `leased/running` Work 使用 `FOR UPDATE SKIP LOCKED`、lease token 和 row version 重领；尝试耗尽或 deadline 超时进入明确失败码。尚未执行真实多 Worker/真实 Redis 断连故障注入。

## 6. 验证结果

最终验证包括：

- APP 探索相关 38 个测试文件：`354 passed`，覆盖 Goal、视觉、Surface Phase、动作、包边界、覆盖终态、证据/结果、取消、重试、租约、ADB 身份和 Fake App 组合周期。
- Ruff：通过。
- `git diff --check`：通过。
- 既有 `/api/v2/tasks` 入口未修改；没有前端菜单/路由改动。

测试证明的是仓库实现，不是生产运行验收。以下项目仍未执行：真实 PostgreSQL 应用 0047 并发读写、真实 Redis Worker 崩溃恢复、真实 MinIO/对象存储损坏注入、Official 云手机安装/动作/截图/关闭、真实 APK 的页面类型与 Journey 覆盖准确率。

## 7. 上线前门禁

1. 数据库必须应用到 Alembic head，确认 `qc_app_exploration_result`、不可变 trigger 和此前 App Capture/Manifest/Index 迁移均存在。
2. 使用真实 PostgreSQL/Redis 启动至少两个 Worker，验证 lease 过期重领不会发生双动作。
3. 在 Official 设备上验证前台 package/activity 读取、跳外部包后的 BACK 恢复、Timed Observation 取消和 Driver close 失败。
4. 对对象存储执行 missing/read/size/checksum 故障注入，核对 Index gap、Result hash 和 Session 终态。
5. 用至少一个无专用 Playbook 的 APK 和一个有专用 Playbook 的 APK 验证目标页、Journey/Tab 里程碑及 `evidence_insufficient` 摘要。

在以上门禁完成前，建议将结论表述为“APP 探索代码闭环已完成，生产环境待联调验收”，不应表述为“真实 APP 自动质检已全面上线”。
