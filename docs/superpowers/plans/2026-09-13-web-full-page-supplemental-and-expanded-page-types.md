# Web Full-page Supplemental and Expanded Page Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 只为 Web V2 探索增加视口主截图后的全页补充截图，并将 Web 正式分类扩展为七类业务页、一个生命周期页和一个兜底页，同时保持 App/H5 代码和运行语义不变。

**Architecture:** 新增 Web 专属长图补采服务，在现有 Surface Session 命令门禁内执行一次只读 `OBSERVE + full_page=true`，以来源 ready Capture 为幂等键保存 `supplemental` 子 Capture，且不推进会话当前指针。Web Catalog 继续使用 `page-type-catalog.v2`，但由 Web 专属模型校验九类与三组语义；分类器消费来源 Capture 的 DOM、视口截图以及可选长图。年龄门在关闭前独立落正式生命周期判定，搜索与活动入口走 Web 专属辅助探索链；覆盖率 v4 只以七类业务页为适用性全集，旧 v1/v2 四类和 v3 五类快照冻结恢复。

**Tech Stack:** Python 3.12、FastAPI、dataclasses、PostgreSQL JSONB、Microsoft Agent Framework、Playwright、pytest。

**Spec:** `docs/superpowers/specs/2026-09-13-web-full-page-supplemental-and-expanded-page-types-design.md`

## Global Constraints

- 本次只允许改变 Web V2 行为。禁止修改 `services/api/app/application/app_*`、`services/api/app/application/h5_*`、`services/api/app/domain/exploration/app_*`、`services/api/app/domain/exploration/h5_*`、App/H5 Catalog、App/H5 分类器与 App/H5 报告合同。
- 禁止修改共享 `services/api/app/domain/page/page_type_catalog.json`、`catalog.py`、`registry.py` 来容纳 Web 的 `unknown`；`neither` 分组必须只存在于 Web 专属模型。
- 共享 `page_capture_tool` 的参数默认值保持不变。浏览器共享层只能新增由 `web_capture_stage="web_full_page_supplemental"` 显式触发的行为，`web_capture_stage=None` 与所有现有 H5 stage 输出必须逐字节保持原语义。
- 主截图始终是 `full_page=false` 的 ready/primary Capture；长图是 `capture_role="supplemental"` 的子 Capture，不能成为动作来源、不能继承为当前页面、不能替代主截图。
- 每个“agent Run + 来源 ready Capture + `web_full_page_supplemental`”最多消费一次；成功、失败和不确定结果均不得无限重试。
- Web 正式分类阈值固定为 `minimum_confidence=0.7`。成年门分类阈值是 `0.7`，自动点击确认仍保留更严格的 `0.8` 动作阈值。
- 业务类型固定为 `home`、`content_list`、`content_detail`、`video_list`、`video_detail`、`activity`、`search_page`；生命周期类型固定为 `age_gate`；兜底类型固定为 `unknown`。
- `activity`、`search_page` 只有在站点适用性证据成立时才进入 required/optional；`age_gate` 与 `unknown` 永不进入业务覆盖率分母。
- 菜单仍只执行具有有效 HTTP(S) `href` 的同源安全链接，并用当前 Playwright 页面 `goto(canonical_url)`；不增加无 `href` 菜单点击。
- 搜索探索不得点击、输入、粘贴、选择联想词、按回车或提交表单。只有 DOM 中带有效同源 HTTP(S) `href` 的搜索入口可用当前页面 `goto(canonical_url)`；无 `href` 控件不执行。此约束取代下方 Task 6 初稿中的 `search_control` 点击设计。
- 同源活动 `href` 使用当前页面直接导航；跨域活动/推广只进入既有隔离广告落地证据链。
- 不新增数据库迁移。新增生命周期、长图和搜索/活动审计字段写入现有 JSONB 或复用 `qc_surface_phase_run`；若现有表无法表达必须字段，停止实施并回到设计复核。
- 所有新增注释使用中文，文件保持 UTF-8。
- 每项行为先写失败测试并观察 RED，再写最小实现并观察 GREEN。
- 遵照用户要求，本计划和后续实施都不得执行 `git commit`。每个任务用 `git diff --check`、精确测试与 `git status --short` 形成审查检查点。

---

## File Structure Map

### New Web-only files

- `services/api/app/application/web_full_page_supplemental_capture.py`：长图命令、结果、错误映射、只读采集服务和 ready reader 装饰器。
- `services/api/app/infrastructure/db/web_full_page_supplemental_claim_repository.py`：使用 `qc_surface_phase_run` 为每个来源 Capture 提供一次性 claim/finish。
- `services/api/app/application/web_auxiliary_navigation.py`：搜索入口与同源活动入口的候选、一次性动作和到达结果；不复用普通菜单点击。
- `services/api/tests/test_web_full_page_supplemental_capture.py`：长图服务与 reader 装饰器单元测试。
- `services/api/tests/test_web_full_page_supplemental_postgres.py`：长图 claim、父子 Capture、当前指针不漂移的 PostgreSQL 集成测试。
- `services/api/tests/test_web_auxiliary_navigation.py`：搜索零提交、活动同源导航和跨域隔离测试。

### Existing Web-only files to modify

- `services/api/app/domain/page/web_page_type_catalog.json`：发布 `2026-09-13.web.v8` 九类目录、分组、媒体规则与详情原则。
- `services/api/app/domain/page/web_page_knowledge.py`：Web 分组、媒体规则、notes 的强类型模型。
- `services/api/app/domain/page/web_page_catalog.py`：九类严格解析、五类历史恢复和 Web 专属分组常量。
- `services/api/app/domain/page/web_site_menu_registry.py`：站点适用性只覆盖七类业务页；分类提示允许新增业务页型。
- `services/api/app/application/web_page_classification.py`：新增结构事实、页型优先级、长图证据引用和 `unknown` 兜底。
- `services/api/app/application/web_assessment_evidence.py`：验证 supplemental 子 Capture 谱系及长图 PNG 限制。
- `services/api/app/agents/explorer/web_assessment_agent.py`：向页面分类模型传入长图和新增规则，保留搜索零提交约束。
- `services/api/app/infrastructure/db/web_assessment_evidence_repository.py`：读取 artifact 的 Capture role、父 Capture 和用途。
- `services/api/app/application/web_page_type_assessment.py`：发布 assessment v4，支持 `age_gate` 与 `unknown` 的不同终态语义。
- `services/api/app/infrastructure/db/web_capture_evidence_repository.py`：严格校验 v4 九类 payload 和 supplemental 引用。
- `services/api/app/application/web_age_gate.py`：关闭前记录 `age_gate`，关闭后保留动作阈值与双 Capture 谱系。
- `services/api/app/application/web_runtime_evidence.py`：把 supplemental 状态投影到 ready/age evidence。
- `services/api/app/application/web_runtime_composition.py`：只在 Web V2 组合根装配长图、年龄门记录和辅助探索。
- `services/api/app/application/web_run_policy.py`：冻结 Web 长图策略；旧策略缺失时默认关闭。
- `services/api/app/domain/exploration/web_coverage_snapshot.py`：v4 七类业务分母及 lifecycle/unknown/supplemental 摘要。
- `services/api/app/application/web_page_applicability.py`：required/optional/not_applicable 只分区七类业务页。
- `services/api/app/domain/page/web_site_menu_catalog.json`：只更新 Web 站点的七类业务适用性分区；不把它继续作为页型判定依据。
- `services/api/app/application/web_inspection_reconciliation.py`：使用 v4 业务分组，不把 lifecycle/neither 当缺口。
- `services/api/app/application/web_page_inspection_notification.py`：只接受并投影 Web assessment v4，不改变 App/H5 通知路径。
- `services/api/app/infrastructure/db/web_coverage_snapshot_row.py`：从现有 `applicability_snapshot` JSONB 还原 v4 扩展字段。
- `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`：持久化 v4 扩展 JSONB，不改表。
- `services/api/app/infrastructure/db/web_coverage_repository.py`：观测页型按九类归档，覆盖聚合只读七类业务页。
- `services/api/app/infrastructure/db/web_report_page_snapshot.py`：报告页快照附带 supplemental、年龄门和 unknown 状态。
- `services/api/app/application/web_report_explanation.py`：报告解释区分主截图、长图成功/失败/未尝试。
- `services/api/app/application/report_insight.py`：仅扩展 `_web_report_logical_pages()` 的 Web V2 分支；H5/App 分支保持原样。
- `services/api/app/api/v2/schemas.py`、`services/api/app/application/workstation_tasks.py`：仅在 Web V2 校验/投影分支让客户端 required types 使用七类业务页；App/H5 分支与序列化结果不变。
- `docs/专项能力与高频回归规则.md`：记录 Web-only 双截图与九类页型回归规则。

### Shared browser files with Web-gated additions only

- `services/api/app/infrastructure/browser/page_capture.py`：仅新增 `web_full_page_supplemental` stage 与只读截图状态指标，不改变其他 stage。
- `services/api/app/infrastructure/browser/semantic_target_resolver.py`：仅新增 `target_mode="web_search_discovery_candidate"` 的 Web 密封目标解析。

---

### Task 1: Web 九类 Catalog 与三组单一事实源

**Files:**
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Modify: `services/api/app/domain/page/web_page_knowledge.py`
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/page/__init__.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`
- Test: `services/api/tests/test_web_run_policy_snapshot.py`

**Interfaces:**
- Consumes: 现有 `PageTypeCatalog`/`PageTypeDefinition` 只作为数据容器；不得调用会拒绝 `category="neither"` 的共享 `PageTypeRegistry` 校验新 Web 目录。
- Produces: `WebPageTypeGroups`、`WebPageMediaRules`、扩展后的 `WebPageTypeCatalog`，以及 `WEB_BUSINESS_PAGE_TYPE_CODES`、`WEB_LIFECYCLE_PAGE_TYPE_CODES`、`WEB_NEITHER_PAGE_TYPE_CODES`、`WEB_PAGE_TYPE_CODES`。

- [ ] **Step 1: 写九类与分组失败测试**

在 `test_web_page_type_catalog.py` 增加：

```python
EXPECTED_WEB_PAGE_TYPES = (
    "home", "content_list", "content_detail", "video_list", "video_detail",
    "activity", "search_page", "age_gate", "unknown",
)


def test_web_catalog_publishes_nine_types_and_exact_groups() -> None:
    catalog = load_web_page_type_catalog()
    assert catalog.codes == EXPECTED_WEB_PAGE_TYPES
    assert catalog.page_type_groups.business == EXPECTED_WEB_PAGE_TYPES[:7]
    assert catalog.page_type_groups.lifecycle == ("age_gate",)
    assert catalog.page_type_groups.neither == ("unknown",)
    assert catalog.require_code("unknown").category == "neither"
    assert "profile" not in catalog.require_code("activity").confusable_with


def test_web_detail_media_rules_are_loaded_not_ignored() -> None:
    catalog = load_web_page_type_catalog()
    assert catalog.media_rules["content_detail"]["image_gallery_or_viewer"] == (
        "支持判定为 content_detail"
    )
    assert catalog.media_rules["video_detail"]["player_shell_without_resource"] == (
        "不支持 video_detail"
    )
    assert "图片查看器、图集、漫画翻页 → content_detail" in (
        catalog.detail_classification_principles
    )
```

参数化删除 `page_type_groups`、`detail_classification_principles`、`notes`、任一 `media_rules`，或把 `unkonw`、悬空 `profile` 放入目录，断言 `WebPageTypeCatalogError`。

- [ ] **Step 2: 运行 Catalog 测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py -q`

Expected: FAIL，当前目录只有五类且解析器拒绝新增根字段。

- [ ] **Step 3: 增加 Web 专属分组和媒体模型**

在 `web_page_knowledge.py` 增加以下精确接口：

```python
@dataclass(frozen=True, slots=True)
class WebPageTypeGroups:
    business: tuple[str, ...]
    lifecycle: tuple[str, ...]
    neither: tuple[str, ...]

    @property
    def all_codes(self) -> tuple[str, ...]:
        return (*self.business, *self.lifecycle, *self.neither)


WebPageMediaRules = Mapping[str, Mapping[str, str]]
```

给 `WebPageTypeCatalog` 增加 `page_type_groups`、`media_rules`、`detail_classification_principles`、`notes`。Web 解析器自行验证 category/coverage 分组，不修改共享 `catalog.py` 或 `registry.py`。

- [ ] **Step 4: 发布 v8 JSON 内容**

把版本设为 `2026-09-13.web.v8`，根分组固定为：

```json
"page_type_groups": {
  "business": ["home", "content_list", "content_detail", "video_list", "video_detail", "activity", "search_page"],
  "lifecycle": ["age_gate"],
  "neither": ["unknown"]
}
```

保留现有五类全部字段；逐字段加入附件中的 `content_detail.media_rules`、`video_detail.media_rules`、六条 `detail_classification_principles`，并补齐 `activity`、`search_page`、`age_gate`、`unknown` 的通用 Catalog 必填字段。具体分组值固定为：七类业务 `category="business"`，`age_gate.category="lifecycle"`，`unknown.category="neither"`；`coverage_required` 只为 `home` 与现有站点适用性明确必达的基础业务类型保留现状，`activity/search_page/age_gate/unknown` 均为 `false`。

`activity.confusable_with` 只允许九类内的 `home`、`age_gate`；“个人中心或纯登录页”放入 `strong_negative_signals`，不保留悬空 `profile`。错误拼写 `unkonw` 不得出现在 JSON。

- [ ] **Step 5: 实现严格 Web 解析与历史五类适配**

在 `web_page_catalog.py` 中：

```python
WEB_V3_BUSINESS_PAGE_TYPE_CODES = (
    "home", "content_list", "content_detail", "video_list", "video_detail",
)
WEB_BUSINESS_PAGE_TYPE_CODES = (
    *WEB_V3_BUSINESS_PAGE_TYPE_CODES, "activity", "search_page",
)
WEB_LIFECYCLE_PAGE_TYPE_CODES = ("age_gate",)
WEB_NEITHER_PAGE_TYPE_CODES = ("unknown",)
WEB_PAGE_TYPE_CODES = (
    *WEB_BUSINESS_PAGE_TYPE_CODES,
    *WEB_LIFECYCLE_PAGE_TYPE_CODES,
    *WEB_NEITHER_PAGE_TYPE_CODES,
)
```

`parse_web_page_type_catalog()` 对 v8 要求九类和完整根字段；`legacy_frozen=True` 时保留现有 v1-v4 四类解析，并为 `2026-09-11.web.v5` 至 `2026-09-13.web.v7` 使用固定五类解析。历史解析不得合成 v8 媒体规则或新页型。

- [ ] **Step 6: 运行测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_run_policy_snapshot.py -q`

Expected: PASS；旧 fixture 仍按冻结集合恢复。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/domain/page/web_page_knowledge.py services/api/app/domain/page/web_page_catalog.py services/api/app/domain/page/web_page_type_catalog.json services/api/tests/test_web_page_type_catalog.py`

Expected: 无输出；`git status --short` 仅显示工作区改动，不产生提交。

---

### Task 2: Web 全页 supplemental 采集与一次性 claim

**Files:**
- Create: `services/api/app/application/web_full_page_supplemental_capture.py`
- Create: `services/api/app/infrastructure/db/web_full_page_supplemental_claim_repository.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Test: `services/api/tests/test_web_full_page_supplemental_capture.py`
- Test: `services/api/tests/test_web_full_page_supplemental_postgres.py`
- Regression: `services/api/tests/test_h5_visual_viewport_capture.py`
- Regression: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Consumes: `PageCaptureBrowser`、`PageCapturePersistenceService`、`SessionCommandGate`、`PageCaptureRequest` 和现有 `qc_surface_phase_run` 唯一键。
- Produces: `WebFullPageSupplementalCommand`、`WebFullPageSupplementalResult`、`WebFullPageSupplementalCaptureService.capture(command)`、`PostgresWebFullPageSupplementalClaimRepository.claim()/finish()`。

- [ ] **Step 1: 写只读双图、失败降级和幂等测试**

新建 `test_web_full_page_supplemental_capture.py`，核心断言：

```python
@pytest.mark.asyncio
async def test_captures_one_full_page_child_without_advancing_primary() -> None:
    result = await service.capture(command)
    request = browser.requests[0]
    assert request.mode is PageCaptureMode.OBSERVE
    assert request.capture_dom is False
    assert request.capture_visible_text is False
    assert request.capture_screenshot is True
    assert request.full_page is True
    assert request.web_capture_stage == "web_full_page_supplemental"
    assert persistence.calls[0]["parent_page_capture_id"] == command.source_capture_id
    assert persistence.calls[0]["capture_role"] == "supplemental"
    assert persistence.calls[0]["inherit_parent_semantics"] is False
    assert result.status == "captured"


@pytest.mark.asyncio
async def test_state_drift_fails_without_persisting_or_retrying() -> None:
    browser.result = _result(final_url="https://example.test/changed", full_page=True)
    first = await service.capture(command)
    second = await service.capture(command)
    assert first.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert second.status == "already_attempted"
    assert persistence.calls == []
```

再覆盖 timeout、PNG 超限/Playwright size error、持久化失败、claim 已消费、非 `web:` Session、来源 Capture 非 current 的失败关闭。

- [ ] **Step 2: 运行新测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_full_page_supplemental_capture.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现 Web-only 长图服务**

使用以下稳定接口：

```python
WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION = "web-full-page-supplemental.v1"


@dataclass(frozen=True, kw_only=True)
class WebFullPageSupplementalCommand:
    task_id: str
    agent_run_id: str
    session_id: str
    source_capture_id: str
    source_url: str
    deadline: datetime


@dataclass(frozen=True, kw_only=True)
class WebFullPageSupplementalResult:
    status: Literal["captured", "failed", "already_attempted"]
    source_capture_id: str
    supplemental_capture_id: str | None
    screenshot_artifact_id: str | None
    reason_code: str


class WebFullPageSupplementalCaptureService:
    async def capture(
        self, command: WebFullPageSupplementalCommand
    ) -> WebFullPageSupplementalResult:
        return await self._capture_once(command)
```

`_capture_once()` 是同文件私有实现：在 `SessionCommandGate.hold()` 内先 claim，再执行一次 `PageCaptureRequest(mode=OBSERVE, capture_dom=False, capture_visible_text=False, capture_screenshot=True, full_page=True, web_capture_stage="web_full_page_supplemental")`。只接受唯一 screenshot artifact，验证 `final_url`、`web_readiness.identity_before/after`、`artifacts_same_epoch`、`structure_before/after` 和 `capture_consistency`；通过后才持久化。失败统一 finish phase，不把视口图复制为长图。

错误映射固定为 timeout → `WEB_FULL_PAGE_SCREENSHOT_TIMEOUT`，浏览器尺寸/像素限制 → `WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED`，身份或结构漂移 → `WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT`，持久化异常 → `WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED`。

- [ ] **Step 4: 仅为显式 Web stage 增加浏览器指标**

在 `page_capture.py` 的 `_validate_request()` 和 `_web_readiness_metrics()` 只增加：

```python
"web_full_page_supplemental"
```

以及 stage 到 role 的映射：

```python
"web_full_page_supplemental": "supplemental_classification"
```

不改变 `PageCaptureRequest.full_page` 默认值、不改变 `_clone_quarantined_readonly_request()`、不改变任何 H5 stage，也不添加自动滚动命令。

- [ ] **Step 5: 用现有 Phase 表实现一次性 claim**

`PostgresWebFullPageSupplementalClaimRepository` 使用：

```python
phase_key = "web_full_page_supplemental"
phase_protocol_version = "web-full-page-supplemental.v1"
idempotency_key = f"{command.agent_run_id}:{command.source_capture_id}"
```

SQL 必须校验 `task.target_platform='web'`、`workflow_version='v2'`、最新 running main Run、`session_owner='web-v2-workflow'`、`session.current_capture_id=source_capture_id` 和当前 lease/sequence/epoch。`ON CONFLICT (task_id,session_id,phase_key,idempotency_key) DO NOTHING` 保证任一终态不重试。

- [ ] **Step 6: 跑单元、PostgreSQL 与 App/H5 不变回归**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_full_page_supplemental_capture.py services/api/tests/test_web_full_page_supplemental_postgres.py services/api/tests/test_h5_visual_viewport_capture.py services/api/tests/test_page_capture_tool.py -q`

Expected: PASS；H5 viewport 请求仍为 `full_page=false`，共享 Tool 既有默认测试不变。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_full_page_supplemental_capture.py services/api/app/infrastructure/db/web_full_page_supplemental_claim_repository.py services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_web_full_page_supplemental_capture.py services/api/tests/test_web_full_page_supplemental_postgres.py`

Expected: 无输出。

---

### Task 3: Supplemental 谱系加载与分类模型双截图输入

**Files:**
- Modify: `services/api/app/application/web_assessment_evidence.py`
- Modify: `services/api/app/infrastructure/db/web_assessment_evidence_repository.py`
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/agents/explorer/web_assessment_agent.py`
- Modify: `services/api/app/application/web_runtime_evidence.py`
- Modify: `services/api/app/application/web_full_page_supplemental_capture.py`
- Test: `services/api/tests/test_web_assessment_evidence_loader.py`
- Test: `services/api/tests/test_web_assessment_evidence_repository.py`
- Test: `services/api/tests/test_web_exploration_assessment_agent.py`
- Test: `services/api/tests/test_web_page_classification.py`

**Interfaces:**
- Consumes: Task 2 的 `WebFullPageSupplementalCaptureService`。
- Produces: `WebReadyPageCapture.supplemental_screenshot_artifact_ids`、`supplemental_evidence_status/reason_code`，以及 `WebFullPageSupplementingReadyCaptureReader.read()/read_current()`。

- [ ] **Step 1: 写跨父子 Capture 的受信加载失败测试**

在 `test_web_assessment_evidence_loader.py` 增加：

```python
@pytest.mark.asyncio
async def test_loads_primary_and_parent_bound_full_page_supplemental() -> None:
    evidence = await loader.load(
        screenshot_artifact_id="viewport",
        dom_artifact_id="dom",
        visible_text_artifact_id="text",
        supplemental_screenshot_artifact_ids=("full-page",),
    )
    assert evidence.supplemental_screenshot_artifact_ids == ("full-page",)
    assert len(evidence.supplemental_screenshot_bytes) == 1
```

再构造 supplemental 与 primary 不同 task、不是直接子 Capture、`capture_role!='supplemental'`、用途不是 `web_full_page_supplemental`、PNG 宽度不等于 1920、高度小于 1080、任一维超过 32768、像素超过 100,000,000 或文件超过 16 MiB，断言固定错误码。

- [ ] **Step 2: 运行 loader/repository 测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_assessment_evidence_loader.py services/api/tests/test_web_assessment_evidence_repository.py -q`

Expected: FAIL，当前只允许同一 Capture 三件套。

- [ ] **Step 3: 扩展受信 artifact 元数据与证据合同**

给 `StoredWebAssessmentArtifact` 增加：

```python
parent_capture_id: str | None
capture_role: str
capture_purpose: str | None
```

给 `WebAssessmentEvidence` 增加：

```python
supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
supplemental_screenshot_bytes: tuple[bytes, ...] = ()
supplemental_screenshot_media_types: tuple[str, ...] = ()
```

`VerifiedWebAssessmentEvidenceLoader.load()` 的新参数默认 `()`，保证旧调用兼容。primary screenshot/DOM/text 仍必须同一 Capture；每个 supplemental 必须与 primary 同 task、直接父节点等于 primary capture、role 为 `supplemental`、用途为 `web_full_page_supplemental`。长图只做 PNG/尺寸/哈希检查，不复用强制 1920×1080 的 viewport 校验。

- [ ] **Step 4: 把长图加入 ready Capture 和模型消息**

给 `WebReadyPageCapture` 增加：

```python
supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
supplemental_evidence_status: Literal["captured", "failed", "not_attempted"] = "not_attempted"
supplemental_evidence_reason_code: str | None = None
```

扩展 `WebPageAssessmentPort.classify()` 和 `WebExplorationAssessmentAgent.classify()` 以接收该元组。`_evidence_message()` 在视口图之后逐张追加“当前来源 Capture 的只读全页 supplemental PNG，不用于可点击性证明”标签和图片内容。没有长图时生成与旧版相同的三证据消息。

- [ ] **Step 5: 实现 ready reader 装饰器**

在 `web_full_page_supplemental_capture.py` 增加：

```python
class WebFullPageSupplementingReadyCaptureReader:
    async def read(self, *, capture_id: str, page_role: str) -> WebReadyPageCapture:
        ready = await self._delegate.read(capture_id=capture_id, page_role=page_role)
        return await self._with_supplemental(ready)

    async def read_current(self, *, page_role: str) -> WebReadyPageCapture:
        ready = await self._delegate.read_current(page_role=page_role)
        return await self._with_supplemental(ready)
```

`_with_supplemental()` 用 ready Capture 的 `capture_id/url` 调用一次长图服务，再用 `dataclasses.replace()` 附加 artifact/status。失败只附加 gap，不改变 `capture_id`、`screenshot_artifact_id` 或当前导航绑定。

- [ ] **Step 6: 运行双截图分类测试确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_assessment_evidence_loader.py services/api/tests/test_web_assessment_evidence_repository.py services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_page_classification.py -q`

Expected: PASS；分类模型收到主截图与可选长图，旧无长图路径仍通过。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_assessment_evidence.py services/api/app/infrastructure/db/web_assessment_evidence_repository.py services/api/app/application/web_page_classification.py services/api/app/agents/explorer/web_assessment_agent.py services/api/app/application/web_runtime_evidence.py services/api/app/application/web_full_page_supplemental_capture.py`

Expected: 无输出。

---

### Task 4: 内容/视频媒体事实、活动/搜索事实和 `unknown` 兜底

**Files:**
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/domain/page/web_page_knowledge.py`
- Modify: `services/api/app/application/web_page_recognition_rules.py`
- Modify: `services/api/app/application/web_primary_content_capture.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_page_recognition_rules.py`
- Test: `services/api/tests/test_web_primary_content_capture.py`

**Interfaces:**
- Consumes: Task 1 的九类 Catalog、Task 3 的双截图 `WebReadyPageCapture`。
- Produces: 可执行结构事实、八步分类优先级、稳定 `unknown` 结果。

- [ ] **Step 1: 写媒体主从关系与新增类型测试**

在 `test_web_page_classification.py` 增加参数化场景：

```python
@pytest.mark.parametrize(
    ("snapshot", "model_type", "expected"),
    [
        (_image_gallery_snapshot(), "content_detail", "content_detail"),
        (_article_with_embedded_video_snapshot(), "content_detail", "content_detail"),
        (_player_shell_without_src_snapshot(), "video_detail", "unknown"),
        (_primary_video_with_src_snapshot(), "video_detail", "video_detail"),
        (_multi_section_home_with_promotions(), "home", "home"),
        (_single_activity_landing(), "activity", "activity"),
        (_open_search_form_snapshot(), "search_page", "search_page"),
        (_search_icon_only_snapshot(), "search_page", "unknown"),
    ],
)
async def test_web_v8_primary_subject_rules(snapshot, model_type, expected) -> None:
    assessment = await _classify(snapshot, model_type=model_type, confidence=0.9)
    assert assessment.page_type == expected
```

另测 `0.69` 全部变 `unknown`，`0.70` 只有结构门槛满足时才能签发；顶层年龄门存在时业务类型全部拒绝。

- [ ] **Step 2: 运行分类测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_recognition_rules.py -q`

Expected: FAIL，当前播放器外壳可放行 `video_detail`，且没有新事实和 `unknown` 终态。

- [ ] **Step 3: 扩展可信结构事实**

给 `WebPageStructureFacts` 与 `WEB_NUMERIC_STRUCTURE_FACTS` 增加：

```python
image_gallery_root_count: int = 0
image_viewer_control_count: int = 0
confirmed_primary_video_resource_count: int = 0
player_shell_without_resource_count: int = 0
opaque_primary_media_count: int = 0
search_input_count: int = 0
search_trigger_count: int = 0
query_result_group_count: int = 0
activity_primary_region_count: int = 0
activity_action_count: int = 0
```

`web_page_structure_facts()` 只用可信 snapshot 的 `tag`、`src`、`media_parent_tag`、`type`、`role`、`class_tokens`、`ancestor_class_tokens`、重复结构和导航/广告归属计算：`video/source` 只有存在受信 `src` 且位于非重复主主体才计为 confirmed；单独 player class 只计 shell；canvas/不可读媒体计 opaque；gallery/viewer 必须由图像主体加翻页/缩放控件组合；搜索必须有输入控件和触发控件或已存在查询结果组；活动必须有单一推广主体和参与/领取/跳转动作。

- [ ] **Step 4: 为业务七类与 unknown 写机器规则并实现优先级**

更新 v8 `recognition_rules`。工作流总优先级仍是先由 Task 5 检测并正式记录 `age_gate`，年龄门存在时不得运行本 Task 的业务分类器；年龄门关闭后，业务分类器只在七类业务页与 `unknown` 闭集中按 `activity/home` 主体冲突、`search_page` 已打开状态、`content_detail/video_detail` 媒体主从、列表类型、`unknown` 兜底的顺序校验，不能再次签发 `age_gate`。`video_detail.required_all` 必须包含 `confirmed_primary_video_resource_count >= 1`；`player_root_count` 只能 supporting，不能 required。

新增：

```python
def unknown_web_page_assessment(
    assessment: WebPageAssessment,
    *, reason: WebPageClassificationReason,
    conflicts: tuple[str, ...] = (),
) -> WebPageAssessment:
    return replace(
        assessment,
        page_type="unknown",
        proposed_page_type="unknown",
        needs_review_reason=reason.value,
        conflicts=tuple(dict.fromkeys((*assessment.conflicts, *conflicts))),
        needs_review=True,
    )
```

目录外模型结果、低置信度、结构不足和无法消解冲突都转为正式 `unknown`；基础设施错误仍抛异常，不能伪装成页型。

- [ ] **Step 5: 扩展 primary content selector 而不误认播放器壳**

`build_web_primary_content_targets()` 支持 `activity`、`search_page`，并将 `_is_video_detail_element()` 改为要求 `tag in {"video","source"}` 且 `src` 非空，或 snapshot 已组合出 confirmed media；player class 只能用于定位补证，不能签发视频类型。图集/图片查看器使用内容详情目标。

- [ ] **Step 6: 运行分类与主体补采测试确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_recognition_rules.py services/api/tests/test_web_primary_content_capture.py -q`

Expected: PASS。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_page_classification.py services/api/app/domain/page/web_page_knowledge.py services/api/app/application/web_page_recognition_rules.py services/api/app/application/web_primary_content_capture.py`

Expected: 无输出。

---

### Task 5: 年龄门关闭前正式分类与关闭后双 Capture 谱系

**Files:**
- Modify: `services/api/app/application/web_age_gate.py`
- Modify: `services/api/app/application/web_page_type_assessment.py`
- Modify: `services/api/app/application/web_runtime_evidence.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Test: `services/api/tests/test_web_age_gate.py`
- Test: `services/api/tests/test_web_page_type_assessment.py`
- Test: `services/api/tests/test_web_runtime_composition.py`

**Interfaces:**
- Consumes: 现有 `WebAgeGateAssessment` 和 Task 2 的长图结果。
- Produces: `WebAgeGatePageTypeRecorder.record()`、扩展 `WebAgeGateHandlingResult` 谱系字段、assessment v4 lifecycle payload。

- [ ] **Step 1: 写“先落 age_gate 再点击”失败测试**

```python
@pytest.mark.asyncio
async def test_age_gate_is_persisted_before_authorized_dismissal() -> None:
    result = await handler.handle(entry, adult_self_attestation=_attestation())
    assert calls == [
        ("record", entry.capture_id, "age_gate"),
        ("click", entry.capture_id, "web_age_gate_confirm"),
        ("read", result.post_gate_capture_id),
    ]
    assert result.pre_gate_capture_id == entry.capture_id
    assert result.dismiss_action_id
    assert result.post_gate_capture_id
```

再测置信度 `0.75`：可正式记录 `age_gate`，但低于 `0.8` 时不点击；缺成年声明时同样先记录，再返回授权阻断；普通广告不记录 age_gate。

- [ ] **Step 2: 运行年龄门测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_age_gate.py services/api/tests/test_web_page_type_assessment.py -q`

Expected: FAIL，当前年龄门只处理动作，没有正式页型记录和完整谱系。

- [ ] **Step 3: 分离分类阈值与动作阈值**

`WebAgeGateHandler.__init__()` 改为：

```python
classification_minimum_confidence: float = 0.7
action_minimum_confidence: float = 0.8
page_type_recorder: WebAgeGatePageTypeRecorder | None = None
```

当 `gate_present=True` 且置信度达到 `0.7` 时，先调用 recorder；低于 `0.8`、缺成年声明或候选不安全时不点击。现有验证码阻断保持最高优先级且不得记录为普通业务页。

- [ ] **Step 4: 实现 lifecycle assessment v4**

将 `WEB_PAGE_TYPE_ASSESSMENT_VERSION` 升为 `web-page-type-assessment.v4`。`WebAgeGatePageTypeRecorder.record()` 写入 `canonical_page_type="age_gate"`、`classification_status="classified"`、`coverage_group="lifecycle"`，证据包含 pre-gate 主截图、DOM 和可选长图。扩展仓储校验，只允许 v4 签发 `age_gate`；v2/v3 仍按原集合恢复。

`WebAgeGateHandlingResult` 使用明确字段：

```python
pre_gate_capture_id: str
gate_present: bool
dismiss_action_id: str | None
clicked_candidate_id: str | None
post_gate_capture_id: str | None
gate_disappeared: bool
```

保留 `entry_capture_id`/`after_capture_id` 只读兼容 property，避免现有报告立刻断裂。

- [ ] **Step 5: 在 Web 组合根安排 pre/post 长图与业务重分类**

`prepare_home()` 顺序固定为：entry Capture → pre-gate supplemental → age assess/record → 授权点击 → post-gate readiness Capture → post-gate supplemental → 九类业务分类。post-gate 分类不得更新或覆盖 pre-gate assessment；二者通过 `dismiss_action_id` 和 parent/action lineage 关联。

- [ ] **Step 6: 运行年龄门和组合测试确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_age_gate.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_runtime_composition.py -q`

Expected: PASS。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_age_gate.py services/api/app/application/web_page_type_assessment.py services/api/app/application/web_runtime_evidence.py services/api/app/application/web_runtime_composition.py services/api/app/infrastructure/db/web_capture_evidence_repository.py`

Expected: 无输出。

---

### Task 6: 搜索零提交发现与同源活动导航

**Files:**
- Create: `services/api/app/application/web_auxiliary_navigation.py`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Test: `services/api/tests/test_web_auxiliary_navigation.py`
- Regression: `services/api/tests/test_web_desktop_navigation.py`
- Regression: `services/api/tests/test_h5_runtime_composition.py`

**Interfaces:**
- Consumes: 可信 ready snapshot、`CaptureCoordinator`、readiness reader、Task 4 classifier、既有广告落地 scheduler。
- Produces: `WebAuxiliaryNavigationCandidate`、`discover_web_auxiliary_navigation_candidates()`、`WebAuxiliaryNavigationExplorer.explore()`。

- [ ] **Step 1: 写候选与零提交测试**

```python
def test_extracts_only_href_backed_search_candidate() -> None:
    candidates = discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN)
    assert [(item.kind, item.url) for item in candidates] == [
        ("search_href", "https://example.test/search"),
    ]


@pytest.mark.asyncio
async def test_href_less_search_control_is_never_executed() -> None:
    result = await explorer.explore(command, source_capture_id="ready")
    assert coordinator.commands == []
    assert result.status == "no_candidate"
```

再测：有 href 必须 `NAVIGATE` 而非 click；同源 activity href 直接导航；跨域 activity 不交给 coordinator 而交给 ad scheduler；无 href 搜索和 activity 均不执行；搜索动作结果 unknown 不重试；普通菜单提取仍不包含无 href 控件。

- [ ] **Step 2: 运行辅助导航测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_desktop_navigation.py -q`

Expected: FAIL，新模块和 target mode 尚不存在。

- [ ] **Step 3: 实现 Web 辅助候选闭集**

```python
WebAuxiliaryNavigationKind = Literal[
    "search_href", "activity_href", "external_promotion"
]


@dataclass(frozen=True, kw_only=True)
class WebAuxiliaryNavigationCandidate:
    candidate_id: str
    source_capture_id: str
    kind: WebAuxiliaryNavigationKind
    label: str
    selector: str | None
    url: str
    confidence: float
```

候选只从当前 Capture 的可信 DOM snapshot 产生。搜索和 activity 候选都必须具备有效 HTTP(S) `href`；无 `href` 控件不进入候选、不执行。所有 URL 先规范化并区分同源/跨域。

- [ ] **Step 4: 实现执行边界与密封目标解析**

`search_href`、`activity_href` 只使用 `InteractionOperation.NAVIGATE` 和 `destination_url`。`semantic_target_resolver.py` 的新分支必须重新读取与动作来源相同的 current snapshot 并唯一匹配候选，不接受 Agent 提供 selector 作为权威；绝不生成 CLICK/INPUT/SUBMIT。

`external_promotion` 只调用现有 `schedule_ready_web_business_page_ad_evidence()` 或底层广告 scheduler，不发普通导航。无 href activity 返回 `not_executable_no_href`。

- [ ] **Step 5: 落地后执行 readiness、双截图与分类**

辅助导航到达后创建 page probe，再走现有 Web readiness；使用 Task 3 的 supplemental reader 和 Task 4 classifier。自然已打开的搜索界面只读分类，不点击结果。每个候选使用 `qc_surface_phase_run` 的 `web_search_discovery` 或 `web_activity_navigation` phase key，未知结果也消费一次。

搜索/活动辅助阶段不得写入现有菜单账本的 `menu_page_type`。普通菜单 href 若实际落到 `search_page` 或 `activity`，保留其正式 Web 页面判定，但将该菜单动作以 `page_type_mismatch` 关闭，不把超出现有数据库约束的类型写入菜单字段；这条边界用仓储测试锁定，因此无需数据库迁移。

- [ ] **Step 6: 跑辅助导航和 App/H5 回归确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_h5_runtime_composition.py -q`

Expected: PASS；H5 target resolution 和 Runtime 装配结果不变。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_auxiliary_navigation.py services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/app/application/web_runtime_composition.py services/api/app/application/web_desktop_navigation.py services/api/tests/test_web_auxiliary_navigation.py`

Expected: 无输出。

---

### Task 7: Assessment v4、七类业务适用性与历史覆盖快照

**Files:**
- Modify: `services/api/app/application/web_page_type_assessment.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Modify: `services/api/app/application/web_page_applicability.py`
- Modify: `services/api/app/domain/page/web_site_menu_catalog.json`
- Modify: `services/api/app/domain/exploration/web_coverage_snapshot.py`
- Modify: `services/api/app/application/web_inspection_reconciliation.py`
- Modify: `services/api/app/application/web_page_inspection_notification.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_snapshot_row.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_repository.py`
- Modify: `services/api/app/api/v2/schemas.py`
- Modify: `services/api/app/application/workstation_tasks.py`
- Test: `services/api/tests/test_web_page_type_assessment.py`
- Test: `services/api/tests/test_web_page_applicability.py`
- Test: `services/api/tests/test_web_page_inspection_notification.py`
- Test: `services/api/tests/test_web_coverage_snapshot.py`
- Test: `services/api/tests/test_web_coverage_snapshot_repository.py`
- Test: `services/api/tests/test_web_inspection_reconciliation.py`

**Interfaces:**
- Consumes: Task 1 页型分组、Task 4 `unknown`、Task 5 lifecycle assessment。
- Produces: `web-page-type-assessment.v4`、`web-coverage-snapshot.v4` 和不改变历史结果的版本映射。

- [ ] **Step 1: 写 v4 分母和历史版本测试**

```python
def test_v4_coverage_partitions_only_seven_business_types() -> None:
    snapshot = WebCoverageSnapshot.create(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=1,
        required_page_types=("home", "content_list"),
        optional_page_types=("activity", "search_page"),
        observed_page_types=("home",),
        unrecognized_page_types=(),
        observed_lifecycle_page_types=("age_gate",),
        unknown_page_capture_ids=(CAPTURE_ID,),
    )
    assert "age_gate" not in snapshot.required_page_types
    assert "unknown" not in snapshot.not_applicable_page_types
    assert snapshot.observed_lifecycle_page_types == ("age_gate",)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("web-page-type-assessment.v2", {"content_list", "content_detail", "video_list", "video_detail"}),
        ("web-page-type-assessment.v3", {"home", "content_list", "content_detail", "video_list", "video_detail"}),
        ("web-page-type-assessment.v4", set(WEB_BUSINESS_PAGE_TYPE_CODES)),
    ],
)
def test_coverage_version_keeps_frozen_business_denominator(version, expected) -> None:
    assert _canonical_types(version) == expected
```

再测 `unknown` 以 `canonical_page_type="unknown"`、`classification_status="needs_review"` 落库；`age_gate` 可 classified 但不进入业务 observed；v3 快照哈希保持不变。

- [ ] **Step 2: 运行 assessment/coverage 测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_page_applicability.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py -q`

Expected: FAIL，当前 v3 只有五类业务分母且 canonical unknown 被拒绝。

- [ ] **Step 3: 发布 v4 assessment 语义**

`WebPageTypeAssessmentRecorder.persist()` 的分类状态规则固定为：七类业务和 `age_gate` 在无冲突时 `classified`；`unknown` 始终 `needs_review` 但 `final_page_type="unknown"`；基础设施失败仍不创建 unknown。证据列表加入 supplemental artifact IDs 与 status/reason。

在 `web_page_type_assessment.py` 新增 Web 专属 `_build_web_page_type_assessment_payload()`，按 Task 1 的 Web 分组直接构造 v4 payload；v4 路径不再调用 `get_default_page_type_registry()` 或共享 `build_page_type_assessment()`，因此无需修改共享 Catalog/Registry。v2/v3 恢复路径维持既有构造与冻结结果。

仓储 `_validate_web_page_type_assessment_payload()` 按 `expected_schema_version` 选择 v2 四类、v3 五类、v4 九类，不读取当前常量重解历史；v4 额外校验 `coverage_group`。`web_page_inspection_notification.py` 只在 Web V2 分支接受 v4 的九类与 `unknown=needs_review`，不修改 App/H5 通知校验。

- [ ] **Step 4: 让适用性只使用七类业务集合**

`resolve_web_page_applicability()`、API `required_page_types` 的 Web V2 校验分支、workstation 的 Web V2 投影分支、站点菜单适用性和 discovery required types 全部改用 `WEB_BUSINESS_PAGE_TYPE_CODES`。同步更新 `web_site_menu_catalog.json` 各 Web 站点的适用性分区，但菜单目录只能提供适用性与历史提示，不能作为页面类型正式判定的主要依据。未知站点安全 fallback 以七类分区，但 `activity/search_page` 默认 `optional`，基础五类保持现有 required 语义；运行时明确发现入口后再提升 applicable 状态，不反向改变已冻结历史 Run。为共享 schema/workstation 文件补充 App/H5 序列化快照断言，证明非 Web V2 分支输出逐字段不变。

- [ ] **Step 5: 发布 coverage snapshot v4 且不迁移表**

新增：

```python
WEB_COVERAGE_SNAPSHOT_V4 = "web-coverage-snapshot.v4"
WEB_PAGE_TYPE_NORMALIZATION_VERSION = "web-page-type-assessment.v4"
```

`WebCoverageSnapshot` 增加：

```python
observed_lifecycle_page_types: tuple[str, ...] = ()
unknown_page_capture_ids: tuple[str, ...] = ()
supplemental_evidence_summary: Mapping[str, int] = field(default_factory=dict)
```

v4 hash material包含上述字段。仓储把它们写入现有 `applicability_snapshot` JSONB 的 `lifecycle_observations`、`unknown_page_capture_ids`、`supplemental_evidence_summary`；顶层关系列仍只存七类业务 required/observed/missing。row parser 对 v2/v3 使用空默认，对 v4 严格还原。

- [ ] **Step 6: 运行 v4 与历史回归确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_capture_evidence_repository.py services/api/tests/test_web_page_applicability.py services/api/tests/test_web_page_inspection_notification.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_web_inspection_reconciliation.py -q`

Expected: PASS；历史 v2/v3 fixture 的 hash 和覆盖状态不变。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_page_type_assessment.py services/api/app/infrastructure/db/web_capture_evidence_repository.py services/api/app/application/web_page_applicability.py services/api/app/domain/page/web_site_menu_catalog.json services/api/app/domain/exploration/web_coverage_snapshot.py services/api/app/application/web_inspection_reconciliation.py services/api/app/application/web_page_inspection_notification.py services/api/app/infrastructure/db/web_coverage_snapshot_repository.py services/api/app/infrastructure/db/web_coverage_snapshot_row.py services/api/app/infrastructure/db/web_coverage_repository.py services/api/app/api/v2/schemas.py services/api/app/application/workstation_tasks.py`

Expected: 无输出。

---

### Task 8: Web 策略冻结、Runtime 装配与正式报告

**Files:**
- Modify: `services/api/app/application/web_run_policy.py`
- Modify: `services/api/app/domain/exploration/web_run_policy_snapshot.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/infrastructure/db/web_report_page_snapshot.py`
- Modify: `services/api/app/application/web_report_explanation.py`
- Modify: `services/api/app/application/web_report_generation_audit.py`
- Modify: `services/api/app/application/report_insight.py`（仅 `_web_report_logical_pages()`）
- Test: `services/api/tests/test_web_run_policy_snapshot.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_report_explanation.py`
- Test: `services/api/tests/test_report_insight.py`（仅新增 Web V2 断言）
- Test: `services/api/tests/test_web_v2_formal_report_e2e.py`

**Interfaces:**
- Consumes: Tasks 2-7 的长图、分类、lifecycle、辅助导航与 coverage 服务。
- Produces: 新 Run 的冻结 `full_page_supplemental_policy`，以及报告中的主/补截图和九类审计。

- [ ] **Step 1: 写策略恢复和报告状态测试**

```python
def test_new_web_policy_enables_full_page_supplemental_only_for_web_v2() -> None:
    body = load_published_web_policy(now=NOW, max_duration_seconds=300)
    assert body["full_page_supplemental_policy"] == {
        "protocol_version": "web-full-page-supplemental.v1",
        "enabled": True,
        "capture_role": "supplemental",
        "maximum_bytes": 16 * 1024 * 1024,
        "maximum_dimension": 32768,
        "maximum_pixels": 100_000_000,
    }


def test_legacy_policy_without_full_page_section_materializes_disabled() -> None:
    frozen = materialize_web_policy(_legacy_snapshot())
    assert frozen.full_page_supplemental_policy.enabled is False
```

报告测试分别断言 `captured`、`failed`、`not_attempted`；年龄门前后是两个 timeline 项；unknown 在“需复核”中且不算覆盖。

- [ ] **Step 2: 运行策略和报告测试确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_run_policy_snapshot.py services/api/tests/test_web_report_explanation.py services/api/tests/test_web_v2_formal_report_e2e.py -q`

Expected: FAIL，策略和报告尚无新字段。

- [ ] **Step 3: 冻结长图策略并兼容旧 Run**

新增 `WebFullPageSupplementalPolicy` dataclass，`load_published_web_policy()` 对新 Run 写入上面的完整正文；`materialize_web_policy()` 对字段存在时严格校验，对旧冻结正文缺失时构造 `enabled=False`。不修改 App/H5 settings，也不使用全局环境开关让其他平台继承。

- [ ] **Step 4: 只在 Web V2 Runtime 装配新服务**

`PostgresWebRuntimeProvider` 创建 Web claim repository、`WebFullPageSupplementalCaptureService`、supplementing ready reader、age gate recorder 和 auxiliary explorer，并注入 `WebV2Runtime`。`capture_runtime.py` 只暴露现有 Web browser/persistence/session gate 依赖，不改变 H5 组合根。

所有首页、菜单页、详情页、同源活动页和搜索页在 ready 后分类前调用 supplemental reader；年龄门入口在点击前调用同一服务。deadline 剩余不足以完成长图时记录 `WEB_FULL_PAGE_SCREENSHOT_TIMEOUT`/gap 并继续主流程。

- [ ] **Step 5: 扩展报告而不把长图当主证据**

`read_web_report_page_snapshot()` 为每个主 Capture 关联最多一个 `capture_role='supplemental'` 且 `parent_page_capture_id=主 Capture`、purpose=`web_full_page_supplemental` 的子 Capture。`report_insight.py` 只在 `_web_report_logical_pages()` 将这些字段投影为 Web V2 页面项；不得改动 H5/App 分支。报告页面项输出：

```python
{
    "primary_screenshot_artifact_id": "11111111-1111-4111-8111-111111111111",
    "supplemental_full_page": {
        "status": "captured",
        "capture_id": "22222222-2222-4222-8222-222222222222",
        "screenshot_artifact_id": "33333333-3333-4333-8333-333333333333",
        "reason_code": "WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
    },
}
```

失败和未尝试时 capture/artifact 为 `None`。报告摘要分别列 `business_page_types`、`lifecycle_page_types`、`unknown_pages`；不得把 `age_gate -> home` 合并为一个页面项。

- [ ] **Step 6: 运行策略、Runtime 和正式报告测试确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_run_policy_snapshot.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_report_explanation.py services/api/tests/test_report_insight.py services/api/tests/test_web_v2_formal_report_e2e.py -q`

Expected: PASS。

- [ ] **Step 7: 完成本任务审查检查点（不提交）**

Run: `git diff --check -- services/api/app/application/web_run_policy.py services/api/app/domain/exploration/web_run_policy_snapshot.py services/api/app/application/web_runtime_composition.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/infrastructure/db/web_report_page_snapshot.py services/api/app/application/web_report_explanation.py services/api/app/application/web_report_generation_audit.py services/api/app/application/report_insight.py`

Expected: 无输出。

---

### Task 9: Web 全量回归、App/H5 隔离证明与规则文档

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Test: all Web tests affected above
- Regression: `services/api/tests/test_app_capture_playbook.py`
- Regression: `services/api/tests/test_h5_visual_viewport_capture.py`
- Regression: `services/api/tests/test_h5_runtime_composition.py`
- Regression: `services/api/tests/test_page_capture_tool.py`
- Regression: `services/api/tests/test_page_capture_persistence.py`

**Interfaces:**
- Consumes: Tasks 1-8 的完整实现。
- Produces: 可复用回归规则、Web 全量测试证据和 App/H5 未受影响的文件/行为证明。

- [ ] **Step 1: 更新专项回归规则**

在 `docs/专项能力与高频回归规则.md` 增加“Web 双截图与九类页型”章节，明确：视口主证据不可替换；长图按来源 Capture 幂等；年龄门前后双 Capture；搜索零提交；播放器壳不等于视频资源；activity/search 适用性不无条件进入分母；unknown 不算覆盖；App/H5 不继承 Web 规则。

- [ ] **Step 2: 运行 Web 专项测试集合**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_full_page_supplemental_capture.py services/api/tests/test_web_full_page_supplemental_postgres.py services/api/tests/test_web_assessment_evidence_loader.py services/api/tests/test_web_assessment_evidence_repository.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_recognition_rules.py services/api/tests/test_web_age_gate.py services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_page_applicability.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_web_inspection_reconciliation.py services/api/tests/test_web_run_policy_snapshot.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_report_explanation.py services/api/tests/test_web_v2_formal_report_e2e.py -q`

Expected: PASS，无 xfail 掩盖本次新增用例。

- [ ] **Step 3: 运行完整 Web 回归**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests -q -k "web or page_capture"`

Expected: PASS；记录 passed/skipped 数量和耗时，任何失败先按 `systematic-debugging` 处理。

- [ ] **Step 4: 运行 App/H5 隔离回归**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_app_capture_playbook.py services/api/tests/test_h5_visual_viewport_capture.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_page_capture_tool.py services/api/tests/test_page_capture_persistence.py -q`

Expected: PASS；App/H5 页型集合、截图参数和 Runtime 装配断言保持原值。

- [ ] **Step 5: 校验没有改动 App/H5 或共享 Catalog 文件**

Run: `git diff --name-only -- services/api/app/application/app_* services/api/app/application/h5_* services/api/app/domain/exploration/app_* services/api/app/domain/exploration/h5_* services/api/app/domain/page/page_type_catalog.json services/api/app/domain/page/catalog.py services/api/app/domain/page/registry.py`

Expected: 无输出。若有输出，停止验收并移除本次引入的越界改动，不得以测试通过替代平台隔离。

- [ ] **Step 6: 最终工作区与格式审查（不提交）**

Run: `git diff --check`

Expected: 无输出。

Run: `git status --short --branch`

Expected: 显示本次与用户既有未提交改动；不得执行 `git add` 或 `git commit`，不得删除用户已有 outputs。

- [ ] **Step 7: 输出验收报告**

报告必须列出：实际修改文件、Web 专项/完整回归统计、App/H5 隔离测试统计、是否有数据库迁移（必须为否）、长图失败降级验证、九类 Catalog 版本、历史 v2/v3 恢复验证、尚未执行的实况站点探索风险。不要声称三站实况通过，除非用户另行授权并实际运行。
