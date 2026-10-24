---
name: app-workflow
description: Use when an Alioth quality-inspection task targets an Android App and the runtime must coordinate APK bootstrap, screenshot-only observation, semantic ActionIntent exploration, recovery, and auditable handoff without DOM or device-driver access.
compatibility: Alioth App Workflow Runtime v2
metadata:
  version: "1.7.0"
---

# app-workflow

## 核心原则

让确定性 Workflow 管理设备副作用，让 Agent 只消费已提交的截图观察并选择语义动作。PostgreSQL 是 Session、Bootstrap、Capture、Work Item 和终态的唯一事实源；Skill 只说明执行纪律，不保存运行状态，也不替代数据库规则。

## 输入门禁

开始前确认任务持久化目标满足：

- `target_platform=app` 且锁定 `workflow_version=v2`；
- `platform_build_ref` 指向已验证 Android APK；
- App QC Feature Flag 已启用，Runtime Provider 为任务启动时锁定的 `fake` 或 `official`；
- Capability Manifest 中 session、install、open、screenshot、click、scroll、back 和 close 均为 `ready`。

任一条件不满足时返回明确能力或配置错误，不得通过 H5 URL、Playwright 或 mobile-demo 旁路继续。

## Bootstrap 顺序

严格按 `install → open → bootstrap capture → APP_READY` 推进：

1. Workflow 以稳定 `session_id` 建立 Android Surface Session，并保持 `starting`。
2. Workflow 使用构建引用和 checksum 调用 `AppBootstrapPort.ensure_installed`。
3. 安装最终成功后调用 `AppBootstrapPort.open_app`；“请求已接收”不等于成功。
4. 打开成功后发起首屏 Screenshot Capture，校验并提交 Artifact、`qc_capture` 和 `qc_app_capture`。
5. CaptureCommitted 后原子写入 Bootstrap Checkpoint、激活 Session，并发布只含引用的 `AppReadyHandoff`。

每个外部调用前后都保存 stage、attempt、request id、row version 和错误。恢复时先读取 PostgreSQL；已确认步骤不得重复执行。Redis 只传 task/session/build/capture 引用，不传 APK、截图正文、Token 或设备句柄。

## APP_READY 后探索

APP_READY 不是任务终态，也不得写入 passed。它只允许 Screenshot Understanding 读取 `screenshot_artifact_id`，产生绑定源 Capture 的候选目标，并把 Exploration Work 交给 Explorer。

**APP_READY ≠ 业务首页到达。** 开屏广告、权限框、登录／年龄门、系统弹层仍可能挡住业务面；未清障前不得把当前帧计为业务覆盖或声称已进入首页。

Explorer 只能：

- 从当前截图的候选集合选择 `candidate_id`；
- 产出 click、scroll、back 或 observe 的语义 `ActionIntent`；
- 在预算耗尽、无安全候选或达到目标时返回明确探索终态。

探索策略必须是通用动态策略：

1. 先识别当前页面类型、稳定态、遮挡和可见业务地标。
2. 根据当前截图动态生成候选，并把 `page`／`app` 范围的安全 sibling 登记到动态 Frontier。
3. 对照 `app_goal` 与页面类型覆盖缺口，优先选择尚未覆盖且信息增益更高的候选；候选的预期页面类型只用于排序，不能作为覆盖证据。
4. 每次动作后只信任新的 Capture 和页面判定更新覆盖；当前分支耗尽时使用 BACK 恢复，再领取全局 Frontier 的其他分支。
5. **不得按 APK 文件名或包名选择固定 Playbook**、Tab 列表或自行拼装逐 App 点击脚本。产品固定 `*_script_override` 默认关闭，仅 `ALIOTH_APP_EXECUTION_MODE=legacy_script` 对照回放。默认走 Agent 通用合同（开屏语义相对／弹窗 Watcher cascade／底栏语义相对），只读 [app-product-capability](../app-product-capability/SKILL.md) 与观察中的 `app_product_capability_facts`；**禁止**用 Profile 绝对坐标覆盖当前帧候选。

完整质检还须读取 `app_coverage.app_rule_inventory_coverage.pending_rules` 的冻结检查点、页型、状态、角色与 `missing_evidence`，选择当前真实候选补齐规则取证需求。到达列表不代表已完成滚动或回顶检查，关闭广告也不能替代目录切换材料；按合同完成相应动作并等待新 Capture。缺少控件、可信位置或登录资料时保留缺口，不能猜坐标、将未执行当不适用或替 Judge 宣布结论。动态策略不受旧课纲巡栏顺序约束。

Explorer 不得调用 Driver、安装或打开 App，不得构造坐标、CSS、XPath、Appium locator，不得选择 input/reset/install/open。实际目标解析、Session Command Permit、动作执行和 before/action/after 采集由 Runtime 完成。

## 探索操作规程

对应需求表「App 探索 Skill」六项，并补充长图／外跳取证／收银台长链／连续拖动证据纪律。  
标注约定：**Skill 指导**＝决策纪律（违反浪费预算）；**平台强制**＝Runtime／Driver 拒绝违规执行。

| 需求表项 | 本规程 | Skill 指导 | 平台强制 |
|----------|--------|------------|----------|
| 界面结构优先 | §1 | 有结构证明时优先选该候选 | UIA 精确／唯一语义命中优先于视觉点；整屏蒙层过滤 |
| 视觉识别降级 | §2 | 无结构时只消费截图候选 | 禁止 Agent 自造坐标；无点则 fail-closed |
| 滑动后重新定位 | §3 | 滚后等新 Capture 再选 | 业务目标禁止沿用旧框；`POST_SCROLL_REBIND_REQUIRED` |
| 系统弹窗 | §4 | 先低风险清障，再判业务 | 高风险允许／安装／支付语义拒绝推进 |
| 外部 Activity | §5 | 外跳只恢复，不点业务 | 包边界门禁拒绝外部业务点击 |
| 返回流程 | §6 | 语义 `back`，以新帧验收 | 外跳／恢复路径受控；禁止猜左上角 |
| 长图与视口证据 | §7 | 视口主证据；序列留血缘 | 拼接总览不得冒充主证据／独立到达 |
| 广告外跳取证 | §8 | 源→落地→回源三联 | 外页不计业务覆盖；一生一次调度 |
| 收银台长链 | §9 | 店内逐步观察 | 外部支付页只恢复；高风险确认不推进 |
| 播放器连续拖动 | §10 | 拖前／拖中／拖后多帧 | 单帧不得冒充拖动完成 |

**导航入口发现（需求表补充）**：只选择带 `source_capture_id` 与合法 `discovery_source`（`vision`／`uia_*`／`promoted_*`）的导航候选；开屏固定右上角兜底仅为 `clearance_synthetic` 清障，不得当长期业务入口。Android 无 Flutter Semantics API（H5 `flt-semantics` 不在本栈）。

### 1. 界面结构优先（Skill 指导；解析侧平台强制）

- **何时优先结构**：当前观察若存在可信 Accessibility／语义节点证明（可见、可用、可点击、非整屏蒙层），优先选择带结构证明的关闭／导航／详情候选。
- **不得自造定位**：普通业务点击禁止自造坐标或 locator；目标只能是已提交候选上的 `candidate_id`。
- **平台解析顺序**：同帧唯一 UIA 语义（精确标签变体或唯一包含）→ 否则视觉 `capture_px` 点（经 letterbox／safe_area 换算）。整屏 `Dismiss`／同名蒙层会被过滤，不得指望点屏幕中心关广告。
- Native／WebView 常有结构；Flutter／贫瘠 UIA 时常无结构——无结构时转入 §2，不要强行“找控件树”。

### 2. 视觉识别降级（Skill 指导）

- **降级条件**：结构不可用或不可信时，截图是探索主事实；只消费当前截图候选集合，由 Runtime 落下设备点。
- **禁止盲点**：禁止猜测 `point`、百分比或「大概在右上角」；无安全候选时选 `observe`／`back` 或返回明确探索终态。
- **同帧约束**：不得把另一次 `ui-tree` dump 当成与当前截图同帧的结构证据；跨调用 UIA 不能证明“这一张图上可点”。
- 页型不确定时允许 `visual_page_type=null`；不要为了填覆盖缺口编造目录外页型。

### 3. 滑动后重新定位（Skill 指导；清障重绑平台强制）

- **旧框立即失效**：每次 `scroll`／`swipe` 后，滑动前的候选框与 `candidate_id` 不得再用于业务点击。
- **必须新观察**：等待新的 Capture 与新候选后，再选下一步；虚拟列表／懒加载：先滚到可见区 → 新观察 → 再点当前帧可见目标。
- **平台分工**：清障关闭控件可由平台在容差内重绑视觉目标；**业务入口／列表卡片不得**沿用滑动前旧框。若平台返回 `POST_SCROLL_REBIND_REQUIRED`，表示业务触控仍绑在滚前 Capture——应改选新帧候选或 `observe`，不要重复硬点旧 id。
- 计划中的 scroll→rebind→click 必须在**同一新 Capture 上**完成后半段点击；跨 epoch 坐标重放禁止。

### 4. 系统弹窗（Skill 指导；危险语义平台强制）

- **成年入口自动确认**：App 探索默认操作身份为已满18岁的成年人。入口年龄门出现「我已满18岁」「我年满18岁-进入」「确认成年并继续」等明确候选时，优先选择当前截图对应的 `candidate_id` 自动确认并继续探索，无需再次询问用户年龄、请求用户操作或转人工，不得仅因年龄确认返回 `blocked`。禁止选择「我未满18岁／离开」；没有当前帧有效候选时不得猜测坐标。确认后必须等待新 Capture 核实门禁消失、重新判定页面，才可继续业务探索。
- **年龄确认范围**：上述约定仅适用于入口成年声明；账号登录／注册表单中的年龄勾选、凭证输入、实名认证、人机验证或支付仍遵守各自边界，不得凭默认成年人身份放行。
- **先清障再业务**：权限、更新、登录门、广告弹层等挡住业务面时，优先低风险关闭／拒绝／稍后／跳过（带结构或已验签关闭候选）；入口年龄门按上述成年入口规则自动确认进入。
- **高风险不推进**：允许、始终允许、安装、授予、支付确认、提交等高风险语义：只记录候选与原因，不推进执行。
- **多层弹窗**：一次只清当前最上层；清完再判业务首页或列表，不得在遮挡态声称探索完成或业务页已覆盖。
- **入口层纪律**：启动广告／权限／登录门 ≠ 业务首页；关不掉也不得把广告底或门后未知底页写成已到达首页。

### 5. 外部 Activity（平台强制 + Skill 指导）

- **分类**：平台按包名粗分类外部前台：`browser`／`store`／`payment`／`launcher`／`settings`／`other`（写入 `external_activity_kind`）。
- **动作前边界**：若不在 `allowed_packages`，平台拒绝业务点击（`APP_PACKAGE_BOUNDARY_VIOLATION`）；外部一律只允许受控恢复（`BACK`，Launcher 用 `WARM_RESTORE`），禁止在外部包继续点业务入口。
- **商店／支付**：与浏览器同等——只恢复、不停留操作；不得把外部页计入业务覆盖。
- **回源验收**：恢复后只信任新 Capture 判断是否回到目标 App；不得假设仍停在外跳前的页型或旧候选仍有效。
- **明确不足终态**：回源连续失败 → `app_external_return_failed`；外跳循环达阈值 → `app_external_restore_loop`；二者不得无限 BACK／重开。

### 6. 返回流程（Skill 指导）

- **适用**：不稳定页、详情／播放页回列表、外跳回源、当前分支耗尽后领取其他 Frontier、遮挡清不掉时的有界退出。
- **怎么回**：使用语义 `back`，不要用猜坐标点左上角返回箭头。
- **怎么验收**：返回成功与否以新 Capture 的包名、页型、遮挡态为准；禁止在未重新观察前复用返回前的覆盖结论或旧候选。
- **与外跳配合**：已在外部包时，返回／恢复优先于任何业务候选；回到目标包后再按 §1–§4 重新排队。

### 7. 长图与视口证据（Skill 指导；角色合同）

- **默认主证据**：当前用户可见视口截图，角色 `viewport_primary`（或稳定业务面的 `stable_business_capture`）。点击、遮挡、弹层判断必须以该视口为准。
- **长页不足一屏**：需要滚动覆盖时，保存有序原帧序列，角色 `long_page_scroll_series`；须带 `parent_capture_id` 与 `frame_capture_ids`；`counts_as_independent_business_arrival=false`。
- **拼接长图**：若生成总览图，角色只能是 `stitched_overview_derived`（派生）；记录来源帧与缺口；**禁止**用拼接图替代原视口做 Judge 主证据或冒充独立业务到达。
- **中途集合变化**：广告刷新、登录态切换、列表数据集变化时分段，不得拼成一张看似同一时刻的长图。
- Explorer **不得**声称“已采集完整长页”除非序列合同完整且缺口显式；缺口不足时记 evidence gap，不得伪 passed。

### 8. 广告外跳取证（Skill 指导；平台一次调度）

- **何时取证**：规则需要广告落地证据、且平台已签发 `capture_ad_landing_evidence`／`ad_outbound` 类候选时；覆盖未进 home 或底栏巡检未完时，平台可能延后签发——不要强行点广告挤掉覆盖。
- **三联证据（`ad_outbound_triplet`）**：
  1. `ad_outbound_source`：点击前／源页视口；
  2. `ad_outbound_landing`：外部落地视口（`external_activity_kind` 必填）；
  3. `ad_outbound_return`：回源后目标 App 新 Capture。
- **覆盖纪律**：落地页 **不得**计入业务页型覆盖；回源后旧候选全部作废，只信新帧。
- **次数**：广告源图／落地取证一生只调度一次；重复点击浪费预算。
- **失败**：缺落地或回源 → gap（`ad_outbound_landing_missing`／`ad_outbound_return_missing`）；回源失败走 §5 终态，不得在外部继续探索。

### 9. 收银台长链（Skill 指导）

- **范围**：目标 App **店内**下单／收银台链路（选品／购物车 → 确认订单 → 店内收银台 → 支付中／结果或取消 → 回业务页）。每步角色 `checkout_chain_step`，须有 `chain_id`、`step_key`、独立 `capture_id`。
- **允许的 step_key**：`cart_or_sku`／`confirm_order`／`in_app_cashier`／`paying_or_processing`／`result_or_cancel`／`return_to_business`。
- **外部支付页**：进入浏览器／微信／支付宝等外部包时，适用 §5——只恢复、不点外部业务；**不把外部页写入 checkout 链步骤**。
- **高风险**：支付确认／提交／开通会员等只记录、不推进执行；缺口码 `checkout_high_risk_confirm_not_advanced`。
- **验收**：链上每步 before／after 可追溯；缺步或出包记 gap，不得用单张收银台截图冒充整链完成。

### 10. 播放器连续拖动（Skill 指导；角色合同）

- **适用**：进度条拖动、seek／scrub、连续滑动改变播放位置等**连续性规则**。
- **证据角色** `player_scrub_series`：至少包含  
  - `before_capture_id`（拖动前）、  
  - 一个或多个 `during_capture_ids`（拖动中／过程见证）、  
  - `after_capture_id`（拖动后）。  
  可选 `control_semantic`（如「进度条拖动」）。
- **禁止**：单帧截图冒充拖动完成；`before==after` 且无 during → 不足。
- **原生交互与连续观察**：进度规则接入语义 `SEEK_MEDIA` 或合同内的唤醒轻触，并用 `timed_capture` 连续帧观察。无障碍 class 只说明画面上有媒体控件，不是播放进度。
- **进度材料缺口**：读不到当前时间、总时长，也没有可比较的连续帧时，必须写入 `playback_progress_material_missing`。不得把 `playback_state_available=false` 写成已取得进度，也不得因此判播放失败。
- **交互**：若平台尚无专用 scrub InteractionPlan，用有界 scroll／swipe 候选 + 逐步新 Capture；每步仍遵守 §3 重绑。不得跨帧复用旧坐标硬拖。
- **到达**：连续拖动序列 `counts_as_independent_business_arrival=false`；业务页到达仍靠稳定视口角色。

### 规程检查表（每轮动作前）

1. 当前是否仍在目标 App？外跳 → 只考虑恢复，不点业务。
2. 是否存在遮挡／系统弹窗／入口阻断层？有 → 先低风险清障，勿点底层业务，勿计业务首页。
3. 本轮候选是否来自**最新** Capture？滑动／返回／恢复之后必须是新帧；出现 `POST_SCROLL_REBIND_REQUIRED` 则禁止沿用旧 id。
4. 所选 `candidate_id` 是否低风险且服务覆盖缺口？高风险 → 跳过。
5. 结构可用则优先结构证明候选；否则仅视觉候选，禁止自造点。
6. 本步若服务长图／外跳／收银台／拖动规则：证据角色与帧集合是否满足 §7–§10？不足则 observe／记 gap，不伪完成。

## 证据与失败

- App Capture 只允许设备真实提供的 Screenshot、UI Tree、Visible Text 和 Action Trace；不得伪造 DOM、OCR 或缺失能力。
- `qc_page_capture` 继续保存 Web/H5 明细，App 只写公共 `qc_capture` 与 `qc_app_capture`。
- 动作成功但 after capture 失败时保存 `evidence_failed`，不得伪装完成。
- 需要 App 不具备证据的规则进入 skipped 或 evidence gap，Judge 不得自行打开 App 补证，也不得写入 passed。
- DEVICE_BUSY、超时和明确 retryable 错误可按锁定预算重试；checksum、包名、Capability 和不支持动作错误不可盲目重试。
- 旧 lease、旧 row version 或旧 finalization epoch 的返回不得覆盖新 Worker 结果。

### 证据角色合同（`app-evidence-role.v1`）

代码合同：`services/api/app/application/app_evidence_role_contract.py`。Explorer／Judge 共用角色码；派生角色不得升格为主证据。

| 角色／包 | 含义 | 可作业务到达？ | 备注 |
|----------|------|----------------|------|
| `viewport_primary` | 当前视口主证据 | 可 | 默认点击／遮挡判断 |
| `stable_business_capture` | 稳定业务面 | 可 | readiness 推荐 |
| `long_page_scroll_series` | 长页原帧序列 | 否 | 须父 Capture＋帧列表 |
| `stitched_overview_derived` | 拼接总览 | 否 | **仅派生**；禁 Judge 主证据 |
| `ad_outbound_triplet` | 广告外跳三联包 | 落地否 | source／landing／return |
| `checkout_chain_step` | 收银台链单步 | 店内可 | 外部支付页排除 |
| `player_scrub_series` | 拖前／中／后 | 否 | 单帧不足 |
| `overlay_occurrence`／`external_landing`／`transition_witness`／`diagnostic_only` | 入口层见证 | 否 | 与 readiness 对齐 |

## 输出

Bootstrap 只输出版本化 `AppReadyHandoff`：任务、Run、Session、`CaptureRef`、Capability Manifest 和检查点版本。后续每轮只输出持久化 Capture/Action/Work Item 引用和明确状态；报告继续使用平台统一任务与规则执行终态。

## 参考（研发）

> 下列文档仅在源码仓库 `docs/`，不随部署产物分发；Agent 运行时不必读取：

- `docs/design/2026-09-07-adb-android-observation-ops-inventory.md` — ADB 观察／操作现况矩阵与已知缺口
- `docs/design/2026-09-03-exploration-optimization/06-interaction-precision.md` — 交互精准度设计目标；letterbox／safe_area 现况以 inventory §3 为准
- `docs/design/2026-09-03-exploration-optimization/07-trusted-evidence.md` — 可信证据与长图原帧／派生分工
- `docs/design/2026-09-08-app-bounded-exploration-milestones.md` — 有限探索里程碑；Skill 不打开生产 `runtime_ready`／Gate 权威
- `services/api/app/application/app_evidence_role_contract.py` — 证据角色合同实现
