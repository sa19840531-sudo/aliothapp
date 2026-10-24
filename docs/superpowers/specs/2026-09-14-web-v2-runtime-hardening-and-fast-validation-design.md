# Web V2 运行时收口、通用详情识别与快速验收设计

> 日期：2026-09-14
>
> 状态：待实施
>
> 适用范围：仅 Web V2；App/H5 不做任何生产代码改动
>
> 前置设计：`docs/superpowers/specs/2026-09-14-web-v2-dynamic-exploration-and-evidence-optimization-design.md`

## 1. 目标

本设计收口 Web V2 当前仍未满足的运行时合同：所有新建、重跑和重新探索都创建当前版本的新 Run，并强制从当前可信 DOM 动态发现真实 `href`；广告只能使用 `href` 在隔离 Playwright 上下文中打开，底层禁止广告点击；探索的每个阶段都有持久化游标、重试和 gap；旧站点菜单 Registry 与 `direct_ad_page_enabled` 不再影响任何探索执行路径；详情页使用与域名、路径和 CMS 无关的主体区域事实进行分类。

完成后，`chigua.com`、`heiliao.com`、`hl365.com` 仅作为统一状态机的真实回归样本，不允许出现任何站点专属流程、选择器或分类规则。

## 2. 当前问题结论

### 2.1 当前实现仍暴露旧策略开关

这是现状审计，不是目标设计。当前创建边界仍接收 `applicability_mode`、`discovery_policy.enabled`、`max_unlisted_menus` 等旧执行字段，使调用方仍可能关闭或误以为能够关闭动态发现。目标状态必须删除这些字段对新建、重跑和重新探索的选择作用，而不是继续保留一条旧执行路径。

### 2.2 广告底层仍接受点击模式

广告已有独立 BrowserContext 和 Page，但页面采集底层仍允许 `CLICK + selector` 触发广告证据。这形成绕开“只使用 href”的第二条路径。

### 2.3 缺少可恢复的 Web 阶段游标

现有菜单 ledger、预算 ledger 和 `qc_surface_phase_run` 只能描述局部结果，不能在 Worker 中断后回答“当前 URL 正在执行哪个阶段、已重试几次、下一步从哪里恢复”。首页 GOTO/TLS、弹窗和长图失败也没有统一的继续策略。

### 2.4 旧开关和 Registry 仍影响新主路径

`direct_ad_page_enabled` 仍从策略/Registry 进入运行时组合，旧 `WebSiteMenuRegistry` 仍能参与主路径物化。历史兼容和新执行语义没有物理隔离。

### 2.5 详情分类存在全站点系统性误判风险

问题不只存在于 `heiliao.com`。当主播放器位于 `article/main` 内、播放器由 DPlayer/Video.js/iframe 或延迟加载容器承载、详情页具有大量相关推荐、正文包含内嵌媒体时，现有事实提取会过滤真实主体或把推荐卡片计入主列表，最终把明确详情页压成 `unknown` 或列表页。

### 2.6 三站最近探索暴露的通用缺口

- `chigua.com` 已保存首页、DOM 和长图，但 148 条菜单账本中有 146 条未复核动态候选，并出现大量 GitLab URL、空 URL 和未知结果。一次站内 href 跨域重定向后，来源页恢复证据未被可靠接续，最终以 `WEB_NAVIGATION_CURRENT_UNTRUSTED` 失败。这说明动态发现还需要来源 Capture 信任门禁、跨域落地隔离和 frontier 防污染。
- `heiliao.com` 已形成 35 个逻辑页面和 36 张长图，18 个列表页稳定为 `video_list`，16 个详情页却被压成 `unknown`。这说明截图链已经可用，主要缺口位于通用主体区域事实，而不是站点 URL 或证据数量。
- `hl365.com` 在最近有效探索中呈现重复内容卡片、年龄门、多个轮换阻断和大量广告元素；更早详情证据也出现模型已观察为详情但结构事实未确认的问题。2026-09-14 暂停前未重新提交该站，因此它仍是待当前分支复验的样本，不能写成已通过。

这三个站点分别覆盖“跨域恢复与候选污染”“列表到视频详情”“混合内容、弹窗与文章详情”等压力场景，但仍不足以单独证明所有 Web 通用性。最终验收必须增加结构变形测试、受控双 origin 浏览器测试和从未进入 Registry 的冷启动域名。

## 3. 不可变执行合同

### 3.1 新 Run 固定动态发现

- 新建任务、历史任务重跑和重新探索都必须创建当前版本的新 Run；不存在“按历史执行策略继续跑”的入口。
- 新 Run 的有效策略固定为 `runtime_dynamic`、`enabled=true`。旧 `max_unlisted_menus` 不再决定是否发现；探索规模改由服务端冻结的通用 `WebFrontierPolicy` 控制。
- 菜单正式采集上限由部署配置 `ALIOTH_WEB_MENU_COLLECTION_LIMIT` 提供，默认值为 `10`，合法范围为 `1..1000`。API 在创建新 Run 时读取并冻结为 `budget.menu_limit`；修改配置并重启 API 后只影响后续新 Run，运行中和历史 Run 保持冻结值。
- 新 Run 的公开请求合同移除整个 `applicability_mode` 和 `discovery_policy` 输入，并且不接受 `direct_ad_page_enabled` 或 Registry 版本作为执行覆盖；请求携带任一旧字段时返回 `422 WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN`，禁止静默覆盖后让调用方误判其已生效。
- `required_page_types` 可继续表达用户希望优先覆盖的标准页型，但只能影响优先级和覆盖目标，不能关闭 href 发现、裁掉动态 Catalog 或切换运行时。
- 历史任务重跑沿用现有工作台语义创建新的 Task ID 和 Run ID，并以 `task_config.source_task_id`、`source_operation=rerun` 记录来源关系；新 Run 使用当前 Catalog 重新解析保留的标准 `required_page_types`，重新冻结当前动态策略和菜单额度，不复制源任务的旧 `applicability_mode`、Catalog 身份或 discovery 配置。
- 只有历史报告、审计和证据查看可以只读解析旧 discovery/menu 字段；该解析能力不得产生导航命令、浏览器动作或可调度 checkpoint。
- 导航候选必须来源于当前可信 Capture 的真实 HTTP(S) `href`。
- 普通业务导航仅允许同源；危险协议、下载、上传、支付、登录和表单提交继续拒绝。

### 3.2 广告只允许 href 隔离打开

- 广告候选必须具有可解析的 HTTP(S) `href`。
- 广告证据命令固定为 `DIRECT_AD_OUTBOUND`，禁止 `CLICK`。
- 页面采集底层遇到 `capture_ad_landing_evidence=true` 且动作不是 `DIRECT_AD_OUTBOUND` 时立即返回 `WEB_AD_CLICK_FORBIDDEN`，不得调用 Playwright click。
- 每个广告创建独立 BrowserContext 和新 Page；广告页不得复用业务 Page。
- 每个 `home/content_list/content_detail/video_list/video_detail` 分类完成后、下一业务导航前，至多取一个广告 href；成功打开后保存请求 URL、最终 URL、DOM、首屏视口截图和来源谱系，并直接签发 `ad_page`。广告不滚动、不截长图，证据完成后关闭独立 Context，再继续原业务会话。
- 广告页不进入普通分类、站内 frontier、业务覆盖率或后续点击。

### 3.3 新旧策略物理隔离

- 新主路径使用 `RuntimeWebNavigationContext`，其中只包含目标 origin、URL 安全策略和动态发现预算。
- `direct_ad_page_enabled` 从新 Run 的 domain、composition、scheduler 和 browser adapter 接口中删除。
- `WebSiteMenuRegistry` 仅允许由 `LegacyWebRunAuditAdapter` 读取。
- `LegacyWebRunAuditAdapter` 是只读审计适配器，必须返回 `execution_allowed=false`，不得接入 scheduler、browser adapter 或 Worker claim 查询。
- 历史报告和冻结策略可继续展示旧字段，但任何“重跑/重新探索”操作都创建新的 Task 和当前版本 Run，只保留 `source_task_id`、`source_operation` 追溯关系。
- 部署切换时仍处于非终态的旧执行 Run 不续跑旧状态机，也不原地迁移游标；将其收口为 `WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED`，随后由重跑入口创建当前版本的新 Run，避免混用两套合同。
- 历史证据中的旧广告 click 只允许展示，不允许重放；新 Run 若再次发现该广告，仍只能执行 href 隔离打开。

### 3.4 Canonical Site Scope 与可信 Capture 门禁

- 初次根导航允许一次受控 canonical 绑定：仅接受 HTTP→HTTPS、同主机或 apex↔`www` 的可验证跳转，成功后冻结 `canonical_site_origin`。
- canonical 绑定完成后，普通导航必须严格同源；跨到无关 registrable domain 时不得重新绑定。
- 只有同时满足“当前 Session、当前 finalization epoch、当前 Capture、稳定 ready、`final_origin=canonical_site_origin`”的 Capture 才能进入分类和 href 发现。
- 跨域落地页只保存请求 URL、最终 URL、重定向链和安全错误，不读取其 DOM 生成 frontier。
- 恢复来源页时必须生成并重新验签新的可信 Capture；恢复失败只终结当前候选，不得污染其他候选。
- 空 href、fragment-only、危险协议和无法规范化的链接只增加聚合计数，不创建正式 frontier 行。

## 4. 持久化阶段状态机

### 4.1 阶段定义

每个逻辑页面按以下阶段推进：

```text
bootstrap_navigation
  → obstruction_clear
  → readiness
  → primary_capture
  → full_page_capture
  → classification
  → href_discovery
  → candidate_navigation
  → frontier_drain
  → evidence_reconciliation
  → coverage_snapshot
  → report_finalization
  → session_close
```

广告候选从 `href_discovery` 分流到：

```text
ad_isolated_navigation
  → ad_capture
  → ad_closeout
```

### 4.2 游标模型

新增 Web 专用 checkpoint，键为 `run_id + logical_page_key`，至少保存：

- `current_stage`
- `stage_status`
- `cursor_payload`
- `attempt_count`
- `next_attempt_at`
- `last_error_code`
- `last_error_detail`
- `gap_codes`
- `row_version`
- `updated_at`

`cursor_payload` 只保存可恢复引用：当前 URL、父 Capture、候选 href 序号、菜单 ledger ID、Session/finalization epoch 和幂等键。不得保存原始 DOM 或截图二进制。

### 4.3 幂等性

阶段幂等键固定为：

```text
run_id + logical_page_key + stage + stage_generation
```

恢复时先查询现有 Capture、Artifact、Surface Phase 和候选 ledger；已完成的副作用直接复用，不得再次截图、重复插入或重复打开广告页。

### 4.4 重试矩阵

| 失败类型 | 最大尝试 | 策略 | 耗尽后的行为 |
|---|---:|---|---|
| GOTO/TLS/连接重置 | 3 | 指数退避并保留最终 URL | 记录 `WEB_NAVIGATION_RETRY_EXHAUSTED`，继续下一候选 |
| 首页根导航失败 | 3 | 指数退避并保留最终 URL | 记录根导航 gap；无可用 frontier 时正常收口为失败终态 |
| 弹窗关闭/状态漂移 | 每个弹窗指纹 2 | 重新捕获并按指纹重试 | 记录 `WEB_OBSTRUCTION_UNRESOLVED`，继续证据允许的只读流程 |
| 主截图失败 | 2 | 仅瞬态错误重试 | 记录 `WEB_PRIMARY_CAPTURE_FAILED`，该 URL 不分类，继续下一候选 |
| 长图超时/持久化瞬态失败 | 2 | 不刷新业务页面，仅重试补图阶段 | 记录明确长图 gap，继续分类和发现 |
| 长图状态漂移 | 1 | 不重试 | 记录 `WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT`，继续分类 |
| 分类证据冲突 | 1 | 不做网络重试 | 保存事实与冲突，签发 `unknown` 并继续 |
| 广告打开失败 | 2 | 仅重新 `goto(href)` | 记录 `WEB_AD_PAGE_OPEN_FAILED`，不创建虚假 `ad_page` |

### 4.5 通用 Frontier 调度

动态发现不可关闭，但不能等同于无限访问。新 Run 冻结服务端 `WebFrontierPolicy`，至少包含最大页面数、最大深度、最大运行时长、同一结构簇样本数、单一路径失败率和每 origin 并发度。客户端不得把预算改成无限，也不得把预算改成 0 来关闭发现。

`menu_limit` 与原始 href 数量不同：它只统计实际领取 execution slot、执行 `goto` 并采集页面证据的唯一同源菜单。首页、详情页、广告、跨域阻断、空 href、危险链接、规范 URL 别名和重复菜单均不占菜单额度。达到 10 个菜单后，系统仍可完成当前页面的 href 安全分析和详情抽样，但不得再执行新的菜单 `goto`；超额菜单记录聚合 `menu_limit_skipped_count` 和有界摘要，不为每条溢出链接无限扩张账本。

候选进入 frontier 前依次执行：

1. 解析并规范化绝对 HTTP(S) URL；
2. 删除 fragment 和通用追踪参数；
3. 合并 HTTP→HTTPS、尾斜杠和已证明的重定向别名；
4. 拒绝空 href、下载、登录、支付、上传和危险协议；
5. 检测日历、分页、标签、搜索 query 组合和重复重定向陷阱；
6. 计算 `route_shape + dom_role + structure_fingerprint`；
7. 按未覆盖页面类型、未覆盖结构簇和导航深度排序；
8. 每个结构簇只抽取冻结预算允许的代表页面。

预算耗尽时必须把剩余候选标记为 `budget_not_executed`，而不是伪造成不可达、未发现或分类失败。

### 4.6 迭代菜单发现与执行

菜单不是只在首页发现一次。首页负责播种一级入口；每个成功进入的菜单、列表、详情、搜索或活动页面，在形成稳定可信 Capture 后都执行一次 href 发现，以捕获二级菜单、分页、详情、上一篇/下一篇和相关推荐。

每次发现遵循以下顺序：

```text
稳定可信 Capture
  → 基于该 Capture 的 final_url 解析相对 href
  → 角色识别和安全过滤
  → 规范 URL、redirect alias、结构簇和全 Run 去重
  → 合并来源谱系
  → 进入统一 frontier
  → 调度器按优先级领取剩余 menu_limit slot
  → goto
```

- 同一 `capture_state_fingerprint` 最多发现一次；弹窗关闭导致可信页面状态变化后允许新一轮发现。
- 同一规范 URL 无论被多少页面重复发现，只保留一个逻辑候选；后续来源只追加 lineage，不再次 `goto`。
- 全局导航在每个页面重复出现时不得消耗新额度。
- 首页发现的大量一级菜单不得一次性预占全部 slot；调度器逐个领取，并让新发现的二级入口按未覆盖结构和页型重新参与排序。
- 菜单 href 以当前可信页面 `final_url` 为解析基准，但安全 origin 始终是冻结的 `canonical_site_origin`；跨域落地不能成为新的菜单基准。
- 详情链接不占 10 个菜单额度，按结构簇详情样本预算单独控制。

## 5. 全 Web 通用详情分类

### 5.1 先识别区域角色，再计算页型事实

结构提取器必须将页面划分为：

- `primary_subject`：页面唯一主要内容；
- `primary_article_body`：正文主体；
- `primary_media_player`：占主导的播放器；
- `inline_article_media`：正文中的附属媒体；
- `secondary_recommendations`：相关推荐、猜你喜欢、更多内容；
- `sidebar_navigation`：侧栏、目录、热门排行；
- `advertisement`：广告或推广区域。

卡片数量只能在区域角色确定后统计。`secondary_recommendations`、`sidebar_navigation` 和 `advertisement` 中的卡片不得计入 `repeated_primary_card_count`。

### 5.2 通用播放器识别

播放器事实覆盖：

- 原生 `video/source`；
- DPlayer、Video.js、Plyr、JW Player 等通用 shell token；
- 具有视频语义的 iframe；
- 延迟加载后形成的播放器容器；
- poster、controls、播放按钮和媒体资源 URL 等组合证据。

播放器位于 `article/main` 内不构成过滤理由。应根据它与标题、正文、区域面积、DOM 中心性和同一区域的文本密度判断它是主播放器还是正文内嵌媒体。

播放器产品 token 只允许作为辅助证据。即使不存在 DPlayer、Video.js、Plyr 或 JW Player 名称，只要原生媒体、iframe、播放控件、poster、媒体资源、区域面积和 DOM 中心性共同证明主播放器，仍应形成相同结论。

### 5.3 页型决策

- `video_detail`：单一主体成立，主导播放器成立，推荐卡片仅位于次要区域。
- `content_detail`：单一文章/图文/图集主体成立；正文内嵌媒体不能否决该类型。
- `content_list/video_list`：主区域确实由重复同构卡片组成，而不是详情页底部推荐。
- `unknown`：仅用于真实空白、错误页、证据不足或主区域强冲突。

生产代码禁止出现域名、`/archives/`、CMS 名称对应的专属分支。不同 CMS 名称只允许作为测试 fixture 的描述。

### 5.4 审计事实

每次分类至少保存以下实际数值和证据引用：

- `primary_subject_count`
- `primary_subject_kind`
- `primary_article_body_count`
- `primary_article_text_length`
- `primary_player_count`
- `inline_article_media_count`
- `repeated_primary_card_count`
- `secondary_recommendation_card_count`
- `breadcrumb_count`
- `detail_navigation_source_count`
- 每个事实对应的 selector/token 摘要

### 5.5 结构簇和变形不变量

- 多个 URL 具有相同 `structure_fingerprint` 时，只算一个独立分类结构样本，避免把同模板的 16 个详情误认为 16 种覆盖。
- 同一脱敏 DOM 替换域名、路径、标题文本和非语义 class 后，页面类型必须保持不变。
- 只有 origin 安全和导航来源事实允许随 URL 变化；主体区域、列表/详情轴和内容/视频轴不得变化。
- 生产代码扫描不得在分类、导航和状态机中出现三个回归域名、`/archives/` 或站点专属选择器。

## 6. 通用长页面证据

- 主 Capture 成功后，长图作为可失败的独立阶段执行；失败不影响分类、href 发现和后续候选。
- 按 2026-09-16 最新要求，请求滚动 1920px，滚动停止即保存一张普通视口 PNG；不要求实际位置精确等于 1920px，不再执行全页或 3000px 分段截图。
- 记录实际滚动位置、视口范围、文档高度和截图引用；截图后的恢复失败只留审计，不得作废已保存图片。
- 补图直接保存 Playwright 原始 PNG，大小和摘要从最终保存字节一次计算；不做二次重编码，不宣称覆盖整页。
- 同一来源 Capture 最多形成一个正式长图终态；恢复时复用已成功的 Surface Phase。

## 7. 数据库和兼容策略

- 新增 `qc_web_run_stage_checkpoint`，不修改 App/H5 表。
- 继续复用 `qc_surface_phase_run` 保存截图、广告等一次性副作用终态。
- checkpoint 只负责运行推进与恢复，Surface Phase 只负责阶段证据，二者职责不得混合。
- 历史 Web Run 不回填 checkpoint，也不创建 legacy checkpoint；历史查看只读已有证据。历史任务重跑必须创建新的 Task、当前版本 Run 和当前 checkpoint，并记录现有 `source_task_id`、`source_operation=rerun`。
- 页面类型 Catalog 新版本只供新 Run 使用；历史 Run 继续使用其冻结版本。
- 数据迁移必须可升级、可降级，并为唯一键、CAS `row_version` 和待重试索引提供测试。

## 8. 快速验证策略

### 8.1 第一层：确定性离线回放

从已留存 Capture 中提取脱敏的最小 DOM 结构 fixture，不提交原始成人内容、图片或完整 HTML。fixture 覆盖：主播放器、正文内嵌视频、相关推荐、侧栏排行、列表主卡片、图文详情、真实证据不足、URL 陷阱和跨域重定向。

回放动态 href、广告命令校验、阶段恢复和分类，并对同一 fixture 随机替换域名、路径和文本。目标是在数分钟内完成高风险逻辑回归，并证明行为不依赖三个样本站点。

### 8.2 第二层：受控双 origin Playwright 验证

使用本地两个 HTTP origin 稳定验证：普通跨域阻断、跨域 DOM 不进入 frontier、来源页恢复、广告 href 隔离打开、广告 click 底层拒绝、redirect alias、长页面上限和 Worker 中断恢复。该层不依赖外站是否恰好投放广告或发生 TLS 错误。

### 8.3 第三层：真实站点能力矩阵

三个站点都提交真实 exploration-only Run。并发度取 `min(可用 Worker, 3)`；单 Worker 时正常排队，不伪造并发。

每站最低见证为：根导航成功或形成可解释外部失败、首页证据或 gap、动态候选安全过滤结果、所有阶段离开非终态、checkpoint 可恢复和最终收口完成。

三站合并后的能力矩阵必须覆盖：至少两种列表结构、至少四种不同详情结构簇、年龄确认、多层或自动消失弹窗、跨域重定向阻断与恢复、长页面证据，以及发现广告时的隔离 `ad_page`。外站未出现广告时，以受控双 origin 测试作为广告硬验收，实况报告记录候选数为 0。

Codex 测试期间实时输出增量，不重复打印旧记录：新增 URL、请求/最终 URL、页型、置信度、菜单 href、关键结构事实、动作、DOM Artifact、截图 Artifact、重试、gap 和 Run 状态。报告器仅为离线验证工具，不在质检系统加入两分钟定时报告功能。

### 8.4 第四层：冷启动与按失败站点延长

至少增加一个从未出现在 Registry、fixture 和回归域名列表中的冷启动目标，证明只提交 URL 即可建立 frontier。只有缺少能力矩阵见证或仍存在未解释 gap 的站点进入延长探索。外部 TLS/Worker 网络失败必须与代码回归分开报告，不得通过站点专属逻辑规避。

## 9. 支持边界

本设计的“通用 Web”指当前页面类型 Catalog 覆盖的、可由标准 HTTP(S) href 安全导航的内容、媒体、列表、详情、活动和搜索页面。纯 JavaScript 按钮导航、登录后页面、支付、上传、下载和高度交互式 Web App 不在本期自动执行范围；系统必须记录明确 gap，不能为了扩大覆盖放开 click 或危险操作。

## 10. 验收标准

1. 任意新建、重跑或重新探索的 Web 任务都创建当前版本的新 Run，且无法关闭动态 href 发现。
2. 新 Web 主路径不读取站点/菜单 Registry 决定是否执行。
3. 广告 click 在页面采集底层被拒绝，广告只能通过 href 在隔离 Playwright 中打开。
4. Worker 在任意阶段中断后能够从 checkpoint 恢复，已完成副作用不重复执行。
5. GOTO/TLS、弹窗和长图失败均产生有界重试或明确 gap，非根失败不阻塞剩余候选。
6. `direct_ad_page_enabled` 不再影响任何探索执行；旧字段仅供历史报告、审计和冻结策略只读展示。
7. 所有 Web 详情页面使用同一套主体区域事实；生产代码没有站点或路径特判。
8. 主播放器详情稳定为 `video_detail`，文章/图文/图集详情稳定为 `content_detail`，次要推荐不制造列表冲突。
9. 跨域落地 DOM、空 href 和无法规范化 URL 不得污染来源站 frontier。
10. frontier 具有规范化、结构聚类、陷阱检测和冻结预算，预算耗尽形成 `budget_not_executed`。
11. 新 Run 默认最多正式采集 10 个唯一同源菜单，配置值被冻结；首页、详情、广告、阻断和别名不占额度。
12. 每个稳定可信页面都可发现一次 href；同一规范菜单只访问一次，重复来源只追加 lineage。
13. 长页面 lazy-load、sticky 层和高度上限具有正式证据或明确 gap。
14. 三站验证由 Codex 实时输出可审计增量，并按合并能力矩阵收口。
15. 受控双 origin 测试稳定证明广告隔离、跨域恢复和底层禁 click。
16. 至少一个从未进入 Registry/fixture 的冷启动域名不增加配置即可探索。
17. App/H5 生产代码无修改，边界回归结果不变。
18. 新 Run 请求携带旧执行覆盖字段时返回 `422 WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN`；不得静默忽略或归一化。
19. 历史任务重跑生成新 Task ID 和 Run ID，并记录 `source_task_id`、`source_operation=rerun`；非终态旧执行 Run 不再续跑旧状态机。
