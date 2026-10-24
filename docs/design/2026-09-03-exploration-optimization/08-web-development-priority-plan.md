# WEB 探索与质检优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在 `dev_2.0` 现有基础上完成 WEB 页面就绪、精准交互、页面识别、导航发现、探索 Skill、报告可见性和六站回归闭环，使每个采集页面都有可追溯的类型判定与质检结论，同一逻辑链接不重复采集和进入质检时间线。

**架构：** 保留 FastAPI 分层单体与现有 Web V2 工作流，以 Capture 为不可变观察事实，以页面状态图作为事实投影，以统一就绪判定、交互后置条件、类型指南和证据 Gate 约束 Explorer、Judge 与报告。URL 白名单只提供辅助先验，不代替当前 HTML、截图和运行时到达证据。

**技术栈：** Python 3.12、FastAPI、PostgreSQL、Playwright/Playwright CLI、Microsoft Agent Framework、Vite、Vue 3、TypeScript、Vitest。

**设计依据：** [探索优化总览](README.md)、[受控探索](01-controlled-exploration.md)、[页面状态图](02-page-state-graph.md)、[精准补采](03-targeted-recapture.md)、[页面识别](04-page-recognition.md)、[截图就绪](05-capture-readiness.md)、[交互精准度](06-interaction-precision.md)、[可信证据](07-trusted-evidence.md)。

**排期依据：** `C:/Users/p/Downloads/Telegram Desktop/玉衡项目进度排期 (1).xlsx` 的“9月任务拆分”工作表，第 7、15、17、18、27、33、56 行。

**代码基线：** `dev_2.0`，基线提交 `6c4129b5443511f7be47f330ed4857fefef64030`。

## 全局约束

- 页面身份、页面类型、产品能力、可见层和证据角色必须分别建模，禁止用一个 `page_type` 同时表达多种语义。
- 页面类型只使用 `video_list`、`video_detail`、`content_list`、`content_detail`；首页、弹窗、广告和人工验证不是第五种业务页面类型。
- 裸域名或规范化根路径可以标记为入口/首页角色，但必须在顶层遮挡处理和页面稳定后再判断业务页面类型。
- URL、标题、菜单名称和站点白名单只作为辅助信号；HTML 结构、可见内容、播放器/正文/重复卡片结构和可信截图是主要判定依据。
- 无法满足必要证据时输出 `unknown`、`conflict` 或 `needs_review`，不得为了提高覆盖率强制猜测类型。
- 候选入口不等于页面到达；命令成功不等于交互生效；交互生效不等于后置条件满足；后置条件满足后仍需证明目标页面到达。
- 同一规范化逻辑 URL 可以保留多个发现来源，但只能执行一次正式页面采集、一次正式分类和一条质检时间线主记录。
- 重复 URL 不重新截图、不重新生成证据、不重复通知 Judge；如果需要重新观察，必须以明确的追加观察或定向补采身份记录。
- 18+ 年龄确认只允许在任务已有成年操作人声明、控件语义唯一且不是登录/注册/支付/协议授权时执行。
- 广告较多页面先保存广告遮挡事实，再有界滚动到可信非广告业务区域采集；不得删除 DOM、隐藏广告或伪造无广告页面作为正式证据。
- JavaScript 强制点击只能用于诊断，不能形成自然交互通过结论。
- Explorer 负责观察和提出有界动作，服务端运行时负责权限、预算、租约和风险控制，Judge 不直接操作浏览器。
- 所有正式证据必须通过完整性、来源、时序、语义/可见层一致性和规则充分性校验。
- 预览报告在存在有效采集或明确阻断事实时必须可查看；只有 Completion Gate 通过后才标记为正式报告并允许正式导出。
- 首轮实现复用现有 JSON/快照扩展字段，不引入数据库迁移；如果实现中发现必须新增稳定关系字段，应独立设计迁移并单独评审。
- 所有新增注释使用中文，所有文件保持 UTF-8。
- 本计划文件当前只创建、不提交；执行阶段的每次提交必须以 `[AI编程]` 开头，并说明模块、数据库迁移、前端路由/菜单同步和未覆盖测试风险。

---

## 1. 青云客任务与优先级

| 顺序 | 优先级 | Excel 任务 | 主责工时 | 协作者 | 当前判断 |
| --- | --- | --- | ---: | --- | --- |
| 1 | P0 | 完善 WEB 页面加载完成判断 | 3 人天 | 天枢 1 人天 | 底层信号已存在，应用层语义不完整 |
| 2 | P0 | 提升 WEB 点击与滑动准确性 | 3 人天 | 天枢 1 人天 | 点击基础较强，滑动、重绑和结果分层不完整 |
| 3 | P1 | 完善页面类型识别说明 | 3 人天 | 星语 3 人天 | 已有四类目录，缺机器可执行的正反证与混淆规则 |
| 4 | P1 | 实现产品形态和入口页面识别 | 2 人天 | 立川 0.5 人天 | 白名单适用性已存在，运行时产品能力识别缺失 |
| 5 | P1 | 完善 WEB 导航入口发现 | 2 人天 | 无 | 目前主要覆盖 `a[href]`，语义按钮和 SPA 入口不足 |
| 6 | P2 | WEB 探索 Skill | 2 人天 | 观星 | 尚无专用 Web Explorer Skill，应在运行时合同稳定后编写 |
| 7 | P2 | WEB 完整流程测试负责人 | 未估算 | 立川 | 有历史六站实测，仍需按本计划重新验收 |

Excel 明确工时合计为青云客 15 人天，不含集成、真实站点波动、完整流程测试和问题回收。建议按 20～25 人天预留，测试从 P0 开始持续执行，最终六站验收放在所有功能任务之后。

## 2. `dev_2.0` 当前能力盘点

### 2.1 已经具备，保留并做回归

- 页面类型目录已收敛为四种规范代码，不需要恢复旧映射。
- 六站菜单白名单已写入 `web_site_menu_catalog.json`，并带页面分类提示。
- 菜单采集默认上限已经是 10，兼容历史上限 5。
- 裸域名、HTTP/HTTPS 跳转和根路径已有规范化逻辑，历史六站复测中重复规范 URL 为 0。
- 年龄确认提示已包含成年操作人声明和 18+ 入口约束。
- Playwright CLI 点击已有唯一性、可见性、actionability、危险文本、同源和节点身份校验。
- Web Capture 已投影到页面状态图，报告支持 `preview` 与 `final` 阶段。
- 工作台任务列表和任务详情已有报告入口代码，报告中心已有预览状态展示。

### 2.2 部分具备，需要本轮补齐

- `page_capture.py` 已采集 DOM 安静、加载器、媒体和关键请求信号，但 `web_page_readiness.py` 仍以固定等待和少量终态为主。
- `web_page_classification.py` 已使用结构事实和辅助提示，但目录缺少必要条件、强否定条件、易混淆类型和补采策略。
- `web_desktop_navigation.py` 主要解析同源 `a[href]`，没有完整覆盖无 `href` 菜单、事件控件、SPA 路由和视觉卡片。
- 点击前验证较完整，但滚动不是目标导向，未把“滚动→重新观察→重新绑定→点击”固化为一个有界计划。
- 报告 API、页面和入口均存在，但仍需验证所有 Web V2 preview/final/blocked 状态下按钮是否稳定可见、查询参数是否正确、500 是否被转换为可读诊断。

### 2.3 本轮保持关闭

- 正式规则驱动的 Web 主动交互补采继续由 `web_active_interaction_boundary.py` 阻断。
- 只有页面图、就绪判定、交互后置条件和可信证据 Gate 同时验收通过，才能另行启用定向补采。

## 3. 关键路径

```text
冻结验收基线
  → P0 页面就绪判定
  → P0 点击/滑动与后置条件
  → 可信证据及页面图联调门禁
  → P1 页面类型指南
  → P1 产品能力、入口页和可见层识别
  → P1 导航入口发现
  → P2 WEB Explorer Skill
  → 工作台报告可见性验收
  → 六站完整回归
```

页面类型指南可以在 P0 开发期间由星语并行准备，但不能在页面就绪门禁完成前接管正式分类。页面状态图和可信证据由其他负责人主导，青云客负责 Web 接入和验收，不在 Web 模块内重建第二套图或证据权威。

## 4. 文件职责规划

| 文件 | 计划职责 |
| --- | --- |
| `services/api/app/infrastructure/browser/page_capture.py` | 采集原始就绪信号、身份前后快照、可见区域和网络能力事实 |
| `services/api/app/application/web_page_readiness.py` | 统一 Web Settle Oracle、证据角色、终态和阻断原因 |
| `services/api/app/application/web_runtime_composition.py` | 把就绪、识别、导航、证据和报告串入 Web V2 正式运行时 |
| `services/api/app/infrastructure/browser/playwright_cli_session.py` | 点击/滚动执行、目标重绑、命中检查和动作回执 |
| `services/api/app/application/web_list_detail_navigation.py` | 列表到详情的有界交互计划和到达后置条件 |
| `services/api/app/application/web_menu_exploration.py` | 菜单计划、终态、规范 URL 去重和一次正式采集约束 |
| `services/api/app/application/web_desktop_navigation.py` | DOM/语义/SPA 导航候选提取与候选身份生成 |
| `services/api/app/domain/page/web_page_type_catalog.json` | 四种页面类型的版本化识别指南 |
| `services/api/app/domain/page/web_page_catalog.py` | 类型指南加载、结构校验和版本冻结 |
| `services/api/app/application/web_page_classification.py` | 结构证据、指南、辅助提示和冲突判定 |
| `services/api/app/application/web_page_applicability.py` | 规则适用性，不承担运行时产品能力识别 |
| `services/api/app/application/web_product_capability_assessment.py` | 新增：产品能力、入口状态和可见层评估模型 |
| `services/api/app/agents/explorer/web_assessment_agent.py` | 只读 AI 识别提示、严格 JSON 结果和成年入口语义 |
| `services/api/app/agents/skills/web-explorer-workflow/SKILL.md` | 新增：Web Explorer 观察与有界操作方法，不定义权限 |
| `services/api/app/api/v1/workstation/reports.py` | 报告 preview/final/blocked 查询与错误映射 |
| `services/api/app/api/v1/workstation/task_insight.py` | 任务详情的报告可用性、页面图和阻断摘要 |
| `app/workstation/src/modules/tasks/pages/TaskListView.vue` | 任务列表报告按钮及预览文案 |
| `app/workstation/src/modules/tasks/pages/TaskDetailView.vue` | 任务详情报告按钮及阻断提示 |
| `app/workstation/src/modules/reports/pages/ReportCenterView.vue` | 报告列表、任务定位和 preview/final 阶段展示 |
| `app/workstation/src/modules/reports/pages/ReportDetailView.vue` | 报告正文、证据缺口、页面时间线和 Gate 状态 |
| `scripts/web_v2_six_site_shadow.py` | 六站离线固定样本回归 |
| `scripts/web_v2_live_evidence.py` | 六站真实运行证据采集与结果汇总 |

## 5. 分阶段实施任务

### Task 0：冻结 WEB 验收基线

**文件：**

- 修改：`services/api/tests/test_web_six_site_shadow_fixtures.py`
- 修改：`services/api/tests/test_web_v2_formal_report_e2e.py`
- 修改：`scripts/web_v2_six_site_shadow.py`
- 参考：`docs/reports/2026-09-04-web-v2-six-site-live-retest-summary.md`

**产出接口：** 六站固定事实集、规范 URL 预期、四类页面预期、阻断状态、报告阶段和菜单上限 10 的可执行回归基线。

- [ ] **Step 1：为已完成能力补字符化测试**

  增加裸域名首页角色、HTTP→HTTPS 合并、四类规范代码、18+ 入口约束、菜单上限 10、规范 URL 去重和 preview 报告入口用例。

- [ ] **Step 2：运行基线测试并记录实际差异**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_menu_limit_ten_migration.py services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_web_v2_formal_report_e2e.py -q`

  Expected: 当前已具备能力全部 PASS；新增但尚未实现的就绪、交互和识别断言按任务边界分别失败，不用放宽断言掩盖缺口。

- [ ] **Step 3：冻结六站验收矩阵**

  每站固定入口 URL、允许重定向、所需页面类型、最多 10 个菜单、广告/弹窗预期、详情目标、报告预期和允许的人工阻断原因。

- [ ] **Step 4：执行阶段提交**

  Commit message: `[AI编程] test(web): 冻结六站探索质检验收基线；无数据库迁移；无前端菜单路由变更；真实站点动态内容留待现场回归`

### Task 1：统一 WEB 页面就绪判定

**文件：**

- 修改：`services/api/app/infrastructure/browser/page_capture.py`
- 修改：`services/api/app/application/web_page_readiness.py`
- 修改：`services/api/app/application/web_runtime_composition.py`
- 修改：`services/api/app/application/web_primary_content_capture.py`
- 测试：`services/api/tests/test_web_page_readiness.py`
- 测试：`services/api/tests/test_web_primary_content_capture.py`
- 测试：`services/api/tests/test_web_runtime_composition.py`

**接口：**

- 产生：`WebReadinessStatus`，取值为 `stable`、`loading`、`transitional`、`timeout`、`identity_drift`、`artifact_skew`、`capture_failed`、`popup_blocked`、`human_verification_blocked`。
- 产生：`WebEvidenceRole`，至少包含 `entry_observation`、`classification`、`inspection`、`interaction_before`、`interaction_after`、`report_preview`。
- 扩展：`WebPageReadinessResult`，保留兼容字段并增加 `status`、`role`、`signals`、`missing_signals`、`identity_before`、`identity_after` 和 `network_capability`。
- 消费：`page_capture.py` 已有 DOM、loader、media、critical request 和 viewport 事实。

- [ ] **Step 1：先写终态与证据角色失败测试**

  覆盖固定静默但 loader 未消失、关键请求能力未知、核心列表未达法定数量、身份前后漂移、截图/DOM 不同 epoch、稳定业务帧、弹窗阻断和人工验证阻断。

- [ ] **Step 2：确认新增测试失败**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_readiness.py -q`

  Expected: FAIL，原因应是缺少新增状态或当前固定等待错误接受了过渡帧。

- [ ] **Step 3：从底层采集事实构造统一判定**

  固定顺序：身份前检 → 可见层 → 核心结构 → 连续样本 → 按角色要求检查网络/媒体 → 正式采集 → 身份后检。网络不可观测必须记录 `unknown`，不能写成零请求。

- [ ] **Step 4：替换应用层固定等待主判据**

  `STABLE_WAIT_SECONDS` 只保留为采样间隔或兼容等待，不能单独产生 `stable`。所有退出必须返回显式终态和原因。

- [ ] **Step 5：接入主内容采集和正式运行时**

  分类只能消费 `classification` 角色通过的帧；规则质检只能消费 `inspection` 角色通过的帧；预览报告允许消费可信阻断事实。

- [ ] **Step 6：运行就绪回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_readiness.py services/api/tests/test_web_primary_content_capture.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_capture_runtime.py -q -k "readiness or primary_content or identity or advertisement"`

  Expected: PASS，固定等待不能单独使加载页通过，身份漂移和证据错帧均失败关闭。

- [ ] **Step 7：执行阶段提交**

  Commit message: `[AI编程] feat(web): 统一页面就绪与证据角色判定；无数据库迁移；前端无需菜单路由同步；网络不可观测与动态媒体仍按显式降级验收`

### Task 2：提升点击、滑动和到达验证准确性

**文件：**

- 修改：`services/api/app/infrastructure/browser/playwright_cli_session.py`
- 修改：`services/api/app/application/web_list_detail_navigation.py`
- 修改：`services/api/app/application/web_menu_exploration.py`
- 修改：`services/api/app/application/web_runtime_composition.py`
- 测试：`services/api/tests/test_web_list_detail_navigation.py`
- 测试：`services/api/tests/test_web_menu_exploration.py`
- 测试：`services/api/tests/test_playwright_cli_runtime.py`

**接口：**

- 产生：`WebInteractionPlan`，字段为 `intent`、`source_capture_id`、`source_epoch`、`target_ref`、`max_scroll_steps`、`deadline_seconds`、`postcondition`。
- 产生：`WebInteractionOutcome`，分别记录 `command_accepted`、`effect_observed`、`postcondition_satisfied`、`transition_arrived`、`after_capture_id` 和 `failure_reason`。
- 约束：滚动后旧元素引用失效，点击前必须基于新 Capture 重新绑定。

- [ ] **Step 1：先写滚动重绑和四层结果测试**

  覆盖目标首屏可见、目标在折叠下方、滚动后目标消失、重复标签、遮挡、跨域跳转、命令成功但无效果、URL 未变但详情内容到达和外跳后回源。

- [ ] **Step 2：确认新增测试失败**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_playwright_cli_runtime.py -q -k "scroll or rebind or postcondition or arrival"`

- [ ] **Step 3：实现目标导向有界滚动**

  每次只滚动一个受控步长；每步后采集新观察；比较目标锚点、内容变化和滚动边界；达到最大步数、deadline 或无位移时明确终结。

- [ ] **Step 4：实现新 epoch 重绑定**

  点击前重新检查目标唯一、可见、enabled、命中测试通过且未被遮挡；旧 selector、元素句柄和坐标不能跨 epoch 复用。

- [ ] **Step 5：拆分动作回执与页面到达**

  只有 `transition_arrived=True` 才能建立可信页面图边和触发正式页面采集；其他三层结果仅作诊断或失败证据。

- [ ] **Step 6：验证 JS 诊断隔离**

  JS click、force click 或 DOM 改写产生的结果标记为 `diagnostic`，不得进入自然点击通过率、正式质检结果或业务覆盖率。

- [ ] **Step 7：运行交互回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_playwright_cli_runtime.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_page_graph_projection.py -q`

  Expected: PASS，滚动后必须重绑，命令成功但无到达不能新增可信转场。

- [ ] **Step 8：执行阶段提交**

  Commit message: `[AI编程] feat(web): 增加目标导向滚动重绑和到达后置条件；无数据库迁移；前端无需菜单路由同步；跨域外跳只保留有界恢复测试`

### Task 3：接通页面图与可信证据门禁

**文件：**

- 修改：`services/api/app/infrastructure/db/page_capture_repository.py`
- 修改：`services/api/app/application/page_graph_projection.py`
- 修改：`services/api/app/application/web_runtime_evidence.py`
- 修改：`services/api/app/application/web_evidence_bundle.py`
- 修改：`services/api/app/application/web_evidence_exit_gate.py`
- 测试：`services/api/tests/test_page_graph_projection.py`
- 测试：`services/api/tests/test_web_runtime_evidence.py`
- 测试：`services/api/tests/test_web_evidence_bundle.py`
- 测试：`services/api/tests/test_web_evidence_exit_gate.py`

**产出接口：** 只有可信到达后的 Capture 投影业务页面节点；候选、失败动作、弹窗层和外部广告落地按独立事实类型保存；正式证据按角色进入 Manifest/InputSet。

- [ ] **Step 1：为非可信到达写投影拒绝测试**
- [ ] **Step 2：为弹窗、外部广告和重复 URL 写分流测试**
- [ ] **Step 3：为同哈希不同观察写追加语义测试**
- [ ] **Step 4：统一所有 Web Writer 的证据校验入口**
- [ ] **Step 5：运行页面图和证据门禁回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_page_graph_projection.py services/api/tests/test_page_graph_progress.py services/api/tests/test_web_runtime_evidence.py services/api/tests/test_web_evidence_bundle.py services/api/tests/test_web_evidence_exit_gate.py -q`

  Expected: PASS，候选不计到达，重复逻辑 URL 不产生第二条正式时间线，存储去重不删除观察语义。

- [ ] **Step 6：执行阶段提交**

  Commit message: `[AI编程] fix(web): 收紧页面图投影与可信证据门禁；无数据库迁移；前端无需菜单路由同步；需与立川证据验收同步复核`

### Task 4：建立四类页面的版本化识别指南

**文件：**

- 修改：`services/api/app/domain/page/web_page_type_catalog.json`
- 修改：`services/api/app/domain/page/web_page_catalog.py`
- 修改：`services/api/app/application/web_page_classification.py`
- 修改：`services/api/app/application/web_page_type_assessment.py`
- 修改：`services/api/app/agents/explorer/web_assessment_agent.py`
- 测试：`services/api/tests/test_web_page_type_catalog.py`
- 测试：`services/api/tests/test_web_page_classification.py`
- 测试：`services/api/tests/test_web_page_type_assessment.py`
- 测试：`services/api/tests/test_web_exploration_assessment_agent.py`

**接口：**

- 每种类型定义 `purpose`、`required_positive_signals`、`supporting_signals`、`strong_negative_signals`、`confusable_with` 和 `missing_evidence_actions`。
- `WebPageAssessment` 保留 `page_type`，并明确 `confidence`、`reason_codes`、`conflicts`、`needs_review` 和 `guide_version`。
- 白名单提示继续通过 `WebPageClassificationHint` 输入，但权重低于当前页面结构事实。

- [ ] **Step 1：先写目录结构和四类边界测试**

  列表页必须体现重复同类条目；详情页必须体现单主体正文或播放器；视频与内容的区分以主承载形式为准；首页综合页只有满足某一类业务结构时才归入四类，否则保持未知。

- [ ] **Step 2：增加混淆对和强否定测试**

  覆盖视频缩略图文章列表、带大量推荐卡片的视频详情、图文详情内嵌视频、搜索/标签页、广告聚合页、仅导航首页和加载骨架屏。

- [ ] **Step 3：确认新增测试失败**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_type_assessment.py -q`

- [ ] **Step 4：扩展目录加载和严格校验**

  四种规范代码保持不变；缺少任何指南字段时启动失败；版本写入每次分类结果，运行中的任务冻结同一版本。

- [ ] **Step 5：调整分类器证据优先级**

  当前 DOM/可见结构为主证据，截图理解为受控补充，URL/菜单/白名单为辅助；强否定冲突时输出 `needs_review`，不得由白名单强行覆盖。

- [ ] **Step 6：运行识别回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_six_site_shadow_fixtures.py -q`

  Expected: PASS，四类代码不增加别名，低证据和强冲突样本稳定进入复核。

- [ ] **Step 7：执行阶段提交**

  Commit message: `[AI编程] feat(web): 增加四类页面版本化识别指南；无数据库迁移；前端无需菜单路由同步；视觉边界样本需六站现场复核`

### Task 5：识别产品能力、入口状态与当前可见层

**文件：**

- 新增：`services/api/app/application/web_product_capability_assessment.py`
- 修改：`services/api/app/application/web_page_applicability.py`
- 修改：`services/api/app/application/web_runtime_composition.py`
- 修改：`services/api/app/agents/explorer/web_assessment_agent.py`
- 修改：`services/api/app/application/task_insight.py`
- 测试：`services/api/tests/test_web_product_capability_assessment.py`
- 测试：`services/api/tests/test_web_page_applicability.py`
- 测试：`services/api/tests/test_web_runtime_composition.py`
- 测试：`services/api/tests/test_task_insight_service.py`

**接口：**

- 产生：`WebProductCapabilityAssessment`，能力集合为 `navigation`、`content`、`video`、`advertisement`、`transaction`、`mixed`。
- 产生：`WebEntryStatus`，取值为 `known`、`unknown`、`blocked`、`confirmed_absent`。
- 产生：`WebVisibleLayer`，取值为 `business`、`age_gate`、`popup`、`advertisement`、`human_verification`。
- 规则：产品能力评估允许修订并追加记录，但不能拆分页面身份，也不能缩小任务启动时冻结的必检分母。

- [ ] **Step 1：先写入口层与产品能力测试**

  覆盖裸域名业务首页、18+ 层、普通公告、广告遮挡、验证码、纯导航入口、视频站、内容站、混合站和支付入口。

- [ ] **Step 2：确认新增测试失败**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_product_capability_assessment.py services/api/tests/test_web_page_applicability.py -q`

- [ ] **Step 3：实现独立评估模型**

  先输出当前可见层，再输出入口状态和产品能力；底页不可见时允许未知，不根据域名或历史白名单猜测被遮挡的首页。

- [ ] **Step 4：接入规则适用性与任务洞察**

  `web_page_applicability.py` 继续负责规则范围，消费已冻结的产品能力修订；未知不会自动变成不适用，冲突会形成复核原因。

- [ ] **Step 5：运行产品识别回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_product_capability_assessment.py services/api/tests/test_web_page_applicability.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_task_insight_service.py -q`

  Expected: PASS，遮挡层不被当首页，裸域名在稳定业务帧后保留首页角色。

- [ ] **Step 6：执行阶段提交**

  Commit message: `[AI编程] feat(web): 增加产品能力入口状态和可见层识别；无数据库迁移；任务详情只同步读模型无菜单路由变更；未知能力保持复核`

### Task 6：扩展 WEB 导航入口发现

**文件：**

- 修改：`services/api/app/application/web_desktop_navigation.py`
- 修改：`services/api/app/infrastructure/browser/page_capture.py`
- 修改：`services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- 修改：`services/api/app/application/web_menu_exploration.py`
- 修改：`services/api/app/domain/page/web_site_menu_registry.py`
- 测试：`services/api/tests/test_web_desktop_navigation.py`
- 测试：`services/api/tests/test_web_desktop_capture_policy.py`
- 测试：`services/api/tests/test_web_menu_exploration.py`
- 测试：`services/api/tests/test_web_site_menu_registry.py`

**接口：**

- 扩展：`WebNavigationCandidate` 增加 `source_kind`、`target_hint`、`source_capture_id`、`source_epoch`、`visibility`、`actionability`、`risk_level` 和 `allowlist_match`。
- `source_kind` 支持 `anchor`、`semantic_link`、`button`、`js_control`、`spa_route`、`visual_card`。
- 候选 ID 由站点、来源 Capture、稳定结构锚点和目标提示生成；候选 ID 不替代规范 URL 页面身份。

- [ ] **Step 1：先写多形态导航候选测试**

  覆盖 `a[href]`、`role=link`、导航按钮、`onclick`、`router-link`、`data-route`、同源视觉卡片、隐藏元素、广告区域、危险操作和外链。

- [ ] **Step 2：确认新增测试失败**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_desktop_capture_policy.py -q`

- [ ] **Step 3：扩展结构候选提取**

  优先语义结构；无法得到 URL 时只生成待验证候选，不伪造链接；候选必须绑定当前 Capture 和 epoch。

- [ ] **Step 4：接入白名单辅助判定**

  白名单命中可提升调度优先级并提供页面类型提示，未命中项仍可被记录和低风险探索；白名单不得产生到达证明或正式类型结论。

- [ ] **Step 5：保持规范 URL 一次正式采集**

  同一 URL 的多个菜单名称、位置和来源合并为一个执行目标，其他来源保留为审计引用；已终态目标不再收集截图和质检记录。

- [ ] **Step 6：运行导航回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_desktop_capture_policy.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_six_site_shadow_fixtures.py -q`

  Expected: PASS，无 `href` 候选可被发现但不计到达，菜单上限保持 10，重复 URL 只有一条正式执行记录。

- [ ] **Step 7：执行阶段提交**

  Commit message: `[AI编程] feat(web): 扩展语义按钮SPA和视觉导航发现；无数据库迁移；前端无需菜单路由同步；视觉候选仍需运行时到达验证`

### Task 7：新增 WEB Explorer Skill

**文件：**

- 新增：`services/api/app/agents/skills/web-explorer-workflow/SKILL.md`
- 修改：`services/api/app/agents/skills/loader.py`
- 修改：`services/api/app/agents/explorer/web_assessment_agent.py`
- 测试：`services/api/tests/test_skills_loader.py`
- 测试：`services/api/tests/test_web_architecture_boundaries.py`
- 测试：`services/api/tests/test_web_exploration_assessment_agent.py`

**Skill 流程：** 观察当前可见层 → 保存阻断前态 → 执行获准的 18+/普通弹窗处理 → 收集导航候选 → 有界调度 → 新观察 → 到达验证 → 页面分类 → 质检通知 → 返回/关闭。

- [ ] **Step 1：先写 Skill 加载和权限边界测试**

  断言 Skill 可发现、内容使用规范四类页面代码、包含成年入口约束、没有 shell/JS 自授权、没有六站白名单硬编码、没有把命令退出码当到达。

- [ ] **Step 2：确认新增测试失败**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_skills_loader.py services/api/tests/test_web_architecture_boundaries.py -q -k "web or skill"`

- [ ] **Step 3：编写单一职责 Skill**

  Skill 只描述如何使用已经存在的观察和动作能力；权限、预算、租约、同源、风险和命令白名单继续由服务端强制。

- [ ] **Step 4：增加确定性加载与版本校验**

  在 `loader.py` 增加 `WEB_EXPLORER_WORKFLOW_SKILL_VERSION`、`WebExplorerWorkflowSkillConfigurationError`、`validate_web_explorer_workflow_skill_content()` 和 `load_web_explorer_workflow_skill_content()`；`web_assessment_agent.py` 在构造阶段确定性加载、校验并直接注入 Workflow 正文，不向只读判定模型挂载 `SkillsProvider` 或暴露 `load_skill`／`read_skill_resource`。浏览器候选动作仍只由运行时执行。

- [ ] **Step 5：运行 Skill 回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_skills_loader.py services/api/tests/test_web_architecture_boundaries.py services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_exploration_workflow.py -q`

  Expected: PASS，Skill 能加载且无法扩大运行时权限。

- [ ] **Step 6：执行阶段提交**

  Commit message: `[AI编程] feat(agent): 新增WEB受控探索工作流Skill；无数据库迁移；无前端菜单路由变更；现场效果由六站回归验证`

### Task 8：保证工作台报告始终可查看

**文件：**

- 修改：`services/api/app/api/v1/workstation/reports.py`
- 修改：`services/api/app/api/v1/workstation/task_insight.py`
- 修改：`services/api/app/application/workstation_reports.py`
- 修改：`services/api/app/application/report_insight.py`
- 修改：`app/workstation/src/modules/tasks/pages/TaskListView.vue`
- 修改：`app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- 修改：`app/workstation/src/modules/reports/pages/ReportCenterView.vue`
- 修改：`app/workstation/src/modules/reports/pages/ReportDetailView.vue`
- 测试：`services/api/tests/test_workstation_reports_api.py`
- 测试：`services/api/tests/test_task_insight_api.py`
- 测试：`app/workstation/src/modules/tasks/pages/TaskListView.test.ts`
- 测试：`app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`
- 测试：`app/workstation/src/modules/reports/pages/ReportCenterView.test.ts`

**接口：**

- `report_availability` 明确为 `none`、`preview`、`final`。
- preview 包含可信采集、质检进度、页面类型复核、阻断和 Completion Gate 缺口。
- final 只在 Gate 通过且报告绑定完成后可正式导出。

- [ ] **Step 1：先写报告按钮状态矩阵测试**

  覆盖运行中有预览、完成但 Gate 未通过、等待人工复核、正式完成、完全无采集和后端报告读取异常。

- [ ] **Step 2：确认新增测试失败**

  Run backend: `docker exec alioth-api python -m pytest services/api/tests/test_workstation_reports_api.py services/api/tests/test_task_insight_api.py -q`

  Run frontend: `npm --prefix app/workstation test -- --run src/modules/tasks/pages/TaskListView.test.ts src/modules/tasks/pages/TaskDetailView.test.ts src/modules/reports/pages/ReportCenterView.test.ts`

- [ ] **Step 3：统一 API 可用性与前端按钮条件**

  任务列表和详情使用同一个 `report_availability` 读模型；`preview` 显示“查看报告（预览）”；`final` 显示“查看报告”；`none` 显示明确原因而不是无声隐藏。

- [ ] **Step 4：处理后端 500**

  数据不完整、报告尚未绑定和 Gate 未通过返回稳定业务状态；真实数据库或存储故障保留 5xx 并返回可审计错误 ID，前端展示重试与诊断信息。

- [ ] **Step 5：运行报告回归与构建**

  Run backend: `docker exec alioth-api python -m pytest services/api/tests/test_workstation_reports_api.py services/api/tests/test_task_insight_api.py services/api/tests/test_web_v2_formal_report_e2e.py -q`

  Run frontend: `npm --prefix app/workstation test`

  Run build: `npm --prefix app/workstation run build`

  Expected: PASS，任务列表和详情都能打开同一任务的预览或正式报告，Gate 未通过不会伪装为 final。

- [ ] **Step 6：执行阶段提交**

  Commit message: `[AI编程] fix(workstation): 统一WEB报告预览与正式入口；无数据库迁移；同步任务和报告页面但不新增菜单路由；真实存储故障仍返回可审计5xx`

### Task 9：完成六站探索与质检回归

**文件：**

- 修改：`scripts/web_v2_six_site_shadow.py`
- 修改：`scripts/web_v2_live_evidence.py`
- 修改：`services/api/tests/test_web_six_site_shadow_fixtures.py`
- 修改：`services/api/tests/test_web_v2_formal_report_e2e.py`
- 新增：`docs/reports/2026-09-07-web-v2-six-site-dev2-regression.md`

**六站范围：** `heiliao.com`、`hl365.com`、`91jav1.com`、`91porna.com`、`huangguoai.com`、`avhub360.com`。

**每站验收矩阵：**

- 裸域名、HTTP/HTTPS 和允许的重定向合并为同一入口身份。
- 最多采集 10 个菜单目标，所有槽位必须是成功、阻断、跳过或复核等明确终态。
- 四种页面类型按站点适用性覆盖；缺失类型必须给出可信不存在、无法到达或证据不足原因。
- 每个正式采集页面必须有类型结果和质检结果；允许 `needs_review`，不允许无记录丢失。
- 同一规范化 URL 不重复截图、不重复证据、不重复质检时间线。
- 18+、公告、广告、验证码和外跳均按独立可见层或阻断事实记录。
- 广告密集页保存遮挡前态，并在有界滚动后选择可信非广告业务区域作为主截图；不能通过 DOM 修改制造通过。
- 每个任务在工作台任务列表和任务详情均能进入 preview 或 final 报告。

- [x] **Step 1：运行全部离线 Web 回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_readiness.py services/api/tests/test_playwright_cli_runtime.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_page_applicability.py services/api/tests/test_web_runtime_evidence.py services/api/tests/test_web_evidence_bundle.py services/api/tests/test_web_v2_formal_report_e2e.py services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_workstation_reports_api.py -q`

  2026-09-09 收口结果：精确收集 `test_web_*.py` 为 `1203 passed, 49 skipped`；全目录 `-k web` 收集被既有 APK Manifest 测试的无关导入错误阻断，未将其计入 Web 失败。

- [ ] **Step 2：运行跨端防回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_app_exploration_mode_parity.py services/api/tests/test_page_graph_projection.py -q`

  Expected: PASS，Web 改动不改变 H5/App 页面类型含义、动作权限和证据 Gate。

- [x] **Step 3：运行前端完整测试和构建**

  Run: `npm --prefix app/workstation test`

  Run: `npm --prefix app/workstation run build`

  2026-09-09 收口结果：工作台 Vitest 90 项通过，生产构建通过。

- [ ] **Step 4：执行六站真实探索**

  每站使用独立任务和独立 Run；记录任务 ID、Run ID、最终状态、菜单终态、页面类型分布、复核数、重复 URL 数、阻断原因、截图选择和报告 URL。

  当前阻断：运行环境已恢复且 Worker 已加载最新代码；因目标页 DOM、可见文本和截图会发送到当前第三方模型服务 `api.routin.ai`，须取得针对该数据外发范围的明确用户授权后再启动，不得绕过安全审查。

- [ ] **Step 5：逐站复核报告**

  从任务列表和任务详情分别打开报告；核对页面时间线、截图/URL/类型对应关系、质检结果、Gate 缺口和导出限制。

- [ ] **Step 6：生成真实回归报告**

  报告必须区分代码测试、离线固定样本、真实站点运行、人工复核和 Completion Gate，不能用其中任一项替代其他项。

- [ ] **Step 7：执行阶段提交**

  Commit message: `[AI编程] test(web): 完成六站探索质检与报告回归；无数据库迁移；无新增前端菜单路由；站点动态内容和人机验证作为现场风险保留`

## 6. 定向补采启用门禁

以下条件全部满足前，`web_active_interaction_boundary.py` 保持正式动作抑制：

- 页面图只接收可信到达，候选和失败动作不会形成业务覆盖。
- 就绪判定能够识别过渡帧、身份漂移、证据错帧和网络不可观测。
- 滚动后强制重新观察和重绑定，动作四层结果可审计。
- 所有 Web Writer 经过统一证据校验，诊断证据不会进入正式 InputSet。
- 精确原状态、规则等价状态、当前新观察和人工处理四种补采语义已经预先声明。
- 登录、支付、一次性上下文和跨域外跳有从严策略。
- 六站回归中没有因重复补采生成重复质检时间线。

满足后另行创建定向补采实施计划，不在本轮任务中隐式打开。

## 7. 完成标准

### 7.1 功能完成

- 每个正式采集页面都有规范页面身份、证据角色、页面类型结果和质检状态。
- 页面类型只输出四种规范代码或明确的未知/冲突/复核状态。
- 裸域名在稳定业务帧后保留首页角色，不因没有路径后缀判定失败。
- 同一规范 URL 只有一条正式采集与质检时间线。
- 无 `href` 菜单、SPA 控件和视觉卡片能够成为待验证候选，但不会被提前算作已到达页面。
- 广告密集页面的主截图来自可信业务区域，同时保留广告遮挡前态。
- 年龄确认只处理唯一、明确且获准的成年入口。
- 菜单采集上限为 10，所有计划槽位都有终态。
- 任务列表和任务详情均能查看 preview 或 final 报告。

### 7.2 质量完成

- 本计划列出的后端目标测试、跨端防回归、前端 Vitest 和前端构建全部通过。
- 六站真实回归完成，并提供任务 ID、Run ID、结果统计和人工复核记录。
- Completion Gate 未通过时报告明确标为 preview，不得伪装 final。
- 代码审查未发现 Explorer 越权、JS 诊断污染、候选冒充到达、白名单覆盖主证据或重复 URL 重复质检。

### 7.3 文档与交接完成

- 更新专项能力与高频回归规则，记录页面就绪、滚动重绑、规范 URL 去重、广告截图和报告可见性回归规则。
- 每个执行提交都说明模块、数据库迁移、前端菜单/路由和测试风险。
- 最终交接明确列出已完成、仍需复核、阻断、未启用能力和回退开关。

## 8. 风险与回退

| 风险 | 控制与回退 |
| --- | --- |
| 新就绪判定过严导致覆盖下降 | 先影子记录新旧判定差异，再按证据角色逐步切换；保留旧结果仅作诊断 |
| 页面类型指南导致历史分类变化 | 冻结指南版本，保留旧 assessment，不原地覆盖历史结论 |
| 无 `href` 候选误点广告或危险入口 | 先记录候选，只有唯一、同源、低风险且后置条件明确时执行 |
| 滚动增加动作数和超时 | 每个计划固定步数、deadline 和无位移终止条件，预算统一结算 |
| 报告按钮展示但数据未就绪 | preview 返回稳定缺口模型；无报告时显示原因，真实故障显示审计 ID |
| 六站动态页面变化 | 白名单只是辅助；现场结果允许明确复核或阻断，不放宽证据标准 |
| Web 改动影响 H5/App | 每阶段运行跨端契约测试，不修改共享语义时另起别名或第二套规则 |

## 9. 推荐执行批次

1. **批次一（P0）：** Task 0～2，冻结基线并完成就绪与交互闭环。
2. **批次二（P0 联调）：** Task 3，接通页面图与可信证据门禁。
3. **批次三（P1）：** Task 4～6，完成类型指南、产品/入口识别和导航发现。
4. **批次四（P2）：** Task 7～8，补齐 Explorer Skill 和报告可见性。
5. **批次五（验收）：** Task 9，执行后端、前端、跨端及六站真实回归。

每个批次完成后先做代码审查和回归，再进入下一批次；发现共享契约不一致时回到对应责任模块修复，不在下游添加兼容猜测。
