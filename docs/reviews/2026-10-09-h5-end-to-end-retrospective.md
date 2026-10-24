# H5 全链路复盘（2026-10-09）

## 范围与约束

- 本轮只读复盘，不修改实现、不启动质检、不重启 Worker、不修改历史数据库。
- 对象：本地 `h5-chain-reliability-and-entry-compatibility` 分支及其未提交修改。不能将此版本等同于测试环境部署版本。
- 排除外部网络、登录、验证码、供应商可用性；保留这些因素与软件问题的分类边界。
- 目标：解释真实执行链、重复失败的共同原因、有限的改进与验收范围，而不是增加一轮无边界修复。
- 历史成功百分比、局部回归通过、探索 Run 结束均不等于业务验收完成。

## 阅读说明

本次沿真实入口追踪了创建、探索、识别、候选、恢复、增量质检、补采与最终化的关键调用分支。下面保留初始分段记录，并增加综合结论。这里不宣称已经逐行读完仓库所有工具、SQL、测试及历史兼容分支；末尾单独列出阅读边界，避免再次把局部检查当作全量验收。

事实分为三层：历史快照中实际发生的结果、当前本地代码可确认的机制、尚未复现的根因假设。旧任务不能证明新修复失效，新回归也不能反过来证明旧任务已修好。

## 分段记录

本文是逐段复核的工作底稿。初始记录只覆盖下列已读取路径，后续补齐探索、证据、收尾与架构比较；不能据初始记录宣称所有分支均已审阅。

### 第一段：创建、执行入口与时限

已读取：`api/v2/tasks.py` 创建入口，`application/v2_task_entries.py` H5 分派，`application/task_submission.py` 创建主流程，`application/h5_task_runtime_settings.py`，`application/worker_execution_control.py`，`worker.py` H5 主执行与收尾分支。

已确认：

1. H5 full_inspection 任务锁定配置与规则集后进入 Task/Run 队列；不是前端直接调用浏览器。
2. Worker 的 `run_with_worker_controls` 包住 `dispatcher.dispatch_h5_v2`。其后的 Publication 等待、Snapshot 排空、规则结算和任务最终化在这一调用之外。入口：`worker.py:1852`、`worker.py:2056`。
3. 外层 deadline 从 Run 的 started_at 算；H5 Runtime 内部又从启动时刻计算自己的 deadline。不能把局部超时等同于整个 Task 的总截止时间。
4. 取消执行后会等待子任务退出，`worker_execution_control.py` 的 finally 内 gather 没有独立清理超时。这里只能确认边界风险，不能据此认定历史三小时任务的唯一原因。

### 第二段初步：探索与现场质检不是两个完全分离阶段

已读取：`application/h5_runtime_composition.py` 的 Executor、消费循环与完成感知 Explorer；`domain/exploration/h5_frontier.py` 的选择与排序。

已确认：

1. full_inspection 启动后先做当前页面质检检查点；随后每个已领取探索轮次之后再次做检查点。轮次也可能只完成恢复或刷新，不必然新增业务页面。见 `h5_runtime_composition.py:3231`、`:3454`。
2. 检查点可以消费质检 Work、处理 Probe、发起 targeted recapture。浏览器在同一任务中会在探索目标与规则取证目标之间转换。需要区分流程交错、逻辑状态漂移、真正并发操作，不能统称浏览器争抢。
3. Frontier 已有页型覆盖优先和清障优先，不是纯页面数量优先。但排序同时加入当前 lineage、本地证据、底栏、详情滚动、栏目等优先级，需要检查页型目标实际是否被前置规则覆盖。
4. 当前局部 Frontier 没有候选而仍有 overlay/obstruction 时，完成感知 Explorer 直接返回 blocked。这个报错是安全终止结果；真实原因可能在上游候选发现、校验、去重、恢复或预算，不能仅修改终止文案解决。

## 架构对照初步

已读取：2026-07-30 H5 Extension 设计；2026-08-13 Inspection Notification Chain 设计前半。

- 原设计明确保留 H5 捕获、识别、Frontier 与动作能力，不要求换成每次纯视觉点击。
- 原设计允许 `page_settle_complete=false` 但证据可用的降级识别，因此“加载未完成”不应一律废弃，“有截图”也不应一律认定为合格业务页。
- 8 月通知链设计明确探索留证后增量发布质检，Postgres 为事实源，Judge 不持浏览器。增量质检本身不是偏离设计。
- 该设计当时未包含 H5 recapture runtime；后续现场补采与关闭后恢复是新增复杂度，需对照后续设计判断其边界，而非假设一直是探索完再补采。

## 综合结论

核心问题不是完全缺少等待、弹窗、页型覆盖或超时策略。这些策略都存在。问题在于它们分散在采集、识别、Frontier、动作校验、补采与收尾多个控制层，局部安全和证据要求逐步挤占页型探索，任务级预算和结束合同又没有统一覆盖整条链。

可以收敛为六个问题域：可用画面判定、页型与规则状态收益排序、弹窗候选发现到执行的一致性、探索与补采阶段协调、任务级模型/时间预算、缺口与最终报告的有限收口。数据库历史错误另列，不混入外部环境故障。

## 真实执行链

```text
创建 H5 Task
  → 校验权限、冻结规则集/规则清单、页型目标、扫描模式与预算
  → PostgreSQL 同事务写 Task、初始 Main Run、Submission、Outbox
  → 队列唤醒 Worker，Worker 绑定 Run 用量上下文和取消/超时控制
  → H5 Runtime 建立 Surface Session，导航入口并保存不可变 Capture
  → 页面识别与 Assessment，Page Graph 写页面状态/动作边投影
  → Frontier 合并实际候选与历史未完成入口，选择一次动作
  → 命令校验、Session 租约、before Capture、动作、after Capture
  → 重新识别，记录真实到达，结算本轮，持久化后继 Work
  → full_inspection 当前页面检查点
       → 发布 Inspection Work；独立 Snapshot 质检可异步消费
       → 活会话 Judge / EvidenceRequest / Restoration / Probe / 重判
       → 某些 Snapshot 请求无法安全延后时，也阻止离页
  → 重新领取探索，同步补采后的 current_capture_id、重绑或恢复候选
  → 重复直到探索完成/预算耗尽/有界阻断/取消/异常
  → 冻结探索、关闭前排空、关闭 Session
  → Worker 等 Publication、排空 Snapshot、结算规则与工作项
  → 预览报告、正式报告内容、Completion Gate、原子报告绑定
```

关键区分：

- Page Graph 是 Capture/Action 的可重建事实投影，不是可随时导航到任意节点的地图。已截到一个页面，不等于有可信、可逆、低风险的恢复路径。
- Frontier 是下一步待做入口及状态；Page Graph 是已经实际发生的节点和边；两者不能互相冒充。
- Capture 数、Page Graph 状态数、确认独立业务页数、业务页型数、规则有效结论数是五种指标。
- 弹窗、加载、滚动、播放状态通常属于同一页面的状态变化，不自动成为新业务页型。
- Judge 读取不可变证据，不直接操作浏览器；实际补采由 Restoration/Probe/Capture Coordinator 执行。
- Inspection Work 的 `snapshot/active_session` 与它后来产生的 EvidenceRequest 的依赖要分别判断，不能只看 Work 的名字。

来源：`infrastructure/db/task_repository.py:218`、`domain/exploration/page_graph.py:141`、`infrastructure/db/page_graph_repository.py:65`、`application/h5_runtime_composition.py:3231`、`application/h5_live_inspection_publish.py:759`。

## 历史任务证据

### 数据范围

- 10 月 9 日停止后的只读诊断：`.alioth/runs/h5-verified-20261009-042245/diagnostic-after-user-stop-20261009-1813.json`。仅按六项当前 submitted 身份统计；旧尝试不与新尝试合并。
- 10 月 5 日保留的 27 个任务历史快照：`.alioth/backups/h5-failed-cleanup-20261005-204852/before-cleanup.json`。这是留存历史样本，不是全库全部 H5 任务，也不是当前部署表现。
- 所有数值是快照事实，不是本轮重新跑出的结果。不做八月胜出比较，不据此填写最终验收费用。

### 10 月 9 日六项当前身份

| 产品 | 图状态数 | 确认独立业务页 | after Capture 中业务页型 | 补证请求 | 恢复结果 | 主要结果 |
| --- | ---: | ---: | --- | --- | --- | --- |
| JHA-031 | 44 | 4 | home、content_list | 3 satisfied、11 unfulfillable | 9 equivalent、2 failed | Main 超时失败，Task review，未绑定正式报告 |
| JHA-128 | 94 | 4 | home、video_list、video_detail | 10 satisfied、28 unfulfillable | 15 equivalent、13 divergent | 当前页面质检/补采未排空导致退出，Task review，未绑定正式报告 |
| JHA-102 | 6 | 0 | 有 home 类型标签，但没有确认独立业务页 | 0 | 0 | 遮挡仍在且 Frontier 无候选，Task review |
| YC003 | 6 | 0 | 无；只有 startup_ad | 0 | 0 | 遮挡仍在且 Frontier 无候选，Task review |
| JHA-103 | 0 | 0 | 无 | 0 | 0 | 执行前外部取消，不能当软件失败或自然完成 |
| JHA-204 | 20 | 1 | home | 3 cancelled、2 unfulfillable | 3 equivalent | 用户停止后取消，不能当自然收口验收 |

上述业务页型列来自实际 after Capture 标签，排除了 startup_ad；标签存在仍不保证页面身份、质量和正式覆盖全部合格。JHA-128 的 94 个图状态不能解释成 94 个页面或 94 类页面。

这份诊断的七类资源未收口计数均为零，但 `all_settled=false`，六项 `report_bound=false`。资源排空与通过 Gate/正式报告绑定不是同一条件。

### 10 月 5 日旧样本重复问题

以下是 Run 错误出现次数，不是产品数，也不是根因已经确认的次数：

| 历史症状 | 次数 | 本轮解释 |
| --- | ---: | --- |
| Frontier 无候选但遮挡仍在 | 6 | 关闭候选发现、授权、验签或去重之后无法执行，需要回看原帧 |
| active-session / Snapshot 补证未排空 | 2 | 质检缺证可以反向阻断继续探索 |
| 来源恢复耗尽 | 2 | 图节点存在不等于旧页面状态能恢复 |
| LLM 调用预算耗尽 | 1 | 模型消耗不只来自业务导航 |
| 冻结墙钟预算耗尽 | 1 | 有时限，但收尾是否同受总时限仍是另一个问题 |
| LOGICAL_TARGET_NOT_UNIQUE | 1 | 语义目标不能唯一绑定实际控件 |
| SQL GroupingError | 5 | 明确的软件/查询问题；本轮未重新复现，不宣称现版已解决 |

同一旧样本动作记录还包含 12 次 `LOGICAL_SOURCE_STATE_STALE`、11 次 `LOGICAL_TRANSITION_UNVERIFIED`、7 次 `POPUP_DISMISSAL_NOT_FOUND`、15 次 `SEMANTIC_TARGET_NOT_FOUND`。这些是动作账本次数，不等于独立根因数。

主机越界、空白落点、HTTP 错误仅保留原记录，不纳入本次软件问题数量；没有重放前不能区分站点行为、环境故障和软件归因。不能为了消除越界错误放宽主机边界。

## 问题一：等待存在，但可用画面没有统一判定

### 当前实现

- 浏览器稳定等待有 15 秒采样窗口、连续稳定样本、核心结构、图片/媒体、加载器、过渡动画、DOM 安静和请求数量检查。
- 捕获后还有 inspection readiness：阻断型加载和稀疏画面拒绝；正文可见的局部加载、媒体动态、后台请求可降级为可用。
- Detector 对不足画面可能在调用 AI 前返回 insufficient；对有业务结构或 inspection ready 的画面允许降级 AI 识别。
- Frontier 又分别维护 line_loading、开屏倒计时、Flutter 启动、逻辑状态、能力投影和 insufficient 的重观察额度。
- 页面完成判定拒绝加载、过渡、遮挡等临时态；所以不能说代码完全把 loading 当正式页面。

### 没做到位

“已截图”“DOM 稳定”“可交互”“可以检查这条规则”“可计入页型覆盖”分散判定，并不是一套统一状态。某帧被上游判为足够、下游仍可能无可信候选；另一帧画面明显可识别却可能因结构不足在 AI 前被拦住。历史每个实例仍须用原截图核实，不能凭日志断言视觉误判。

检测失败后对同一 Capture 重新解析不等于等待页面加载；等待必须产生新的实际观察。`prepare_after_action` 将观察动作或状态指纹变化计为 progressed，也不等于新增业务类型或新规则证据。

### 建议合同（未实施）

统一四种状态：临时等待、可清障、可探索但证据不完整、可判定。轻量采样先处理明显 loading；模糊时让 AI 基于新画面判一次并选择有限等待。每次等待占用同一任务时限，不能每次重观察续期。无效临时帧保留诊断，不增加有效覆盖，也不触发普通全量规则质检。

不是“一律等到所有网络空闲”，视频与广告可能永远动态；要看当前探索/规则目标所需的内容是否可用。

来源：`infrastructure/browser/page_capture.py:3747`、`:4398`、`:4510`；`agents/tools/page_detection_tools.py:3038`、`:4801`；`application/h5_exploration_observation.py:175`、`:540`；`application/h5_exploration_workflow.py:811`。

## 问题二：覆盖更多页型不是当前排序的第一业务目标

### 当前实现

有冻结 required_page_types、真实观察的类型记录、缺口、深度、动作/页面上限和 70/20/10 队列策略。实际到达类型不符时会记录 observed 类型，而不是直接把 expected 类型算成功。

但 Task Frontier 排序先比较 lineage、清障、详情滚动、结构分支、底栏、列表滚动、横向收集、本地详情取证与返回，再比较 scheduler；未覆盖页型还在后面的比较项。已经有“页型优先”不等于它在整个排序中是第一优先。

### 没做到位与建议

应把安全清障作为前置条件，然后以“新增页型 + 新规则关键状态/证据收益”作为主要收益，以恢复、AI、动作和时间作为成本。不能用多点几个同类详情来抵消支付/设置/活动等类别缺口。

同类页仍有价值，例如不同支付方式、登录前后、详情播放/失效、弹窗清除前后、列表深处的规则证据。但必须说明新增什么规则能力或状态，达到代表性取证后让出额度给其他页型。不是机械规定每类只访问一页，也不是追求全部链接。

来源：`domain/exploration/h5_frontier.py:1456`；`application/bounded_exploration_scheduler.py:49`、`:629`；`application/h5_exploration_workflow.py:1049`。

## 问题三：弹窗策略仍在，候选到执行之间过于脆弱

### 现有策略没有消失

平台检查可见 modal/高层遮挡、完整弹窗普查、真实关闭语义、命中、唯一性、低风险和状态签名；还有多层业务弹窗最多 20 轮、重复签名/重观察 3 次、连续失败 4 次等有界策略。

AI 负责识别与提出语义候选，不直接得到任意 selector、脚本或自由坐标执行权。当前额外视觉导航只适用于模式允许的 Flutter/hybrid、语义稀疏且稳定无遮挡场景；它不是通用“看见 X 就点击”的弹窗兜底。

执行解析同时保留通用 modal 证明和注册模板分支，如 van-dialog/van-popup、特定关闭类名与文本能力。准确描述应是“AI 语义识别 + 通用结构证明 + 模板能力 + 动作前校验”，不是纯视觉智能，也不能仅凭这些组件模式认定做了某产品编号特判。检测端能提出关闭动作，执行端还必须用同一帧证明匹配；两端的能力覆盖不一致就会失败。

### 报错的准确含义

`H5 Frontier 没有可执行候选，但当前页面仍存在活动遮挡` 是末端安全拒绝，不是一个单一根因。上游可能发生：关闭按钮漏识别或在面板外、同语义控件不唯一、签名因异步弹层变化失效、候选在失败/循环策略中被移除、底页结构透出但不能授权操作。不得只放宽终止条件，否则可能点击广告、授权或付款。

对照代码：`H5CompletionAwareExplorer.decide_next` 在局部无候选而仍遮挡时直接 blocked，并不会保证再调用 AI 重新规划。视觉导航分支本身也拒绝遮挡态。因此“已有 AI”并不代表在这一失败出口还能智能发现关闭按钮。

### 建议合同（未实施）

对当前最上层弹窗统一执行“新帧普查 → 语义判断 → 唯一且可命中的安全关闭目标 → 动作前新帧校验 → 原层消失/下一层验证”。旧候选失效时撤销后重新发现，不拿旧签名/坐标继续点，也不反复重开页面复活弹窗。有限智能兜底只补识别与重新定位，安全界限不取消。

来源：`application/h5_modal_close.py:1`；`application/h5_popup_loop_policy.py:1`；`agents/tools/page_detection_tools.py:9127`、`:2761`；`infrastructure/browser/semantic_target_resolver.py:1446`、`:3198`；`application/h5_runtime_composition.py:437`。

## 问题四：补采和探索不是无序并发，而是交错和状态恢复成本

### 当前互斥与同步

生产 Capture Runtime 使用 PostgreSQL SessionCommandGate，按 Task/Session/Epoch 和命令序列取得租约；释放命令时，只接受对应命令的真实 Capture 更新 current_capture_id。不能笼统说“没有互斥、两个流程同时乱点”。

但 full_inspection 每个探索动作后的检查点会运行 Judge、Probe、targeted recapture；请求还未满足时会等待、恢复或终止本轮探索。下一轮 synchronize 必须读取新 Capture、再识别、重绑；跨页或目标消失时走来源恢复。这里存在真实的状态交接和逻辑目标漂移成本。

Snapshot 判断可以不占浏览器；Snapshot 判断产生的补采请求则可能需要浏览器。是否延后必须看请求的重放允许、等价、来源等级和可信路线。历史 JHA-128 就在这一屏障退出。

### 当前已有防护，但不等于链路有效

- 同步补采后的新现场、旧弹窗消失时改选入口、恢复后回检查点等保护在本地代码中存在。
- targeted_recapture 恢复 Capture 被专项规则限制，不能普通全规则再发布，目的正是防止递归补采；不能说系统毫无去重。
- Probe 成功仍需 Supplement/Manifest/InputSet 挂接和原规则重判，不能只以“动作成功”“恢复等价”表示规则覆盖完成。
- read-only 缺证不一定可重现。旧样本有 alioth_23 请求：目标为广告加载失败状态，绑定稳定 home、required_interaction=null、只允许重新观察。反复截图不能保证触发那个条件。
- 当前 `h5_evidence_reachability_boundary.py` 只明确拦截权限锁定态关闭底页的一类请求，并不是通用可达性规划器。

### 建议合同（未实施）

保留增量保存和不可变证据 Judge，但先区分：必须趁当前现场取得的短动作证据、可安全延后的可恢复证据、未触发/不可复现/不允许执行的条件证据。按 Capture/目标/所缺证据合并可兼容请求；相同输入和无新证据不重判。补采不可达要有明确缺口终态，不能持续阻塞所有新页型。

安全互斥只管一次命令；阶段协调还必须管“当前浏览器是谁的业务目标”和“交接后是否重建 Frontier”。这是不同层的问题。

来源：`infrastructure/db/surface_session_repository.py:442`、`:602`；`application/h5_live_inspection_publish.py:759`；`application/h5_runtime_composition.py:1215`；`application/h5_targeted_recapture.py:211`、`:818`；`application/h5_probe_work.py:316`；`application/h5_evidence_reachability_boundary.py:33`。

## 问题五：预算不是全任务统一约束，消耗也不等于导航进展

full 默认上限为 80 个探索动作、200 次 AI 调用，并取冻结配置中更严格的值；Token 上限是可选。截图识别、动作后识别、来源恢复后识别、Probe 目标规划、规则判定/重判都可能调用模型。一次业务导航不等于一次调用。

已有 accepted Assessment 复用、部分检测缓存、正式结果恢复、规则复用计划和幂等工作发布。不能建议“加缓存”就视为原来没有。不同状态的真实证据也不能直接复用旧判定。

当前 H5 Executor 在单轮前后检查计量，审计读取按 agent_run_id。独立 Snapshot Consumer 有自己的 rule_judge Run；活会话消费又可能使用 Main Run。因此不能把 Main Run 的 200 次上限等同于全 Task 所有消费者总限额，更不能从 Run 错误文案推算完整 Token 或费用。

单轮内部可有多次调用，当前循环检查不是每次调用前的全任务原子预留。这里是边界不足，不是本轮已经测出某次超额的数量。局部 recapture 的三动作/180 秒预算也不能独自保证所有请求累计成本受限；关闭后补采另有原 Run 剩余预算核查，不应一概说补采无限制。

建议：统一 Task/本次执行预算账户，调用前预留、调用后结算，覆盖探索和 Consumer；按识别/清障/恢复/补采/Judge 分账；轻量等待不每次调用模型；新页型和新证据收益优先。保留已采用的复用机制，只补其失效边界。不提高总预算来掩盖重复消耗。

来源：`application/h5_task_runtime_settings.py:1`；`application/h5_runtime_composition.py:3520`、`:5090`；`infrastructure/db/llm_usage_event_repository.py:219`；`infrastructure/db/h5_inspection_publication_repository.py:1161`；`agents/runtime/inspection_work_item_consumer.py:731`、`:914`。

## 问题六：各局部有界，不代表任务端到端在同一时限内结束

### 已确认的具体路径

1. Worker 超时控制包住 dispatch；正常返回后的 Publication/Snapshot/规则最终化/报告不在这个 operation 内。
2. `_finalize_rule_executions` 为 H5 单独选择 5400 秒等待，即最多再等 90 分钟的窗口；有进展就继续等待，无进展按宽限窗退出。
3. `_wait_for_inflight_inspection_work` 的 waited/stalled 只累计 sleep，不含数据库查询耗时，所以不是严格墙钟上限。
4. Runtime 与 Probe/关闭后补采已有 deadline，不能说没有超时。取消后的 gather/await cleanup 却没有独立清理硬时限，协作取消如果不能及时退出仍可能拖延。
5. 最终报告/原子最终化失败会保留可恢复状态；资源已排空仍可能 review/session_draining、没有正式绑定报告。

这是当前代码可确认的长链路风险。测试环境三小时任务的代码版本未核对，不能宣称“正好 90+90 分钟”就是它的真实根因。

另：`task_finalization_repository.py` 中“三次相同错误隔离”当前所读 SQL 明确限 Web V2，不可借此声称 H5 也有同等总重试隔离。

### 建议合同（未实施）

在创建任务时冻结一次总 deadline，探索、补采、Judge、收尾都用剩余时间，留出固定清理窗口。预算耗尽或无真实收益时停止产生新任务/新请求；只做有界结算与资源回收。剩余规则按已确认结论、证据不足、未触发、目标未到达等真实原因说明，不伪造成功、不降低 Gate。

区分三个维度：执行结束、覆盖/证据完整、规则通过。任务可以“已经结束但有缺口”；不能为了显示 100% 伪造规则结论。当前 task_progress 的规则分母会影响进度显示，因此若调整展示也必须先明确此区分，而不是改数据库成功状态。

来源：`application/worker_execution_control.py:1`；`worker.py:1852`、`:3556`、`:3768`、`:3960`、`:1223`；`application/h5_probe_work.py:500`；`application/h5_closed_session_recapture.py:1`；`domain/task/completion.py:214`；`application/task_progress.py:229`。

## 原架构是否偏离

### 确认存在过两种阶段约定

- 7 月 30 日 H5 扩展设计允许非完全稳定但视觉证据可用的降级识别，并保留安全候选执行。
- 8 月 13 日通知链设计允许增量发布、独立 Consumer；Judge 不持浏览器。
- 8 月 15 日纯探索设计明确：Explorer 不接收 EvidenceRequest/Probe/Judge 结果；探索封存后独立质检，不反向改变本次探索。
- 8 月 15 日弹窗设计明确逐层“关闭后新 Capture、新识别”，最多 20 轮，不是随意猜关闭键。
- 8 月 28 日逻辑页面设计区分业务身份与状态，反对继续在大检测器堆 SPA/Flutter 分支。
- 9 月 16 日及 10 月专项规则又明确接通活会话补采、来源恢复、离页检查点及可恢复 Snapshot 延后。

### 判断

当前底层仍遵循不可变 Capture、独立 Judge、PG 事实、动作审计和 Gate；不是全盘换成错误架构。

但 full_inspection 的实际生命周期不符合 8 月 15 日“探索封存后质检、不反向影响探索”的隔离合同。后续专项规则确实主动引入了现场补采，因此这是多次设计演进后的合同不一致，不能仅凭旧文档断言全部属于未授权误改。

最需要先定的不是是否再换框架，而是一个阶段合同：探索保留浏览器所有权；静态 Judge 可异步；只有无法延后的短现场取证在离页前做；复杂历史恢复统一在有限补证阶段做。若用户选择严格“全探索结束再质检”，必须接受一部分只存在于原现场的动态证据不能回收，不能把纯阶段化当作零代价方案。

现有 capture/detection/composition 文件均很大，专项与身份校验多处交叉，维护成本明显偏高；文件大小本身不是软件失败证据。本轮不据此建议大重构。

## 已有修复与未证明事项

| 能力/问题 | 本轮确认的级别 | 不应宣称的结论 |
| --- | --- | --- |
| 等待、局部加载降级、重观察上限 | 当前实现存在 | 所有慢页已正确识别 |
| 页型目标、真实 after 到达、未知入口保留 | 当前实现存在 | 页型收益已是全局首要排序 |
| 弹窗逐层处理、普查、语义安全校验 | 当前实现存在 | 明显弹窗已全部可关闭 |
| 补采后同步、旧弹窗消失时保留新入口 | 当前实现和专项记录存在 | 旧失败任务已被新代码修好 |
| 来源恢复后先回检查点、异步新弹窗重新识别 | 当前实现/专项记录及历史组件回归记录 | 当前六产品新版本已闭环 |
| 内层滚动选择、滚动过程证据合并 | 当前专项记录和相关实现存在 | 截图已被每条相关规则实际消费 |
| Publication 幂等、请求/规则终态结算 | 当前关键路径存在 | Gate 一致性及正式报告全部通过 |
| SQL GroupingError | 旧任务确有记录 | 当前版本已修复或仍复现 |
| 统一 Task 总预算、严格端到端时限、统一阶段合同 | 当前所读路径未形成完整保证 | 提高预算/多等一下即可解决 |

本轮未运行测试，历史测试通过数量只作为组件修复记录，不再次计为本轮验证。运行在修复前的任务和修复后新 submitted 必须分开；取消任务不能补算自然完成。

## 有限的下一步建议（待逐项讨论，不执行）

1. 先定阶段与结束合同，明确总时限、清理窗口、无收益停止、现场补采的范围；验证超时、取消、慢 Judge、无法恢复均在限时内有解释终态。
2. 统一“画面可用”与弹窗闭环；用慢页、局部加载、多层/框外关闭、异步遮挡四类已有失败证据验证，不新建一套万能浏览器。
3. 调整页型/规则状态收益排序并限制同类采样；验证可达新页型不会被滚动和旧快照恢复饿死。
4. 合并可兼容缺证目标，拒绝不可触发/不可执行的补采；验证原位补证、新会话恢复、条件未触发分别有明确结算，不循环生成新请求。
5. 接好统一 Task 模型/时间预算与正式报告；仅在前四项合同确认后再讨论真实任务验收。

固定验收集应至少覆盖：慢 loading、局部 loading、多层弹窗、异步签名变化、框外关闭、同类页面去重与新页型选择、现场/历史补采、deadline/取消/关闭。每个问题只要求对应失败证据的闭环；新出现但不在本轮范围的问题单列，不无限追加到同一轮修复。

真实验收不得保证不存在或被安全边界阻止的页型“全部达到”；所有冻结规则必须有处理记录或明确缺口，但“检查过/证据不足”不是“有有效结论”。平台成功率仍为 `(passed + failed + confirmed_not_applicable) / total`，不把 skipped 自动当 confirmed_not_applicable。

## 阅读索引与剩余边界

| 分段 | 已读取的关键路径 |
| --- | --- |
| 创建与冻结 | api/v2/tasks、v2_task_entries、task_submission、task_repository 的原子创建及清单冻结、h5_task_runtime_settings |
| 启动与控制 | Worker H5 处理、worker_execution_control、Runtime Factory/Executor、纯探索/full 模式分流 |
| 页面与候选 | page_capture 的稳定采样/readiness、page_detection 的主流程/缓存/瞬态/弹窗/视觉门禁、semantic_target_resolver 的关闭能力/模板/来源验签、observation/frontier/scheduler |
| 单轮与恢复 | exploration_turn 的领取/租约/执行/后继/异常、workflow 的启动/动作后/失败延续、source synchronize/restore、浏览器丢失恢复主路径 |
| 事实与互斥 | PageIdentity/State/Transition、Graph 投影、Capture/Manifest/Graph 同事务写入、Capture Coordinator、Session command acquire/release、探索专用离场门禁 |
| 质检与补采 | live publish/checkpoint/drain、Consumer 的复用/Judge/写回/超时、Publication 消费身份、Request admission/reachability、targeted resolver/drain、Probe/新会话恢复 |
| 收尾与报告 | Snapshot drain、规则等待/补记、Work 最终化、CompletionGateFacts、清单 Gate、报告生成/原子绑定入口、进度计算 |
| 架构与历史 | 上述六类设计文档、专项高频回归条款、10 月 5 日快照和 10 月 9 日六项诊断 |

仍未逐行审完：大型采集/检测文件的全部辅助函数、所有 Publication/恢复 SQL、认证外部适配、全部报告内容生成器、V1/非 H5 兼容分支、全测试目录，以及全库所有历史任务。本次足以确认上述执行结构和具体边界，不足以宣称“所有代码无遗漏”或给每次历史失败确定唯一根因。

后续如果继续复盘，按问题域读取这些剩余路径并补证；不启动质检、不以阅读为名悄悄修代码。本文作为分段接续记录，已有结论必须带原代码与历史证据范围，避免依赖聊天印象。
