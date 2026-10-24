# Web 未识别页面分析与 dev_2.0 集成记录

## 统计口径

基于七站首轮以及黑料、HL365 追加复测的正式分类证据。同一站优先采用最新一次在线结果；不是本次重新访问站点，也不把模型提议当作已证实页型。最新结果共有 12 个正式 unknown 页面。动作前空白帧、就绪诊断帧、滚动补证和未到达页面不计入 unknown。

## 未识别页面清单

| 站点/路径 | 模型提出类型 | 最终分数 | 未识别原因 | 后续方向 |
|---|---|---:|---|---|
| chigua `/archives/274784/` | unknown | 0.65 | PRIMARY_REGION_CONFLICT：两个独立正文候选区域 | 核对正文与次要区域边界；不能仅按 class 删除其中一块 |
| 91jav1 `/theme/` | video_list | 0.65 | LOW_CONFIDENCE | 核对主题聚合卡片与媒体特征，不直接降低阈值 |
| 91jav1 `/actress/hot` | content_list | 0.65 | LOW_CONFIDENCE | 核对人物目录/列表主体；现有列表类型可能足够 |
| 91jav1 `/article/58281` | unknown | 0.60 | EVIDENCE_INSUFFICIENT | 正文、重复卡片与推荐归属混合，补齐主区域证据 |
| 91jav1 `/my` | unknown | 0.80 | EVIDENCE_INSUFFICIENT | URL 不证明个人中心或登录类型；先核对交互主体与状态 |
| heiliao `/index/search` | search_page | 0.60 | EVIDENCE_INSUFFICIENT：缺 search_trigger_count、query_result_group_count | 区分搜索输入初始页与结果页；先检查可见触发器投影与现有合同对初始态的要求 |
| heiliao `/archives/39668/` | content_detail | 0.65 | STRONG_NEGATIVE_CONFLICT：主体不足、重复卡片冲突 | 正文与促销/推荐区域归属 |
| heiliao `/archives/8148/` | content_detail | 0.65 | STRONG_NEGATIVE_CONFLICT：主体不足、重复卡片冲突 | 同上，使用结构特征而非站点特判 |
| heiliao `/archives/115347/` | video_detail | 0.65 | STRONG_NEGATIVE_CONFLICT：主播放器/唯一主体不足、重复卡片冲突 | 区分真实播放器、封面、内嵌媒体及推荐；检查是否需合法主体曝光补证 |
| heiliao `/archives/115713/` | video_detail | 0.65 | STRONG_NEGATIVE_CONFLICT：主播放器/唯一主体不足、重复卡片冲突 | 同上 |
| heiliao `/djbl/` | content_list | 0.65 | MODEL_NEEDS_REVIEW；列表必要结构条件已满足 | 检查模型复核理由及提示输入一致性，不能仅补卡片数强制通过 |
| hl365 `/` | home | 0.65 | LOW_CONFIDENCE；home 必要结构条件均满足 | 检查模型低置信度原因，不再归因于导航计数为零 |

## 哪些页面类型尚未取得可信覆盖

- `home`：HL365 未通过；黑料首轮首页 unknown 已在追加复测变为 home（0.80）。
- `content_detail` / `video_detail`：黑料的四个内容候选仍冲突；chigua 一处详情候选仍主体冲突。不能用模型提议统计详情成功。
- `content_list` / `video_list`：91jav1 的人物/主题聚合入口，以及黑料 `/djbl/` 尚未正式通过。
- `search_page`：黑料搜索入口未通过；91porna 的搜索路径实际分类为 video_list，不能以路径含 search 算作搜索覆盖。
- 活动/专题类辅助入口：黄果 `/topics/` 实际为 content_list，不能仅按探索意图算活动页覆盖。
- `ad_page`：存在可信成功证据；失败的 21 个广告分支是非公网 DNS 地址拦截，没有到达可分类广告页，不属于未知页型。

91porna 和黄果本轮已探索的业务页面无正式 unknown，但不代表站点全部 URL 均覆盖。AVHub 未通过 readiness，因此尚无正式页型判定；它是采集前置条件失败，不应与模型未识别混为一类。

## 类型与特征的决策

当前证据不支持立即新增页面类型。主要问题是现有类型需要的主体/播放器/搜索事实不足、区域归属冲突，或模型主动复核/低置信度。建议优先级：

1. 正文、推荐和播放器同 Capture 证据归属：覆盖通用混合容器、非正文促销卡片、真实媒体主体，保留反例与现有强冲突保护。
2. 搜索页初始态：证明确有主区域输入和可用触发器后，审视是否应以页面状态表达“未提交查询”，而非要求初始页必须已有结果。先出合同方案再修改。
3. 模型证据一致性与复核理由：结构合同满足但置信度不足，不能通过添加无关特征或降低阈值抹去复核。
4. AVHub 的稳定采样和广告 DNS 属于独立链路，不能靠新增类型修复。

只有独立、重复出现且不能由现有类型加状态表达的主任务页面，才考虑新类型及其正负例、证据合同和权限；本次不实施新的分类功能。

## 证据来源

- [七站修复与探索总报告](2026-09-18-web-seven-sites-repair-and-validation.md)
- [七站逐步证据索引](2026-09-19-web-seven-sites-step-by-step-evidence.md)
- [两站追加复测证据](2026-09-19-web-two-sites-followup-evidence.md)
- 本地原始审计：`outputs/web-seven-sites-repairs-20260918/live/classification-audit.json` 与 `followup/classification-audit.json`。

## 提交与合并范围

提交现有 Web 探索源码、测试、规则及报告，包含此前积累的共用采集器 Web 开关隔离改动和迁移 `20260918_0124`；不新增 App/H5 文件修改，不需要前端菜单/路由同步。`outputs` 下原始截图、DOM、运行状态和构建缓存留在本地，不作为源码提交。

集成方向为远程 `origin/dev_2.0` → 当前 `codex/web-v2-home-menu-ad-dedup`；不反向合并、不推送。此前完整门禁为 603 passed、0 skipped；本次合并后的检查结果以 Git 操作完成回执为准，不宣称重新在线验证七站。

### 本次集成验证

- Web 源码与报告提交：`0fdd3a74c330f4ce1e895d8bc718e826fdc18319`，46 个文件，包含本页未识别清单。
- 拉取远程 `dev_2.0` 至 `24441e45f124748484bf61729bad9a3ddc324c75`，新增 6 个上游提交。规则文档仅一处内容冲突，保留两侧新增章节；App/H5 专属源码与上游完全一致，没有手工调整。
- 集成审查发现上游 `read_web_before_action_binding` 的 SQL 混入 Python `#` 注释。隔离测试库只读 prepare 实际 SQL，重现 `PostgresSyntaxError / 42601`；仅改为 SQL `--` 注释，不改变查询条件、公共 Capture metadata 来源或身份校验。新增 Web 专属静态和真实 PostgreSQL 回归，未修改 App/H5 分支。
- 合并后扩大门禁 **625 passed、0 failed、0 skipped**，耗时 130.16 秒（3 条环境警告），包括实际 SQL 编译、真实 PostgreSQL、Playwright、App/H5 判定适配器隔离及新增 URL 身份回归。回执：`outputs/web-seven-sites-repairs-20260918/gate-dev-merge.xml`。这不是全仓库全量测试，也不是重新在线采集七站。
- Ruff 与差异检查通过。Git 自动维护曾报告历史 tree `43fdd19a6275c4fff10c0d2ed3af1c68524633f7` 异常；提交与 fetch 成功，当前 HEAD 和 `origin/dev_2.0` 的 `fsck --connectivity-only --no-reflogs --no-dangling` 检查通过。后续 Git 命令仅临时禁用自动维护，未删除对象、清理历史或修改全局配置；历史自动维护异常仍需独立排查。
