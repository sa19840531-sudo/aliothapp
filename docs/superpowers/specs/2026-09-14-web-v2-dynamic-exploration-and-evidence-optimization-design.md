# Web V2 动态探索与证据链完整优化设计

> 日期：2026-09-14
>
> 状态：设计基线
>
> 适用范围：仅 Web V2
>
> 关联设计：`2026-09-13-web-full-page-supplemental-and-expanded-page-types-design.md`

## 1. 设计目标

本设计用于统一解决当前 Web V2 在站点接入、页面证据、广告页面、页面分类、成年声明与运行状态方面的问题。

最终目标是：新增网站时只提交目标 URL，不再维护域名或菜单白名单；系统从真实 DOM 动态发现同源链接，为每个稳定页面保存视口主证据和全页补充证据，对广告使用隔离标签页留存 `ad_page`，并保证任务、运行、报告和审计读取同一份冻结事实。

本设计只改变 Web V2。App、H5 的 Catalog、截图参数、导航、报告和运行语义必须保持不变。

## 2. 已确认的现状与问题

### 2.1 新任务仍依赖站点与菜单白名单

当前实现仍存在 `web-menu-allowlist.v4`、`allowlisted_observe_only`、`allowlist_match`、`strict_catalog` 和 `unknown_site_safe_fallback`。未知站点虽然可以进入安全兜底，但站点适用性、菜单提示和部分导航授权仍受静态目录影响，因此尚未实现真正的任意网站动态探索。

### 2.2 全页补图策略启用但未执行

实况任务的冻结策略已经启用 `full_page_supplemental_policy`，但数据库中不存在 `phase_key=web_full_page_supplemental` 的阶段记录。已分类的 `home`、`content_list`、`age_gate` 和 `unknown` 均记录为 `WEB_FULL_PAGE_SCREENSHOT_SUPPLEMENTAL_UNAVAILABLE`。

根因是补图 claim 要求来源 Capture 已具有正式 `business_page` admission，而正式 admission 只有在页面分类完成后才写入。当前顺序形成“先分类才能补图、先补图才能完整分类”的循环依赖。

### 2.3 广告已经部分采集，但终态仍为失败

现有广告链能够产生来源/落地 DOM、PNG、截图一致性描述和动作记录，但广告阶段仍以 `WEB_AD_LANDING_EVIDENCE_INSUFFICIENT` 或 `WEB_AD_LANDING_OUTCOME_UNKNOWN` 结束。广告被套用了普通业务页面的证据充分性语义，没有形成可直接查询的广告页面终态。

### 2.4 缺少 `ad_page`

当前 Web v8 Catalog 只有七类业务页、一个生命周期页和一个兜底页，没有正式广告页面类型。广告 Capture 与全页长图还共同使用 `capture_role=supplemental`，如果不结合明确用途，查询时无法可靠区分二者。

### 2.5 通用文章/图文详情页可能被误判为 `unknown`

`hl365.com` 实况审计直接暴露了该问题：多个 `/archives/*.html` 页面已被模型识别为 `content_detail`，并观察到 `article.post`、`.post-content`、标题、作者时间、正文、面包屑和列表到详情来源，但确定性结构提取结果仍报告 `article_body_count`、`paragraph_text_length`、`single_subject_element_count` 等关键事实缺失，最终把详情页降为 `unknown`。根因位于通用主内容结构提取和确定性分类门禁，因此所有采用相似 CMS 模板、语义标签或正文容器的站点都可能受影响，禁止通过域名或 URL 路径特判修复。

### 2.6 成年声明在 Task 与 Run 之间不一致

部署级成年声明已经进入 `qc_agent_run.input_context.task_config._task_target`，运行时能够读取；但 `exploration_only` 任务没有把 `_task_target` 写入 `qc_task.task_config`。任务详情、审计或重试从主表读取时，会产生配置未生效的假象。

### 2.7 排队与配置失败没有清晰区分

单 Worker 模式下，新网站任务会处于 `queued/pending`，等待当前探索结束。当前展示不足以让用户区分正常排队、配置错误、导航失败和证据失败。

## 3. 核心设计决策

### 3.1 新 Web Run 完全取消白名单依赖

新 Web Run 不得读取站点或菜单白名单来决定链接是否可执行，也不得要求目标域名预先存在于静态 Catalog。

以下字段和语义从新任务的执行合同中移除：

- `allowlisted_observe_only`
- `allowlist_match`
- `strict_catalog`
- `unknown_site_safe_fallback`
- 按域名预配置的菜单路径和页面类型定案

历史目录保留为 `legacy-only`，只允许旧 Run 按冻结版本恢复，禁止影响新任务。

### 3.2 URL 安全策略继续作为硬门禁

取消白名单不等于取消安全边界。新运行时必须继续执行以下通用规则：

- 只接受 HTTP(S) URL；
- 普通业务导航必须同源；
- 禁止 `javascript:`、`file:`、`data:` 等协议；
- 禁止自动登录、支付、上传、下载和表单提交；
- 普通菜单、搜索和活动只执行真实有效的 `href`；
- 普通跨域链接阻断并审计；
- 跨域广告只能进入隔离标签页；
- 所有重定向保存请求 URL、最终 URL 和同源判断结果。

### 3.3 静态 Catalog 只保留页面类型知识

页面类型 Catalog 不是站点白名单，必须继续保留。它只描述页面类型、结构规则、媒体规则和覆盖分组，不得包含“某个域名允许访问哪些路径”的执行授权。

### 3.4 Web 页面类型扩展为十类

新 Catalog 分组固定为：

```text
business:
  home
  content_list
  content_detail
  video_list
  video_detail
  activity
  search_page

lifecycle:
  age_gate

evidence_only:
  ad_page

neither:
  unknown
```

`age_gate`、`ad_page`、`unknown` 永不进入业务覆盖率分母。

## 4. 动态探索流程

```text
提交任意网站 URL
        ↓
建立目标 origin 与 URL 安全策略
        ↓
打开首页并保存稳定主 Capture
        ↓
补采同状态全页长图
        ↓
使用 DOM、视口截图和长图分类
        ↓
从真实 DOM 提取同源 HTTP(S) href
        ↓
普通链接当前页 goto；广告链接隔离页打开
        ↓
对新页面重复采集、分类和链接发现
```

导航候选必须来自当前可信 Capture 的真实 DOM。菜单名称、URL 路径和历史站点知识只能作为弱提示，不能覆盖真实落地页面证据。

## 5. 动态适用性与覆盖率

去除站点目录后，页面适用性由本次运行的证据决定：

- `home`：固定 required；
- `applicable`：已发现明确、安全且可达的页面入口；
- `covered`：已访问并完成正式分类与证据留存；
- `applicable_uncovered`：存在入口，但访问、采集或分类未完成；
- `not_observed`：没有发现对应入口，不直接判失败，也不进入分母；
- `not_applicable`：只有在存在充分运行证据时才能签发。

覆盖率分母为 `home` 加本次运行实际发现为 applicable 的业务类型；分子为已完成正式分类和证据留存的业务类型。禁止因为站点没有视频、活动或搜索入口而无条件扣分。

## 6. 全页补充证据

### 6.1 正确时序

全页补图必须发生在稳定主 Capture 生成之后、正式页面分类之前：

```text
ready 主 Capture
  → 一次性 supplemental claim
  → Playwright full_page=true
  → 保存长图子 Capture
  → 页面分类
  → 写入正式 admission
```

### 6.2 Claim 条件

Claim 只验证：

- Task、Run、Session、finalization epoch 一致；
- 来源 Capture 是 Session 当前 Capture；
- 来源 Capture 状态为 captured；
- readiness 为 stable；
- 来源角色为 `after_action` 或 `primary`；
- 主截图和 DOM 已存在；
- URL、页面身份和结构没有漂移。

Claim 不得要求来源 Capture 已有 `page_type`、`business_page` admission 或正式分类结果。

### 6.3 长图合同

长图子 Capture 必须满足：

```text
capture_role = supplemental
capture_purpose = web_full_page_supplemental
parent_capture_id = 来源主 Capture ID
```

每个来源 Capture 最多尝试一次。成功、失败和取消都必须产生 `qc_surface_phase_run` 终态。长图失败不得破坏主 Capture、页面分类或后续探索。

失败原因至少区分：

- `WEB_FULL_PAGE_SCREENSHOT_TIMEOUT`
- `WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED`
- `WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT`
- `WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED`
- `WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED`
- `WEB_FULL_PAGE_SCREENSHOT_CANCELLED`

禁止把所有基础设施异常统一吞并为 `SUPPLEMENTAL_UNAVAILABLE`。

## 7. 广告页面与 `ad_page`

### 7.1 直接签发规则

广告页面不进入普通页面分类或证据充分性判断。只有同时满足以下条件才能直接签发 `ad_page`：

- 来源 DOM 元素被可靠识别为广告或推广；
- 元素具有有效 HTTP(S) `href`；
- 通过广告隔离入口创建新 Playwright 标签页；
- 标签页确实打开成功。

### 7.2 执行流程

```text
发现广告 href
  → 隔离标签页打开
  → 保存请求 URL、最终 URL、截图和基础 DOM
  → 记录 page_type=ad_page
  → 关闭标签页
```

`ad_page` 不进入 `evidence_insufficient`、`unknown`、required/optional 判断或后续探索。标签页没有成功打开时，只记录 `WEB_AD_PAGE_OPEN_FAILED`，不得虚构广告页面 Capture。

### 7.3 广告证据合同

```text
capture_role = supplemental
capture_purpose = web_ad_page
page_type = ad_page
parent_capture_id = 广告来源页面 Capture ID
```

广告证据不得推进当前业务页面指针，也不得污染站内覆盖率。

## 8. 通用 `content_detail` 分类修复

结构事实提取器必须基于通用页面语义识别：

- `article`、`article.post`；
- `.post-content`、`.entry-content`、`.article-content`、`.detail-content`；
- `.post-title`、`.post-meta`；
- 面包屑、上一篇/下一篇；
- 单一主体图集和正文内嵌媒体；
- 可信列表到详情导航来源。

正文长度必须从主内容区域计算，不能因为视口裁剪、广告过滤、嵌套节点去重或可见文本截断被清零。

满足“单一主体 + 标题 + 正文容器 + 足够正文或图集内容 + 详情语义/列表到详情来源”时，应允许签发 `content_detail`。文章正文中的内嵌视频不能自动把页面改判为 `video_detail`；只有播放器构成页面主要主体时才允许 `video_detail`。

分类审计必须保存结构事实实际值，而不能只保存规则缺失字符串。

实现中禁止加入 `hl365.com`、`/archives/` 或其他站点/路径硬编码。`hl365.com` 只作为已知回归样本，通用规则还必须覆盖常见 `article`、WordPress/Typecho 风格正文、图集、漫画阅读和自定义详情容器。

## 9. Supplemental 身份与查询规范

所有 supplemental Capture 必须设置明确用途：

```text
web_full_page_supplemental
web_ad_page
web_age_gate_supplemental
```

证据身份由以下字段联合确定：

```text
capture_role + capture_purpose + parent_capture_id + page_type
```

查询全页长图时必须限定 `capture_purpose=web_full_page_supplemental`，禁止把所有 `capture_role=supplemental` 的 Capture 都统计成长图。

## 10. 成年声明一致性

所有 Web V2 任务，无论 `execution_mode` 是 `full_inspection` 还是 `exploration_only`，都必须把完整 `_task_target` 写入 `qc_task.task_config`。

持久化后必须满足：

```text
qc_task.task_config._task_target
==
qc_agent_run.input_context.task_config._task_target
```

部署级成年声明配置一次后适用于后续创建的所有 Web 任务，不按网站重复配置。配置修改后必须重启或重建 API，使进程缓存重新加载；只重启 Worker 不足以改变任务创建时冻结的声明。

## 11. 队列与状态展示

前端和接口必须区分：

- `queued`：正常排队；
- `pending`：Run 已创建、等待 Worker；
- `running`：正在探索；
- `configuration_error`：配置错误；
- `navigation_failed`：导航失败；
- `evidence_failed`：证据采集失败；
- `classification_review`：页面类型需要复核。

任务详情应展示排队位置、前方任务数、可用 Worker 数和开始条件，禁止把正常排队描述为配置无效。

## 12. 持久化与兼容

优先复用现有 JSONB、Capture、Artifact 和 Surface Phase 表。增加 `ad_page` 前必须审计数据库中的页面类型 CHECK/枚举约束；只有闭集约束确实阻止新类型时才增加最小迁移。

历史 Run 必须按其冻结 Catalog、菜单目录和覆盖规则恢复，不得把新十类集合或动态适用性规则回填到旧数据。新报告读取旧数据时应明确标记 legacy schema。

## 13. 实施顺序

1. 发布十类 Web Catalog、`evidence_only` 分组和 supplemental purpose 合同。
2. 修复长图 claim 的分类前置循环与异常审计。
3. 修复通用主内容结构事实提取，并把 `hl365.com` 作为回归样本之一。
4. 引入隔离广告 `ad_page`，移除广告普通证据充分性判定。
5. 让新 Web Run 完全脱离站点/菜单白名单。
6. 改造动态适用性与覆盖率。
7. 修复 Task/Run 成年声明快照一致性。
8. 优化队列和失败状态展示。
9. 执行未知域名、长页、详情、广告、年龄门、活动和搜索实况验收。

## 14. 测试要求

### 14.1 无白名单探索

- 使用至少三个从未配置过的新域名；
- 不修改任何站点目录即可启动；
- 能发现首页真实菜单和多层同源链接；
- 能从列表进入详情；
- 普通跨域和危险链接仍被阻断。

### 14.2 长图

- 每个稳定页面保存视口主截图和最多一张全页 supplemental；
- 长图阶段成功、失败和取消均可查询；
- 长图不推进当前 Capture 指针；
- 广告 supplemental 不会被统计成长图；
- 年龄门关闭前后两个页面状态分别补图。

### 14.3 广告

- 广告 href 使用隔离标签页；
- 打开成功后直接签发 `ad_page`；
- 保存请求 URL、最终 URL、PNG 和基础 DOM；
- 不产生 `evidence_insufficient` 或 `unknown`；
- 不进入业务覆盖率和后续探索。

### 14.4 页面分类

- 使用多个不同站点和 CMS 模板验证文章/图文详情稳定签发 `content_detail`；
- `hl365.com/archives/*.html` 作为已知回归样本通过，但实现不得包含站点或路径特判；
- 图集和漫画详情属于 `content_detail`；
- 正文内嵌视频不误判为 `video_detail`；
- 主播放器页面能够签发 `video_detail`；
- 真实错误、空白、证据不足和强冲突页面进入 `unknown`。

### 14.5 一致性与兼容

- Task 与 Run 的 `_task_target` 完全一致；
- 新成年声明只影响重新加载配置后创建的新任务；
- 旧 Run 按冻结版本恢复；
- App/H5 回归结果与改动前一致。

## 15. 验收标准

全部满足后，本设计才视为完成：

1. 新网站只提交 URL，不再增加白名单、菜单目录或站点配置。
2. 同源真实 `href` 能够动态发现并安全探索。
3. 每个稳定 Web 页面都有视口主截图。
4. 每个稳定页面最多有一张可验证的真正全页长图。
5. 长图成功和失败都有正式阶段记录。
6. 广告隔离打开后保存为 `ad_page`。
7. 广告不再进入 `evidence_insufficient` 或 `unknown`。
8. 所有满足通用详情结构门槛的文章、图文、图集和漫画详情页稳定判为 `content_detail`，`hl365.com` 仅作为其中一个回归样本。
9. `unknown` 只用于真实证据不足、错误页或强冲突。
10. 成年声明在 Task 与 Agent Run 中完全一致。
11. 排队、配置失败、导航失败和证据失败能够清晰区分。
12. 旧任务保持冻结语义，App/H5 行为不受影响。
