# Web V2 Runtime Hardening and Fast Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收口新 Web Run 的动态 href、广告隔离、阶段恢复和通用详情分类合同，并以分层回放加三站代表性见证缩短实况验收时间。

**Architecture:** 新建、重跑和重新探索统一创建当前版本的新 Run，并使用唯一的 `RuntimeWebNavigationContext`；动态发现不可关闭，由服务端冻结的 `WebFrontierPolicy` 控制规模。只有 canonical site scope 内的当前可信 Capture 才能分类和发现 href。历史 Registry 只由 legacy 审计适配器只读解析，不存在旧执行路径；Web 专用 checkpoint 推进到证据对账、报告封存和 Session 关闭，页面分类先提取主/次区域角色，再根据主体事实区分详情与列表。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/SQLAlchemy、Playwright、pytest。

**Spec:** `docs/superpowers/specs/2026-09-14-web-v2-runtime-hardening-and-fast-validation-design.md`

## Global Constraints

- 只修改 Web V2；不得修改 App/H5 生产代码、Catalog、导航、截图或报告语义。
- 新 Run 固定使用真实 DOM 的动态 HTTP(S) href 发现，客户端不能关闭。
- 新建、历史重跑和重新探索都创建当前版本的新 Run；旧 Run 只能查看，不得恢复旧状态机继续执行。
- 新请求携带旧执行覆盖字段时必须明确返回 `422 WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN`，不得静默归一化。
- `ALIOTH_WEB_MENU_COLLECTION_LIMIT` 默认 10，合法范围 `1..1000`；任务创建时冻结，详情和非菜单证据不占额度。
- 动态发现必须有服务端冻结的页面、深度、时长、结构簇和陷阱预算，不能无限访问。
- 跨域落地 DOM、空 href 和无法规范化 URL 不得进入来源站 frontier。
- 普通导航必须同源；广告只能通过 href 在独立 Playwright BrowserContext/Page 打开。
- 页面采集底层必须拒绝广告 click，不能只依赖上层调度约定。
- `direct_ad_page_enabled` 和旧菜单 Registry 不得参与新 Run 的行为选择。
- 历史 Run 仅保持报告、审计和证据的只读兼容；旧广告 click 和全部旧探索动作都不允许重新执行。
- 所有详情分类规则必须与域名、URL 路径和 CMS 名称无关。
- 每个可重试阶段必须持久化 attempt、next retry、error 和 gap；已完成副作用必须幂等复用。
- 所有新增注释使用中文并保持 UTF-8。
- 每项生产行为先写失败测试，确认 RED 后写最小实现并确认 GREEN。
- 不提交原始成人 DOM、图片或完整 HTML；分类 fixture 必须最小化并脱敏。
- 三站只用于实况能力矩阵；通用性必须由随机域名/路径变形、双 origin 和冷启动目标共同证明。
- 不删除或覆盖 `outputs/` 中现有未跟踪探索产物。

---

## File Structure Map

### New files

- `services/api/app/domain/exploration/web_run_stage_checkpoint.py`：Web 阶段、状态、游标和重试值对象。
- `services/api/app/domain/exploration/web_frontier_policy.py`：服务端冻结的页面、深度、时长、结构簇和陷阱预算。
- `services/api/app/application/web_frontier_scheduler.py`：URL 规范化、可信来源门禁、结构聚类、优先级和预算终态。
- `services/api/app/application/web_full_page_stabilization.py`：lazy-load 稳定、sticky 层审计、滚动和高度上限策略。
- `services/api/app/application/web_run_stage_orchestrator.py`：阶段推进、恢复、gap 和幂等调度。
- `services/api/app/infrastructure/db/web_run_stage_checkpoint_row.py`：checkpoint SQLAlchemy 行模型。
- `services/api/app/infrastructure/db/web_run_stage_checkpoint_repository.py`：claim、CAS 更新和待重试查询。
- `services/api/app/application/web_legacy_run_audit.py`：旧策略/Registry 的只读审计边界。
- `services/api/migrations/versions/20260914_0114_web_run_stage_checkpoint.py`：仅 Web checkpoint 迁移。
- `services/api/tests/fixtures/web_detail/`：脱敏最小 DOM fixture。
- `services/api/tests/test_web_run_stage_checkpoint.py`：状态机领域测试。
- `services/api/tests/test_web_run_stage_checkpoint_postgres.py`：迁移、CAS 和恢复集成测试。
- `services/api/tests/test_web_runtime_recovery.py`：Worker 中断、重试耗尽和 gap 后继续测试。
- `services/api/tests/test_web_frontier_scheduler.py`：跨域污染、空 href、URL 陷阱、聚类和预算测试。
- `services/api/tests/test_web_trusted_capture_discovery.py`：canonical origin 和可信 Capture 发现边界测试。
- `services/api/tests/test_web_detail_subject_regions.py`：全站点通用详情区域测试。
- `services/api/tests/test_web_detail_metamorphic.py`：随机域名、路径、标题和非语义 class 变形不变量测试。
- `services/api/tests/test_web_full_page_long_document.py`：长页面稳定、分段、上限和恢复幂等测试。
- `scripts/validation/web_v2_live_acceptance.py`：Codex 测试专用的三站提交、实时增量和代表性见证收口，不新增质检系统定时报表功能。
- `services/api/tests/test_web_v2_live_acceptance_reporter.py`：增量去重和验收门槛测试。
- `services/api/tests/test_web_controlled_two_origin_acceptance.py`：受控跨域、广告、恢复和冷启动浏览器验收。

### Existing files to modify

- `services/api/app/core/config.py`
- `services/api/app/api/v2/schemas.py`
- `services/api/app/domain/exploration/web_discovery_policy.py`
- `services/api/app/application/web_page_applicability.py`
- `services/api/app/application/web_run_policy.py`
- `services/api/app/application/workstation_tasks.py`
- `services/api/app/application/web_runtime_composition.py`
- `services/api/app/application/web_runtime_discovery.py`
- `services/api/app/domain/page/web_site_menu_registry.py`
- `services/api/app/application/web_ad_landing_evidence.py`
- `services/api/app/infrastructure/browser/ad_isolated_page.py`
- `services/api/app/infrastructure/browser/page_capture.py`
- `services/api/app/infrastructure/browser/capture_runtime.py`
- `services/api/app/application/web_full_page_supplemental_capture.py`
- `services/api/app/application/web_primary_subject_facts.py`
- `services/api/app/application/web_page_classification.py`
- `services/api/app/domain/page/web_page_type_catalog.json`
- `services/api/app/infrastructure/db/web_report_page_snapshot.py`
- `services/api/app/application/web_report_explanation.py`
- `docs/专项能力与高频回归规则.md`

---

### Task 1: 固定新 Run 的动态 href 合同

**Files:**
- Modify: `services/api/app/domain/exploration/web_discovery_policy.py`
- Create: `services/api/app/domain/exploration/web_frontier_policy.py`
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/api/v2/schemas.py`
- Modify: `services/api/app/application/web_page_applicability.py`
- Modify: `services/api/app/application/web_run_policy.py`
- Modify: `services/api/app/domain/exploration/web_run_policy_snapshot.py`
- Modify: `services/api/app/application/web_runtime_discovery.py`
- Modify: `services/api/app/application/workstation_tasks.py`
- Test: `services/api/tests/test_web_policy_creation_boundary.py`
- Test: `services/api/tests/test_web_run_policy_snapshot.py`
- Test: `services/api/tests/test_web_runtime_discovery.py`
- Test: `services/api/tests/test_config_env_loading.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`
- Test: `services/api/tests/test_workstation_task_service.py`

**Interfaces:**
- Produces: `WebDiscoveryPolicy.for_new_run() -> WebDiscoveryPolicy`，固定 `enabled=True`、`max_unlisted_menus=None`、`same_origin_only=True`；当前适用性快照另行固定 `applicability_mode="runtime_dynamic"`。
- Produces: `WebFrontierPolicy.for_new_run() -> WebFrontierPolicy`，冻结 `max_pages`、`max_depth`、`max_duration_seconds`、`max_samples_per_structure`、`max_query_variants_per_path` 和 `max_concurrency_per_origin`。
- Produces: 新建、重跑和重新探索入口统一进入当前 Web 提交流程；重跑复用现有 `source_task_id`、`source_operation=rerun` 追溯来源，并创建新的 Task ID 和 Run ID。
- Rejects: 新请求中的整个 `applicability_mode`、`discovery_policy`、`direct_ad_page_enabled` 和 Registry 版本字段，返回 `422 WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN`。
- Preserves: `required_page_types` 仅作为当前 Catalog 下的优先覆盖目标，不作为关闭发现或锁定旧 Catalog 的开关。
- Produces: `load_web_menu_collection_limit() -> int`，读取 `ALIOTH_WEB_MENU_COLLECTION_LIMIT`，默认 `10`，合法范围 `1..1000`。
- Produces: 新 Run 的 `budget.menu_limit` 固定为任务创建时读取到的配置值；详情、首页、广告、跨域和别名不占该额度。
- Preserves: 历史冻结策略的原值只能由 legacy 读取。

- [ ] **Step 1: 写新建、重跑和重新探索只能创建当前 Run 的失败测试**

```python
@pytest.mark.parametrize(
    "legacy_override",
    [
        {"applicability_mode": "strict_catalog"},
        {"discovery_policy": {"enabled": False}},
        {"direct_ad_page_enabled": False},
        {"menu_registry_version": "web-menu-allowlist.v4"},
    ],
)
def test_new_run_rejects_legacy_execution_override(client, legacy_override) -> None:
    response = client.post("/api/v2/tasks", json=web_task_payload(**legacy_override))
    assert response.status_code == 422
    assert response.json()["code"] == "WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN"


def test_rerun_creates_current_run_without_copying_legacy_policy() -> None:
    source = stored_legacy_web_run()
    rerun = rerun_historical_web_task(source.task_id)
    assert rerun.task_id != source.task_id
    assert rerun.run_id != source.run_id
    assert rerun.task_config["source_task_id"] == source.task_id
    assert rerun.task_config["source_operation"] == "rerun"
    assert rerun.policy.discovery_policy == WebDiscoveryPolicy.for_new_run()
    assert rerun.policy.catalog.version == CURRENT_WEB_CATALOG_VERSION
    assert rerun.policy.required_page_types == normalize_current_required_types(
        source.required_page_types
    )
```

- [ ] **Step 2: 写菜单配置默认 10、合法值可调整、非法值 fail-fast 的失败测试**

```python
def test_web_menu_collection_limit_defaults_to_ten(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", raising=False)
    assert load_web_menu_collection_limit() == 10


def test_web_menu_collection_limit_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "25")
    assert load_web_menu_collection_limit() == 25


@pytest.mark.parametrize("value", ["0", "-1", "1001", "invalid"])
def test_web_menu_collection_limit_rejects_invalid_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", value)
    with pytest.raises(ValueError, match="ALIOTH_WEB_MENU_COLLECTION_LIMIT"):
        load_web_menu_collection_limit()
```

- [ ] **Step 3: 运行测试并确认动态关闭和菜单配置测试均失败**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_policy_creation_boundary.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_config_env_loading.py -q`

- [ ] **Step 4: 从 Web 请求 schema 移除旧策略字段，实现唯一的当前 Run 创建器、明确 422 和严格菜单配置读取**

```python
@classmethod
def for_new_run(cls) -> "WebDiscoveryPolicy":
    return cls(enabled=True, max_unlisted_menus=None, same_origin_only=True)


def load_web_menu_collection_limit() -> int:
    raw = os.getenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "10").strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("ALIOTH_WEB_MENU_COLLECTION_LIMIT 必须为整数") from error
    if not 1 <= value <= 1000:
        raise ValueError("ALIOTH_WEB_MENU_COLLECTION_LIMIT 必须在 1..1000 范围内")
    return value
```

工作台 Web 重跑不得再调用复制旧 `applicability_mode`、Catalog 版本和 Catalog hash 的兼容物化函数；只把源任务仍属于当前标准 Catalog 的 `required_page_types` 作为新任务优先覆盖目标，再走与首次提交相同的当前策略创建器。

- [ ] **Step 5: 写新 Run 冻结配置值、历史只读不回写菜单额度的失败测试**

```python
def test_new_run_freezes_configured_menu_limit(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "10")
    frozen = create_new_web_run_policy()
    assert frozen.snapshot.payload["budget"]["menu_limit"] == 10
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "25")
    assert materialize_web_policy(frozen.snapshot).menu_limit == 10
```

- [ ] **Step 6: 写同源真实 href 可执行、普通跨域和危险协议被拒绝的参数化测试**

```python
@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://new.example/post/1", "actionable"),
        ("https://other.example/post/1", "cross_origin_blocked"),
        ("javascript:alert(1)", "unsafe_scheme"),
        ("data:text/html,x", "unsafe_scheme"),
    ],
)
def test_runtime_href_safety_is_origin_based(href: str, expected: str) -> None:
    assert classify_runtime_href("https://new.example/", href).code == expected
```

- [ ] **Step 7: 运行配置、策略、发现和快照测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_config_env_loading.py services/api/tests/test_web_policy_creation_boundary.py services/api/tests/test_web_run_policy_snapshot.py services/api/tests/test_web_runtime_discovery.py -q`

- [ ] **Step 8: 提交**

```text
[AI编程] fix(web): 固定新任务动态href发现合同

模块：Web 配置、任务创建边界、策略与动态发现；数据库迁移：否，复用现有 `source_task_id`、`source_operation`；前端菜单/路由：不涉及；风险：旧客户端提交执行覆盖字段将收到明确 422，需要同步调用方。
```

### Task 2: 可信 Capture 门禁与通用 Frontier 调度

**Files:**
- Create: `services/api/app/application/web_frontier_scheduler.py`
- Modify: `services/api/app/application/web_runtime_discovery.py`
- Modify: `services/api/app/infrastructure/db/web_runtime_discovery_repository.py`
- Create: `services/api/tests/test_web_frontier_scheduler.py`
- Create: `services/api/tests/test_web_trusted_capture_discovery.py`
- Test: `services/api/tests/test_web_runtime_discovery.py`
- Test: `services/api/tests/test_web_runtime_discovery_postgres.py`

**Interfaces:**
- Produces: `CanonicalSiteScope.bind(requested_url, redirect_chain) -> CanonicalSiteScope`，只允许根导航阶段的 HTTP→HTTPS、同主机或 apex↔`www` 绑定。
- Produces: `TrustedWebCaptureAuthority.assert_discoverable(capture) -> None`，验证 Session、epoch、current、ready 和 canonical origin。
- Produces: `WebFrontierScheduler.ingest(source_capture, anchors) -> WebFrontierIngestResult`。
- Produces: `WebFrontierScheduler.discover_once(source_capture) -> WebFrontierIngestResult`，以 `capture_state_fingerprint` 幂等。
- Produces: `WebFrontierCandidate(canonical_url, route_shape, dom_role, structure_fingerprint, priority, source_capture_id, source_page_url, source_origin, raw_href)`。

- [ ] **Step 1: 写跨域落地 DOM 不能污染来源 frontier 的失败测试**

```python
def test_cross_origin_arrival_dom_is_not_discoverable() -> None:
    capture = captured_page(
        requested_url="https://site.example/github.html",
        final_url="https://gitlab.example/project",
        current=True,
        ready=True,
    )
    with pytest.raises(UntrustedWebCaptureError, match="WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"):
        authority.assert_discoverable(capture)
```

- [ ] **Step 2: 写空 href、fragment-only 和危险协议只计数、不建 frontier 行的失败测试**

```python
@pytest.mark.parametrize("href", ["", "#comments", "javascript:void(0)", "data:text/html,x"])
def test_non_navigable_anchor_is_aggregated_without_frontier_row(href: str) -> None:
    result = scheduler.ingest(trusted_capture(), [anchor(href=href)])
    assert result.inserted_candidates == ()
    assert result.rejected_count == 1
```

- [ ] **Step 3: 写 URL 规范化、追踪参数剔除、别名合并和 query 陷阱预算测试**

```python
def test_tracking_and_fragment_variants_collapse_to_one_candidate() -> None:
    result = scheduler.ingest(
        trusted_capture(),
        [
            anchor("/post/1?utm_source=a#top"),
            anchor("https://site.example/post/1?utm_source=b"),
            anchor("/post/1"),
        ],
    )
    assert [item.canonical_url for item in result.inserted_candidates] == ["https://site.example/post/1"]
```

- [ ] **Step 4: 写同结构的 16 个详情只抽取冻结样本数、其余记为 `budget_not_executed` 的失败测试**

```python
def test_structure_cluster_sampling_limits_duplicate_templates() -> None:
    result = scheduler.ingest(trusted_capture(), same_structure_detail_anchors(count=16))
    assert len(result.actionable_candidates) == FRONTIER_POLICY.max_samples_per_structure
    assert len(result.budget_not_executed) == 16 - FRONTIER_POLICY.max_samples_per_structure
```

- [ ] **Step 5: 写首页播种、进入菜单后继续发现二级菜单的失败测试**

```python
async def test_each_trusted_page_discovers_once_and_adds_nested_menu() -> None:
    await scheduler.discover_once(home_capture(anchors=[anchor("/category/")]))
    await scheduler.mark_visited("https://site.example/category/")
    nested = await scheduler.discover_once(
        menu_capture("https://site.example/category/", anchors=[anchor("sub/"), anchor("/category/")])
    )
    assert "https://site.example/category/sub/" in nested.frontier_urls
    assert nested.logical_candidates_for("https://site.example/category/") == 1
```

- [ ] **Step 6: 写菜单上限 10 只计算唯一同源菜单、详情和重复全局导航不占额度的失败测试**

```python
async def test_frozen_menu_limit_counts_only_unique_executed_menu_pages() -> None:
    result = await explore_with_menu_limit(
        menu_limit=10,
        menu_candidates=unique_menu_anchors(12),
        duplicate_global_nav=unique_menu_anchors(5),
        detail_candidates=detail_anchors(6),
    )
    assert result.executed_unique_menu_count == 10
    assert result.menu_limit_skipped_count == 2
    assert result.detail_candidate_count == 6
```

- [ ] **Step 7: 运行新测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_frontier_scheduler.py services/api/tests/test_web_trusted_capture_discovery.py -q`

- [ ] **Step 8: 实现 canonical scope、逐可信状态发现、URL 规范化、结构聚类、谱系合并、菜单额度和优先队列；跨域诊断不得读取目标 DOM**

```python
def ingest(self, source_capture: WebCapture, anchors: Sequence[RuntimeAnchor]) -> WebFrontierIngestResult:
    self._capture_authority.assert_discoverable(source_capture)
    self._state_discovery_guard.claim_once(source_capture.capture_state_fingerprint)
    candidates = tuple(filter(None, (self._normalize(anchor) for anchor in anchors)))
    return self._budget_and_rank(source_capture=source_capture, candidates=candidates)
```

- [ ] **Step 9: 运行发现、菜单额度和 PostgreSQL 回归并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_frontier_scheduler.py services/api/tests/test_web_trusted_capture_discovery.py services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_runtime_discovery_postgres.py services/api/tests/test_web_menu_exploration.py -q`

```text
[AI编程] feat(web): 增加可信来源和动态frontier调度

模块：Web 动态发现与安全边界；数据库迁移：否，复用发现账本；前端菜单/路由：不涉及；风险：canonical origin 和 URL 聚类。
```

### Task 3: 删除旧执行路径并隔离历史只读审计

**Files:**
- Create: `services/api/app/application/web_legacy_run_audit.py`
- Modify: `services/api/app/application/web_run_policy.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_run_policy_snapshot.py`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_legacy_run_execution_boundary.py`

**Interfaces:**
- Produces: `RuntimeWebNavigationContext(target_origin, url_safety_policy, discovery_policy)`。
- Produces: `LegacyWebRunAuditAdapter.load(snapshot) -> LegacyWebAuditView`，只读旧字段。
- Produces: 历史重跑通过现有提交流程创建新 Task 和当前 Run，以 `source_task_id`、`source_operation=rerun` 建立来源关系，不复制旧策略。
- Removes from new runtime: `direct_ad_page_enabled` 和 `WebSiteMenuRegistry` 参数。
- Rejects: 对非终态旧 Run 的 Worker claim，收口为 `WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED`，不得创建 legacy checkpoint。

- [ ] **Step 1: 写新 composition 不接受 Registry 或广告开关的失败测试**

```python
def test_new_runtime_context_has_no_site_registry_or_ad_flag() -> None:
    context = build_new_web_navigation_context(target_url="https://arbitrary.example/")
    assert not hasattr(context, "menu_registry")
    assert not hasattr(context, "direct_ad_page_enabled")
```

- [ ] **Step 2: 写历史快照仍可只读展示旧字段的失败测试**

```python
def test_legacy_audit_reads_registry_without_exposing_it_to_runtime() -> None:
    audit = LegacyWebRunAuditAdapter.load(legacy_snapshot_with_menu_registry())
    assert audit.menu_registry_version == "web-menu-allowlist.v4"
    assert audit.execution_allowed is False
```

- [ ] **Step 3: 写旧 Run 不可继续执行、历史重跑创建当前 Run 的失败测试**

```python
def test_worker_does_not_claim_non_terminal_legacy_run() -> None:
    result = claim_web_run(non_terminal_legacy_run())
    assert result.terminal_gap.code == "WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"


def test_historical_rerun_has_new_identity_and_current_contract() -> None:
    source = stored_legacy_web_task()
    rerun = rerun_historical_web_task(source.task_id)
    assert rerun.task_id != source.task_id
    assert rerun.run_id != source.run_id
    assert rerun.task_config["source_task_id"] == source.task_id
    assert rerun.task_config["source_operation"] == "rerun"
    assert rerun.runtime_version == CURRENT_WEB_RUNTIME_VERSION
    assert rerun.discovery_policy.enabled is True
```

- [ ] **Step 4: 运行精确测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_run_policy_snapshot.py services/api/tests/test_web_legacy_run_execution_boundary.py -q`

- [ ] **Step 5: 新建只读 legacy adapter，并让 production composition 只装配当前 runtime context**

```python
def build_new_web_navigation_context(policy: FrozenWebPolicy) -> RuntimeWebNavigationContext:
    return RuntimeWebNavigationContext(
        target_origin=policy.target_origin,
        url_safety_policy=policy.url_safety_policy,
        discovery_policy=WebDiscoveryPolicy.for_new_run(),
        frontier_policy=policy.frontier_policy,
    )
```

- [ ] **Step 6: 删除 `direct_ad_page_enabled` 在 scheduler、factory 和 adapter 中的参数传递；只在历史只读 JSON 反序列化中保留**

```python
@dataclass(frozen=True)
class LegacyWebAuditView:
    menu_registry_version: str | None
    direct_ad_page_enabled: bool | None
    execution_allowed: Literal[False] = False
```

- [ ] **Step 7: 使用 ripgrep 验证旧标识只存在于 legacy、迁移、历史 fixture 或兼容测试**

Run: `rg -n "direct_ad_page_enabled|WebSiteMenuRegistry" services/api/app services/api/tests`

Expected: 新 Run 的 composition、scheduler 和 browser adapter 中无命中。

- [ ] **Step 8: 运行组合、历史只读和旧执行拒绝测试并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_run_policy_snapshot.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_legacy_run_execution_boundary.py -q`

```text
[AI编程] refactor(web): 隔离旧站点目录只读审计

模块：Web 运行时组合、历史只读审计与重跑边界；数据库迁移：否，复用现有任务来源字段；前端菜单/路由：不涉及；风险：部署时非终态旧 Run 会明确终止，需由当前重跑入口重新创建。
```

### Task 4: 在最底层禁止广告 click

**Files:**
- Modify: `services/api/app/application/web_ad_landing_evidence.py`
- Modify: `services/api/app/infrastructure/browser/ad_isolated_page.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Test: `services/api/tests/test_web_ad_landing_evidence.py`
- Test: `services/api/tests/test_web_auxiliary_navigation.py`
- Test: `services/api/tests/test_web_capture_runtime.py`

**Interfaces:**
- Consumes: 已校验的广告 HTTP(S) href 和来源 Capture ID。
- Produces: `WebAdPageReceipt` 或结构化 `WEB_AD_*` gap。
- Enforces: `capture_ad_landing_evidence=True` 只允许 `DIRECT_AD_OUTBOUND`。

- [ ] **Step 1: 写页面采集底层拒绝广告 click 的失败测试**

```python
async def test_page_capture_rejects_ad_click_before_playwright_action() -> None:
    result = await runtime.capture(
        action=BrowserAction.CLICK,
        selector="#ad",
        capture_ad_landing_evidence=True,
    )
    assert result.error_code == "WEB_AD_CLICK_FORBIDDEN"
    playwright_page.click.assert_not_awaited()
```

- [ ] **Step 2: 写隔离上下文必须使用 href goto、关闭后不改变业务 Page 的失败测试**

```python
async def test_ad_href_uses_isolated_context_and_preserves_business_page() -> None:
    receipt = await opener.open_href(source_page, "https://ads.example/landing")
    assert receipt.page_type == "ad_page"
    isolated_page.goto.assert_awaited_once_with("https://ads.example/landing", wait_until="domcontentloaded")
    isolated_context.close.assert_awaited_once()
    assert source_page.url == "https://site.example/list"
```

- [ ] **Step 3: 运行精确测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_capture_runtime.py -q`

- [ ] **Step 4: 在 request validation 和 action dispatcher 两层拒绝广告 click**

- [ ] **Step 5: 删除广告普通分类/evidence-insufficient 分支；成功打开直接签发 `ad_page`**

```python
if opened_page is not None:
    return WebAdPageReceipt(
        capture_id=opened_page.capture_id,
        requested_url=href,
        final_url=opened_page.final_url,
        page_type="ad_page",
        parent_capture_id=source_capture_id,
    )
```

- [ ] **Step 6: 写旧 Run 广告 click 只能由审计适配器展示、不能生成命令或 gap 的兼容测试**

```python
def test_legacy_ad_click_is_read_only_and_never_replayed() -> None:
    audit = LegacyWebRunAuditAdapter.load(legacy_snapshot_with_ad_click())
    assert audit.ad_action_kind == "CLICK"
    assert audit.execution_allowed is False
    assert audit.navigation_commands == ()
    assert audit.generated_gaps == ()
```

- [ ] **Step 7: 运行广告、浏览器与辅助导航测试并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_capture_runtime.py -q`

```text
[AI编程] fix(web): 底层禁止广告点击并强制隔离打开

模块：Web 广告证据与浏览器适配；数据库迁移：否；前端菜单/路由：不涉及；风险：历史广告动作不再重放。
```

### Task 5: 建立 Web 阶段 checkpoint 领域和数据库

**Files:**
- Create: `services/api/app/domain/exploration/web_run_stage_checkpoint.py`
- Create: `services/api/app/infrastructure/db/web_run_stage_checkpoint_row.py`
- Create: `services/api/app/infrastructure/db/web_run_stage_checkpoint_repository.py`
- Create: `services/api/migrations/versions/20260914_0114_web_run_stage_checkpoint.py`
- Create: `services/api/tests/test_web_run_stage_checkpoint.py`
- Create: `services/api/tests/test_web_run_stage_checkpoint_postgres.py`

**Interfaces:**
- Produces: `WebRunStage`、`WebStageStatus`、`WebStageCursor`、`WebStageRetryPolicy`。
- Produces repository methods: `claim(...)`、`mark_succeeded(...)`、`schedule_retry(...)`、`record_gap_and_advance(...)`、`list_due(...)`。

- [ ] **Step 1: 写状态推进、非法回退和重试耗尽的领域失败测试**

```python
def test_transient_navigation_exhaustion_records_gap_and_advances() -> None:
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="url:1")
    cursor = cursor.fail_transient("WEB_GOTO_TLS_FAILED", max_attempts=3)
    cursor = cursor.fail_transient("WEB_GOTO_TLS_FAILED", max_attempts=3)
    cursor = cursor.fail_transient("WEB_GOTO_TLS_FAILED", max_attempts=3)
    assert cursor.status is WebStageStatus.GAPPED
    assert cursor.gap_codes == ("WEB_NAVIGATION_RETRY_EXHAUSTED",)
    assert cursor.next_stage is WebRunStage.FRONTIER_DRAIN
```

- [ ] **Step 2: 运行领域测试并确认模块不存在而失败**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_run_stage_checkpoint.py -q`

- [ ] **Step 3: 实现不可变游标、固定阶段图和重试策略矩阵**

```python
class WebRunStage(StrEnum):
    BOOTSTRAP_NAVIGATION = "bootstrap_navigation"
    OBSTRUCTION_CLEAR = "obstruction_clear"
    READINESS = "readiness"
    PRIMARY_CAPTURE = "primary_capture"
    FULL_PAGE_CAPTURE = "full_page_capture"
    CLASSIFICATION = "classification"
    HREF_DISCOVERY = "href_discovery"
    CANDIDATE_NAVIGATION = "candidate_navigation"
    FRONTIER_DRAIN = "frontier_drain"
    EVIDENCE_RECONCILIATION = "evidence_reconciliation"
    COVERAGE_SNAPSHOT = "coverage_snapshot"
    REPORT_FINALIZATION = "report_finalization"
    SESSION_CLOSE = "session_close"
```

- [ ] **Step 4: 写 migration upgrade/downgrade、唯一键、待重试索引和 CAS 失败测试**

- [ ] **Step 5: 实现表和 repository；更新必须使用 `row_version` 乐观锁**

```python
updated = await session.execute(
    update(WebRunStageCheckpointRow)
    .where(WebRunStageCheckpointRow.id == checkpoint_id)
    .where(WebRunStageCheckpointRow.row_version == expected_row_version)
    .values(**changes, row_version=expected_row_version + 1)
)
if updated.rowcount != 1:
    raise WebStageCheckpointConflict(checkpoint_id)
```

- [ ] **Step 6: 运行领域、迁移和 PostgreSQL 集成测试并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_run_stage_checkpoint.py services/api/tests/test_web_run_stage_checkpoint_postgres.py -q`

```text
[AI编程] feat(web): 增加探索阶段持久化游标

模块：Web 探索状态机；数据库迁移：新增 qc_web_run_stage_checkpoint；前端菜单/路由：不涉及；风险：并发 claim 与恢复幂等。
```

### Task 6: 接入阶段恢复、重试和 gap 后继续

**Files:**
- Create: `services/api/app/application/web_run_stage_orchestrator.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/application/web_full_page_supplemental_capture.py`
- Modify: `services/api/app/application/web_ad_landing_evidence.py`
- Modify: `services/api/app/infrastructure/db/web_runtime_discovery_repository.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_full_page_supplemental_capture.py`
- Test: `services/api/tests/test_web_runtime_recovery.py`

**Interfaces:**
- Consumes: `WebStageCursor` 和现有 Capture/Artifact/Surface Phase/Menu ledger。
- Produces: `advance_once(run_id, logical_page_key) -> WebStageAdvanceResult`。
- Guarantees: 每次调用最多推进一个阶段，事务提交后才执行下一阶段。

- [ ] **Step 1: 写 Worker 在长图完成后中断，恢复时不重复截图的失败测试**

```python
async def test_resume_after_full_page_reuses_completed_surface_phase() -> None:
    await seed_checkpoint(stage="full_page_capture", status="running")
    await seed_successful_full_page_phase(parent_capture_id=CAPTURE_ID)
    result = await orchestrator.advance_once(RUN_ID, PAGE_KEY)
    assert result.next_stage == "classification"
    full_page_capture.capture.assert_not_awaited()
```

- [ ] **Step 2: 写非首页 GOTO/TLS 耗尽后继续下一候选的失败测试**

```python
async def test_navigation_retry_exhaustion_does_not_abort_remaining_frontier() -> None:
    result = await run_with_candidates([tls_failing_href, healthy_href])
    assert "WEB_NAVIGATION_RETRY_EXHAUSTED" in result.gaps
    assert healthy_href in result.visited_urls
```

- [ ] **Step 3: 写弹窗按指纹重试两次、长图失败继续分类的失败测试**

```python
async def test_full_page_failure_records_gap_and_still_classifies() -> None:
    result = await run_page(full_page_error=TimeoutError())
    assert "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT" in result.gaps
    assert result.classification.page_type == "content_detail"
```

- [ ] **Step 4: 运行恢复测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_runtime_recovery.py -q`

- [ ] **Step 5: 实现单阶段事务推进器，并在副作用前后写 checkpoint/Surface Phase**

```python
async def advance_once(self, run_id: UUID, logical_page_key: str) -> WebStageAdvanceResult:
    checkpoint = await self._checkpoints.claim_due(run_id, logical_page_key)
    existing = await self._effects.find_completed(checkpoint.idempotency_key)
    outcome = existing or await self._execute_stage(checkpoint)
    return await self._persist_outcome(checkpoint, outcome)
```

- [ ] **Step 6: 将 `ProductionWebV2RuntimeExecutor.run` 改为循环调用 orchestrator；顶层只处理不可恢复的配置/数据库失败**

```python
while due := await orchestrator.next_due(run_id):
    await orchestrator.advance_once(run_id, due.logical_page_key)
return await orchestrator.final_result(run_id)
```

- [ ] **Step 7: 写报告封存后、Session 关闭前中断的恢复测试，确认不重新探索页面且能够完成 `session_close`**

- [ ] **Step 8: 运行恢复、长图、广告和 runtime composition 回归并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_runtime_recovery.py services/api/tests/test_web_full_page_supplemental_capture.py services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_runtime_composition.py -q`

```text
[AI编程] feat(web): 接入可恢复探索状态机

模块：Web 探索执行与证据阶段；数据库迁移：使用 checkpoint 表；前端菜单/路由：不涉及；风险：中断恢复与重复副作用。
```

### Task 7: 重构全站点通用详情主体事实

**Files:**
- Modify: `services/api/app/application/web_primary_subject_facts.py`
- Create: `services/api/tests/fixtures/web_detail/video_primary_with_recommendations.html`
- Create: `services/api/tests/fixtures/web_detail/article_with_inline_video.html`
- Create: `services/api/tests/fixtures/web_detail/gallery_primary.html`
- Create: `services/api/tests/fixtures/web_detail/list_with_repeated_primary_cards.html`
- Create: `services/api/tests/fixtures/web_detail/vendor_neutral_iframe_player.html`
- Create: `services/api/tests/fixtures/web_detail/conflicting_primary_regions.html`
- Create: `services/api/tests/test_web_detail_subject_regions.py`
- Test: `services/api/tests/test_web_v8_primary_subject_capture.py`

**Interfaces:**
- Produces: `WebSubjectRegionFacts`，包含主体、正文、主播放器、正文内媒体、主卡片和次要推荐计数。
- Consumes: 脱敏 DOM、可选元素几何摘要和导航来源事实。

- [ ] **Step 1: 写主播放器位于 article 内且有 12 个推荐卡片的失败测试**

```python
def test_player_inside_article_remains_primary_and_recommendations_are_secondary() -> None:
    facts = extract_subject_region_facts(load_fixture("video_primary_with_recommendations.html"))
    assert facts.primary_subject_kind == "video"
    assert facts.primary_player_count == 1
    assert facts.repeated_primary_card_count == 0
    assert facts.secondary_recommendation_card_count == 12
```

- [ ] **Step 2: 写文章正文内嵌视频不成为主播放器的失败测试**

```python
def test_inline_article_video_is_not_primary_player() -> None:
    facts = extract_subject_region_facts(load_fixture("article_with_inline_video.html"))
    assert facts.primary_subject_kind == "article"
    assert facts.primary_player_count == 0
    assert facts.inline_article_media_count == 1
    assert facts.primary_article_text_length >= 800
```

- [ ] **Step 3: 写真正列表仍保留主重复卡片的失败测试**

```python
def test_repeated_cards_in_main_region_remain_list_evidence() -> None:
    facts = extract_subject_region_facts(load_fixture("list_with_repeated_primary_cards.html"))
    assert facts.repeated_primary_card_count == 8
    assert facts.secondary_recommendation_card_count == 0
```

- [ ] **Step 4: 写没有播放器产品 token 的 iframe/控件组合仍识别为主播放器的失败测试**

```python
def test_vendor_neutral_player_evidence_does_not_require_library_name() -> None:
    facts = extract_subject_region_facts(load_fixture("vendor_neutral_iframe_player.html"))
    assert facts.primary_subject_kind == "video"
    assert facts.primary_player_count == 1
    assert facts.player_vendor_tokens == ()
```

- [ ] **Step 5: 运行新测试并确认现有提取器失败**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_detail_subject_regions.py -q`

- [ ] **Step 6: 实现区域角色、祖先边界、媒体/控件/几何组合证据和推荐区域识别；播放器产品 token 只能增加证据，不能成为必要条件**

```python
primary_player = media_evidence.is_present and (
    media_evidence.in_primary_region
    and media_evidence.area_ratio >= policy.primary_media_area_ratio
    and not media_evidence.inside_secondary_region
)
```

- [ ] **Step 7: 保存 selector/token 摘要但不保存正文内容，并运行主体事实回归**

```python
audit = SubjectFactAudit(
    selector_summaries=facts.selector_summaries,
    token_summaries=facts.token_summaries,
    text_content=None,
)
```

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_detail_subject_regions.py services/api/tests/test_web_v8_primary_subject_capture.py -q`

- [ ] **Step 8: 提交**

```text
[AI编程] refactor(web): 提取通用详情主体区域事实

模块：Web DOM 结构事实；数据库迁移：否；前端菜单/路由：不涉及；风险：详情主体与相关推荐边界。
```

### Task 8: 用主体事实统一判定全部 Web 详情页

**Files:**
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Modify: `services/api/app/infrastructure/db/web_report_page_snapshot.py`
- Modify: `services/api/app/application/web_report_explanation.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_page_rule_capture_regressions.py`
- Test: `services/api/tests/test_web_report_explanation.py`
- Create: `services/api/tests/test_web_detail_metamorphic.py`

**Interfaces:**
- Consumes: `WebSubjectRegionFacts`。
- Produces: 可审计 `content_detail`、`video_detail`、list 或真实 `unknown` 决策。
- Preserves: 历史报告按冻结 Catalog 只读解释，不把历史 Catalog 装配进当前分类执行器。

- [ ] **Step 1: 写四类页型边界失败测试**

```python
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("video_primary_with_recommendations.html", "video_detail"),
        ("article_with_inline_video.html", "content_detail"),
        ("gallery_primary.html", "content_detail"),
        ("list_with_repeated_primary_cards.html", "content_list"),
    ],
)
def test_page_type_uses_primary_region_not_total_card_count(fixture: str, expected: str) -> None:
    assert classify_fixture(fixture).final_page_type == expected
```

- [ ] **Step 2: 写证据不足和强冲突仍为 unknown 的反例测试**

```python
def test_empty_or_conflicting_primary_region_remains_unknown() -> None:
    result = classify_fixture("conflicting_primary_regions.html")
    assert result.final_page_type == "unknown"
    assert result.reason_code == "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT"
```

- [ ] **Step 3: 写同一 DOM 随机替换域名、路径、标题和非语义 class 后页型不变的变形测试**

```python
def test_detail_classification_is_invariant_under_site_identity_mutation() -> None:
    original = classify_fixture("video_primary_with_recommendations.html")
    mutated = classify_html(
        mutate_site_identity(
            load_fixture("video_primary_with_recommendations.html"),
            origin="https://never-cataloged.example",
            path="/random/subject-42",
        )
    )
    assert mutated.final_page_type == original.final_page_type == "video_detail"
    assert mutated.structure_fingerprint == original.structure_fingerprint
```

- [ ] **Step 4: 运行分类测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_rule_capture_regressions.py -q`

- [ ] **Step 5: 发布新 Catalog 规则；详情强证据优先于次要推荐卡片数量**

```json
{
  "rule": "video_detail_primary_subject",
  "requires": ["single_primary_subject", "primary_player"],
  "ignores_as_list_conflict": ["secondary_recommendations", "sidebar_navigation", "advertisement"]
}
```

- [ ] **Step 6: assessment 和报告保存全部主体事实实际值、结构指纹及证据摘要**

- [ ] **Step 7: 扫描生产代码，确认无三个回归域名、`/archives/` 或站点专属选择器**

Run: `rg -n "chigua\.com|heiliao\.com|hl365\.com|/archives/" services/api/app`

Expected: 允许的命中仅为反硬编码校验文本或不参与执行的说明；分类、导航和状态机不得命中。

- [ ] **Step 8: 运行分类、变形、Catalog、报告和历史只读解释回归并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_rule_capture_regressions.py services/api/tests/test_web_detail_metamorphic.py services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_report_explanation.py services/api/tests/test_web_run_policy_snapshot.py -q`

```text
[AI编程] fix(web): 统一全部详情页主体分类

模块：Web 页面分类与报告；数据库迁移：否；前端菜单/路由：不涉及；风险：详情与列表、正文视频与主播放器边界。
```

### Task 9: 通用长页面稳定与有界证据

**Files:**
- Create: `services/api/app/application/web_full_page_stabilization.py`
- Modify: `services/api/app/application/web_full_page_supplemental_capture.py`
- Create: `services/api/tests/test_web_full_page_long_document.py`
- Test: `services/api/tests/test_web_full_page_supplemental_capture.py`
- Test: `services/api/tests/test_web_full_page_supplemental_postgres.py`

**Interfaces:**
- Produces: `WebFullPageStabilizationPolicy(max_document_height, max_scroll_steps, stable_rounds, lazy_load_timeout_ms)`。
- Produces: `stabilize_for_full_page(page, policy) -> WebFullPageStabilizationResult`。
- Produces: `WebFullPageStabilizationResult(document_height, scroll_steps, sticky_layers, state_hash, limit_exceeded)`。

- [ ] **Step 1: 写 lazy-load 高度连续稳定后才截图的失败测试**

```python
async def test_lazy_loaded_document_requires_consecutive_stable_rounds() -> None:
    page.document_heights = [1200, 2400, 3600, 3600, 3600]
    result = await stabilize_for_full_page(page, policy(stable_rounds=2))
    assert result.document_height == 3600
    assert result.scroll_steps == 4
```

- [ ] **Step 2: 写超高/无限滚动页面到达上限后形成明确 gap 的失败测试**

```python
async def test_unbounded_document_stops_at_frozen_height_limit() -> None:
    result = await capture_full_page(growing_forever_page(), policy(max_document_height=20_000))
    assert result.status == "failed"
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    assert result.primary_capture_remains_usable is True
```

- [ ] **Step 3: 写 sticky 层只能在语义不变且动作被审计时临时隐藏的失败测试**

```python
async def test_sticky_layer_mutation_requires_semantic_equivalence_and_audit() -> None:
    result = await capture_full_page(page_with_sticky_header(), policy())
    assert result.sticky_layer_audit.selector == "header.sticky"
    assert result.sticky_layer_audit.semantic_hash_before == result.sticky_layer_audit.semantic_hash_after
```

- [ ] **Step 4: 运行长页面测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_full_page_long_document.py -q`

- [ ] **Step 5: 实现滚动稳定、lazy-load 等待、sticky 审计、高度上限和可选分段截图；不得改变业务 current Capture**

```python
stabilized = await stabilize_for_full_page(page, policy)
if stabilized.limit_exceeded:
    return FullPageOutcome.gap("WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED", stabilized.audit)
return await capture_without_advancing_current(page, stabilized)
```

- [ ] **Step 6: 运行长图单元/PostgreSQL/恢复回归并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_full_page_long_document.py services/api/tests/test_web_full_page_supplemental_capture.py services/api/tests/test_web_full_page_supplemental_postgres.py services/api/tests/test_web_runtime_recovery.py -q`

```text
[AI编程] feat(web): 增加通用长页面有界证据

模块：Web 长图稳定与证据；数据库迁移：否；前端菜单/路由：不涉及；风险：无限滚动和 sticky 层语义漂移。
```

### Task 10: 构建快速离线回放与 Codex 实时增量报告器

**Files:**
- Create: `scripts/validation/web_v2_live_acceptance.py`
- Create: `services/api/tests/test_web_v2_live_acceptance_reporter.py`
- Create: `services/api/tests/test_web_controlled_two_origin_acceptance.py`
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Produces: `SiteMinimumWitnessPolicy`，要求根导航或可解释外部失败、首页证据/gap、候选安全过滤、无非终态 checkpoint 和完整收口。
- Produces: `SuiteCapabilityMatrix`，聚合列表结构簇、详情结构簇、年龄门、弹窗、跨域恢复、广告隔离和长页面见证。
- Produces: `collect_delta(cursor) -> ValidationDelta` 和 Codex 验证期间的实时 Markdown/JSONL 增量；不改动生产报告调度。
- Produces: 每站 `site-summary.json`、`events.jsonl`、`report.md`，只引用 Artifact ID/本地证据路径，不复制原始 DOM。

- [ ] **Step 1: 写增量报告不重复 URL/动作/Artifact 的失败测试**

```python
def test_reporter_emits_only_records_after_cursor() -> None:
    first = reporter.collect_delta(cursor=None)
    second = reporter.collect_delta(cursor=first.next_cursor)
    assert first.event_ids
    assert set(first.event_ids).isdisjoint(second.event_ids)
```

- [ ] **Step 2: 写单站最低门槛与三站合并能力矩阵分离的失败测试**

```python
def test_site_close_uses_minimum_witness_while_suite_uses_capability_matrix() -> None:
    state = acceptance_state(site_a=complete_minimum(), site_b=incomplete_minimum())
    assert state.should_close("site_a.example") is True
    assert state.should_close("site_b.example") is False
    assert state.suite_capabilities.detail_structure_clusters >= 4
```

- [ ] **Step 3: 写外部 TLS 与代码失败分栏统计的失败测试**

```python
def test_external_tls_failure_is_not_reported_as_classifier_regression() -> None:
    item = classify_validation_failure(error_code="WEB_GOTO_TLS_FAILED", response_received=False)
    assert item.category == "external_transport"
    assert item.blocks_code_acceptance is False
```

- [ ] **Step 4: 写受控双 origin 测试，覆盖普通跨域不发现 DOM、广告 href 隔离、广告 click 拒绝和来源恢复**

```python
async def test_controlled_origins_prove_cross_origin_and_ad_contracts(two_origin_site) -> None:
    result = await explore(two_origin_site.source_url)
    assert two_origin_site.source_origin not in load_legacy_registry_origins()
    assert two_origin_site.foreign_internal_url not in result.frontier_urls
    assert result.ad_pages[0].browser_context_id != result.business_context_id
    assert result.ad_click_attempt.error_code == "WEB_AD_CLICK_FORBIDDEN"
    assert result.source_recovery.trusted is True
```

- [ ] **Step 5: 运行报告器和双 origin 测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_v2_live_acceptance_reporter.py services/api/tests/test_web_controlled_two_origin_acceptance.py -q`

- [ ] **Step 6: 实现只读查询、实时增量、单站最低门槛、合并能力矩阵和受控收口；不在生产执行器中写定时报表或站点专属流程**

```python
delta = await reporter.collect_delta(cursor=cursor)
await reporter.write_jsonl(delta)
cursor = delta.next_cursor
for site in state.sites_ready_to_close_without_losing_capability_coverage():
    await controller.close_site(site)
```

- [ ] **Step 7: 更新专项规则，固定动态 href、可信 Capture、frontier 防爆、广告禁 click、checkpoint、通用详情、长页面和分层验收回归项**

- [ ] **Step 8: 运行报告器和受控浏览器测试并提交**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_v2_live_acceptance_reporter.py services/api/tests/test_web_controlled_two_origin_acceptance.py -q`

```text
[AI编程] test(web): 增加分层实况验收报告器

模块：Web 验证工具与专项规则；数据库迁移：否；前端菜单/路由：不涉及；风险：外站数据变化和单 Worker 排队。
```

### Task 11: 完整 Web 回归和代码审核

**Files:**
- Create: `docs/reports/2026-09-14-web-v2-runtime-hardening-code-review.md`
- Modify only if findings require: 本计划列出的 Web 文件和测试

**Interfaces:**
- Produces: 审核结论、问题级别、修复 commit、剩余风险和验证命令。

- [ ] **Step 1: 运行新增精确回归**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_config_env_loading.py services/api/tests/test_web_policy_creation_boundary.py services/api/tests/test_web_frontier_scheduler.py services/api/tests/test_web_trusted_capture_discovery.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_run_stage_checkpoint.py services/api/tests/test_web_run_stage_checkpoint_postgres.py services/api/tests/test_web_runtime_recovery.py services/api/tests/test_web_detail_subject_regions.py services/api/tests/test_web_detail_metamorphic.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_full_page_long_document.py services/api/tests/test_web_v2_live_acceptance_reporter.py services/api/tests/test_web_controlled_two_origin_acceptance.py -q`

- [ ] **Step 2: 运行完整 Web 测试集合**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_*.py -q`

- [ ] **Step 3: 运行只读 App/H5 边界回归并确认没有改动其生产代码**

Run: `git diff --name-only codex/web-catalog-home-menu-depth...HEAD`

Expected: 不包含 App/H5 生产实现路径。

- [ ] **Step 4: 审核可信 Capture、frontier 污染/陷阱、广告安全、历史兼容、并发 CAS、幂等副作用、分类变形不变量、长页面和 gap 语义**

- [ ] **Step 5: 修复所有 P0/P1，以及会破坏本计划 Completion Gate 的 P2；每个修复增加失败测试**

- [ ] **Step 6: 重新运行 Step 1-3，并将实际输出、commit 和未覆盖风险写入审核报告**

- [ ] **Step 7: 提交审核修复和报告**

```text
[AI编程] fix(web): 收口运行时优化审核问题

模块：Web 运行时审核修复；数据库迁移：记录实际结果；前端菜单/路由：不涉及；风险：报告中列明。
```

### Task 12: 三站分层实况探索验收

**Files:**
- Create: `outputs/web-v2-runtime-hardening-20260914/chigua.com/`
- Create: `outputs/web-v2-runtime-hardening-20260914/heiliao.com/`
- Create: `outputs/web-v2-runtime-hardening-20260914/hl365.com/`
- Create: `docs/reports/2026-09-14-web-v2-runtime-hardening-live-validation.md`

**Interfaces:**
- Consumes: 健康 API、Worker、数据库和三个 URL。
- Produces: Codex 实时增量、每站最低见证报告、三站合并能力矩阵和最终对比报告。

- [ ] **Step 1: 运行迁移、API/Worker 健康、可用 Worker 数和成年声明冻结前置检查**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe scripts/validation/web_v2_live_acceptance.py preflight --require-dynamic-policy --require-menu-limit 10 --require-trusted-capture --require-frontier-policy --require-ad-href-only --require-checkpoint --require-two-origin-acceptance`

- [ ] **Step 2: 提交三个 exploration-only Run；调度并发度使用 `min(worker_count, 3)`**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe scripts/validation/web_v2_live_acceptance.py start --site https://chigua.com/ --site https://heiliao.com/ --site https://hl365.com/ --output outputs/web-v2-runtime-hardening-20260914`

- [ ] **Step 3: 实时打印新增事件，直到各站达到最低见证或超时**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe scripts/validation/web_v2_live_acceptance.py monitor --output outputs/web-v2-runtime-hardening-20260914 --interval-seconds 2 --minimum-site-witness --suite-capability-matrix --site-timeout-minutes 30`

- [ ] **Step 4: 对达到单站最低见证且其当前结构簇不再补充合并矩阵的站点执行受控收口**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe scripts/validation/web_v2_live_acceptance.py close-complete --output outputs/web-v2-runtime-hardening-20260914`

- [ ] **Step 5: 对未完成最低见证或仍承载缺失能力矩阵项的站点执行一次最长 30 分钟延长，不修改站点配置和生产流程**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe scripts/validation/web_v2_live_acceptance.py extend-incomplete --output outputs/web-v2-runtime-hardening-20260914 --minutes 30`

- [ ] **Step 6: 生成最终报告并验证证据文件/Artifact 引用存在**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe scripts/validation/web_v2_live_acceptance.py report --output outputs/web-v2-runtime-hardening-20260914 --verify-artifacts`

- [ ] **Step 7: 报告逐站输出以下事实**

```text
Run/Task ID、请求 URL、最终 URL、页面类型、置信度、主体结构事实、父子 Capture、
DOM Artifact、视口截图、全页截图或 gap、广告 ad_page、动作、重试、阶段游标、
外部 TLS/网络问题、可信来源拒绝、frontier 规范化/结构簇/预算、冻结 menu_limit、
已执行唯一菜单数、菜单超额聚合数、发现/访问/覆盖数量、
单站最低见证、合并能力矩阵、受控双 origin 结果、最终收口原因。
```

- [ ] **Step 8: 提交验证报告；原始运行产物是否纳入 Git 按仓库现有规则执行**

```text
[AI编程] test(web): 完成三站分层探索验收

模块：Web 实况验证；数据库迁移：已验证 0114；前端菜单/路由：不涉及；风险：外站 TLS 和页面内容具有时效性。
```

## Completion Gate

- [ ] 当前分支仍为 `codex/web-v2-dynamic-exploration-evidence`。
- [ ] 新建、历史重跑和重新探索都创建当前版本的新 Run，动态 href 发现不能通过 API 参数或冻结策略关闭。
- [ ] 新请求携带任一旧执行覆盖字段均返回 `422 WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN`，没有静默归一化。
- [ ] 普通导航只执行可信 DOM 中的同源 HTTP(S) href。
- [ ] 初始 canonical site scope 只允许受控绑定，跨域落地 DOM、空 href 和危险 URL 不进入 frontier。
- [ ] frontier 具有规范化、重定向别名、陷阱检测、结构聚类和冻结预算；预算耗尽记录 `budget_not_executed`。
- [ ] `ALIOTH_WEB_MENU_COLLECTION_LIMIT` 默认 10、允许 `1..1000`，新 Run 冻结配置且历史 Run 不回写。
- [ ] 每个新 Run 实际采集的唯一同源菜单不超过冻结额度；首页、详情、广告、阻断、别名和重复导航不占额度。
- [ ] 首页和每个后续稳定可信页面均发现一次；二级菜单可入队，全局导航重复项只追加 lineage。
- [ ] 广告 click 在底层被拒绝，广告只使用 href 和独立 Playwright 上下文。
- [ ] `direct_ad_page_enabled` 和旧 Registry 不影响新 Run 主路径。
- [ ] 历史 Run 只能只读查看；重跑生成新 Task ID 和 Run ID，并记录现有 `source_task_id`、`source_operation=rerun`，非终态旧 Run 不再被 Worker claim。
- [ ] 每个运行阶段有持久化游标、CAS 和幂等恢复。
- [ ] checkpoint 覆盖 frontier 排空、证据对账、Coverage、报告封存和 Session 关闭。
- [ ] GOTO/TLS、弹窗和长图失败按矩阵重试或记录 gap，并在安全时继续剩余候选。
- [ ] 所有 Web 详情页按通用主体区域事实分类，无站点/路径硬编码。
- [ ] `heiliao` 16 个已知详情只是回归样本；同结构的任意站点均能得到相同结果。
- [ ] 明确主播放器详情为 `video_detail`，正文内嵌视频不否决 `content_detail`。
- [ ] 同一详情 DOM 随机替换域名、路径、标题和非语义 class 后分类不变。
- [ ] 通用长页面具备 lazy-load 稳定、sticky 审计、高度上限和明确 gap。
- [ ] App/H5 生产代码无改动且边界回归不变。
- [ ] 代码审核无未解决 P0/P1 或阻断验收的 P2。
- [ ] 三站 Codex 实时增量、证据引用和最终报告均已生成。
- [ ] 受控双 origin 验证广告隔离、底层禁 click、跨域 DOM 隔离和来源恢复。
- [ ] 冷启动随机 origin 不增加 Registry/fixture 配置即可完成动态发现。
- [ ] 达到单站最低见证且不再贡献缺失能力矩阵的站点及时收口，只有必要站点执行延长探索。
