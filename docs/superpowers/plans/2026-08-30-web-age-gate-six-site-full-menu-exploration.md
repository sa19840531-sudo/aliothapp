# Web Age Gate and Six-Site Full-Menu Exploration Implementation Plan

> 2026-08-31 执行口径修订：Web 单站只实际采集 Catalog 顺序中的前 10 个同源菜单，剩余同源菜单统一闭账为 `menu_limit_skipped`；首页、跨域项及一层详情不占菜单额度。六站预期为 54 个已执行同源菜单、103 个限额跳过、6 个首页和 3 个跨域阻断，完整账本仍为 166 项。H5/App 不在本次修订范围内。

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在不改变 H5/App 既有行为的前提下，为桌面 Web V2 增加操作者成年声明、18 岁门禁识别、逐页阻断弹窗清理、五类页面判定、六站菜单白名单全量遍历及列表到详情采集。

**Architecture:** 保持现有 Web 独立 Worker、Browser Profile 和 `WEB_SURFACE_SESSION_OWNER` 边界。新增两个互不替代的领域目录：`web_page_type_catalog.json` 只定义五种页面语义，`web_site_menu_catalog.json` 只定义站点级可执行菜单。一次 Run 只选择与目标 origin 精确匹配的一个站点目录；六站统计只在六个单站 Run 完成后汇总。所有点击都来自可信 Capture 中的密封候选 ID，LLM 只做语义判定，代码负责同源、可见、Hit Test、风险和重定向复验。

**Tech Stack:** Python 3.12、FastAPI/Pydantic、Microsoft Agent Framework、Playwright、pytest、Ruff、JSON Catalog。

**Design spec:** `docs/superpowers/specs/2026-08-30-web-entry-age-gate-menu-catalog-design.md`

---

## 实施边界

- 本计划只实施 `target.platform == "web"` 且 `execution_mode == "exploration_only"` 的路径。
- 桌面 Profile 固定 `1920 × 1080`；截图只做 PNG 无损压缩/优化，不改变像素尺寸，也不改变现有 `image/png` 证据合同。
- Web 打开即首页，不执行 H5 的线路加载、启动广告页或 H5 年龄确认逻辑。
- 操作者必须在提交任务时主动声明成年；服务端记录声明版本、操作者和时间，客户端不能伪造后两者。
- 不处理验证码、滑块、Cloudflare challenge 或其他人机验证；发现后终止当前站点并标记人工复核。
- 每个首页、菜单页和详情页都先做入口 Capture，再逐层关闭普通阻断弹窗，最后形成无遮挡基线。
- 每个页面最多清理 12 层、总计 120 秒、单层稳定等待 5 秒；签名不变即停止当前页面。
- `z-index > 500` 只是阻断候选信号，不等于自动点击；点击仍需 LLM 选择和代码安全复验。
- 白名单只授权同源 GET 导航与观察，不授权提交、下载、支付、上传、登录或跨域动作。
- 页面类型只允许 `home`、`web_video_list`、`web_video_detail`、`web_content_list`、`web_content_detail`。
- Page Type 覆盖不能提前结束探索；当前站点的每个菜单身份都必须进入终态。
- 本计划不引入数据库迁移。新增摘要放入既有 JSON 任务配置/运行结果和 Capture Artifact。

## 文件结构总览

新增领域文件：

- `services/api/app/domain/page/web_page_type_catalog.json`
- `services/api/app/domain/page/web_page_catalog.py`
- `services/api/app/domain/page/web_site_menu_catalog.json`
- `services/api/app/domain/page/web_site_menu_registry.py`
- `services/api/app/domain/exploration/web_exploration.py`

新增应用文件：

- `services/api/app/application/web_age_gate.py`
- `services/api/app/application/web_page_readiness.py`
- `services/api/app/application/web_page_classification.py`
- `services/api/app/application/web_list_detail_navigation.py`
- `services/api/app/application/web_menu_exploration.py`

新增 Agent 文件：

- `services/api/app/agents/explorer/web_assessment_agent.py`

主要修改文件：

- `services/api/app/domain/task/targets.py`
- `services/api/app/api/v2/schemas.py`
- `services/api/app/application/v2_task_entries.py`
- `services/api/app/application/app_workflow_dispatch.py`
- `services/api/app/application/web_desktop_navigation.py`
- `services/api/app/application/web_desktop_exploration.py`
- `services/api/app/application/web_exploration_workflow.py`
- `services/api/app/application/web_runtime_composition.py`
- `services/api/app/infrastructure/browser/page_capture.py`
- `services/api/app/infrastructure/browser/capture_runtime.py`
- `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- `services/api/app/agents/runtime/models.py`
- `services/api/app/infrastructure/queue/redis_queue.py`
- `services/api/app/worker.py`

---

### Task 1: 建立 Web 独立五类 Page Type Catalog

**Files:**

- Create: `services/api/app/domain/page/web_page_type_catalog.json`
- Create: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/page/__init__.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`

**Interfaces:**

```python
WEB_PAGE_TYPE_CODES = (
    "home",
    "web_video_list",
    "web_video_detail",
    "web_content_list",
    "web_content_detail",
)

def load_web_page_type_catalog() -> PageTypeCatalog: ...
```

- [ ] **Step 1: 写失败测试，锁定五类和不回退 H5**

```python
def test_web_catalog_contains_exactly_five_codes() -> None:
    catalog = load_web_page_type_catalog()
    assert catalog.codes == WEB_PAGE_TYPE_CODES
    assert "web-home" not in catalog.codes


def test_web_catalog_failure_does_not_load_default_h5_catalog(tmp_path: Path) -> None:
    with pytest.raises(WebPageTypeCatalogError):
        load_web_page_type_catalog(tmp_path / "missing.json")
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_page_type_catalog.py -q`

Expected: FAIL，模块或 Catalog 尚不存在。

- [ ] **Step 3: 实现独立加载器和五类 JSON**

复用 `load_page_type_catalog(path)` 的解析模型，但路径固定指向 Web JSON。为五类分别补充 DOM、布局和 OCR 信号；`home` 是首页语义，不再沿用导航菜单生成的伪类型。

```python
def load_web_page_type_catalog(path: Path | None = None) -> PageTypeCatalog:
    catalog_path = path or Path(__file__).with_name("web_page_type_catalog.json")
    try:
        catalog = load_page_type_catalog(catalog_path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise WebPageTypeCatalogError(str(exc)) from exc
    if catalog.codes != WEB_PAGE_TYPE_CODES:
        raise WebPageTypeCatalogError("Web Catalog 必须且只能定义五类页面")
    return catalog
```

- [ ] **Step 4: 运行定向测试**

Run: `python -m pytest services/api/tests/test_web_page_type_catalog.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/domain/page services/api/tests/test_web_page_type_catalog.py
git commit -m "[AI编程] feat(web): 增加独立五类页面目录" -m "涉及模块：Web 页面领域；数据库迁移：无；前端菜单/路由：无需同步；测试风险：仅验证目录加载和类型边界。"
```

### Task 2: 将六站审计清单提升为生产菜单 Catalog

**Files:**

- Create: `services/api/app/domain/page/web_site_menu_catalog.json`
- Create: `services/api/app/domain/page/web_site_menu_registry.py`
- Modify: `services/api/app/domain/page/__init__.py`
- Source: `docs/data/web_v2_six_site_menu_allowlist.json`
- Test: `services/api/tests/test_web_site_menu_registry.py`

**Interfaces:**

```python
class WebMenuDecision(StrEnum):
    HOME_BASELINE = "home_baseline"
    ALLOWLISTED_OBSERVE_ONLY = "allowlisted_observe_only"
    BLOCKED_CROSS_ORIGIN = "blocked_cross_origin"

@dataclass(frozen=True, kw_only=True)
class WebSiteMenuEntry:
    entry_id: str
    menu_path: tuple[str, ...]
    href: str
    canonical_url: str
    decision: WebMenuDecision

class WebSiteMenuRegistry:
    def resolve_site(self, target_url: str) -> WebSiteMenuCatalog: ...
```

- [ ] **Step 1: 写失败测试，锁定原始附件的全部菜单和单站解析**

```python
def test_catalog_keeps_all_audited_active_anchors() -> None:
    registry = WebSiteMenuRegistry()
    counts = {site.site_id: len(site.entries) for site in registry.sites}
    assert counts == {
        "heiliao.com": 30,
        "hl365.com": 43,
        "www.91jav1.com": 8,
        "91porna.com": 65,
        "huangguoai.com": 9,
        "avhub360.com": 11,
    }
    assert sum(counts.values()) == 166


def test_registry_only_returns_target_origin_catalog() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://heiliao.com/")
    assert site.site_id == "heiliao.com"
    assert len(site.entries) == 30


def test_unknown_site_fails_closed() -> None:
    with pytest.raises(WebSiteMenuCatalogNotFoundError):
        WebSiteMenuRegistry().resolve_site("https://unknown.example/")
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_site_menu_registry.py -q`

Expected: FAIL，生产 Catalog 和 Registry 尚不存在。

- [ ] **Step 3: 复制完整审计数据并实现严格注册表**

使用 `apply_patch` 将 `docs/data/web_v2_six_site_menu_allowlist.json` 中经校验的六个 `sites` 完整复制到生产目录，不手工删减条目。Registry 必须校验：站点 origin 唯一、entry ID 唯一、host/path/query 规范化、fragment 忽略、HTTPS 不降级、跨域条目只能是阻断状态、首页每站恰好一个。

```python
def resolve_site(self, target_url: str) -> WebSiteMenuCatalog:
    key = canonical_origin(target_url)
    try:
        return self._sites_by_origin[key]
    except KeyError as exc:
        raise WebSiteMenuCatalogNotFoundError(key) from exc
```

- [ ] **Step 4: 验证生产目录与审计源等价**

Run: `python -m pytest services/api/tests/test_web_site_menu_registry.py -q`

Run: `python -m json.tool services/api/app/domain/page/web_site_menu_catalog.json > $null`

Expected: PASS；总计 166、6 首页、157 同源观察、3 跨域阻断。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/domain/page services/api/tests/test_web_site_menu_registry.py
git commit -m "[AI编程] feat(web): 增加六站菜单白名单目录" -m "涉及模块：Web 菜单领域；数据库迁移：无；前端菜单/路由：无需同步；测试风险：目录版本更新需保持审计源与生产副本一致。"
```

### Task 3: 冻结操作者成年自我声明

**Files:**

- Modify: `services/api/app/domain/task/targets.py`
- Modify: `services/api/app/domain/task/__init__.py`
- Modify: `services/api/app/api/v2/schemas.py`
- Modify: `services/api/app/application/v2_task_entries.py`
- Modify: `services/api/app/application/app_workflow_dispatch.py`
- Test: `services/api/tests/test_task_target_contracts.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`
- Test: `services/api/tests/test_app_workflow_dispatch.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class AdultSelfAttestation:
    declared: bool
    statement_version: str
    declared_by: str
    declared_at: datetime

@dataclass(frozen=True, kw_only=True)
class WebTaskConfig:
    browser_profile_id: str
    adult_self_attestation: AdultSelfAttestation
```

- [ ] **Step 1: 写失败测试，拒绝缺失或伪造的声明**

```python
def test_web_target_requires_adult_self_attestation() -> None:
    with pytest.raises(TaskTargetValidationError, match="成年声明"):
        TaskTarget(
            platform=TaskTargetPlatform.WEB,
            target_url="https://heiliao.com/",
            web=WebTaskConfig(browser_profile_id="desktop-1920x1080-v1"),
        )


def test_v2_submission_freezes_server_actor_and_time() -> None:
    target = submitted_record.task_config["_task_target"]["web"]
    assert target["adult_self_attestation"]["declared"] is True
    assert target["adult_self_attestation"]["declared_by"] == "actor-1"
    assert target["adult_self_attestation"]["declared_at"].endswith("Z")
```

- [ ] **Step 2: 运行定向测试并确认失败**

Run: `python -m pytest services/api/tests/test_task_target_contracts.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_app_workflow_dispatch.py -q`

Expected: FAIL，Web 请求和冻结目标尚无声明字段。

- [ ] **Step 3: 实现请求字段和服务端冻结**

请求只接受 `declared=true` 与固定 `statement_version`。`declared_by` 从既有 `AccessScope.actor_user_id` 获取，`declared_at` 由服务端 UTC 时钟生成。序列化到 `_task_target.web.adult_self_attestation`，反序列化必须严格验证；H5/App 结构不增加此字段。

```python
class AdultSelfAttestationRequest(BaseModel):
    declared: Literal[True]
    statement_version: Literal["adult-v1"] = "adult-v1"
```

- [ ] **Step 4: 运行目标、提交和分发测试**

Run: `python -m pytest services/api/tests/test_task_target_contracts.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_app_workflow_dispatch.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/domain/task services/api/app/api/v2/schemas.py services/api/app/application/v2_task_entries.py services/api/app/application/app_workflow_dispatch.py services/api/tests/test_task_target_contracts.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_app_workflow_dispatch.py
git commit -m "[AI编程] feat(web): 冻结操作者成年声明" -m "涉及模块：Web 任务入口与目标合同；数据库迁移：无；前端菜单/路由：无需同步但调用方需提交 adult_self_attestation；测试风险：旧 Web 草稿需补声明后才能提交。"
```

### Task 4: 泛化导航结构并保留每个菜单身份

**Files:**

- Modify: `services/api/app/application/web_desktop_navigation.py`
- Test: `services/api/tests/test_web_desktop_navigation.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebNavigationCandidate:
    entry_id: str
    label: str
    url: str
    menu_path: tuple[str, ...]
    dom_fingerprint: str
    source_signal: str
```

- [ ] **Step 1: 写失败测试覆盖六站导航结构差异**

```python
@pytest.mark.parametrize(
    ("html", "expected_labels"),
    [
        ('<nav><a href="/a">A</a></nav>', ("A",)),
        ('<div class="main-navigation"><a href="/b">B</a></div>', ("B",)),
        ('<ul id="top-nav"><li><a href="/c">C</a></li></ul>', ("C",)),
        ('<div aria-label="Primary navigation"><a href="/d">D</a></div>', ("D",)),
    ],
)
def test_extracts_semantic_and_fuzzy_navigation(html, expected_labels) -> None:
    assert tuple(item.label for item in extract_navigation_candidates(
        html, base_url="https://example.test/"
    )) == expected_labels


def test_same_url_in_two_menu_paths_keeps_two_identities() -> None:
    candidates = extract_navigation_candidates(MOVIESETS_DUPLICATE_HTML, base_url=BASE_URL)
    assert [item.url for item in candidates].count(f"{BASE_URL}moviesets") == 2
    assert len({item.entry_id for item in candidates}) == len(candidates)
```

- [ ] **Step 2: 运行导航测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_desktop_navigation.py -q`

Expected: FAIL，现实现要求 class 含 nav 且必须有 ul/li，并按 URL 去重。

- [ ] **Step 3: 实现分层导航信号和菜单路径**

候选容器按以下信号并集识别：语义 `<nav>`；class/id token 模糊包含 `nav`；`role=navigation`；`aria-label`/`data-*` 中的导航语义。`ul/li` 是强结构信号但不是必选条件。忽略 HTML 注释、隐藏模板和无 href 分组。按 DOM 顺序和父菜单文本生成 `menu_path`；使用“站点 + 菜单路径 + href + DOM 顺序”生成 `entry_id`，不按 URL 或 Page Type 去重。

- [ ] **Step 4: 运行导航测试**

Run: `python -m pytest services/api/tests/test_web_desktop_navigation.py -q`

Expected: PASS；包括 nav 直属 `<a>`、下拉 `ul/li/a`、重复 URL、多种信号和注释排除。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_desktop_navigation.py services/api/tests/test_web_desktop_navigation.py
git commit -m "[AI编程] feat(web): 泛化导航识别并保留菜单身份" -m "涉及模块：Web DOM 导航解析；数据库迁移：无；前端菜单/路由：无需同步；测试风险：非标准导航需通过运行时漂移审计发现。"
```

### Task 5: 把首跳拆成入口探测、门禁处理和真实首页基线

**Files:**

- Modify: `services/api/app/application/web_exploration_workflow.py`
- Modify: `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Test: `services/api/tests/test_web_exploration_workflow.py`
- Test: `services/api/tests/test_web_desktop_capture_policy.py`
- Test: `services/api/tests/test_web_capture_runtime.py`

**Interfaces:**

```python
class WebCaptureStage(StrEnum):
    ENTRY_PROBE = "web_entry_probe"
    PAGE_PROBE = "web_page_probe"
    PAGE_READY_BASELINE = "web_page_ready_baseline"

@dataclass(frozen=True)
class PageCaptureRequest:
    # 既有字段保持不变
    web_capture_stage: str | None = None
```

- [ ] **Step 1: 写失败测试，禁止首跳自动清障**

```python
async def test_first_navigation_captures_entry_before_any_click() -> None:
    result = await bootstrapper.start(command)
    assert result.after_capture.request.web_capture_stage == "web_entry_probe"
    assert result.after_capture.request.clear_obstructions is False
    assert driver.clicks == []


def test_h5_capture_defaults_are_unchanged() -> None:
    request = PageCaptureRequest(mode=PageCaptureMode.GOTO)
    assert request.startup_policy == "h5"
    assert request.web_capture_stage is None
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_exploration_workflow.py services/api/tests/test_web_desktop_capture_policy.py services/api/tests/test_web_capture_runtime.py -q`

Expected: FAIL，现策略对 Web GOTO 强制 `clear_obstructions=True`，无法先保留门禁原貌。

- [ ] **Step 3: 实现三阶段 Capture**

首跳 `NAVIGATE` 完成后只截图、DOM、可见文本和 UI snapshot，标记为 `web_entry_probe`，不做任何自动关闭。年龄门禁结束后，进入首页普通阻断处理；普通阻断清空后才生成 `web_page_ready_baseline`。菜单页和详情页使用 `web_page_probe -> 清障 -> web_page_ready_baseline`。所有新字段默认关闭，H5/App 调用不传时行为不变。

- [ ] **Step 4: 运行 Capture 回归**

Run: `python -m pytest services/api/tests/test_web_exploration_workflow.py services/api/tests/test_web_desktop_capture_policy.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_runtime_composition.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_exploration_workflow.py services/api/app/infrastructure/browser/web_desktop_capture_policy.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_web_exploration_workflow.py services/api/tests/test_web_desktop_capture_policy.py services/api/tests/test_web_capture_runtime.py
git commit -m "[AI编程] refactor(web): 拆分入口与页面就绪采集" -m "涉及模块：Web Capture 编排；数据库迁移：无；前端菜单/路由：无需同步；测试风险：需重点回归 H5/App 默认采集策略不变。"
```

### Task 6: 产出 effective z-index 阻断候选和密封候选 ID

**Files:**

- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Create: `services/api/app/domain/exploration/web_exploration.py`
- Test: `services/api/tests/test_page_capture_tool.py`
- Test: `services/api/tests/test_web_obstruction_snapshot.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebInteractionCandidate:
    candidate_id: str
    capture_id: str
    capability: str
    text: str
    tag_name: str
    role: str | None
    rect: dict[str, float]
    selector: str
    effective_z_index: int
    visible: bool
    hit_test_passed: bool
```

- [ ] **Step 1: 写失败测试，区分自身 z-index 与祖先堆叠层**

```python
def test_candidate_uses_effective_ancestor_z_index() -> None:
    candidate = snapshot_candidate(
        element_style="z-index:auto",
        ancestor_style="position:fixed;z-index:900",
    )
    assert candidate["effective_z_index"] == 900
    assert candidate["candidate_id"].startswith("web-candidate:")


def test_low_z_or_non_hit_test_element_is_not_clickable_obstruction() -> None:
    assert obstruction_candidates(snapshot(z_index=500)) == ()
    assert obstruction_candidates(snapshot(z_index=900, hit_test=False)) == ()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_obstruction_snapshot.py services/api/tests/test_page_capture_tool.py -q`

Expected: FAIL，当前快照没有 effective z-index 和 Web 密封候选 ID。

- [ ] **Step 3: 实现只读候选生成**

在浏览器侧计算元素及定位祖先的有效堆叠值，结合 fixed/sticky、viewport 覆盖率、可见性和 `elementFromPoint` Hit Test 产出候选。selector 只保存在可信快照；对 Agent/LLM 暴露 `candidate_id`、文本、角色、几何、z-index 和截图索引，不暴露可自由拼接的 selector。

候选 ID 必须绑定 `capture_id + DOM fingerprint + rect`，后续点击前重新计算并验证仍指向同一元素。

- [ ] **Step 4: 运行快照和通用 Capture 测试**

Run: `python -m pytest services/api/tests/test_web_obstruction_snapshot.py services/api/tests/test_page_capture_tool.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/infrastructure/browser/page_capture.py services/api/app/domain/exploration/web_exploration.py services/api/tests/test_web_obstruction_snapshot.py services/api/tests/test_page_capture_tool.py
git commit -m "[AI编程] feat(web): 增加阻断层密封候选快照" -m "涉及模块：浏览器证据与 Web 探索领域；数据库迁移：无；前端菜单/路由：无需同步；测试风险：复杂 stacking context 需真实站点 shadow 验证。"
```

### Task 7: 增加 MAF 年龄门禁识别与安全点击闭环

**Files:**

- Create: `services/api/app/application/web_age_gate.py`
- Create: `services/api/app/agents/explorer/web_assessment_agent.py`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Test: `services/api/tests/test_web_age_gate.py`
- Create: `services/api/tests/test_web_semantic_target_resolver.py`
- Create: `services/api/tests/test_web_exploration_assessment_agent.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebAgeGateAssessment:
    gate_present: bool
    confirmation_candidate_id: str | None
    confidence: float
    reason: str
    human_verification_present: bool

class WebAgeGateAssessmentPort(Protocol):
    async def assess(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        candidates: tuple[dict[str, object], ...],
    ) -> WebAgeGateAssessment: ...
```

- [ ] **Step 1: 写失败测试，锁定允许与禁止的识别结果**

```python
async def test_age_gate_clicks_only_llm_selected_safe_candidate() -> None:
    assessor.result = WebAgeGateAssessment(
        gate_present=True,
        confirmation_candidate_id="web-candidate:confirm-18",
        confidence=0.98,
        reason="页面要求确认已满18岁",
        human_verification_present=False,
    )
    result = await handler.handle(entry_capture)
    assert result.clicked_candidate_id == "web-candidate:confirm-18"
    assert result.gate_disappeared is True


async def test_human_verification_is_never_clicked() -> None:
    assessor.result = HUMAN_VERIFICATION_RESULT
    with pytest.raises(WebHumanVerificationBlockedError):
        await handler.handle(entry_capture)
    assert coordinator.intents == []


async def test_maf_agent_returns_schema_validated_candidate_id_only() -> None:
    result = await agent.assess_age_gate(ENTRY_EVIDENCE)
    assert result.confirmation_candidate_id == "web-candidate:confirm-18"
    assert not hasattr(result, "selector")
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_age_gate.py services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_web_exploration_assessment_agent.py -q`

Expected: FAIL，Web 年龄判定端口和候选模式尚不存在。

- [ ] **Step 3: 实现年龄门禁状态机**

顺序固定为：验证冻结的成年声明 -> 将入口截图、DOM 和候选交给 MAF -> 得到候选 ID -> resolver 从源 Capture 读取 selector -> 复验文本含“已成年/满18/进入”等确认语义且不含验证码/登录/支付/下载 -> 点击 -> Capture -> 验证门禁消失。门禁不存在时不点击。LLM 低置信、候选缺失、候选漂移或门禁未消失都进入人工复核。

`semantic_target_resolver.py` 新增 `web_sealed_candidate` 模式，只接受候选 ID；不得把 selector 放入 `ActionIntent.parameters`。

`web_assessment_agent.py` 使用仓库既有 Microsoft Agent Framework 客户端构建方式，定义无副作用、无浏览器工具的结构化判定 Agent。它统一承载年龄门禁、普通阻断、五类页面和详情候选选择四类只读判断；每种响应都经过 Pydantic/数据类闭集校验，禁止自然语言结果直接驱动点击。

- [ ] **Step 4: 运行年龄门禁、resolver 和 H5 年龄回归**

Run: `python -m pytest services/api/tests/test_web_age_gate.py services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_age_confirm_clear.py -q`

Expected: PASS；Web 与 H5 年龄流程互不调用。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_age_gate.py services/api/app/agents/explorer/web_assessment_agent.py services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/app/infrastructure/browser/capture_runtime.py services/api/tests/test_web_age_gate.py services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_web_exploration_assessment_agent.py
git commit -m "[AI编程] feat(web): 增加成年门禁安全确认" -m "涉及模块：Web 入口门禁与语义目标解析；数据库迁移：无；前端菜单/路由：无需同步；测试风险：LLM 低置信和人机验证统一进入人工复核。"
```

### Task 8: 实现每页逐层阻断清理与就绪门

**Files:**

- Create: `services/api/app/application/web_page_readiness.py`
- Modify: `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- Modify: `services/api/app/application/web_exploration_workflow.py`
- Test: `services/api/tests/test_web_page_readiness.py`
- Test: `services/api/tests/test_web_desktop_capture_policy.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebPageReadinessResult:
    status: Literal["ready", "popup_blocked", "human_verification_blocked"]
    ready_capture_id: str | None
    dismissed_layers: tuple[str, ...]
    evidence_capture_ids: tuple[str, ...]

class WebPageReadinessCoordinator:
    MAX_LAYERS = 12
    MAX_ELAPSED_SECONDS = 120
    STABLE_WAIT_SECONDS = 5

    async def prepare(self, *, source_capture_id: str, page_role: str) -> WebPageReadinessResult: ...
```

- [ ] **Step 1: 写失败测试覆盖多层、无弹窗、签名不变和预算耗尽**

```python
async def test_closes_multiple_obstructions_until_ready() -> None:
    result = await coordinator.prepare(source_capture_id="capture-1", page_role="menu")
    assert result.status == "ready"
    assert len(result.dismissed_layers) == 3
    assert result.ready_capture_id == "capture-ready"


async def test_unchanged_obstruction_signature_stops_current_page() -> None:
    result = await coordinator.prepare(source_capture_id="capture-1", page_role="detail")
    assert result.status == "popup_blocked"
    assert driver.click_count == 1
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_page_readiness.py services/api/tests/test_web_desktop_capture_policy.py -q`

Expected: FAIL，现清障是 Capture 内隐式策略，不能表达逐层 LLM 决策和终态。

- [ ] **Step 3: 实现逐层闭环**

每一层执行：Capture -> 根据截图、DOM、effective z-index 候选由 MAF 判断是否阻断 -> 返回关闭候选 ID -> 代码复验 -> 点击 -> 等待稳定 -> 新 Capture -> 比较阻断签名。没有阻断时立即形成 `web_page_ready_baseline`。年龄门禁只允许在首跳年龄阶段处理；普通页面发现年龄门禁或人机验证时不复用普通关闭器。

- [ ] **Step 4: 运行就绪门和 Capture 回归**

Run: `python -m pytest services/api/tests/test_web_page_readiness.py services/api/tests/test_web_desktop_capture_policy.py services/api/tests/test_web_exploration_workflow.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_page_readiness.py services/api/app/infrastructure/browser/web_desktop_capture_policy.py services/api/app/application/web_exploration_workflow.py services/api/tests/test_web_page_readiness.py services/api/tests/test_web_desktop_capture_policy.py
git commit -m "[AI编程] feat(web): 增加逐页阻断清理就绪门" -m "涉及模块：Web 页面就绪编排；数据库迁移：无；前端菜单/路由：无需同步；测试风险：弹窗层级和稳定等待需控制单页预算。"
```

### Task 9: 使用 MAF 对无遮挡基线判定五类页面

**Files:**

- Create: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/domain/exploration/web_exploration.py`
- Test: `services/api/tests/test_web_page_classification.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebPageAssessment:
    page_type: str | None
    confidence: float
    evidence: tuple[str, ...]
    needs_review_reason: str | None = None

class WebPageAssessmentPort(Protocol):
    async def classify(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        visible_text_artifact_id: str,
        allowed_page_types: tuple[str, ...],
    ) -> WebPageAssessment: ...
```

- [ ] **Step 1: 写失败测试，锁定证据门与五类闭集**

```python
async def test_classification_only_accepts_web_catalog_code() -> None:
    assessor.result = WebPageAssessment(
        page_type="web_video_list",
        confidence=0.96,
        evidence=("重复视频卡片", "多个详情链接"),
    )
    assert (await classifier.classify(READY_CAPTURE)).page_type == "web_video_list"


@pytest.mark.parametrize("page_type", ["web-home", "login", "unknown"])
async def test_out_of_catalog_result_becomes_needs_review(page_type: str) -> None:
    assessor.result = WebPageAssessment(page_type=page_type, confidence=0.99, evidence=())
    result = await classifier.classify(READY_CAPTURE)
    assert result.page_type is None
    assert result.needs_review_reason == "WEB_PAGE_TYPE_UNSUPPORTED"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_page_classification.py -q`

Expected: FAIL，分类服务尚不存在。

- [ ] **Step 3: 实现 Catalog 约束的多模态分类**

仅允许读取 `web_page_ready_baseline` 的截图、DOM 和可见文本。首页由首跳真实首页上下文与主体结构共同判断；列表强调重复卡片/条目，详情强调单主体；视频类型必须有播放器或视频资源结构证据。URL 和菜单文案只能作为弱佐证，不能单独签发类型。证据冲突、置信不足或第六类结果统一返回 `needs_review`，不得回退 H5 Catalog。

- [ ] **Step 4: 运行分类和 Catalog 测试**

Run: `python -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_type_catalog.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_page_classification.py services/api/app/domain/exploration/web_exploration.py services/api/tests/test_web_page_classification.py
git commit -m "[AI编程] feat(web): 增加五类页面证据判定" -m "涉及模块：Web 页面分类；数据库迁移：无；前端菜单/路由：无需同步；测试风险：真实站点页面结构差异需 shadow 样本校准。"
```

### Task 10: 对账白名单并执行当前站全部菜单 Frontier

**Files:**

- Create: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_desktop_exploration.py`
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Modify: `services/api/app/domain/exploration/web_exploration.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_desktop_exploration.py`

**Interfaces:**

```python
class WebMenuTerminalStatus(StrEnum):
    HOME_BASELINE = "home_baseline"
    DETAIL_CAPTURED = "detail_captured"
    MENU_PAGE_CAPTURED = "menu_page_captured"
    NO_DETAIL_CANDIDATE = "no_detail_candidate"
    POPUP_BLOCKED = "popup_blocked"
    NAVIGATION_FAILED = "navigation_failed"
    CROSS_ORIGIN_BLOCKED = "cross_origin_blocked"
    ALLOWLIST_DRIFTED = "allowlist_drifted"
    RUNTIME_DISCOVERED_UNREVIEWED = "runtime_discovered_unreviewed"

@dataclass(frozen=True, kw_only=True)
class WebMenuLedgerEntry:
    entry_id: str
    status: WebMenuTerminalStatus
    source_capture_id: str
    final_capture_id: str | None
    page_type: str | None
    detail_capture_id: str | None
    error_code: str | None
```

- [ ] **Step 1: 写失败测试，证明单站全菜单而不是页面类型去重**

```python
async def test_91porna_finishes_every_catalog_entry() -> None:
    summary = await explorer.explore(command_for("http://91porna.com/"), initial_capture_id="home")
    assert summary.menu_total == 65
    assert summary.status_counts["home_baseline"] == 1
    assert summary.status_counts["cross_origin_blocked"] == 2
    assert summary.executed_same_origin_count == 62
    assert summary.frontier_remaining == 0


async def test_five_page_types_do_not_stop_remaining_menus() -> None:
    summary = await explorer.explore(command, initial_capture_id="home")
    assert set(summary.page_type_keys) == set(WEB_PAGE_TYPE_CODES)
    assert len(summary.menu_ledger) == len(site_catalog.entries)


async def test_duplicate_url_menu_paths_are_both_executed() -> None:
    summary = await explorer.explore(command_for("http://91porna.com/"), initial_capture_id="home")
    moviesets = [item for item in summary.menu_ledger if item.canonical_url.endswith("/moviesets")]
    assert len(moviesets) == 2
    assert all(item.status.is_terminal for item in moviesets)
```

- [ ] **Step 2: 运行探索测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_desktop_exploration.py -q`

Expected: FAIL，现 Explorer 最多 12 个 Page Type，且按 URL/Page Type 去重。

- [ ] **Step 3: 实现“目录对账 + 菜单身份 Frontier + Ledger”**

入口首页就绪后只加载当前 target origin 的目录。将运行时 DOM 菜单与目录按 host/path/query、菜单路径和可解释文字对账：

- `home_baseline` 绑定首页 Capture，不重复 GOTO；
- `allowlisted_observe_only` 且 DOM 精确存在才入 Frontier；
- 跨域目录条目直接形成 `cross_origin_blocked`；
- 目录条目缺失或 href 漂移形成 `allowlist_drifted`，禁止使用旧 URL 强行直达；
- DOM 新菜单形成 `runtime_discovered_unreviewed`，只审计不执行；
- 一个菜单失败只关闭该 Ledger 项，继续下一个；
- 同 URL 的不同 `entry_id` 分别执行并各自留证；
- 只有 `frontier_remaining == 0` 且每个目录/运行时条目都有终态才完成。

删除 `max_page_types=12` 作为完成限制；保留全局 Run deadline，并可增加不改变菜单覆盖的失败保护。页面样本可按指纹复用，但不能省略菜单路径执行证据。

- [ ] **Step 4: 运行菜单、导航和单站计数测试**

Run: `python -m pytest services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_desktop_exploration.py services/api/tests/test_web_desktop_navigation.py -q`

Expected: PASS；91porna 单次 Run 为 65 个终态，六站合计测试为 166 个终态。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_menu_exploration.py services/api/app/application/web_desktop_exploration.py services/api/app/application/web_desktop_navigation.py services/api/app/domain/exploration/web_exploration.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_desktop_exploration.py
git commit -m "[AI编程] feat(web): 改为站点全菜单终态探索" -m "涉及模块：Web 菜单探索与审计账本；数据库迁移：无；前端菜单/路由：无需同步；测试风险：长菜单站点受 Run deadline 影响时应输出 partial 而非伪成功。"
```

### Task 11: 从可信列表 DOM 进入一层详情并返回菜单流程

**Files:**

- Create: `services/api/app/application/web_list_detail_navigation.py`
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Test: `services/api/tests/test_web_list_detail_navigation.py`
- Test: `services/api/tests/test_web_menu_exploration.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebDetailCandidate:
    candidate_id: str
    source_capture_id: str
    canonical_url: str
    expected_page_type: Literal["web_video_detail", "web_content_detail"]
    evidence: tuple[str, ...]

def detail_candidates_from_ready_snapshot(
    snapshot: Mapping[str, object],
    *,
    list_page_type: str,
    origin: str,
) -> tuple[WebDetailCandidate, ...]: ...
```

- [ ] **Step 1: 写失败测试，禁止生成 URL 或把菜单/广告当详情**

```python
def test_video_list_selects_visible_repeated_card_detail() -> None:
    candidates = detail_candidates_from_ready_snapshot(VIDEO_LIST_SNAPSHOT, list_page_type="web_video_list", origin=ORIGIN)
    assert candidates[0].expected_page_type == "web_video_detail"
    assert candidates[0].candidate_id == "web-candidate:card-1"


@pytest.mark.parametrize("snapshot", [MENU_ONLY, PAGINATION_ONLY, CROSS_ORIGIN_AD, HIDDEN_CARD])
def test_untrusted_structures_do_not_create_detail_candidate(snapshot) -> None:
    assert detail_candidates_from_ready_snapshot(snapshot, list_page_type="web_content_list", origin=ORIGIN) == ()
```

- [ ] **Step 2: 运行详情测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_menu_exploration.py -q`

Expected: FAIL，Web 尚无独立列表详情合同。

- [ ] **Step 3: 实现一层可信详情采集**

候选只能来自当前无遮挡 Capture 的重复卡片/列表条目真实 `<a href>`，且同源、可见、矩形有效、Hit Test 通过、非导航/分页/面包屑/登录/广告/下载/支付。MAF 可以在已密封候选中选择一个，但不能返回 URL 或 selector。点击后重新执行 `page_probe -> 逐层清障 -> ready baseline -> 五类分类`，并校验为对应详情类型；随后使用受控历史返回或重新导航到下一白名单菜单，不递归探索详情内链接。

没有可信候选时形成 `no_detail_candidate`；菜单本身已经是详情页时形成 `detail_captured`；详情失败不阻止下一菜单。

- [ ] **Step 4: 运行详情、resolver 和菜单回归**

Run: `python -m pytest services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_semantic_target_resolver.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_list_detail_navigation.py services/api/app/application/web_menu_exploration.py services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_semantic_target_resolver.py
git commit -m "[AI编程] feat(web): 增加列表到详情可信采集" -m "涉及模块：Web 列表详情导航；数据库迁移：无；前端菜单/路由：无需同步；测试风险：无可信详情候选时必须保留 no_detail_candidate 事实。"
```

### Task 12: 组合生产 Runtime、完整摘要和 1920×1080 压缩合同

**Files:**

- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/agents/runtime/models.py`
- Modify: `services/api/app/infrastructure/queue/redis_queue.py`
- Modify: `services/api/app/worker.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_worker_web_v2_route.py`
- Create: `services/api/tests/test_web_screenshot_contract.py`

**Interfaces:**

```python
@dataclass
class RunResult:
    # 既有字段保持原顺序
    web_exploration_summary: dict[str, object] | None = None

class AgentRunRecord(BaseModel):
    # 既有字段保持原顺序
    web_exploration_summary: dict[str, object] | None = None
```

摘要至少包含：`site_id`、Catalog 版本、成年声明审计引用、年龄门禁结果、首页基线、菜单总数、每个菜单 Ledger、状态计数、五类页面样本、详情检查、运行时新增菜单、目录漂移、页面时间线和证据缺口。

- [ ] **Step 1: 写失败测试，锁定生产组合、单站摘要和图像尺寸**

```python
async def test_runtime_composes_age_readiness_catalog_and_full_menu_services() -> None:
    runtime = await PostgresWebRuntimeProvider().build(record=record, settings=settings)
    assert runtime.age_gate_handler is not None
    assert runtime.page_readiness is not None
    assert runtime.menu_registry.resolve_site(record_target_url).site_id == "91porna.com"


def test_executor_reports_menu_terminal_counts_not_page_type_coverage() -> None:
    result = executor_result_for_91porna()
    assert result.web_exploration_summary["menu_total"] == 65
    assert result.web_exploration_summary["frontier_remaining"] == 0
    assert "65 个菜单终态" in result.final_text


def test_web_screenshot_compression_keeps_1920_by_1080() -> None:
    raw, optimized = capture_and_optimize_desktop_fixture()
    assert image_size(raw) == (1920, 1080)
    assert image_size(optimized) == (1920, 1080)
    assert len(optimized) <= len(raw)
```

- [ ] **Step 2: 运行生产组合测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_runtime_composition.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_web_screenshot_contract.py -q`

Expected: FAIL，生产组合尚未注入新服务，结果没有菜单摘要合同。

- [ ] **Step 3: 完成 Runtime 组合和结果投影**

`PostgresWebRuntimeProvider` 显式组装 Web 年龄门禁、就绪门、Web Page Catalog、站点菜单 Registry、五类分类器、列表详情器和全菜单 Explorer。`ProductionWebV2RuntimeExecutor` 以菜单 Ledger 判断成功/部分成功，不再用 Page Type 数量作为完成标准。

`_apply_result` 将可 JSON 化摘要复制到 `AgentRunRecord.web_exploration_summary`。Capture 和 action trace 继续沿用现有持久化链路，不新增数据库表。站点目录未注册、成年声明缺失、人机验证、年龄门禁失败属于 `NEEDS_REVIEW`；预期的 `cross_origin_blocked` 不是运行失败。

截图仍通过现有压缩路径做文件体积优化；读取压缩前后图像头验证宽高不变。禁止通过 resize、thumbnail 或 viewport 降级实现压缩。

- [ ] **Step 4: 运行 Web 组合、Worker 和截图合同测试**

Run: `python -m pytest services/api/tests/test_web_runtime_composition.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_web_screenshot_contract.py services/api/tests/test_web_browser_profile.py -q`

Expected: PASS；Profile 和每张截图均保持 1920×1080。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/application/web_runtime_composition.py services/api/app/agents/runtime/models.py services/api/app/infrastructure/queue/redis_queue.py services/api/app/worker.py services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_web_screenshot_contract.py
git commit -m "[AI编程] feat(web): 组合全菜单探索运行时与摘要" -m "涉及模块：Web Runtime、Worker、截图合同；数据库迁移：无；前端菜单/路由：无需同步；测试风险：长任务摘要大小和真实图像压缩率需监控。"
```

### Task 13: 六站 shadow 验证、规则同步和全平台回归

**Files:**

- Create: `scripts/web_v2_six_site_shadow.py`
- Create: `docs/reports/2026-08-30-web-v2-six-site-shadow-validation.md`
- Modify: `docs/专项能力与高频回归规则.md`
- Test: `services/api/tests/test_web_six_site_shadow_fixtures.py`
- Test: `services/api/tests/test_web_architecture_boundaries.py`

**Interfaces:**

```python
SIX_SITE_TARGETS = (
    "https://heiliao.com/",
    "https://hl365.com/",
    "https://www.91jav1.com/",
    "http://91porna.com/",
    "http://huangguoai.com/",
    "https://avhub360.com/",
)
```

- [ ] **Step 1: 写离线失败测试，使用已审计 HTML 固件验证六站结构**

```python
def test_six_site_fixture_aggregate_matches_catalog() -> None:
    report = reconcile_all_fixture_sites()
    assert report.active_anchor_count == 166
    assert report.home_baseline_count == 6
    assert report.allowlisted_observe_only_count == 157
    assert report.blocked_cross_origin_count == 3
    assert report.comment_only_anchor_count == 4


def test_web_modules_do_not_import_h5_or_app_explorers() -> None:
    violations = web_architecture_violations()
    assert violations == ()
```

- [ ] **Step 2: 运行离线六站和架构边界测试并确认失败**

Run: `python -m pytest services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_web_architecture_boundaries.py -q`

Expected: FAIL，shadow 工具和新边界断言尚不存在。

- [ ] **Step 3: 实现显式 opt-in 的 shadow 工具和详细报告**

脚本默认只对附件固件、生产 Catalog 和解析器做离线对账；只有显式 `--live`、提供成年声明且运行环境允许网络时，才按六个独立任务顺序运行。每站报告必须列出：

1. 目标与 Browser Profile；
2. 首跳、年龄门禁和普通阻断处理过程；
3. 目录版本与 DOM 对账结果；
4. 每个菜单路径、URL、目录判定、执行终态、页面类型和证据 Capture ID；
5. 列表菜单的详情候选与详情类型；
6. 漂移、新菜单、跨域阻断、失败原因和剩余 Frontier；
7. 截图尺寸、压缩前后字节数与无尺寸压缩证明；
8. 当前站完成条件，以及六站活动的 166 项汇总。

Live 模式发现人机验证必须停止当前站，不点击、不绕过，并在报告中记为 `human_verification_blocked`。报告不得包含敏感 Cookie、令牌、完整个人数据或不必要的露骨截图预览。

- [ ] **Step 4: 更新专项规则**

把旧规则中“class 含 nav 且必须 `ul/li`”改为“语义 nav、class/id/ARIA/data 模糊 nav 信号并集，`ul/li` 是强信号但非硬条件”；补充双 Catalog、成年自我声明、每页就绪门、单站全菜单终态、详情只来自可信 DOM、1920×1080 文件体积压缩不改尺寸、人机验证不处理，以及 Web/H5/App 隔离回归要求。

- [ ] **Step 5: 执行离线验收和完整回归**

Run: `python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_age_gate.py services/api/tests/test_web_page_readiness.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_screenshot_contract.py services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_web_architecture_boundaries.py -q`

Run: `python -m pytest services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker_app_route.py -q`

Run: `python -m ruff check services/api/app/application services/api/app/domain/page services/api/app/domain/exploration services/api/app/infrastructure/browser services/api/tests/test_web_*.py scripts/web_v2_six_site_shadow.py`

Expected: 全部 PASS，无 H5/App 行为差异。

- [ ] **Step 6: 提交**

```bash
git add scripts/web_v2_six_site_shadow.py docs/reports/2026-08-30-web-v2-six-site-shadow-validation.md docs/专项能力与高频回归规则.md services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_web_architecture_boundaries.py
git commit -m "[AI编程] test(web): 增加六站全菜单验收" -m "涉及模块：Web shadow 验证与专项规则；数据库迁移：无；前端菜单/路由：无需同步；测试风险：live 验证受站点可用性、网络和人机验证影响，离线合同必须始终可重复。"
```

---

## 最终验收门

实现完成前逐项确认：

- [ ] Web 提交缺少 `declared=true` 的成年声明时失败关闭，操作者和时间由服务端冻结。
- [ ] 首跳先保留截图和 DOM，再由 MAF 判断 18 岁门禁；未发现门禁时零点击。
- [ ] 年龄按钮只能通过源 Capture 的候选 ID 点击；selector 不进入 Agent 参数。
- [ ] 发现验证码或人机验证时停止，不尝试点击或绕过。
- [ ] 首页、菜单页、详情页均在无遮挡基线后分类。
- [ ] 普通阻断支持连续多层，遵守 12 层、120 秒、5 秒和签名不变保护。
- [ ] Web Catalog 只有五类，加载失败不回退 H5 Catalog。
- [ ] 当前 Run 只加载当前站点目录，未知站点失败关闭。
- [ ] Page Type 全覆盖后仍执行当前站未完成菜单。
- [ ] 相同 URL 的不同菜单路径分别执行并分别留证。
- [ ] 运行时新菜单只审计不执行，白名单漂移不使用旧 URL 直达。
- [ ] 列表详情 URL 只来自当前可信 DOM，LLM 不能生成 URL。
- [ ] 每站 Frontier 为空且所有菜单进入终态才完成；六站汇总为 166 个活动菜单终态。
- [ ] 六站汇总严格为 6 个首页、157 个同源观察和 3 个跨域阻断。
- [ ] 所有截图保持 1920×1080，仅压缩文件体积，不缩放尺寸。
- [ ] H5 线路加载、启动广告、H5 Catalog、H5 年龄确认和 App Runtime 结果不变。
- [ ] 输出包含每站、每菜单、每详情的过程、结果、证据 ID、失败原因和目录漂移。

## 设计覆盖自审

| 设计要求 | 实施任务 |
|---|---|
| 双 Catalog 与五类闭集 | Task 1、2、9 |
| 成年声明与 18 岁门禁 | Task 3、5、7 |
| 人机验证不处理 | Task 7、8、13 |
| 每页普通阻断清理 | Task 5、6、8 |
| nav 模糊结构和菜单身份 | Task 4、10 |
| 单站全菜单而非类型提前结束 | Task 10、12 |
| 列表到下一层详情 | Task 11 |
| 1920×1080 只压体积 | Task 12、13 |
| 详细过程与结果输出 | Task 10、12、13 |
| H5/App 隔离 | Task 3、5、7、12、13 |

计划执行时，Task 1 至 Task 13 必须按顺序推进；每个 Task 的失败测试、最小实现、通过测试和提交都是独立检查点。Live 六站验证只能在离线合同、架构边界和 H5/App 回归全部通过后执行。
