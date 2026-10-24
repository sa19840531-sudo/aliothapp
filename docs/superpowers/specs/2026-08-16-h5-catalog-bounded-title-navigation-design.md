# H5 目录边界、帖子标题导航与安全候选兜底设计

日期：2026-08-16  
范围：H5 V2 页面探索、列表详情导航、Explorer 决策容错  
状态：待用户审阅

## 1. 背景与已确认问题

真实任务已经证明三个独立阻断：

1. `post_list /square` 的首卡能力签发 `.dynamicData-box:visible`，浏览器解析器最终命中父级 `dynamicData-content`。动作审计显示点击成功，但 URL、页面类型与页面状态均未变化。真实 DOM 中可进入详情的结构是 `.dynamicData-box > .dynamicData-content > .title`。
2. `content_list /ai` 上只剩一个平台签发的底栏“我的”候选时，模型错误返回非完成终态。共享 Explorer 将该语义偏差抛为 `ExplorerAgentOutputError`，导致整个 H5 Run 以 `H5_EXPLORATION_TURN_FAILED` 终止。
3. 当前可选动作可能引用 `page_type_catalog.json` 之外的预期页面类型；现有可选引用清洗会把未知 `expected_next_page_type` 改成空值，却仍保留动作。若动作进入未知链接，主页面类型目录校验可能把识别错误升级为 Run 失败，违背“未知链接跳过并继续”的要求。

## 2. 目标与非目标

### 2.1 目标

- `post_list` 只通过首条帖子的真实标题进入规范 `content_detail`。
- 帖子详情沿用视频详情的业务子页策略：处理可信关闭能力或 `z-index > 500` 的遮挡；无可信弹窗时同会话 `back`，验证返回 `/square` 后继续 sibling。
- H5 模型错误提前终止时，只要当前存在唯一、平台已签发且未阻断的安全候选，就确定性执行该候选，不让单次模型语义偏差终止 Run。
- 当前页面或导航目标不能映射到 `page_type_catalog.json` 时，记录跳过事实并继续其他目录内分支，不进入覆盖率、不执行规则、不伪造成已支持页面。
- 覆盖率只统计“用户目标类型与当前目录类型的交集”。当前目录没有 `ranking`，因此本轮必须明确报告为 `uncatalogued/skipped`，不能伪造覆盖或私自扩展目录。

### 2.2 非目标

- 不在本专项中新增 `ranking` 页面类型。新增目录类型需要独立定义实体语义、识别信号、转移关系和规则适用性。
- 不把任意标题、任意 `.title` 或任意链接开放为可执行 selector。
- 不放宽 high/critical 风险动作，不自动确认支付、发帖、关注或其他业务副作用。
- 不用视觉坐标代替已有的帖子结构能力。

## 3. 方案比较与选择

### 3.1 帖子详情入口

- **选择：精确标题结构能力。** 检测器只对 `post_list` 的 `.dynamicData-box .title` 签发列表详情能力；UI Snapshot 显式保留该受控节点；浏览器解析器按 selector 对应的精确 class token 选择首个可见、中心可命中的标题。
- 备选：继续点击整卡。真实动作已证明无导航效果，拒绝。
- 备选：截图坐标点击标题。对列表滚动、文案高度和响应式布局敏感，且没有必要，拒绝。

### 3.2 模型提前终止

- **选择：H5 专属、类型化的确定性兜底。** 共享 Explorer 用专门异常表示“有候选却提前终止”；H5 包装器只在唯一 selected entry、唯一当前候选、候选未阻断、风险为 low/medium 时，从该持久化候选构造 ActionIntent，并继续通过完整 H5 参数验签。
- 备选：额外请求模型一次。增加时延与费用，且相同输入仍可能重复失败，拒绝作为唯一兜底。
- 备选：把 Work 标为 bounded/review。会丢失后续目录内安全分支，不满足持续探索要求。

### 3.3 未编目链接

- **选择：候选准入与落地页双门禁。** 已知目标在 Frontier 准入前过滤；重定向或无法预知的落地页在识别后标记跳过并恢复父页。两层均以同一 PageTypeRegistry 为权威。
- 备选：遇到未知类型立即失败。会让站点新增链接阻断全部目录内覆盖，拒绝。
- 备选：把未知类型强制映射为 `home/content_detail`。会污染覆盖与规则结果，拒绝。

## 4. 架构与组件边界

### 4.1 PageTypeRegistry 是唯一目录权威

所有页面类型、预期下一类型和覆盖统计继续从 `services/api/app/domain/page/page_type_catalog.json` 加载。禁止在 H5 Runtime 中复制第二份类型集合。

候选准入规则：

- 候选显式携带 `expected_next_page_type` 时，必须能规范化为目录 code；无法规范化时整条候选丢弃，而不是只清空字段。
- 会产生页面导航的结构候选必须有目录内预期类型。普通 popup/back/observe 等同页或恢复能力不受此条影响。
- 风险审计增加稳定原因 `uncatalogued_expected_page_type_dropped`，不得记录页面正文或敏感 URL 参数。

### 4.2 未编目当前页的有界收口

模型主类型不在目录且确定性结构、路由别名也无法映射时，页面识别返回“未编目跳过”结果，而不是技术错误：

- `detection_status=unrecognized`；
- `page_type=null`；
- `candidate_actions=[]`；
- 元数据写入 `catalog_disposition=skipped` 和 `catalog_skip_reason=uncatalogued_page_type`；
- 不写入 covered page types，不进入 Assessment、Rule Judge 或报告页面分母。

若该页由已选导航候选进入，任务 Frontier 将该 entry 记为 `skipped/uncatalogued_page_type`。存在保存的 history parent 时优先同会话 `back` 并重新 Capture/Detect/Assess；没有父页时选择其他任务级 pending sibling。未知页不得声明 completed，也不得让整个 Run 失败。

### 4.3 帖子标题能力

列表详情合同新增 `post_list` 精确 selector：

```text
.dynamicData-box:visible .title:visible
```

UI Snapshot 的 regularCandidates 只增加该结构的精确后代 selector，不扩展为通用 `[class~='title']`。Snapshot 元素匹配从“页面类型 token 集合”收紧为“签发 selector 对应 token 集合”：帖子标题 selector 只能匹配 `title`，不能退回 `dynamicData-content`。

领域语义仍使用统一的“当前列表首个内容卡片”，执行参数保存精确标题 selector、来源 `post_list` 和预期 `content_detail`。浏览器只执行当前 Snapshot 重新解析出的首个可见、可命中标题 selector。

### 4.4 帖子详情生命周期

标题动作进入 `content_detail` 后：

1. 保存 `history_parent_state_version=/square 来源状态` 与 `history_parent_location=/square`。
2. 有可信 Dialog 关闭能力或遮挡节点最高 `z-index > 500` 时，按业务页最多 20 轮逐层处理。
3. 只有低层容器、内联关闭文本或没有可信弹窗时，不执行关闭候选。
4. 当前详情候选清空后，优先选择保存的父页 sibling，执行正深度 `back`。
5. 返回 `/square` 并识别为稳定 `post_list` 后继续下一分支；首次 back 校验失败才允许受控 navigate，恢复总上限保持 2 次。

### 4.5 唯一安全候选兜底

共享 Explorer 新增专门异常类型，仅表示“模型在仍有 eligible candidates 时返回非完成终态”。H5 包装器捕获后必须同时满足：

- H5 Task Frontier 快照存在且只有一个 selected entry；
- selected entry 与 `work.frontier.candidates` 中唯一候选完全相等；
- selected entry ID 与 Observation 绑定字段相等；
- 候选不在 blocked 集合，风险仅 low/medium；
- 动作类型属于 H5 boundaries，预算和总 deadline 尚未耗尽。

满足后使用持久化候选构造唯一 ActionIntent。purpose、参数、视口 `390×844`、Capture profile、source capture、deadline 均由平台派生，最后调用现有 `_validate_h5_selected_candidate_binding`。任一条件不满足继续失败关闭，禁止从多个候选自行猜选，也禁止执行 high/critical。

## 5. 数据流

```text
Capture
  → Detect（PageTypeRegistry 校验）
  → 当前页未编目？
      → 记录 skipped → 标记来源 entry skipped → back/下一 sibling
  → 目录内页面
      → 生成候选
      → 预期类型未编目？丢弃候选并审计
      → Task Frontier 选择唯一候选
      → Explorer 决策
          → 合法 ActionIntent：正常执行
          → 错误提前终止：H5 唯一安全候选兜底
      → before/action/after Capture
      → 详情页按弹窗规则处理
      → back 验证来源页
```

## 6. 错误处理与审计

- 未编目页面是业务范围跳过，不是 `H5_EXPLORATION_TURN_FAILED`。
- 目录 JSON 无法加载、目录自身非法或 Registry 不一致仍是部署错误，必须失败关闭。
- 唯一候选兜底在动作审计中写明 `decision_source=platform_selected_candidate_fallback` 和原始错误类型，不保存模型原文。
- 标题动作成功但未产生 `content_detail` 时，按现有 no-progress/候选失败语义记录，不伪报详情覆盖；后续可继续其他 sibling。
- `ranking` 当前不在目录，最终报告列为 `uncatalogued/skipped`。可达覆盖比例以用户目标与目录交集为分母，并同时列出原始 13 类型的逐项状态。

## 7. 测试策略

### 7.1 RED 测试

- 真实 `dynamicData-box/dynamicData-content/title` DOM 必须签发标题 selector；旧实现应仍签发整卡而失败。
- UI Snapshot 必须包含精确帖子标题节点；旧实现不包含该普通 div。
- Resolver 必须选择 `title`，并拒绝父 `dynamicData-content`、其他 `.title` 与越界/不可命中标题。
- 模型在唯一 H5 selected candidate 存在时返回 bounded/blocked，旧实现应抛错；新实现应生成并通过绑定校验的 ActionIntent。
- 多候选、blocked、high/critical、selected 身份不一致时不得兜底。
- 未编目 `expected_next_page_type` 必须删除整条导航候选；旧实现仅清空字段。
- 未编目当前页必须生成 skipped 事实并恢复父页，不得失败 Run 或加入覆盖。

### 7.2 回归与实跑

- 运行页面识别、H5 Observation/Workflow/Runtime、MAF Explorer、浏览器 Snapshot/Resolver、任务 Frontier 和 Work 仓储相关测试。
- Ruff 与 `git diff --check` 必须通过。
- Docker 只执行 restart，不 build；重启前后 API/Worker 镜像 ID 必须一致。
- 使用 admin 自动登录、提交用户给定 `/api/v2/tasks` 请求。
- 实跑逐项记录：页面类型、URL、动作、before/after Capture、popup 轮次、back 恢复、Work 终态、向量检索尝试/命中/静态命中/降级次数。
- 最终按 13 个用户目标分别列出 `covered / not_reached / uncatalogued / not_observed`，不为达到 90% 伪造站点不存在或目录未定义的页面。

## 8. 验收标准

- `post_list → 标题 → content_detail → 弹窗判断 → back → post_list` 完整通过。
- 模型一次错误提前终止不会阻断唯一已验签安全候选。
- 未编目候选或落地页被跳过且其他目录内 sibling 继续探索。
- `profile → activity/settings/payment` 均遵循 back 后继续 sibling。
- `video_list → video_detail` 保持已有低层容器直接 back 规则。
- 总时长仍为 3600 秒，单动作无独立固定时限，H5 视口仍为 `390×844`。
- 最终覆盖报告同时给出目录内覆盖比例、原始目标逐项状态和向量库使用证据。
