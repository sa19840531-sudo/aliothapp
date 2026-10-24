---
name: page-exploration
description: 规划页面探索顺序、候选页面队列和下一步页面动作的阶段 Skill。Main Agent 在任务开始、页面识别后或探索受阻时读取本 Skill，决定 goto、click、scroll、wait、skip、stop 等动作建议，并保持 V1 探索边界可控。
---

# page-exploration

## 职责

`page-exploration` 用于指导 Agent 在质检任务中选择“先看哪些页面、下一步怎么探索、什么时候停止”。

本 Skill 负责探索策略与动作建议，不直接调用浏览器、不执行点击、不做页面类型最终识别、不判定规则结果。

## 何时读取

- 任务开始时，需要从入口地址和规则集生成初始页面队列。
- `page-detection` 输出当前页面类型和状态后，需要决定下一步探索动作。
- 当前页面存在 Tab、列表、详情、广告、支付、设置、播放器等候选区域时。
- 页面跳转失败、加载异常、重复页面过多或风险动作出现时。
- 探索预算即将耗尽，需要判断继续、跳过或停止时。

## 输入

- 任务上下文：`task_id`、入口 URL、业务线、目标平台、设备类型、规则集版本。
- 规则上下文：规则 `url_pattern`、适用页面类型、规则优先级、页面抽样策略。
- 页面状态：`page-detection` 输出的 `page_type`、`page_state`、`candidate_actions`、`next_page_hypotheses`、`risk_flags`。
- 探索状态：`working_memory.exploration_frontier` 中的 `pending_actions` / `explored_actions` / `inactive_actions`，以及已访问页面、待访问页面、失败页面、跳过页面、动作历史、当前深度。
- 运行边界：最大页面数、最大深度、同类页面抽样数量、风险动作策略、重试次数。

## 探索原则

1. 先覆盖规则要求的页面类型，再扩展可见导航中的候选页面。
2. V1 以入口 URL、规则 `url_pattern`、页面内明确导航和可见候选动作为主，不做无边界爬取。
3. 同类页面使用抽样策略控制数量，避免列表、详情、推荐流无限扩张。
4. 外链、登录、支付、下载、提交表单等高风险动作只记录候选，不直接推进。
5. 每次动作后必须回到 `page-detection` 重新识别页面类型和状态。
6. 当前版本不允许 Agent 为普通证据不足自行追加 `observe` / `scroll` / `timed_capture` 补采动作；但 `line_loading`、启动广告倒计时、Flutter 启动壳等生命周期瞬态页，可消费平台 Frontier 已签发的同会话重观察候选。稳定 `video_list` 首屏没有可命中标题、DOM 与 Snapshot 同时证明第二屏存在已登记标题结构且页面仍位于顶部时，也可消费平台签发的“一次向下滚动一屏”候选；滚动后必须重新采集，只能点击新快照中当前可见且中心点命中的标题，不得重复滚动。无法稳定识别、页面覆盖不足或证据不足且没有可信候选时，按 `evidence_insufficient` 收口，不扩大探索范围。

## 处理步骤

1. 根据入口 URL 和规则 `url_pattern` 初始化 `pending_pages`。
2. 读取 `working_memory.exploration_frontier.pending_actions`；`page-detection` 中间件已把当前页面的 `candidate_actions` 与 `next_page_hypotheses` 写入 frontier，并按动作语义去重；`high`/`critical` 风险动作会直接进入 `inactive_actions` 的 `blocked` 终态。
3. 合并 `pending_pages` 与 frontier 中的待探索动作，按规则覆盖率、页面类型缺口、风险级别、历史访问状态排序。
4. 为下一步输出单一动作建议，并说明预期页面类型和选择原因；若该动作来自 frontier，必须保留对应 `exploration_action_id`，供后续 `page_capture_tool` 绑定本次探索动作。
5. 不要把 `page-detection` 的候选动作直接当作最终动作；候选动作必须先进入 frontier，再由本 Skill 排序、选择、跳过或停止。
6. 若最新识别为 `line_loading`、`loading`、`transitioning` 或 `needs_more_observation` 且 `rule_matching_allowed=false`，不得点击加载页关闭入口，也不得重新 `goto`。`line_loading` 必须优先消费平台签发的 `same_session_reobserve` 候选，在同一浏览器会话等待线路完成后重新采集；重观察预算耗尽仍无法离开瞬态页时，才按证据不足或人工复核收口。进入 `startup_ad` 后先完成该页规则，再执行可审计的跳过/关闭或后续低风险页面发现动作。首页存在多层弹窗时，必须在前置生命周期阶段结束并进入业务首页后再判断；首层完成一次弹窗相关规则后执行可信关闭动作；运行时会在同一次页面采集中有界清理后续弹窗并保存逐层动作证据。只有安全结构无法继续解析时才按证据不足收口，后续层不得重复匹配同一底层业务规则。页面稳定且无遮挡后，必须消费 Frontier 中由可见底部导航等结构生成的低风险候选，不能以“已到首页”为由结束探索。进入稳定业务列表并完成该列表规则后，优先执行当前列表真实可见首卡的受控详情入口，再完成详情/播放页规则并通过浏览器历史返回；返回产生的新列表采集只用于恢复位置和补充 Frontier，同一业务列表不重复质检、同一首卡不重复点击，随后继续其他未访问底部导航分支。
7. 若触发边界限制，先确定 `stop_reason`，再调用 `exploration_closure_tool` 将不再执行的动作写为 `blocked`/`skipped`/`failed`，并将未开始候选规则匹配且不再处理的页面写为 `abandoned`；禁止只输出文字 `skip` 或 `stop`。已经开始候选规则匹配但未写完规则结果的页面不能关闭，必须先完成候选规则写回。
8. 页面操作完成后，等待 `page_capture_tool` 返回新的 `page_capture_id`；若本次操作绑定了 frontier 动作，调用时必须传入 `exploration_action_id`，使运行内存把该动作标记为 `explored`。Frontier 同时提供 `navigation_depth` 时必须原样传入；点击动作只能在当前浏览器会话执行，不能回填入口页或其他旧 URL。
9. 新页面必须回到 `page-detection` 重新识别页面类型和状态，再更新访问状态和队列。
10. 对失败页面记录失败原因，必要时允许有限重试，超过阈值后使用 `exploration_closure_tool` 写入明确终态。

## 动态页面转移参考

页面总数不能在任务开始时确定。只按实际证据逐步扩展 Frontier，优先遵循以下低风险
主链路，并允许任一阶段动态增加、失败或缺席：

```text
line_loading/loading --同会话重观察--> startup_ad 或 home
startup_ad --倒计时重观察、跳过或关闭--> home
home --首页弹窗链逐层质检/有界清障--> stable home --底部 Tab--> video_list/post_list/profile/activity/payment
video_list/post_list --首个低风险内容卡片--> video_detail/content_detail
业务列表或详情 --返回/切换底部 Tab--> 其他未访问分支
```

- “进入方式”用于创建当前时刻可执行的 Frontier 动作；“离开方式”用于验证动作是否形成新采集。
- 只消费当前 DOM 中真实可见、低风险的入口，不因通用关键词臆造页面或固定数量。
- Cloudflare、验证码、访问拒绝、空白页等作为当前分支阻塞事实隔离，不得让其终止其他待探索分支。
- 购买、支付确认、提交、下载和登录属于高风险边界，只记录候选与原因，不实际触发。

## 输出格式（示意）

`expected_page_type` 必须使用页面类型目录中的 canonical code，不能使用旧分类、历史别名或中文名称。

```json
{
  "next_action": "click",
  "exploration_action_id": "exploration-1",
  "target": "商品详情入口",
  "expected_page_type": "content_detail",
  "risk_level": "low",
  "reason": "当前规则集包含详情页规则，列表页中存在可见详情入口，且该类页面尚未覆盖",
  "pending_pages_delta": [
    {
      "page_url": "https://example.com/detail/123",
      "source": "visible_link",
      "expected_page_type": "content_detail",
      "priority": 80
    }
  ],
  "stop_reason": null
}
```

## 动作建议

- `goto`：打开入口 URL 或规则命中的明确 URL。
- `click`：点击低风险导航、Tab、详情入口、分页、展开按钮；预期进入新 URL 时必须同步声明 `navigation_depth`，页内 Tab、展开、关闭弹窗等同页动作不得声明。
- `scroll`：仅消费平台签发的低风险页面发现或用户可见列表导航候选；允许稳定视频列表从页顶一次向下滚动一屏寻找已登记标题，滚动后重新采集并执行可见性复核。当前版本不得由 Agent 自造滚动，也不得把滚动用于通用补采或覆盖补齐。
- `wait`：等待加载、跳转、播放器状态或弹窗稳定；当前版本不得为补采主动追加同会话 `observe` 或 `timed_capture`。
- `skip`：当前页面或动作触发边界限制，记录原因后跳过。
- `stop`：规则所需页面已覆盖，或探索预算/风险边界已触发。

`skip`/`stop` 是决策输出，不是运行状态写入；相应 Frontier 项必须通过 `exploration_closure_tool` 收口后才算完成。

## V1 边界

- V1 不做站点级深度爬虫，只做任务内受控探索。
- 导航边界与用户覆盖策略由 `navigation-boundary-skill` 统一定义，本 Skill 只输出探索动作建议。
- 对登录、支付、下载、外链、提交等动作，输出高风险候选和跳过原因，不直接推进。
- 对同类列表/详情页，使用规则或任务配置中的 `page_sampling_policy` 控制抽样。
- 连续识别失败或导航失败时，按任务重试阈值停止当前分支。

## 与其他 Skill/Tool 的关系

- 上游：读取 `task-planning` 的任务阶段安排和规则集范围。
- 下游：输出动作建议给页面操作 Tool；若动作来自 frontier，页面操作 Tool 必须传入 `exploration_action_id`；页面操作后必须读取 `page-detection`。不再执行的动作或未检查页面通过 `exploration_closure_tool` 写入终态。
- 与 `rule-matching`：探索优先级应服务于规则覆盖，但不直接判断规则是否命中。
- 与 `evidence-report`：探索过程中产生的页面基线证据可被后续规则结果复用。

## 参考

> 以下文档仅存在于源码仓库 `docs/design/` 目录，不随部署产物分发；仅供研发人员查阅历史设计背景，Agent 运行时不需要也不应尝试读取：

- `docs/design/2026-07-03-alioth-integrated-solution-design.md`
- `docs/design/2026-07-01-alioth-qc-workflow-detail.md`
