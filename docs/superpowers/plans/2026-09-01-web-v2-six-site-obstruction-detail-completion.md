# Web V2 Six-Site Obstruction and Single-Detail Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成六站 Web V2 普通阻断、最多 10 个菜单、每菜单唯一详情、双账本与 Web-only 会话收敛，并生成完整实况报告。

**Architecture:** 浏览器 Snapshot 生成可信阻断层和 HTML 主详情锚点，LLM 只选择候选 ID，执行器复核后关闭阻断或直接导航同源详情 URL。菜单 Catalog 账本与运行时发现账本分离，Web V2 Session 使用独立遗留动作清理器，App/H5 不注入该能力。

**Tech Stack:** Python 3.12、FastAPI 应用层、Playwright、PostgreSQL、Microsoft Agent Framework、pytest、Ruff。

**Spec:** `docs/design/2026-09-01-web-v2-six-site-obstruction-detail-completion-design.md`

## Global Constraints

- 只修改 Web V2 路径，不改变 App/H5 探索、页型、弹窗或会话行为。
- Web 页型仍只有 `home`、`web_video_list`、`web_video_detail`、`web_content_list`、`web_content_detail`。
- 每站最多执行 10 个 Catalog 同源菜单；六站目标共 54 个菜单。
- 每个菜单最多选择并采集一个逻辑详情，禁止递归详情探索。
- 普通阻断移除绝对 `z-index > 500` 门槛，改用截图判定视觉阻断和 DOM 证明安全关闭控件。
- 人机验证不得点击；18 岁门禁继续走独立年龄流程。
- 截图保持 1920×1080，只做 PNG 无损体积压缩，不修改尺寸。
- 所有注释使用中文 UTF-8；文档全部放在 `docs`。
- 所有生产行为变更必须先有失败测试，再写最小实现。

---

### Task 1: 阻断层与安全关闭候选

**Files:**
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/domain/exploration/web_exploration.py`
- Modify: `services/api/app/application/web_page_readiness.py`
- Test: `services/api/tests/test_web_obstruction_snapshot.py`
- Test: `services/api/tests/test_web_list_detail_navigation.py`
- Test: `services/api/tests/test_web_page_readiness.py`

**Interfaces:**
- Consumes: UI Snapshot 的 `elements`、viewport、DOM class/role/aria/alt、命中和覆盖事实。
- Produces: `build_web_obstruction_candidates(capture_id, ui_snapshot) -> tuple[WebInteractionCandidate, ...]`，候选携带密封阻断层身份但公开投影不暴露 selector。

- [ ] **Step 1: 写 91jav1 关闭包装层失败测试**

  在 `test_web_obstruction_snapshot.py` 增加真实缩减 fixture：`#popup-window` 内 `.close-img.on-popup-close > img.close-image`，无文本、z-index 2000。断言只生成一个 `web_obstruction_dismiss`，并优先包装层而非装饰图片。

- [ ] **Step 2: 写 avhub360 低 z-index Dialog 与高 z-index 页头测试**

  fixture 包含 z-index 100 的可见 `role=dialog`、`close-btn > img[alt=关闭]`，以及 z-index 1900 的 `#site-header` 链接。断言关闭候选来自 Dialog，页头不进入候选；候选生成不再要求大于 500。

- [ ] **Step 3: 写悬浮广告视觉阻断与非阻断浮层边界测试**

  用同一结构分别标记覆盖主体中心/滚动锁定和仅位于边角。断言前者可生成受限关闭候选，后者不生成候选。

- [ ] **Step 4: 运行 RED 测试**

  Run: `pytest services/api/tests/test_web_obstruction_snapshot.py services/api/tests/test_web_list_detail_navigation.py -q -k "popup_wrapper or low_z_dialog or floating_ad"`

  Expected: FAIL，原因是当前代码仍要求 z-index 大于 500，且无文本关闭包装层被判为普通交互。

- [ ] **Step 5: 最小实现结构阻断证明**

  在 Snapshot 中增加阻断容器类型、显式打开状态、视口覆盖、主体中心覆盖、滚动锁定和结构导航排除事实；在领域函数中只从已证明层生成候选。关闭 class/alt/aria 与受限委托包装层签发 `web_obstruction_dismiss`，导航和普通链接保持不可关闭。

- [ ] **Step 6: 固定长度 readiness intent**

  在 `web_page_readiness.py` 使用 `sha256(capture_id + candidate_id + stage)` 生成 `web-ready:<64 hex>`；原始 Capture 和候选 ID保留在 parameters 中。增加断言 `len(intent.intent_id) <= 128`。

- [ ] **Step 7: 运行 GREEN 与邻近测试**

  Run: `pytest services/api/tests/test_web_obstruction_snapshot.py services/api/tests/test_web_page_readiness.py services/api/tests/test_web_semantic_target_resolver.py -q`

  Expected: 全部通过。

- [ ] **Step 8: 提交 Task 1**

  Run: `git add services/api/app/infrastructure/browser/page_capture.py services/api/app/domain/exploration/web_exploration.py services/api/app/application/web_page_readiness.py services/api/tests/test_web_obstruction_snapshot.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_page_readiness.py services/api/tests/test_web_semantic_target_resolver.py && git commit -m "[AI编程] fix(web): 重构桌面阻断层安全关闭；无数据库迁移；无需前端菜单路由；实况动态弹层仍需复测"`

### Task 2: HTML 主详情候选与唯一同源导航

**Files:**
- Create: `services/api/app/application/web_detail_candidate_extraction.py`
- Modify: `services/api/app/application/web_list_detail_navigation.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/application/web_semantic_target_resolver.py`
- Test: `services/api/tests/test_web_list_detail_navigation.py`
- Test: `services/api/tests/test_web_semantic_target_resolver.py`
- Test: `services/api/tests/test_web_six_site_shadow_fixtures.py`

**Interfaces:**
- Consumes: menu ready UI Snapshot、当前菜单 URL、站点 origin、列表页型。
- Produces: `detail_candidates_from_ready_snapshot(...) -> tuple[WebDetailCandidate, ...]`；每个候选绑定唯一规范 URL、来源 Capture、卡片身份和预期详情类型。

- [ ] **Step 1: 写六站 URL 结构缩减 fixture**

  覆盖 `/archives/113850/`、`/archives/220555.html`、`/videos/FSTU-032`、`/article/58600`、`/article/index/id/58344`、`/detail/3690/`，候选均允许 `center_hits_target=false`。断言它们仍可成为详情候选。

- [ ] **Step 2: 写同卡片封面/标题去重与标签排除测试**

  同一卡片的封面和标题指向同一 URL，只保留一个候选；`hg-tag`、category、分页、“更多”、弹窗内部地址按钮、footer 和导航链接全部排除。

- [ ] **Step 3: 写唯一详情动作测试**

  调用 `capture_one` 后断言只协调一次 Navigate 动作，`destination_url` 来自可信候选，`target_mode=web_detail_candidate_url`，且不执行 selector Click。第二候选永不执行。

- [ ] **Step 4: 运行 RED 测试**

  Run: `pytest services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_web_six_site_shadow_fixtures.py -q -k "offscreen or deduplicates_primary_anchor or one_detail_navigation or six_site_detail_patterns"`

  Expected: FAIL，原因是当前提取器要求中心命中且详情执行仍使用 Click。

- [ ] **Step 5: 提取专用候选模块**

  将详情候选模型、规范 URL、安全过滤、卡片主链接选择和去重放入 `web_detail_candidate_extraction.py`。识别重复卡片的标题/封面主锚点，拒绝 tag、元信息和阻断层链接。

- [ ] **Step 6: 改为可信 URL Navigate**

  `WebListDetailNavigator` 仍让 LLM 只选择 candidate ID；选中后从密封候选恢复规范 URL，构造一次低风险 Navigate ActionIntent。Intent 使用固定长度哈希，参数保存候选和来源 Capture。

- [ ] **Step 7: 复验来源和最终 URL**

  保留 `validate_web_final_url`，要求最终 URL 与候选 URL 同源且无未授权重定向。页面就绪和详情页型验证逻辑保持不变。

- [ ] **Step 8: 运行 GREEN 测试**

  Run: `pytest services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_web_six_site_shadow_fixtures.py -q`

  Expected: 全部通过。

- [ ] **Step 9: 提交 Task 2**

  Run: `git add services/api/app/application/web_detail_candidate_extraction.py services/api/app/application/web_list_detail_navigation.py services/api/app/application/web_runtime_composition.py services/api/app/application/web_semantic_target_resolver.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_web_six_site_shadow_fixtures.py && git commit -m "[AI编程] feat(web): 增加HTML主详情唯一导航；无数据库迁移；无需前端菜单路由；详情重定向需六站实况验证"`

### Task 3: 五类页面结构证据补强

**Files:**
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_six_site_shadow_fixtures.py`

**Interfaces:**
- Consumes: 主详情候选派生的结构计数和现有 Snapshot DOM tokens。
- Produces: `WebPageStructureFacts` 新增 `primary_detail_link_count`、`repeated_primary_card_count`、`media_card_count`，并继续由五类闭集分类器验签。

- [ ] **Step 1: 写 huangguo drama-card 视频列表测试**

  fixture 没有 `<video>`，但有多个 `hg-drama-card`、封面和 `/detail/<id>/` 主链接。模型返回 `web_video_list` 时应通过结构门。

- [ ] **Step 2: 写静态回家页直接内容详情测试**

  fixture 具有单一 `main/article/detail-content` 主体且无重复卡片。模型返回 `web_content_detail` 时应通过，并由菜单访问器直接记为 `detail_captured`。

- [ ] **Step 3: 写导航/标签伪列表拒绝测试**

  只有 nav、tag、分页或弹窗地址链接时，`primary_detail_link_count=0`，不得签发列表页。

- [ ] **Step 4: 运行 RED 测试**

  Run: `pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_six_site_shadow_fixtures.py -q -k "drama_card or direct_content_detail or rejects_navigation_tag_list"`

  Expected: FAIL，原因是当前视频列表要求 video 元素或有限 dom signal，且没有主详情计数。

- [ ] **Step 5: 实现结构事实与 Catalog signal**

  增加主详情链接、重复主卡片和媒体卡片计数；补充 `video-img-box`、`poster`、`thumbnail`、`drama-card`、`archive`、`content-card`、`detail-content` 等通用 signal，不加入站点 selector 或第六页型。

- [ ] **Step 6: 运行 GREEN 测试**

  Run: `pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_six_site_shadow_fixtures.py -q`

  Expected: 全部通过。

- [ ] **Step 7: 提交 Task 3**

  Run: `git add services/api/app/application/web_page_classification.py services/api/app/domain/page/web_page_type_catalog.json services/api/tests/test_web_page_classification.py services/api/tests/test_web_six_site_shadow_fixtures.py && git commit -m "[AI编程] fix(web): 补强独立五类页型结构证据；无数据库迁移；无需前端菜单路由；新结构信号需六站实况复验"`

### Task 4: Catalog 权威账本与运行时发现分离

**Files:**
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/domain/exploration/web_exploration.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_six_site_shadow_fixtures.py`

**Interfaces:**
- Consumes: Catalog entries、运行时导航候选、菜单访问结果。
- Produces: `WebMenuExplorationSummary.menu_ledger` 只含 Catalog；新增 `runtime_discovered_ledger`；错误码与诊断字段分离。

- [ ] **Step 1: 写双账本长度测试**

  对 30 个 Catalog 条目和 46 个运行时新增候选，断言 `len(menu_ledger)==30`、`len(runtime_discovered_ledger)==46`，运行时记录不影响 `menu_total`、执行数或完成率。

- [ ] **Step 2: 写固定错误码测试**

  让 visitor 抛出 `CaptureCoordinationError` 和 `StringDataRightTruncationError`，断言 `error_code="WEB_MENU_NAVIGATION_FAILED"`，异常类型进入 `diagnostic_type`，错误码满足 `WEB_[A-Z0-9_]+`。

- [ ] **Step 3: 运行 RED 测试**

  Run: `pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py -q -k "runtime_discovered_ledger or fixed_error_code"`

  Expected: FAIL，原因是当前运行时记录直接追加到 `menu_ledger`，异常类名拼入错误码。

- [ ] **Step 4: 实现双账本和诊断字段**

  新增独立运行时记录数据类/字段；`menu_ledger` 只按 Catalog 顺序输出。为菜单和详情终态增加有界 `diagnostic_type`、`diagnostic_message`，保持固定错误码。

- [ ] **Step 5: 运行 GREEN 测试**

  Run: `pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_six_site_shadow_fixtures.py -q`

  Expected: 全部通过。

- [ ] **Step 6: 提交 Task 4**

  Run: `git add services/api/app/application/web_menu_exploration.py services/api/app/application/web_runtime_composition.py services/api/app/domain/exploration/web_exploration.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_six_site_shadow_fixtures.py && git commit -m "[AI编程] fix(web): 分离Catalog与运行时导航账本；无数据库迁移；无需前端菜单路由；旧报告兼容需回归"`

### Task 5: Web-only 遗留动作清理和失败摘要保留

**Files:**
- Create: `services/api/app/application/web_exploration_session_cleanup.py`
- Create: `services/api/app/infrastructure/db/web_exploration_session_cleanup_repository.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Test: `services/api/tests/test_web_exploration_session_cleanup.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_surface_session_repository.py`

**Interfaces:**
- Consumes: 已进入 draining、owner 为 `web-v2-workflow` 且 session ID 以 `web:` 开头的 Session。
- Produces: `WebExplorationSessionCleanup.cleanup(task_id, session_id) -> int`，只终结该 Web Session 的遗留 `evidence_pending` 动作。

- [ ] **Step 1: 写 hl365 遗留动作数据库测试**

  创建 Web V2 draining Session、一条 `evidence_pending` action 和已捕获 before Capture。执行 cleanup 后断言 action 为 failed、固定错误码已写入、Surface close readiness 变为 true。

- [ ] **Step 2: 写 App/H5 不可清理测试**

  使用非 `web-v2-workflow` owner、非 `web:` session ID 或非 Web V2 Runtime 注入，断言 cleanup 拒绝或更新 0 行；App/H5 action 状态保持不变。

- [ ] **Step 3: 写关闭失败仍保留摘要测试**

  explorer 返回含菜单账本的 summary，`runtime.close` 抛异常。断言失败 RunResult 仍携带 `web_exploration_summary` 和 `page_timeline`，而不是空 fallback。

- [ ] **Step 4: 运行 RED 测试**

  Run: `pytest services/api/tests/test_web_exploration_session_cleanup.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_surface_session_repository.py -q -k "web_exploration_cleanup or close_failure_preserves_summary"`

  Expected: FAIL，原因是当前没有 Web-only cleanup，关闭异常会丢弃 exploration summary。

- [ ] **Step 5: 实现 cleanup 端口和 PostgreSQL 仓储**

  SQL 必须联结 `qc_surface_session`，同时验证 draining、无 lease、owner、session 前缀和 action 状态，再将遗留动作保守终结为 failed。不得更新 App/H5 Session。

- [ ] **Step 6: 仅向 Web V2 Runtime 注入 cleanup**

  `build_default_web_capture_runtime` 只有在 `web_desktop_policy_enabled=True` 且 `include_h5_exploration=False` 时注入。关闭顺序为 begin_draining → cleanup → seal_exploration_for_close → close Driver。

- [ ] **Step 7: 保留失败摘要**

  `ProductionWebV2RuntimeExecutor` 在 close error 时将已经生成的 summary 和 timeline写入失败 RunResult，并附加 Web 会话关闭诊断。

- [ ] **Step 8: 运行 GREEN 与边界测试**

  Run: `pytest services/api/tests/test_web_exploration_session_cleanup.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_surface_session_repository.py services/api/tests/test_h5_runtime_composition.py -q`

  Expected: 全部通过，H5 关闭合同不变。

- [ ] **Step 9: 提交 Task 5**

  Run: `git add services/api/app/application/web_exploration_session_cleanup.py services/api/app/infrastructure/db/web_exploration_session_cleanup_repository.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/application/web_runtime_composition.py services/api/tests/test_web_exploration_session_cleanup.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_surface_session_repository.py services/api/tests/test_h5_runtime_composition.py && git commit -m "[AI编程] fix(web): 收敛桌面探索遗留动作并保留摘要；无数据库迁移；无需前端菜单路由；异常关闭恢复需实库验证"`

### Task 6: 专项回归、规则文档和代码审查

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/plans/2026-09-01-web-v2-six-site-obstruction-detail-completion.md`

**Interfaces:**
- Consumes: Tasks 1-5 的全部代码和测试。
- Produces: 可复用回归规则、全量 Web 验证证据和审查结论。

- [ ] **Step 1: 更新专项回归规则**

  记录无绝对 z-index 的视觉阻断、关闭包装层、HTML 主详情导航、每菜单单详情、双账本、固定动作身份和 Web-only cleanup。

- [ ] **Step 2: 运行 Web 专项测试**

  Run: `pytest services/api/tests -q -k "web_ and not h5"`

  Expected: 0 failed。

- [ ] **Step 3: 运行 App/H5 边界测试**

  Run: `pytest services/api/tests/test_h5_runtime_composition.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_capture_tool.py -q`

  Expected: 除仓库基线已知失败外无新增失败；任何基线失败必须用基线 commit 复跑证明。

- [ ] **Step 4: 运行 Ruff**

  Run: `ruff check services/api/app/application services/api/app/domain/exploration services/api/app/infrastructure/browser services/api/app/infrastructure/db services/api/tests`

  Expected: 0 errors。

- [ ] **Step 5: 请求代码审查**

  使用 `superpowers:requesting-code-review`，以本计划和设计文档为验收合同，检查 Critical/Important/Minor 问题。Critical 和 Important 必须修复并重新运行相关测试。

- [ ] **Step 6: 提交文档与审查修复**

  Run: `git add docs/专项能力与高频回归规则.md docs/superpowers/plans/2026-09-01-web-v2-six-site-obstruction-detail-completion.md services/api && git commit -m "[AI编程] test(web): 固化六站阻断与单详情回归；无数据库迁移；无需前端菜单路由；外站变化仍需实况验证"`

### Task 7: 六站顺序实况探索与最终报告

**Files:**
- Modify: `scripts/run_web_v2_six_site_live_exploration.py`
- Modify: `services/api/tests/test_web_v2_six_site_live_script.py`
- Create: `docs/reports/2026-09-01-web-v2-six-site-single-detail-live-exploration-final.md`

**Interfaces:**
- Consumes: 独立 Web V2 API/Worker、六站 Catalog 和 Tasks 1-6 的实现。
- Produces: 六站 54 菜单、54 逻辑详情的证据账本与完整报告。

- [ ] **Step 1: 写严格验收失败测试**

  六站聚合脚本必须断言：执行菜单数分别为 10/10/6/10/8/10；每个已选菜单 `status=detail_captured`；每菜单 `detail_action_count<=1`；Catalog ledger 长度精确；Frontier 为 0；PNG 全部 1920×1080。

- [ ] **Step 2: 运行 RED 测试**

  Run: `pytest services/api/tests/test_web_v2_six_site_live_script.py -q -k "requires_one_detail_per_menu or exact_catalog_ledger"`

  Expected: FAIL，原因是脚本尚未执行新的严格单详情验收。

- [ ] **Step 3: 实现脚本验收和报告字段**

  增加详情动作计数、候选证据、阻断关闭链、双账本、尺寸检查和每站失败恢复信息。脚本仍按六站顺序运行，单站最多 10 个菜单。

- [ ] **Step 4: 运行脚本单元测试**

  Run: `pytest services/api/tests/test_web_v2_six_site_live_script.py -q`

  Expected: 全部通过。

- [ ] **Step 5: 构建并启动隔离 Web-only 环境**

  使用独立数据库、Redis 队列、证据根目录和 Web-only Worker；确认 App/H5 worker 与队列未被使用。

- [ ] **Step 6: 顺序执行六站**

  Run: `python scripts/run_web_v2_six_site_live_exploration.py --sites heiliao,hl365,91jav1,91porna,huangguoai,avhub360 --menu-limit 10 --one-detail-per-menu --viewport 1920x1080`

  Expected: 六站执行菜单总数 54，逻辑详情总数 54，Frontier 0。

- [ ] **Step 7: 对失败站点只做有界重跑**

  若外站暂态网络或 LLM 调用失败，只重跑失败站点；不得扩大菜单上限或为已成功菜单采集第二个详情。若出现新结构缺口，先新增失败 fixture，再按 TDD 修复。

- [ ] **Step 8: 生成最终报告**

  报告包含每站首页阻断过程、菜单 URL、菜单 Capture、全部详情候选 ID、唯一选中候选、详情 URL、详情 Capture、分类、截图尺寸、失败重试和最终统计，不输出敏感正文。

- [ ] **Step 9: 最终验证并提交**

  Run: `pytest services/api/tests/test_web_v2_six_site_live_script.py -q && ruff check scripts/run_web_v2_six_site_live_exploration.py services/api/tests/test_web_v2_six_site_live_script.py`

  Expected: 全部通过。

  Run: `git add scripts/run_web_v2_six_site_live_exploration.py services/api/tests/test_web_v2_six_site_live_script.py docs/reports/2026-09-01-web-v2-six-site-single-detail-live-exploration-final.md && git commit -m "[AI编程] report(web): 完成六站菜单与单详情实况探索；无数据库迁移；无需前端菜单路由；报告记录外站暂态风险"`

## Plan Self-Review

- Spec coverage: 阻断、单详情、五类页型、双账本、固定动作身份、Web-only cleanup、1920×1080、54 菜单和报告均有对应任务。
- Placeholder scan: 计划不含 TBD、TODO 或未定义的“稍后处理”。
- Type consistency: 详情候选统一由 `web_detail_candidate_extraction.py` 产生；菜单权威与运行时账本分别为 `menu_ledger`、`runtime_discovered_ledger`；cleanup 接口统一为 `cleanup(task_id, session_id) -> int`。
