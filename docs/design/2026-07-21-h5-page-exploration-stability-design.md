# H5 页面探索稳定化设计

## 背景

本设计参考 `h5_page_dom_batch2_report.md` 中第二批 H5 站点 DOM 采集结果。附件按手机视口采集，禁用了图片、视频、字体等媒体资源，只保留页面结构、入口、弹窗/遮罩候选、关闭候选和 z-index。该样本适合抽象“动态页面探索”中的结构共性，而不适合作为固定页面清单。

当前 Alioth 已具备页面采集、页面识别、动态 Frontier、页面质检记忆、规则门禁和报告收口能力。本方案不新增一套爬虫系统，而是在既有链路上补充 H5 结构化探索剖面，让页面探索更稳定、更可审计、更少依赖模型临场判断。

## 目标

1. 将 H5 站点高频 DOM 共性沉淀为确定性信号和候选动作。
2. 保持现有 `page_capture_id`、`exploration_action_id`、`qc_page_capture`、`qc_rule_execution` 审计链路不变。
3. 让页面探索按真实 DOM 和运行时 Frontier 收敛，不依赖运行前预知页面总数。
4. 降低多层弹窗、线路加载、开屏广告、底部导航、列表详情返回等场景的误判和漏探。
5. 对高风险入口和高风险动作只记录、阻断或转人工复核，不实际触发。

## 非目标

1. 不做站点级无限爬取，不把 Alioth 变成通用爬虫。
2. 不新增目录外页面类型；仍使用现有 14 个 canonical page type。
3. 不让模型决定导航边界、风险等级、规则匹配门禁或完成状态。
4. 不将完整 DOM、截图、base64 或对象存储路径暴露给 Main Agent。
5. 不绕过现有 `page_sampling_policy`、运行时护栏和 Tool 权限模型。

## 附件共性抽象

第二批样本呈现出明显的 H5 同构特征：

- 入口通常经历 `line_loading -> startup_ad -> home`，其中线路页和开屏广告常共用 `loading-page` 外壳。
- 弹窗和遮罩集中在 `van-dialog`、`van-popup`、`loading-page`、`start-up` 等结构，z-index 常见 `2000`、`3000`、`99999`。
- 稳定业务页面通常使用 `van-tabs` 或 `van-tabbar` 承载推荐、视频、社区、我的、会员、漫画、游戏等入口。
- 视频/社区/图文列表可通过真实可见内容容器进入首个低风险详情页，详情页完成后用浏览器历史返回恢复分支。
- 购买、充值、支付、下载、安装、登录、提交等入口频繁出现，但应作为高风险动作阻断，不作为探索推进动作。
- 部分入口在站点名称或入口特征上已具备安全风险，应在打开前记录跳过终态。

## 推荐架构

### 1. H5 探索剖面

新增一个 H5 探索剖面，作为页面识别和 Frontier 生成的确定性补充。建议落点：

- `services/api/app/domain/page/page_type_catalog.json`：补充别名、DOM hint、layout hint 和 next page candidate，不新增 page type。
- `services/api/app/application/page_exploration_profile.py`：提供纯函数式 DOM 信号提取，不依赖浏览器、数据库或大模型。
- `services/api/app/agents/tools/page_detection_tools.py`：在现有模型识别结果后调用剖面结果做受控修正和候选动作补强。

剖面输出建议使用稳定结构：

```json
{
  "profile": "h5_common",
  "shell": "line_loading | startup_ad | stable | unknown",
  "obstruction": {
    "present": true,
    "selector": "div.van-dialog:nth-of-type(2)",
    "dismiss_selector": "text=关闭",
    "z_index": 3000,
    "fingerprint": "sha256:..."
  },
  "tabbar_items": [
    {
      "label": "视频",
      "selector": "div.van-tabbar-item:nth-of-type(2)",
      "active": false,
      "expected_page_type": "video_list"
    }
  ],
  "content_card": {
    "selector": "div.page-content .item:nth-of-type(1)",
    "expected_page_type": "content_detail"
  },
  "risk_entries": [
    {
      "label": "充值",
      "selector": "text=充值",
      "risk_level": "high",
      "reason_code": "PAYMENT_OR_RECHARGE_ENTRY"
    }
  ]
}
```

### 2. 入口安全预筛

在任务进入浏览器前增加轻量预筛。预筛只使用用户提交的入口名称、URL、备注和规则集上下文，不打开页面、不读取页面内容。

预筛结论：

- `allow`：进入正常 `page_capture_tool(mode="goto")`。
- `skip_safety`：不打开页面，写入入口动作的 `blocked` 或页面覆盖为空的审计终态。
- `manual_review`：风险无法判断时不自动打开，由人工确认或配置允许后重跑。

预筛应沉淀为应用层服务，不写在 Prompt 中。高风险词库可复用 `domain/rule/forbidden_content.py` 的敏感词能力，但入口预筛需要单独的 reason code，避免和页面内容规则混淆。

### 3. 页面识别补强

`page_detection_tool` 仍是页面类型唯一结论来源，但其内部可以先让模型输出结构化结果，再由剖面做确定性修正：

- `loading-page` 同时具备倒计时、跳过、关闭、进入结构时，优先归为 `startup_ad`。
- 仅有加载、获取线路、正在获取最佳线路且无可质检广告结构时，归为 `line_loading`，`rule_matching_allowed=false`。
- 稳定业务页面存在可见遮挡时，`page_state` 修正为 `popup`，只保留当前最上层一个关闭动作。
- DOM 能确定 `van-tabbar` 非激活项时，移除模型生成的同义 Tab 动作，改用确定性 selector。
- 列表页存在真实可见内容卡片时，补充一个低风险首卡详情动作，并避开广告、游戏、下载、充值容器。
- 详情页补充 `browser_history_back` 候选动作，帮助探索回到来源列表。

### 4. Frontier 优先级

动态探索仍由 `PageInspectionState` 和 `work_dispatcher` 管理。H5 场景下建议使用以下优先级：

1. 当前页面未识别：强制 `page_detection_tool(page_capture_id)`。
2. `line_loading`：强制同会话 `observe` 补采，携带 `supersedes_page_capture_id`。
3. `startup_ad`：先完成开屏广告页面候选规则和写回，再执行关闭/跳过/进入。
4. 业务页遮挡：每次只关闭当前最上层遮挡，并重新采集识别。
5. 稳定业务页：先做当前页规则匹配和写回。
6. 底部 Tab 和页内 Tab：按规则覆盖缺口与抽样策略选择分支。
7. 列表页首卡详情：同类列表完成后进入一个低风险详情样本。
8. 详情页返回：完成详情规则后用浏览器历史返回，恢复后不重复质检同一列表。
9. 剩余动作收口：预算、风险或抽样限制触发时使用 `exploration_closure_tool` 写入终态。

## 页面生命周期

H5 探索主链路如下：

```text
入口预筛
  -> goto 入口
  -> line_loading observe 补采
  -> startup_ad 页面质检
  -> 关闭/跳过/进入开屏广告
  -> home
  -> 逐层关闭弹窗
  -> stable home 页面质检
  -> 底部 Tab / 页内 Tab 分支
  -> video_list / post_list / content_list / profile / activity / payment
  -> 列表首卡详情
  -> video_detail / content_detail
  -> 浏览器历史返回
  -> 继续剩余低风险分支
  -> Frontier 清空或显式收口
```

该链路是优先级参考，不是固定页面清单。任一阶段缺席、失败、重复或被阻塞时，只收口当前页面或当前动作，不终止其他可执行 Frontier。若失败发生在中间页面或动态分支上，必须进入“中间页失败与重试恢复链路”，不能把重试误当成从入口重新探索。

## 中间页失败与重试恢复链路

中间页面挂起、浏览器上下文销毁、网络中断或动作执行后页面不可用时，恢复目标是“回到原本要继续处理的页面或动作”，而不是从头探索一遍入口站点。恢复链路分三层：

1. **同会话恢复优先**：如果浏览器会话仍可用，优先执行 `observe`、`timed_capture`、受控 `reload` 或 `browser_history_back`。这些动作保留当前页面栈，避免重新经历入口线路加载、开屏广告和首页弹窗。
2. **重新打开进入 retry replay**：只有会话已损坏、页面上下文不可恢复或同会话恢复连续失败时，才允许重新 `goto`。本次 `goto` 必须标记为重试恢复动作，并绑定原始页面或原始 Frontier。
3. **恢复路径压缩**：重新打开后再次出现的 `line_loading`、`startup_ad`、首页弹窗或中间遮挡，属于恢复路径上的中间态。它们必须产生 `page_capture_id` 并留审计，但默认不进入正常 Frontier，也不重复执行已经完成的页面规则。

重试恢复动作建议携带以下结构：

```json
{
  "retry_of_page_capture_id": "capture-before-failure",
  "retry_of_exploration_action_id": "action-before-failure",
  "retry_attempt": 2,
  "retry_mode": "same_session_recover | retry_replay",
  "expected_resume_page_type": "video_list",
  "resume_target_fingerprint": "sha256:...",
  "resume_reason_code": "PAGE_CONTEXT_DESTROYED"
}
```

恢复路径页面按指纹处理：

- 指纹与已见线路页、开屏广告或弹窗一致：标记为 `retry_duplicate` 或 `superseded`，只保留审计，不重复质检和写规则结果。
- 指纹不同且页面本身可质检：作为恢复路径上的新中间页面质检一次，但不得扩展其普通业务 Frontier，完成后继续向 `expected_resume_page_type` 收敛。
- 连续多次无法回到 `expected_resume_page_type`：当前恢复动作进入 `failed`，相关页面进入 `failed/degraded` 或人工复核，其他 Frontier 继续处理。

retry replay 模式下的完成条件不是“重新打开链路走完”，而是原始目标动作或目标页面进入终态。恢复链路上产生的中间页面只用于审计、去重、必要的过渡页质检和失败解释，不能污染正常页面覆盖统计。

## 动作风险模型

低风险且可执行：

- 关闭、跳过、进入、我知道了等弹窗/开屏广告消除动作。
- 底部 Tab、页内 Tab、同源导航。
- 列表首个真实内容卡片。
- 浏览器历史返回。
- 只读 observe、timed_capture、scroll，以及同会话受控 reload。

记录但不执行：

- 登录、注册、绑定手机号、输入凭证。
- 购买、充值、支付、下单、绑卡。
- 下载、安装、上传。
- 评论、发帖、关注、点赞等会改变用户数据的动作。
- 跨域外链跳转，除非规则明确需要外链探测且有专用受控 Tool。

所有高风险动作应进入 `inactive_actions`，状态为 `blocked`，并保留 `reason_code`、目标 selector、来源页面和预期风险。

## 数据与审计

本方案沿用当前事实源：

- `page_capture_id` 是页面采集事实身份。
- `exploration_action_id` 是页面探索动作身份。
- `qc_page_capture` 保存页面采集、生命周期和失败补偿。
- `qc_page_exploration_action` 保存 Frontier 动作状态。
- `qc_rule_execution` 保存页面绑定规则结论。
- `qc_agent_run.working_memory` 和实时 checkpoint 保存运行快照。

建议补充的审计字段优先放入现有 JSON 结构，不急于新增迁移：

- `h5_profile.profile`
- `h5_profile.shell`
- `h5_profile.obstruction.fingerprint`
- `action_family`
- `risk_reason_code`
- `deterministic_signal_source`
- `retry_of_page_capture_id`
- `retry_of_exploration_action_id`
- `retry_mode`
- `expected_resume_page_type`
- `resume_target_fingerprint`
- `retry_path_page_capture_ids`

如果后续查询报表需要按这些字段聚合，再评估迁移为结构化列。

## 完成标准

一次 Run 只有满足以下条件才可完成：

1. 当前 Run 已发现页面全部进入 `inspected`、`superseded`、`abandoned`、`failed` 或 `degraded`。
2. `exploration_frontier` 不存在 `candidate` 或 `selected` 动作。
3. 已开始候选规则匹配的页面，候选规则全部写回或降级收口。
4. 被预算、风险或抽样策略跳过的动作均已通过 `exploration_closure_tool` 写入终态。
5. retry replay 恢复链路已回到原始目标并完成，或已对原始目标动作写入明确失败终态。
6. 模型自然语言输出不能作为完成凭据。

局部页面失败不等于全局失败。局部失败应转为页面 `failed/degraded` 和人工复核项，然后继续处理其他 Frontier。

## 错误处理

- `line_loading` 连续 observe 超过阈值：当前页面失败，保留入口和补采链路，任务进入人工复核或继续其他分支。
- 开屏广告关闭后 DOM 指纹不变：累计遮挡恢复失败次数，超过阈值后当前动作失败，不在旧 DOM 连续点击。
- 关闭遮挡后仍有新遮挡：生成新 `page_capture_id`，重新识别并只处理当前最上层。
- 页面识别 `ambiguous/insufficient`：按缺失证据补采，旧采集标记 `superseded`。
- 页面识别 `error`：阻断规则匹配，写页面失败或人工复核，不调用 `candidate_rules_tool`。
- Cloudflare、验证码、访问拒绝、空白页：作为当前分支阻塞事实隔离，不新增页面类型。
- 浏览器返回失败：关闭该返回动作，来源列表不重复质检，继续其他低风险 Frontier。
- 中间页挂起但会话仍可用：优先同会话 observe、timed_capture、reload 或 history back，不重新 goto 入口。
- 中间页恢复必须重新 goto：进入 retry replay，绑定原始 page capture 与 exploration action；恢复路径上的重复线路页、开屏和弹窗只留审计，不污染正常 Frontier。
- retry replay 发现新的开屏广告或遮挡指纹：按恢复路径中间页质检一次，完成后继续向目标页面收敛。
- retry replay 超过阈值仍无法到达目标页面：原始动作失败，目标页面转人工复核，其他 Frontier 不受影响。

## 测试策略

### 单元测试

- `page_exploration_profile`：覆盖 `loading-page`、`start-up`、`van-dialog`、`van-popup`、`van-tabbar`、`van-tabs`、首卡详情、风险入口提取。
- `page_detection_tools`：覆盖模型漏报时的确定性动作补强，模型误报时的受控过滤。
- `PageInspectionState`：覆盖 `action_family` 去重、遮挡指纹不变重试、retry replay 绑定原始动作、详情返回后列表不重复质检。

### 集成测试

- `line_loading -> startup_ad -> home` 全链路。
- 开屏广告先质检再关闭。
- 首页多层弹窗逐层关闭，每层都产生新采集。
- 稳定首页底部 Tab 分支覆盖。
- 列表首卡详情和浏览器返回。
- 中间页失败后同会话恢复，确认不重新经历入口链路。
- 中间页失败后 retry replay，确认重复线路页、开屏和弹窗只留审计，不重复已完成规则。
- retry replay 发现新开屏广告指纹，确认只作为恢复路径中间页质检一次。
- 高风险入口进入 blocked，不产生浏览器触发。
- 安全预筛跳过入口，不打开页面但有可审计终态。

### Fixture 来源

从附件抽取脱敏 DOM 片段作为测试 fixture，保留结构、class、z-index、可见文本类别和 selector，移除站点名称、成人内容标题、真实 IP、真实域名和敏感文案。

## 实施步骤

1. 新增脱敏 H5 DOM fixture 和剖面单元测试。
2. 实现 `page_exploration_profile.py` 的纯函数信号提取。
3. 补强 `page_type_catalog.json` 的 H5 别名、DOM hint 和 next candidates。
4. 将剖面接入 `page_detection_tools.py` 的后处理阶段。
5. 增强 `PageInspectionState` 的动作族去重和遮挡指纹失败计数。
6. 增加 retry replay 状态字段、恢复路径去重和原始目标终态收口逻辑。
7. 补齐真实 Agent 集成测试，验证 Frontier 收敛、重试恢复和报告完成口径。
8. 更新 `docs/专项能力与高频回归规则.md`，记录 H5 探索回归规则。

## 迁移影响

数据库迁移：首版不需要。  
前端菜单/路由：不需要。  
规则集数据：不需要修改规则定义，但可补充页面类型 Catalog。  
运行时配置：可复用现有 `page_sampling_policy`、最大步数、最大页面数和超时配置。

## 风险与缓解

- 风险：剖面规则过强，误过滤模型发现的真实候选动作。  
  缓解：只过滤与确定性 DOM 冲突的动作，并把过滤原因写入识别结果。

- 风险：新增 H5 逻辑继续扩大 `page_detection_tools.py` 单文件负担。  
  缓解：剖面提取放入独立 application 模块，Tool 只做调用与结果合并。

- 风险：附件站点具有敏感内容，测试数据不宜原样入库。  
  缓解：只保留脱敏 DOM 结构，不保留真实内容、真实域名和可访问地址。

- 风险：安全预筛与页面内容质检规则混淆。  
  缓解：入口预筛只决定是否打开页面，不产出内容规则结论；规则结论仍必须绑定真实页面采集。

## 验收标准

1. 脱敏 fixture 下 H5 主链路可稳定生成同一批 Frontier 动作。
2. 多层遮挡场景不会在旧 DOM 上连续点击多个关闭动作。
3. 开屏广告不会被提前关闭而丢失页面绑定规则结果。
4. 底部 Tab 分支和列表首卡详情至少覆盖一个代表性样本。
5. 高风险动作只进入 blocked 审计，不触发浏览器操作。
6. 中间页失败恢复不会把重复线路页、开屏和弹窗当成新的正常探索分支。
7. retry replay 无法回到目标页时，原始动作有明确失败终态且其他 Frontier 可继续。
8. Run 完成状态只由页面记忆和 Frontier 终态决定。
9. 相关单元测试和真实 Agent 集成测试通过。
