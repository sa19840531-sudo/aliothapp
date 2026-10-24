# H5 Logical Surface SPA / Flutter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变普通多页 H5 既有探索结果的前提下，为 DOM SPA、Flutter Web 与 Hybrid 页面增加可审计的渲染识别、逻辑页面身份、表面状态、统一目标和动作后验效能力。

**Architecture:** Capture 在 `ui_snapshot.v2` 内以可选 `logical_surface.v1` 采集统一证据，页面识别只绑定业务页型而不混合四类置信度，Frontier 仅在判定模式开启且渲染类型确认后使用逻辑身份。Runtime 继续复用来源 Capture、目标唯一性和命中测试，在动作后把逻辑转换写入现有 `state_diff`；Shadow 模式只持久化和报告，不改变候选、覆盖与规则匹配。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、Playwright、PostgreSQL JSONB、pytest、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-08-28-h5-logical-surface-spa-flutter-design.md`

## Global Constraints

- 普通 `multipage_dom` 的 Capture、候选 ID、候选顺序、`state_version`、访问去重、覆盖和规则匹配保持现状。
- 弱页型确定性修正继续使用 `min(model_confidence, 0.5)`；不得改成 `0.7`，也不得由 Flutter / SPA 渲染识别抬高。
- `page_type_confidence`、`render_profile_confidence`、`logical_transition_proof`、`target_trust` 必须分域存储和判断。
- SPA / Flutter 覆盖需要 `logical_transition_proof=trusted`、`detection_status=recognized`、`page_type_confidence >= 0.7` 和 after `identity_fingerprint` 绑定同时成立。
- 截图与 OCR 只补充文字和视觉状态，不得独立签发通用坐标点击。
- `activity`、`settings`、`payment` 只允许从可信 `profile` 逻辑页面的本地入口签发。
- 不新增 Worker、不新增数据库迁移、不新增公开 API 必填字段；所有新字段均为可选 JSON metadata。
- 三个开关默认关闭：`ALIOTH_H5_LOGICAL_SURFACE_ENABLED`、`ALIOTH_H5_LOGICAL_SURFACE_SHADOW`、`ALIOTH_H5_LOGICAL_TARGETS_ENABLED`。
- 所有新增注释使用中文，所有文件保持 UTF-8。
- 本地 Python 环境没有 pytest；测试统一在现有 `alioth-api` 容器内执行，不 build 镜像。
- 每批提交只暂存计划列出的文件，始终排除仓库根目录的用户文件 `demo.txt`。

## File Structure

- Create `services/api/app/domain/exploration/h5_logical_surface.py`: 逻辑页面值对象、稳定归一化、双指纹、页型绑定与转换判定。
- Create `services/api/app/application/h5_logical_surface_evidence.py`: 从 `ui_snapshot.v2` 提取 DOM、Flutter Semantics、Hybrid 和视觉补充证据并识别 Render Profile。
- Modify `services/api/app/core/config.py`: 声明三段发布开关及有效模式属性。
- Modify `services/api/app/infrastructure/browser/page_capture.py`: 同帧采集 document / history / DOM / Semantics 事实，写入 Shadow 逻辑状态和动作前后逻辑差异。
- Modify `services/api/app/infrastructure/browser/capture_runtime.py`: 把开关传入浏览器与 Resolver，并公开兼容的逻辑 `state_diff`。
- Modify `services/api/app/infrastructure/browser/semantic_target_resolver.py`: 对统一 DOM / Semantics 目标做来源状态验签和实时复验。
- Modify `services/api/app/agents/tools/page_detection_tools.py`: 消费 `logical_surface.v1`、绑定页型和身份、执行分域门禁。
- Modify `services/api/app/application/h5_page_type_assessment_adapter.py`: 对已确认 SPA / Flutter 应用联合覆盖门禁，普通 H5 维持原路径。
- Modify `services/api/app/application/h5_exploration_observation.py`: 在开启判定模式时把逻辑身份投影为 Frontier 状态和候选来源状态。
- Modify `services/api/app/application/h5_exploration_workflow.py`: 对 `uncertain` 转换执行有界重观察，对可信新逻辑页更新历史和覆盖。
- Modify `services/api/app/application/h5_runtime_composition.py`: 显式装配逻辑页面模式和目标模式，避免业务代码自行读取环境变量。
- Modify `services/api/app/agents/runtime/working_memory.py`: 按逻辑身份去重已访问页面，保留表面状态审计而不膨胀页面数。
- Modify `services/api/app/infrastructure/db/task_insight_repository.py`: 从现有 JSONB metadata 投影渲染类型、身份和转换审计字段。
- Modify `services/api/app/application/page_exploration_summary.py`: 汇总 Render Profile、逻辑页面、表面变化、降级与阻断统计。
- Modify `docs/专项能力与高频回归规则.md`: 固化 SPA / Flutter 分域置信度和兼容回归规则。

---

### Task 1: 逻辑页面领域模型、证据适配器与开关

**Files:**
- Create: `services/api/app/domain/exploration/h5_logical_surface.py`
- Create: `services/api/app/application/h5_logical_surface_evidence.py`
- Modify: `services/api/app/core/config.py:229-260,653-730`
- Create: `services/api/tests/test_h5_logical_surface.py`
- Modify: `services/api/tests/test_config_env_loading.py`

**Interfaces:**
- Produces: `build_h5_logical_surface(*, ui_snapshot: Mapping[str, Any], page_url: str, surface_session_id: str | None, document_token: str | None) -> dict[str, Any]`.
- Produces: `compare_h5_logical_surfaces(*, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any]`.
- Produces: `bind_h5_page_type(*, logical_surface: Mapping[str, Any] | None, page_type: str | None, detection_status: str, page_type_confidence: float, confidence_source: str) -> dict[str, Any] | None`.
- Produces: `WorkflowSettings.h5_logical_surface_effective` and `WorkflowSettings.h5_logical_targets_effective` read-only properties.

- [ ] **Step 1: 写 Render Profile、双指纹、转换和分域置信度失败测试**

```python
def test_flutter_dynamic_node_ids_do_not_change_identity() -> None:
    first = build_h5_logical_surface(
        ui_snapshot=_flutter_snapshot(node_ids=("flt-101", "flt-102")),
        page_url="https://example.test/",
        surface_session_id="session-1",
        document_token="document-1",
    )
    second = build_h5_logical_surface(
        ui_snapshot=_flutter_snapshot(node_ids=("flt-901", "flt-902")),
        page_url="https://example.test/",
        surface_session_id="session-1",
        document_token="document-1",
    )
    assert first["render_profile"]["kind"] == "flutter_web"
    assert first["identity_fingerprint"] == second["identity_fingerprint"]


def test_modal_changes_state_but_not_identity() -> None:
    before = build_h5_logical_surface(ui_snapshot=_spa_home(), page_url=URL,
                                      surface_session_id="s", document_token="d")
    after = build_h5_logical_surface(ui_snapshot=_spa_home(modal=True), page_url=URL,
                                     surface_session_id="s", document_token="d")
    transition = compare_h5_logical_surfaces(before=before, after=after)
    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"


def test_render_profile_confidence_never_raises_page_type_confidence() -> None:
    surface = build_h5_logical_surface(ui_snapshot=_flutter_snapshot(), page_url=URL,
                                       surface_session_id="s", document_token="d")
    bound = bind_h5_page_type(
        logical_surface=surface,
        page_type="profile",
        detection_status="recognized",
        page_type_confidence=0.42,
        confidence_source="model_self_report",
    )
    assert bound["confidence_domains"]["render_profile_confidence"] >= 0.9
    assert bound["confidence_domains"]["page_type_confidence"] == 0.42
    assert bound["coverage_allowed"] is False
```

- [ ] **Step 2: 运行测试并确认缺少模块/接口而失败**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_h5_logical_surface.py -q"`

Expected: FAIL，提示 `h5_logical_surface` 或上述函数尚不存在。

- [ ] **Step 3: 实现证据适配、稳定归一化、双指纹和转换契约**

```python
LOGICAL_SURFACE_PROTOCOL_VERSION = "logical_surface.v1"
CONFIRMED_RENDER_PROFILES = frozenset({"dom_spa", "flutter_web", "hybrid"})


def build_h5_logical_surface(*, ui_snapshot, page_url, surface_session_id, document_token):
    evidence = extract_h5_logical_surface_evidence(ui_snapshot=ui_snapshot)
    render_profile = detect_h5_render_profile(evidence=evidence)
    identity_signals = normalize_identity_signals(evidence=evidence)
    state_signals = normalize_state_signals(evidence=evidence)
    identity_fingerprint = stable_h5_surface_hash(identity_signals)
    return {
        "protocol_version": LOGICAL_SURFACE_PROTOCOL_VERSION,
        "render_profile": render_profile,
        "document_identity": stable_h5_surface_hash(
            [surface_session_id or "", document_token or "", origin_of(page_url)]
        ),
        "identity_fingerprint": identity_fingerprint,
        "state_fingerprint": stable_h5_surface_hash(
            [identity_fingerprint, state_signals]
        ),
        "identity_signals": identity_signals,
        "state_signals": state_signals,
        "evidence_sources": evidence["sources"],
        "evidence_quality": evidence["quality"],
        "degraded_reasons": evidence["degraded_reasons"],
    }
```

实现时必须剔除动态节点 ID、UUID、时间、金额、计数、用户正文和精确像素；列表数量分桶为 `0 / 1 / 2-5 / 6-20 / 20+`，几何坐标使用固定区间。`logical_page_changed` 必须同时具有身份锚点和独立结构佐证；只有 URL / hash / query 变化返回 `uncertain` 或 `unchanged`。

- [ ] **Step 4: 写三开关及不变量失败测试**

```python
def test_h5_logical_surface_flags_default_off(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", raising=False)
    monkeypatch.delenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", raising=False)
    monkeypatch.delenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", raising=False)
    settings = config_module._build_workflow_settings()
    assert settings.h5_logical_surface_enabled is False
    assert settings.h5_logical_surface_effective is False
    assert settings.h5_logical_targets_effective is False


def test_shadow_disables_logical_targets(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", "true")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", "true")
    settings = config_module._build_workflow_settings()
    assert settings.h5_logical_surface_effective is False
    assert settings.h5_logical_targets_effective is False
```

- [ ] **Step 5: 实现配置字段和有效属性**

```python
class WorkflowSettings(BaseModel):
    h5_logical_surface_enabled: bool = Field(default=False)
    h5_logical_surface_shadow: bool = Field(default=False)
    h5_logical_targets_enabled: bool = Field(default=False)

    @property
    def h5_logical_surface_effective(self) -> bool:
        return self.h5_logical_surface_enabled and not self.h5_logical_surface_shadow

    @property
    def h5_logical_targets_effective(self) -> bool:
        return self.h5_logical_surface_effective and self.h5_logical_targets_enabled
```

环境变量使用项目已有布尔解析函数，不接受模糊真值；非法值回退 `False`。

- [ ] **Step 6: 运行本批测试与配置回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_h5_logical_surface.py services/api/tests/test_config_env_loading.py services/api/tests/test_app_qc_settings.py -q"`

Expected: PASS，且默认 `WorkflowSettings()` 不改变现有测试。

- [ ] **Step 7: 提交批次 1**

```powershell
git add services/api/app/domain/exploration/h5_logical_surface.py services/api/app/application/h5_logical_surface_evidence.py services/api/app/core/config.py services/api/tests/test_h5_logical_surface.py services/api/tests/test_config_env_loading.py
git commit -m "[AI编程] feat(h5): 建立逻辑页面证据与置信度分域" -m "涉及模块：H5 逻辑页面领域模型、Render Profile 证据适配与发布开关。数据库迁移：无。前端菜单/路由：无需同步。未覆盖风险：尚未接入 Capture、Frontier 与 Runtime，仅提供默认关闭的纯函数和配置契约。"
```

### Task 2: Capture Shadow 采集与动作前后逻辑差异

**Files:**
- Modify: `services/api/app/infrastructure/browser/page_capture.py:1034-1060,1310-1500,6818-6890,7805-7890,8676-8735,9023-9055`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/tests/test_page_capture_tool.py`
- Modify: `services/api/tests/test_web_capture_runtime.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

**Interfaces:**
- Consumes: `build_h5_logical_surface`、`compare_h5_logical_surfaces`、三项 `WorkflowSettings` 属性。
- Produces: `ui_snapshot["logical_surface"]` 可选字段和 `action_trace[*]["state_diff"]["logical_surface_transition"]` 可选字段。
- Produces: `PlaywrightPageCaptureBrowser(..., logical_surface_enabled: bool = False, logical_surface_shadow: bool = False)`。

- [ ] **Step 1: 写默认关闭、Shadow 写审计和 Flutter 激活降级测试**

```python
async def test_ui_snapshot_omits_logical_surface_when_disabled() -> None:
    browser = PlaywrightPageCaptureBrowser(logical_surface_enabled=False)
    result = await browser.capture(_observe_request())
    assert "logical_surface" not in _artifact(result, "ui_snapshot")


async def test_shadow_capture_persists_logical_surface_without_changing_status() -> None:
    browser = PlaywrightPageCaptureBrowser(
        logical_surface_enabled=True,
        logical_surface_shadow=True,
    )
    result = await browser.capture(_observe_request(session_id="session-1"))
    surface = _artifact(result, "ui_snapshot")["logical_surface"]
    assert result.execution_status == "completed"
    assert surface["protocol_version"] == "logical_surface.v1"
    assert surface["shadow"] is True


async def test_semantics_activation_failure_keeps_capture_successful() -> None:
    result = await _capture_flutter_page(semantics_activation="failed")
    surface = _artifact(result, "ui_snapshot")["logical_surface"]
    assert result.execution_status == "completed"
    assert "flutter_semantics_unavailable" in surface["degraded_reasons"]
```

- [ ] **Step 2: 运行测试并确认逻辑字段缺失而失败**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_page_capture_tool.py -k 'logical_surface or semantics_activation_failure' -q"`

Expected: FAIL，断言 `logical_surface` 不存在或构造参数不受支持。

- [ ] **Step 3: 在同一帧 UI Snapshot 内追加 document 与逻辑证据**

```python
document_facts = {
    "time_origin_ms": await page.evaluate("() => performance.timeOrigin"),
    "history_length": await page.evaluate("() => history.length"),
}
snapshot["document"] = document_facts
if logical_surface_enabled:
    snapshot["logical_surface"] = {
        **build_h5_logical_surface(
            ui_snapshot=snapshot,
            page_url=str(page.url),
            surface_session_id=session_id,
            document_token=f"{id(page)}:{document_facts['time_origin_ms']}",
        ),
        "shadow": logical_surface_shadow,
    }
```

`document_token` 只参与哈希，不落盘。对 `_collect_ui_snapshot` 的现有调用全部提供默认参数，确保关闭开关时序列化结果逐字段不变；逻辑字段在 `_sanitize_and_bound_ui_snapshot` 限额内裁剪。

- [ ] **Step 4: 写动作前后转换嵌套字段失败测试**

```python
async def test_action_trace_contains_compatible_logical_transition() -> None:
    result = await _click_between_same_url_spa_surfaces()
    diff = result.action_trace[-1]["state_diff"]
    assert diff["transition_kind"] == "same_page_interaction"
    assert diff["logical_surface_transition"] == {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "logical_page_changed",
        "logical_transition_proof": "trusted",
        "before_identity_fingerprint": ANY_SHA256,
        "after_identity_fingerprint": ANY_SHA256,
        "corroborating_signals": ["active_navigation", "main_structure"],
    }
```

- [ ] **Step 5: 把逻辑转换写入现有 `state_diff` 并完成显式装配**

```python
state_diff = _ui_state_diff(before_snapshot=before_snapshot, after_snapshot=after_snapshot)
if logical_surface_enabled:
    state_diff["logical_surface_transition"] = compare_h5_logical_surfaces(
        before=before_snapshot.get("logical_surface"),
        after=after_snapshot.get("logical_surface"),
    )
```

`capture_runtime.py` 的 `_public_state_diff` 必须保留该嵌套对象但不得删除或改写原 `transition_kind`。`h5_runtime_composition.py` 从 `WorkflowSettings` 显式传入开关，不在领域函数中调用 `get_settings()`。

- [ ] **Step 6: 运行 Capture、持久化和 Runtime 回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_page_capture_tool.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_runtime_composition.py -q"`

Expected: PASS，默认关闭用例不出现新 metadata，Shadow 用例只新增审计字段。

- [ ] **Step 7: 提交批次 2**

```powershell
git add services/api/app/infrastructure/browser/page_capture.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/application/h5_runtime_composition.py services/api/tests/test_page_capture_tool.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_runtime_composition.py
git commit -m "[AI编程] feat(h5): 影子采集SPA与Flutter逻辑状态" -m "涉及模块：Playwright Capture、动作前后状态差异与 H5 Runtime 装配。数据库迁移：无，复用 ui_snapshot/action_trace JSON 证据。前端菜单/路由：无需同步。未覆盖风险：本批不改变 Frontier、覆盖与动作候选。"
```

### Task 3: 页面识别绑定与联合覆盖门禁

**Files:**
- Modify: `services/api/app/agents/tools/page_detection_tools.py:633-715,1275-1315,1902-2860,5314-5335`
- Modify: `services/api/app/application/h5_page_type_assessment_adapter.py:80-215`
- Modify: `services/api/tests/test_page_detection_tools.py`
- Modify: `services/api/tests/test_h5_page_type_assessment_adapter.py`

**Interfaces:**
- Consumes: `ui_snapshot.logical_surface` 和 `bind_h5_page_type`。
- Produces: `PageDetectionResult.logical_surface: dict[str, Any] | None`。
- Produces: `logical_surface.page_type_binding`，含 `page_type`、`identity_fingerprint`、`page_type_confidence`、`confidence_source`、`coverage_allowed`、`rule_matching_allowed` 和拒绝原因。

- [ ] **Step 1: 写置信度不串升与联合门禁失败测试**

```python
async def test_flutter_profile_keeps_weak_model_confidence_capped_at_half() -> None:
    result = await detect_page_for_agent(_flutter_bundle(model_confidence=0.96))
    assert result["page_type"] == "profile"
    assert result["confidence"] == 0.5
    assert result["confidence_source"] == "deterministic_refinement_cap"
    assert result["logical_surface"]["confidence_domains"] == {
        "page_type_confidence": 0.5,
        "render_profile_confidence": 0.96,
    }
    assert result["logical_surface"]["page_type_binding"]["coverage_allowed"] is False


def test_spa_assessment_requires_identity_binding_and_trusted_transition() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result=_recognized_spa_detection(
            confidence=0.91,
            identity_binding=True,
            transition_proof="uncertain",
        ),
    )
    assert result.page_type_assessment["status"] == "EVIDENCE_INSUFFICIENT"
    assert result.page_type_assessment["reason_code"] == "logical_transition_untrusted"
```

- [ ] **Step 2: 运行测试并确认新逻辑字段/门禁尚未实现**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_page_type_assessment_adapter.py -k 'logical_surface or flutter_profile or identity_binding or transition_untrusted' -q"`

Expected: FAIL，新字段不存在或 SPA 仍被旧门禁直接计入。

- [ ] **Step 3: 页面识别读取逻辑证据并绑定实际页型**

```python
logical_surface = _logical_surface_from_ui_snapshot(bundle.artifacts)
bound_surface = bind_h5_page_type(
    logical_surface=logical_surface,
    page_type=result_page_type,
    detection_status=detection_status,
    page_type_confidence=result_confidence,
    confidence_source=confidence_source,
)
return _result_payload(
    ...,
    confidence=result_confidence,
    confidence_source=confidence_source,
    logical_surface=bound_surface,
)
```

不得修改 `_page_type_confidence` 的弱修正 `0.5` 上限。只有现有完整强确定性页型结构分支继续使用 `confidence_source=deterministic_structure`；Render Profile 识别不得进入该分支。

- [ ] **Step 4: 在 Assessment 和规则匹配中应用联合门禁**

```python
if render_profile in CONFIRMED_RENDER_PROFILES:
    binding = logical_surface.get("page_type_binding", {})
    if binding.get("identity_fingerprint") != logical_surface.get("identity_fingerprint"):
        return "EVIDENCE_INSUFFICIENT", "logical_identity_unbound"
    if logical_surface.get("logical_transition_proof") != "trusted":
        return "EVIDENCE_INSUFFICIENT", "logical_transition_untrusted"
    if confidence < min_covered_confidence:
        return "EVIDENCE_INSUFFICIENT", "page_type_low_confidence"
```

初始入口没有 before→after 转换时，允许以同一 document 的两次稳定观察形成 `logical_transition_proof=trusted`；单次 Snapshot 不得伪造转换。普通 H5 缺少 `logical_surface` 时走原 `_assessment_status`。

- [ ] **Step 5: 运行页面识别与 Assessment 全模块回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_page_type_assessment_adapter.py services/api/tests/test_h5_page_type_assessment_repository.py -q"`

Expected: PASS，现有普通 H5 识别与 `0.7` 覆盖阈值用例保持不变。

- [ ] **Step 6: 提交批次 3**

```powershell
git add services/api/app/agents/tools/page_detection_tools.py services/api/app/application/h5_page_type_assessment_adapter.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_page_type_assessment_adapter.py
git commit -m "[AI编程] feat(h5): 绑定逻辑身份与页型联合门禁" -m "涉及模块：H5 页面识别、页型 Assessment、覆盖与规则准入。数据库迁移：无。前端菜单/路由：无需同步。未覆盖风险：Frontier 尚未消费逻辑身份，动作目标仍走现有 DOM/Flutter 路径。"
```

### Task 4: Frontier、Working Memory 与有界重观察

**Files:**
- Modify: `services/api/app/application/h5_exploration_observation.py:185-360`
- Modify: `services/api/app/application/h5_exploration_workflow.py:422-1225,1301-1510`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/agents/runtime/working_memory.py`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `services/api/tests/test_h5_exploration_workflow.py`
- Modify: `services/api/tests/test_working_memory.py`

**Interfaces:**
- Consumes: `PageDetectionResult.logical_surface.page_type_binding` 和 `logical_transition_proof`。
- Produces: `build_h5_frontier(..., logical_surface_effective: bool = False)`。
- Produces: observation metadata `logical_surface_identity_fingerprint`、`logical_surface_state_fingerprint`、`render_profile`、`logical_transition_proof`。
- Produces: candidate metadata `source_identity_fingerprint`、`source_state_fingerprint`。

- [ ] **Step 1: 写普通 H5 完全兼容与 SPA 同 URL 状态身份失败测试**

```python
def test_multipage_frontier_is_byte_compatible_when_feature_disabled() -> None:
    baseline = build_h5_frontier(detection_result=_home_detection(), observed_at=NOW,
                                 navigation_depth=0)
    disabled = build_h5_frontier(detection_result=_home_detection_with_surface(),
                                 observed_at=NOW, navigation_depth=0,
                                 logical_surface_effective=False)
    assert disabled.to_dict() == baseline.to_dict()


def test_confirmed_spa_same_url_uses_identity_fingerprint_in_state_version() -> None:
    home = build_h5_frontier(detection_result=_spa_detection("home", "identity-home"),
                             observed_at=NOW, navigation_depth=0,
                             logical_surface_effective=True)
    profile = build_h5_frontier(detection_result=_spa_detection("profile", "identity-profile"),
                                observed_at=NOW, navigation_depth=1,
                                logical_surface_effective=True)
    assert home.observation.location == profile.observation.location
    assert home.observation.state_version != profile.observation.state_version
    assert profile.candidates[0].metadata["source_identity_fingerprint"] == "identity-profile"
```

- [ ] **Step 2: 运行测试并确认状态版本仍只依赖路由而失败**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_h5_exploration_observation.py -k 'multipage_frontier_is_byte_compatible or confirmed_spa_same_url' -q"`

Expected: FAIL，SPA 的同 URL 状态未区分或候选缺少来源指纹。

- [ ] **Step 3: 仅为确认且有效的逻辑渲染类型投影身份**

```python
logical_identity = effective_logical_identity(
    detection_result=detection_result,
    enabled=logical_surface_effective,
)
if logical_identity is not None:
    state_material["logical_surface"] = {
        "identity_fingerprint": logical_identity,
        "state_fingerprint": logical_surface["state_fingerprint"],
    }
```

`effective_logical_identity` 必须要求 `render_profile.kind` 属于确认集合、身份非空且绑定页型一致；`multipage_dom`、`unknown`、`suspected` 和 Shadow 一律返回 `None`。候选 ID 仍由既有稳定哈希生成，但输入中追加来源逻辑身份和状态指纹。

- [ ] **Step 4: 写 uncertain 重观察与弹窗不增页失败测试**

```python
async def test_uncertain_logical_transition_reobserves_before_coverage() -> None:
    result = await _advance_spa_workflow(transition_proof="uncertain")
    assert result.work.frontier.candidates[0].kind == "observe"
    assert result.work.frontier.candidates[0].metadata["purpose"] == (
        "复验 SPA/Flutter 逻辑页面转换"
    )
    assert result.work.frontier.observation.metadata["logical_reobserve_count"] == 1


def test_modal_surface_state_does_not_increment_visited_logical_pages() -> None:
    memory = WorkingMemory()
    memory.record_observation(_logical_observation("identity-home", "state-base"))
    memory.record_observation(_logical_observation("identity-home", "state-modal"))
    assert memory.visited_logical_page_count == 1
    assert memory.observed_surface_state_count == 2
```

- [ ] **Step 5: 实现两次稳定观察和有界重观察**

```python
if logical_transition_proof in {"uncertain", "insufficient"}:
    if logical_reobserve_count < 2:
        return _logical_reobserve_frontier(
            prior=prior,
            count=logical_reobserve_count + 1,
        )
    return _logical_transition_blocked_frontier(
        prior=prior,
        reason_code="h5_logical_transition_unstable",
    )
```

两次 after 观察的 `identity_fingerprint` 和结构佐证稳定后才能升级为 `trusted`。Working Memory 以身份指纹计逻辑页数、以状态指纹保留动作审计；弹窗、loading、筛选、分页、轮播和滚动不得增加逻辑页数。

- [ ] **Step 6: 运行 Frontier、Workflow、Working Memory 回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_working_memory.py services/api/tests/test_h5_runtime_composition.py -q"`

Expected: PASS，普通 H5 的候选顺序和旧状态版本断言不变。

- [ ] **Step 7: 提交批次 4**

```powershell
git add services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_exploration_workflow.py services/api/app/application/h5_runtime_composition.py services/api/app/agents/runtime/working_memory.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_working_memory.py
git commit -m "[AI编程] feat(h5): 投影SPA与Flutter逻辑页面Frontier" -m "涉及模块：H5 Frontier、Workflow、Working Memory 和 Runtime 装配。数据库迁移：无。前端菜单/路由：无需同步。未覆盖风险：统一目标验签尚未启用，逻辑目标开关仍保持关闭。"
```

### Task 5: 统一动作目标、来源状态验签与操作后验效

**Files:**
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py:187-1520`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py:100-310`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/tests/test_web_capture_runtime.py`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `services/api/tests/test_h5_exploration_workflow.py`

**Interfaces:**
- Consumes: candidate metadata `source_identity_fingerprint`、`source_state_fingerprint` 和统一目标结构。
- Produces: `ResolvedInteractionTarget.target_source`、`target_signature`、`source_state_fingerprint`、`target_trust` 可选字段。
- Produces: 阻断码 `LOGICAL_SOURCE_STATE_STALE`、`LOGICAL_TARGET_NOT_UNIQUE`、`LOGICAL_TARGET_HIT_TEST_FAILED`。

- [ ] **Step 1: 写来源状态陈旧、Semantics 唯一解析和 OCR 禁止独立点击失败测试**

```python
async def test_logical_target_rejects_stale_source_state() -> None:
    with pytest.raises(SemanticTargetResolutionError) as exc:
        await resolver.resolve(_request(
            source_state_fingerprint="state-old",
            snapshot_state_fingerprint="state-current",
        ))
    assert exc.value.error_code == "LOGICAL_SOURCE_STATE_STALE"


async def test_flutter_semantics_target_requires_unique_role_label_and_hit_test() -> None:
    target = await resolver.resolve(_flutter_request(label="我的", role="button"))
    assert target.target_source == "flutter_semantics"
    assert target.target_trust == "trusted"
    assert target.target_signature.startswith("sha256:")


async def test_visual_only_target_cannot_authorize_generic_click() -> None:
    with pytest.raises(SemanticTargetResolutionError) as exc:
        await resolver.resolve(_visual_only_request(text="支付"))
    assert exc.value.error_code == "VISUAL_TARGET_REQUIRES_STRUCTURAL_ANCHOR"
```

- [ ] **Step 2: 运行测试并确认统一字段与阻断码尚不存在**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_web_capture_runtime.py -k 'logical_target or flutter_semantics_target or visual_only_target' -q"`

Expected: FAIL，Resolver 未校验来源状态或未返回统一目标字段。

- [ ] **Step 3: 为 DOM / Semantics / Hybrid 目标生成稳定签名并实时复验**

```python
target_signature = stable_h5_surface_hash({
    "source": target_source,
    "role": role,
    "label": normalized_label,
    "parent": normalized_parent_signature,
    "rect_bucket": quantize_rect(rect),
})
if requested_state_fingerprint != snapshot_state_fingerprint:
    raise SemanticTargetResolutionError(
        "来源逻辑页面状态已经变化",
        error_code="LOGICAL_SOURCE_STATE_STALE",
        retryable=True,
    )
```

解析必须同时检查可见、唯一、可点击、父级结构、当前矩形和 `elementFromPoint` / Semantics 命中。Hybrid 候选用签名去重；OCR 只能为已有结构目标补充 label，不能创建通用坐标目标。

- [ ] **Step 4: 写 Profile 局部拓扑和操作后验效失败测试**

```python
def test_profile_local_entries_bind_source_identity_and_state() -> None:
    frontier = build_h5_frontier(
        detection_result=_profile_with_local_entries("activity", "settings", "payment"),
        observed_at=NOW,
        navigation_depth=1,
        logical_surface_effective=True,
    )
    by_type = {item.metadata["expected_next_page_type"]: item for item in frontier.candidates}
    assert set(by_type) >= {"activity", "settings", "payment"}
    assert all(item.metadata["source_page_type"] == "profile" for item in by_type.values())
    assert all(item.metadata["source_state_fingerprint"] for item in by_type.values())


async def test_trusted_logical_page_change_is_required_for_navigation_success() -> None:
    result = await _execute_spa_navigation(transition_proof="uncertain")
    assert result.status.value == "blocked"
    assert result.error_code == "LOGICAL_TRANSITION_UNVERIFIED"
```

- [ ] **Step 5: 接入目标模式并验收 before→action→after**

```python
logical_transition = state_diff.get("logical_surface_transition", {})
if expects_logical_navigation and logical_transition.get(
    "logical_transition_proof"
) != "trusted":
    return _error_result(
        request,
        action_execution_id=action_execution_id,
        status=InteractionResultStatus.BLOCKED,
        error_code="LOGICAL_TRANSITION_UNVERIFIED",
        error_message="动作后没有形成可信的逻辑页面转换",
        retryable=True,
    )
```

弹窗、loading 和局部状态动作允许 `surface_state_changed` 成功，但不得增加覆盖。`expected_next_page_type` 只用于表达意图，after 的实际识别结果与身份绑定决定覆盖。Profile 局部入口同时要求来源实际页型、来源身份和目标结构可信。

- [ ] **Step 6: 运行 Runtime、Resolver 和 Workflow 回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_working_memory.py -q"`

Expected: PASS，普通 DOM selector 与既有 Flutter 专用动作仍通过原测试。

- [ ] **Step 7: 提交批次 5**

```powershell
git add services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_exploration_workflow.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py
git commit -m "[AI编程] feat(h5): 验签逻辑目标与动作状态转换" -m "涉及模块：H5 Semantic Target、浏览器交互、Profile 局部拓扑和动作后验效。数据库迁移：无。前端菜单/路由：无需同步。未覆盖风险：视觉 OCR 仅保留补证边界，未引入新的 OCR 服务。"
```

### Task 6: 任务报告、审计投影与专项回归规则

**Files:**
- Modify: `services/api/app/infrastructure/db/task_insight_repository.py:180-330,680-720`
- Modify: `services/api/app/application/task_insight.py:280-335,1360-1450`
- Modify: `services/api/app/application/page_exploration_summary.py`
- Modify: `services/api/tests/test_task_insight_repository.py`
- Modify: `services/api/tests/test_task_insight_service.py`
- Modify: `services/api/tests/test_report_insight.py`
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Consumes: `common_capture.metadata.page_detection.logical_surface` 和 action execution 的 `state_diff.logical_surface_transition`。
- Produces: `page_exploration.logical_surface` 报告块；不增加公开 API 必填字段。

- [ ] **Step 1: 写报告聚合失败测试**

```python
def test_page_exploration_reports_logical_surface_without_counting_modal_as_page() -> None:
    summary = build_page_exploration_summary(
        captures=[
            _logical_capture("identity-home", "state-base", "dom_spa"),
            _logical_capture("identity-home", "state-modal", "dom_spa"),
            _logical_capture("identity-profile", "state-profile", "flutter_web"),
        ],
        actions=[_logical_action("surface_state_changed", "trusted")],
    )
    logical = summary["logical_surface"]
    assert logical["unique_logical_page_count"] == 2
    assert logical["observed_surface_state_count"] == 3
    assert logical["render_profile_counts"] == {"dom_spa": 2, "flutter_web": 1}
    assert logical["transition_counts"]["surface_state_changed"] == 1
```

- [ ] **Step 2: 运行报告测试并确认逻辑汇总字段缺失**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_report_insight.py -k logical_surface -q"`

Expected: FAIL，SQL 投影或 `page_exploration.logical_surface` 不存在。

- [ ] **Step 3: 从 JSONB 投影非敏感逻辑字段并聚合**

```python
logical_surface = {
    "unique_logical_page_count": len(identity_fingerprints),
    "observed_surface_state_count": len(state_fingerprints),
    "render_profile_counts": dict(sorted(render_profile_counts.items())),
    "transition_counts": dict(sorted(transition_counts.items())),
    "semantics_activation": semantics_activation_summary,
    "degraded_reason_counts": dict(sorted(degraded_reason_counts.items())),
    "blocked_reason_counts": dict(sorted(blocked_reason_counts.items())),
}
```

SQL 只读取 JSON 路径，不增加列或索引；API 中没有新证据的历史任务不输出该块。有逻辑证据时列出每个身份对应的实际 `page_type`、置信度来源和冲突状态，不输出 DOM 原文、输入值或账号信息。

- [ ] **Step 4: 更新专项回归规则**

在 `docs/专项能力与高频回归规则.md` 增加“SPA / Flutter Web 逻辑页面”章节，固定以下检查项：

```text
1. 四个信任域分别断言，禁止数值串升。
2. 弱页型修正上限固定为 0.5；覆盖阈值保持 0.7。
3. 普通多页 H5 在开关关闭和 Shadow 下候选顺序不变。
4. 弹窗、loading、筛选、分页、轮播、滚动只改变表面状态。
5. OCR / 截图不得独立签发通用点击。
6. activity、settings、payment 只从 profile 本地入口签发。
7. 报告按 identity_fingerprint 计页面、按 state_fingerprint 计状态。
```

- [ ] **Step 5: 运行洞察、报告和协议兼容回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py services/api/tests/test_report_insight.py services/api/tests/test_page_capture_persistence.py -q"`

Expected: PASS，旧任务无逻辑字段时响应保持兼容。

- [ ] **Step 6: 提交批次 6**

```powershell
git add services/api/app/infrastructure/db/task_insight_repository.py services/api/app/application/task_insight.py services/api/app/application/page_exploration_summary.py services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_report_insight.py docs/专项能力与高频回归规则.md
git commit -m "[AI编程] feat(h5): 汇总逻辑页面探索审计报告" -m "涉及模块：任务洞察、报告聚合与 H5 专项回归文档。数据库迁移：无，仅读取现有 JSONB metadata。前端菜单/路由：无需同步，新增字段为可选报告块。未覆盖风险：历史任务没有逻辑证据时仅展示既有报告。"
```

### Task 7: 全量审查、Docker 验证与真实 Shadow 验收

**Files:**
- Modify if review finds defects: only the owning batch files.
- Create: `docs/reports/2026-08-28-h5-logical-surface-implementation-verification.md`

**Interfaces:**
- Consumes: 前六批提交。
- Produces: 可复核的静态审查、测试、开关回退、真实站点 Shadow 结果和剩余风险报告。

- [ ] **Step 1: 执行静态差异审查**

Run: `git diff --check HEAD~6..HEAD`

Expected: 无尾随空格、冲突标记或补丁格式错误。

Run: `git status --short --branch`

Expected: 仅允许 `demo.txt` 和本批验证报告未跟踪；不得出现意外业务文件。

逐批执行：

```powershell
git show --stat --oneline HEAD~5
git show --stat --oneline HEAD~4
git show --stat --oneline HEAD~3
git show --stat --oneline HEAD~2
git show --stat --oneline HEAD~1
git show --stat --oneline HEAD
```

审查重点：默认关闭是否完全兼容、Shadow 是否无决策副作用、四域置信度是否串升、旧 `transition_kind` 是否保留、动态文本是否进入身份指纹、视觉目标是否绕过结构验签。

- [ ] **Step 2: 运行 H5 受影响模块测试**

Run:

```powershell
docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests/test_h5_logical_surface.py services/api/tests/test_config_env_loading.py services/api/tests/test_page_capture_tool.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_page_type_assessment_adapter.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_working_memory.py services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_report_insight.py -q"
```

Expected: 全部 PASS；记录精确 passed / skipped / warning 数量。

- [ ] **Step 3: 运行 API 全量回归**

Run: `docker exec alioth-api sh -lc "cd /workspace && python -m pytest services/api/tests -q"`

Expected: 全部 PASS；若存在与本功能无关的既有失败，报告测试名、失败证据和与本批差异的关系，不能声称全量通过。

- [ ] **Step 4: 验证默认关闭和 Shadow 开关回退**

不 build 镜像。先以三开关全关闭重启现有 API / Worker，再以 `ENABLED=true`、`SHADOW=true`、`TARGETS=false` 重启；分别执行健康检查和一个普通 H5 观察任务。默认关闭结果不得出现 `logical_surface`，Shadow 结果必须写审计字段且候选 ID、候选顺序、覆盖集合与默认关闭一致。

检查命令：

```powershell
docker compose restart api worker
docker ps --format "table {{.Names}}`t{{.Status}}"
```

若 Compose 服务名不是 `api` / `worker`，先执行 `docker compose ps --services`，然后仅重启对应现有服务。

- [ ] **Step 5: 执行代表站点 Shadow 验收并记录阻断**

使用现有管理员登录接口获取 token，禁止把密码或 token 写入报告和 Git。通过 `/api/v2/tasks` 为现有 H5 列表逐个创建 `scan_mode=full` 任务，先保持逻辑能力为 Shadow；对普通 DOM、DOM SPA、Flutter Web、Hybrid 至少各形成一条可复核证据。报告记录：

```text
站点、任务 ID、Render Profile、渲染置信度、document_identity 稳定性、
identity_fingerprint 数量、state_fingerprint 数量、可信/不确定转换数、
实际覆盖页型、Flutter Semantics 激活状态、降级原因、阻断原因、剩余页面类型缺口。
```

真实远端站点出现登录态、DNS、TLS、防爬、空白页或预算阻断时，不绕过安全边界；保留证据并在修复前先完成报告。

- [ ] **Step 6: 写验证报告和修复审查缺陷**

验证报告必须包含：提交批次与回滚点、测试结果、普通 H5 对比、SPA / Flutter / Hybrid 证据、页面类型覆盖、Profile 局部入口、阻断问题、开关回退结果、未覆盖风险。审查发现的缺陷按所属模块写失败测试后修复，并追加独立 `fix` 提交，不改写前六批历史。

- [ ] **Step 7: 提交验证报告或审查修复**

```powershell
git add docs/reports/2026-08-28-h5-logical-surface-implementation-verification.md
git commit -m "[AI编程] test(h5): 记录SPA与Flutter逻辑探索验收" -m "涉及模块：H5 逻辑页面实现验证与真实站点 Shadow 报告。数据库迁移：无。前端菜单/路由：无需同步。未覆盖风险：报告内逐项记录远端站点、登录态和任务预算造成的剩余缺口。"
```

若存在修复，先提交对应 `[AI编程] fix(h5): ...` 批次，再单独提交验证报告，保证每个修复可独立回滚。

## Self-Review Result

- Spec coverage: Render Profile、DOM / Semantics / Hybrid 适配、双指纹、状态转换、统一目标、安全门禁、Profile 局部拓扑、Shadow 发布、报告和真实验证分别由 Task 1-7 覆盖。
- Compatibility: Task 2 与 Task 4 明确断言默认关闭和普通多页 H5 输出不变；Task 7 再做开关对比。
- Confidence consistency: `page_type_confidence` 只由现有页型识别产生；渲染、转换、目标信任均为独立字段，联合门禁位于 Task 3。
- Persistence: 只复用 `ui_snapshot`、`action_trace` 和 Capture metadata JSON，不需要数据库迁移。
- Rollback: 文档基线、领域模型、Capture、识别门禁、Frontier、目标验效、报告分别独立提交；审查修复不改写历史。
