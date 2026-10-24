# APP 联调清单（按优先级 · 仅依据设计 MD）

依据：`F:\2026-10-22-cross-platform-flow-foundation-design.md`  
只谈设计合同中的 **App（Android）联调／验收**；不谈实现分支或部署。  
总门槛：三端都完成最小真实闭环才算「三端通用」；**Mock Driver 不算 App 通过**（§14）。

### 落地进度（领域合同）

| 级 | 状态 | 代码 |
| --- | --- | --- |
| **P0** | 领域合同已落地（身份词典、三结果、能力矩阵、单入口、平台／设备冲突域） | `domain/exploration/flow_foundation.py` |
| **P1** | 领域守卫已落地（归属／预约承诺／fence／单执行者／租约承接／unknown 不重放／纯观察） | 同上；测例 `tests/test_flow_foundation_p0_p1.py` |
| **P1 基础设施接线** | 应用服务＋组合根挂接已落地（内存预约仓；Coordinator 入口守卫） | `application/flow_foundation_p1.py`；`CaptureCoordinator(flow_foundation_p1=…)`；`app_v2_runtime` 注入；测例 `tests/test_flow_foundation_p1_wiring.py` |
| **P2 控制闭环** | 领域＋内存 claim／hold／预算／恢复核验已落地 | `domain/.../flow_foundation_control.py`；`application/flow_foundation_control.py`；测例 `tests/test_flow_foundation_p2_p4.py` |
| **P3 连续证据** | 事件序／时钟域／缺帧／重采重传／v3／封存版本合同已落地 | `domain/.../flow_foundation_evidence.py`；同上测例 |
| **P4 试点门禁** | 纯观察／低风险回源验收守卫已落地（非真机签字） | `domain/.../flow_foundation_pilot.py`；同上测例 |
| **PG 迁移＋仓储** | Alembic `20261022_0143` 七表＋`PostgresFlowFoundationRepository` 已落地 | `migrations/versions/20261022_0143_flow_foundation.py`；`infrastructure/db/flow_foundation_repository.py` |
| **Runtime 接线** | `AppFlowFoundationRuntime`＋CaptureWriter／探索观察后触发＋租约 handoff 已落地（内存默认可测） | `application/flow_foundation_runtime.py`；`app_v2_runtime`；`AppCaptureWriter`；`app_screenshot_exploration`；`DeviceLeaseRepository.handoff_to_successor_session`；测例 `tests/test_flow_foundation_runtime_wiring.py` |
| **P5 只读 API** | `GET .../flows`／`observations`／`flow-links` 已注册（空库 200） | `api/v2/flow_foundation.py` |
| **FlowDefinition／TimedObservation v3** | P4.1／P4.2 定义注册＋v3 工厂（App 基线）已落地 | `domain/.../flow_foundation_definitions.py`；`timed_observation.py` |
| P1／P2／P3 PG 写穿 | **组合根已挂 PG repo** | `app_v2_runtime` → `PostgresFlowFoundationRepository`；须先跑迁移 `20261022_0143` |
| Outbox／对象存储双边提交 | **未做** | 迟到上传 Outbox、InputSet 版本身份仍待 |
| 稳定 Capture 禁剥绑定（App） | **已落地（无真机）** | `page_capture_persistence._should_retry_without_surface_binding`：`app:` session 永不剥绑定 |
| P4 真机试点签字 | **未做** | Mock Driver 不算通过；须可控 Android 跑 4.1／4.2；灰度／故障注入（设计 P5）未做 |
| P6 门户流程 UI | **未做** | 只读 API 已有；详情组件未接 |

### 无真机可签工程项

以下项不依赖真实 Android，可用内存／单元测签字（本轮已落地）：

| 项 | 内容 | 状态 |
| --- | --- | --- |
| **A** | 探索路径 `persist_claim_decision`＋`ParentHoldGate`／`assert_parent_claimable` 阻断已 hold 父待办 | **landed** |
| **B** | App `app:` surface session 禁止失配剥绑定（§9.3） | **landed** |
| **C** | 调度单调抬升 `observed_at` 标注 `observed_at_clock_domain`／`scheduler_synthetic_observed_at` | **landed** |
| **D** | `recover_then_handoff`（仓储＋`PostgresDevicePool` 薄包装）；领域仍走 `build_lease_handoff` | **landed** |
| **E** | `CaptureCoordinator` 可选 `require_permit`（parameters 含 fence／permit 时） | **landed** |
| **F** | 观察序列 `seal_observation_sequence`／runtime `seal_series`（内存封存，无新迁移） | **landed** |

### 须真机

| 项 | 说明 |
| --- | --- |
| **P4.1** | 纯观察试点：可控 Android 真机签字（**Mock 不算**） |
| **P4.2** | 低风险交互回源试点：可控 Android 真机签字（**Mock 不算**） |

其余灰度／故障注入（设计 P5）与门户流程 UI（P6）可后置，不挡无真机正确性项。

---

## 优先级总览

| 优先级 | 含义 | 对应设计 | 未通过时 |
| --- | --- | --- | --- |
| **P0 阻断** | 没有则不得宣称联调开始／不得开放真动作 | §1 裁决、§13 P0、硬性正确性 | **停** |
| **P1 正确性** | 归属／互斥／未知结果——错了会脏数据或双点击 | §7–§9、硬性门槛、T06–T08/T11/T13/T27 | **停开放动作** |
| **P2 控制闭环** | 触发、挂起、恢复、预算——短流程能否成立 | §5–§6、§13 P3、T01–T05/T09/T10 | 可测证据，不可试点收口 |
| **P3 连续证据** | 观察序、缺帧、时间来源、v3 合同 | §10、§13 P2、T16–T19/T21/T28 | 观察类规则不得正式通过 |
| **P4 试点闭环** | 设计要求的两个真实场景 | §13 P4 | 不得签「App 已验收」 |
| **P5 收口展示** | 查询／详情／报告版本 | §11、§13 P6 | 可后置，不挡正确性签字 |

依赖（§13）：**P1 证据链可用之后，才能开放执行（P2 真动作）**；P1／P3 与 P2 控制可在接口冻结后并行准备，但真机发动作以 P1 门禁为准。

---

## P0 — 阻断级（先做完再联调）

| 序 | 联调项 | 设计依据 | 通过标准 |
| --- | --- | --- | --- |
| 0.1 | 无第二套 App 设备执行循环；流程控制嵌入既有调度 | §1 裁决 2 | 架构／联调说明可指认唯一动作入口 |
| 0.2 | `target_platform`≠`surface_kind`；WebView 不改任务、不绕设备锁 | §1 裁决 4；T25 | 冲突域＝同一 `device_id` |
| 0.3 | 能力矩阵：截图／UIA／窗口／包名／进度等 → ready／partial／unsupported | §1 裁决 5；§5；§13 P0 | 进度等 partial **不能**启动「必须证明播放」的正式流程 |
| 0.4 | 不因基建自动开放支付／清数据／账号变更／Judge 操控设备 | §1 裁决 9 | 试点清单无此类项 |
| 0.5 | 三项结果模型分立：execution／evidence／resume | §1 裁决 8；§4.3 | 存储或 API 能分别读出，不单字段 success 覆盖 |
| 0.6 | 身份词典冻结（task／scope／phase_run／segment／subject／occurrence／fence 等） | §4.2；§13 P0 | 联调用例共用同一套 ID 语义 |

**P0 全过 → 进入 P1。**

---

## P1 — 正确性（开放真动作前）

> 硬性门槛（§14）：跨流程错归属、跨任务接受、失权动作、重复副作用、历史输入覆盖、虚假恢复＝**零**。

| 序 | 联调项 | 设计依据 | 必测 ID | 通过标准 |
| --- | --- | --- | --- | --- |
| 1.1 | 稳定 Capture 身份；禁止失配后剥绑定再入库 | §8；§9.3 | T11 | 错绑动作／制品被拒 |
| 1.2 | 采集预约→内容承诺→上传校验；迟到只补原记录 | §9 | T13 T14 T15 | A 料不进 B；无承诺不进正式证据 |
| 1.3 | 同设备单执行者；原生＋WebView 同冲突域 | §7.2 | T07 T25 | 第二 Session 无法并行动作 |
| 1.4 | 执行入口＋写库校验 fence；旧通道可撤销 | §7.2 | T08 | 撤销后新动作＝0；不能撤则隔离 |
| 1.5 | 跨 Session 租约承接：静默→同资源→轮转 fence→记前驱；禁止换机 | §7.2 | T27 | 同 `device_id`；不双持有 |
| 1.6 | 结果 unknown：先核验；无法证明未发生不重放 | §1 裁决 7；§7.3 | T06 | 不二次点击 |
| 1.7 | 纯观察不伪造 ActionExecution | §8；§10 | T21 | action_execution_id 可空且无假点击 |

**P1 全过 → 才允许 P2／P4 发真实设备动作。**

---

## P2 — 控制闭环（触发 · 挂起 · 恢复 · 预算）

| 序 | 联调项 | 设计依据 | 必测 ID | 通过标准 |
| --- | --- | --- | --- | --- |
| 2.1 | 观察后、领主探索动作前统一 `evaluate_trigger` | §5.1 | T01 T03 | eligible／deferred／skipped＋原因码 |
| 2.2 | 触发键不含截图 hash／播放秒／Upload 时间等 | §5.2 | T01 T02 | 同态不重复实例；换主体可新实例（合同允许时） |
| 2.3 | 原子 claim：校验父待办版本→唯一键→预算预留→Phase／hold→Outbox | §5.3 | T04 | 双 Worker 仅一实例、一预留、一 hold |
| 2.4 | 持久挂起 App Frontier（ParentWorkHold）；禁止只存调用栈 | §6.1 | T05 | 崩溃后仍能定位唯一父待办 |
| 2.5 | 恢复：受控 back／warm restore；重验 package／activity／进程／账户／主体 | §6.2 | T10 | Activity 同但账户／主体变 → 拒绝旧目标 |
| 2.6 | 清数据≠普通返回；无可靠新会话种子 → 标不支持 | §6.3 | — | 试点与能力矩阵一致 |
| 2.7 | 短流程不递归；不重复接管已有登录／广告／系统弹窗阶段 | §6.3 | — | 优先级与调用边界可演示 |
| 2.8 | 取消／硬截止／回源失败：停新动作；三项结果独立；不虚报恢复 | §7.3 | T09 | 展示／API 三结果可分读 |
| 2.9 | 预算：流程池与任务总额一致；unknown 不先退款再盲重试 | §7.1 | T03 | 不足则 deferred／skipped |

---

## P3 — 连续观察与证据质量

| 序 | 联调项 | 设计依据 | 必测 ID | 通过标准 |
| --- | --- | --- | --- | --- |
| 3.1 | 采样入口分配 `resource_event_seq`；不靠上传后排序 | §10.2 | T17 | 序不靠墙钟／进度／UUID |
| 3.2 | 写库／调度时间 ≠ observed_at；分时钟域 | §2；§10.2 | T17 T18 | legacy 时刻打标，不回填 |
| 3.3 | 同字节不同样本保留两事件；缺帧保留原 index | §10.1–10.4 | T16 T19 | 不压缩编号伪装连续 |
| 3.4 | App 不假设 UIA 有进度／时长；截图与树未必同帧 | §10.3 | T03 | partial 规则合同匹配 |
| 3.5 | 重采新身份／新窗口；重传沿用原身份 | §9.3 | T28 | 两者可区分 |
| 3.6 | TimedObservation 仅在 writer／reader／调度／适配器均支持时用 v3 | §11.1 | T23 | 旧协议不静默变浏览器请求 |
| 3.7 | 封存后补齐→新版本；旧 InputSet／结论不变 | §9.3 | T20 | 不复活已结束任务 |

---

## P4 — 试点真实闭环（设计 §13）

| 序 | 试点 | 优先级内顺序 | 通过标准 |
| --- | --- | --- | --- |
| 4.1 | **纯观察**：已到详情类业务态的有界连续观察 | 先做（无副作用） | 触发→采样→封存／缺口可解释；不冒充播放验证 |
| 4.2 | **低风险交互＋回源**：已授权展开／关闭后受控返回 | 后做（依赖 P1＋P2.5） | 动作账本＋恢复核验＋三结果；禁止支付／清数据／未授权 deep link |

未完成 4.1／4.2 → **不得签 App 端「P4 通过」**。

---

## P5 — 查询与展示（可后置）

| 序 | 联调项 | 设计依据 | 说明 |
| --- | --- | --- | --- |
| 5.1 | `GET .../flows`、`.../observations`、`.../flow-links` | §11.2 | P1 即可具备只读；UI 可晚于正确性 |
| 5.2 | 任务详情区分采样时间／接收时间、迟到、断段、三结果 | §11.3 | 不挡 P0–P4 签字 |
| 5.3 | 仅探索不发 Judge；完整质检保留原门禁 | T24 | 与试点任务类型一致即可抽检 |

---

## 建议联调日程（按优先级压缩）

| 日序 | 只做 | 出口门禁 |
| --- | --- | --- |
| Day 0 | **P0** 全表 | 能力矩阵＋无双循环＋三结果模型 |
| Day 1 | **P1**（归属／设备锁／unknown） | 硬性正确性零缺陷才发真动作 |
| Day 2 上午 | **P2** 触发＋claim＋挂起 | T01／T04／T05 |
| Day 2 下午 | **P2** 恢复＋取消 | T09／T10 |
| Day 3 上午 | **P3** 连续观察／时间序 | T16／T17／T19 |
| Day 3 下午 | **P4.1** 纯观察试点 | 试点记录归档 |
| Day 4 | **P4.2** 交互回源试点＋**P1/P2 回归** | App 联调结论 |
| 可后置 | **P5** | 不写入「基建正确性」通过条件 |

---

## 失败即停（任何优先级）

- 第二套设备执行循环，或绕过统一执行权发 ADB／云手机命令  
- WebView 换 Surface／Session 绕同一 `device_id`  
- 跨 Session 恢复换机或未撤旧通道双持有  
- partial／无进度源给出正式播放类通过  
- 用清数据／支付等凑 P4  
- 仅 Web／H5 闭环就签含 App 的三端通用通过  

---

## 签字

| 项 | 记录 |
| --- | --- |
| 已通过最高优先级 | P0／P1／P2／P3／P4（勾到哪级） |
| 失败／跳过的 ID | |
| 结论 | 通过／做到 Px 有条件通过／不通过 |
| 有条件附录 | unsupported、未测 T、跟进项 |
