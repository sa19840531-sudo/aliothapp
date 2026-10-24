# App 年龄门 qa-a 真机复核阶段报告

日期：2026-09-12 至 2026-09-14。本文时间均为 UTC+08:00。

## 当前阶段：按用户要求停止 R33，使用最新代码启动 R34 并定期汇报探索树

- 用户两次明确纠正：移除白名单／定制化适配，按新的有限自由探索设计确认并修复后才继续测试。仅切换 `dynamic_discovery` 参数不足以完成此要求。
- R26 因要求变更已于 10:57 正式取消；11:00:53 Surface closed、qa-a lease released，60 个检查终结。取消时 Cmd10 结果未知属于取消影响，不能记成产品功能失败。R27 已在两批修复验证及提交后，于 11:51:49 使用[动态请求](2026-09-13-qaa-dynamic-discovery-request.json)正式提交。
- 审计已确认：提交边界按包名自动命中产品 profile；底栏发现按 APK／包名回退 playbook 并过滤或插值入口；流程完成判定由 playbook 注入固定底栏；访问进度用定制别名袋把多个不同入口混为一项。本次已沿运行链移除这些定制行为，并补齐动态决策回归。
- R32 未通过 Gate；审计后修复聚合恢复状态、固定标签页型映射、UIA 入口提示、确定性批次重复取证及动态范围固定业务要求。R33 于 9 月 14 日 05:48 启动、08:53 自然封存并释放设备，结果仍为证据不足。检查收口及本轮发现的误拦截审计继续进行，未通过设备验收。
- 后文固定六类分母只描述历史 Run，不再作为新探索目标。当前截图／UIA、有限预算、当前目标重验、到达证据及作用域仍按新设计各自承担职责。

### R34：最新代码重跑（9 月 14 日 10:43）

- 用户明确要求停止当前轮次、使用最新代码重跑并定期输出探索树。R33 主 Run 经正式取消接口转 canceled；逻辑检查 Run 不在该接口队列中，直接取消返回 404。确认 Task 已 canceled 且无 running／judged Attempt 后，使用现有检查仓储取消剩余 **95 项**非终态 Work，再通过既有消费 Run 收口方法关闭子 Run。该操作属于用户主动中断检查，不能将其记为检查通过；此前 08:53 的探索自然封存事实不变。
- 重新启动前，PG 确认 active Run／lease／Surface 均为空，迁移版本 `20260912_0112`。仅重启 8030 API／Worker 为 PID `58230`／`58231`，加载已合并并推送的 `b00145ba`；readyz 与认证预检通过，未改动 qa-b／8020。
- POST `/api/v2/tasks` 返回 202：Task `c5938e44-69ef-4907-9364-37422b4362b3`，Run `ad759656-8249-4a03-8d5d-3fe69dacabcf`。沿用原 build_ref、qa-a、330 动作／180 分钟，继续 dynamic_discovery，不增加固定栏目或页型要求、不购买付费。
- 10:43 Task／Run running，qa-a 租约 active、Surface starting，尚无 Capture 或动作。后续每 **10 分钟**汇报探索树，区分实际到达路径、仅观察节点、候选／待探索及阻断分支；影子图与 Frontier 状态不能替代同次动作的业务到达证明。

## 去定制修复第一批：提交边界

- `build_app_exploration_target_snapshot` 移除按包名自动 `registry.resolve`。新任务即使命中已知包，也按动态发现生成目标；显式 `profile_id` 才读取画像，旧快照仍可回读。目标包边界与 Target 内容哈希保持有效。
- V2 提交服务真实 `task_config` 回归确认 `profile_id=null`、`applicability_mode=dynamic_discovery`、`completion_mode=dynamic_discovery`，不会从 generic5 等画像带入固定页型子集与数量。
- 提交入口、目标快照及 profile 三个测试文件共 **84 passed**，Ruff 与差异空白检查通过。无数据库迁移，无前端菜单／路由变化。运行期候选、底栏进度和决策限制仍在第二批修复，因此此时尚未恢复真机测试。

## 去定制修复第二批：运行期候选、进度与决策

本批以 [有限探索设计 §6](../design/2026-09-02-bounded-exploration-page-state-graph-design.md) 的当前观察、覆盖／发现／恢复调度和新后态证明为依据。页面目录仅用于识别、排序与报告；产品模板不得改变真实入口的准入或伪造存在性。

- 底栏生成移除 APK／包名 playbook 回退、模板标签交集、模板顺序与空槽坐标插值。已知与未知包均保留当前实际发现的标签；选中态只读当前像素／UIA 事实。全局导航范围本身不等于底栏，顶栏仍作为普通当前导航候选保留。
- `app_flow_contract` 升至 v2，底栏证据只累计受信 UIA 已观察入口并集，后续新观察继续加入；Vision 入口仍由 Frontier 管理。旧模板不再注入分母；v1 已保存完成字段仍按原事实回读，后续更新可迁移实际观察，不改写历史封存记录。此流程证据不能代替完整探索终态与 Gate。
- 删除首页／主页、视频／短剧、暗网／漫画等产品别名袋。访问、尝试、清零与耗尽仅共享同一实际标签的表示归一，避免一个入口被访问或失败就抑制另一入口。
- 动态模型决策移除“底栏未完就禁止内容／宫格”的候选裁剪及强制先后序；详情、返回、编辑入口和规则取证候选交给现有有限调度。越包恢复和有当前证据的清障继续保留，业务到达仍需实际 After。
- 两张真实已持久化 Capture `66fda932-648a-5ade-9b08-23d2718252f5`、`b820885e-2b04-5c30-975e-8c63c825a873` 只读回放保留 17／19 个混合候选。顶栏色图／小说保留自己的当前来源，均不再标为底栏；未调用设备或重写原始观察。两帧来自旧 R25，尚无修复后 UIA 底栏投影，不能据该回放宣称新 Run 已完成底栏或 Gate。
- 实际 R27 草稿经 V2 命令与动态策略构建核实：`dynamic_discovery`、`profile_id=null`、`dynamic_frontier`、`playbook_id=null`、`curriculum_progress=null`、`active_tabs=[]`、`page_limit=null`，资源仍为 330 动作／10800 秒。通用 11 类目录不形成新任务的固定必达页面名单；动态终态不要求未发现目录项全部变成已覆盖。

第二批主工作区验证已完成并提交为 `92f00139`；提交边界第一批为 `eccab366`，之后才恢复实机。无新增数据库迁移、前端菜单或路由变更。

- 16 个相关测试文件共 **709 passed、20 failed**，覆盖 V2 入口、目标、候选生成、进度、截图闭环、全局 Frontier、有限调度、MAF、年龄清障、交互执行与运行装配。新增／调整用例均通过；MAF 文件为 76 passed。
- 20 项失败以修改前 `de61b331` 的生产源码在独立 Python 进程逐项复现，失败断言一致：coverage page seek 8 项、global frontier 2 项、screenshot exploration 7 项、runtime composition 1 项、mode parity 2 项。保留为既有回归问题，整组并非全绿。
- Fake 闭环补齐真实 `InteractionCommandValidator` 与 Broker 的 `resolved_candidate_id` 上下文。明确验证交互 succeeded、当前 Before 候选、Driver execute 和 click trace，避免动作失败时只凭 After／后继 Work 通过。该闭环现已通过；旧模板分母曾掩盖更后面的执行断言。
- 修改文件 Ruff 通过；`app_screenshot_exploration.py` 保留修改前已有的 I001／E402 两项导入布局问题，排除这两项后的检查通过。`git diff --check` 通过。


## R27：修复后实际运行模式已核实

- 11:51:22 隔离 API `/api/v1/readyz` 四项依赖正常，认证成功；新 API／Worker PID 为 `94401`／`94402`，加载代码 `92f00139`，数据库仍为 `20260912_0112`。重启前已确认该隔离库无活动 Run／lease。
- 11:51:49 `POST /api/v2/tasks` 返回 202。Task `89a409c9-5ada-48f8-b0b3-308b58bf0372`，Run `6fb016e2-ff29-479b-a166-8c083d2e2fd3`，仍使用同一 build_ref 和 qa-a。
- 11:54 实际已持久化 Target、Goal 与首个 Work 三者均为动态发现；`profile_id=null`、`exploration_mode=dynamic_frontier`、`playbook_id=null`、`active_tabs=[]`、`curriculum_progress=null`、`required_action_count=0`。保留 330 动作／10800 秒资源预算。
- 服务端默认抽样配置将实际 policy 的 `page_limit` 写为 100；同时 `quantity_unbounded=true`，数量合同明确其角色为 `optional_safety_hint_not_completion`，禁止以 page_count_cap／固定前 N 项声明完成。此前纯内存草稿预检的该字段为 null，两者需区分；实际运行不是固定 100 页的验收目标。
- 11:53:05 已取得 Bootstrap Capture `04b4c6e6-0bb3-4acf-aca6-f882d5157dcf`，当前识别为开屏广告弹层。11:54:14 Task／Run running、Surface active、qa-a lease active，关闭动作已规划，尚无本轮关闭成功的 After 证明。
- 后续已跟进到实际业务页面及重复返回阻断；本轮已取消，不能据动态配置、动作成功或规则检查全部终结宣称完成验收。

### R27 实际进展与取消收尾

- 连续广告队列经过逐次关闭，于 12:38:37 的 After `ea00c421-0217-541b-8874-731745bcb469` 露出首页主体；随后处理页脚广告。不能将不同广告素材简单视为同一弹层卡死，也不能把首页露出直接当作全部探索完成。
- 后续实际观察到稳定首页、个人页、AI 内容列表和社区列表；“我已保存”关闭取得 After。短剧曾进入详情／加载及解锁遮罩，缺少实际播放证明；设置重绑失败。全局 Frontier 最终只有 4 项 visited，不能将页型观察数量等同于已验证业务覆盖。
- Cmd23 进入会员解锁落地页。Cmd24、25 两次 BACK 后仍停留在该页；Cmd25 After `7636e0c7-eb3b-5f56-a9d6-1ef954b4aa2a` 的实际图像仍为会员页，视觉摘要的首页分类不能证明已返回。Cmd26 关闭候选重验失败，Cmd27 再次安排“返回关闭付费半屏”，已形成重复恢复阻断。
- 13:35:14 通过正式取消接口停止本轮。Cmd27 结果为 `APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION`，不认定设备动作已执行，也不记为产品功能失败。13:35:54 qa-a lease released；13:40:49 复核 Task／Run canceled、Surface closed，Work 25 succeeded／1 cancelled，Frontier 4 visited／1 failed／24 cancelled。
- 同 Run 的 195 个检查已全部终结、failed 为 0，但 Gate 仍为空；这是取消收尾事实，不是 Completion Gate 通过。
- 阻断时 Work 中动作消耗已累计，`no_progress_count`、`return_repeat_count` 却仍为 0，`last_return` 为空。候选生成与调度记账根因及修复见下；修复、回归和本地提交完成前不启动下一轮。

### R27 阻断修复：候选、外层阶段机与进展记账

- 固定付费返回并非此次 dynamic MAF 分支直接生成。截图候选富化仍会合成／重标记“返回关闭付费半屏”，并写入固定首页预期；MAF 外层 `SurfacePhaseAwareExplorer` 又按 payment/paywall 优先选择 BACK，绕过普通动态决策。当前修复从候选生成与外层阶段机两处移除动态模式的该路径，保留普通真实 BACK、当前关闭及其他安全导航。
- Cmd24／25 的同次动作证明均为 unproven，但新截图 hash 被传为 scheduler progressed，导致无进展清零。Cmd26 重验失败没有实际动作候选，新截图仍清零；其空返回签名还会清空旧返回周期历史。修复让新链路使用动作后语义证明，空签名仅在真实进展时清空周期历史，无进展时保留；不修改现有预算或终态阈值。
- R27 两张实际 Capture `7636e0c7-eb3b-5f56-a9d6-1ef954b4aa2a`、`1b0925c8-31e0-535f-8be2-c55a81829cba` 只读重建分别保留 13／12 个候选，付费专属标记均为 0，阶段机均未选择 BACK。第二帧仍保留两个实际普通 BACK；第一帧的当前关闭仍可被选中。回放不触碰设备、不改写原始观察，也不证明设备已离开会员页。
- 触控前重观察没有 Work 策略参数，因此统一不补造付费返回候选，只重绑当前真实候选；不依赖进程内会话缓存或模型自报运行策略，Worker 重启也不会重新开启这条 Before 合成路径。Bootstrap／后继按可信运行策略处理，原有包外恢复仍保留。
- 最终主工作区 App 相关 13 文件 **711 passed／22 failed**；22 项失败以修改前 `c5acc4b1` 的三个生产模块在独立进程逐项复现，失败原因和断言一致：截图闭环 7 项、App Turn 4 项、Global Frontier 2 项、Runtime Composition 1 项、Coverage Page Seek 8 项。旧式导航后态、模板数量与恢复夹具问题仍存在，整组并非全绿。
- 共用调度器的 Web／H5 与守卫四文件 **199 passed**。上述两组共 17 文件、910 passed／22 个基线失败；73 项针对性测试已包含于 App 组，不重复累计。修改文件 Ruff 通过，截图模块仅排除已有 I001／E402；`git diff --check` 通过。
- 本批无数据库迁移、前端菜单或路由变化；共用返回历史行为已有 Web／H5 回归，但修复后实机到达与最终 Gate 仍须新 Run 验证。

### R28 修复后重新提交

- 修复提交 `7858b0cc`。重启前确认隔离库 active Run／lease 均为 0；仅重启 8030 API／Worker，新 PID 为 1549／1550。14:05:35 readyz 的 PostgreSQL、Redis、证据存储与 Worker 四项正常，认证返回 200。
- 14:06 正式 V2 提交返回 202：Task `906dd527-9890-410c-a984-51771572a1b7`，Run `29e5d455-822c-4f7b-810a-0cc06a8e6f72`。仍为同 build_ref、qa-a、330 动作／10800 秒和 `dynamic_discovery`，不注入固定页型、数量或栏目要求。
- 14:06:35 Task／Run running、qa-a lease active、Surface starting，尚无 Capture、动作与 Gate。持久化 Target 已确认 `profile_id=null`、`completion_mode=dynamic_discovery`；此时 Work 尚未创建，实际 Goal／运行策略要待后续 Work 核实，不能把空 Work 当作已确认。
- 14:07:49 已取得 Bootstrap Capture `bf21d1f5-9e8e-4944-9c74-3426708fd7a3`；14:07:57 Surface active，视觉理解与首个 Work 尚未就绪，尚无实际动作后态或 Gate。
- 已将自动跟进切换至 R28，仅在重要进展、真实故障、完成或需要用户操作时通知。启动受理和目标快照不代表实机探索完成或 Completion Gate 通过。

### R28 清障与播放观察进展

- 14:11 已核实实际 Work：Goal 为 `dynamic_discovery`，策略为 `dynamic_frontier`，画像／Playbook／课纲均为空，`active_tabs=[]`、`required_action_count=0`、`quantity_unbounded=true`。默认 `page_limit=100` 仍仅为 `optional_safety_hint_not_completion`，不是固定数量验收目标。
- Cmd14 After `1f5c956e-5dc9-5935-9e47-4573da4e657f`（14:50:24）实际图像证明中央推广已移除；随后 Cmd15 关闭页脚，After 为 home/stable。此前 Cmd12 的会员推广仍覆盖主体，不能根据该帧 popup_visible=false 或后续 stable 分类提前宣称清障。
- Cmd16 短剧导航后为 video_detail/loading，原 Frontier 项进入 retry_wait；Cmd17 选择观察加载完成。该次 Before `6a55d7a2-aeb4-562e-915d-76a9d7f41f08`（14:59:51）与 After `d480217f-b4db-5f7f-8a67-dde8d50e271f`（15:00:59）均已人工查看，播放进度从 **01:18／04:45** 推进至 **02:24／04:45**，画面同步变化，取得实际播放推进证据。
- 15:02:47 Task／Run 仍 running，Surface 与租约 active，Work 15 succeeded／1 running；107 个规则检查全终结、failed 为 0，Gate 仍为空。观察到播放推进不能代替原导航项的 Frontier 结算、完整业务探索或 Completion Gate。

### R28 自然停止与收尾

- Cmd20 社区、Cmd22 AI、Cmd23 主页依次完成，Frontier 累计 3 项 visited。Cmd24 进入会员解锁页面后，Cmd25 选择当前关闭候选，未重复 R27 的强制付费 BACK 路径，但实际点击存在错误。
- 15:36:52 探索结果自然封存为 `evidence_insufficient`，原因 `app_repeated_business_page_plateau`，并非人工取消。证据索引为 `app-evidence-index:9de48f47dea5b93e573591c4cc7f11203f70eb6de429142cb3f70fd1ba6e44dc`。
- 封存摘要记录覆盖 home／profile／post_list／video_detail 共 4 类，仍有 7 个 eligible candidate，`frontier_exhausted=false`；无新业务页动作计数为 6、重复业务页动作计数为 3。其余 27 个 Frontier 项随收尾取消，不能据此声明探索候选已经耗尽。该停滞阈值的具体计数依据仍在审计。
- 15:36:56 qa-a lease released，Surface closed。15:43:14 Task／Run 仍 running，Work 23 succeeded／1 no_progress；405 个规则检查尚有 150 个未终结、failed 为 0，Completion Gate 尚未生成。探索封存、设备释放和最终检查结算分别记录。

### R28 两条已确认问题

- Cmd25 的原关闭候选中心约为 (1359.5, 2602.5)，当前 Before `a4490be2-b0b3-5c40-8c35-f87980b9f98c` 重绑后变为 (1346.4, 2720.8)。官方 Action Trace `dbbcc719-90c1-54c2-8d09-81fbc8616dc7` 记录实际点击 (1346, 2721)，落在“我的”底栏范围 x=1172..1398、y=2669..2730，After 为 profile；不能将该结果记成正确关闭。当前错误坐标的生成来源继续追查，不采用固定底栏禁点区域替代根因修复。
- 公共 scheduler 在社区／AI／主页以及最后 Profile Work 的 no_progress 均为 0。实际停止来自 App coverage evaluator：Cmd25 新增 profile 覆盖后，因动作被归为 popup close 而跳过业务停滞更新，继承此前 6／3 计数，随后提前停止。应分别修复真实进展记账与错误关闭目标；新增覆盖不能反证点击正确，也不回写 R28 已封存结果。

### R28 第一批修复：关闭后的进展记账

- 已将新稳定业务证据与新增覆盖页型的计数重置放在业务动作累计条件之外。关闭后确认新业务证据清零 repeated；新增覆盖页型清零 no_new。同页关闭仍不累加不清零，未证明到达或仍被遮挡也不重置；普通业务动作保留原有计数。
- 主工作区截图闭环 183 passed／7 failed，终态 35 passed，到达证明与动作进展两文件 14 passed，合计四文件 **232 passed／7 failed**。七项失败使用修改前 `d71c6c1d` 截图模块源码在独立进程逐项复现，断言一致（仅对象地址不同）；新页型、新同类页面、同页关闭、普通动作、未证明与仍遮挡六项新回归均已包含。
- 修改文件 Ruff 排除原有 I001／E402 后通过，`git diff --check` 通过。无数据库迁移、前端菜单或路由变化；错误关闭目标仍由下一批修复，完成前不启动新轮。

### R28 第二批修复：关闭目标的当前证据冲突

- 追加核验确认 Cmd25 的错误落点来自当前 Before 的 Vision 原生候选 `d9e934…`，并非直接沿用旧坐标或当前像素校准。源 Capture 的关闭曾有像素校准，但当前支付页没有该校准；角色级重绑仅凭同为 dismiss 与距离接近便选中 Vision 错误框。同 Capture 的可信 UIA 有 6 个底栏项，但没有可信关闭控件。
- 已加入独立触点冲突判定：无当前关闭正证据的 dismiss 中心与同帧可信 UIA 普通动作控件冲突时剔除候选，沿原有重观察路径处理。没有固定底栏区域、栏目或支付页分支；一般 Vision 关闭仍允许，当前像素／可信 UIA 关闭证据可覆盖底层普通控件。
- 新增三场景正式 Runtime 回归，修改前为 1 failed／2 passed，明确复现冲突时仍执行点击。最后审查发现关闭文案含“重新加载”时，普通 retry 全量重扫会复活已过滤目标；已将普通 retry 兜底限定于非 dismiss／skip 角色，并新增实际 execute=0 的防复活回归。最终 Runtime 文件 96 passed，新冲突模块 8 passed，合计 **104 passed**。核对失败场景 Driver execute=0、成功场景当前 Before 与实际坐标，及跨 Capture／签名／尺寸／方向的负例；修改文件 Ruff 与 `git diff --check` 通过。
- 原始 a449 观察只读回放共 17 个候选，其中 9 个为当前可信 UIA 普通动作控件；错误 `d9e934…` 判定冲突并被剔除，当前无像素校准或可信 UIA 关闭正证据。保留原始 Capture／Action Trace，不触控设备；该回放证明修复命中故障帧，不代表新轮实机已通过。
- 16:05:34 再核实 R28 Task／Run 仍 running，405 个检查剩 18 个未终结、failed 为 0，Surface／lease 已关闭释放，Gate 尚无；仍由原 8030 进程完成本轮检查。
- 16:07:45 检查收尾已确认：Task `review`，405 个检查全部终结、failed=0，Completion Gate `gate_passed=false`，`app_coverage_satisfied=false`。随后按 ID 核对：原探索 Run `29e5d455…` 实为 `failed/incomplete`（15:36:56），检查子 Run `347c73aa…` 为 `done/app_inspection_work_drained`（16:06:48），不能将子 Run done 当成原探索成功。16:06:51 的 Gate 快照记录 nonterminal_enforced_work_item、review_policy_unsatisfied、app_coverage_or_visual_quality_incomplete；其中 nonterminal 是 Gate 评估时快照，不能据此声称 16:07:45 仍有未终结检查。该轮自然证据不足，不是验收通过。

### R29：两批修复后启动动态复测

- 计数修复提交 `202f4e94`，当前关闭目标冲突与 retry 防复活提交 `d4c1c052`；两批共六个相关测试文件 336 passed／7 个修改前同断言失败，无数据库迁移或前端菜单／路由变化。
- 重启前确认隔离库 active Run／lease 均为 0，仅重启 8030 API／Worker，新 PID 为 7525／7526，数据库仍为 `20260912_0112`。16:14:19 readyz 四项依赖正常，认证访问原任务返回 200。
- R29 正式 V2 提交返回 202：Task `b1d06013-45d7-453e-8a26-1628446aa822`，Run `16fabe2b-19e0-4985-875d-075cce97d697`。同一 build_ref、qa-a、330 动作／180 分钟资源预算和 `dynamic_discovery`；不注入产品白名单、固定栏目、页型或数量目标。
- 16:14:54 Task／Run running，qa-a lease active、Surface starting，尚无 Capture、Work 或 Gate。持久化 Target 已核实 profile_id=null、applicability_mode=completion_mode=dynamic_discovery；实际 Goal／策略待首个 Work 创建后核验，空 Work 不构成已确认。
- 16:16:08 已取得 Bootstrap Capture `9d3622a4-0847-4308-9203-a89c0ffc74cf`；16:16:22 Surface active，视觉理解与首个 Work 尚未就绪。自动跟进已切至 R29，仅在重要进展、真实故障、完成或需要用户操作时通知；首帧启动不等于修复后交互或 Completion Gate 通过。

### R29：清障实证与自然收尾

- 首个实际 Work 已核实 Goal applicability_mode=dynamic_discovery，Policy dynamic_frontier、playbook／curriculum 为空、active_tabs=[]、required_action_count=0、quantity_unbounded=true。page_limit=100 的角色仍为 optional_safety_hint_not_completion，不是固定数量目标。
- Cmd14 After `3df996b7-4fc2-5b99-bebf-4eb5a6c9c4ae`（17:04:37）经主侧查看实际截图，中央推广及底部横幅已经消失，主体和底栏可见，仍有小型浮动推广；不能只凭 home/stable 摘要证明清障。
- Cmd15 短剧导航后为 loading；Cmd16 观察后的实际视觉事实仍有付费解锁遮挡；Cmd17 选择通用包内 BACK，After `eb63c851-d470-5155-a49b-5b6434ddf134`（17:14:19）仍为 video_detail/overlay。没有本轮播放推进证明，不能沿用 R28 的播放证据。
- 三步无进展计数依次为 1／2／3，17:15:27 Work 被结算为 bounded，reason=`bounded_exploration_no_progress_limit`。当时尚余 314／330 次动作；Work 中仍有 19 个候选，包含其他当前可信 UIA 底栏入口。Frontier 1 failed／29 cancelled，停止范围是否应局限当前分支仍在审计，不以提高阈值替代修复。
- 17:17:34 自然封存 evidence_insufficient，索引 `app-evidence-index:03082991b70eae18e31f995d764a607495f90d752b6c645561acc748d8d1c98a`；本轮并未人工取消。17:17:38 lease released、Surface closed。
- 17:19:25 Task review，原 Run failed/incomplete，检查子 Run `a45973f0-6431-57f0-b172-b2ae48fdf7b4` done；293 个检查全部终结、failed=0，Gate passed=false、app_coverage_satisfied=false。封存摘要只有 scheduler 停止原因，没有完整页型分母；最后 Work 记录 covered_page_types=[home]，二者分开陈述。

### R29：分支无进展错误扩大为 Run 终态

- 源码审计确认：Runner 原来只在决策已经是 ExplorationTerminal 时调用全局 Frontier 恢复；普通 Action 到唯一派发检查才被 `no_progress_limit` 转成 BOUNDED，已错过恢复机会，随后 Runtime 按任务终态取消剩余 Frontier。设计 §6.4 的当前分支停止由此扩大成整个 Run 停止。
- 修复在 App Runner 决策前识别局部无进展，复用现有恢复选路、当前候选重验、领取和结算。只有恢复得到带持久化 Frontier 归属的动作时重开局部无进展窗口；原有动作／耗时／deadline、重试、返回与弹层循环、恢复次数、覆盖事实均保留。其他已恢复终态继续原处理；没有可恢复候选时保持证据不足。
- 真实 R29 Work 与内存 Frontier 只读回放选出该 Capture 的 UIA「AI」候选，非 restore_only。真实 `bounded_dispatch_terminal` 从 no_progress 停止变为可继续；调度账本差异严格只有 no_progress_count 从 3 到 0，Work 预算和覆盖事实未变。该回放没有调用设备或修改原数据库，不证明新 Run 已成功到达。
- 最终主工作区四文件联跑 **171 passed／6 failed**，其中新增 21 项全部通过，覆盖真实 Runner／派发检查、无候选收口、硬停止组合、普通动作不重开窗口、restore_only 原次数保留及耗尽后切换候选。六项失败（Global Frontier 2、App Turn 4）均已用未修改 HEAD 的 Runner 在独立进程复现，整组并非全绿。旧 HEAD 对新增主链用例明确返回 BOUNDED，新实现建立后继。
- 恢复窗口并不计为已证明进展：restore_only 仍按 item 的原有次数上限收口，总预算和循环历史不清零。修改文件 Ruff、格式及差异空白检查通过；无数据库迁移，无前端菜单／路由同步。此时尚未启动新 Run，设备到达及最终 Gate 仍待复测。

### R30：分支恢复修复后重新提交

- 修复已本地提交为 `9b91524b`。重启前隔离库 active Run／lease／Surface 均为 0，迁移为 `20260912_0112`，本地 Docker PG／Redis 健康；仅重启 8030 API／Worker 至 `11659`／`11660`，四项 readiness 与 auth/me 均 200。
- 9 月 13 日 17:42 正式 V2 提交返回 202，Task `e78fc4e1-a869-49ba-894f-9904c428f60c`、Run `6daf807d-c5c4-492d-81ec-406fd6dea399`。沿用同 APK build_ref、ONIRENT qa-a、330 动作／180 分钟和 dynamic_discovery；已持久化 Target 核实 profile_id=null、动态完成模式，未注入固定栏目／页型／数量目标。
- 17:43:35 Task／Run running、qa-a lease active、Surface starting，尚无 Capture、动作及 Work。自动跟进已切换至 R30，后续核对实际动态策略、分支恢复的同次后态及最终 Gate；启动事实不代表修复后设备到达或完整验收。

### R30：清障、跨页推进与视觉输出降级核查

- 实际 Work 已核实 dynamic_frontier、playbook=null、active_tabs=[]、curriculum=null，Goal 为 dynamic_discovery。Cmd12 底部公告关闭重验失败后自动重新观察，随后继续清障。Cmd14 After `91698567-ecc9-5fd4-849e-5c5eaf0f3457`（18:33:26）原始截图已核对：中央推广和底部公告消失，主体及底栏可见，仍有小型浮动推广。
- Cmd15 After 为短剧详情 loading，不能认定实际播放。Cmd16 从短剧遮罩页面导航到社区，After `f993984c-fdff-513e-bd09-ddf0ff021f87`（18:40:02）识别为 post_list/popup，Frontier 记录 visited；该记录不代替无弹窗的稳定覆盖。
- 社区 Cmd17 After、Cmd18 Before 分别出现 provider_parse_failed／JSONDecodeError，日志记录 JSON 分隔符解析错误；Cmd18 After 则是单独的 provider_response_truncated／finish_reason=length。三帧不能合并归因于截断。日志仅保留输出开头，未持久化完整 completion，不能进一步断言具体非法字符形态，也不能据此认定内容拒绝。49 项视觉 Provider 测试通过；本次仅只读核查，未修改解析代码、输出预算或历史事实。
- Cmd18 被重验阻止后，任务自动改派「我的」。Cmd19 After `8ec98dc9-bced-5e48-aacc-c32be26abb80`（18:52:55）已恢复正常视觉识别并到达 profile/modal；Cmd20 After `2cec9993-fe09-5313-aaf0-150edb42c5d0`（18:56:16）识别为 profile/stable。18:57:47 Task／Run 仍 running，Cmd21 已计划 AI 导航，Frontier 2 visited／2 retry_wait／24 pending／1 running，146 项检查全部终结、0 failed，Gate 尚空。
- 当前跨页推进不能证明本次 `no_progress_limit` 恢复窗口已经在真机触发，也不能代替最终探索封存和 Completion Gate。继续观察原 Run，没有因单次重验失败或暂时视觉降级取消。

### R30：局部三次无进展后，正式恢复其他 Frontier 分支

- Cmd25 短剧导航后及 Cmd26／27 返回后仍有遮罩。以 Cmd27 After `e73ccbb2-5298-52db-9655-a0ba1cd18e12` 为来源的持久化 Work `exploration-work:0c9ba0ce8d6e22a711718ced3bd1ae925fc58ea51f2e4e805f778a078769f92f` 记录 no_progress_count=3、return_repeat_count=1、popup_repeat_count=1，剩余 304 次动作。
- 同来源正式签发 Cmd28「暗网」导航，intent=`app-frontier-intent:4e114823d39d4c38f279f3192b24959984ac27887ea61029251f1e0f559ee2cd`，带持久化 Frontier item 及当前重验候选，restore_only=false。Action `action-execution:43835ad41762761311e9cd24339b4495fb1eedf8e1e87a65c7110c42824f97fe` 于 19:26:57 completed，取得 After `46c6de46-1986-516a-add5-fd3ddba2408d`。这证明本次修复已在真实 Run 的阈值场景触发，未将剩余任务直接取消。
- After 识别为 payment/stable；其后继 Work `exploration-work:b8ddc87053a710f0de9c1b4258effa3e6528c2d43838489590f7d3f0edef63f1` 的 no_progress_count=1、调度动作消耗从 24 增为 25、剩余动作从 304 减为 303，covered_page_types 仍为 [home,profile]。恢复窗口不伪造新增业务覆盖，也未把预算恢复为初值。
- 此前 Cmd23 的实际 Before／After 图片已核对：页面原本就是「暗网」选中的会员落地页，关闭仅移除了底部公告，没有误点解锁；先前 home／主页选中的视觉摘要不准确。随后 Cmd24 已正常回到主页，不以页面摘要替代实际画面。
- 19:31 原 Run 仍 running，Cmd29 页面下滑被 `COMMAND_TARGET_RISK_UNDERRATED` 阻止后取得恢复截图，正在只读核查风险归因；尚未取消正常运行。最终 Gate 仍为空，阈值恢复成功不等于完整探索或最终验收通过。

### R30：动态采样风险误判与收尾

- Cmd29／30 均为 LOW SCROLL，只有向下方向，没有点击坐标或 resolved_target；两次在 resolved_target_policy 阶段报 COMMAND_TARGET_RISK_UNDERRATED，driver_invoked=false。平台候选 purpose／expected_state_hint 中的 payment 是观察页型，被通用目标文本策略误判为进入支付页，不能据此指认为支付点击。
- 修复只对 Resolver 从同来源分析重新验签、候选身份完全一致、无控件框／解析目标／自定义坐标且方向为 down 的动态页面采样，使用动作名称进行目标语义复核；采样说明和预期页型不再充当控件副作用。内部标记不从请求读取，普通视觉目标仍保留完整风险校验，不按产品或包名放行。
- Cmd29／30 的实际来源及 Before 已通过 PostgreSQL 仓库完整只读 Resolver 回放：两者均确认为无目标整页采样；旧全文风险判断复现原错误，新目标判断通过。保存 `replay-scroll-r30.json`，无设备调用、未修改原始失败。新增正例在旧 HEAD 生产源码下也复现驱动前失败，不能把回归通过代替新设备后态。
- 主工作区新风险边界、交互 Runtime、CommandPolicy、Broker 四文件联跑 **141 passed**，新增 10 项包含实际 Driver／Broker 正例、支付点击和普通滚动负例、伪造合同／坐标／轨迹／方向，以及服务端禁用 SCROLL 零执行。Ruff 与差异空白检查通过。无数据库迁移、前端菜单或路由变化；修复后真机后态尚待 R31。
- 重复误拦截后正式取消 R30。19:41:51 探索封存 evidence_insufficient／evidence_incomplete；19:41:56 qa-a lease released、Surface closed，Task／Run canceled。239 个 enforced 检查已全部终结、0 failed，Gate 仍为空，不能据检查清空认定验收成功。局部 Frontier 耗尽发生在取消前，不能全部归因为人工取消。

### R31：动态滚动修复后正式复测

- 修复已本地提交 `96a1aa56`。重启前隔离 PostgreSQL 的活动 Run／lease／Surface 均为零，迁移仍为 `20260912_0112`；仅重启 8030 API／Worker 为 19751／19752。19:57:30 readiness 四项依赖正常，认证成功。
- `POST /api/v2/tasks` 返回 202：Task `d78400f7-bdca-47da-908b-cf890a66b8c2`、Run `b53a7a56-8048-47fc-ac19-2d107a6a9527`。沿用同一 build_ref、qa-a、dynamic_discovery 和 330 动作／180 分钟资源预算，不恢复产品白名单或固定必达分母。
- 19:58:27 Task／Run running，qa-a lease active，Surface starting，尚无 Capture／动作／Work，Gate 为空。自动跟进已切换 R31；启动与回归通过不能代替本轮真实滚动后态或完整验收。

### R31：探索截止与检查排空观察（23:27）

- 最后 Cmd11 于 22:58:55 completed；After `8029fb24-66eb-5260-8cf9-bdc885e03a80` 为首页广告弹窗。Work 实际 deadline 为 23:00:33，23:01:00 后 8 个 Work succeeded、1 个 cancelled，12 个 Frontier 全部 cancelled；主代理未发取消。尚未获得动态滚动的真机执行证明。
- 23:27 Task／主 Run 仍 running，Surface／qa-a lease active、Gate 为空，56 个检查中 15 个非终态、0 failed。21 时段视觉服务曾连续三次返回 provider 错误，22:26 和 22:39 已有成功请求；截图服务亦有 502／超时后重试成功，不能概括为持续不可用。
- 23:18 数据库确认全部检查为 active_session，App Publication 和 Evidence Request 均无记录；9 个 retry_wait 已耗尽 3 次，另有 4 个 retry_wait 与 2 个 running 仍在第 2 次。实际链为主 Run 关闭前同步排空，已有结算器可在 Task open 时按当前活会话写证据不足并继续关闭；耗尽中间态本身不能判为死锁。
- 独立审计确认排空 `max_wait_seconds` 原只累计 sleep，遗漏发布、消费和状态读取耗时。本次用单调时钟把每轮执行耗时计入预算，轮次返回后预算用尽便沿既有结算器收尾；不硬取消在途判定，不改探索 deadline、不新增重试或完成门槛。运行中的 R31 仍使用原进程代码，当前修改尚未加载。
- 主工作区排空、关闭、补采与既有结算四文件联跑 **78 passed**；新增两项用可控单调时钟验证超预算不再休眠／开新轮，以及预算内继续到终态。移除执行耗时扣减的旧逻辑运行时回放令首个新用例失败（仍额外 sleep 0.5 秒）。Ruff 与差异检查通过，无数据库迁移、前端菜单或路由同步；该局部修复仅在轮次边界检查预算，不能声称整个在途判定会被 900 秒硬中断。
- 23:28:15 探索索引自然 sealed，`app-evidence-index:4586e7253ad191ddf650e3e1b366660663e28200c57a409ac318e1ee829748ff`，outcome=evidence_insufficient、terminal_reason=evidence_incomplete、gap_count=2；预算事实为 deadline_reached。23:28:20 qa-a lease released、Surface closed，未人工取消或部署新计时修复；因此耗尽检查造成永久死锁的猜测已被实际收尾否定。23:30 Task／主 Run 仍 running，新增检查 Run `c851e8ce-a965-58fa-8b23-84ed644efccf` running，128 项检查仍有 72 非终态、0 failed、Gate 空，不能把探索封存等同于整个任务完成。

### R31 最终结算与 R32 启动（9 月 14 日）

- 00:30:12 R31 Task 进入 review，主 Run failed/incomplete（原始错误为 App Explorer Work 租约已失效），检查 Run failed/app_inspection_chain_failed。128 个检查全部终结、9 failed；这 9 项均为 attempt_count=max_attempts=3、INSPECTION_WORK_ITEM_FINALIZATION_INCOMPLETE。Gate=false，保留原阻断 nonterminal_enforced_work_item、review_policy_unsatisfied、app_coverage_or_visual_quality_incomplete，不能以终结后计数改写 Gate 评估事实。
- 主 Run 为 8 steps、32 AI calls、332212 tokens；检查 Run 为 14 AI calls、236842 tokens。上述分别是当前数据库的两个 Run 口径，不代表所有上游失败请求均已计入。
- 排空计时修复已提交 `30de4e12`，探索封存事实已提交 `ef46fa8c`。重启前隔离 PostgreSQL 的 active Run／lease／Surface 均为 0，迁移为 `20260912_0112`。仅重启本轮 8030 服务为 API 30284／Worker 30285，readyz 四依赖及 auth/me 均成功，之后才创建新任务。
- 00:34 通过 POST /api/v2/tasks 返回 202，R32 Task `c773adb4-b93d-4a39-965e-a03bf1453ac9`、Run `636308c1-b3b8-4f61-8376-96a69c59a996`。沿用原 build_ref、qa-a、dynamic_discovery、330 动作／180 分钟，无产品白名单或固定栏目／页型数量，不购买付费。启动成功不代表动态滚动后态或排空计时已完成真机验收。

### R32 最终结算（9 月 14 日 04:57）

- 03:35 达到 180 分钟探索预算，23 个探索 Work succeeded、1 个 budget_exhausted；Frontier 2 visited、24 cancelled。03:40:08.623029 自然封存，结果 evidence_insufficient/bottom_tabs_incomplete，索引 `app-evidence-index:57259f5791563861e45c25c3e50035893c667975c8883373eebc67e5ba9fab0b`，SHA256 `6d3747845586174fd01455520e280a4da620e549f7c6f83bc90bbf561c9bf2fe`，gap_count=0；03:40:12.939098 租约释放、Surface closed。未人工取消或重启在途进程。探索预算耗尽后的最终观察与封存约 5 分钟完成；后续检查排空另列。
- Cmd16 短剧 After `f14627cc-9a6f-5b24-a6e9-1b2a41422db5` 为 video_detail/loading，后续 Before 为稳定详情；Cmd23 暗网 After `888da92a-2555-5448-a217-3d58c4332746` 为 payment/stable；Cmd24 我的 After `34c1fc3f-2164-5536-8f45-a4c11cb5fb5b` 为 profile/popup。这些是实际画面观察，最终覆盖合同仅累计 home，底栏访问仅主页、我的；未把截图分类或动作 completed 当作覆盖完成。对应到达证明未成立的原因正在只读核对。
- 04:57:34.400162 Task 进入 review，Gate=false，阻断 `review_policy_unsatisfied`、`app_coverage_or_visual_quality_incomplete`。430 个 enforced 检查全部终结、2 failed、0 nonterminal。两项失败 `04d7216b-a34a-4c8b-ae7f-9d1af5e24630`、`102e1446-c591-4ce9-87a9-4086933019ab` 均为 snapshot/rule_engine、attempt_count=max_attempts=3、`INSPECTION_WORK_ITEM_FINALIZATION_INCOMPLETE`；需继续核对缺失的正式规则结果。
- 主 Run 最终 failed/incomplete、错误 bottom_tabs_incomplete，24 steps、96 AI calls、1883194 tokens；检查 Run `cf5e8775-fbed-5401-b039-47f67f956200` 最终 failed/app_inspection_chain_failed，28 AI calls、545427 tokens，04:57:29.121145 结束。动作表为 20 completed、3 failed，与 Run steps 分属不同统计口径。
- 本轮未取得动态 SCROLL 真机验证，不能把滚动回归或排空计时修复算作完整设备验收。先审计本轮同 Run 覆盖和两项检查失败，再确定后续修复与复测，尚未启动 R33。

### R32 review 后的聚合恢复状态修复

- Gate 评估后 Task 虽为 review，finalization_state 仍为 session_draining、Epoch=0，后台每约 30 秒重试聚合发布并报状态／合同不匹配。因此上文终态指 Task/Run 和已有 Gate 评估事实，不能据此宣称 Finalization 已密封。
- 发布快照及 EvidenceInputSet 已接受 running/review，但聚合 Work 的发布、领取和结果回写只接受 running。三处统一状态口径，保留 session_draining、Epoch、Run／输入集合／规则合同、租约与 Attempt 原校验，无迁移或前端变更。
- 三条 SQL 合同回归旧实现失败，修复后四文件 21 passed，Ruff 通过。R32 两个聚合 Work 的事务回滚重放，旧实现均拒绝，新实现复用原 ID、created=false，分别保持 evidence_insufficient／succeeded；回滚后记录不变。05:11 在 PG 确认无 active Run／租约／Surface 后，仅重启 8030 测试 API/Worker（41082/41083）加载修复；readyz 与原授权身份读取任务均 HTTP 200，恢复循环不再抛聚合发布异常。Gate 阻断事实未改变，仍为 session_draining：现有实现对事实未变化的 blocked Gate 复用原快照并延后重试，不应把该状态误判成同一发布异常，也不代表 sealed 或 Gate 通过。


### R32 审计后续：动态到达证明的残留映射与确定性批次超时

- 发现 `app_action_observation` 仍在候选缺少显式预期时，按“短剧／暗网／AI”等名称补造页型要求。已移除此静态映射；保留当前候选的显式预期、可信选中态和后态事实。该遗漏导致同一后态因标签不同而获得不同的底栏留证语义；R32 Cmd16 为 loading、Cmd23 为低置信 payment，不能因此反推本轮已经完成可信访问，历史事实不回填。
- 动作证明文件 51 passed，Ruff 与 diff 检查通过。新增标签对照回归在旧实现有 6 个失败；扩展四文件 338 passed／19 failed，用修改前动作证明模块复现相同 19 个失败（三个扩展文件 287 passed／19 failed），因此本次未引入额外失败，但不宣称扩展回归全绿。
- 两个失败 Snapshot Work 各含 `alioth_9/14/18/19/25/42` 六条规则，三次 Attempt 均 120 秒超时，整批 Judge 未返回，故结果载荷为空且缺少正式 Packet／Execution 关联。六个 Handler 本身不调用 LLM，本轮 app:// URL 也不发 HTTP 检查；已成功的同规则批次耗时 8–107 秒，另有第三次约 108 秒成功的批次。当前证据指向逐规则 Packet／可信证据读取的累计耗时，仍需定位具体读取或持久化阶段，尚未提高超时或启动 R33。


### R32 UIA 入口提示与后态证明修复

- 进一步核对实际执行候选，Cmd16 来自 UIA，仅携带 `expected_state_hint=tab:短剧`，原始 Vision 的 `video_list` 提示并未被执行；Cmd23 同为 `tab:暗网`。旧比较把入口身份当作页型，因而永远无法命中视觉分类。现只把注册页型作为类型预期，入口身份提示继续保留用于审计。
- 同步确保页型证明沿用已有可信业务后态：加载、遮挡或低置信身份不算切栏；可信选中态正例继续有效。四项 UIA 提示回归旧实现失败，六项后态反例在增加后态约束前失败；修复后动作证明 61 passed。R32 实际 Cmd16／23 仍不满足可信后态，不能回填已访或改写 Gate。


### R32 确定性批次重复取证修复

- 只读测量两个失败 Capture，2.65 MB 截图分别约 17.72／17.53 秒，其余材料约 0.2–0.4 秒；确认逐规则重复下载是显著开销。`judge_many` 的确定性分支现开启批次作用域，旧附件路径复用已校验原文，不改变 120 秒预算、重试次数、逐规则归属／摘要校验或 Packet 生成。
- 两个 Capture 各重放四个可信证据包读取：`9d5dc07d…` 原 20 次对象读取、82.26 秒，现 5 次、17.91 秒；`bc505593…` 原 36 次、68.14 秒，现 9 次、19.52 秒。原文摘要与材料顺序完全一致，两组始终各 4 次 Packet 调用。PG 连接设为只读，Packet 保存替换为内存计数；未写正式检查或修改 R32。
- 六文件 209 passed（Judge、可信证据、清单读取兼容、批次缓存、确定性能力及 Work Consumer），包含批次异常释放、两批隔离、跨任务路径／摘要变化拒绝。缓存限单批 16 MiB／128 条，不跨 Work 或重试；清单主路径不改。此为取证性能和本地链路回归，尚未证明新真机 Run 的完整 6 规则批次与最终 Gate 通过。


### 动态终止的固定业务类型要求移除

- 发现旧动态终止仍要求额外排除 payment／post_publish／video_detail：完整 Catalog、仅 home 稳定覆盖、零 Pending、Local／Global Frontier 已耗尽的纯函数重放，旧逻辑仍返回 assessment_scope_incomplete。这与有限自由探索允许单页 App 按实际发现范围完成的要求不一致；它不是 R32 的直接终止原因，R32 仍为预算耗尽和观察到底栏未完成。
- 现保留原范围收敛条件和显式目标验收，动态闭合时将未见目录类型单列 not_observed；已有 no_entry 仍为 not_applicable。报告不把未观察到写成产品不存在。同 Run 聚合范围读取同步该合同，字段不完整／自相矛盾时仍保留 unresolved，避免终止与规则范围采用两套分母。
- 七文件 153 passed，另两项探索轮次联动回归通过，覆盖单页完整目录、已知未完成／阻断入口、预算、Local／Global Frontier、显式目标、封存结果／哈希／Run 权威与报告投影；Ruff/diff 通过。扩展检查新增一项旧合同断言，已更新为“未观察 payment 与已见未完成 profile 分列”并验证通过；其余 19 项既有失败与本轮开始时一致。新字段不会回填 R32，也不改变既有 Gate=false。尚待加载全部修复后开始 R33 设备复测。


### R33 修复后设备复测启动（9 月 14 日 05:48）

- 已提交 `8f15b09e`、`e6907f92`、`2aaca2df`、`c5dbc149`、`2acfcb89`。PG 确认无 active Run／租约／Surface，迁移版本 `20260912_0112`；仅重启 8030 测试进程（API 43356、Worker 43357）加载修复，readyz／原授权身份校验成功。
- POST `/api/v2/tasks` 返回 202：Task `0963293b-7923-45cd-98f6-3bd52abfc12e`，Run `6fdfec83-f4bf-4504-b8a9-669e70e7db64`。沿用原 build_ref、qa-a、330 动作／180 分钟，不购买付费。持久化 Target v3 为 dynamic_discovery、profile_id=null、journey_steps=[]；Catalog 11 类仅作识别词表与原始统计，不作为本 App 必达数量。
- 05:58 Task／Run running，qa-a lease active、Surface active。Cmd2 关闭 completed，After `52c43d1e-04ac-55d5-b56e-e2831f87a70b` 为 home/popup；Cmd3 正在准备下一次关闭。当前仅证明启动并实际执行动作，尚未完成动态切栏、SCROLL、六规则批次或最终 Gate 验收。


### R33 自然收尾与识别波动审计（9 月 14 日 08:55）

- 本轮实际观察到稳定首页、短剧播放、AI 列表、社区及个人中心，并执行两次动态安全下滑；动作完成、稳定观察、Frontier 到达及正式覆盖分列。封存 `covered_page_types=[home,profile,post_list]`，不能把其它稳定观察直接补入历史覆盖。短剧与 AI 的观察、动作证明和覆盖更新之间仍在只读核查。
- 08:52:17 Cmd37 后态采集失败，Worker 明确记录 `TimeoutError: App Capture deadline 已到`，探索摘要保留 `budget_exhaustion_reason=deadline_reached` 与 `APP_ACTION_AFTER_CAPTURE_MISSING`。不是人工取消；没有延长预算或补造动作后态。
- 08:53:06.507639 封存索引 `app-evidence-index:2681eaf08f21753fa2aa4e1214471a5e4edcb82c7cbadb2d7368160a35a0de3e`，摘要 `sha256:cc6edce52ba3db1d04448de66244becfb4e7e6c3203c4f8d81fec0d3ea922ac1`，2 项证据缺口，结果 `evidence_insufficient/evidence_incomplete`。08:53:11 qa-a 租约 released、Surface closed；动作共 26 completed、8 failed、1 evidence_failed。
- 08:53 已有 282 项检查全终结且零失败；后续检查 Run `37da4f47-cbd3-5826-ad37-5c290d7e9bd9` 启动收口，08:55 总数增至 552、267 项未终结、零失败。两次计数分母不同，不能用前一时点宣称最终检查完成；Task／主 Run 当时仍 running，Gate 为空。
- Cmd32 的来源 Capture `981e1a0f-32c7-56b5-ab09-f9523457c8a6` 与当前 Before `bb342107-1d35-5b30-98d6-c058675084df` 均 stable，重验后的页面身份完全相等，且都有唯一低风险“收益明细”候选；仅页型由 post_list 变成 content_list 就触发 `APP_CANDIDATE_REVALIDATION_REQUIRED`。离线复现同样返回拒绝且 Driver=0，确认分类字段被用作额外硬门槛。
- 下一对 Capture 的身份本身也因空态文案进入业务对象而变化，不能用页面标题相似推断身份相同。修复范围限定为两侧受验证身份完全相同的分类波动，仍要求当前唯一同语义候选及所有触控绑定，不使用固定页型配对白名单。

### R33 同身份触控重绑修复

- Runtime 复用 `confirmed_app_identity`，分别验证来源和当前 Capture 的业务身份；仅当二者非空且完全相等时，分类字段变化不再提前拒绝 page／local 候选。仍消费当前候选几何，保留包、Activity、截图、尺寸／方向、控件语义／角色／风险和唯一性检查。同页型原有行为未增加身份强制要求。
- 新增回归的两个页型变化正例在旧实现上失败，修复后通过；另覆盖 page／local 两种作用域、身份缺失／变化／绑定篡改、当前候选不唯一和风险升级。主工作区 Runtime、动作证明、页面身份及新增文件四组共 **216 passed**，Ruff 与差异空白检查通过。无数据库迁移、前端菜单／路由变化；尚未在新 Run 验证该修复。

### R33 可信切栏覆盖遗漏修复

- Cmd14 的稳定短剧后态已包含当前截图绑定的底栏“短剧”选中锚点，旧消费者仅读像素标签和 UIA selected，忽略该视觉事实，随后因预期 video_list 与实际 video_detail 不同拒绝到达。现复用页面身份及局部状态验证，仅消费底部条带的 selected_tab；顶部“AI短剧”不作为底栏选中证明，陌生栏目使用同一通用规则。
- Cmd17 的 AI 切栏已有 `tab_seek_screenshot_evidenced`，但此专用返回分支未计入 `arrival_proven`、未更新导航路径，正式覆盖因此遗漏。现保留原 outcome，当业务后态可信、稳定、无遮挡时证明到达并追加实际目标身份；遮挡或低可信仍不计业务到达。
- 新增正例在旧实现失败，修复后通过；负例包括顶部同名、原本已选目标、未绑定局部状态、遮挡和低可信。主工作区四文件 **128 passed**；覆盖、动作进展及截图闭环三文件 **197 passed、7 failed**，修改前 HEAD 的生产模块注入同套测试也为 197／7，失败用例一致。Ruff 通过，无数据库迁移或前端菜单／路由变化。
- 09:13 检查总数 552，232 项未终结、零失败，Task／两个 Run 仍 running、Gate 为空。六规则完整批次尚不能据现有审计宣称已通过设备验收。修复尚未加载运行进程，历史封存覆盖保持原样，待收口后新 Run 验证。
- 09:29 完成同一 R33 Task／Session／Run 的真实已存 ActionIntent、InteractionResult、CaptureRef 和视觉分析只读回放，对比旧版 `f3dc4b74` 与修复 `b5d26604`。Cmd14 Before `9469e2ad`、After `881920db-6093-5d1f-bada-479d7deb3b35`：旧版 intermediate／different_business_target_observed，修复后以当前截图底栏“短剧”选中锚点证明切换，arrival=true、路径追加实际视频详情身份，未改成预期 video_list。Cmd17 Before `333eee3f`、After `ce0b0f7e-6060-51f5-87cc-98b7bf642250`：两版均保留切栏证据，修复后 arrival 由 false 变 true，追加实际 AI 页面身份。无设备补采、数据库写入或历史覆盖回填；这是旧证据上的新逻辑验证，仍非新 Run 验收。
- 09:24 六规则完整集合 `alioth_9/14/18/19/25/42` 的 6 个 Work（`3fdf2be8`、`6192898a`、`7dd53fc9`、`929b54d1`、`b85daf5f`、`ee2d6515`）均 pending、attempt=0，无 Attempt／Packet／Execution；检查 Run 当时处理的是部分规则批次。09:27 全部检查尚余 208 项未终结，不能把完整批次未领取描述为成功或超时。

## 历史阶段结论（R23）

- 年龄确认已在 R7 真机上自动通过，有实际 execute 与 After 后态证据。
- 持续探索又发现并分批修复：重验池外像素候选复活、圆形 X 对文字／眼睛误检、启动 ADB 超时恢复、决策层池外 BACK、详情页忽略真实底栏、累计弹层预算删除有效关闭候选。
- R7–R22 已完成各自会话／租约收尾；不能将取消或失败 Run 计为完整业务探索或 Completion Gate 通过。
- R14–R16 修复 overlay 清障状态、关闭描述词表误删、同轮底栏领取与空页型错误拒绝清障。R17–R19 对连续广告的停滞判断过早；独立设备诊断经过 11 次首页清障才露出首页，证实有限队列并非已证明卡死。`69d7448e` 已撤销 unproven 无条件 noop，保留指纹比较与既有预算。
- R20 的广告正文误点和系统返回词表拒绝已分批修复，提交 `c396ee24`、`96e677be`；正式策略回放、真实截图落点回放及对应回归完成，详见文末。R21 正式自动完成 11 次首页清障，再关闭公告条，取得 home/stable 同次后态证明。之后已进入视频详情及其他底栏分支，但暴露旧槽位点击和 UIA 窗口裁剪坐标问题；9 月 13 日 00:11 主动取消，00:15 Surface／租约均收尾。仅有 home 的已验证覆盖，不是六类覆盖或 Completion Gate 通过。

- R22 取得 home/video_detail/post_list 三类同次覆盖，随后“我已保存”在 Official 边界连续被拒。已正式取消、封存并释放资源；`e0970b78` 修复当前可信候选语义丢失，真实指令只读回放通过。R23 已在真机验证账号提示关闭；后因公告误触个人页与连续无效导航正式取消，继续核查两条坐标链。六类完整覆盖仍未完成。

## 运行基线

- 旧 API PID `79711`、Worker PID `79712` 于 12:26:56 启动，早于 `d99e9ed0` 修复；旧 R6 结论是执行者人工取消，不是模型拒绝。
- 本轮使用本地 Docker PostgreSQL：`127.0.0.1:5432/alioth_hjll_qab_20260912`，Redis：`127.0.0.1:6379/12`。
- 本轮执行仓库已有 `0111`、`0112` 迁移，运行库由 `0110` 升至 `0112`；年龄入口修复未新增迁移。
- 15:00 重启本次 API、Worker，PID 分别为 `98749`、`98750`，代码为 `8a6d8015`。

## R7 任务与制品

- 15:01:18 通过 `POST /api/v2/tasks` 创建 R7，绑定设备 `qa-a`。
- Task：`aea95071-ead3-4136-bd6a-66f90b9cea39`。
- Run：`fc6dbcbb-4d74-4a0f-898b-63c866930d7f`。
- 已验证制品：`app-build:eb62d564-5bdf-498b-9c0f-35d33ae98a8a`。
- APK SHA256：`0dbb53e9aff712a081c074e7f502bcdb0f4aecfb7c023c7955d9b894a52d469a`。

## 年龄门动作链

| 时间 | 证据 | 结果 |
|---|---|---|
| 启动阶段 | Bootstrap Capture `2d56b12f-b2ee-464e-8b6b-619a91f84776` | 识别为 `auth_gate/age_verification`，`requires_auth=false`；候选为“我已满18岁”，风险为 `low`、控件角色为 `dismiss` |
| 15:03:44 | `/v1/apps/execute` 返回 `200` | 动作 `action-execution:bdaea3c373af569cf59e189ee39e680b5988b7f51b1565c202e7ae92624342e2` 已实际执行 |
| 15:04:13 | After Action Capture `c145347a-5820-5076-b285-0d9dc3bafa29` | 已核对截图，年龄门消失，进入开屏倒计时；动作状态为 `completed` |

## 阶段结论与边界

已确认年龄门在 qa-a 真机上完成自动确认，且存在完整的候选、`execute=200`、After Action Capture 和后态变化证据。R7 随后因广告关闭阻断被主动取消，不能据年龄入口通过宣称业务覆盖完成或 Completion Gate 通过。

Web 参考仅采用闭集候选与新 Capture 复评一致原则，不据此声称 App 缺少专用年龄节点。原始证据位于 `.alioth/hjll-qaa-20260912-agefix`，不纳入 Git。

## R7 后续阻断：被排除的像素 X 又覆盖可信关闭钮

- 开屏倒计时切换为轮播广告后，旧「跳过启动广告」候选在 15:06:11 被拒绝，运行时随后重新采集并选择「关闭弹层：关闭」。这次过期候选拒绝本身不代表卡死。
- 15:09:37 的 before Capture `de3baf16-1295-5eee-935f-6b7b11e2c42a` 明确存在右上角可信 UIA 关闭控件，`page_state=popup`、`visual_page_type=null`。该候选中心约 `(0.8653, 0.1117)`，位置与来源帧一致。
- 同帧像素校准误认广告中的 X，中心约 `(0.2736, 0.1090)`。该像素候选已因超出位置容差被筛除；但 `_prefer_pixel_calibrated_dismiss_candidate` 在筛选池没有匹配候选时，仍用校准事实重建它，并覆盖可信 UIA 控件。最终在 15:10:10 触发 `APP_CANDIDATE_REVALIDATION_REQUIRED`。未知业务页型是最后的拒绝条件，错误地重新引入候选才是根因。
- 15:10:38 通过正式 cancel API 停止 R7，15:10:46 Surface=closed、qa-a Lease=released。Task/Run=canceled，保留既有失败及 sealed evidence index；未把取消或无点击的恢复截图计为关闭成功。
- 修复仅收敛像素优先选择：必须来自已经过语义、位置、风险和置信度重验的候选池，并与本帧校准框一致；没有唯一匹配则不选择像素候选，不从事实重造已被排除的控件。保留原未知页面、包边界和当前帧校验。
- 使用两张真实持久化观察做只读回放：修复前筛选池仅有正确 UIA 控件，却返回池外像素候选；修复后像素优先结果为空，正确 UIA 控件保留且通过触控校验。回放未执行设备点击，也未修改历史数据库事实。实机复测结果另行记录。

- 本批验证：交互运行时、AuthGatePhase、年龄候选三个测试文件共 `94 passed, 1 failed`。唯一失败 `test_app_navigation_rejects_after_source_page_type_drifts` 已用修改前 HEAD 模块复现，为既有导航页型漂移行为，与像素清障补丁无关；新增五个参数化场景和既有合法像素优先用例全部通过。两张实际 Capture 的只读回放通过；Ruff 与 `git diff --check` 通过。本批无数据库迁移，无前端菜单／路由变化。

## R8 修复后复测：开屏关闭已通过，后续抓图链路超时

- 15:23 重启测试 API/Worker，15:24:22 依赖就绪后通过 `POST /api/v2/tasks` 创建 R8：Task `c9729db1-27e8-42de-94ea-aecb04a7993d`，Run `f6e55a4d-ed05-4a50-b5e8-67e49ff99e4c`；代码 `611c0cf5`，数据库仍为 `20260912_0112`，继续使用相同 APK 构建和 qa-a。
- R8 启动截图 `f97adf1f-7b5f-4ac0-931f-f1eb0bc9ae7f` 直接进入开屏广告，年龄确认状态已被 App 保留，因此 R8 不算第二次独立年龄确认验收。
- 「关闭」动作的 before Capture 为 `2b2a0982-be3b-519c-9899-a316fe26fa48`；15:28:47 设备 `/v1/apps/execute` 返回 200，15:29:12 动作 completed。After Capture `634512c8-4e64-52e8-b978-9fe51cc88002` 显示开屏广告消失、首页出现，证明修复后已实际越过 R7 失败点。
- 首页另有会员促销浮层。上述 After 视觉一度误判为 `home/stable`，后续 before Capture `2c65e7e2-26a8-5aa2-bf3d-c7b6c7a74c0d` 已识别为 `home/popup`。不把首次误判的稳定页或其已发布规则工作项当作无遮挡业务覆盖完成。
- before 视觉分析经历 ReadTimeout、APIConnectionError 和两轮重试，共约 308 秒。随后抓图在 15:37:23 返回 `CAPTURE_FAILED` / HTTP 502，明确错误为 qa-a 的 `adb shell screencap` 超时；尚无社区动作的设备 execute 回执与新 After Capture。
- 按用户要求在 15:37:53 正式 cancel；15:37:55 Task/Run=canceled。社区动作落为取消期间结果未知，不推断社区已到达。取消后的会话关闭与设备恢复仍需单独核验，不能据任务 canceled 推定租约已释放。

- 取消收尾在 15:41:01 完成：`/v1/sessions/close=200`、Surface=closed、qa-a Lease=released；证据索引 `app-evidence-index:96813c89891b12c1b0d6fa23a103549a772c92e816958381359099b9ca8daad6` 已 sealed，5 项 evidence gaps，8 个规则工作项已终态，Completion Gate 未通过。运行级统计为 2 steps、3 AI calls、26453 tokens，不能当作所有视觉服务调用的完整用量。
- 调用链确认执行前 `DriverAppRuntimeIdentityReader` 通过 Driver.observe 再次抓图检查前台包／Activity，故 before 视觉完成并不意味着设备 execute 已发出。抓图链路会处理 transient 502 并刷新 ADB。本轮使用已有恢复机制恢复连接，未因一次网络故障新增生产策略。
- 15:43 创建 R9：Task `ca84d303-2d75-4f31-899e-cd5e5f152fcf`，Run `77216c85-65b6-43ff-b055-99207448567f`。提交前 qa-a `sys.boot_completed=1`，读取耗时 0.6 秒，依赖就绪；保持同 APK、同探索目标。结果待后续记录。


## R9 继续推进与圆形关闭误检修复

- R9 已再次关闭开屏广告；底栏尝试后没有到达目标页，Frontier 正确记录 `APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN`，随后转入新人特惠弹层清障。15:54:50 关闭动作执行，15:55:31 After `39052586-84a2-55ff-87a7-4b3700d4f688` 显示大弹层消失、首页内容可见。
- 下一轮「关闭广告弹窗」目标却落在内容卡片文字图案内。真实校准框中心约 `(0.3625, 0.5832)`，宽高仅约 18×24 像素；像素校准还把原本没有遮挡证据的首页改为 popup。15:57:58 正式取消 R9；16:00:57 Surface=closed、qa-a Lease=released。未把取消期间未知的第 5 条命令计作关闭成功。
- 真正根因在圆形关闭检测分支：文字孔洞也可能满足近圆连通域与内部黑色对角线，但该分支缺少普通白 X 检测已有的周边孤立校验。两个实际误检周边亮色比例为 0.1686、0.3956；真实新人弹层圆形关闭钮为 0.0184。
- 最终生产修改仅为圆形分支补齐相同的 0.08 周边亮色隔离阈值。不新增内容语义门禁，不改变已有页面触控策略，也不取消 Vision 漏识别弹层时的像素救援。
- 真实 PNG 检测回放：首页原先 3 个候选减为 1 个，保留底部公告条真实白 X；新人弹层原先 3 个候选减为 1 个，保留实际圆形关闭钮。额外候选签发回放移除了派生像素候选及 popup 事实，未复原已覆盖的原始关闭候选；它用于验证误点被排除，不声称完整重放历史模型输出。原始证据保存在本次 `.alioth` 目录，数据库历史事实未改写。
- 本批验证：新增圆形隔离回归 4 项通过；交互运行时、截图理解、关闭重叠、年龄候选与 AuthGatePhase 既有用例共 132 通过、7 失败，失败均用修改前生产模块复现（1 个导航页型、6 个 UIA／候选合同旧问题）。新增用例与截图理解／关闭重叠联跑为 42 通过、6 个相同基线失败。Ruff、差异检查与两张真实 PNG 正反例回放通过。本批无数据库迁移、无前端菜单／路由同步，完整真机结果仍待后续复测。


## R10 启动连接故障与 R11 继续复测

- 加载 `12541bc8` 后创建 R10：Task `10850908-66b0-4654-8be8-b5bb82dfeaf7`，Run `9d13db26-edd2-4853-9c18-bf1774a64404`。16:11 的两次会话建立均因 qa-a `adb shell getprop sys.boot_completed` 超时失败，HTTP 503 / `SESSION_START_FAILED`；无 App 安装、动作或 Capture。Run=failed、Task=review、Gate=false，Surface=lost，设备进入隔离。
- 已在没有在用会话后，仅对 qa-a 固定端点 `172.104.172.119:30615` 执行 ADB disconnect/connect；启动状态读取 0.64 秒恢复且连续探测通过。随后经 `POST /api/v1/workstation/devices/cloud_phone:qa-a/release-quarantine` 正式解除隔离，未改 qa-b，也未直接修改租约表。
- R11 于 16:14:09 启动：Task `8d3a61c6-ed0b-4a58-8c90-1d1270fd670f`，Run `4f308946-b6a7-47ed-b020-3ba638fe6b81`。已成功建立会话、安装和打开 App；16:19:45 开屏关闭执行，16:20:13 After `0e576c81-9d0b-5584-8e5d-ab66b6ccfe73` 识别首页促销弹层，后续正在处理底部通知关闭。R11 尚未完成，不能据此宣称完整业务覆盖。

- 启动链路补丁：`ensure_ready()` 在 boot probe 真实传输超时时，对当前端点调用既有重连流程一次，再继续探测；不重置截止时间，第二次超时及非超时异常原样失败。本批共享 ADB、健康评分、ONIRENT 三组测试共 32 项通过；新增成功恢复与持续超时回归，无数据库迁移、无前端菜单／路由变化。R11 运行期间未重载此补丁，因此不能把 R11 启动成功当作新补丁的故障恢复实测。

- R11 第 3 条命令因当前帧同语义候选不唯一被拒绝，恢复截图后第 4 条命令已执行。虽名为“底部横幅关闭”，After `a21b5e17-19bc-5487-bdf5-b8275f2f9f6f` 实际是从终身卡促销切换为新人促销，底部通知仍在，故不计通知关闭成功。第 5 条命令在 16:30:39 实际 execute=200，16:31:21 After `5123b6f4-ee36-596b-a8af-7a4433819e37` 核对中央促销与遮罩消失。下一候选中心 `(0.9441, 0.8561)` 为通知条真实 X，未再落入 R9 的正文文字；业务导航尚待验证。

## R11 业务到达后的池外系统返回阻断

- 第 6 条命令完成后，16:34:54 After `51309abd-a482-51b1-9c10-ddd26d509939` 已为 `home/stable`。第 7 条「底栏短剧」实际执行，16:38:02 After `4d22ae88-ecf4-532b-8a2b-06c2feb18411` 为 `video_detail/popup`，说明已离开首页到达短剧界面，但尚不计无遮挡业务覆盖。
- 下一 Work `exploration-work:e32a3689248df3d68ea3f0092ebe9dc8a90d236b483bb4e00486126cca2ec1f2` 在 16:38:45 抛出 `ActionIntent candidate_id 不属于当前 App Frontier`，探索技术失败。16:39:08 正式取消；16:42:32 Surface=closed、qa-a Lease=released。保留 sealed Index `app-evidence-index:585f7e00552d7943576e5836d11f47a2af780baae78e56115ceafd2b29da75f0`，2 个 gaps；关闭前 8 个未排空规则工作项按证据不足收口，不能据工作项终态声称全部规则验证通过。
- 真实 Work 只读回放复现：候选包含当前帧签发的关闭候选与底栏，没有 BACK；MAF 确定性分支却生成 `forced-system-back:4d22ae88-ecf4-532b-8a2b-06c2feb18411`。它没有进入 Work Frontier，原调度归属校验正确拒绝。不是模型自由回答或设备返回失败。
- 修改删除决策层的池外系统返回签发，同时删除底栏过滤空池时的同类注入。已有可执行 BACK／WARM_RESTORE 保留；没有返回候选时继续既有清障／导航选择，空池仍交给既有无进展处理。调度器的候选归属和触控合同保持原校验。
- 修改后真实 Work 返回 `candidate:0a912aec5d913421eac0ed0f1466c7e1011d132814c016c12a6a8b678a476a94` 的关闭动作，通过原 `_selected_candidate_for_decision`；这只证明调度归属修复。进一步人工核对截图发现该像素候选实际是眼睛图标，属于独立圆形误检，须修复后再下发新任务。回放未访问设备、未改历史数据。回归及后续真机结果继续记录。

- 决策修复批次验证：MAF 与 App Runner 两文件共 113 passed、6 failed；6 项失败均以修改前 HEAD 模块重现（MAF 2 项、Runner 4 项）。本次 7 项返回／关闭／空池回归全部通过；Ruff、差异检查和真实 Work 归属回放通过。本批无数据库迁移、无前端菜单／路由变更。


## R11 圆内黑瞳误识别为关闭 X

- 同一短剧 After `4d22ae88-ecf4-532b-8a2b-06c2feb18411` 的像素候选中心 `(0.934375, 0.734704)` 实际落在眼睛图标。圆形分支只要求黑色区域占据中心四象限，中央黑瞳也满足，因此派生了错误 popup 与关闭候选。
- 修复要求黑色笔画伸入四个外围对角区，并以外围暗像素计算各臂最低占比；不使用页面名称、坐标白名单或 App 专属按钮规则。初版只收窄四象限阈值会遗漏真实促销圆形 X，已通过真实正例发现并修正，未提交该中间版本。
- 主工作区真实 PNG 回放：眼睛负例检出从 1 降为 0；新人促销 `cf81d719-688d-5eab-9dc6-4c83cdb1f2d1` 的真实圆形 X 仍检出 1 个且边界不变；首页 `39052586-84a2-55ff-87a7-4b3700d4f688` 的通知条白 X 仍检出 1 个且边界不变。保存 `replay-eye-isolation.json`，未修改历史视觉结果。

- 本批验证：圆形隔离、关闭重叠与截图理解中的相关用例共 16 项通过，涵盖新增眼睛负例、真实比例圆 X 正例和原有白 X。三张真实 PNG 回放、Ruff 与差异检查通过。本批无数据库迁移、无前端菜单／路由变化；不据这些局部结果宣称完整探索通过。

## R12 加载全部修复继续复测

- 16:58 重启本地 8030 API／Worker，加载 `44046bf2`（含 `d0fb092f` 当前候选返回与 `84071555` 启动重连修复）；PostgreSQL、Redis、对象存储和 Worker readiness 全部通过，数据库 `20260912_0112` 与迁移 head 一致。
- 16:58:54 经 `POST /api/v2/tasks` 创建 Task `1a387b58-c85e-479c-a208-28ae51839eca`、Run `8fbea670-d27e-4336-9d70-a38c0851d215`。保持同 APK 构建、qa-a、六类目标与预算。会话建立、安装和启动已通过，结果仍待持续观察。

- R12 已关闭开屏、两层促销与底部通知。After `dad83e42-9944-5529-b563-07d8cc3377d6` 为 `home/stable`，主页入口动作后 Frontier 已结算 visited。17:24:51 的短剧 After `2424aeaf-98b5-59e8-87ca-bf7789c7f5a1` 正确为 `video_detail/loading`、`popup_visible=false`，未再派生眼睛关闭候选。后续第 9 条动作使用本帧 Provider 返回的 `candidate:f9b0f0d34716091d959270909fb6dd00a20952483a3c27486c6b942ee0b92fa7` 系统返回，未再出现池外归属异常；因此已越过 R11 故障点。短剧仍因加载态未获得稳定到达证明，Frontier 为 retry_wait，不能计作完整覆盖。


## R12 详情页可见底栏却重复 BACK

- 第 9、10 条返回均执行完成，After 分别为 `79d30d02-bb8c-5757-a2d0-bbf5d8dc2cd5`、`e860c6fc-088d-5f9e-ad6c-b1423ce55af0`，仍在短剧页，底栏可见。期间下一集出现解锁层，第二次 BACK 后回到播放页；不能把集数变化计为底栏导航推进。17:31:42 探索器又计划第 11 条系统 BACK，已形成在短剧页重复返回、没有巡栏的停滞。
- 17:32:05 正式取消 R12，第三次 BACK 未确认执行；等待会话和租约独立收口，不把取消当作资源已释放。
- 根因是详情／付费页返回优先分支仅检查页型和覆盖缺口，忽略同一当前候选池里的真实底栏。修复提前复用原有 `visible_bottom` 筛选，仅没有真实底栏时才优先 BACK；有底栏时继续既有弹窗清障及底栏寻页，不引入新的位置阈值或放宽候选校验。
- 真实 Work `exploration-work:62416900f6fd1dcfce233929dc2b52508a61d23c9fe9a7de3ef076ba6eef0b88` 只读回放从系统返回改为池内底栏主页候选，通过 Runner 归属校验。回放保存 `back-work-r12.json` 与 `back-work-r12-after.txt`，未操作设备。

- R12 收尾于 17:35:09 完成，Surface=closed、qa-a Lease=released；Index `app-evidence-index:212c74d846828af51af44101357fb5c28aa1a4263a5346db5e4daec3b70604a1` 已 sealed、20 项 gaps，结果为 evidence_insufficient。

- 本批回归新增 4 个场景全部通过，MAF 与 App Runner 联跑 117 passed、6 个相同基线失败；Ruff、差异检查和真实 Work 回放通过。无数据库迁移、无前端菜单／路由同步；后续以新 Run 验证真实底栏推进。

## R13 验证详情页底栏继续探索

- 17:36 加载 `45ca4496` 后，经 `POST /api/v2/tasks` 创建 Task `8cf50d7d-16b4-4f8e-a267-1358a41af57a`、Run `d35adeb7-4fc6-43ca-b755-bbef9cb7974e`，保持相同 APK、qa-a、六类目标与预算。本地 readiness 通过；17:38 完成安装和启动并取得 bootstrap Capture `53c4fb1f-050c-4011-84a8-13d44325838b`，仍在运行，尚无最终验收结论。

## R13 累计弹层帧数绕过仍有效的关闭候选

- R13 关闭开屏后，首页广告弹层陆续更换。第 4 条底栏短视频点击的 After `7b471493-6784-5f72-b62b-5c520ffbbdba` 仍为 home/popup，Frontier 正确记为 `APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN`。第 5、6 条关闭后分别为 `58102e3a-cec7-5e27-b984-666c72a5c126`、`05e313c7-a6bc-5d31-8a98-e87ff743b36b`，广告内容与 overlay_signature 不同，仍有中央圆形 X。
- 持久化 action_trace `1c5d27c7-16db-5b93-bb2f-03a7dff7927f` 证明两次均为 `parameters.resolved_target`、`runtime_ui_tree=not_used`，实际坐标 `(721, 2158)` 与 1440×3040 截图中真实圆 X 中心一致，不能归因 UIA 覆盖或坐标缩放。签名变化只证明画面变化，未单独证明每次点击已关闭上一层。
- 17:56:53 系统选择第 7 条底部社区，虽然当前池内仍有合法 dismiss 候选。17:57:33 正式取消，防止继续点被挡底栏；18:01:03 Surface=closed、Lease=released。Index `app-evidence-index:7fae77517f959d5cedbba40b1aed34020bb9e31c647f9a94af8d7cdc7decbd13` 已 sealed，13 项 gaps，结果 evidence_insufficient。

- 真实 Work `exploration-work:1cf2ff8384ad42c186e11d8b7d79c228e97f6c249d107a005b6d39f8003d5eef` 的 `dynamic_home_popup_frame_count=6`、`dismiss_noop_streak=0`。原始视觉结果有关闭候选 `candidate:7f06f187eb852b6bb4444e80b7dc56f3fb3dc6fba5ffde96eae0324684b27d9d`，候选生成的满帧让路分支将其删除，MAF 也有对应的阈值早退；此处不是连续 noop 触发。
- 修复在候选生成阶段保留该池内关闭控件及原有寻页候选，MAF 对真实挡路弹窗先选择合法关闭控件；删除计数覆盖关闭优先级的早退，不改共用动作、时长、无进展或重复状态预算。
- 真实 Analysis + Work metadata 重建回放（`replay-popup-r13.json`）：修改前 4 个候选并选择社区，修改后保留原 4 个候选并加回由当前 Analysis 原本签发的关闭候选，共 5 个；原可执行性与 Runner 归属校验均通过，最终选择关闭。没有重造坐标、候选或修改历史数据；仍需新 Run 验证后续真实清障。

- 本批验证：MAF、截图探索、App Turn、共用调度器四文件共 286 passed、13 failed；13 项均通过注入修改前 HEAD 的两个生产模块复现，新增 8 项全部通过。MAF 与两测试文件 Ruff 通过；截图探索生产文件的 3 项 Ruff 问题（I001/E402/E501）在修改前 HEAD 完全相同，未借本批扩大格式改动。真实回放与 diff 检查通过，无数据库迁移、无前端菜单／路由变化。

## R14 像素已证明关闭控件，overlay 状态仍使决策绕开清障

- R14 开屏关闭成功后，首页广告宫格被 Vision 标为 `home/overlay`。第 3 条「底栏我的」执行后 After `a36795cb-98d7-56fb-9895-09a82ec2e18d` 仍在首页，Frontier 未记作到达；随后又计划第 4 条底栏首页。18:21:10 正式取消，未把取消期间动作算作业务到达。
- 人工核对持久化截图：这是覆盖主体内容、带全屏遮罩与中央圆形 X 的广告宫格。像素校准已正确输出候选 `candidate:6f894bd478b98969af6fff0ca3d033a8f39e94fd1e49b3babc95c7fad2b43dce`，中心 `(721,2456)`、`popup_visible=true`、`vision_popup_surface=true`，但校准函数将 overlay 当作已有弹层而保留状态，MAF 又把无其它强标记的 overlay 当作软浮层。两处语义不一致导致跳过有效关闭。
- 修复在校准成功时将 overlay 与已有的 stable→popup 处理统一；没有检出 X 的 overlay 原样返回，启动及年龄等其它状态不改。未增加 MAF 的第二套像素判断或坐标签发。
- 真实 PNG + 持久化分析回放：移除派生 pixel key 后重新校准，候选 ID、边界及坐标不变，page_state 从 overlay 变为 popup，重建 Frontier 后选择该关闭候选并通过原可执行性／归属校验。`replay-overlay-r14.json` 只验证校准与重建选择，未复原原始 Vision 输入、未重放全局 Frontier 历史过滤、未操作设备。

- 18:24:24 R14 Surface=closed、qa-a Lease=released；Index `app-evidence-index:481f33ed1164f110fe09808b9e6f5d3212648c63d36f751c023328630150900b` 已 sealed、7 项 gaps，结果 evidence_insufficient。
- 本批验证：圆形隔离、关闭重叠、截图理解三组 48 passed、6 failed；6 项均用修改前 HEAD 校准模块复现。新增 4 个场景（有 X／无 X overlay、startup_ad、countdown）通过，Ruff 与差异检查通过。无数据库迁移、无前端菜单／路由变化，真机后续仍待新 Run。

## R15 加载 overlay 清障状态修复

- 18:26:13 经 `POST /api/v2/tasks` 创建 Task `893d706f-31c5-46aa-8d61-3a6b38168a9f`、Run `531571b4-936d-4e70-9952-2ae52bb7bba0`，加载 `8ff16896`；继续相同 APK、qa-a、覆盖目标及预算，readiness 通过。新 Run 正在运行，尚未通过完整业务探索或 Completion Gate。

## R15 当前关闭候选再次丢失与 Frontier 领取失败

- R15 开屏关闭后首页为 popup，第 3、4、5 条均选择关闭广告；After 依次为 `024a1308-bc0c-5b8e-8d48-39ba902fe4bc`、`9c5ace81-a4f0-521b-889e-ebff99e07cc5`、`62b4a0b7-6c87-5627-9735-7968e5457149`，未重现 R14 的 overlay 跳过关闭。最后仍有中央广告层，未计为业务覆盖。
- 18:41:59 app-stage 报 `AppExplorationLeaseLostError`，探索技术失败并开始终态观察；18:43:19 正式取消 R15。错误发生在取消请求之前，不能归因用户取消。会话／租约收尾另行核验。
- Work `exploration-work:26162ef3b19969ba626a7ae2e223062633c2af90557ca76471dcbe6ac659253e` 的原始 Analysis 有合法关闭 `candidate:1c3c9677675ce54aff79da2d17415844f81521b56741b7821d5ba121ec2251e6`，发布的 Work 仅余社区／我的。此时 frame_count=5、noop=0、no_progress=0、仅用 4 个动作，预算未耗尽；正在定位不同于 R13 满帧分支的删除位置。
- 任务级仅两个底栏 Frontier，18:41:53.65 以 `APP_FRONTIER_STALE_BOTTOM_NAV_SKIPPED` 变为 superseded。18:41:59 模型提前返回终态，fallback 选择当前社区后发生领取失败；尚无证据证明真实设备租约过期或被另一任务抢走，需对照当前候选与历史指纹状态衔接确认。相关只读快照保存 `lease-work-r15.json`、`frontier-r15.json`。

- R15 收尾于 18:46:24 完成，Surface=closed、qa-a Lease=released；Index `app-evidence-index:649277ee99d4659cf9c41fcb66813cfc3d3549126d9a5507d93ee3e628b8039f` sealed、2 项 gaps，结果 evidence_insufficient，不能据封存声称业务探索通过。

### R15 关闭候选过滤根因与修复

- 删除发生在 `_filter_dynamic_safety_candidates`：真实关闭候选 purpose 为“关闭当前下载广告弹窗以恢复主页；当前截图唯一孤立 X 像素结构校准”，命中“下载”副作用词表。该候选已满足现有显式清障身份与当前 popup 的低风险关闭条件；不是同名次数／满帧修复失效。
- 修复只在原过滤处分流符合上述两个既有判断的关闭候选；真实下载、支付、购买与高风险候选继续过滤，不重建候选或坐标。
- 真实 Analysis + Work 重建回放 `replay-popup-r15.json`：修改前仅两个底栏；修改后增加回当前 Analysis 原有关闭候选，共三个候选，MAF 选择该关闭并通过共用 eligible 与 Runner 归属校验。回放未操作设备，也未重放全局 Frontier 历史过滤。

- 本批截图探索测试 158 passed、7 failed；7 项均用修改前 HEAD 生产模块复现。新增 6 项中三个关闭正例在 HEAD 失败、修改后通过，三个副作用／高风险反例前后均拒绝。MAF、App Turn、共用调度器另有 134 passed、6 个相同历史基线失败。测试文件 Ruff、真实回放与差异检查通过；生产文件已有的 I001/E402/E501 基线未扩大。无数据库迁移、无前端菜单／路由同步。

### R15 Frontier 领取根因与修复

- “社区／我的”当前 Capture 的 candidate_id 虽不同，重算 `global_navigation.v8` 指纹与历史待办精确相同。本轮先登记并保留这些 pending 候选，`_schedule_pending_frontier_once` 又把历史记录标成 superseded；随后模型 fallback 选中当前社区，按本轮登记结果领取该记录，数据库按终态合同拒绝领取。该分支将 None 上抛为 LeaseLost，导致探索技术失败；没有证据表明实际设备或 Work 租约丢失。
- 最小修复是历史底栏让路分支直接交回当前帧决策，保持 pending 不变；之后当前候选仍按既有指纹领取同一记录并通过动作重验。未修改数据库、指纹、租约或终态触发器，也未创建新一代重复 Frontier。

- 完整同轮回归在修改前 HEAD 精确复现 `App Frontier 当前候选不可领取`，修改后通过：只有一次 claim，原记录不 supersede，执行的是当前 candidate_id，successor settlement 绑定原 item。App Turn + 共用调度器 65 passed、4 个基线失败；4 项也用 HEAD 复现。生产／测试 Ruff 与差异检查通过。此用例使用内存队列和受控后态证明，不等于真机到达或 Completion Gate；无数据库迁移、无前端菜单／路由变化。

## R16 关闭语义与当前底栏领取复测

- 19:01:34 经 `POST /api/v2/tasks` 创建 Task `a52ac739-6cd1-4554-b9bc-8d4203caf0e3`、Run `a6ded265-e5c4-4f5b-9a90-699fffe6549d`，加载 `c2837076`；保持同一 APK、qa-a、六类覆盖目标和预算。本地 PostgreSQL／Redis／对象存储／Worker readiness 全部通过，数据库 `20260912_0112` 无新增迁移。

## R16 空业务页型阻断当前已校准关闭控件

- 开屏关闭成功，After `55336752-f83f-5046-861b-3ca7d022ee34` 进入 home/popup。第 3 条关闭的 before `45f0a075-4120-507d-9300-0553cfbaa4e5` 仍有真实中央圆 X，来源／当前候选边界完全一致，但 Vision 将业务页型留空。19:08:07 以 `APP_CANDIDATE_REVALIDATION_REQUIRED` 拒绝触控，未执行该点击。
- 19:09 正式取消；19:09:28 Surface=closed、qa-a Lease=released。Index `app-evidence-index:580e9dd2928e4c70e4edc7c5d9260193605cd89807fd769304abedd52db8ac36` 已 sealed、6 项 gaps，结果 evidence_insufficient。两个底栏在取消前保持 pending，本轮未到 R15 的实际底栏领取验证点。
- 根因是 `_touch_visual_allows_candidate` 只允许已证明的 UIA 清障覆盖空页型，当前唯一像素校准关闭候选在同一阶段却仍被空业务页型拒绝。修复复用现有像素优先匹配函数，只接受挡路层中的当前已重验 dismiss；现有支付排除、来源／尺寸／方向、风险／置信度、语义／位置和唯一性校验保持。
- 真实 ActionIntent + Source/Before Analysis 只读门禁回放 `replay-unknown-close-r16.json`：来源一致、边界相同、位置重绑与当前像素匹配全通过；修改前 touch_allowed=false，修改后 true。此回放没有操作设备，也未替代完整 Runtime 回归。

- 完整 Runtime 新增 6 项全部通过：None／unknown 页型真实关闭触发一次 Driver，source_capture_id 和坐标绑定当前帧；缺校准、错边界、支付事实、无关闭角色均为零触控。两个正例在修改前 HEAD 失败。全量 84 passed、1 failed，该失败也在 HEAD 复现；Ruff、真实回放与差异检查通过。无数据库迁移、无前端菜单／路由同步，后续以新 Run 验证实际清障。

## R17 当前像素关闭页型容错复测

- 19:18:24 经 `POST /api/v2/tasks` 创建 Task `8cbbac38-2431-4eea-94be-3c5acf6fa6b8`、Run `4ed51d6a-b3d7-40bb-9386-3a0fe23a4863`，加载 `23e837a7`；保持同 APK、qa-a、六类覆盖目标与预算，readiness 四项通过。新 Run 尚未完成。

- R17 第 2–6 条动作执行完成；首页广告层连续更换。19:35:25 After `973df003-ab22-5768-8112-198b5759f2e0` 为 home/popup、下载广告，第 7 条仍选择当前关闭候选；核对其 purpose 为“关闭广告弹窗并露出首页内容；当前截图唯一孤立 X 像素结构校准”，不含下载，因此本帧不能作为 R15 词表误删分支的直接真机回归证据。已有四条底栏 Frontier 保持 pending，未发生 R15 的同轮 supersede/claim 失败，但尚未实际进入底栏领取。尚无稳定业务页覆盖，继续观察并核对坐标转换。

## R17 未清障计数的阶段性判断（后续独立诊断已纠正）

> 以下记录当时的判断与提交验证。21:03 后的独立设备诊断确认有限广告队列，因此“unproven 即关闭无效”的推论不成立；修正见文末。本段测试通过仅证明当时实现符合测试，并未证明该判定符合真实 App 行为。

- R17 完成第 2–8 条共 7 个动作，首页连续六次广告关闭后仍为 popup；第 9 条关闭处理中于 19:44 正式取消，该动作结果记为取消期间未知，未算作成功。
- `f2a3ebee-5b18-5aaa-9881-cfc25152dc5b`、`973df003-ab22-5768-8112-198b5759f2e0`、`21e550e4-a53e-591c-84fa-a8d2d599ca4c` 三个连续后继 Work 的同次动作证明均为 `unproven / obstruction_not_removed`，业务不可见；但 `dismiss_noop_streak`、调度器 `no_progress_count` 均为 0。`_is_dismiss_noop_after_close` 忽略该结果，再比较随广告文案变化的 overlay_signature，导致错误重置进展计数。
- 最小修复只让已执行关闭且 After 仍为 popup 的 `unproven` 结果记为 noop；原有结构改变／真实清障处理、候选选择、动作预算和终止阈值未改。
- 真实连续 Work／Analysis／动作结果回放 `replay-noop-r17.json`：修改前两种计数均 0/0/0，修改后均 1/2/3，业务覆盖始终为空。回放仅在相邻真实 Work 上传递变化的计数字段，不重放共享事实合并、全局 Frontier 或设备。
- 坐标链只读核对：当前截图／设备均 1440×3040，capture_px 基准通过 normalizer→Resolver→map_point_to_device 后应恒等映射；未发现缩放错误。现有 R17 回执没有最终 ops.tap 实参／mapping audit，不能经验性证明每次最终落点，也不能将 Driver 成功等同于清障。APK 为编译后 Flutter 应用，未据静态资源推断广告队列长度。

- 19:47:00 R17 Surface=closed、qa-a Lease=released；Index `app-evidence-index:87877fd78a152201a56dd97bce920e04202a974a6509565ed8c8de79b30eeb49` 已 sealed、17 项 gaps，结果 evidence_insufficient。52 项检查工作已结束且无失败，不构成业务探索／Completion Gate 通过。

- 本批完整截图探索＋共用调度测试 173 passed、7 failed，7 项在修改前 HEAD 同样失败；新增 unproven 正例在 HEAD 失败、修改后通过，真实清障／intermediate／未成功触控三项反例前后通过。测试 Ruff 与差异检查通过；生产文件既有格式基线未扩大。无数据库迁移、无前端菜单／路由同步，真机行为仍待 R18。

## R18 未清障计数真机复测

- 19:52:25 经 `POST /api/v2/tasks` 创建 Task `f2215b44-8d24-4f22-bd41-77838011a6cc`、Run `90207c8c-0d35-4ddf-815b-248636d62c74`，加载 `d1aaef12`；API/Worker PID 为 38879/38880，四项 readiness 通过，保持相同 APK、qa-a、六类覆盖目标与预算。新 Run 尚未完成。

- 20:05:27 后继 Work `exploration-work:370a4131ceaea32045bea62f697dddfdd551043cac830fae93989385b4f5aa5e` 的 After `df82d1bf-0e15-5fda-a8e3-875bdb23cb14` 为 home/popup、同次关闭结果 unproven。真实持久化 dismiss_noop_streak=1、no_progress_count=1；此前两次 intermediate 保持 0。首次真机证明本批结果优先计数分支有效，业务覆盖仍为空，连续性及收尾尚待观察。

## R18 关闭控件边缘档位误判弹层替换

- R18 第 2–7 条共 6 个动作完成，其中开屏关闭 1 次、首页关闭 5 次。首页持续遮挡约 17 分钟且业务覆盖为空，20:15:19 正式取消；第 8 条关闭记 APP_WORKFLOW_CANCELED，不算作成功。
- 同次 Before `c88cffbd-9823-5985-b480-ddddb20ea10f` / After `227860f4-aff8-5184-ac45-4a08e33bd4b8` 中，关闭 X 边界完全相同、kind/roles/topmost_ad/popup_height_band 均相同；仅广告边界底部 `.72→.672`，将相同控件从内部第 5 档变成外部第 6 档，结构 hash 改变，生成 intermediate/obstruction_replaced 并将计数从 1 重置为 0。
- 下一对 Before `8e87f829-e65d-5682-a9a0-279b485e948b` / After `92823714-9aa1-5811-b32c-a33596ecf366` 同样因 `{5,6}→{6}` 改变身份。两对原始关闭控件与广告素材变化不能证明真实遮挡层替换；不是已修复 unproven 分支失效。
- 最小修复删除相对坐标越界的额外 -1/6 档，复用已有 `_fraction_band` 的 [0,1] 归一化；保留原高度分段和内部位置分段，未增加预算／阈值或改动候选／坐标合同。
- `replay-structure-r18.json` 用两对真实持久化 Analysis 比较修改前 HEAD 与当前代码，修改前 same_obstruction 均 false，修改后均 true。该回放仅验证结构身份，不重写历史 Work，不重放整轮调度或操作设备。

- 20:17:55 R18 Surface=closed、qa-a Lease=released；Index `app-evidence-index:6cbfce29974dbbb24d1569163b304db61d240cf91f1e5fc0909f3cd2a2c0c3d2` 已 sealed、14 项 gaps，结果 evidence_insufficient。40 项检查工作结束且无失败，未通过业务探索／Completion Gate。

- 四组完整测试 299 passed、8 failed，8 项均在修改前 HEAD 复现；新增上下越界及 R18 精确坐标三个用例在 HEAD 失败、修改后通过，两个边内用例与原有堆叠布局测试前后通过。生产／测试 Ruff、真实回放与差异检查通过。无数据库迁移、无前端菜单／路由同步；仍待 R19 真机验证。

## R19 边缘结构身份与连续未清障计数复测

- 20:22:19 经 `POST /api/v2/tasks` 创建 Task `5fb75ad9-397a-4ab5-97fc-d39891c4f6d5`、Run `f2bc26d4-cd51-42b5-b922-a1607b41e197`，加载 `847927d2`，API/Worker PID 41863/41864。四项 readiness 通过，保持相同 APK、qa-a、六类覆盖目标及预算；新 Run 尚未完成。

- 20:35:31 After `768e3a47-9697-5c8e-9818-df9041bc557c` 对应同次关闭 unproven，两种计数均 1；20:38:50 After `2d591da9-8608-514f-a5e2-44e49f117574` 仍为 unproven，两种计数均 2。真机证明边缘框选变化不再将这次连续未清障重置为 intermediate/0；业务覆盖仍为空，停止结果尚待后态。

- 20:42:04 After `409e6740-da3d-5ed0-91a3-dd78a9a00f8c` 为 intermediate，两种计数回到 0。本对关闭 X 相同、相对位置均 5，广告框高度 `.48→.35` 导致高度分段 5→4（`r19-structure-comparison.json`）。未将此直接认定为同一边缘 bug 或自动停止已通过。20:43:25 正式取消 R19；本轮第 2–6 条共 5 个动作完成，首页关闭 4 次；第 7 条取消期间结果另按账本记录，不算作成功。

- 20:46:26 R19 Surface=closed、qa-a Lease=released；Index `app-evidence-index:5690211bd1f3739399532581ca6a3150d7c3611165f6be0d20da38a74512e844` 已 sealed、13 项 gaps，结果 evidence_insufficient，无技术失败，未通过 Completion Gate。

## R19 后独立诊断：有限广告队列及过早停滞判断修正

- 在 R19 完全释放设备后，单独启动同一已安装 App；每次操作前核对 qa-a 无 active/reserved 租约。未重装、清数据或改正式 Run 事实，诊断截图和动作记录留于本地 `diagnostic-r19-*` 文件，不并入正式 Gate 证据。
- 一次开屏关闭后，首页依次处理广告宫格（一次系统 BACK）、8 张广告卡片（逐帧核对当前 X 后各点击一次）、两层促销（一次 BACK、一次当前 X）。首页阶段共 11 次动作；21:03:50 发起最后关闭，随后截图显示全屏遮罩与中央促销层消失，首页露出。全程未点击广告正文、下载或购买按钮。
- `card-7-closed` 与无点击的 `card-7-idle` 对照保持同一广告、仅动画变化；之后点击才切换到不同促销层。这个有界诊断证实此次队列有终点，不支持此前将 R17–R19 的 4–6 次首页关闭视为卡死。BACK 在宫格上同样只推进一层，不是可跳过整条队列的恢复方式。
- 撤销 `d1aaef12` 中 unproven 无条件返回 noop 的分支，恢复既有弹层指纹比较。`unproven` 继续表示尚未清障，不产生业务覆盖；相同弹层指纹仍累计无进展，不同指纹继续受原总动作／时长预算约束。保留结构几何容错与其“不能仅凭素材变化证明结构替换”的语义，不再把结构相同误推成关闭动作无效。
- 本段是独立诊断与修正依据，正式自动探索是否能走完整条队列、继续业务导航，仍须新 Run 验证。

- 本批完整截图探索、动作观察与共用调度测试 217 passed、7 failed；将修改前 HEAD 生产模块注入同一套测试得到 216 passed、8 failed，新增“unproven + 不同弹层指纹”恰为额外失败，另外 7 项仍为已有基线。六项聚焦测试、测试 Ruff 与差异检查通过。R17 三帧真实回放由 1/2/3 恢复为 0/0/0，前后业务覆盖均为空；回放未改历史事实。无数据库迁移、无前端菜单／路由变化。

## R20 有限广告队列与业务探索复测

- 21:16:31 经 `POST /api/v2/tasks` 创建 Task `97ff350a-a7ce-4114-a8cf-9fcb9b06daa8`、Run `7217faa1-708a-40f5-b1be-6fc14a08c3f3`，加载 `69d7448e`，API/Worker PID 46081/46082。本地 PostgreSQL、Redis、对象存储和 Worker readiness 四项通过；保持同一 APK、qa-a、六类覆盖目标和原预算。正式自动探索尚待观察。

## R20 真实关闭未校准、错误重绑定与回源拒绝

- Cmd 2 关闭开屏；Cmd 3、5、6、7、8 共 5 次首页关闭推进到后续广告。Cmd 4、9 在执行前以 `APP_CANDIDATE_REVALIDATION_REQUIRED` 拒绝，没有触控；两次均重新采集后继续。不能将这两个可恢复失败单独视为持续卡死。
- Cmd 10 的来源 `0fc769d4-a660-5243-8d16-2d6d9db6a26a` 与 Before `1a479663-915f-5f69-b211-3273d478ae94` 均有真实圆 X，中心 `(721,2158)`。两帧圆形与四臂 X 校验通过，但周边亮度比 `.081167/.118915 > .08`，没有生成像素校准。Vision 关闭中心由 `.7175` 变成 `.605`，在现有 `.12` 重绑定位置容差内，最终解析为 `(720,1839)`，偏离真 X 约 319 像素并位于广告主体。
- Cmd 10 虽 completed，After `4f43da52-62f8-5487-83a8-74c1c36e05bb` 已为外部浏览器；这次不能算清障。Cmd 11 系统返回随后被 `COMMAND_TARGET_SEMANTICS_UNTRUSTED` 拒绝，构成明确新阻断。21:51:23 正式取消，21:51:27 Surface=closed、qa-a Lease=released；Index `app-evidence-index:4e0e27d2c08066c5088635a1ec1808795272d5fc696c09fee85497b53767f079` sealed、22 项 gaps，结果 evidence_insufficient。64 项规则工作结束且无失败，不等于业务覆盖或 Gate 通过。

### R20 系统返回目标风险修复

- 真实 BACK 的目的文本为“当前前台已离开目标 App 包边界，只允许恢复目标包后再执行 sibling。”，其中“允许”触发目标权限词表。正式边界没有目标控件文本，遂按 Agent 危险语义拒绝；此动作本身没有点击目标，Android Driver 的 BACK 固定派发 `ops.back()`。
- 修复只让目标风险推断对 BACK 返回无目标副作用；命令操作／风险／参数白名单、两次 scope 校验、租约与运行范围校验保持。没有通过改写目的文本绕开词表，也没有放开支付点击。
- 正式执行边界与命令策略两文件共 36 passed。新增 BACK 正例在修改前复现拒绝、修改后恰有一次 dispatch；服务端禁用 BACK 与低风险支付点击均零 dispatch。真实 Cmd 11 指令回放由 `COMMAND_TARGET_SEMANTICS_UNTRUSTED` 变为 accepted，仅证明策略分支，未操作设备。无数据库迁移、无前端菜单／路由变化；真实回源仍待新 Run。

### R20 圆形关闭近邻采样修复

- 六张真实 R20 截图离线对照：旧圆形 isolation 使用按钮全宽 padding，四张真实 X 因远处素材被计入而漏检；半宽 padding 后六张均检出同一真实 X，近邻亮度比均为 0。R9 文字负例在半宽下仍拒绝，R11 眼睛仍由四臂形状条件拒绝，原新人促销 X 与白 X 保留；四分之一宽在历史形状合同下会放过一个文字负例，未采用。
- 生产仅将圆形检测的外围 padding 改为半宽，未改 0.08 阈值、形状条件、广告保护或运行时位置容差。第一次重验拒绝中的粗广告保护框问题保持原保护与恢复语义，未因此同时放宽候选签发。
- `replay-close-target-r20.json` 用 Cmd 10 原始来源／Before Analysis 和 PNG 重放校准、原候选筛选、像素优先及触控校验：修改前唯一候选生成 `(720,1839)`；修改后唯一当前像素候选生成 `(721,2158)`，当前来源绑定与触控校验均通过。此回放不重写历史 Capture，也未执行设备。
- 圆形隔离、关闭重叠、截图理解和交互 Runtime 四文件共 134 passed、7 failed；修改前 HEAD 注入同一套测试为 132 passed、9 failed，差异恰为新增上下远侧素材两例，另外 7 项为相同基线失败。聚焦 12 项、生产／测试 Ruff 与差异检查通过。无数据库迁移、无前端菜单／路由同步；自动清障后态仍待新 Run。

## R21 圆形关闭与回源复测

- 22:22:35 经 `POST /api/v2/tasks` 创建 Task `ee5e6e11-5b0e-4b4d-8d8c-a3ff694954bf`、Run `ebc6dc7c-a66d-463b-957f-dc65d09fc811`，加载 `96e677be`（包含 `c396ee24` 回源策略修复），API/Worker PID 54199/54200。readiness 四项通过，保持同一 APK、qa-a、六类覆盖与原预算；新 Run 尚未完成。

- Cmd 2 完成开屏关闭。Cmd 3 的 Before `ce7fbb7f-518d-597d-871c-ca0ec83c4437` 业务页型为 null、page_state=popup，但唯一当前像素关闭候选绑定有效，22:33:54 实际完成；After `80886ea5-a727-51aa-bdfd-64ff1447b5e4` 已核对 PNG，广告宫格切换为下一张卡片。这是真实空页型清障分支的执行证据，尚未露出稳定业务页。该 After 的“会员推广”摘要不准确，以实际截图及当前控件为准。

- Cmd 4–8 连续完成另外五次首页关闭，累计首页六次。Cmd 8 的来源 `c9a1cc90-8d8f-5701-af72-b004ec198b1d` 与 Before `4c522086-03c6-5fa3-94c4-3cfbd4f2a46f` 当前关闭边界一致，中心 `(721,2158)`；23:00:03 completed，After `173bfe16-145f-52e8-b92b-73f019cd99cc` 已核对 PNG，进入下一张插画广告且仍在首页，未发生 R20 此段的正文误点外跳。队列尚未清完，不计稳定业务覆盖。

- Cmd 9–11 依次推进后段插画／品牌广告；Cmd 12 虽名为“底部浮层关闭”，After `246edb3b-b63f-542b-a437-060dff1029c8` 从终身卡促销切换到新人特惠，故记促销推进而非公告条关闭。Cmd 13 的 Before `03809a72-4df7-5f7c-bebd-3cc953d0a3e5` 再次业务页型 null，但当前关闭重验通过；23:27:24 completed，After `44e60c97-8ea5-5136-9355-2983535c15e6` 已核对 PNG，中央促销与全屏遮罩均消失，首页露出。这是在正式 Run 中完成 11 次首页清障的证据，期间无需人工触控；仍有底部公告条被标记 popup，后续业务导航尚待实测。

- Cmd 14 继续关闭公告条；23:32:49 completed，After `569642ad-1eeb-556b-ac07-38e4097d531f` 为 home/stable。后继 Work `0e1f0989e74c637079ea0298ae233fc0d19836ae782fc243050f262589ed65a8` 的同次证明为 obstruction_removed/visible_business_after_close，covered_page_types=[home]，两种无进展计数均 0。23:34:24，“底栏短剧”成功领取为 running、attempt_count=1，并生成 Cmd 15；实测越过同轮导航领取分支，目标页到达仍待 After。

- Cmd 15 于 23:38:38 completed，After `bd7f2970-805a-523d-809c-dc5b54e1c34d` 为 video_detail/stable，已离开首页。该入口预期 video_list，后继证明为 intermediate/different_business_target_observed，所以短剧 Frontier 按现有规则 SUPERSEDED，covered 仍仅 home；不把稳定实际页型误写为预期页型已验收。只读核查确认幂等发布不重置 terminal 项，当前不存在已证明的短剧→主页自动循环。
- Cmd 16 底栏主页虽 completed，After `75f0c9ca-f66a-5287-9e7c-9ecd4d5b7757` 仍 video_detail/stable（解锁提示），没有回到首页。Cmd 17 改用当前池内 BACK，23:45:46 completed，After `cd3ead02-163d-512b-bc0e-f3f309ce8aff` 仍 video_detail/stable、摘要为播放页。随后另一个底栏分支成功领取并生成 Cmd 18。该 BACK 未复现池外签发或目标语义拒绝；这不是 R20 外部浏览器回源的同一实机分支。

- Cmd 18 completed，After `09ff20aa-4164-5e1a-8ed9-b85fe90c96ca` 的实际 PNG 显示“暗网”底栏蓝色选中、正文为会员解锁页；Vision 的 home/主页选中摘要错误。当前像素关闭候选位于下方公告条，不能将关闭它等同于绕过会员限制。该入口因现有后态证明未通过进入 retry_wait，系统先执行 Cmd 19 公告清障。另发现三个 UIA 底栏候选的 y 边界接近截图末端，正只读核查其坐标来源，尚未据此改生产。


### R21 最终收尾与归因更正

- Cmd 19 公告关闭后，After `deea93b6-c5df-5279-aa37-1dc9af29e5c7` 为 payment/stable。Cmd 20 BACK completed，After `349f613d-4181-564b-9c1b-73d4e4b7633d` 仍 payment/stable；Cmd 21 再次 BACK，00:10:32 completed，After `8b0a6f67-93be-5538-8fb1-c08859f72aa5` 留存但取消前未完成理解。R21 共 20 个实际动作完成（Cmd 2–21），没有执行器错误；completed 不等于业务到达。
- 00:11:52 经正式取消接口停止，00:15:45 Surface closed、qa-a Lease Released。最终证据索引 `app-evidence-index:fa545e38ea9b8a9706dc912ea24938c838946d08820a0003e195b411d1e9bfed`，摘要 `sha256:a4eb5abcf85f530875f40dbdca08c97df0b5d1f9cf3c72ebf82df0b890b1999a4`；42 项证据缺口，159 项检查完成、0 failed。检查完成不构成 Completion Gate 验收；Run 聚合为 62 次 AI 调用、1,504,532 tokens。
- **旧槽位点击的独立根因**：Cmd 16 的 Before `17b86f1f-0ded-54b8-ba12-9b872cb38fc1` 没有当前底栏节点或 Vision 底栏，Runtime 仍按来源水平槽生成 `(155,2652)`。修改前完整 Runtime 回放可复现一次错误点击；删除源槽合成后拒绝执行并要求重观察。提交 `d15a3859`，完整 Runtime 86 passed。不能将此动作直接归因于下一条 UIA 尺寸问题。
- **UIA 坐标空间根因**：`09ff20aa-4164-5e1a-8ed9-b85fe90c96ca` 原始 UIA 根为 1440×2730，而 Capture 为 1440×3040；主页、社区、我的三个标签对齐候选把 2669..2730 除以 2730，再乘 3040，错误落到 2972..3040。新的统一缩放恢复完整 Capture 坐标空间；横屏、缩略图、无根尺寸及非法尺寸均有覆盖。原始底栏仅 61 px 高，既有 3% 高度门槛仍不签发整组受信底栏，但唯一标签对齐使用正确坐标。
- **付费页 BACK 的准确解释**：Work `410a9a2a22eceb13c14886291486e1fd4dee186e0a24ad876aa89b940b753e78` 固定 observed_at=00:05:45 回放中，所有候选通过基础资格；required 队列时间消耗 5,749,936 ms，超过其 5,040,000 ms（84 分钟）分配，所以五个底栏和预期 home 的 payment BACK 被过滤。discovery 仍余约 6.85 分钟，普通 BACK／observe／scroll 可用。总时间尚未耗尽，动作也仅 18/231；这是子队列时间耗尽，不是 payment 禁止导航、坐标安全带过滤或全局动作耗尽。外层约 90.18 分钟的业务时长地板与 scheduler 的 84 分钟分配是不同合同。此前即时观察中的相反推测以本条为准，本轮未修改支付策略或预算代码。
- **验证边界**：坐标相关三文件联跑 71 passed、3 failed；三个候选数量／排序失败用修改前截图理解模块逐一复现，未作为本轮新增回归处理。Ruff、diff 检查通过。下一轮任务时限由本次测试配置的 120 改为 180 分钟，动作上限 330、六类分母及生产队列分配策略保持；为已证实的有限广告队列留出后续导航时间，不能据此预告完整覆盖。


## R22 当前底栏与 UIA 坐标复测

- 9 月 13 日 00:36:33 通过 `POST /api/v2/tasks` 创建 Task `b7468990-c3f7-40f8-8d13-5d42d6d90591`、Run `a10e60cb-13bd-4c0b-b651-bc94bd5e6cdc`。运行代码 `bdef83f0`（包含 `d15a3859`），API/Worker PID 64739/64740；readiness 四项通过。
- 仍使用同一 APK build_ref、qa-a 和本地 Docker PG／Redis。六类覆盖目标和 330 动作上限保持，测试时限调整为 180 分钟，生产预算代码未改。00:37:58 完成首次安装／启动，进入 bootstrap_capturing，暂无运行错误；本轮尚未形成覆盖或 Gate 结论。

- 00:44:07 Cmd 2 开屏关闭 completed，After `46d22743-c673-521c-b816-d4d3814ca27f` 已人工核对证据 PNG，进入首页广告宫格，中央白色 X 仍在。Cmd 3 于 00:50:13 completed，After `2ffe121c-8378-5fff-8898-6f47896c4d65` 留存，视觉理解待完成；尚无稳定业务覆盖。
- 持续跟踪已启用，每两分钟读取当前 Run 状态；无实质变化保持安静，发现已证实阻断时继续正式取消、收尾、分析修复、分批提交与复测。当前 Run 仍在运行，不是最终验收结果。

- Cmd 4 第二次首页清障于 00:56:40 completed。After `054bd128-6c24-54ba-9e71-cec2fcf18e97` 使用 heuristic 降级理解，仍签发正确圆形 X；Cmd 6 的 Before `b838ede8-9cbf-543f-9a40-0d8ba329eb50` LLM 将 dismiss 框错标到 y=.545..60、medium 广告正文框标为 y=.60..84。两张原始 PNG 已核对，真实 X 一直在 `(721,2158)` 附近。像素检测对两帧均唯一检出 X，但 Before 的广告保护框覆盖它且与错误 dismiss 框不相交，故过滤像素补正；Cmd 6 重验失败，没有执行该次错误点击。
- 系统补采恢复帧 `14504a1a-f83c-5099-86c6-df96d08bbc90` 后继续 Cmd 7，01:07:05 completed，After `b9adb5a6-b0bf-5376-ab6c-6713c624e5f9` 已核对原始 PNG，广告素材确实切换到下一张，目标 App 首页仍在背景，未外跳。这是单次拒绝后自动恢复执行，尚不构成持续阻断；本轮不据此放宽广告保护几何。

- Cmd 8–16 继续完成后续广告关闭；Cmd 16 的 Before `2986071e-faf2-52db-9409-7c2dc8f6cbbd` 业务页型再次为 null/popup，仍按当前关闭证据通过执行。01:49:57 completed，After `3247e9a0-09c8-53e7-abc6-f229872b62f9` 已核对原始 PNG：中央广告及遮罩消失，首页内容和底栏可见，只剩公告条。R22 至此有 12 个首页关闭动作 completed（Cmd 3、4、7–16），其中不能把每次 completed 都当成有效清掉一层；Cmd 6 拒绝后已自动恢复并实际推进。下一步仍需核对公告条、稳定首页证明及底栏导航。

- Cmd 17 公告清障于 01:54:17 completed，After `08db360d-5452-5465-be4a-f08f74988252` 为 home/stable；Work `exploration-work:8769c6fff7ba985d10777bf5bcbed806891b22f474102583aab79a4fdec3c45e` 同次证明 obstruction_removed/visible_business_after_close，covered=[home]，noop/no_progress 均 0。01:55:08 “底栏短剧”领取 running，Cmd 18 进入 executing；底栏到达及本轮两项导航修复仍待 After 实测。

- Cmd 18 底栏短剧于 01:57:55 completed，After `e3ea735d-394e-55f4-b457-21fd7d32b33d` 为 video_detail/loading，故当时不计新覆盖。Cmd 19 按当前候选观察加载完成，02:01:01 completed；After `e64adce5-8afa-5508-9610-f00663e66b31` 为 video_detail/stable。Work `exploration-work:859d075dba89f5b4f3987a896b18f581685c1a460ab7d8562d583fcf126b98ce` 证明 observation_completed/bound_capture_observed，covered=[home,video_detail]，noop/no_progress=0。这是实际详情页型覆盖，不能替代入口原预期的 video_list。Cmd 20 底栏主页已领取，实际回源结果待 After。

- Cmd 20 底栏主页在 Before `a21e929f-4a09-56cd-ba5c-5d3f0f748240` 没有当前主页候选（当前为第 2 集解锁详情）时返回 `POST_SCROLL_REBIND_REQUIRED`，没有沿用 R21 的旧水平槽完成虚假点击。随后 Cmd 21 执行当前系统 BACK，02:06:46 completed；After `a8bbe38d-0c7f-57be-836d-8110fdfc361c` 仍 video_detail/stable、第 1 集播放，证明 return_relationship_unconfirmed，不能称已回首页。系统已领取另一个底栏分支，Cmd 22 底栏暗网 planned；暂无重复返回循环证据。

- Cmd 22 底栏暗网于 02:11:09 completed，After `083d6613-1fd3-5914-a520-d2241dbbd569` 识别为 home/popup、会员解锁促销，不据此确认 content_list 到达。该帧 `trusted_accessibility_candidate_alignment` 已实测记录 screen_width=1440、screen_height=3040、aligned_candidate_count=3；主页、社区、我的三个 uia_aligned 候选 y=.8779605263..8980263158，乘 3040 恢复原始 2669..2730，修复了 R21 同类 UIA 根窗口拉伸问题。Cmd 23 正在处理当前公告关闭，仍需继续验证跨分支实际到达。

- Cmd 23 于 02:15:12 重验失败，Cmd 24 自动恢复并完成关闭后，系统继续底栏分支；Cmd 25 底栏 AI 于 02:24:36 completed，Before `03ea013f-ca0c-5ff2-b63b-a0fa5180f8de` 为 payment/stable，After `b7c1996f-8878-57b3-813f-0e14feebcf9e` 为 content_list/stable（AI 模板网格）。Work `exploration-work:756e74490a79856171bc4935e8c9bdcbdf0a9566f6a5049b7c55fc0fd1fb7a58` 为 tab_seek_screenshot_evidenced/after_page_matches_expected_tab，AI Frontier 已 VISITED；但本次 covered 仍 [home,video_detail]，正在核对覆盖累积合同，暂不报 3/6。Cmd 26 底栏社区执行中。

- 覆盖合同只读核查完成：`tab_seek_screenshot_evidenced` 单独用于底栏访问／Frontier VISITED；`arrival_proven` 与 `_next_coverage_metadata` 的正式业务覆盖累积另有条件。AI 页当前只有 Tab 访问证明，未满足该累积路径，符合现有合同，不能把 VISITED 直接算为第三类业务覆盖；本轮未改覆盖定义。
- Cmd 26 底栏社区于 02:29:14 completed，After `e57acd03-92f3-5c79-b3cb-fddf11165fb2` 为 post_list/popup，只取得 Tab 访问证明。Cmd 27 继续关闭公告，02:33:39 completed，After `73b3a0f9-3c1c-543b-9042-ceb2a4221e58` 为 post_list/stable；Work `exploration-work:24d086dda6d8994a9124d96c8f02b46042a41d7701068f6f50acb5e6b32906bd` 为 obstruction_removed/visible_business_after_close，covered=[home,video_detail,post_list]，noop/no_progress=0。R22 此时累计三类覆盖，Cmd 28 底栏我的执行中；仍无完整六类覆盖或 Gate 结论。


### R22 账号提示阻断与收尾

- Cmd 28 底栏我的于 02:37:30 completed，进入 profile/modal 的账号凭证提示；Cmd 29 底栏漫画因当前帧没有匹配控件被拒，未沿用旧槽位穿透。Cmd 30、31 的当前按钮“我已保存”均标记 low/dismiss，但连续被 Driver 以 `COMMAND_TARGET_SEMANTICS_UNTRUSTED` 拒绝，retryable=false；After `e5721f31-9fb4-54b0-809e-40844d7c169f` 仍为同一账号提示。两次均非正常关闭进展，故正式取消本轮处理确认链路。报告不保存账号或二维码正文。
- 02:45 前经取消接口返回 200，Run finished_at=02:45:03.121038、Task/Run 均 canceled。Cmd 32 在取消阶段以 `APP_WORKFLOW_CANCELED` 结束，不计为完成动作。全部动作表复核为 23 completed、7 failed；失败分解：2 次普通候选重验、2 次旧导航重绑拒绝、2 次确认语义拒绝、1 次取消。此前中间摘要将 Cmd 23、24 均写作完成有误，已依据全量动作表更正为 Cmd 23 失败、Cmd 24 恢复完成。
- Run 当前聚合 64 次 AI 调用、1,690,146 tokens；阶段同次覆盖为 home/video_detail/post_list 三类。02:48:09 Surface closed、qa-a 租约 released；240 项检查完成、0 failed。索引 `app-evidence-index:996a372aec8f0a275c0b1e1b516305155ba188426518664a00330fe651c667b5` 于 02:48:04 封存，摘要 `sha256:c9aa7823963d0c3168832ae0117aa9fe4b17b539b1325da33da75ff2b50b68a7`，60 项缺口，正式结果 evidence_insufficient/evidence_incomplete。最终取消摘要没有重新生成覆盖累积，三类覆盖引用前述同次 Work 证明；不据取消终态或检查完成宣称完整覆盖或 Gate 验收。

### R22 阻断修复与真实事实回放

- 根因在 Official 执行边界丢失可信候选语义：上游 Runtime 对当前 low/dismiss 已通过校验，Official 再次调用同一策略时没有可信角色，按“保存”触发语义拒绝。Driver adapter 虽进入，bridge/device 均未执行；这不是点击无效或设备异常。
- Runtime 新增当前重绑后 `resolved_candidate_id`；Official 根据该 ID 回读同 Task/Session/Capture、目标包及截图制品的数据库候选，再校验来源签名、尺寸、方向、实际触点、目标文案与风险。请求自报角色不能签发信任，原命令范围与两次租约校验保留，无迁移、无前端菜单／路由变更。
- 真实 Cmd 31 的来源 `4d64fd3d-0f78-52bb-9daf-c97af578d825`、Before `bd6455bf-339d-59e8-bbdd-17a9b8095859` 经完整 AppCandidateResolver 只读回放，解析到持久化候选 `candidate:f3b1609217753d4fb6e2b4a39342dcb8ce6ff428059efdb563f95b94bfa34908`，low/dismiss、1440×3040 的 `(446,2082)`。原策略返回 `COMMAND_TARGET_SEMANTICS_UNTRUSTED`，新 Official 绑定校验通过；设备调用为 0，实际关闭仍待新 Run 后态。
- Runtime／命令策略／执行上下文三文件 135 passed；数据库读取及 Official 相关测试联跑 94 passed、1 failed、1 skipped。失败为 HTTP 500 的错误码合同断言；已在 detached worktree 的修改前 HEAD `62490ea6` 复现，同文件基线 27 passed、1 failed，唯一失败为该 500 用例。跳过项为未配置的 live probe；Ruff、diff 检查通过，不能据此声称全部接口回归通过。


## R23 Official 当前关闭语义复测

- 9 月 13 日 03:14 经 `POST /api/v2/tasks` 返回 202，创建 Task `cfd1fae5-30fd-482c-9e9f-8402ebeaacdc`、Run `ba8d7536-3a5b-41fc-bf0c-edbf90775cbe`。加载提交 `e0970b78`，API/Worker PID 71493/71494，readiness 四项通过。
- 同一 APK build_ref、ONIRENT qa-a、本地 Docker PG／Redis；180 分钟、330 动作与六类分母保持。03:14:53 Task/Run running、Surface starting、qa-a 租约 active，暂未产生首帧或动作。
- 持续跟踪已切换本轮，每两分钟读同次事实，无实质变化保持安静；出现已证实阻断继续按原授权取消、修复、提交与复测。实际关闭账号提示和最终 Gate 均待验证。

- Cmd 2 开屏关闭、Cmd 3–13 共 11 个首页关闭动作均 completed，期间无执行错误。Cmd 13 于 03:58:32 完成，After `38297a01-afa4-5864-9a28-2da6b0828665` 原始 PNG 已核对：中央广告遮罩消失，首页内容及底栏露出，仍有底部公告。Cmd 14 正在关闭公告；此时尚未取得稳定业务覆盖证明，不能把 completed 数量直接等同于有效清障层数。

- Cmd 14 公告关闭于 04:01:37 completed，After `94e3aefa-c538-5bea-b298-43167363125f` 形成 Work `exploration-work:ea49d4e2def544c9200a747ddcee7d132ac8639537770b685f9d36ae1cad6512` 的 obstruction_removed/visible_business_after_close，covered=[home]，noop/no_progress=0。Cmd 15 底栏短剧于 04:04:52 completed，After `703b530d-0f19-5f5c-b735-2109cac55821` 留存，目标页理解待完成；本轮至此无执行错误，但尚无第二类覆盖证明。

- Cmd 16 等待加载完成于 04:07:17 completed，After `74e3b8b1-f2e7-5181-8958-33630eec4488` 为 video_detail/stable。Work `exploration-work:65fda72afa42a7428e4ab0db826f108f88c668ec2a01bbd5d6bd6aafba82ef63` 为 observation_completed/bound_capture_observed，covered=[home,video_detail]、noop/no_progress=0；当前为两类阶段覆盖，不能将详情页计作 video_list。Cmd 17 底栏主页 planned，继续等待当前控件重验与返回后态。

- Cmd 17 底栏主页以 `POST_SCROLL_REBIND_REQUIRED` 拒绝旧坐标，Cmd 18 当前 BACK 完成后仍为 video_detail/stable。Cmd 19 底栏暗网完成，After `34ec447a-74d0-5cfe-a8d2-accb87ae2a7e` 为 payment/stable；Cmd 20 继续社区导航，After `236c43b5-7e6f-511e-b579-d36d8fd92294` 为 post_list/popup，仅有 Tab 访问证明，covered 仍两类。
- Cmd 21 关闭动作于 04:22:45 completed，After `af0aa2fd-7620-566c-9ea4-c45f3197eec7` 却为 profile/modal 账号凭证提示，不能视为社区清障。Before `d266d17b-dcd2-54b9-88d4-1a60fa3ee211` 原始 PNG 已核对，公告 X 在底栏上方；Vision 给出的 dismiss 框 y=.88..91 与 UIA 我的底栏区域冲突。实际点击轨迹和放行分支正在只读核查。Cmd 22 “我已保存”已进入 evidence_pending，未再返回 R22 的语义拒绝，实际提示关闭尚待 After。报告不存凭证正文。

- **e0970b78 真机修复已成立**：Cmd 22 “我已保存”于 04:25:19 completed，After `b5071d31-c405-5f53-800d-7581560d3730` 原始 PNG 已核对，账号凭证提示确实消失、个人页正常露出；不只是在 Official 校验或模拟桥接通过。此结果证明当前关闭链路恢复，不能抵销 Cmd 21 的异常跳转，也不能代替六类覆盖或最终 Gate。


### R23 公告误触、无效导航与正式收尾

- Cmd 21 的 action_trace 制品 `c407c8e6-2cd5-56d6-b712-6d0e45aa204d`（绑定 After `af0aa2fd-7620-566c-9ea4-c45f3197eec7`）记录 `resolution_method=parameters.resolved_target`、实际 `(1354,2721)`、command_result=ok。该点同时落在错误 Vision dismiss 框与当前 UIA“我的”底栏区域，结合实际 profile/modal 后态，确认误触；不是正确清障。候选来源帧与动作 Before 分离是原重绑定合同，不能仅因两个 Capture ID 不同归因。
- Cmd 22 提示正确关闭后，Cmd 23 短剧与 Cmd 24 主页均 completed，但 After `1160efe8-fb90-5d16-b284-6d4f30384223`、`0b517af0-f8aa-5345-a170-4381b55f9242` 仍个人页，Cmd 25 又开始主页动作。04:34 经正式取消接口返回 202，Task/Run canceled，finished_at=04:34:17.177086；04:37:18 Surface closed、qa-a 租约 released。
- 动作台账共 22 completed、2 failed：Cmd 17 当前底栏重绑拒绝，Cmd 25 为 `APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION`，取消时已派发但设备后果未知，不能计为成功。阶段覆盖仍 home/video_detail 两类；当前 Run 聚合 53 次 AI 调用、1,338,237 tokens，04:37:14 封存索引 `app-evidence-index:51162d86ff28c665c4464c029e2ea0ffd4103fb54d7ebe1c3ba0b51b8643b674`，摘要 `sha256:428e7317b5029d88a035d03631ed11381586f8b8fbf127ef416bea7073c80265`；49 项证据缺口，结果 evidence_insufficient/evidence_incomplete，206 项检查完成、0 failed。取消终态未重新累计覆盖，两类引用前述同次 Work；检查完成不等于 Gate 通过。


### R23 固定底栏抬升修复

- Cmd 23 短剧与 Cmd 24 主页的持久化 action_trace 分别记录 `(331,2652)`、`(101,2652)`，均使用 parameters.resolved_target，runtime_ui_tree=not_used。当前 UIA 未提供可用底栏后，Runtime 对 Vision 候选无条件套用固定 .84..905 条带，制造同一 y=2652；不能将它视为模型给出的真实落点。
- 删除该上抬函数和两处调用，保留原手势带判断，当前合法 UIA／Vision 候选仍可执行。完整 Resolver 使用两步真实 source/Before 只读回放，旧实现复现上述实际落点，新实现均返回可重试 `POST_SCROLL_REBIND_REQUIRED`。这是取消错误坐标生成，尚不能声称已获得正确当前底栏或导航成功；UIA 旧弹窗事实来源继续核对。

- 主工作区完整 Runtime 回归 87 passed，覆盖普通 App 底栏和 tab_seek/vision_band 两条路径拒绝补造坐标且 Driver 零执行；安全当前 Vision 框及当前 UIA 优先正例保持。Ruff、diff 检查通过。


### R23 公告 X 评分量纲修复

- 原始 Before d266 在像素检测层检出两个白叉：内容“X视频”的字母 X `(11,1143)-(34,1173)`，公告真实 X `(1342,2585)-(1377,2620)`。无广告保护过滤，两者都进入排序；旧评分用归一化宽高比判断方形度，真实 35×35 px 控件被算成 aspect≈2.11，方形分为零，最终两项得分 .131781/.144106，差小于 .08 且弱分不足，放弃校准。
- 仅将方形度改为像素宽高比后，两项得分约 .249115/.364106，差 .114991，现有排序自然选中公告 X，无需改阈值或新增点击门禁。真实 source/Before/PNG 经校准和完整 Resolver 回放，旧落点 `(1354,2721)`、新落点 `(1360,2602)`，校准事实 mode=rebind、ambiguity_count=2、precise_tap_refined=true；没有执行设备动作。
- 主工作区截图理解／关闭重叠／圆形隔离三模块联跑 54 passed、3 failed；三个失败均为此前 UIA 候选数量／排序基线，使用提交 eaa10f7f 的旧评分函数重跑同三项仍全部复现。新增非方形屏幕多 X 回归通过，Ruff、diff 检查通过。


### R23 UIA 陈旧事实诊断与采集隔离

- Cmd 22 Before/After、Cmd 23 Before/After、Cmd 24 Before/After 六个 Capture 的 UI-tree artifact 均不同，却都是 7322 bytes，SHA256 `f99ed58d4b3ae27d7ce3d43b87984a1782faf96975523f88249b8ffb5be7b147`，逐字节保留已消失账号提示。实际路径为 Onirent static ADB，所有采集请求成功；非零 dump 原本会抛错，但日志未记录远端文件 mtime／刷新结果，无法在“返回成功但固定文件未覆盖”与“Android 内部 Accessibility 陈旧”间确定唯一根因。
- 当前 Vision 底栏原始框本身也落入系统手势区，移除固定抬升只消除补造坐标，不能据此声称导航已解决。每次 UIA dump 改用独立远端 XML，只读并清理本次文件，防止静默沿用历史文件；保留原错误、超时和 stdout 回退。
- 主工作区 ADB／Official／只读观察三模块联跑 **57 passed**，Ruff 与 diff 检查通过；无数据库迁移、无前端菜单或路由变更。
- 释放设备后，手动唯一文件诊断曾返回新 XML（20207 bytes），目标 App 已被收尾关闭，内容为桌面，故只证明通道能取得新树。随后通过新 `AdbDeviceOps.dump_ui_hierarchy` 的只读调用在 dump 阶段达到原 45 秒超时，未取得成功 XML；不能将该诊断写成真机通过。App 内刷新及两项定位修复仍待新 Run 的当前 Capture 和实际 After。


## R24 公告定位与当前底栏复测

- UIA 新方法在设备恢复后只读重试成功：dump／cat／本次文件 rm 均返回 0，使用同一独立路径，取得 20207 bytes 当前桌面 XML，SHA256 `de697b7f50a9b67238a08b2f480b5ee5b96cb74ba6b4eb747b4e215f4ea905b5`，不含旧账号提示。保留前一次超时记录；此结果只确认新采集路径可用，不代表 App 内节点刷新或导航通过。
- 三批代码分别为 `eaa10f7f`（移除固定底栏抬升）、`1be31203`（像素方形度评分）、`cc53016e`（独立 UIA 输出文件）。仅重启 8030，API/Worker PID 77086/77087；readiness 的 PostgreSQL、Redis、evidence_storage、worker 四项及 auth/me 均 200。
- 9 月 13 日 05:06 经 `POST /api/v2/tasks` 返回 202，创建 Task `54a6874c-ed76-4ca1-afaa-06560ba94fa3`、Run `c1983dd2-86b2-45be-ba13-753f01a56acb`。同一 APK build_ref、ONIRENT qa-a、本地 Docker PG／Redis，180 分钟、330 动作与六类分母保持。
- 持续跟踪已切换 R24，每两分钟读取同次事实，无实质变化保持安静；重点核验公告 X 的实际触点与 After、提示关闭后 UIA 的当前事实、底栏实际到达。新轮尚在进行，未取得覆盖或最终 Gate。


- Cmd 4 曾因当前候选不唯一重验失败，Cmd 5 随后自动恢复，后续清障持续推进。Cmd 14 于 05:57:17 completed，After `375f6b6e-7898-5897-b9fc-2ddc5056d875` 原始 PNG 已核对，中央会员弹窗与遮罩消失，公告条仍在；该步只记录 obstruction_replaced，不计稳定覆盖。
- Cmd 15 于 06:02:02 completed，After `99814a0f-ffb7-51e6-99be-a092529a5cbc` 原始 PNG 已核对：公告条及右侧 X 消失，页面仍在首页。Work `exploration-work:4e7a224159df009788a5f2f16eaf6fdd4ae24898320529df1d146e363d13bf7c` 证明 obstruction_removed/visible_business_after_close，covered=[home]、noop/no_progress=0。当前已有一类阶段覆盖；后续社区公告的原误触场景、个人页 UIA 刷新与底栏导航仍待验证。

- Cmd 16 短剧导航 completed 后为带金币解锁遮罩的 video_detail；Cmd 17、18 观察后仍 overlay，不能套用前轮的稳定详情覆盖结论。Cmd 19 “主页”于 06:13:58 completed，After `bff2656b-09da-50b4-a514-b881088a6ba2` 为 home/stable，原始 PNG 已核对确实返回首页；下一步 Before `13a896f0-fe79-58e5-bf33-1ff19fef1212` 再次为 home/stable。系统已继续 Cmd 20 底栏暗网。这证明本次从解锁详情返回首页的导航有效，不能替代个人页旧 UIA 场景验收。


### R24 导航阻断与正式收尾

- Cmd 20 暗网于 06:18:29 completed，After `df3f6a06-1db8-5486-bf60-55539daf6aa9` 为 payment/stable。Cmd 21 社区于 06:24:42 completed，但 After `2670a43a-b0a9-52dd-b325-d73c606a6299` 仍 payment/stable。Cmd 22、23 AI 连续 `POST_SCROLL_REBIND_REQUIRED`，已不再是单次拒绝后有效恢复，06:34 经正式接口取消。06:36:55 Surface closed、qa-a 租约 released；Run 最终 finished_at=06:36:56.285155。
- 动作总表 19 completed、3 failed；失败为 Cmd 4 关闭重验及 Cmd 22/23 AI 重验。43 次 AI 调用、887508 tokens；162 项检查完成、0 failed。06:36:50 封存索引 `app-evidence-index:5f2a72109a0528c088fe03f7a2a850b9b9816b6b03f2dc0a9d4e7ecf584cb974`，摘要 `sha256:8d4c1bb318c3acbb679547d1030ea0c134a86b285e882c0e8de05efcd32e9934`，45 项缺口，结果 evidence_insufficient/evidence_incomplete。取消摘要未重新累计覆盖，当前只可引用同次 Work 已证明的 home；不能将检查完成或动作 completed 当作 Gate。
- Cmd 21 action_trace 制品 `f2a45295-ffb4-5a95-bfb4-814f39710ab9` 的最后动作记录社区实际 `(1360,2602)`、parameters.resolved_target、runtime_ui_tree=RuntimeError。Before `16ce4297-0cbd-5b4e-adbc-21cd27a3eb3a` 中社区 Vision 框落入手势区，公告关闭是唯一剩余安全底部框；`tab_seek` 绕过导航作用域和角色后按水平距离误选公告。真正点的是关闭 X，不能把社区未到达归因于 App 社区功能失效。
- 两条 Vision 建池路径已统一导航身份判定，清障与正文不再冒充底栏；真实 source/Before 完整 Resolver 只读回放，旧实现复现上述点，新实现返回可重试重验错误。主工作区 Runtime 完整 **92 passed**，Ruff、diff 检查通过；无迁移、无前端菜单／路由变更。
- Cmd 22/23 当前 AI Vision 候选实际存在，但中心 y=.96/.95 落入原手势区。它们的当前 Capture 没有 UIA，API 日志多次记录独立 XML 的 `cat: No such file or directory`；不是当前 UIA 有按钮却被错误解析，也不能用 Cmd 20 历史 UIA 冒充当前。新独立文件已阻止历史 XML 复用；为何 dump 没有生成文件仍需补足原命令输出证据，不能先断言 Android 内部缓存或增加重试次数解决。


### R24 非空闲页面的 UIA 根因复现

- R24 收尾后进行隔离诊断，未创建新 Run，所有手动动作只用于定位采集故障，不计正式覆盖。桌面两次、App 启动页三次独立 dump 均取得 XML；首页广告逐层关闭后，进入与 R24 相同的会员解锁页。
- 普通与 `--compressed` 两种 `uiautomator dump` 均退出码 0、stdout 为空、stderr=`ERROR: could not get idle state.`，约 13.4 秒后读取各自独立文件均返回不存在。直接确认本页是等待空闲失败，而非旧 XML 解析、文件名冲突或 cat 通道故障；compressed 不能解除该阻断。
- AOSP DumpCommand 在 `waitForIdle(1000, 10000)` 超时后打印上述错误并返回，尚未执行 root 读取和文件写入，与设备原始输出一致。原始记录保存在忽略目录 `uia-payment-diagnostic-r24.json`，诊断结束后目标 App 已 force-stop，返回 0。来源：[AOSP DumpCommand](https://android.googlesource.com/platform/frameworks/uiautomator/+/17fac436d78f6ac642386a245fb4fdb7243a91a4/cmds/uiautomator/src/com/android/commands/uiautomator/DumpCommand.java)。
- 图片链路另经代码核查为 1440×3040 全图等比缩放至 341×720 JPEG，没有裁剪、补边或坐标重定义；其取整误差不能解释导航候选超过 200 像素的偏移。导航角色修复 `4c8d10c9` 已提交，下一步采集降级仍待验证，不能宣称本页导航已通过。

### 当前节点降级采集的实现与验证

- 仅标准输出明确包含 idle 超时时，执行一次性系统 legacy runner 读取当前 root。主采集路径仍使用独立文件；非 idle 设备错误、传输错误不触发此降级。每次临时 jar/XML 独立、finally 清理，失败不能读取旧文件。XML 标记 `alioth-observation-mode=current_root_without_idle`。
- 真机兼容性诊断发现两个实际约束：runner 需要设备已有 android.test.base.jar；包装脚本把 ANDROID_DATA 改为 /data/local/tmp，而 legacy dump 又追加 local/tmp，导致文件目标错误。改为原 shell 身份直接调用系统 Launcher、保持 ANDROID_DATA=/data，并只向 legacy dump 传本次 basename。无 APK 安装、无权限增加、无常驻服务。
- 生产版 helper 在 qa-a 桌面取得有效 XML 20203 bytes，SHA256 `7bf8624bdb422db9ee15b87f405cc8ff72567be39709485ef5cbf80a76fc19b7`；write/run/cat/cleanup 全部 rc=0，runner 约 2.51 秒。此结果仅证明本机兼容性，会员页降级及实际导航仍待新 Run。
- JAR 由随附 Java 源码与 `scripts/build-adb-ui-hierarchy-helper.py` 构建，本机 Android platform 36、build-tools 37.0.0、Android Studio JBR；连续两次构建 SHA256 相同：`a7578365a755681fcb9728a2e566d70fdd59b2e783d3b431b8c42cb4fec1b564`。`pip wheel --no-deps` 打包并逐字节确认包含同一 jar；部署运行不需要编译工具。
- 主工作区 ADB+新 helper **40 passed**。扩展联跑 ADB、Official API、Official Runtime、Broker、Interaction Runtime 为 **171 passed / 1 failed**，唯一失败是既有 HTTP500 `ACTION_EXECUTION_FAILED` 与 `ACTION_OUTCOME_UNKNOWN` 分类期望冲突；从未修改的 HEAD `4c8d10c9` 导出同组源文件到临时目录后独立复现同一失败。未改该无关契约；本批无数据库迁移、无菜单/路由变化。

## R25 非空闲 UIA 与实际导航复测

- 修复提交 `56fe338b` 包含当前节点降级与打包，已含导航角色修复 `4c8d10c9`。仅重启本次 8030 测试 API/Worker（84247/84248）；`/api/v1/readyz` PostgreSQL、Redis、evidence_storage、worker 四项和 auth/me 均 200。
- 9 月 13 日 08:19 左右 `POST /api/v2/tasks` 返回 202，Task `2b792b70-5d21-4d22-b989-5a956552f710`、Run `868d5461-1547-4cc9-aa07-9ff4fea48697`；相同 build_ref、ONIRENT qa-a、本地 Docker PG/Redis、180 分钟、330 动作、六类分母。08:19:57 PG 为 running，lease active、Surface starting，尚无 Capture；没有据此声称覆盖或 Gate。

- Cmd 13 于 09:07:25 completed，After `fde90f30-0c97-5420-b790-13cb647c77ed` 原始 PNG 证明中央会员广告与遮罩消失，但底部公告条仍在；虽然视觉 page_state=stable，Work 仍为 intermediate/obstruction_replaced、covered=[]，不能提前计入首页覆盖。
- Cmd 14 主页导航 completed 后，Cmd 15 再次关闭公告，于 09:15:24 completed。After `e40ee3f8-a8be-5c87-9fc3-a47cadf9c658` 原始 PNG 已核对公告条及其 X 消失。Work `exploration-work:5369d95e1d0c34986c7e091d262e7a7db7b51dd28971fe755ddeadc902991baf` 记录 obstruction_removed/visible_business_after_close、covered=[home]、noop/no_progress=0。09:18 Cmd 16 短剧进入 evidence_pending，仍在推进；当前只证明一类阶段覆盖，会员页 UIA 降级及底栏实际到达尚待核验。

- Cmd 18 在短剧付费遮罩帧尝试暗网时触发 `POST_SCROLL_REBIND_REQUIRED`；随后 Cmd 20 返回关闭付费半屏，于 09:25:30 completed。After `719a3c62-2ff7-5e43-bcee-cc5b8fd22309` 原始 PNG 已核对为无付费半屏的短剧详情，完整底栏可见；同 Capture UI-tree `a643e63b-0fc2-5bf2-96cb-0841c3bdafe7` 为 15599 bytes、SHA256 `04037b99f0d47b99bc72b5250f377aaef37dfb518f71bbec798d8ebe335709b7`，包含当前六个底栏节点。本帧为标准采集，不能冒充 idle 降级已在会员页验证。
- 同次 Work `exploration-work:8f28fb5e5c2da5e62161570f3073ca258d7a0bd909d0fd412f59b349c472c09a` 记录 covered=[home,video_detail]。此前重验失败已自动恢复，不能据单次拒绝取消任务。Cmd 21 社区于 09:29:22 completed，After 被识别为 post_list/popup，Cmd 22 正清障；社区稳定覆盖与会员页采集仍未确认。

- Cmd 22 社区清障 After `66fda932-648a-5ade-9b08-23d2718252f5` 原始 PNG 已核对：社区帖子信息流及完整底栏可见，底部公告条消失。同次 Work `exploration-work:e5ea0e58e5ab18dfaf125412969e61c0a6e0f194904ce0903cd92e9eb55af4d5` 记录 obstruction_removed/visible_business_after_close，covered=[home,video_detail,post_list]，形成三类阶段覆盖。
- 本帧 UI-tree `17151183-0afb-5734-9f24-dce9df951168` 为 25707 bytes，SHA256 `83c35916104fc50be95ccaff2a109c5df771df73c1067372ace28da2b748965d`，根属性 `alioth-observation-mode=current_root_without_idle`，包含当前六个底栏可点击节点。API 09:29—09:39 多个采集均记录该模式，证明降级工具已在正式 Run 的 App 内生效；这不替代尚未复现的会员页导航验收。
- Cmd 23 AI 底栏单次重验失败后，Cmd 24 安全下滑于 09:39:36 completed，任务仍在推进，未据单次拒绝取消。另行只读核对 Cmd 23 自身 Before 的候选与 UIA，不能拿 Cmd 22 的节点替代该帧事实。

### R25 自行收束与正文关闭误检

- Cmd 25 所请求的关闭候选来自像素签发，当前 Before `926ec6da-72bb-531e-9d38-9b63bfa0bc3c` 的实际触点 `(463,2312)` 位于第二条置顶帖正文／素材；After `74053eb9-190d-5129-99b8-5f43ab3b2243` 进入 content_detail。该步是误点正文导致导航，不能算成功关闭。
- Cmd 27/28 实际均点 `(312,623)`，原始详情截图中对应「海角社区 APP分享活动」的「区」字内部交叉。检测原框 `(299,610)-(327,637)`；同帧校准前 vision_popup_surface=false、dialog_visible=false，校准器却自行签发关闭并改写 popup_visible/page_state。最终 observation `b097b979-6660-5a5e-9e6b-8a345a0b25f6` 原图已核对没有弹窗。
- 09:48:09 最后 Work 自行 bounded，obstruction_not_removed、dismiss_noop/no_progress=3，covered=[home,video_detail,post_list]；主代理未发取消。09:49:19 Surface closed、qa-a lease released。09:58:52 443 项检查中仍有 155 nonterminal、0 failed，Task/Run 尚 running、Gate 尚空；设备释放不代表最终收尾或六类完成。
- 白色 X 原全宽孤立窗口对「区」字的周围白笔画比例仅 0.0713，低于 0.08；半宽近邻升至约 0.148，能排除正文。仅调整白色交叉近邻窗口，不按页型禁止关闭，不修改动作风险、像素评分或重试上限。
- 当前真实截图回放：主目录 67 帧仅最终正文误检发生变化，其余检出保持；另对 R25 Cmd25—28 六帧回放，误点交叉均消失。新增无字体依赖、三种倍率及真假混合测试；旧实现对新增用例 4 项失败，新实现全部通过。主工作区校准／理解／Runtime 联跑 **151 passed / 3 failed**；三个理解候选数量／容量期望失败均在未修改 HEAD 模块上复现。Ruff、diff 检查通过后单独提交；无迁移、无前端同步。

### R25 当前底栏投影修复

- Cmd 23 自身 Before `b820885e-2b04-5c30-975e-8c63c825a873` 的 UI-tree `39eafc97-35b2-5cf4-9c5b-1fdce25629bc` 为 25354 bytes，包含 idle 降级标记及六个当前底栏节点；AI 原框 `[720,2669][946,2730]`。原 trusted_accessibility_controls.bottom_navigation_count=0，是投影按 3040 整屏过滤底边／高度所致，不能归因于 UIA 未采集。
- 当前目标包的全宽 root 高 2730；修复以该当前根判断贴底，同时保留整屏下部和组结构约束，仅在根证据充分时允许较矮导航。所有输出仍是原始 UIA 像素边界，按完整截图尺寸归一化；未恢复固定抬升或根高缩放。
- 新测试覆盖全屏／裁剪根、正文横排、小根与外包节点，旧 HEAD 对其中三项复现失败。主工作区 Surface、Understanding、Runtime 联跑 **163 passed / 3 failed**，仍是已复现的三个理解候选数量／容量既有失败。真实树投影返回完整六栏。主代理已查看该 Before 原始 PNG，完整融合与 Resolver 回放另行核对，新设备到达仍待后续 Run。

- R25 探索索引实际已于 09:49:15.158707 封存：`app-evidence-index:ce8ffab1876a6adbf88141c28b9588a8f85a4ed411511d191f7693a89b509684`，SHA256 `a5177cbeaabcf742b844de3cf3703d6004c7211455b39c1dd98dc379d2d7b195`。探索结果 evidence_insufficient，原因 bounded_exploration_no_progress_limit；索引 gap_count=0 不代表覆盖完成。25 个动作中 23 completed、2 failed；Rule 检查及 Task 最终结算仍在后续进行，不能将探索结果 completed_at 当作整个任务终态。

- 同一 Before 的完整 Understanding 融合与 AppCandidateResolver 只读回放已通过：20 个融合候选含 5 个 uia_issued，AI 候选 `candidate:3f24e53f28fa9097bfe4662f2a726330e49caf13b2fa3b29b390a5721eb2740f` 保持当前 Capture／截图签名；Resolver 返回 `(833,2700)`、capture_px、1440×3040，来自 AI 原框中心，未借用旧树或固定抬升。证据保存为忽略目录 `replay-understanding-r25-c23.json`。此回放不修改原失败记录，不发设备动作，不代表新 Run 已到达。

### R25 最终结算与 R26 启动

- R25 Task 于 10:26:00.930203 进入 review；主 Run `868d5461-1547-4cc9-aa07-9ff4fea48697` 最终 failed/incomplete，原因 bounded_exploration_no_progress_limit，26 steps、111 AI calls、3763180 tokens。检查 Run `aa9a6bd3-6ac4-5078-ab0f-f37326c7e64c` 为 done/app_inspection_work_drained、32 AI calls、954677 tokens；分别列示，避免混用 Run 统计口径。
- 443 个 enforced 检查全部终结、0 failed；最终 Gate=false，阻断原文为 nonterminal_enforced_work_item、review_policy_unsatisfied、app_coverage_or_visual_quality_incomplete。检查计数与 Gate 阻断分别如实保留，不以“检查清空”覆盖 Gate 的否决。设备／Surface 已释放，探索封存证据不足，不能宣称六类完成。
- 两批修复 `4f8cbce4`（正文交叉）与 `c097f52f`（当前应用窗口底栏）分别审查提交。等待 R25 正式收尾后，仅重启 8030 API/Worker 为 89157/89158；readiness 四项依赖与 auth/me 均 200。
- 9 月 13 日 10:27 左右通过 `POST /api/v2/tasks` 返回 202，R26 Task `b8bf5f3a-e262-4748-9318-786e48227d00`、Run `94c1be8c-d366-48fe-90ad-1a1ebbc9db4a`。沿用同 APK build_ref、ONIRENT qa-a、本地 Docker PG／Redis、180 分钟、330 动作和六类分母。重点核对 AI／会员页的实际导航 After 及正文不再出现像素假关闭；新轮尚未获得验收结果。
