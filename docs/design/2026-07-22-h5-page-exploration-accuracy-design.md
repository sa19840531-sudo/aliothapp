# H5 页面探索准确性增强设计

## 背景

本设计基于第四批 H5 DOM 采集附件：

- `h5_page_dom_batch4_summary.html`
- `h5_page_dom_batch4_report.md`

第四批样本按手机端视口采集，禁用了图片、视频、字体等媒体资源，只保留页面结构、入口、遮挡、关闭候选、底部入口和流程路径。该批样本更接近 Alioth 当前要处理的真实页面探索问题：页面是动态 H5 应用，业务入口通常不是静态链接，而是隐藏在启动页、弹窗、底部 Tab、页面 Tab、列表卡片和个人中心入口之后。

当前项目已经具备页面采集、页面识别、H5 DOM 剖面、动态 Frontier、规则候选、规则执行和工作记忆能力。本设计不引入新的爬虫体系，不扩大页面类型目录，而是在现有链路中补强“证据已可用但稳定性指标未结束”场景下的识别准确性，减少误判为 `insufficient` 和重复 `observe`。

## 目标

1. 提升 H5 页面探索的页面类型识别准确性，尤其是 `home`、`content_list`、`video_list`、`post_list`、`profile`、`payment`、`activity` 等稳定业务页。
2. 降低因 `page_settle_complete=false` 导致的误判 `insufficient`。
3. 避免同 URL、同 DOM 指纹的页面反复 `observe`，最终耗尽瞬态恢复次数。
4. 将第四批样本中的共性 DOM 信号沉淀为确定性、可测试、可审计的 H5 profile。
5. 保持第一版改动低风险：不新增数据库迁移，不改规则完成判定，不实现 retry replay。

## 非目标

1. 不把 Alioth 扩展成通用站点爬虫。
2. 不新增 canonical page type，继续使用现有页面类型目录。
3. 不依赖站点名单或固定 URL 清单。
4. 不让模型决定动作风险等级、规则门禁或最终完成状态。
5. 不把不确定的视频卡片点击强行归类为 `video_detail`。
6. 第一版不实现会话损坏后的 retry replay。

## 附件共性

### 采集规模

第四批摘要中共有 48 个入口，其中 40 个成功采集，7 个因入口名称或入口特征存在安全风险而跳过。共采集 372 个结构快照，覆盖 13/14 类目标页面类型，未确认的是 `video_detail`。

目标页面覆盖表现如下：

| 页面类型 | 覆盖情况 | 说明 |
| --- | --- | --- |
| `line_loading` | 39 站 / 75 帧 | 入口高频出现，常含线路、加载中、获取最佳线路文案 |
| `home` | 38 站 / 39 帧 | 关闭启动遮挡后进入，常带推荐流和底部 Tab |
| `content_list` | 38 站 / 38 帧 | 常由推荐、漫画、片库、分类 Tab 进入 |
| `content_detail` | 16 站 / 16 帧 | 由列表首个低风险内容卡片进入 |
| `video_list` | 25 站 / 25 帧 | 视频、短视频、热播、影视、片库入口明显 |
| `video_detail` | 未确认 | 点击后缺少可靠播放器、详情 URL 或详情 DOM 信号 |
| `ranking` | 25 站 / 25 帧 | 排行、热榜、热门入口，常复用列表结构 |
| `profile` | 25 站 / 25 帧 | 我的、会员中心、钱包、任务入口集中 |
| `settings` | 25 站 / 25 帧 | 多从个人中心进入 |
| `payment` | 25 站 / 26 帧 | 充值、VIP、金币、钱包页面高频出现 |
| `post_list` | 25 站 / 25 帧 | 社区、广场、帖子流 |
| `post_publish` | 24 站 / 24 帧 | 发布、投稿、上传入口，需要风险隔离 |
| `activity` | 24 站 / 24 帧 | 任务、福利、签到、公告 |
| `startup_ad` | 4 站 / 4 帧 | 全屏广告、倒计时、跳过、进入 |

### URL 与路由共性

样本中 HTTPS 占绝大多数，直连 IP 多于域名，入口路径多数是根路径，少量是 `/product`、`/movie`、`/comics`、`/mine`。页面内 `href` 候选很少，hash 路由也不明显，说明主要探索方式不能依赖传统链接抓取，而应依赖运行时 DOM 上的可点击结构。

这对当前系统的含义是：页面探索应以 DOM 结构入口和运行时动作队列为主，URL 只能作为辅助证据。

### 页面路径共性

典型路径可以抽象为：

```text
入口
  -> line_loading
  -> startup_ad 或其他启动遮挡
  -> home
  -> content_list / video_list / post_list / ranking / activity / payment / profile
  -> content_detail 或保守尝试 video_detail
  -> browser_history_back
  -> 继续探索剩余低风险分支
```

其中 `line_loading` 和 `startup_ad` 经常会覆盖底层业务 DOM。页面上看起来已经有首页或列表，但最上层遮挡仍是线路页或启动广告。页面识别必须优先判断当前最上层壳层，再决定是否允许规则匹配。

### 遮挡与关闭共性

第四批摘要中遮挡和关闭候选非常集中：

| 信号 | 统计或表现 |
| --- | --- |
| modal/popup 类 | 约 1180 次 |
| `.loading-page` | 摘要区约 535 次，常见弹层类统计中约 2072 次 |
| `.down-time` 倒计时关闭 | 约 282 次 |
| 文本关闭按钮 | 约 89 次 |
| `.van-overlay` | 摘要区约 5 次，常见弹层类统计中约 41 次 |
| `.van-popup` | 摘要区约 2 次，常见弹层类统计中约 8 次 |
| `z-index >= 1000` | 约 3743 个候选 |
| 最高 z-index | 99999 |

因此，探索动作不能简单“找一个关闭按钮”。应按当前可见遮挡栈和 z-index 选择最上层关闭动作，每次只关闭一层，然后重新采集识别。

### 风险共性

附件中选择详情时只点击首个看起来属于站内内容流的安全卡片，并跳过下载、安装、登录、支付、充值等入口。结合 Alioth 质检目标，需要进一步区分：

- 可以探索的只读页面：充值页、VIP 页、钱包页、个人中心、任务页。
- 必须阻断的真实变更动作：支付、购买、下单、绑卡、提交、登录、注册、发布、上传、下载、安装。

如果把“进入支付页面”和“点击支付按钮”都标成高风险，会漏掉 `payment` 页面覆盖；如果都放行，又会产生真实交易或状态变更风险。

## 当前实现问题

### 稳定性门禁过早

当前 `page_detection_tools.py` 在读取证据后，会先检查：

```python
if pipeline_metrics.get("page_settle_complete") is False:
    return _insufficient_result(..., missing_sources=["page_stability"])
```

该判断发生在 `build_h5_exploration_profile(...)` 之前。结果是：

1. DOM、截图、可见文本、UI snapshot 已经可用时，仍会被判为 `insufficient`。
2. H5 profile 无法参与 `line_loading`、`startup_ad`、稳定业务页的确定性识别。
3. working memory 会把页面视为瞬态页，继续生成同会话 `observe`。
4. 如果页面内容指纹没有变化，会重复 observe，最终触发 `TRANSIENT_PAGE_RECOVERY_EXHAUSTED`。

这与前面本地链路测试现象一致：页面可以在浏览器中正常打开，证据也已持久化，但页面识别一直停在 `needs_more_observation` 或 `insufficient`，规则无法进入。

### H5 profile 接入位置偏后

当前 `page_exploration_profile.py` 已经提供纯函数 DOM 信号提取，并在页面识别模型返回后做 `line_loading` 和 `startup_ad` 的确定性修正。但它还没有参与最关键的稳定性门禁决策，因此无法修复“页面未完全静默但结构已稳定”的场景。

### 重复 observe 缺少内容稳定判断

`page_capture_tools.py` 已经输出 `content_fingerprint`，working memory 也会保存该字段。该字段可以识别“页面内容没有变化”。目前瞬态恢复更关注 page type 是否是 `line_loading` 等状态，缺少“连续相同 fingerprint 时停止补采”的策略。

## 设计方案

### 总体思路

推荐第一版采用保守增强方案：

1. 将 H5 profile 构建前置到稳定性门禁之前。
2. 将 `page_settle_complete=false` 从硬失败改为可分类的降级信号。
3. 使用现有 `content_fingerprint` 阻断重复 observe。
4. 扩展 H5 profile 的结构信号，但不改持久化字段。
5. 强化动作风险模型，允许只读页面导航，阻断真实变更动作。

备选方案对比：

| 方案 | 内容 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- | --- |
| A. 识别阶段降级增强 | H5 profile 前置，稳定性失败可降级识别 | 改动小，直接解决当前误判 | 对 capture 等待策略不做深改 | 推荐第一版 |
| B. 采集等待策略重写 | 调整 `_wait_for_page_settle` 判断标准 | 能从源头减少 false unstable | 影响所有页面采集，回归面大 | 后续再做 |
| C. 站点画像或样本学习 | 根据批量样本训练或维护站点 profile | 覆盖率可能更高 | 容易站点化、不可解释、维护重 | 不建议第一版 |

## P0：稳定性门禁降级

### 新决策顺序

页面识别主流程调整为：

```text
读取持久化证据
  -> 检查 DOM / screenshot 是否存在
  -> 构建 h5_profile
  -> 读取 pipeline_metrics.page_settle_complete
  -> 根据 h5_profile + DOM 信号决定是否硬 insufficient
  -> 调用模型或确定性 fallback
  -> 后处理页面类型、页面状态、候选动作
```

### 稳定性失败分类

当 `page_settle_complete=false` 时，按下表处理：

| 场景 | 判定依据 | 处理 |
| --- | --- | --- |
| 真实线路加载 | `h5_profile.shell=line_loading`，且无稳定业务结构 | 返回 `line_loading`，不允许规则匹配，保留 observe 恢复 |
| 启动广告 | `h5_profile.shell=startup_ad` 或可见 `.loading-page .down-time` | 返回 `startup_ad`，补关闭动作，不允许普通业务规则 |
| 业务 DOM 已稳定 | 可见底部 Tab、页面 Tab、列表卡片、个人中心、支付页、社区流等 | 继续页面识别，风险标记为稳定性降级 |
| 证据不足 | 无 DOM、无截图、DOM 为空或明显阻塞页 | 返回 `insufficient` |
| 指纹重复无变化 | 同 URL、同 fingerprint 连续出现 | 停止 observe，按可用证据识别或转人工复核 |

### 输出标记

建议在返回结果中增加 risk flag，不新增数据库字段：

- `deterministic_h5_stability_degraded`
- `deterministic_h5_profile_before_stability_gate`
- `repeated_observe_same_content_fingerprint`
- `stable_dom_with_background_activity`

这些字段进入现有 `risk_flags` 或 `metrics`，用于审计和测试断言。

## P0：重复 observe 收敛

同会话恢复时，如果满足以下条件，应停止继续 observe：

1. 当前 capture 与前一次同源恢复 capture 的 URL 相同。
2. `content_fingerprint` 相同。
3. 两次都只是 `page_settle_complete=false`，没有新增遮挡层或新页面类型信号。

停止后按以下顺序处理：

1. 如果 H5 profile 可识别稳定业务页，则进入页面识别和规则匹配。
2. 如果 H5 profile 仍是 `line_loading`，按线路加载失败或人工复核收口。
3. 如果是阻塞页、验证页、空白页，转为明确失败，不继续无效 observe。

第一版可以只在 working memory 或 page detection 附近做最小判断，不需要实现完整 retry replay。

## P1：H5 profile 信号扩展

当前 `page_exploration_profile.py` 应继续保持纯函数，不依赖浏览器、数据库或模型。建议扩展以下字段：

```json
{
  "profile": "h5_common",
  "shell": "line_loading | startup_ad | stable | unknown",
  "shell_confidence": 0.0,
  "business_structure_present": true,
  "obstruction": {
    "present": true,
    "dismiss_selector": "...",
    "z_index": 3000,
    "source": "startup_ad | dialog | popup | overlay | sticky_banner",
    "fingerprint": "sha256:..."
  },
  "tabbar_items": [],
  "page_tab_items": [],
  "content_card": null,
  "risk_entries": [],
  "page_type_candidates": [],
  "stability_hints": []
}
```

### 壳层识别

补强以下信号：

- `line_loading`：线路、获取线路、正在获取最佳线路、加载中、loading 文案。
- `startup_ad`：`.loading-page .down-time`、跳过、关闭、进入、立即进入、倒计时。
- `stable`：有可见底部 Tab、页面 Tab、列表卡片、个人中心模块、支付页模块、社区流等。

当 `loading-page` 与底层业务 DOM 同时存在时，以最上层壳层和关闭控件为准。

### 遮挡识别

按 z-index 和 DOM 层级提取当前最上层遮挡，只输出一个首选关闭动作。候选来源包括：

- `.loading-page .down-time`
- `.van-dialog`
- `.van-popup`
- `.van-overlay`
- `.home-suspend .btn-close`
- 文本按钮：关闭、跳过、知道了、我知道了、进入、稍后

如果没有明确关闭控件，但有可点击 overlay，可以输出 overlay 关闭候选；若 overlay 被更高层 dialog 覆盖，则不得优先点击 overlay。

### 导航入口识别

补充底部 Tab 和页面 Tab：

- 底部 Tab：`div.van-tabbar:visible > div.van-tabbar-item`
- 页面 Tab：`.van-tabs .van-tab`

标签到页面类型的保守映射：

| 标签关键词 | 期望页面类型 |
| --- | --- |
| 首页、推荐、热门、home | `home` |
| 视频、短视频、热播、影视、片库 | `video_list` |
| 漫画、动漫、图文、小说 | `content_list` |
| 社区、广场、圈子、论坛、评论 | `post_list` |
| 排行、榜、热门、Top | `ranking` |
| 我的、个人、会员中心 | `profile` |
| 设置、账号、系统 | `settings` |
| 充值、VIP、金币、钱包 | `payment` |
| 任务、福利、签到、活动 | `activity` |
| 发布、投稿、上传 | `post_publish` |

映射只作为候选和后处理依据，不直接绕过页面识别目录校验。

### 内容首卡识别

对 `content_list`、`video_list`、`post_list` 补充首个低风险内容卡片候选：

- 优先选择真实内容流容器，如 `.media-item`、`.data-item`、`.post-item`、`.feed-item`。
- 排除广告容器、下载入口、安装入口、充值入口、登录入口、游戏入口、外链入口。
- `video_list` 点击后只有出现播放器、视频容器、详情路由或明显详情 DOM 时，才确认 `video_detail`。
- 不能确认时只记录动作结果，不写入 `video_detail` 覆盖。

## P1：页面识别后处理

页面识别结果仍由 `page_detection_tool` 统一返回，但后处理顺序调整为：

1. H5 profile 修正瞬态壳层：`line_loading`、`startup_ad`。
2. H5 profile 修正稳定性降级：已具备业务结构时继续识别。
3. Active tabbar 修正模型误判的 `home`。
4. 当前最上层遮挡修正 `page_state=popup`。
5. 补充确定性关闭动作。
6. 补充底部 Tab、页面 Tab 和首卡详情候选。
7. 过滤真实高风险动作。

`rule_matching_allowed` 的原则保持不变：

- `line_loading`、`startup_ad` 不允许普通业务规则匹配。
- 稳定业务页允许规则匹配。
- 带遮挡业务页先执行遮挡关闭，重新采集后再进入规则匹配。
- 登录墙或认证必需页面不允许继续普通规则匹配。

## P1：动作风险模型

动作风险需要区分“导航到页面”和“触发状态变更”。

### 低风险可执行

- 关闭、跳过、进入、知道了等遮挡关闭动作。
- 底部 Tab、页面 Tab、同源只读导航。
- 列表首个真实内容卡片。
- 浏览器历史返回。
- 只读 observe、timed_capture、scroll。
- 导航到充值、VIP、钱包、个人中心等页面本身。

### 高风险阻断

- 支付、购买、下单、绑卡、提交订单。
- 登录、注册、绑定手机号、输入凭证。
- 发布、投稿、上传、评论、关注、点赞。
- 下载、安装、唤起外部应用。
- 跨域外链跳转，除非后续有专门受控工具和规则要求。

高风险动作进入 `inactive_actions`，状态为 `blocked`，保留来源页面、selector、目标文案和 reason code。

## P2：Frontier 优先级

探索队列建议按以下顺序选择动作：

1. 当前页面未识别：先执行 `page_detection_tool`。
2. 当前最上层遮挡：只关闭一个遮挡，然后重新采集。
3. `line_loading`：同会话 observe，但受重复 fingerprint 收敛限制。
4. `startup_ad`：完成启动广告页规则后执行关闭或跳过。
5. 稳定业务页：先完成当前页规则匹配与规则执行。
6. 底部 Tab、页面 Tab：覆盖缺失业务分支。
7. 列表首卡详情：每类列表最多进入一个低风险详情样本。
8. 详情页返回：完成详情页规则后回到来源列表。
9. 剩余 Frontier 收口：预算、风险或重复限制触发时写入明确终态。

该顺序可以减少两类问题：

- 当前页还没完成就切到其他 Tab，导致规则绑定错页。
- 同一个弹窗关闭 selector 在同一个 DOM 指纹上反复失败。

## 数据与审计

第一版不新增数据库迁移。建议复用现有结构承载增强信息：

- `PageDetectionResult.risk_flags`
- `PageDetectionResult.metrics`
- `candidate_actions.target_keywords`
- `candidate_actions.purpose`
- `ExplorationActionMemory.failure_reason`
- `PageCaptureResult.content_fingerprint`
- `capture_metadata.pipeline_metrics`

可记录的审计信息：

- H5 profile 是否在稳定性门禁前构建。
- 稳定性失败是否被降级处理。
- 降级依据是业务结构、内容指纹、底部 Tab 还是页面 Tab。
- 重复 observe 是否被同 fingerprint 收敛。
- 高风险动作被阻断的 reason code。

如果后续需要报表维度，再考虑把 `h5_profile`、`stability_degraded_reason`、`action_family` 等字段结构化入库。

## 错误处理

| 错误或状态 | 处理策略 |
| --- | --- |
| DOM 缺失 | 返回 `insufficient`，missing source 为 `dom` |
| screenshot 缺失 | 返回 `insufficient`，missing source 为 `screenshot` |
| `page_settle_complete=false` 但业务结构稳定 | 降级识别，添加 risk flag |
| `line_loading` 连续 observe 无变化 | 收口为线路加载失败或人工复核 |
| `startup_ad` 关闭动作失败且指纹无变化 | 隔离该关闭动作，重新观察或转人工复核 |
| 顶层遮挡无法定位关闭控件 | 不执行业务导航，记录遮挡阻塞 |
| `video_detail` 信号不足 | 不写确定页面类型，不计入覆盖 |
| 高风险动作 | 阻断并写入 inactive action |

## 测试方案

### 单元测试

新增或补充以下测试：

1. `page_settle_complete=false` 且 DOM 有稳定底部 Tab 时，不直接返回 `insufficient`。
2. `page_settle_complete=false` 且 DOM 是真实 `line_loading` 时，仍禁止规则匹配。
3. `.loading-page .down-time` 覆盖底层首页时，识别为 `startup_ad`。
4. 多层弹窗按 z-index 只输出最上层关闭动作。
5. 底部 Tab 和页面 Tab 映射到正确候选页面类型。
6. 列表首卡过滤广告、下载、登录、充值、外链容器。
7. 支付页导航允许，支付提交动作阻断。
8. `video_detail` 缺少播放器或详情 DOM 时不确认。

### Working memory 测试

补充以下测试：

1. 同 URL、同 `content_fingerprint` 连续 observe 后停止继续恢复。
2. 重复关闭同 selector 且 fingerprint 未变化时隔离动作。
3. 当前页规则未完成时，不抢先执行全局底部 Tab。
4. 当前详情页完成后，浏览器历史返回优先于其他全局导航。

### 集成测试

使用本地完整链路目标页面回归：

- 入口经过 `/product` 或同类动态路由。
- DOM、截图、UI snapshot 已持久化。
- 即使 `page_settle_complete=false`，也不应连续 3 次 observe 后失败。
- 能识别至少一个稳定业务页并进入候选规则匹配。
- `line_loading` 和 `startup_ad` 不误跑普通业务规则。

## 分阶段落地

### 第一阶段：P0

范围：

- H5 profile 前置到稳定性门禁之前。
- `page_settle_complete=false` 改为分类降级。
- 同 URL、同 fingerprint 重复 observe 收敛。
- 补充最小测试。

不做：

- 数据库迁移。
- retry replay。
- 页面类型目录扩展。
- 完成判定策略重写。

### 第二阶段：P1

范围：

- 扩充 H5 profile：遮挡、Tab、页面 Tab、首卡、风险入口。
- 页面识别后处理使用 profile 输出候选动作。
- 风险模型区分只读导航和真实变更动作。
- 补充更多 DOM fixture 测试。

### 第三阶段：P2

范围：

- Frontier 优先级调整。
- 重复遮挡动作隔离。
- 当前页规则完成前的全局导航约束优化。
- 详情页返回和列表分支恢复优化。

### 后续阶段

仅在需要时再做：

- retry replay。
- H5 profile 结构化持久化字段。
- 跨任务站点画像。
- 采集等待策略重写。

## 验收标准

第一版完成后，应满足：

1. 稳定业务 DOM 已存在时，不因 `page_settle_complete=false` 直接返回 `insufficient`。
2. `line_loading`、`startup_ad` 仍不允许普通业务规则匹配。
3. 同 URL、同 `content_fingerprint` 不会无限 observe。
4. 底部 Tab、页面 Tab、首卡详情候选都有确定性来源。
5. 高风险动作被阻断，但只读支付/会员页面仍可被识别覆盖。
6. `video_detail` 保持保守，缺少可靠信号时不计入覆盖。
7. 本地完整链路不再因同一稳定页面重复 observe 而触发 `TRANSIENT_PAGE_RECOVERY_EXHAUSTED`。

## 与既有设计的关系

本文档是 `2026-07-21-h5-page-exploration-stability-design.md` 的准确性补充。前一版设计覆盖更完整的稳定化、重试恢复和生命周期方案；本文档收敛第一版落地范围，优先解决第四批样本暴露出的页面识别准确性问题。

