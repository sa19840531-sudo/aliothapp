# Web V2 硬预算、两站阻断修复与六站复测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 H5/App 探索的前提下，为 Web V2 建立跨重试的每站 10 菜单/每菜单 1 详情硬预算，修复 heiliao/91jav1 首页阻断闭环，并交付六站过程与采集报告。

**Architecture:** 保留 `WEB_V2` 独立 Runtime，用 Web-only 两表账本在浏览器动作前原子预留菜单/详情预算，再从持久化 Capture/Action 血缘导出摘要。年龄门禁与普通阻断在「存在但无安全候选」时失败关闭；六站脚本只在最终报告形成后读取脱敏证据包。

**Tech Stack:** Python 3.12, FastAPI, asyncpg, Alembic, PostgreSQL, Playwright, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-01-web-v2-hard-budget-two-site-retest-design.md`

## Global Constraints

- 仅 `WEB_V2` 可注入新预算账本；H5/App 路由、Runtime、目录、预算和清障不变。
- Viewport/Screen 固定 `1920×1080`；截图保持 PNG 像素尺寸，只允许无损体积优化。
- 每站最多 1 个首页、10 个 Catalog 同源菜单；超限条目不得有菜单 Capture/Action。
- 列表菜单恰好 1 次详情动作；直接详情菜单 0 次额外详情动作。
- 普通阻断使用截图 + DOM + 可见性 + hit-test + 容器关系，禁止把固定 `z-index > 500` 作为门槛。
- 人机验证不点击、不绕过；成年确认只能在有效自我声明和密封安全候选下执行。
- 首页、菜单、详情和阻断动作必须可追溯 screenshot + DOM + URL + action；缺失时 `NEEDS_REVIEW`。
- 短期令牌只存于当前进程内存，不输出、不落盘、不放入命令行或报告。
- 所有注释使用中文 UTF-8；修复后更新 `docs/专项能力与高频回归规则.md`。
- Git 提交头必须精确使用 `[AI编程]`，并说明模块、迁移、前端菜单/路由和测试风险。

---

### Task 1: 阻断评估失败关闭

**Files:**
- Modify: `services/api/app/agents/explorer/web_assessment_agent.py`
- Modify: `services/api/app/application/web_page_readiness.py`
- Test: `services/api/tests/test_web_exploration_assessment_agent.py`
- Test: `services/api/tests/test_web_page_readiness.py`
- Test: `services/api/tests/test_web_obstruction_snapshot.py`

**Interfaces:**
- Consumes: `WebAgeGateAssessmentEnvelopeModel`, `WebObstructionAssessmentEnvelopeModel`, 密封候选 ID 闭集。
- Produces: `WebAgeGateReviewRequiredError` 和新的 `WebObstructionReviewRequiredError`；页面就绪协调器将后者映射为 `popup_blocked`。

- [ ] **Step 1: 把年龄门禁 fail-open 断言改为 RED 测试**

  将旧的 `test_maf_agent_normalizes_age_gate_without_safe_candidate_to_absent` 替换为：

  ```python
  async def test_maf_agent_age_gate_without_safe_confirmation_requires_review():
      with pytest.raises(WebAgeGateReviewRequiredError):
          await assessor.assess(
              screenshot_artifact_id=SCREENSHOT_ID,
              dom_artifact_id=DOM_ID,
              candidates=(),
          )
  ```

- [ ] **Step 2: 增加普通阻断无安全关闭候选的 RED 测试**

  模型返回 `obstruction_present=True`、非年龄/人机验证、候选为空；断言 assessor 不得返回 absent，readiness 不得发出 Ready Observe：

  ```python
  assert result.status == "popup_blocked"
  assert result.ready_capture_id is None
  assert coordinator.commands == []
  ```

- [ ] **Step 3: 增加 91jav1 与 heiliao 结构边界 RED 测试**

  - `#popup-window` 存在但 close 不唯一或 hit-test 失败：必须 blocked，不得 absent。
  - 非阻断角落悬浮广告：不生成 `web_obstruction_dismiss`，模型判定 absent 时可就绪。
  - 无文本 `.close-img` 与 `.on-popup-close` 委托祖先只在 unique + hit-safe + 阻断容器内时产生唯一候选。

- [ ] **Step 4: 运行 RED**

  Run: `pytest services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_page_readiness.py services/api/tests/test_web_obstruction_snapshot.py -q -k "without_safe or unresolved or popup_window or floating_ad"`

  Expected: FAIL，年龄和普通阻断分支当前都会改写为 absent/ready。

- [ ] **Step 5: 最小实现失败关闭**

  ```python
  class WebObstructionReviewRequiredError(RuntimeError):
      """存在普通阻断，但当前 Capture 无安全关闭候选。"""
  ```

  `web_assessment_agent.py` 在两个「存在但无候选」分支分别抛出 `WebAgeGateReviewRequiredError` / `WebObstructionReviewRequiredError`，不再构造 absent。`WebPageReadinessCoordinator.prepare()` 捕获普通阻断异常后返回 `_blocked_result("popup_blocked", ...)`。

- [ ] **Step 6: 运行 GREEN 与 Web 门禁回归**

  Run: `pytest services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_age_gate.py services/api/tests/test_web_page_readiness.py services/api/tests/test_web_obstruction_snapshot.py -q`

  Expected: 0 failed，且测试输出无警告。

- [ ] **Step 7: 提交 Task 1**

  ```text
  [AI编程] fix(web): 阻断评估无安全候选时失败关闭；无数据库迁移；无需前端菜单/路由；两站动态弹层待实测
  ```

### Task 2: Web-only 硬预算迁移与仓储

**Files:**
- Create: `services/api/migrations/versions/20260901_0081_web_exploration_budget_ledger.py`
- Create: `services/api/app/application/web_exploration_budget_ledger.py`
- Create: `services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py`
- Create: `services/api/tests/test_web_exploration_budget_ledger_migration.py`
- Create: `services/api/tests/test_web_exploration_budget_ledger_repository.py`

**Interfaces:**
- Consumes: `WebSiteMenuDefinition`、Catalog 顺序、`task_id/run_id/session_id`。
- Produces: `WebExplorationBudgetLedgerPort.bootstrap/claim_menu/reserve_detail/finalize/read_summary`；数据库硬约束保证每站最多 10 菜单、每菜单最多 1 菜单动作和 1 详情动作。

- [ ] **Step 1: 增加迁移 RED 测试**

  加载 revision 并断言 SQL 包含：

  ```text
  qc_web_exploration_budget
  CHECK (menu_limit = 10)
  qc_web_menu_ledger_entry
  execution_slot BETWEEN 1 AND 10
  menu_action_count IN (0, 1)
  detail_action_count IN (0, 1)
  UNIQUE ... execution_slot WHERE execution_slot IS NOT NULL
  ```

- [ ] **Step 2: 增加仓储 RED 测试**

  使用现有 fake asyncpg pool 风格验证：

  - bootstrap 一次物化完整 Catalog，只给前 10 个同源可执行条目分配 `1..10`。
  - 同一 Run/site/entry 重复 bootstrap 幂等；Catalog/origin/session 漂移失败关闭。
  - 第 11 个 `claim_menu()` 返回 `None`且不产生动作 ID。
  - 同一 entry 第二次 `reserve_detail()` 返回已预留/冲突，不创建新 intent。
  - 非 `surface_kind='web'` 或非 `web-v2-workflow` Session 无法 bootstrap。

- [ ] **Step 3: 运行 RED**

  Run: `pytest services/api/tests/test_web_exploration_budget_ledger_migration.py services/api/tests/test_web_exploration_budget_ledger_repository.py -q`

  Expected: collection FAIL，新 revision/端口/仓储尚不存在。

- [ ] **Step 4: 实现领域数据对象与 Protocol**

  ```python
  @dataclass(frozen=True, kw_only=True)
  class WebMenuActionReservation:
      site_id: str
      menu_entry_id: str
      execution_slot: int
      intent_id: str
      already_reserved: bool

  @dataclass(frozen=True, kw_only=True)
  class WebDetailActionReservation:
      site_id: str
      menu_entry_id: str
      intent_id: str
      already_reserved: bool

  class WebExplorationBudgetLedgerPort(Protocol):
      async def bootstrap(self, command, site) -> None: ...
      async def claim_menu(self, *, command, entry, source_capture_id) -> WebMenuActionReservation | None: ...
      async def reserve_detail(self, *, command, entry, source_capture_id, candidate_id, destination_url) -> WebDetailActionReservation: ...
      async def finalize(self, record: WebMenuLedgerTerminalRecord) -> None: ...
      async def read_summary(self, *, agent_run_id: str, site_id: str) -> WebMenuExplorationSummary: ...
  ```

  稳定 intent 只由冻结身份生成：

  ```python
  menu_intent_id = f"web-menu:{agent_run_id}:{site_id}:{menu_entry_id}"
  detail_intent_id = f"web-detail:{agent_run_id}:{site_id}:{menu_entry_id}"
  ```

- [ ] **Step 5: 实现两表迁移与 PostgreSQL 仓储**

  revision 从 `20260826_0080` 向前；使用 raw `op.execute()`。仓储在事务内通过 `qc_surface_session` 校验 task/session/Web owner，用 `FOR UPDATE` 与唯一索引预留预算；不复制 screenshot/DOM，只保存既有 Capture/Action ID。

- [ ] **Step 6: 运行 GREEN 与迁移回归**

  Run: `pytest services/api/tests/test_web_exploration_budget_ledger_migration.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_app_qc_foundation_migrations.py -q`

  Expected: 0 failed。

- [ ] **Step 7: 提交 Task 2**

  ```text
  [AI编程] feat(web): 增加菜单与单详情持久化硬预算；引入 0081 数据库迁移；无需前端菜单/路由；并发恢复需实库验证
  ```

### Task 3: 把硬预算接入 Web Runtime 与动作血缘

**Files:**
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_desktop_exploration.py`
- Modify: `services/api/app/application/web_list_detail_navigation.py`
- Modify: `services/api/app/application/web_exploration_workflow.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/application/web_exploration_budget_ledger.py`
- Modify: `services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py`
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_desktop_exploration.py`
- Test: `services/api/tests/test_web_list_detail_navigation.py`
- Test: `services/api/tests/test_web_exploration_workflow.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_exploration_budget_ledger_repository.py`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_architecture_boundaries.py`

**Interfaces:**
- Consumes: Task 2 的 reservation/ledger 端口。
- Produces: 稳定菜单/详情 intent、`menu_action_execution_id`、`detail_action_execution_id`，以及从持久化账本恢复的 `WebMenuExplorationSummary`。

**Preflight rulings:**
- Session 注册与首页 Navigate 必须拆为两个明确阶段；完整 Catalog 账本在 active Web Session 注册后、首页动作前建立，不得读取 bootstrapper 私有成员或复制启动逻辑。
- Registry 暴露规范化菜单 Catalog 内容 SHA-256；`source_attachment_sha256` 只证明操作方附件，禁止冒充 Catalog 内容摘要。
- `WebMenuLedgerEntry` 与持久化 summary 携带 reservation count、menu/detail intent 和实际 ActionExecution ID；哈希 URL 仅存储，返回 Explorer summary 时按冻结 Registry `entry_id` 水合可读 canonical URL。
- 实际动作 ID 只取 `coordination.interaction.action_execution_id`；`CaptureCoordinationError` 已带 interaction 时也必须保留，禁止本地推算。

- [ ] **Step 1: 写「重试不突破 10」RED 测试**

  用同一 `agent_run_id/site_id` 运行两次 Explorer，断言：

  ```python
  assert sum(entry.menu_action_count for entry in persisted) <= 10
  assert len({entry.menu_intent_id for entry in persisted if entry.menu_intent_id}) <= 10
  assert visitor.visit_count <= 10
  ```

- [ ] **Step 2: 写「重试不二次进详情」RED 测试**

  详情 reservation 后模拟协调失败和重启；第二次调用不得再发 Navigate，结果为待复核而不是补采。

- [ ] **Step 3: 写动作 ID 传递与直接详情 RED 测试**

  - 已执行菜单必须保留实际 `menu_action_execution_id`。
  - 列表菜单必须保留唯一 `detail_action_execution_id`。
  - 直接详情菜单 `detail_action_count == 0` 且 `detail_action_execution_id is None`。
  - 首页/跨域/超限条目不得有菜单或详情动作 ID。

- [ ] **Step 3a: 写 Session/账本时序与 Catalog 摘要 RED 测试**

  - 严格断言 register active Session -> ledger bootstrap full catalog -> 首页 coordinate。
  - 首页 coordinate 失败时仍从已物化账本输出 site/catalog/全部未执行终态。
  - Registry 任一菜单内容变化必须改变规范化 content SHA-256；附件摘要不得替代。
  - H5/App composition root 不得反向导入 Web ledger/repository。

- [ ] **Step 4: 运行 RED**

  Run: `pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_desktop_exploration.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_exploration_workflow.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_architecture_boundaries.py -q -k "persistent_budget or retry or action_execution or bootstrap_order or catalog_content_sha or architecture"`

  Expected: FAIL，当前 Explorer 只有进程内切片，且账本不传递动作执行 ID。

- [ ] **Step 5: 在动作前预留预算**

  `WebMenuExplorer` 在 visitor 前 `claim_menu()`；`WebListDetailNavigator` 在密封候选确定后、协调之前 `reserve_detail()`。已有 reservation 但无可证明终态时返回 `NEEDS_REVIEW`，不生成新 intent。

- [ ] **Step 6: 使用稳定 intent 并保留实际 ActionExecution ID**

  删除 `_navigation_index` 和 `ready_capture_id` 作为幂等身份的依赖。从 coordination result 或已发生动作的 `CaptureCoordinationError.interaction` 中取得动作 ID，写入 `WebMenuVisitResult` / `WebListDetailResult` / `WebMenuLedgerEntry`。列表详情 navigator 显式接收 command + entry；直接详情不得 reserve_detail。

- [ ] **Step 7: 仅在 `PostgresWebRuntimeProvider` 注入仓储**

  active Web Session 注册后、首页 Navigate 前 bootstrap 完整 Catalog；首页失败时从已物化账本输出 site/catalog/未执行终态，不再输出空 dict。持久化 summary 按冻结 Registry 水合 URL，但绝不使用哈希占位导航。H5/App 组合根不导入新模块。

- [ ] **Step 8: 运行 GREEN 与隔离回归**

  Run: `pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_desktop_exploration.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_exploration_workflow.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_architecture_boundaries.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_app_architecture_boundaries.py services/api/tests/test_worker_web_v2_route.py -q`

  Expected: 0 failed，H5/App 边界断言不变。

- [ ] **Step 9: 提交 Task 3**

  ```text
  [AI编程] fix(web): 接入十菜单与单详情执行预算；使用 0081 账本无新迁移；无需前端菜单/路由；中断恢复保守进入复核
  ```

### Task 4: 成功门禁、Web 证据包与失败摘要

**Files:**
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Modify: `services/api/app/application/task_insight.py`
- Modify: `services/api/app/api/v1/workstation/task_insight.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_capture_evidence_repository.py`
- Create: `services/api/tests/test_web_evidence_bundle_api.py`

**Interfaces:**
- Consumes: 持久化菜单账本、`qc_page_capture`、`qc_evidence_artifact`、`qc_action_execution`。
- Produces: 严格 partial 判定，以及 `GET /api/v1/workstation/tasks/{task_id}/web-evidence` 脱敏只读包。

- [ ] **Step 1: 增加失败状态 RED 表驱动测试**

  ```python
  @pytest.mark.parametrize("status", [
      "no_detail_candidate",
      "page_type_unclassified",
      "detail_selection_untrusted",
      "detail_page_type_mismatch",
      "evidence_incomplete",
      "budget_conflict",
  ])
  def test_web_exploration_review_statuses_never_report_success(status):
      assert _web_exploration_is_partial(exploration_for(status), summary=summary_for(status))
  ```

- [ ] **Step 2: 增加证据包 RED 测试**

  `list_for_task(task_id)` 只返回当前租户任务的：Capture ID、parent ID、URL、role、page type、action execution ID，artifact ID/type/content type/checksum。不返回 storage path、DOM 原文、action payload、ticket 或 token。

- [ ] **Step 3: 增加证据完整性 RED 测试**

  所有账本引用 Capture 都必须在证据包中有 URL + PNG screenshot + DOM；动作后 Capture 还必须有 action trace 或可追溯的 ActionExecution。任一缺失都返回 `evidence_incomplete`。

- [ ] **Step 4: 运行 RED**

  Run: `pytest services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_capture_evidence_repository.py services/api/tests/test_web_evidence_bundle_api.py -q`

  Expected: FAIL，新状态未纳入 partial，`list_for_task` 和 Web evidence API 不存在。

- [ ] **Step 5: 实现严格成功门禁与失败摘要**

  `_web_exploration_is_partial()` 检查完整失败闭集。首页/close 失败时合并 Task 3 持久化账本摘要，保留 site/catalog/证据 Capture ID 和诊断，不伪造页面成功。

- [ ] **Step 6: 实现 Web-only 证据只读包**

  仓储使用任务/租户范围 SQL 读取。Service 仅对 Web V2 任务开放；FastAPI 路由复用现有工作台权限校验。

- [ ] **Step 7: 运行 GREEN 与权限回归**

  Run: `pytest services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_capture_evidence_repository.py services/api/tests/test_web_evidence_bundle_api.py services/api/tests/test_task_insight_api.py -q`

  Expected: 0 failed。

- [ ] **Step 8: 提交 Task 4**

  ```text
  [AI编程] feat(web): 增加严格成功门禁与证据包；无数据库迁移；无需前端菜单/路由；证据只读包不暴露原文与票据
  ```

### Task 5: 六站严格脚本、Markdown/HTML 报告和回归规则

**Files:**
- Modify: `scripts/web_v2_six_site_shadow.py`
- Modify: `services/api/tests/test_web_six_site_shadow_fixtures.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Create: `docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.md`
- Create: `docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.html`

**Interfaces:**
- Consumes: 六站 final report 和 `/web-evidence` 脱敏包。
- Produces: 严格统计门禁、每站过程节点、页面类型和静态 Markdown/HTML 报告。

- [ ] **Step 1: 增加六站严格验收 RED 测试**

  断言：

  ```text
  home_baseline = 1 per site
  executed menus = 10/10/6/10/8/10
  aggregate catalog/executed/skipped/cross-origin = 166/54/103/3
  frontier = 0
  list menu detail action = 1
  direct detail menu detail action = 0
  every referenced capture = URL + PNG + DOM
  ```

- [ ] **Step 2: 增加 Markdown/HTML 脱敏 RED 测试**

  `render_live_html()` 使用 `html.escape`，不输出 `<script>`、token、ticket URL、DOM 原文、storage path 或高熵敏感字段。CLI 同时接收 `--live-report-path` 和 `--live-html-report-path`，stdout 仍只输出白名单摘要。

- [ ] **Step 3: 运行 RED**

  Run: `pytest services/api/tests/test_web_six_site_shadow_fixtures.py -q -k "home_baseline or action_execution or evidence_bundle or live_html or process_nodes"`

  Expected: FAIL，当前脚本无 Web 证据包验收和 HTML 输出。

- [ ] **Step 4: 实现证据规范化与严格验收**

  新增 `_normalize_live_site_evidence_bundle`、`_required_capture_ids_from_ledger`、`_validate_live_site_evidence_bundle`。只在任务终态且 final report 完成后请求 Web evidence，原始 API 响应不写入报告。

- [ ] **Step 5: 实现过程节点与双报告**

  每站输出：提交、入队、首页 probe/年龄/普通阻断/ready、菜单 action/probe/ready/type、详情 candidate/action/probe/ready/type、终态、证据封存。HTML 为无 JavaScript 静态页。

- [ ] **Step 6: 更新专项回归规则**

  记录失败关闭、两表预算、稳定 intent、直接详情 0 动作、列表详情 1 动作、证据完整性、非 z-index 门槛和 H5/App 隔离回归。

- [ ] **Step 7: 运行 GREEN**

  Run: `pytest services/api/tests/test_web_six_site_shadow_fixtures.py -q`

  Run: `ruff check scripts/web_v2_six_site_shadow.py services/api/tests/test_web_six_site_shadow_fixtures.py`

  Expected: 均为 0 failed/errors。

- [ ] **Step 8: 提交 Task 5**

  ```text
  [AI编程] test(web): 固化六站证据与双报告验收；无数据库迁移；无需前端菜单/路由；外站结构时变风险由实测报告记录
  ```

### Task 6: 完整验证、独立代码审查与六站真实复测

**Files:**
- Modify after real run: `docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.md`
- Modify after real run: `docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.html`

**Interfaces:**
- Consumes: Tasks 1-5 通过审查的代码、本地 PostgreSQL/Redis/Web Worker/LLM/Chromium。
- Produces: 新提交后六站最终报告和验证记录。

- [ ] **Step 1: 运行 Web 专项回归**

  Run: `pytest services/api/tests -q -k "web_ and not h5"`

  Expected: 0 failed。

- [ ] **Step 2: 运行 H5/App 隔离回归**

  Run: `pytest services/api/tests/test_h5_runtime_composition.py services/api/tests/test_web_architecture_boundaries.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_app_workflow_router.py -q`

  Expected: 0 failed。

- [ ] **Step 3: 运行 Ruff 和迁移检查**

  Run: `ruff check services/api/app/agents/explorer/web_assessment_agent.py services/api/app/application/web_* services/api/app/infrastructure/db/web_* services/api/tests/test_web_* scripts/web_v2_six_site_shadow.py`

  Run: `pytest services/api/tests/test_app_qc_foundation_migrations.py services/api/tests/test_web_exploration_budget_ledger_migration.py -q`

  Expected: 0 errors/failed。

- [ ] **Step 4: 逐任务审查与整分支审查**

  每个 Task 使用 `superpowers:requesting-code-review` 检查 spec 符合性和代码质量；Critical/Important 必须修复和复审。任务全部完成后再对计划起点至 HEAD 做整分支审查。

- [ ] **Step 5: 离线校验六站 Catalog**

  Run: `python scripts/web_v2_six_site_shadow.py`

  Expected: Catalog `166`、同源执行目标 `54`、超限 `103`、跨域 `3`，没有 allowlist 漂移。

- [ ] **Step 6: 在受控进程内获得短期令牌并顺序复测**

  使用正式 `POST /api/v1/auth/login` 获得短期 token，仅赋给当前进程的 `ALIOTH_WEB_V2_ACCESS_TOKEN`，不回显。然后运行：

  ```text
  python scripts/web_v2_six_site_shadow.py --live --adult-self-attestation adult-v1 --api-base-url <local-loopback-api> --live-report-path docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.md --live-html-report-path docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.html
  ```

  若出现人机验证，当站安全停止并进入报告。外站暂态错误只允许重跑失败站点，不增加菜单或详情预算。

- [ ] **Step 7: 验证报告与最终提交**

  核对六站过程节点、页面类型、证据完整性和 `166/54/103/3`、首页 `6`、Frontier `0`。报告不得包含 token/ticket/DOM 原文。

  ```text
  [AI编程] report(web): 完成六站硬预算复测报告；沿用 0081 迁移无新变更；无需前端菜单/路由；报告记录外站时变与人机停止风险
  ```

## Plan Self-Review

- Spec coverage: Web-only 隔离、固定 1920×1080、两站阻断、跨重试 10 菜单/单详情、证据包、失败状态、六站顺序与双报告均有对应任务。
- Placeholder scan: 命令中 `<local-loopback-api>` 是运行环境输入，不是实现占位；代码、表名、端口、状态和测试终态均已定义。
- Type consistency: Task 2 的 reservation/ledger 端口是 Task 3 的唯一预算源；Task 4 从同一 Capture/Action 血缘生成证据包；Task 5 只消费最终报告与证据包。
- Ruling: 详情已在菜单页就绪阶段被分类为 detail 时，逻辑详情数计 1，额外详情动作计 0；若错，会导致六站详情总量口径需重新定义。
