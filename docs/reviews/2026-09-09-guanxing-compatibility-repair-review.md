# 观星九项兼容修复：分批审查记录

原审查基线：`dev_2.0` / `a1ee8e4a`。实际实施父版本：`270a1866`。方案提交：`407a25ae`。开工期间发生外部 pull 快进，已保留其后续 H5/Lab/工作台改动；本轮按用户授权仅提交本地 Git。

## B1：共享 H5 交互和稳定等待

- **回退范围**：移除 cf9 的“局部像素达到 0.995 即绕过外围锚点”分支；将 e6 的关键资源类型恢复为 document/xhr/fetch/script。没有回退 cf9 的邻域面积变化搜索，也保留语义遮挡、滚动效果和长页/一致性实现。
- **审查结论**：新旧行为差异位于视觉专用路径与共享 settle，不改 CLI/命令策略/统一协调器。可信局部动画正例保留；未证明完全透明且全屏像素不变的任意遮罩都可由图像识别，本批只恢复被该提交绕过的已有保护。
- **反向验证**：在独立 Python 进程中加载基线视觉解析器与基线关键资源集合，运行当前两条回归，结果 2 failed；没有把旧源码写回工作区。
- **绿色回归**：`.venv/bin/python -m pytest -q services/api/tests/test_h5_visual_navigation_runtime.py services/api/tests/test_page_capture_tool.py`，432 passed。
- **实际浏览器**：复用此前隔离 Playwright 遮罩反例，当前解析返回 `VISUAL_TARGET_SOURCE_STATE_STALE`，真实点击日志 `not_clicked_by_parser`。本地临时证据：`/tmp/alioth-guanxing-fixes-20260909/b1-browser.json`。
- **静态检查**：本批四个 Python 文件 Ruff 与 diff check 通过。
- **部署影响**：无数据库迁移；无需前端菜单/路由同步；未执行真实站点、真实设备或生产验收。

## B3：终态 SQL 精确关联与耗尽收口

- **修复**：从当前 Task/Epoch、精确 Work/Capture/Attempt 选取终态请求；终态候选不受重新派发的 attempt 上限过滤。按既有 `(task_id, rule_id, page_capture_id)` 唯一合同预先去重，成员按当前工作归属回写。跨 consumer Run 不串写成员，也不改写已有 execution 的复核原因。
- **审查追加**：初版只覆盖两页各一个 Work，主审要求补测同 Capture 的不同 batch/input，并修正重复 upsert；随后补测已有 execution 属于另一 Run 的复核保护。
- **反向验证**：基线 SQL 在双页耗尽、同 Capture 多 Work、跨运行构造三场景失败。该仓储从 `a1ee8e4a` 到实际父版本 `270a1866` 无差异。
- **绿色回归**：隔离 `pgvector/pgvector:pg16`，使用事务回滚；`test_inspection_terminalization_postgres.py`、`test_inspection_wakeup.py`、`test_inspection_work_item_repository.py` 共 63 passed，Ruff 通过。
- **测试维护**：原 claim 测试的 fake 把规则成员行重复用于补采绑定查询，导致解析缺字段；已只在 fixture 中区分两次查询返回，保留所有原有 claim/租约断言，完整仓储文件不再排除测试。
- **部署影响**：无新增数据库迁移，无前端同步；只迁移本轮空测试库，未修改业务数据库。不同 consumer Run 共用旧 Capture 的执行结果仍不得串用，本批不改变 execution 唯一合同或自动搬移运行归属。

## B5：恢复消费者兼容既有路线合同

- **修复**：同 Session 的 `ORIGINAL_SURFACE_PIN` 直接重新观察，不查询或伪造页面图边。App 消费者接通 SCROLL，仍以当前 Capture 唯一低风险候选绑定，交给原 CaptureCoordinator 执行并读取后态。
- **主审追加**：把无目标文本的例外严格限定为 SCROLL；补上真实 Replay → Restoration → StateEquivalenceGate → Probe 发布链。错误业务状态得到 DIVERGENT，Probe 发布次数为零。
- **反向验证**：`270a1866` 隔离版本中，SCROLL 在绑定前被拒绝；Pin 被结算为不支持而未进入观察/等价性。非滚动语义负例在基线已经通过，作为防止放宽扩散的控制用例保留。
- **绿色回归**：replay、App runtime、restoration 三文件 21 passed；再加规划、组合、消费者、路线、restoration/proxy 的九文件组合 80 passed、1 skipped。跳过项依赖本环境未安装的 cryptography；不将该项视为通过。Ruff、编译与 diff check 通过。
- **边界**：WARM_RESTORE 继续在绑定前拒绝；未改变原路线 API、页面图、覆盖进度或受限命令策略。无迁移、无前端同步；未执行真实 Android 设备或生产补采验收。
- **本地证据**：`/tmp/alioth-guanxing-fixes-20260909/logs/` 内 baseline-270a1866-scroll-red、pin-chain-red 与 current-b5 日志。

## B4：预算分账、异步截止和纯结算

- **预算修复**：关闭前重新读取最新持久化 Work，而非复用启动快照；预算转换保留业务/清障池和时长地板，业务动作不可借用清障额度，总硬边界只裁减余额。
- **异步修复**：每轮共享耗时游标，计入等待、状态读取和结算；Probe 派发前重新检查三类边界。消费者新增可选 deadline，慢 claim/resume/plan 后重新核对，恢复计划通过原 builder 收紧截止并保持合法身份。
- **主审追加**：单纯阻止超时 Probe 会让已受理请求无法收口，因此增加最小纯结算端口，并在现有组合根接线。只取消当前 Task/Run/Epoch 尚未执行的 Probe，将相应请求结算不足/过期，再沿原唤醒和 Judge 收口；保留运行中和已执行结果。未改核心探索队列、动作计划仓储或主调度器。
- **并发审查**：Probe claim 不使用 Task advisory lock。补上 Work 行锁、锁等待后的新语句复查和取消 CTE 的 action 防护；不是仅凭锁名称判断安全。保护任意关联 action ledger，包含结果未知的动作，而非仅保护 completed Capture。
- **反向验证**：加载 `270a1866` 的预算转换、drain 和 consumer 代码，原 11 条预算/异步回归全部失败；消费者额外时限测试也失败，其中新增 deadline 入参在旧版不存在属于接口差异，未把这一 TypeError 单独当成业务越界证明。
- **纯结算验证**：零时长和 recapture 返回时已跨截止的两条边界测试均完成两轮规则收口，Probe 调用为零，结算耗时被扣除。真实 PG 两项回归通过，覆盖同任务旧 Run/Epoch、未执行 Probe 取消、任意动作记录保护、幂等，以及另一个事务领取后进入 running 的锁等待。
- **最终组合回归**：`test_supplemental_budget_compatibility.py`、`test_app_supplemental_evidence_drain.py`、`test_app_supplemental_evidence_drain_repository.py`、`test_app_supplemental_evidence_drain_settlement_postgres.py`、`test_app_runtime_composition.py`、`test_app_targeted_recapture_consumer.py`、`test_app_v2_runtime.py`、`test_app_exploration_closure.py` 共 **160 passed**；Ruff 与 diff check 通过。日志为 `/tmp/alioth-guanxing-fixes-20260909/b4-final-green.log`。
- **证据边界**：恢复已完成的旧 Probe 发布仍保留其原持久化身份，当前 drain 在返回后再次核对总截止；没有全库重写旧 Probe 的 deadline。并发保证沿用“成功领取并持有 Work 租约后才能规划动作”的原合同，本批未重做失效消费者写入协议。进行中或已有未知副作用的工作不被伪造为已排空，仍交原恢复/明确失败路径。
- **部署影响**：无数据库迁移，无前端菜单/路由变化；生产补采开关和 runtime_ready 未放开，未做真实设备或正式任务验收。

## B2：Judge 普通合同与多角色补采兼容

- **归因更正**：`920ed9ec` 已在实际父版本中修复 F01 普通 handoff 的精确来源字段配对。本轮保留它，并增加实际 Maf → RuleJudgeService 产生合法 pass 的回归；不把别人的已有修复记为本轮新增。
- **F07 修复**：补采主材料替换时保留 baseline 的 before 等未替换角色，按声明的角色 Manifest 引用校验 ID、Capture 和版本；未提供角色 Manifest 引用时保留原按 Capture 读取方式。单个主 Manifest 不再导致合法多角色输入一律拒绝。
- **主审追加**：最初“保留角色”仍会被全局附件 ID 过滤掉 before；简单丢弃基线 ID 又会扩大 before 子集。因此增加可选的内部角色基线 ID 候选映射，只与实际角色材料求交；旧 primary 与未选中 before 附件均排除。普通无补采输入不序列化新字段，不改现有 InputSet 持久化协议。
- **模式兼容**：同时覆盖 ON 的 Manifest 路径、OFF/SHADOW 的附件表路径。普通无 Manifest 多角色输入仍按原必需类型投影，不因新增“每个角色必须非空”门槛误伤；新增保留角色才检查投影是否丢空。来源 Run、Capture、路径和原附件完整性校验保持。
- **反向验证**：`270a1866` 的旧调用合同中，原始 handoff 有 before + primary，解析后只剩新 primary；显式多角色精确 Manifest 调用得到“精确 Manifest 只能绑定单一 primary Capture”。证据 `/tmp/alioth-b2-f07-baseline-270a1866.log`，不是用新参数在旧版的 TypeError 代替反例。
- **最终回归**：handoff、Maf、RuleJudge、contract lock、Manifest 来源/权威、普通 evidence 和 evidence gap 八个测试文件共 **141 passed**，Ruff、编译与 diff check 通过。主审独立复核了选中 before 保留、未选中 before/旧 primary 排除及普通输入的实际 pass。日志 `/tmp/alioth-guanxing-fixes-20260909/b2-final-green.log`。
- **证据与部署边界**：使用真实应用服务和读取器，数据库/Manifest/对象存储为测试替身；没有执行生产对象存储或新 InputSet 全流程验收。无数据库迁移，无前端同步，生产补采开关保持原样。

## 原九项核心合同回归

使用本轮隔离 PostgreSQL 执行统一采集协调、受限命令、CLI、预算调度、调度事实、页面图/投影/进度及运行归属相关的 13 个测试文件，结果 **204 passed、12 skipped**。12 项跳过均为 `ALIOTH_CLI_INTEGRATION=1` 才执行的真实锁版 CLI/Chromium 场景，本次未启用；数据库集成项已使用本轮隔离库执行。本地日志：`/tmp/alioth-guanxing-fixes-20260909/core-contract-green.log`。

按 `270a1866` 对照工作树逐文件比较：统一协调器、受限命令策略、有界调度及事实、CLI 会话/进程、页面图模型/服务/合同和页面图/进度仓储共 13 个生产文件保持字节一致。兼容修复位于共享 H5、Judge 交接、终态 SQL 和新增补采消费者，不建立第二套原九项实现。

| 观星原任务 | 本轮处理位置及保护 |
| --- | --- |
| 统一页面操作执行流程 | B1 恢复共享 H5 视觉复验；B5 的滚动仍经原 CaptureCoordinator |
| 受限命令与风险控制 | 原命令策略保留；未开放任意 Shell/JS/ADB，未放宽非滚动语义绑定 |
| Playwright CLI 页面观察能力 | CLI 版本/隔离/会话实现保留；B1 去掉无关资源的全局稳定阻断 |
| Playwright CLI 页面操作能力 | 不改 CLI 动作接管边界；修复共享 H5 交互入口的回归 |
| 有限度自由探索调度 | B4 在补采接入点沿用业务/清障分账、剩余预算和截止，核心调度器保留 |
| 页面与页面状态记录 | 页面身份与状态模型保留，不因补采额外建立业务页面 |
| 页面跳转路径与操作结果 | B5 不为当前 Surface Pin 伪造导航边；实际滚动重新采集后态 |
| 页面探索树和进度 | B2/B3 修正上游判定输入和终态归属；树/覆盖算法及冻结分母保留 |
| 页面路线查询接口 | 原只读路线查询保留；B5 修正新消费者错误拒绝 Pin/SCROLL 的兼容问题 |

本轮不覆盖前轮 F08 的 H5 聚合证据预检投影，也不补齐 F09 新消费者的 iOS 支持；不会据此宣布 tianshu 全部补采模块已经完成。正式补采仍遵守原 enabled/runtime_ready 和生产未就绪边界。

## 本地交付与最终检查

| 实际提交顺序 | 提交 | 结果 |
| --- | --- | --- |
| 方案 | `407a25ae` | 先冻结范围、回退/修正决策和逐批验证要求 |
| B1 | `ccbf53da` | H5 视觉保护与无关资源稳定限制的局部回退 |
| B3 | `69a6a16f` | 精确终态归属、同截图去重和耗尽工作收口 |
| B5 | `cec77907` | 原 Surface 就地观察和可信 SCROLL 消费 |
| B4 | `be86645d` | 原预算分账、异步截止、耗尽纯结算与并发保护 |
| B2 | `45734102` | 普通 Judge 回归保护、多角色与原显式证据子集兼容 |

最终业务代码检查时暂存区为空，工作树只剩开工前已有的 `.gitignore` 修改及历史未跟踪设计/审查材料。没有将它们混入任何修复提交；没有执行 push。原九项涉及的 13 个核心生产文件再次比对仍与 `270a1866` 字节一致。

本轮独立 `pgvector/pgvector:pg16` 测试容器已停止，未迁移或写入业务数据库。临时红绿日志、复现脚本和隔离基线副本保留在 `/tmp/alioth-guanxing-fixes-20260909/`；B2 补充日志位于前文列出的 `/tmp/alioth-b2-*.log`。测试通过数按命令范围列出，不将重复运行加总为独立覆盖率。

范围内 F02/F03/F04/F05/F06/F07/F10/F11/F12 已完成本轮代码回退或修复；F01 保留并验证父版本已有修复。F08/F09 仍属前述独立模块边界，未以本轮交付替它们签署验收。
