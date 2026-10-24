# W17-W20 阶段机 Worker/Interaction Runtime 接入合同

> 状态：合同层完成，待 Worker 主链路接入。

## 目标

W17-W20 阶段机用于处理 H5/App 探索前的动态遮挡和门禁状态：

| 工作项 | 阶段 | 目标 |
| --- | --- | --- |
| W17 | 启动广告倒计时 | 等待倒计时、关闭广告、确认稳定页。 |
| W18 | 连续弹窗 | 逐层关闭弹窗，每层关闭后重新采集和识别。 |
| W19 | 认证门禁 | 识别登录墙、游客入口、关闭入口或人工阻断。 |
| W20 | 系统弹窗 | 按策略处理权限、浏览器或 App 系统弹窗。 |

本分支只冻结运行时交接合同，不直接改 Worker 主循环，避免在真实链路中引入不可控分支。

## 当前已具备能力

| 能力 | 代码位置 | 状态 |
| --- | --- | --- |
| 领域阶段机 | `services/api/app/domain/exploration/*_phase.py` | 已完成。 |
| 阶段运行与步骤账本 | `services/api/app/infrastructure/db/surface_phase_repository.py` | 已完成。 |
| 阶段判定到账本写入 | `services/api/app/application/surface_phase_runtime.py` | 已完成。 |
| 阶段动作交接合同 | `services/api/app/application/surface_phase_action_handoff.py` | 已完成。 |

## 正式接入顺序

Worker 或 Explorer Runtime 后续接入时，必须按以下顺序执行：

1. 从当前 Session 的 `current_capture_id` 构造阶段观测对象。
2. 调用 `SurfacePhaseRuntime.evaluate_and_record()` 写入阶段步骤。
3. 如果阶段决策是终态，直接结束该阶段。
4. 如果阶段决策包含 `next_action`，调用 `build_surface_phase_action_handoff()`。
5. 若交接类型是 `action`，把 `ActionIntent` 交给 `CaptureCoordinator.coordinate()`。
6. 若交接类型是 `timed_capture`，等待 `delay_ms` 后调用 `CaptureCoordinator.capture_observation()`。
7. 真实动作或重采完成后，用新的 Capture 重新构造观测，进入下一轮阶段判定。

## 禁止事项

- 禁止 Worker 直接操作 Playwright/App Driver 绕过 `CaptureCoordinator`。
- 禁止把 `timed_capture` 当作真实点击动作写入 `ActionExecution`。
- 禁止阶段机直接写规则执行结果。
- 禁止没有 `source_capture_id` 的阶段步骤进入账本。
- 禁止没有 before/after Capture role 的真实动作进入 `ActionIntent`。

## 后续验收

| 验收项 | 口径 |
| --- | --- |
| W17 启动广告 | 倒计时等待、关闭动作、关闭后 Capture 均可在阶段步骤中看到。 |
| W18 多层弹窗 | 每层弹窗关闭后必须产生新 Capture，不允许一次性假完成。 |
| W19 认证门禁 | 无法绕过时阶段终态为 blocked，不进入稳定页面。 |
| W20 系统弹窗 | 策略、动作和重采结果均可审计。 |

自动化验收应落在 W46-W49，不在当前合同分支伪造真实 Worker 闭环。
