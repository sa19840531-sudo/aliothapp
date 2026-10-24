# Page Type Catalog Single Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `page_type_catalog.json` 成为页面类型唯一数据源，所有分类、别名、路由、Agent 上下文与审计信息均由不可变 Registry 派生。

**Architecture:** 在领域层新增 `PageTypeRegistry`，负责加载和校验 Catalog、计算稳定摘要并提供只读查询接口。现有代码通过兼容加载函数逐步迁移；Agent 主指令注入紧凑 Catalog Context，页面识别继续注入完整目录；数据库导航别名仅作为 Catalog 投影与候选索引。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、PostgreSQL、Microsoft Agent Framework、pytest、ruff。

## Global Constraints

- 不调用任何远程 API；测试不得触发真实 LLM、浏览器网络或外部服务。
- 所有新增注释使用中文，所有文件保持 UTF-8。
- `page_type_catalog.json` 是唯一页面类型定义源。
- 不新增数据库迁移；Catalog 版本与 SHA256 写入现有 `qc_agent_run.input_context` 和页面识别 JSON。
- 不调整前端菜单或路由。
- 每个任务遵循先失败测试、再最小实现、再局部回归。

---

### Task 1: Catalog Schema 与不可变 Registry

**Files:**
- Create: `services/api/app/domain/page/registry.py`
- Modify: `services/api/app/domain/page/page_type_catalog.json`
- Modify: `services/api/app/domain/page/catalog.py`
- Modify: `services/api/app/domain/page/__init__.py`
- Create: `services/api/tests/test_page_type_registry.py`
- Modify: `services/api/tests/test_page_type_catalog.py`

**Interfaces:**
- Consumes: 现有 `PageTypeCatalog`、`PageTypeDefinition` 和 JSON 目录。
- Produces: `PageTypeRegistry.from_path(path)`、`get_default_page_type_registry()`、`all_codes`、`business_codes`、`lifecycle_codes`、`coverage_required_codes`、`rule_matchable_codes`、`transient_codes`、`content_sha256`、`resolve_route_prefix()`、`default_detail_type()`、`build_agent_context()`。

- [ ] **Step 1: 写 Registry 失败测试**

```python
def test_default_registry_derives_page_type_groups() -> None:
    registry = get_default_page_type_registry()
    assert registry.lifecycle_codes == ("line_loading", "startup_ad")
    assert "home" in registry.coverage_required_codes
    assert "line_loading" in registry.transient_codes
    assert "startup_ad" in registry.rule_matchable_codes


def test_registry_rejects_unknown_cross_reference(tmp_path: Path) -> None:
    payload = valid_catalog_payload()
    payload["page_types"][0]["default_detail_page_type"] = "missing_detail"
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(InvalidPageTypeCatalogError, match="missing_detail"):
        PageTypeRegistry.from_path(path)
```

- [ ] **Step 2: 运行 Registry 测试并确认失败**

Run: `pytest services/api/tests/test_page_type_registry.py -q`

Expected: FAIL，因为 `registry.py` 和新增接口尚不存在。

- [ ] **Step 3: 扩展 Catalog 数据模型**

为 `PageTypeDefinition` 增加：

```python
category: Literal["business", "lifecycle"]
coverage_required: bool
runtime_traits: tuple[str, ...]
route_prefixes: tuple[str, ...]
navigation_aliases: tuple[NavigationAliasDefinition, ...]
default_detail_page_type: str | None
```

为顶层 `PageTypeCatalog` 增加 `schema_version: str`，并更新 JSON 中全部 14 个页面类型。当前 12 个业务类型设为 `category="business"`、`coverage_required=true`；`line_loading`、`startup_ad` 设为生命周期类型；仅 `line_loading` 包含 `transient`。

- [ ] **Step 4: 实现不可变 Registry 与强校验**

实现默认 Registry 缓存、规范化 JSON SHA256、code 格式与唯一性校验、跨引用校验、路由冲突和正式别名冲突校验。`load_default_page_type_catalog()` 改为返回默认 Registry 的 `catalog`，保留兼容签名。

- [ ] **Step 5: 运行领域测试**

Run: `pytest services/api/tests/test_page_type_registry.py services/api/tests/test_page_type_catalog.py -q`

Expected: PASS。

- [ ] **Step 6: 运行静态检查**

Run: `ruff check services/api/app/domain/page services/api/tests/test_page_type_registry.py services/api/tests/test_page_type_catalog.py`

Expected: PASS。

### Task 2: 覆盖范围、生命周期和瞬态类型统一派生

**Files:**
- Modify: `services/api/app/agents/tools/exploration_tools.py`
- Modify: `services/api/app/application/page_exploration_summary.py`
- Modify: `services/api/app/agents/runtime/page_inspection_memory.py`
- Modify: `services/api/app/agents/runtime/business_middlewares/page_inspection_memory_middleware.py`
- Modify: `services/api/tests/test_exploration_tools.py`
- Modify: `services/api/tests/test_page_exploration_summary.py`
- Modify: `services/api/tests/test_page_type_assessment_runtime.py`
- Modify: `services/api/tests/test_page_coverage_contract_regressions.py`
- Modify: `services/api/tests/test_working_memory.py`

**Interfaces:**
- Consumes: `get_default_page_type_registry()` 及 Task 1 的分类属性。
- Produces: 不再声明完整 `_BUSINESS_PAGE_TYPES`、`_LIFECYCLE_PAGE_TYPES`、`TRANSIENT_PAGE_TYPES`。

- [ ] **Step 1: 修改测试只从 Registry 获取期望集合**

```python
registry = get_default_page_type_registry()
assert result["missing_page_types"] == list(registry.coverage_required_codes[-1:])
```

增加断言：`PageTypeAssessmentInput` 最大数量等于 `len(registry.coverage_required_codes)`，而不是固定 12。

- [ ] **Step 2: 运行覆盖和运行时测试并确认至少一个失败**

Run: `pytest services/api/tests/test_exploration_tools.py services/api/tests/test_page_exploration_summary.py services/api/tests/test_page_type_assessment_runtime.py services/api/tests/test_page_coverage_contract_regressions.py services/api/tests/test_working_memory.py -q`

Expected: FAIL，失败点指向固定集合或固定长度。

- [ ] **Step 3: 替换生产代码中的分类常量**

`exploration_tools.py` 使用 `coverage_required_codes` 校验完整评估并动态设置 `max_length`；`page_exploration_summary.py` 使用 Registry 分类；页面记忆和中间件通过 Registry 的 `transient_codes` 判断瞬态恢复。

- [ ] **Step 4: 运行局部回归**

Run: `pytest services/api/tests/test_exploration_tools.py services/api/tests/test_page_exploration_summary.py services/api/tests/test_page_type_assessment_runtime.py services/api/tests/test_page_coverage_contract_regressions.py services/api/tests/test_working_memory.py -q`

Expected: PASS。

### Task 3: 导航别名、路由和默认详情映射统一

**Files:**
- Modify: `services/api/app/domain/page/nav_alias.py`
- Modify: `services/api/app/application/page_exploration_profile.py`
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/infrastructure/db/page_nav_alias_repository.py`
- Modify: `scripts/seed_page_nav_alias.py`
- Modify: `services/api/tests/test_page_nav_alias.py`
- Modify: `services/api/tests/test_page_exploration_profile.py`
- Modify: `services/api/tests/test_page_detection_tools.py`

**Interfaces:**
- Consumes: `PageTypeRegistry.resolve_alias(label)`、`resolve_route_prefix(prefix)`、`default_detail_type(code)`。
- Produces: Catalog 驱动的正式别名种子；数据库返回值必须通过 Registry code 校验。

- [ ] **Step 1: 写 Catalog 驱动映射测试**

```python
def test_catalog_drives_alias_route_and_detail_mapping() -> None:
    registry = get_default_page_type_registry()
    assert registry.resolve_alias("短视频").canonical_code == "video_list"
    assert registry.resolve_route_prefix("movies") == "video_list"
    assert registry.default_detail_type("video_list") == "video_detail"
```

增加测试：数据库返回未知 `canonical_code` 时 Resolver 忽略该结果并回退正式 Catalog 别名。

- [ ] **Step 2: 运行映射测试并确认失败**

Run: `pytest services/api/tests/test_page_nav_alias.py services/api/tests/test_page_exploration_profile.py services/api/tests/test_page_detection_tools.py -q`

Expected: FAIL，因为映射仍由 Python 常量提供。

- [ ] **Step 3: 删除重复映射并接入 Registry**

删除 `DEFAULT_PAGE_NAV_ALIAS_TERMS`、`_BUSINESS_ROUTE_PAGE_TYPES` 以及 `_expected_page_type_from_label()` 的本地映射。保留页面结构识别算法，但路由和默认详情目标调用 Registry。`build_default_page_nav_alias_seed_rows()` 从 Registry 的正式别名生成。

- [ ] **Step 4: 限制数据库别名角色**

`PageNavAliasResolver` 对 Repository 返回结果执行 `registry.require_code()`；未知 code 不参与识别。种子脚本显式从默认 Registry 构建正式投影，数据库候选不覆盖 Catalog 定义。

- [ ] **Step 5: 运行映射回归**

Run: `pytest services/api/tests/test_page_nav_alias.py services/api/tests/test_page_exploration_profile.py services/api/tests/test_page_detection_tools.py -q`

Expected: PASS。

### Task 4: Agent Catalog Context 动态注入

**Files:**
- Modify: `services/api/app/agents/instructions.py`
- Modify: `services/api/app/agents/bootstrap.py`
- Modify: `services/api/app/agents/skills/qc-workflow/SKILL.md`
- Modify: `services/api/app/agents/skills/phase/page-detection/SKILL.md`
- Modify: `services/api/app/agents/skills/phase/page-exploration/SKILL.md`
- Modify: `services/api/tests/test_agent_instructions.py`
- Modify: `services/api/tests/test_page_type_assessment_runtime.py`
- Modify: `services/api/tests/test_skills_loader.py`
- Modify: `services/api/tests/test_evidence_contract_skills.py`

**Interfaces:**
- Consumes: `registry.build_agent_context(scope="main")`。
- Produces: `build_main_agent_instructions(registry: PageTypeRegistry | None = None) -> str`；保留 `MAIN_AGENT_INSTRUCTIONS` 作为默认构建结果兼容旧调用。

- [ ] **Step 1: 写动态指令契约测试**

```python
def test_main_agent_instructions_include_catalog_context() -> None:
    instructions = build_main_agent_instructions()
    registry = get_default_page_type_registry()
    assert registry.content_sha256 in instructions
    assert json.dumps(
        list(registry.coverage_required_codes), ensure_ascii=False
    ) in instructions
```

增加静态检查：三个 Skill 不再包含完整 12 类型串。

- [ ] **Step 2: 运行 Agent 契约测试并确认失败**

Run: `pytest services/api/tests/test_agent_instructions.py services/api/tests/test_page_type_assessment_runtime.py services/api/tests/test_skills_loader.py services/api/tests/test_evidence_contract_skills.py -q`

Expected: FAIL，因为主指令仍维护固定清单且没有 Catalog SHA256。

- [ ] **Step 3: 实现动态 Main Agent Context**

将静态正文与 `build_agent_context()` 序列化结果拼接。`bootstrap.py` 使用构建函数；Skill 改为引用 `coverage_required_page_types`、`lifecycle_page_types` 和 `rule_matchable_page_types`，不再维护完整清单。

- [ ] **Step 4: 运行 Agent 契约回归**

Run: `pytest services/api/tests/test_agent_instructions.py services/api/tests/test_page_type_assessment_runtime.py services/api/tests/test_skills_loader.py services/api/tests/test_evidence_contract_skills.py -q`

Expected: PASS。

### Task 5: Catalog 版本与 SHA256 审计

**Files:**
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/infrastructure/db/page_capture_repository.py`
- Modify: `services/api/app/infrastructure/queue/state_store.py`
- Modify: `services/api/app/api/v1/workstation/page_config.py`
- Modify: `services/api/tests/test_page_detection_tools.py`
- Modify: `services/api/tests/test_page_capture_persistence.py`
- Modify: `services/api/tests/test_agent_run_state_store.py`
- Modify: `services/api/tests/test_workstation_page_config_api.py`

**Interfaces:**
- Consumes: `registry.content_sha256` 和 `registry.catalog.version`。
- Produces: 页面识别结果字段 `catalog_sha256`；`qc_agent_run.input_context.page_type_catalog`；工作台配置响应中的 `catalog_sha256`。

- [ ] **Step 1: 写审计字段失败测试**

```python
assert result["catalog_sha256"] == get_default_page_type_registry().content_sha256
assert input_context["page_type_catalog"] == {
    "version": registry.catalog.version,
    "sha256": registry.content_sha256,
}
```

- [ ] **Step 2: 运行审计相关测试并确认失败**

Run: `pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_agent_run_state_store.py services/api/tests/test_workstation_page_config_api.py -q`

Expected: FAIL，因为 SHA256 尚未输出或持久化。

- [ ] **Step 3: 写入现有 JSON 审计结构**

扩展页面识别 Pydantic 模型和持久化 detection JSON。`state_store._input_context()` 加入 Catalog 版本和摘要；不新增数据库列。工作台页面配置响应增加摘要，便于前后端确认当前目录。

- [ ] **Step 4: 运行审计回归**

Run: `pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_agent_run_state_store.py services/api/tests/test_workstation_page_config_api.py -q`

Expected: PASS。

### Task 6: 调用方统一、规则文档与完整验证

**Files:**
- Modify: `services/api/app/worker.py`
- Modify: `services/api/app/agents/tools/rule_tools.py`
- Modify: `services/api/app/api/v1/rules.py`
- Modify: `services/api/app/api/v1/tasks.py`
- Modify: `services/api/app/api/v1/workstation/inspection_scope.py`
- Modify: `services/api/app/application/task_progress.py`
- Modify: `services/api/app/application/task_query.py`
- Modify: `services/api/app/infrastructure/db/task_repository.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: 对应既有测试文件

**Interfaces:**
- Consumes: `get_default_page_type_registry()`。
- Produces: 生产代码不再直接构造第二套页面类型集合；兼容加载函数仅供过渡和旧测试。

- [ ] **Step 1: 将直接调用方迁移到 Registry**

替换直接 `load_default_page_type_catalog()` 的生产代码调用；严格 code 校验使用 `registry.require_code()`，遍历使用 Registry 派生属性。

- [ ] **Step 2: 增加静态防回归测试**

```python
for path in production_paths:
    text = path.read_text(encoding="utf-8")
    assert "_BUSINESS_PAGE_TYPES =" not in text
    assert "_LIFECYCLE_PAGE_TYPES =" not in text
    assert "TRANSIENT_PAGE_TYPES =" not in text
```

测试同时确认 Skill 与 `instructions.py` 不包含旧完整清单。

- [ ] **Step 3: 更新专项回归规则**

记录：

- 页面类型 canonical code 只能在 Catalog 定义；
- 完整分类集合必须由 Registry 派生；
- Skill 只能引用运行时 Catalog Context；
- 数据库导航别名只作为候选和正式定义投影；
- 新增页面类型必须通过 Registry、Agent Context、探索收口和审计契约测试。

- [ ] **Step 4: 运行全部页面类型相关测试**

Run:

```text
pytest services/api/tests/test_page_type_registry.py services/api/tests/test_page_type_catalog.py services/api/tests/test_page_detection_tools.py services/api/tests/test_page_nav_alias.py services/api/tests/test_page_exploration_profile.py services/api/tests/test_exploration_tools.py services/api/tests/test_page_exploration_summary.py services/api/tests/test_page_type_assessment_runtime.py services/api/tests/test_page_coverage_contract_regressions.py services/api/tests/test_agent_instructions.py services/api/tests/test_skills_loader.py services/api/tests/test_working_memory.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_agent_run_state_store.py services/api/tests/test_workstation_page_config_api.py -q
```

Expected: PASS。

- [ ] **Step 5: 运行 API 全量测试**

Run: `pytest services/api/tests -q`

Expected: PASS；不得出现真实远程调用。

- [ ] **Step 6: 运行代码质量检查**

Run: `ruff check services/api/app services/api/tests scripts/seed_page_nav_alias.py`

Expected: PASS。

- [ ] **Step 7: 检查差异**

Run: `git diff --check`

Expected: 无错误。

- [ ] **Step 8: 本地代码审核**

逐文件检查领域边界、是否残留重复完整清单、Catalog 跨引用、数据库别名优先级、Agent Prompt 注入、错误处理、UTF-8 中文注释和测试覆盖。审核发现必须先修复并重新运行受影响测试，再输出最终结果。
