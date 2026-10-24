# Web Catalog 导航识别与不限菜单层级设计

## 背景与结论

附件包含 `home`、`content_list`、`content_detail`、`video_list`、`video_detail` 五组识别说明，以及导航信号和通用站点观察。

当前 `web_page_type_catalog.json` 已完整包含附件中四个非首页页型的正向、辅助、强否定和补证特征，并在原有内容上保留了更严格的结构要求。附件与当前文件的 `navigation_signals`、`common_site_observations` 完全一致。唯一未纳入的是正式 `home` 页型及其特征。

`home` 是正式页面类型，加入 Web 质检覆盖闭集。`page_role=home` 继续表达探索流程中的根页面身份；`page_type=home` 表达当前页面属于首页类型，两者分别保存并允许相互校验。

当前顶层 `platform`、`navigation_signals` 和 `common_site_observations` 会被通用 Catalog 解析器静默忽略。本设计要求这些字段进入 Web 专属强类型领域模型、运行策略快照和分类证据。

## 目标

1. 完整吸收附件特征，形成 `home`、`content_list`、`content_detail`、`video_list`、`video_detail` 五类 Web 页面闭集。
2. 将自然语言识别说明与机器可执行的结构规则分离。
3. 让页面类型判定以 DOM、截图和当前 URL 的可审计事实为主，菜单目录仅作为辅助先验。
4. 使用导航特征识别真实菜单容器及菜单候选，降低正文卡片、广告、页脚、登录和支付入口被误采为菜单的概率。
5. 支持不限菜单层级的递归发现；不设置 `menu_depth` 上限，但继续由同源限制、低风险动作、总动作预算和任务 deadline 收口。
6. 为任意网站建立运行时首页基线和菜单发现流程，不再要求站点必须预先存在于静态菜单目录。

## 非目标

- 不把 URL、域名或静态菜单目录作为页面类型定案依据。
- 不用裸域名或静态菜单目录替代首页结构证据；根 URL 是首页强证据，但当前无遮挡 DOM 和截图仍进入判定与审计。
- 不允许跨域菜单、登录、注册、支付、下载、提交、删除或授权类动作自动执行。
- 不取消任务 deadline 和总菜单动作预算。本设计中的“不限”只表示菜单树层级不限。
- 不把广告卡片或正文详情入口当作主导航菜单；详情探索继续走列表到详情的独立链路。

## Catalog 领域模型

### Web 专属根模型

新增 Web 专属 Catalog 根类型，保留现有 `PageTypeDefinition`，并增加：

- `platform: Literal["web"]`
- `navigation_signals: WebNavigationSignalDefinition`
- `common_site_observations: WebCommonSiteObservations`
- `page_types: tuple[PageTypeDefinition, ...]`

附件中的 `home` 以完整 `PageTypeDefinition` 写入 `page_types`，并设置 `coverage_required=true`、`category=business`、`rule_matchable=true`。`content_list` 与 `video_list` 的 `confusable_with` 允许引用 `home`。

加载器必须拒绝以下情况：

- `platform` 缺失或不是 `web`；
- `home` 页型缺失、为空或字段形状错误；
- `navigation_signals` 缺失、为空或字段形状错误；
- `common_site_observations` 缺失、为空或字段形状错误；
- 顶层出现未声明字段；
- 五类页面闭集、顺序或相互引用不符合发布契约。

### 自然语言说明与可执行规则

现有自然语言字段继续用于模型解释和人工报告。每个业务页型新增机器可执行的 `recognition_rules`：

- `required_all`：所有条件必须成立；
- `required_any`：至少一项条件成立；
- `strong_negative_any`：任一条件成立即产生强冲突；
- `supporting`：只增加证据权重，不单独放行。

规则只允许引用白名单中的结构事实及操作符，例如：

- `repeated_primary_card_count >= 2`
- `primary_detail_link_count >= 2`
- `media_card_count >= 2`
- `player_root_count >= 1`
- `article_body_count >= 1`
- `paragraph_text_length >= 120`
- `top_level_obstruction_count == 0`

禁止在可执行规则中直接引用域名、静态菜单 entry ID 或完整 URL。

`dom_hints` 改成精确 token、前缀或显式 selector 三种受控匹配方式。不得继续使用把 `item`、`card`、`detail`、`video` 当作任意字符串子串的匹配方式。通用 token 只能作为辅助证据，并且必须和列表/详情结构事实组合。

## 页面判定数据流

页面 Capture 先产生两个互相独立的事实对象：

1. `WebPageStructureFacts`：规范根 URL、首页多板块、重复卡片、详情入口、媒体卡片、播放器、正文、段落、广告和阻断层等业务主体事实。
2. `WebNavigationStructureFacts`：顶部导航容器、可见分类入口、搜索入口、登录入口、Logo、固定/粘性布局、页脚导航、下拉触发器、展开子菜单和被遮挡状态。

确定性规则引擎首先计算每个业务页型的正向命中、强否定冲突和缺失事实。模型接收：

- 五类页型自然语言说明；
- 导航信号；
- 通用站点观察；
- 两组结构化事实；
- 截图、DOM 和可见文本证据引用；
- 当前 URL 形态，但明确标记为辅助证据。

最终签发仍需同时满足模型判断和确定性结构门槛。静态菜单分类提示只能排序或补充说明，不能替代结构门槛。

裸域名、规范根路径或可信根重定向确定 `page_role=home`，并作为 `page_type=home` 的强正向证据。最终首页签发仍保存无遮挡 DOM、截图、导航和多板块事实；如果根页面被单一详情主体替换，必须记录 `home` 与详情结构的强冲突，而不是静默改写证据。

## 菜单识别与候选生成

菜单发现分两阶段：

1. 识别导航容器：使用 DOM 语义、位置、布局、截图和 `navigation_signals` 综合确认顶部主导航、侧边业务导航或展开后的移动菜单。
2. 提取菜单候选：只从已确认导航容器提取可见、可交互的链接或展开控件。

搜索、登录、注册和 Logo 可以证明容器具有导航语义，但这些控件本身不进入可执行菜单队列。页脚链接、广告区域、支付、下载、提交、授权及跨域入口保留审计记录，但不得自动执行。

`[data-route][class*="card"]` 等正文卡片不再自动视为菜单。只有位于可信导航容器，或具有明确导航 ARIA/路由语义且通过截图布局复核时，才可以进入菜单候选。正文卡片继续由详情候选提取器处理。

静态 `web_site_menu_catalog.json` 降级为站点先验：

- 已发布条目用于候选排序、命名和历史审计关联；
- 运行时 DOM 候选不需要先匹配静态目录；
- 未登记站点自动创建只含当前根页面的运行时站点上下文；
- 页面类型不因是否命中静态目录而改变。

## 不限菜单层级与有界执行

每个菜单候选新增并持久化：

- `parent_menu_entry_id: str | None`
- `menu_depth: int`
- `exploration_path: tuple[str, ...]`
- `source_capture_id`
- `discovery_capture_id`
- `logical_target_identity`

首页直接可见菜单的 `menu_depth=1`。点击无 URL 展开控件后发现的子菜单，以父候选深度加一。直接进入新页面后发现的页面级导航仍保留其探索父链，不能统一重置成深度 1。

不设置 `max_menu_depth`，也不因 `menu_path` 长度拒绝候选。终止条件只有：

- 总菜单动作预算耗尽；
- 任务 deadline 到达并保留收口时间；
- 没有新的稳定逻辑候选；
- 人机验证、未知动作结果或安全策略阻断；
- 当前页面所有可执行候选均已访问或复用。

总菜单动作预算首期保持 10，目录内和目录外候选共用同一预算。后续如需调整数量，只修改独立预算配置，不与菜单层级耦合。

无 URL 候选的逻辑身份必须由稳定容器身份、完整菜单路径、目标语义和父候选身份组成，不得把 Capture ID、DOM epoch 或每次重新生成的临时 entry ID 作为主要身份，否则同一展开按钮会在每次 Capture 后被重复发现并消耗预算。

## 持久化与兼容

Web 运行策略快照保存完整 Web Catalog 正文，并在恢复时重新执行全部强类型校验。Catalog 版本升级为新的 Web 版本，旧 Run 继续消费其冻结快照，不读取新规则。

新运行发布 `web-page-type-assessment.v3` 与 `web-coverage-snapshot.v3`，以五类页型作为规范分母。历史 v1/v2 assessment 和 coverage snapshot 保留其冻结的四类语义，反序列化时使用版本内置的 legacy 集合，不得用当前五类常量重新解释。适用性模式新增 `strict_catalog`；旧 API 输入 `strict_four` 仅作为兼容别名接受，新运行仍解析为五类并记录兼容原因。

菜单账本为运行时候选增加父链和深度字段。数据库约束要求 `menu_depth >= 1`，根首页基线不进入菜单动作深度。`exploration_path` 必须非空且末项与当前菜单标签一致。

对既有静态条目，父链未知时根据 `menu_path` 生成同页层级；无法可靠恢复跨页父链的历史数据标记为 `lineage_unavailable`，不得伪造父候选。

## 错误与审计

新增或统一以下诊断：

- `WEB_CATALOG_ROOT_FEATURES_INVALID`
- `WEB_PAGE_RULE_FACT_MISSING`
- `WEB_NAVIGATION_CONTAINER_UNPROVEN`
- `WEB_MENU_NON_NAVIGATION_ELEMENT`
- `WEB_MENU_LINEAGE_INVALID`
- `WEB_DISCOVERY_BUDGET_EXHAUSTED`

每次页面分类保存规则命中明细；每个菜单候选保存容器证据、父链、稳定逻辑身份、安全判定和是否执行。被排除的页脚、广告、登录或交易入口也保存原因，避免“未采集”和“未发现”混淆。

## 测试设计

1. 附件特征覆盖测试：五类页面特征无遗漏，`home` 是可加载、可分类和可覆盖的正式页型。
2. 根字段校验测试：删除或损坏 `platform`、`navigation_signals`、`common_site_observations` 时加载失败。
3. 规则引擎测试：单独的 `item`、`card`、`detail` 或 `video-list` token 不能放行错误页型。
4. 强否定测试：真实播放器、长正文、重复卡片和顶层阻断按规则产生可审计冲突。
5. 首页页型测试：根 URL 建立首页角色和强正向证据，DOM、截图、导航、多板块与强冲突事实完整入审计。
6. 导航容器测试：顶部主导航通过；仅 Logo+登录、仅页脚、广告导航和正文卡片被排除。
7. 未登记站点测试：无需静态站点目录即可建立首页基线并发现同源低风险菜单。
8. 深层菜单测试：至少构造 25 层 DOM 菜单和跨 Capture 展开链，确认不因深度被拒绝。
9. 预算测试：不限层级不改变总动作预算；第 11 个菜单动作形成预算耗尽终态。
10. 身份稳定测试：同一无 URL 展开控件在多次 Capture 中只形成一个逻辑候选。
11. 冻结策略与恢复测试：新旧 Catalog 快照互不污染，恢复后父链、深度和预算一致。
12. 全量 Web 分类、菜单、持久化、策略快照和正式报告回归。

## 验收标准

- 附件的有效识别特征全部进入可加载、可验证、可消费的 Web Catalog。
- `navigation_signals` 和 `common_site_observations` 不再是静默无效字段。
- `home` 成为正式页面类型，五类页面均可进入分类、覆盖和报告链路。
- 页面类型不能由静态菜单目录、URL 或单个宽泛 DOM token 定案。
- 未登记网站可以执行同源低风险菜单发现。
- 菜单层级不存在硬上限，且审计链能够还原父子关系。
- 总动作预算、deadline 和危险动作阻断继续生效。
