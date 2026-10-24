# Web Unbounded Menu and Three-Site Exploration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the fixed ten-menu Web exploration cap, lower formal Web page-type confidence to `0.7`, add navigation/detail-link knowledge, and produce fresh evidence-backed exploration results for three target origins.

**Architecture:** Represent new menu budgets explicitly as unbounded with `menu_limit=None`, while preserving positive integer limits on historical rows. Runtime DOM/screenshot/URL evidence remains authoritative; static Catalog data is advisory. A forward Alembic migration relaxes database constraints, application planning stops slicing unbounded frontiers, and the live runner accepts arbitrary validated HTTPS targets.

**Tech Stack:** Python 3.12, FastAPI, dataclasses, asyncpg, PostgreSQL, Alembic, Playwright, pytest, Docker Compose

**Spec:** `docs/superpowers/specs/2026-09-12-web-unbounded-menu-and-three-site-exploration-design.md`

## Global Constraints

- Web formal page-type default confidence is exactly `0.7`; non-Web thresholds are unchanged.
- DOM structure, visible screenshot, final URL, and page body evidence are authoritative; URL/menu/Catalog hints remain advisory.
- New Web menu budgets use `NULL` to mean no count cap; positive historical limits remain readable and enforceable.
- Same-origin, HTTPS downgrade prevention, risky-action blocking, idempotency, lineage, and run deadline remain mandatory.
- Adult self-attestation continues to come from `.env` and is independent of account, session, and site.
- Docker verification must reuse existing images and must not run a build.
- All comments and documentation added by this implementation use Chinese UTF-8 text.
- No frontend route or menu synchronization and no application-data backfill are required.

---

### Task 1: Extend Web Page Knowledge and Set the Formal Threshold

**Files:**
- Modify: `services/api/app/domain/page/web_page_knowledge.py`
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Modify: `services/api/app/application/web_page_classification.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`
- Test: `services/api/tests/test_web_page_classification.py`

**Interfaces:**
- Consumes: `parse_web_page_type_catalog(payload, legacy_frozen=False)` and the `WebPageClassifier` constructor's `minimum_confidence` keyword.
- Produces: `WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE: Final[float] = 0.7`, parsed `WebNavigationSignals`, parsed `WebDetailLinkSignals`, and `WebPageTypeCatalog.detail_link_signals`.

- [ ] **Step 1: Write failing catalog and threshold tests**

Add boundary tests that load the production Catalog, assert the supplied nested knowledge survives parsing, and prove `0.70` is accepted while `0.69` remains low confidence when the same trusted structural evidence is supplied:

```python
def test_web_catalog_preserves_navigation_and_detail_link_knowledge() -> None:
    catalog = load_web_page_type_catalog()
    assert catalog.navigation_signals.collapsed_and_visible_nav.rules[0].startswith("只把当前屏幕")
    assert "breadcrumb" in catalog.navigation_signals.distinguish_from_breadcrumb
    assert catalog.detail_link_signals.purpose.startswith("在列表或菜单页中")
    assert "to_video_detail" in catalog.detail_link_signals.content_vs_video_detail_link


@pytest.mark.asyncio
async def test_formal_page_type_default_confidence_boundary_is_seventy_percent() -> None:
    accepted = await _classify_ready_content_list(model_confidence=0.70)
    rejected = await _classify_ready_content_list(model_confidence=0.69)
    assert accepted.page_type == "content_list"
    assert rejected.needs_review_reason == "WEB_PAGE_TYPE_LOW_CONFIDENCE"
```

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py -q'
```

Expected: failures show the unknown `detail_link_signals` root field, missing nested navigation fields, and the old `0.8` threshold rejecting `0.70`.

- [ ] **Step 3: Add typed knowledge structures and exact parsers**

Add frozen records in `web_page_knowledge.py`; nested dictionaries are exposed as read-only mappings and arrays become tuples:

```python
@dataclass(frozen=True, kw_only=True)
class WebCollapsedAndVisibleNav:
    description: str
    rules: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class WebDetailLinkSignals:
    purpose: str
    required_positive_signals: tuple[str, ...]
    supporting_signals: tuple[str, ...]
    strong_negative_signals: tuple[str, ...]
    distinguish_from_nav_and_ad: Mapping[str, tuple[str, ...]]
    content_vs_video_detail_link: Mapping[str, tuple[str, ...]]
    usage_notes: tuple[str, ...]
```

Extend `_WEB_CATALOG_ROOT_FIELDS`, `_WEB_NAVIGATION_SIGNAL_FIELDS`, `_parse_navigation_signals`, and add `_parse_detail_link_signals`. Reject missing, extra, empty, or duplicate fields. Preserve legacy v1-v4 parsing without synthesizing missing knowledge.

- [ ] **Step 4: Replace Catalog knowledge and bump its content version**

Set `version` to `2026-09-12.web.v6`. Replace `navigation_signals` with the user-supplied object and append the supplied `detail_link_signals` before `common_site_observations`. Keep the page-type order exactly:

```json
["home", "content_list", "content_detail", "video_list", "video_detail"]
```

- [ ] **Step 5: Centralize the page-type threshold**

```python
WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE: Final[float] = 0.7

class WebPageClassifier:
    def __init__(
        self,
        *,
        assessor: WebPageAssessmentPort,
        catalog: PageTypeCatalog,
        minimum_confidence: float = WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence 必须在 0 到 1 之间")
        self._assessor = assessor
        self._catalog = catalog
        self._minimum_confidence = minimum_confidence

# `_has_required_structure` 中仅替换现有参数默认值：
minimum_confidence: float = WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE,
```

Do not change strong-negative, evidence-insufficient, obstruction, or model-needs-review gates.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the Task 1 command again. Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 1**

```powershell
git add services/api/app/domain/page/web_page_knowledge.py services/api/app/domain/page/web_page_catalog.py services/api/app/domain/page/web_page_type_catalog.json services/api/app/application/web_page_classification.py services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py
git commit -m "[AI编程] feat: 扩展Web页型知识并统一0.7阈值" -m "模块: Web页型Catalog与正式分类；数据库迁移: 无；前端菜单/路由: 无需同步；测试风险: 模型低置信度与强负向冲突回归。"
```

---

### Task 2: Make Menu Planning Explicitly Unbounded

**Files:**
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_exploration_budget_ledger.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_runtime_discovery.py`

**Interfaces:**
- Consumes: `plan_web_menu_collection(site, menu_limit, discovery_policy, required_page_types)`.
- Produces: `WEB_MENU_COLLECTION_LIMIT: None`, `WebMenuExplorationSummary.menu_limit: int | None`, and unbounded collection semantics when `menu_limit is None`.

- [ ] **Step 1: Replace cap-oriented tests with unbounded behavior tests**

Keep compatibility tests for explicit historical limits `5` and `10`, then add a default-path test using at least twelve unique eligible same-origin menus:

```python
def test_default_menu_collection_keeps_every_unique_eligible_menu() -> None:
    site = _site_with_unique_menus(12)
    plan = plan_web_menu_collection(site)
    assert len(plan.selected_entries) == 12
    assert not [entry for entry in plan.skipped_entries if entry.collection_eligibility.eligible]


@pytest.mark.asyncio
async def test_unbounded_explorer_executes_more_than_ten_visible_menus() -> None:
    summary = await _explore_site_with_visible_menu_count(12)
    assert summary.menu_limit is None
    assert summary.executed_same_origin_count == 12
    assert summary.menu_limit_skipped_count == 0
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_discovery.py -q'
```

Expected: the default plan selects ten entries and reports limit skips.

- [ ] **Step 3: Implement nullable limit semantics**

```python
WEB_MENU_COLLECTION_LIMIT: None = None

def plan_web_menu_collection(
    site: WebSiteMenuCatalog,
    *,
    menu_limit: int | None = WEB_MENU_COLLECTION_LIMIT,
    discovery_policy: WebDiscoveryPolicy | None = None,
    required_page_types: tuple[str, ...] = (),
) -> WebMenuCollectionPlan:
    if menu_limit is not None and (
        isinstance(menu_limit, bool) or not isinstance(menu_limit, int) or menu_limit < 1
    ):
        raise ValueError("menu_limit 必须为正整数或 None")
    capacity = len(unique_entries) if menu_limit is None else menu_limit
    selected_entries = unique_entries[:capacity]
    skipped_entries.extend(unique_entries[capacity:])
    return WebMenuCollectionPlan(
        selected_entries=tuple(selected_entries),
        skipped_entries=tuple(skipped_entries),
        alias_of=alias_of,
    )
```

For unbounded discovery, do not reserve a numeric slice for runtime entries. Continue prioritizing required page-type opportunities only as ordering hints.

- [ ] **Step 4: Remove new-run limit terminal generation**

For unbounded plans, no executable eligible entry receives `WEB_MENU_LIMIT_SKIPPED`. Retain the enum and historical parser. Keep the deadline closeout guard:

```python
remaining_seconds = (command.deadline - datetime.now(command.deadline.tzinfo)).total_seconds()
if remaining_seconds <= 15:
    reason = "WEB_DISCOVERY_CLOSEOUT_RESERVED"
```

Aliases, deprecated entries, ineligible entries, and cross-origin entries retain their own explicit audit reasons.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Task 2 command again. Expected: default unbounded and explicit historical-limit tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add services/api/app/application/web_menu_exploration.py services/api/app/application/web_exploration_budget_ledger.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_discovery.py
git commit -m "[AI编程] feat: 去除Web菜单默认数量上限" -m "模块: Web菜单规划与预算合同；数据库迁移: 下一提交引入；前端菜单/路由: 无需同步；测试风险: 历史5/10预算兼容与deadline收口。"
```

---

### Task 3: Relax PostgreSQL Budget and Execution-Slot Constraints

**Files:**
- Create: `services/api/migrations/versions/20260912_0111_web_unbounded_menu_budget.py`
- Modify: `services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py`
- Test: `services/api/tests/test_web_exploration_budget_ledger_migration.py`
- Test: `services/api/tests/test_web_exploration_budget_ledger_repository.py`
- Test: `services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py`
- Test: `services/api/tests/test_web_runtime_discovery_postgres.py`

**Interfaces:**
- Consumes: nullable `WebMenuExplorationSummary.menu_limit` from Task 2.
- Produces: Alembic revision `20260912_0111`, nullable budget limits, and positive unbounded integer execution slots.

- [ ] **Step 1: Add failing PostgreSQL tests for an unbounded budget**

```python
row = await connection.fetchrow(
    "SELECT menu_limit FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid AND site_id=$2",
    run_id,
    site_id,
)
slots = await connection.fetch(
    "SELECT execution_slot FROM qc_web_menu_ledger_entry "
    "WHERE agent_run_id=$1::uuid AND site_id=$2 AND execution_slot IS NOT NULL "
    "ORDER BY execution_slot",
    run_id,
    site_id,
)
assert row["menu_limit"] is None
assert [item["execution_slot"] for item in slots] == list(range(1, 13))
```

Also retain a historical row with `menu_limit=5` and assert slot six is rejected.

- [ ] **Step 2: Run PostgreSQL tests and verify RED**

```powershell
docker exec alioth-api sh -lc 'python -m pytest services/api/tests/test_web_exploration_budget_ledger_migration.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py services/api/tests/test_web_runtime_discovery_postgres.py -q'
```

Expected: the current schema rejects `NULL` and slots above ten.

- [ ] **Step 3: Create migration 0111**

Use `down_revision = "20260912_0110"`. Upgrade operations include:

```sql
ALTER TABLE qc_web_exploration_budget ALTER COLUMN menu_limit DROP NOT NULL;
ALTER TABLE qc_web_exploration_budget ALTER COLUMN menu_limit DROP DEFAULT;
ALTER TABLE qc_web_exploration_budget DROP CONSTRAINT ck_qc_web_budget_menu_limit;
ALTER TABLE qc_web_exploration_budget ADD CONSTRAINT ck_qc_web_budget_menu_limit
  CHECK (menu_limit IS NULL OR menu_limit > 0);
ALTER TABLE qc_web_menu_ledger_entry ALTER COLUMN execution_slot TYPE integer;
ALTER TABLE qc_web_menu_ledger_entry DROP CONSTRAINT ck_qc_web_menu_slot;
ALTER TABLE qc_web_menu_ledger_entry ADD CONSTRAINT ck_qc_web_menu_slot
  CHECK (execution_slot IS NULL OR execution_slot >= 1);
```

Replace `qc_enforce_web_menu_execution_slot()` so `NULL` has no upper bound and a positive historical limit is enforced. Downgrade fails closed if a null budget or slot above ten exists; otherwise restore the previous smallint and `5/10` constraints.

- [ ] **Step 4: Update repository SQL and hydration**

New bootstrap rows insert `NULL`. Replace fixed checks with:

```sql
AND (budget.menu_limit IS NULL OR budget.menu_limit > 0)
AND (budget.menu_limit IS NULL OR entry.execution_slot <= budget.menu_limit)
```

Runtime slot allocation uses `COALESCE(MAX(execution_slot), 0) + 1`, keeps the unique partial index, and never assigns a slot to home, aliases, cross-origin, or zero-action terminal rows. `_build_summary` preserves `None` rather than calling `int(None)`.

- [ ] **Step 5: Upgrade the test database and verify GREEN**

```powershell
docker exec alioth-api sh -lc 'python -m alembic -c services/api/alembic.ini upgrade head'
docker exec alioth-api sh -lc 'python -m pytest services/api/tests/test_web_exploration_budget_ledger_migration.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py services/api/tests/test_web_runtime_discovery_postgres.py -q'
```

Expected: Alembic reaches `20260912_0111`; all selected tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add services/api/migrations/versions/20260912_0111_web_unbounded_menu_budget.py services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py services/api/tests/test_web_exploration_budget_ledger_migration.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py services/api/tests/test_web_runtime_discovery_postgres.py
git commit -m "[AI编程] feat: 支持Web不限菜单持久预算" -m "模块: Web预算账本与PostgreSQL；数据库迁移: 新增0111且无需数据回填；前端菜单/路由: 无需同步；测试风险: 历史显式限额与并发slot分配。"
```

---

### Task 4: Keep Catalog Advisory and Validate Runtime Navigation

**Files:**
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Modify: `services/api/app/application/web_navigation_recognition.py`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_desktop_navigation.py`
- Test: `services/api/tests/test_web_navigation_recognition.py`
- Test: `services/api/tests/test_web_menu_exploration.py`

**Interfaces:**
- Consumes: `WebSiteMenuRegistry.resolve_site_context(target_url)` and `WebNavigationCandidate`.
- Produces: arbitrary-origin runtime contexts and executable candidates derived from visible, hit-tested DOM evidence at confidence `>=0.7`.

- [ ] **Step 1: Add failing runtime-first tests**

```python
def test_unknown_https_origin_uses_runtime_home_without_catalog_gate() -> None:
    context = WebSiteMenuRegistry().resolve_site_context("https://both.pnsmxatxp.cc/")
    assert context.source_kind == "runtime"
    assert context.site.origin == "https://both.pnsmxatxp.cc"
    assert context.site.entries[0].decision is WebMenuDecision.HOME_BASELINE


def test_visible_navigation_at_seventy_percent_is_candidate_but_breadcrumb_is_not() -> None:
    candidates = recognize_navigation_candidates(
        _snapshot_with_nav_and_breadcrumb(confidence=0.70)
    )
    assert [item.label for item in candidates] == ["视频", "资讯"]
```

Add a collapsed-navigation test proving hidden descendants are absent before expansion and present with parent lineage after a trusted visible/hit-tested expansion capture.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_menu_exploration.py -q'
```

Expected: any remaining threshold, hidden-node, breadcrumb, or Catalog-primary behavior is exposed.

- [ ] **Step 3: Enforce runtime-first candidate eligibility**

Keep Catalog reconciliation for ordering and aliases. A candidate must be visible, hit-tested, at least `0.7`, outside breadcrumbs, and either direct navigation or safe expansion:

```python
return (
    candidate.visible
    and candidate.hit_test_passed
    and candidate.navigation_confidence >= 0.7
    and not candidate.within_breadcrumb
    and candidate.actionability in {"direct_navigation", "safe_expand"}
)
```

Do not synthesize candidates from Catalog entries. Continue excluding footer-only links, pagination, downloads, ads, login actions, hidden DOM, and breadcrumbs.

- [ ] **Step 4: Verify the focused navigation suite**

Run the Task 4 command again. Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add services/api/app/application/web_menu_exploration.py services/api/app/application/web_desktop_navigation.py services/api/app/application/web_navigation_recognition.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_menu_exploration.py
git commit -m "[AI编程] fix: 以运行时可见证据判定Web导航" -m "模块: Web导航识别与菜单对账；数据库迁移: 无；前端菜单/路由: 无需同步；测试风险: 收起导航父子关系、面包屑和静态提示降权。"
```

---

### Task 5: Verify Detail-Link Acquisition and Landing Classification

**Files:**
- Modify: `services/api/app/application/web_list_detail_navigation.py`
- Modify: `services/api/app/application/web_primary_content_visitor.py`
- Test: `services/api/tests/test_web_list_detail_navigation.py`
- Test: `services/api/tests/test_web_primary_content_visitor.py`

**Interfaces:**
- Consumes: repeated visible list units and `WebRuntimeMenuEntry.menu_depth`.
- Produces: one trusted detail selection with `navigation_depth = source_menu_depth + 1`, plus landing DOM/screenshot/URL/page-type evidence.

- [ ] **Step 1: Add negative and positive detail-link tests**

```python
@pytest.mark.asyncio
async def test_repeated_cards_open_single_subject_detail_beyond_ten_menu_depth() -> None:
    result, intents = await _visit_repeated_cards(menu_depth=12, landing="video_detail")
    assert result.detail_page_type == "video_detail"
    assert result.selected_detail_candidate_id is not None
    assert intents[-1].parameters["navigation_depth"] == 13


@pytest.mark.parametrize("kind", ["category", "pagination", "advertisement", "external"])
def test_non_detail_repeated_links_are_excluded(kind: str) -> None:
    assert select_detail_candidates(_list_snapshot(kind=kind)) == ()
```

- [ ] **Step 2: Run detail suites and verify RED or protect existing GREEN**

```powershell
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_primary_content_visitor.py -q'
```

Expected: a depth-13 or structural exclusion test fails if any ten-depth assumption remains. If already green, retain these tests as regression proof and do not rewrite working production code.

- [ ] **Step 3: Make only the minimal correction shown by RED**

Preserve the depth contract:

```python
def _detail_navigation_depth(entry: WebSiteMenuEntry | WebRuntimeMenuEntry) -> int:
    if isinstance(entry, WebRuntimeMenuEntry):
        return max(1, int(entry.menu_depth)) + 1
    return 2
```

Selection requires repeated primary structure, title/cover summary, a clickable region, and a target distinct from category, pagination, advertising, or external links. Success still requires a `content_detail` or `video_detail` landing.

- [ ] **Step 4: Run detail suites and verify GREEN**

Run the Task 5 command again. Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 5**

```powershell
git add services/api/app/application/web_list_detail_navigation.py services/api/app/application/web_primary_content_visitor.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_primary_content_visitor.py
git commit -m "[AI编程] test: 固化Web详情链接真实落地证据" -m "模块: Web列表到详情导航；数据库迁移: 无；前端菜单/路由: 无需同步；测试风险: 深层菜单navigation_depth与混合列表误选。"
```

---

### Task 6: Generalize the Live Runner for Arbitrary Targets and Unbounded Reports

**Files:**
- Modify: `scripts/web_v2_six_site_shadow.py`
- Modify: `scripts/web_v2_live_evidence.py`
- Test: `services/api/tests/test_web_six_site_shadow_fixtures.py`

**Interfaces:**
- Consumes: the `targets: Sequence[str]` parameter of `run_live_sites` and nullable `menu_limit` summaries.
- Produces: arbitrary unique canonical HTTPS targets and reports that display `不限` for a null limit.

- [ ] **Step 1: Add failing arbitrary-target and null-limit tests**

```python
def test_live_targets_accept_unique_https_origins_outside_frozen_catalog() -> None:
    targets = _normalize_live_targets((
        "https://heiliao.com/",
        "https://hl365.com/",
        "https://both.pnsmxatxp.cc/",
    ))
    assert targets[-1] == "https://both.pnsmxatxp.cc/"


def test_live_report_renders_null_menu_limit_as_unbounded() -> None:
    report = _live_report(menu_limit=None, menu_limit_skipped_count=0)
    assert "菜单数量上限：不限" in render_live_markdown(report)
```

Retain rejection tests for duplicates, credentials, fragments, invalid schemes, unsafe API targets, and redirects involving the token.

- [ ] **Step 2: Run runner tests and verify RED**

```powershell
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_six_site_shadow_fixtures.py -q'
```

Expected: the third origin is rejected as non-Catalog and null limits fail integer normalization.

- [ ] **Step 3: Separate frozen fixtures from generic live targets**

Keep `SIX_SITE_TARGETS` only for offline historical fixtures. Add repeatable `--target-url`; reject combining it with `--site-id`:

```python
def _normalize_live_targets(targets: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(_canonical_https_target(value) for value in targets)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("实时目标必须非空且不可重复")
    return normalized
```

- [ ] **Step 4: Make reports nullable-limit aware**

New runs require `menu_limit is None` and `menu_limit_skipped_count == 0`. Historical fixtures may retain explicit limits. Compute expected sites from requested targets, and render:

```python
menu_limit_text = "不限" if report.menu_limit is None else str(report.menu_limit)
```

Retain full evidence-ledger validation for every returned site.

- [ ] **Step 5: Run runner tests and verify GREEN**

Run the Task 6 command again. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 6**

```powershell
git add scripts/web_v2_six_site_shadow.py scripts/web_v2_live_evidence.py services/api/tests/test_web_six_site_shadow_fixtures.py
git commit -m "[AI编程] feat: 支持任意Web目标的不限菜单实况报告" -m "模块: Web实况执行器与报告；数据库迁移: 依赖0111；前端菜单/路由: 无需同步；测试风险: 历史六站夹具兼容与动态目标报告校验。"
```

---

### Task 7: Regression Documentation, Full Verification, Docker Restart, and Three-Site Run

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Create: `outputs/web-live-20260912-three-sites/final-exploration-report.json`
- Create: `outputs/web-live-20260912-three-sites/final-exploration-report.md`
- Create: `outputs/web-live-20260912-three-sites/final-exploration-report.html`
- Create: evidence files under `outputs/web-live-20260912-three-sites/evidence/`

**Interfaces:**
- Consumes: Tasks 1-6, existing Docker services, the admin login, and the persisted adult statement.
- Produces: passing regression evidence and a fresh three-site report with traceable DOM/screenshot/URL artifacts.

- [ ] **Step 1: Update reusable regression rules**

```markdown
- Web 正式页型默认置信度为 0.7；Catalog/URL/菜单文案只作辅助先验。
- 新建 Web 菜单预算以 NULL 表示不限数量，仍受 deadline、同源、危险操作、去重与 lineage 门禁约束。
- 主导航只统计当前可见且可点击的并列入口；隐藏节点和面包屑不计入导航。
- 详情入口必须由重复列表单元与单条落地主体共同证明，分类、分页、广告和外链不得冒充详情。
```

- [ ] **Step 2: Run the focused Web suite without production DB leakage**

```powershell
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_primary_content_visitor.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_six_site_shadow_fixtures.py -q'
```

Expected: zero failures.

- [ ] **Step 3: Run migration and PostgreSQL integration suites**

```powershell
docker exec alioth-api sh -lc 'python -m alembic -c services/api/alembic.ini upgrade head'
docker exec alioth-api sh -lc 'python -m pytest services/api/tests/test_web_exploration_budget_ledger_migration.py services/api/tests/test_web_exploration_budget_ledger_repository.py services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py services/api/tests/test_web_runtime_discovery_postgres.py services/api/tests/test_web_v2_formal_report_e2e.py -q'
```

Expected: Alembic head is `20260912_0111`; zero failures.

- [ ] **Step 4: Restart existing services without build and verify health**

```powershell
docker compose restart api worker
docker ps
```

Expected: API, worker, PostgreSQL, and Redis are running and health checks become healthy. Do not run `docker compose build` or pass `--build`.

- [ ] **Step 5: Obtain a fresh token and run three sites**

The runner obtains a token through the existing login endpoint. It must never write the password into reports or stdout:

```powershell
docker exec alioth-api sh -lc 'python scripts/web_v2_six_site_shadow.py --live --api-base-url http://127.0.0.1:8000 --username admin --password-env ALIOTH_LIVE_PASSWORD --adult-statement-env ALIOTH_WEB_ADULT_STATEMENT --target-url https://heiliao.com/ --target-url https://hl365.com/ --target-url https://both.pnsmxatxp.cc/ --output-dir outputs/web-live-20260912-three-sites'
```

Expected: three terminal site records and JSON/Markdown/HTML reports. Every opened homepage or menu page has at most one isolated external-ad record; a page without a qualifying ad records that fact explicitly.

- [ ] **Step 6: Audit fresh reports and artifacts**

Verify each target follows:

```text
home formal assessment -> visible navigation candidates -> menu action lineage
-> list formal assessment -> detail candidate selection -> detail landing assessment
-> isolated external-ad URL + DOM + screenshot -> terminal frontier state
```

Confirm no ordinary menu/detail success crosses origin, no menu is skipped for being eleventh or later, and every referenced artifact exists.

- [ ] **Step 7: Run final repository checks**

```powershell
git diff --check
git status --short --branch
```

Expected: no whitespace errors. Report unrelated pre-existing changes separately without modifying or discarding them.

- [ ] **Step 8: Commit documentation and evidence**

```powershell
git add docs/专项能力与高频回归规则.md outputs/web-live-20260912-three-sites
git commit -m "[AI编程] test: 完成三站Web不限菜单探索验收" -m "模块: Web探索验收与回归规则；数据库迁移: 已验证0111；前端菜单/路由: 无需同步；测试风险: 外部站点内容随时间变化，报告仅代表本次实况。"
```

---

## Completion Criteria

- Production Web Catalog parses the exact new navigation and detail-link knowledge.
- Default formal Web classification threshold is `0.7` at every production entry point.
- A new persistent run can claim and execute at least twelve unique safe menus without a count-limit terminal.
- Historical positive menu limits remain readable and enforced.
- Unknown HTTPS origins use a runtime home context and are not rejected for lacking a static Catalog entry.
- Detail links are selected from repeated primary list units and verified by a single-subject landing page.
- Focused and PostgreSQL integration tests pass from fresh command output.
- Docker services restart without build.
- Fresh reports and evidence exist for all three requested origins, including explicit audited failures rather than omitted rows.
