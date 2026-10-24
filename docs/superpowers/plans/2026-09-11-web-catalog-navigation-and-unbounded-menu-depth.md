# Web Catalog Navigation and Unbounded Menu Depth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让附件中的首页、导航和站点特征成为可验证的 Web 运行时知识，并实现不限制菜单树层级、但受同源安全、10 次总动作预算与 deadline 约束的菜单探索。

**Architecture:** 将 Web 页面类型闭集扩展为 `home`、`content_list`、`content_detail`、`video_list`、`video_detail` 五类，用组合式 `WebPageTypeCatalog` 包装通用 `PageTypeCatalog`，强类型加载导航信号、站点观察和机器规则。`page_role=home` 只表达探索流程中的根页面身份，`page_type=home` 才是正式分类结果；两者分别保存并交叉校验。分类器分别计算业务主体事实与导航事实；菜单发现先验证导航容器，再产生候选。静态菜单目录降级为可选先验，运行时候选通过稳定父链身份递归发现，数据库保存其深度和 lineage。旧四类冻结快照按原版本继续恢复，新运行使用五类协议。

**Tech Stack:** Python 3.12、FastAPI、dataclasses、PostgreSQL、Alembic、pytest、Playwright Capture DOM 快照。

**Spec:** `docs/superpowers/specs/2026-09-11-web-catalog-navigation-and-unbounded-menu-depth-design.md`

## Global Constraints

- `home` 是正式页面类型；`WEB_PAGE_TYPE_CODES` 更新为 `home`、`content_list`、`content_detail`、`video_list`、`video_detail`。`page_role=home` 仍保留，但不能替代 `page_type=home`。
- 页面类型以 DOM、截图和结构事实为主；裸域名、规范根路径或可信根重定向是 `home` 的强正向证据，静态菜单目录只提供辅助先验。
- 旧 `web-page-type-assessment.v1/v2` 与 `web-coverage-snapshot.v1/v2` 保持四类冻结语义；新运行使用五类 v3 协议，禁止用新常量反向破坏历史快照。
- 菜单树不设置层级上限；总菜单动作预算保持 10，目录内外候选共用预算。
- 只自动执行当前站点同源、可见、低风险菜单；登录、注册、支付、下载、提交、删除、授权和跨域动作继续阻断。
- 所有新增注释使用中文，所有文件保持 UTF-8。
- 每个生产行为必须先写一个会因缺少该行为而失败的测试，并实际观察 RED 后再实现。

---

### Task 1: Web 专属 Catalog 根模型与附件特征

**Files:**
- Create: `services/api/app/domain/page/web_page_knowledge.py`
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Modify: `services/api/app/domain/page/__init__.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`
- Test: `services/api/tests/test_web_run_policy_snapshot.py`

**Interfaces:**
- Consumes: `PageTypeCatalog`、`PageTypeDefinition` 和现有 `_parse_catalog(payload)`。
- Produces: `WebPageTypeCatalog.core`、`platform`、`navigation_signals`、`common_site_observations`，以及兼容属性 `version`、`source`、`page_types`、`codes`、`require_code()`。`home` 与另外四类一样由 `page_types` 和 `require_code()` 暴露。机器规则在 Task 3 加入。

- [ ] **Step 1: 写附件特征和根字段失败测试**

在 `test_web_page_type_catalog.py` 增加真实加载测试，断言首页角色与顶层字段能够被消费，而不是只检查 JSON 文本：

```python
def test_web_catalog_retains_home_navigation_and_site_observations() -> None:
    catalog = _load_web_catalog_module().load_web_page_type_catalog()

    assert catalog.platform == "web"
    assert catalog.codes == EXPECTED_WEB_PAGE_TYPES
    assert catalog.require_code("home").required_positive_signals == (
        "页面顶部存在完整主导航（含多个分类入口、搜索、登录/注册等）",
        "主体区域同时出现多个内容板块（更新、热门、推荐、分类入口、轮播等）",
        "存在多个并列的内容卡片（视频或帖子），而非单一内容主体",
    )
    assert catalog.navigation_signals.common_positive_signals
    assert catalog.common_site_observations.age_gate
```

再参数化破坏 `platform`、`page_types.home`、`navigation_signals` 和 `common_site_observations`，断言 `parse_web_page_type_catalog()` 抛出 `WebPageTypeCatalogError`。加入额外根字段时同样必须失败。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py -q`

Expected: FAIL，原因是返回对象没有 `platform` 或正式 `home` 页型，以及非法顶层字段仍被接受。

- [ ] **Step 3: 实现 Web 专属强类型模型**

在 `web_page_knowledge.py` 定义组合模型：

```python
@dataclass(frozen=True)
class WebNavigationSignals:
    common_positive_signals: tuple[str, ...]
    supporting_signals: tuple[str, ...]
    strong_negative_signals: tuple[str, ...]
    usage_notes: tuple[str, ...]


@dataclass(frozen=True)
class WebCommonSiteObservations:
    age_gate: str
    ad_overlay: str
    mixed_content: str
    navigation: str
    player: str


@dataclass(frozen=True)
class WebPageTypeCatalog:
    core: PageTypeCatalog
    platform: Literal["web"]
    navigation_signals: WebNavigationSignals
    common_site_observations: WebCommonSiteObservations

    @property
    def codes(self) -> tuple[str, ...]:
        return self.core.codes

    def require_code(self, code: str) -> PageTypeDefinition:
        return self.core.require_code(code)
```

在 Web 解析器中使用根字段精确集合校验。通用 `_parse_catalog` 只接收其认识的字段副本，Web 专属字段由 Web 解析器自行构造；`PageTypeRegistry` 只验证 `catalog.core`。

- [ ] **Step 4: 将附件 `home` 特征写入正式 `page_types`**

在 `web_page_type_catalog.json` 增加：

```json
{
  "code": "home",
  "name": "Web首页",
  "status": "supported",
  "category": "business",
  "coverage_required": true,
  "rule_matchable": true,
  "purpose": "站点入口与综合推荐页，聚合主导航、多板块热门内容、视频/帖子卡片、广告与官方入口。",
    "required_positive_signals": [
      "页面顶部存在完整主导航（含多个分类入口、搜索、登录/注册等）",
      "主体区域同时出现多个内容板块（更新、热门、推荐、分类入口、轮播等）",
      "存在多个并列的内容卡片（视频或帖子），而非单一内容主体"
    ],
    "supporting_signals": [
      "URL 为站点根路径或极简首页路径",
      "存在站点标识（logo、名称、口号）及官方推广信息",
      "常见年龄确认层、广告弹窗或悬浮入口",
      "页面可滚动加载多组推荐内容，结构呈聚合态而非单一列表"
    ],
    "strong_negative_signals": [
      "页面主体仅聚焦单个视频播放器或单篇完整正文",
      "仅为某一分类下的纯列表，缺少首页特有的多板块聚合",
      "被完整阻断层遮挡，关闭后仍无业务主体"
    ],
    "confusable_with": ["content_list", "video_list"],
    "missing_evidence_actions": [
      "关闭年龄门与广告弹窗后重新采集",
      "滚动完整页面确认是否存在多板块结构",
      "检查顶部导航与站点标识是否存在"
    ],
  "runtime_traits": ["stable", "desktop_web", "homepage_aggregate"],
  "aliases": ["home", "Web首页", "首页"],
  "signals": {
    "ocr_keywords": ["首页", "热门", "推荐", "更新"],
    "dom_hints": ["home-page", "homepage", "site-home", "main-navigation"],
    "layout_hints": ["顶部主导航下同时存在多个内容板块和并列内容卡片"]
  },
  "entry_hints": [],
  "exit_hints": ["打开当前导航容器中的同源分类菜单"],
  "next_page_candidates": [],
  "risk_actions": ["登录", "注册", "支付", "下载", "跳转广告"],
  "related_rules": [],
  "route_prefixes": [],
  "navigation_aliases": [],
  "default_detail_page_type": null,
  "source": "Web 五类业务页面设计",
  "remark": "裸域名或规范根路径是首页强证据，最终结果同时保存 DOM、截图、导航和多板块证据。"
}
```

把该对象放入 `page_types`，并保留用户已写入的另外四类特征、`navigation_signals` 和 `common_site_observations`。`content_list` 与 `video_list` 的 `confusable_with` 增加 `home`。发布顺序固定为 `home`、`content_list`、`content_detail`、`video_list`、`video_detail`。

- [ ] **Step 5: 让运行策略冻结并恢复完整 Web Catalog**

把 `FrozenWebPolicy.catalog` 类型改成 `WebPageTypeCatalog`。`load_published_web_policy()` 继续保存原始 JSON；`materialize_web_policy()` 必须恢复 Web 专属字段并校验 `guide_version == catalog.version`。

- [ ] **Step 6: 运行 Catalog 与策略测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_run_policy_snapshot.py -q`

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add services/api/app/domain/page/web_page_knowledge.py services/api/app/domain/page/web_page_catalog.py services/api/app/domain/page/web_page_type_catalog.json services/api/app/domain/page/__init__.py services/api/app/application/web_run_policy.py services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_run_policy_snapshot.py
git commit -m "[AI编程] feat(web-catalog): 强类型化首页与导航识别知识；无数据库迁移；无需前端菜单路由同步；暂未覆盖真实站点视觉回归"
```

### Task 2: 五类页型协议、适用性、覆盖与历史兼容

**Files:**
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/exploration/web_coverage_snapshot.py`
- Modify: `services/api/app/application/web_page_applicability.py`
- Modify: `services/api/app/domain/exploration/web_discovery_policy.py`
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Modify: `services/api/app/domain/page/web_site_menu_catalog.json`
- Modify: `services/api/app/api/v2/schemas.py`
- Modify: Web assessment、coverage、capture evidence、completion gate 与 report repository 中写死的 v2 版本过滤
- Create: `services/api/migrations/versions/20260911_0107_web_home_page_type.py`
- Test: `services/api/tests/test_web_page_applicability.py`
- Test: `services/api/tests/test_web_coverage_snapshot.py`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_home_page_type_migration.py`

**Interfaces:**
- 新运行发布 `web-page-type-assessment.v3` 与 `web-coverage-snapshot.v3`，规范闭集为五类。
- 历史 v1/v2 assessment 与 coverage snapshot 按其冻结的四类闭集恢复，不用当前 `WEB_PAGE_TYPE_CODES` 重新解释。
- 新增 `strict_catalog` 适用性模式；API 暂时接受 `strict_four` 作为已废弃别名，但对新运行解析为五类并写入明确兼容原因，不能继续缩小分母。

- [ ] **Step 1: 写五类闭集和旧快照兼容失败测试**

断言新 Catalog、适用性快照、覆盖快照、首页基线和报告都包含 `home`；根 Capture 成功分类为 `page_type=home`。另构造冻结 v2 四类快照并确认仍可恢复，避免历史数据因常量扩展而失效。

```python
def test_new_web_coverage_requires_formal_home_type() -> None:
    snapshot = resolve_web_page_applicability(
        requested_mode="strict_catalog",
        requested_page_types=None,
        catalog_site=None,
    )
    assert snapshot.required_page_types == (
        "content_detail", "content_list", "home", "video_detail", "video_list"
    )


def test_legacy_v2_snapshot_keeps_frozen_four_type_denominator() -> None:
    restored = WebCoverageSnapshot.from_dict(_legacy_v2_payload())
    assert "home" not in restored.required_page_types
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_applicability.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_runtime_composition.py -q`

Expected: FAIL，当前常量和分母只有四类，首页基线仍可能签发列表类型。

- [ ] **Step 3: 扩展正式类型集合并显式区分历史协议**

定义 `WEB_HOME_PAGE_TYPES={"home"}`、现有 list/detail 集合和五类 `WEB_PAGE_TYPE_CODES`。v3 构造器使用五类集合；v1/v2 反序列化使用模块内固定的 legacy 四类集合。禁止以字符串替换的方式把所有 v2 查询直接改为 v3：读取链在迁移期必须按数据版本分支，写入链只产出 v3。

- [ ] **Step 4: 更新站点适用性和首页基线**

六个静态站点的 applicability 都将 `home` 放入 required；仅视频站点仍可把 content 类型置为 not_applicable。首页 `home_baseline` 的 `menu_page_type` 必须是 `home`；详情导航只允许从 list 页型启动。

- [ ] **Step 5: 增加 v3 数据库约束迁移**

Revision `20260911_0107`，`down_revision="20260911_0106"`。迁移允许 v3 assessment/coverage 版本，并保持 v1/v2 历史行合法；菜单账本约束允许 `home_baseline + menu_page_type=home`，list/detail 状态约束保持各自集合。

- [ ] **Step 6: 运行五类协议、仓储和迁移测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_applicability.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_home_page_type_migration.py -q`

Expected: PASS；新运行五类、旧快照四类、首页类型为 `home`。

- [ ] **Step 7: 提交本任务**

```bash
git add services/api/app services/api/migrations/versions/20260911_0107_web_home_page_type.py services/api/tests/test_web_page_applicability.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_home_page_type_migration.py
git commit -m "[AI编程] feat(web-page-type): 将home纳入五类覆盖协议并兼容旧快照；引入数据库迁移20260911_0107；无需前端菜单路由同步；全量报告回归在收口任务完成"
```

### Task 3: 机器可执行页面规则与精确 DOM token 匹配

**Files:**
- Modify: `services/api/app/domain/page/web_page_knowledge.py`
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Create: `services/api/app/application/web_page_recognition_rules.py`
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/agents/explorer/web_assessment_agent.py`
- Test: `services/api/tests/test_web_page_recognition_rules.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_exploration_assessment_agent.py`

**Interfaces:**
- Consumes: `WebPageStructureFacts` 和 `WebPageTypeCatalog.recognition_rules`。
- Produces: `evaluate_web_page_rules(page_type, facts, rules) -> WebPageRuleEvaluation`，其中结果包含 `required_satisfied`、`positive_hits`、`supporting_hits`、`strong_conflicts` 和 `missing_facts`。

- [ ] **Step 1: 写宽泛 token 误放行的失败测试**

```python
@pytest.mark.parametrize(
    ("page_type", "tokens"),
    (("content_list", ("item",)), ("video_detail", ("video-list",))),
)
def test_generic_or_cross_axis_token_cannot_satisfy_required_structure(page_type, tokens):
    facts = WebPageStructureFacts(
        navigation_element_count=0,
        repeated_link_count=2 if page_type.endswith("_list") else 0,
        primary_detail_link_count=2 if page_type.endswith("_list") else 0,
        repeated_primary_card_count=2 if page_type.endswith("_list") else 0,
        video_element_count=0,
        single_subject_element_count=0,
        dom_tokens=tokens,
    )
    result = evaluate_web_page_rules(
        page_type=page_type,
        facts=facts,
        rules=load_web_page_type_catalog().recognition_rules[page_type],
    )
    assert result.required_satisfied is False
```

- [ ] **Step 2: 运行规则测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_recognition_rules.py services/api/tests/test_web_page_classification.py -q`

Expected: FAIL，因为规则模型和 `evaluate_web_page_rules` 尚不存在。

- [ ] **Step 3: 定义规则结构与安全操作符**

实现不可执行任意表达式的规则结构：

```python
WebFactOperator = Literal["eq", "gte", "lte", "contains_exact"]

@dataclass(frozen=True)
class WebFactPredicate:
    fact: str
    operator: WebFactOperator
    value: bool | int | str

@dataclass(frozen=True)
class WebPredicateGroup:
    reason_code: str
    all_of: tuple[WebFactPredicate, ...]

@dataclass(frozen=True)
class WebPageRecognitionRules:
    required_all: tuple[WebFactPredicate, ...]
    required_any: tuple[WebFactPredicate, ...]
    supporting: tuple[WebFactPredicate, ...]
    strong_negative_any: tuple[WebPredicateGroup, ...]
```

解析时 `fact` 必须属于显式事实白名单；数值事实只允许 `eq/gte/lte`，`dom_tokens` 只允许 `contains_exact`。未知字段、操作符、空规则组和类型不匹配必须使 Catalog 加载失败。

- [ ] **Step 4: 写入五类精确规则**

规则至少覆盖以下固定契约：

```text
home.required_all = is_canonical_root_url==true, top_level_obstruction_count==0
home.required_any = top_navigation_container_count>=1, visible_category_entry_count>=2, homepage_section_count>=2, repeated_primary_card_count>=2
home.strong_negative = single_subject_element_count>=1 且 player_root_count/article_body_count 任一成立
video_list.required_all = repeated_link_count>=2, primary_detail_link_count>=2, repeated_primary_card_count>=2
video_list.required_any = media_card_count>=2, video_element_count>=1, dom_tokens exact video-list/video-grid/media-card
video_detail.required_any = video_element_count>=1, player_root_count>=1, dom_tokens exact video-player/video-js/player-root/playback
content_list.required_all = repeated_link_count>=2, primary_detail_link_count>=2, repeated_primary_card_count>=2
content_list.required_any = dom_tokens exact article-list/content-grid/post-list/content-card/article-card/post-card
content_detail.required_any = article_body_count>=1, paragraph_text_length>=120, dom_tokens exact article-body/post-content/entry-content/detail-content
all types strong negative = top_level_obstruction_count>=1
```

`is_canonical_root_url` 由裸域名、规范 `/` 路径或可信同源根重定向产生。根 URL 是 `home` 的强必需证据之一，但仍需至少一个导航、多板块或聚合卡片事实；单一播放器或长正文形成强冲突。删除 `content_list` 的 `item/card` 和 `content_detail` 的裸 `detail`，裸 `video/source/player` 不再作为确定性 token。

- [ ] **Step 5: 用规则评估替换硬编码结构门和强否定**

`WebPageClassifier.classify()` 只调用一次 `evaluate_web_page_rules()`；`_has_required_structure()` 和 `_strong_negative_conflicts()` 删除或降为兼容包装。把规则评估摘要加入 assessor payload，模型返回仍不能绕过规则门。

- [ ] **Step 6: 运行分类相关测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_recognition_rules.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_exploration_assessment_agent.py -q`

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add services/api/app/domain/page/web_page_knowledge.py services/api/app/domain/page/web_page_catalog.py services/api/app/domain/page/web_page_type_catalog.json services/api/app/application/web_page_recognition_rules.py services/api/app/application/web_page_classification.py services/api/app/agents/explorer/web_assessment_agent.py services/api/tests/test_web_page_recognition_rules.py services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_exploration_assessment_agent.py
git commit -m "[AI编程] fix(web-classification): 使用可审计规则替换宽泛DOM子串放行；无数据库迁移；无需前端菜单路由同步；真实站点混合首页仍需回归"
```

### Task 4: 导航结构事实与可信菜单容器

**Files:**
- Create: `services/api/app/application/web_navigation_recognition.py`
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Modify: `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- Modify: `services/api/app/agents/explorer/web_assessment_agent.py`
- Test: `services/api/tests/test_web_navigation_recognition.py`
- Test: `services/api/tests/test_web_desktop_navigation.py`
- Test: `services/api/tests/test_web_desktop_capture_policy.py`

**Interfaces:**
- Consumes: Capture snapshot 中的 `within_web_navigation`、`within_navigation`、`within_footer`、`is_advertisement`、`rect`、`role`、`tag`、文本和交互属性。
- Produces: `WebNavigationStructureFacts`、`assess_navigation_container(snapshot) -> WebNavigationAssessment`，并让 `extract_navigation_candidates()` 接收已验证容器范围。

- [ ] **Step 1: 写导航容器行为失败测试**

覆盖四种真实结果：顶部多分类导航通过；仅 Logo+登录失败；仅页脚失败；正文 `[data-route].card` 失败。测试断言候选输出，不断言内部选择器文本：

```python
def test_article_cards_outside_proven_navigation_are_not_menu_candidates() -> None:
    snapshot = {
        "elements": [
            {"tag": "a", "interactive": True, "visible": True,
             "within_web_navigation": False, "within_footer": False,
             "is_advertisement": False, "class_tokens": ["card"],
             "data_route": "/article/1", "text": "文章一"}
        ]
    }
    assessment = assess_navigation_container(snapshot)
    assert assessment.proven is False
    assert assessment.reason_codes == ("WEB_NAVIGATION_CONTAINER_UNPROVEN",)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_desktop_capture_policy.py -q`

Expected: FAIL，因为导航结构评估尚不存在，现有正文 card 仍可能进入候选。

- [ ] **Step 3: 实现导航事实与容器判定**

```python
@dataclass(frozen=True)
class WebNavigationStructureFacts:
    top_navigation_container_count: int
    visible_category_entry_count: int
    search_entry_count: int
    auth_entry_count: int
    logo_entry_count: int
    footer_only_entry_count: int
    dropdown_trigger_count: int
    expanded_submenu_count: int
    advertisement_entry_count: int
    navigation_obstructed: bool
```

`proven=True` 至少要求一个非页脚、非广告、无遮挡的导航容器和两个可见分类入口。搜索、登录和 Logo 只能增加容器置信度，不能单独满足分类入口数量。

- [ ] **Step 4: 收紧候选来源并保留审计原因**

从默认 `WEB_NAVIGATION_SOURCE_SELECTOR` 移除独立的 `[data-route][class*="card"]`。候选记录增加 `navigation_container_id` 和 `navigation_evidence`。登录、注册、搜索、支付、下载、广告、页脚及跨域候选可以随 Capture 保存，但 `actionability="blocked"`，并使用稳定原因码。

- [ ] **Step 5: 把导航事实和附件导航说明传给 assessor**

分类 payload 新增五类 `page_type_catalog`、`navigation_signals`、`common_site_observations` 和 `navigation_structure_facts`。约束明确：导航事实可支持正式 `home` 页型和列表页；根 URL 是 `home` 的强证据，但任何单个信号都不能越过确定性组合门槛。

- [ ] **Step 6: 运行导航与分类测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_desktop_capture_policy.py services/api/tests/test_web_page_classification.py -q`

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add services/api/app/application/web_navigation_recognition.py services/api/app/application/web_page_classification.py services/api/app/application/web_desktop_navigation.py services/api/app/infrastructure/browser/web_desktop_capture_policy.py services/api/app/agents/explorer/web_assessment_agent.py services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_desktop_capture_policy.py
git commit -m "[AI编程] fix(web-menu): 仅从可信导航容器生成菜单候选；无数据库迁移；无需前端菜单路由同步；移动端汉堡菜单视觉回归未覆盖"
```

### Task 5: 静态菜单降级为先验并支持未登记站点

**Files:**
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Modify: `services/api/app/application/web_page_classification_hints.py`
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_runtime_composition.py`

**Interfaces:**
- Consumes: 目标绝对 URL、可选静态 `WebSiteMenuCatalog` 和运行策略。
- Produces: `WebSiteMenuRegistry.resolve_site_context(target_url) -> WebSiteMenuContext`；未登记站点返回稳定运行时 site，而不是抛出 `WebSiteMenuCatalogNotFoundError`。

- [ ] **Step 1: 写未登记站点失败测试**

```python
def test_unknown_site_builds_runtime_home_context_without_catalog_entry() -> None:
    context = WebSiteMenuRegistry().resolve_site_context("https://example.net/path")

    assert context.source_kind == "runtime"
    assert context.site.origin == "https://example.net"
    assert len(context.site.entries) == 1
    assert context.site.entries[0].decision is WebMenuDecision.HOME_BASELINE
    assert context.site.entries[0].canonical_url == "https://example.net/"
```

并增加探索测试：给该运行时 site 注入两个同源导航候选，至少一个能够进入预算并访问；页面分类不获得静态 expected type。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_menu_exploration.py -q`

Expected: FAIL，当前 `resolve_site()` 对未登记 host 抛错。

- [ ] **Step 3: 实现稳定运行时站点上下文**

```python
@dataclass(frozen=True)
class WebSiteMenuContext:
    site: WebSiteMenuCatalog
    source_kind: Literal["catalog", "runtime"]
    content_sha256: str


def resolve_site_context(self, target_url: str) -> WebSiteMenuContext:
    try:
        site = self.resolve_site(target_url)
        return WebSiteMenuContext(site, "catalog", self.catalog_content_sha256)
    except WebSiteMenuCatalogNotFoundError:
        return _runtime_home_context(target_url)
```

运行时 `site_id` 使用规范 origin 的 SHA-256；首页条目只承担基线身份，不携带业务页型提示。`content_sha256` 对运行时上下文的规范 JSON 计算，保证恢复一致。

- [ ] **Step 4: 改造菜单探索和预算 bootstrap**

`WebMenuExplorer`、运行时装配和预算仓储统一消费 `WebSiteMenuContext`。静态条目优先排序，但运行时 DOM 候选无需匹配静态 entry。`resolve_web_page_classification_hint()` 对未命中静态目录返回 `hint=None`，不得把 `eligible=False` 解释为页面不可分类。

- [ ] **Step 5: 运行未知站点及既有站点回归并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_page_classification.py -q`

Expected: PASS，既有目录站点顺序与预算保持不变。

- [ ] **Step 6: 提交本任务**

```bash
git add services/api/app/domain/page/web_site_menu_registry.py services/api/app/application/web_page_classification_hints.py services/api/app/application/web_menu_exploration.py services/api/app/application/web_runtime_composition.py services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_composition.py
git commit -m "[AI编程] feat(web-menu): 支持未登记站点并将静态目录降级为先验；无数据库迁移；无需前端菜单路由同步；未覆盖全部历史站点重定向"
```

### Task 6: 稳定菜单父链与不限层级调度

**Files:**
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Modify: `services/api/app/application/web_runtime_discovery.py`
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_desktop_exploration.py`
- Test: `services/api/tests/test_web_desktop_navigation.py`
- Test: `services/api/tests/test_web_runtime_discovery.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_discovery_targets.py`

**Interfaces:**
- Consumes: 当前 Capture、当前探索父候选、同页 DOM `menu_path` 和稳定逻辑目标。
- Produces: `parent_menu_entry_id`、`menu_depth`、`exploration_path`、`logical_target_identity`，以及不含 Capture/epoch 临时值的 `discovery_entry_id()`。

- [ ] **Step 1: 写 25 层和跨 Capture 父链失败测试**

```python
def test_twenty_five_level_menu_path_is_preserved_without_depth_rejection() -> None:
    path = tuple(f"层级{i}" for i in range(1, 26))
    candidate = _candidate(menu_path=path, menu_depth=25, exploration_path=path)
    assert WebRuntimeMenuEntry.from_candidate(candidate).menu_depth == 25


def test_same_no_href_control_keeps_one_identity_across_capture_epochs() -> None:
    first = _candidate(url=None, source_epoch=1, source_capture_id="capture-1")
    second = replace(first, source_epoch=2, source_capture_id="capture-2")
    assert discovery_entry_id(first) == discovery_entry_id(second)
```

异步探索测试连续返回 12 层展开控件；断言不会出现“深度超限”，但只执行前 10 个动作，第 11 个形成 `WEB_DISCOVERY_BUDGET_EXHAUSTED`。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_discovery_targets.py -q`

Expected: FAIL，因为候选没有 lineage 字段，且无 URL 身份包含 Capture/临时 entry ID。

- [ ] **Step 3: 增加候选 lineage 合同**

`WebNavigationCandidate` 增加：

```python
parent_menu_entry_id: str | None = None
menu_depth: int = 1
exploration_path: tuple[str, ...] = ()
navigation_container_id: str | None = None
logical_target_identity: str = ""
```

校验 `menu_depth >= 1`、`exploration_path` 非空、末项等于规范化当前标签。为兼容现有构造器，省略 `exploration_path` 时在 `__post_init__` 中从 `menu_path` 派生，省略 `logical_target_identity` 时调用唯一的稳定身份构造器派生；生产提取链必须显式传递父链。没有父候选时深度为 1；展开后发现候选时使用 `parent.menu_depth + 1`，不设置最大值。

- [ ] **Step 4: 修正稳定身份和候选池合并**

有 URL 候选身份使用规范 URL、完整菜单路径和父逻辑身份。无 URL 候选身份使用导航容器身份、规范标签、target hint、完整探索路径、父逻辑身份和 source kind。Capture ID、epoch、临时 DOM entry ID 只保留审计，不参与主身份哈希。

`candidate_pool` 以 `logical_target_identity` 合并，重复 Capture 只刷新最新可执行来源，不新增逻辑候选。

- [ ] **Step 5: 正确传递实际 navigation depth**

`web_desktop_exploration.py` 不再为所有运行时候选写死 `navigation_depth=1`，而是传 `candidate.menu_depth`。该字段只用于审计和父链一致性，不作为动作拒绝上限。

- [ ] **Step 6: 运行不限层级和预算测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_discovery_targets.py -q`

Expected: PASS；25 层保留，跨 Capture 身份稳定，动作数不超过 10。

- [ ] **Step 7: 提交本任务**

```bash
git add services/api/app/application/web_desktop_navigation.py services/api/app/application/web_runtime_discovery.py services/api/app/application/web_menu_exploration.py services/api/app/application/web_desktop_exploration.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_discovery_targets.py
git commit -m "[AI编程] feat(web-menu): 增加稳定父链并取消菜单层级上限；无数据库迁移；无需前端菜单路由同步；持久化迁移在下一任务完成"
```

### Task 7: 菜单 lineage 持久化与恢复

**Files:**
- Create: `services/api/migrations/versions/20260911_0108_web_menu_lineage.py`
- Modify: `services/api/app/infrastructure/db/web_runtime_discovery_repository.py`
- Modify: `services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py`
- Modify: `services/api/app/application/web_exploration_budget_ledger.py`
- Modify: `services/api/app/application/web_evidence_bundle.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Test: `services/api/tests/test_web_menu_lineage_migration.py`
- Test: `services/api/tests/test_web_runtime_discovery_postgres.py`
- Test: `services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py`
- Test: `services/api/tests/test_web_evidence_bundle.py`

**Interfaces:**
- Consumes: Task 6 的 lineage 字段。
- Produces: `qc_web_menu_ledger_entry.parent_menu_entry_id`、`menu_depth`、`exploration_path`、`logical_target_identity`，以及读取摘要中的同名字段。

- [ ] **Step 1: 写迁移和仓储失败测试**

迁移测试必须实际升级测试数据库并断言：

```python
assert columns >= {
    "parent_menu_entry_id", "menu_depth", "exploration_path", "logical_target_identity"
}
```

仓储集成测试插入 25 层运行时候选，读取摘要后断言深度、路径和父 ID 原样恢复；尝试写入 `menu_depth=0` 或空 `exploration_path` 必须触发约束失败。

- [ ] **Step 2: 运行数据库测试并确认 RED**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_menu_lineage_migration.py services/api/tests/test_web_runtime_discovery_postgres.py -q`

Expected: FAIL，数据库不存在 lineage 列。

- [ ] **Step 3: 创建 Alembic 迁移**

Revision `20260911_0108`，`down_revision="20260911_0107"`。为 `qc_web_menu_ledger_entry` 增加：

```sql
parent_menu_entry_id varchar(128),
menu_depth integer,
exploration_path jsonb,
logical_target_identity varchar(128)
```

运行时候选要求四个字段完整，`menu_depth >= 1`，`exploration_path` 是非空数组，`logical_target_identity` 非空。Catalog 历史行允许字段为空。父 ID 不建立同表外键，因为父候选可能因预算关闭只存在于发现审计中，但触发器必须验证同 agent run/site 作用域内的父记录或显式 `lineage_unavailable` 历史诊断。

- [ ] **Step 4: 更新仓储写入、读取和恢复校验**

`record_discovery()` 在同一 INSERT 中保存 lineage；授权阶段不得修改这些不可变来源字段。`_build_summary()` 和 evidence bundle 输出同名字段。恢复时验证深度和路径，不得统一重置为 1。

- [ ] **Step 5: 运行迁移与持久化测试并确认 GREEN**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_menu_lineage_migration.py services/api/tests/test_web_runtime_discovery_postgres.py services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py services/api/tests/test_web_evidence_bundle.py -q`

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add services/api/migrations/versions/20260911_0108_web_menu_lineage.py services/api/app/infrastructure/db/web_runtime_discovery_repository.py services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py services/api/app/application/web_exploration_budget_ledger.py services/api/app/application/web_evidence_bundle.py services/api/app/application/web_runtime_composition.py services/api/tests/test_web_menu_lineage_migration.py services/api/tests/test_web_runtime_discovery_postgres.py services/api/tests/test_web_exploration_budget_ledger_postgres_integration.py services/api/tests/test_web_evidence_bundle.py
git commit -m "[AI编程] feat(web-audit): 持久化不限层级菜单父链；引入数据库迁移20260911_0108；无需前端菜单路由同步；降级要求无新lineage数据"
```

### Task 8: 文档、全链路回归与真实站点验收

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/specs/2026-09-11-web-catalog-navigation-and-unbounded-menu-depth-design.md` only if implementation reveals an approved contract correction
- Test: all Web unit/integration suites touched above

**Interfaces:**
- Consumes: Tasks 1–6 的完整行为。
- Produces: 可重复的专项回归规则、完整验证输出和真实站点探索证据。

- [ ] **Step 1: 更新专项回归规则**

记录以下长期规则：

```text
Web 首页必须作为正式 `home` 页面类型建模；`page_role=home` 只是探索上下文，不能代替分类结果。
Catalog 顶层特征必须强类型加载，禁止静默忽略。
页面类型不得由 URL、静态菜单或宽泛 DOM 子串定案。
菜单只从可信导航容器产生，正文卡片走详情候选链路。
菜单层级不限；总动作预算、deadline、同源与危险动作阻断必须保留。
无 URL 菜单身份不得以 Capture ID 或 epoch 为主键材料。
```

- [ ] **Step 2: 运行完整 Web 相关单元测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests -q -k "web"`

Expected: PASS，0 failures。

- [ ] **Step 3: 运行静态检查**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m ruff check services/api/app/domain/page services/api/app/application/web_page_recognition_rules.py services/api/app/application/web_navigation_recognition.py services/api/app/application/web_page_classification.py services/api/app/application/web_desktop_navigation.py services/api/app/application/web_menu_exploration.py services/api/app/infrastructure/browser/web_desktop_capture_policy.py services/api/app/infrastructure/db/web_runtime_discovery_repository.py services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_recognition_rules.py services/api/tests/test_web_navigation_recognition.py`

Expected: exit 0。

- [ ] **Step 4: 重启 Docker 且不 build**

Run: `docker compose restart`

Expected: 所有 Alioth 服务恢复 healthy；不得执行 `docker compose build`。

- [ ] **Step 5: 对 heiliao.com 与 hl365.com 执行真实探索验收**

使用 `admin / alioth123` 获取 token，分别创建启用同源运行时发现的 Web 任务。验收必须确认：

- 根 URL 记录为 `page_role=home`，并在结构证据满足时签发正式 `page_type=home`；若详情主体与根 URL 冲突，必须保存冲突证据并进入复核，不得伪装成列表页；
- 导航候选来自可信菜单容器；
- 静态目录未命中不会阻断探索；
- 菜单 lineage、深度、来源 Capture 和稳定逻辑身份进入结果；
- 同一无 URL 控件不因重复 Capture 生成多个逻辑候选；
- 实际菜单动作不超过 10；
- 登录、支付、下载、广告和跨域入口保持阻断；
- 页面和抽取广告落地页均保存 URL、DOM 与截图证据。

- [ ] **Step 6: 检查最终差异和用户既有修改**

Run: `git status --short --branch`

Run: `git diff --check`

确认未改动无关文件，且 `web_page_type_catalog.json` 中用户原有 v4 特征没有丢失。

- [ ] **Step 7: 提交文档与验收收口**

```bash
git add docs/专项能力与高频回归规则.md
git commit -m "[AI编程] doc(web): 固化Catalog与不限层级菜单回归规则；无数据库迁移；无需前端菜单路由同步；真实站点结果受外部页面变化影响"
```
