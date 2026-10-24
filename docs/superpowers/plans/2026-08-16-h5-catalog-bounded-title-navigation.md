# H5 Catalog-Bounded Title Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 H5 V2 通过帖子标题进入详情、跳过目录外页面，并在模型错误提前终止时继续执行唯一已验签安全候选。

**Architecture:** PageTypeRegistry 继续作为页面类型唯一权威；页面识别负责丢弃目录外目标并产生可审计 skip，任务 Frontier 负责把对应 selected entry 置为跳过后恢复父页。列表详情能力通过精确帖子标题 selector 贯通 Detection、UI Snapshot 与 Resolver；Explorer 容错只在 H5 包装器内消费类型化提前终止异常，并继续使用现有候选参数绑定门禁。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL、Playwright、Microsoft Agent Framework、pytest、Ruff、Docker Compose。

## Global Constraints

- 当前目录唯一来源固定为 `services/api/app/domain/page/page_type_catalog.json`，禁止复制页面类型集合。
- 当前目录没有 `ranking`；实跑报告必须标记为 `uncatalogued/skipped`，不得私自新增或伪造覆盖。
- 帖子详情规范类型为 `content_detail`；只点击 `.dynamicData-box .title`，禁止退回整卡或视觉坐标。
- `content_detail` 沿用业务页弹窗上限 20 轮；无可信关闭能力且遮挡 `z-index <= 500` 时直接同会话 `back`。
- H5 视口固定 `390×844`，总时长固定 3600 秒，单动作没有独立固定超时。
- 唯一候选兜底只允许未阻断的 low/medium 候选；多个候选、high/critical、身份不一致继续失败关闭。
- Docker 只能 restart，禁止 build；API/Worker 重启前后镜像 ID 必须一致。
- 保留未跟踪 `.alioth-h5-v2.override.yml` 与 `.alioth-llm-bridge.py`，禁止暂存或输出 Key。
- 修复专项规则必须同步 `docs/专项能力与高频回归规则.md`。

---

## File Responsibility Map

- `services/api/app/application/h5_list_detail_navigation.py`：列表类型、规范详情类型、受控 selector 与 Snapshot token 的单一合同。
- `services/api/app/agents/tools/page_detection_tools.py`：DOM 签发标题能力、目录引用清洗、未编目识别结果。
- `services/api/app/infrastructure/browser/page_capture.py`：只采集精确帖子标题节点，不开放通用 `.title`。
- `services/api/app/infrastructure/browser/semantic_target_resolver.py`：从当前 UI Snapshot 重新解析受控标题能力。
- `services/api/app/domain/exploration/h5_frontier.py`：新增向后兼容的 `skipped` entry 终态与转换方法。
- `services/api/app/application/h5_exploration_observation.py`：把目录 disposition 投影到 SurfaceObservation 元数据。
- `services/api/app/application/h5_exploration_workflow.py`：未编目落地页把 selected entry 标记 skipped，再选择父页或其他 sibling。
- `services/api/app/agents/explorer/maf_agent.py`：用专门异常区分“仍有候选却提前终止”。
- `services/api/app/application/h5_runtime_composition.py`：H5 唯一安全候选 ActionIntent 的确定性兜底。

---

### Task 1: Sign and Resolve the First Post Title

**Files:**
- Modify: `services/api/app/application/h5_list_detail_navigation.py`
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Test: `services/api/tests/test_page_detection_tools.py`
- Test: `services/api/tests/test_page_capture_tool.py`
- Test: `services/api/tests/test_web_capture_runtime.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`

**Interfaces:**
- Consumes: `h5_list_detail_selector_allowed(source_page_type, selector)`、`h5_list_detail_snapshot_element_matches(element, source_page_type, selector)`。
- Produces: `H5_POST_TITLE_SELECTOR = ".dynamicData-box:visible .title:visible"`；`post_list → content_detail` 的精确结构能力。

- [ ] **Step 1: Write failing detector and contract tests**

在 `test_page_detection_tools.py` 将真实结构回归改为：

```python
def test_post_list_supports_real_dynamic_data_title_structure() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="dynamicData-box"><div class="dynamicData-content">'
            '<div class="title">帖子标题</div></div></div>'
        ],
    )
    assert actions[0].target == ".dynamicData-box:visible .title:visible"
    assert actions[0].expected_next_page_type == "content_detail"
```

在 `test_web_capture_runtime.py` 增加 selector/token 对齐断言：标题 selector 只能匹配 `class_tokens=["title"]`，必须拒绝 `dynamicData-content`。

- [ ] **Step 2: Run RED tests**

Run:

```powershell
docker exec alioth-api python -m pytest services/api/tests/test_page_detection_tools.py -k 'dynamic_data_title' -q
docker exec alioth-api python -m pytest services/api/tests/test_web_capture_runtime.py -k 'post_title' -q
```

Expected: 第一项仍返回 `.dynamicData-box:visible`；第二项错误接受父级 token 或没有标题 selector。

- [ ] **Step 3: Implement selector-specific capability matching**

在 `h5_list_detail_navigation.py` 增加：

```python
H5_POST_TITLE_SELECTOR = ".dynamicData-box:visible .title:visible"

_SNAPSHOT_TOKENS_BY_SELECTOR = {
    H5_POST_TITLE_SELECTOR: frozenset({"title"}),
}
```

`h5_list_detail_snapshot_element_matches` 先按 selector 精确查 token；没有 selector 专项时才使用现有页面类型 token 集合。将标题 selector 加入 `post_list` allowlist，并在 `page_detection_tools.py` 的 `post_list` 结构优先级中放在 `.dynamicData-box:visible` 之前。

- [ ] **Step 4: Preserve the exact title node in UI Snapshot**

在 `page_capture.py` 的 `regularCandidates` 选择器中只加入：

```javascript
".dynamicData-box .dynamicData-content .title,"
```

不得加入 `[class~='title']`。在 `test_page_capture_tool.py` 使用真实 Chromium HTML 断言 Snapshot 中存在 `title` 节点，并且其他容器的普通 `.title` 不因本能力被额外纳入。

- [ ] **Step 5: Make Resolver require the exact signed leaf**

保持 `_resolve_list_detail_navigation` 的当前 Snapshot、可见、中心命中与排序门禁。通过新的 selector-specific token 合同使 `min(matches)` 只能返回首个帖子标题；增加父内容、第二条标题、离屏标题和 disabled 标题回归。

- [ ] **Step 6: Run Task 1 GREEN tests**

Run:

```powershell
docker exec alioth-api python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_page_capture_tool.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_runtime_composition.py -k 'list_detail or post_title or dynamic_data' -q
docker exec alioth-api python -m ruff check services/api/app/application/h5_list_detail_navigation.py services/api/app/agents/tools/page_detection_tools.py services/api/app/infrastructure/browser/page_capture.py services/api/app/infrastructure/browser/semantic_target_resolver.py
```

Expected: 全部通过。

- [ ] **Step 7: Commit Task 1**

```powershell
git add services/api/app/application/h5_list_detail_navigation.py services/api/app/agents/tools/page_detection_tools.py services/api/app/infrastructure/browser/page_capture.py services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/tests/test_page_detection_tools.py services/api/tests/test_page_capture_tool.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_runtime_composition.py
git commit -m "[AI编程] fix: 通过H5帖子标题进入详情" -m "涉及模块：H5列表详情与浏览器Snapshot。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实站点帖子详情弹窗与返回待无build实跑。"
```

---

### Task 2: Skip Uncatalogued Targets and Landing Pages

**Files:**
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/domain/exploration/h5_frontier.py`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/app/application/h5_page_type_assessment_adapter.py`
- Test: `services/api/tests/test_page_detection_tools.py`
- Test: `services/api/tests/test_h5_task_frontier.py`
- Test: `services/api/tests/test_h5_exploration_observation.py`
- Test: `services/api/tests/test_h5_exploration_workflow.py`
- Test: `services/api/tests/test_h5_page_type_assessment_adapter.py`

**Interfaces:**
- Consumes: `get_default_page_type_registry().catalog.codes` 与现有 deterministic route/structure fallback。
- Produces: `H5FrontierEntryStatus.SKIPPED`、`H5TaskFrontierSnapshot.mark_skipped(entry_id, reason, finished_at)`、Observation 元数据 `catalog_disposition=skipped`。

- [ ] **Step 1: Write failing optional-reference tests**

在 `test_page_detection_tools.py` 固定以下合同：

```python
def test_unknown_expected_page_type_drops_entire_candidate() -> None:
    output = _recognized_output("home").model_copy(
        update={
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="未知入口",
                    risk_level="low",
                    expected_next_page_type="ranking",
                )
            ]
        }
    )
    sanitized = page_detection_tools._sanitize_optional_catalog_references(
        output,
        load_default_page_type_catalog(),
    )
    assert sanitized.candidate_actions == []
    assert "uncatalogued_expected_page_type_dropped" in sanitized.risk_flags
```

另加目录内 `profile` 候选保留、原本没有 expected type 的可信 popup 保留。

- [ ] **Step 2: Write failing current-page skip test**

构造模型返回 `page_type="ranking"`、确定性结构与路由都不能映射的页面，断言：

```python
assert result["detection_status"] == "unrecognized"
assert result["page_type"] is None
assert result["candidate_actions"] == []
assert result["catalog_disposition"] == "skipped"
assert result["catalog_skip_reason"] == "uncatalogued_page_type"
assert result.get("error_code") is None
```

Expected RED: 当前实现返回 `INVALID_PAGE_TYPE`。

- [ ] **Step 3: Implement catalog sanitization and skip result**

在 `_sanitize_optional_catalog_references` 中，原始 `expected_next_page_type` 非空而规范化结果为空时 `continue` 丢弃整条 action，并追加 `uncatalogued_expected_page_type_dropped`。主页面类型只有在 deterministic structure/route/alias 均无法映射时返回正常 unrecognized skip 结果；目录加载或目录自身非法仍抛部署错误。

- [ ] **Step 4: Add backward-compatible skipped Frontier status**

在 `h5_frontier.py` 增加：

```python
class H5FrontierEntryStatus(StrEnum):
    ...
    SKIPPED = "skipped"

def mark_skipped(
    self,
    entry_id: str,
    *,
    reason: str,
    finished_at: datetime,
) -> H5TaskFrontierSnapshot:
    return self._finish_entry(
        entry_id,
        status=H5FrontierEntryStatus.SKIPPED,
        reason=reason,
        finished_at=finished_at,
    )
```

`SKIPPED` 必须要求 `finished_at`，但不得计入 executed coverage、blocked_count 或 failed_count；旧 payload 不包含该状态时仍按原 schema 解析。

- [ ] **Step 5: Project skip metadata and continue siblings**

`build_h5_frontier` 将以下字段从 detection 复制到 Observation：

```python
"catalog_disposition": _optional_text(detection_result.get("catalog_disposition")),
"catalog_skip_reason": _optional_text(detection_result.get("catalog_skip_reason")),
```

`H5ExplorationWorkflow.continue_after_action` 在 landing frontier 为 catalog skipped 时调用：

```python
task_frontier = task_frontier.mark_skipped(
    selected_entry_id,
    reason="uncatalogued_page_type",
    finished_at=observed_at,
)
```

随后仍执行 merge 与 `_select_next_task_entry(..., **_history_parent_selection_preference(frontier))`，使保存的父页 sibling 优先触发现有 back 恢复；没有父页时选择其他 pending sibling。

同时修改 `h5_page_type_assessment_adapter.py`：当且仅当 `page_type is None`、`catalog_disposition == "skipped"` 且 `catalog_skip_reason == "uncatalogued_page_type"` 时，返回 `execution_status="skipped"` 且不生成 assessment；其他缺失 `page_type` 的结果继续以 `PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED` 失败关闭。新增精确正例和缺少/伪造元数据的负例。

- [ ] **Step 6: Run Task 2 GREEN tests**

Run:

```powershell
docker exec alioth-api python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_page_type_assessment_adapter.py -k 'catalog or uncatalog or skipped or history_parent' -q
docker exec alioth-api python -m ruff check services/api/app/agents/tools/page_detection_tools.py services/api/app/domain/exploration/h5_frontier.py services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_exploration_workflow.py
```

Expected: 未编目页不失败、不计覆盖，selected entry 为 skipped，父页/其他 sibling 可继续。

- [ ] **Step 7: Commit Task 2**

```powershell
git add services/api/app/agents/tools/page_detection_tools.py services/api/app/domain/exploration/h5_frontier.py services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_exploration_workflow.py services/api/app/application/h5_page_type_assessment_adapter.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_page_type_assessment_adapter.py
git commit -m "[AI编程] fix: 跳过H5未编目页面" -m "涉及模块：页面识别、H5任务Frontier与来源恢复。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实重定向到未编目页面待无build实跑。"
```

---

### Task 3: Recover from a Premature Model Terminal with One Signed Candidate

**Files:**
- Modify: `services/api/app/agents/explorer/maf_agent.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Test: `services/api/tests/test_maf_explorer_agent.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`

**Interfaces:**
- Consumes: selected H5 Task Frontier entry、`_h5_candidate_parameters`、`h5_candidate_purpose`、`CaptureProfilePolicy.for_surface`。
- Produces: `ExplorerPrematureTerminalError`；`_platform_selected_h5_action_intent(work) -> ActionIntent`。

- [ ] **Step 1: Write the typed-error RED test**

在 `test_maf_explorer_agent.py` 让 Fake Agent 在一个 eligible Web candidate 存在时返回 bounded，断言：

```python
with pytest.raises(ExplorerPrematureTerminalError):
    await agent.decide_next(work)
```

Expected RED: 当前只抛基类 `ExplorerAgentOutputError`。

- [ ] **Step 2: Write H5 fallback RED tests**

在 `test_h5_runtime_composition.py` 构造唯一 selected H5 candidate，Delegate 抛 `ExplorerPrematureTerminalError`，断言返回 ActionIntent：

```python
assert decision.parameters["candidate_id"] == candidate.candidate_id
assert decision.parameters["viewport_width"] == 390
assert decision.parameters["viewport_height"] == 844
assert decision.intent_id.startswith("h5-platform-fallback:")
```

参数化负例：两个 candidates、candidate blocked、risk high、selected entry ID 不一致，均必须继续抛错且不产生 ActionIntent。

- [ ] **Step 3: Introduce the typed shared exception**

在 `maf_agent.py` 增加：

```python
class ExplorerPrematureTerminalError(ExplorerAgentOutputError):
    """模型在仍有可执行候选时错误声明非完成终态。"""
```

`_validated_model_terminal` 仅把对应分支改为抛该子类；其他结构错误、无效 candidate ID、模型调用错误保持原类型和行为。

- [ ] **Step 4: Build the H5-only deterministic ActionIntent**

在 `h5_runtime_composition.py` 增加 `_platform_selected_h5_action_intent`。它必须验证唯一 selected entry 与唯一 candidate 身份一致、候选不 blocked、风险是 low/medium，然后使用：

```python
parameters = _h5_candidate_parameters(work=work, candidate=candidate)
digest = stable_protocol_hash(
    {
        "work_id": work.work_id,
        "source_capture_id": work.source_capture_id,
        "candidate_id": candidate.candidate_id,
        "decision_source": "platform_selected_candidate_fallback",
    }
)
return ActionIntent(
    task_id=work.task_id,
    session_id=work.session_id,
    intent_id=f"h5-platform-fallback:{digest}",
    idempotency_key=f"h5-platform-fallback:{digest}",
    surface_kind=work.surface_kind,
    source_capture_id=work.source_capture_id,
    action_type=candidate.action_type,
    semantic_target=candidate.semantic_target,
    parameters=parameters,
    purpose=h5_candidate_purpose(candidate),
    risk_level=candidate.risk_level,
    capture_profile=CaptureProfilePolicy.for_surface(
        work.surface_kind,
        candidate.action_type,
    ),
    deadline=work.deadline,
)
```

`H5CompletionAwareExplorer.decide_next` 只捕获 `ExplorerPrematureTerminalError`，调用该函数，然后仍执行 `_validate_h5_selected_candidate_binding`。不得捕获通用 `ExplorerAgentOutputError`。

- [ ] **Step 5: Run Task 3 GREEN tests**

Run:

```powershell
docker exec alioth-api python -m pytest services/api/tests/test_maf_explorer_agent.py services/api/tests/test_h5_runtime_composition.py -k 'premature_terminal or platform_selected or selected_candidate_binding' -q
docker exec alioth-api python -m ruff check services/api/app/agents/explorer/maf_agent.py services/api/app/application/h5_runtime_composition.py
```

Expected: 唯一安全候选兜底通过；所有负例保持失败关闭。

- [ ] **Step 6: Commit Task 3**

```powershell
git add services/api/app/agents/explorer/maf_agent.py services/api/app/application/h5_runtime_composition.py services/api/tests/test_maf_explorer_agent.py services/api/tests/test_h5_runtime_composition.py
git commit -m "[AI编程] fix: 兜底H5唯一安全候选" -m "涉及模块：MAF Explorer与H5 Runtime。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：外部模型重复提前终止需真实任务观察。"
```

---

### Task 4: Document, Regress, Restart Without Build, and Run the Full Task

**Files:**
- Modify: `docs/superpowers/plans/2026-08-15-h5-business-page-popup-loop.md`
- Modify: `docs/专项能力与高频回归规则.md`
- Verify only: `.alioth-h5-v2.override.yml`
- Verify only: `.alioth-llm-bridge.py`

**Interfaces:**
- Consumes: Tasks 1–3 的提交与所有现有 H5 V2 能力。
- Produces: 可审计回归记录、无 build Docker 实跑、页面/向量/Worker 最终报告。

- [ ] **Step 1: Update reusable regression rules**

文档必须明确：帖子标题 selector 专项验签、目录外候选整条丢弃、目录外落地页 skipped/back、唯一 selected low/medium 候选的类型化兜底、`ranking` 当前未编目不进覆盖分母。

- [ ] **Step 2: Run focused and broad regressions**

Run:

```powershell
docker exec alioth-api sh -lc 'python -m pytest services/api/tests/test_h5_*.py services/api/tests/test_maf_explorer_agent.py services/api/tests/test_page_detection_tools.py services/api/tests/test_page_capture_tool.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_v2_control_plane_repositories.py -q'
docker exec alioth-api python -m ruff check services/api
git diff --check
```

Expected: 本次相关测试全部通过。若仓库既有 H5 Evidence Handoff 两项仍失败，单独重跑并确认与本次改动无调用关系；真实流程若命中同一问题则纳入后续阻断修复。

- [ ] **Step 3: Commit documentation**

```powershell
git add docs/superpowers/plans/2026-08-15-h5-business-page-popup-loop.md docs/专项能力与高频回归规则.md
git commit -m "[AI编程] docs: 固化H5目录跳过与标题详情回归" -m "涉及模块：H5专项回归文档。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实站点目录外重定向和目标覆盖待实跑。"
```

- [ ] **Step 4: Record image IDs and restart only**

Run:

```powershell
docker inspect --format '{{.Name}}|{{.Image}}|{{.State.Status}}' alioth-api alioth-worker
docker compose -f infra/docker-compose.yml -f .alioth-h5-v2.override.yml restart alioth-api alioth-worker
docker inspect --format '{{.Name}}|{{.Image}}|{{.State.Status}}' alioth-api alioth-worker
```

Expected: 两次镜像 ID 完全相同；命令中没有 `build`、`up --build` 或镜像重建。

- [ ] **Step 5: Verify effective configuration without printing the Key**

检查 provider=`custom`、style=`chat_completion`、model=`grok-4.5`、容器 base URL=`http://host.docker.internal:18080/v1`、timeout=`60`、retries=`5`、state store=`postgres`、sensitive=`true`、H5 V2=`on`。API Key 只输出 `<configured>`。

- [ ] **Step 6: Login and submit the exact task**

使用 `POST /api/v1/auth/login` 的 `token` 字段构造 Bearer header，再向 `POST /api/v2/tasks` 提交用户给定 payload；URL 使用原始 `https://35.194.167.63:85/`，不得提交 Markdown 包装字符串。

- [ ] **Step 7: Monitor to terminal and repair blockers**

每 20 秒读取任务和 page timeline；同时查询 `qc_v2_work_item`、`qc_action_execution`、Capture/Assessment 与 Worker 日志。每个新阻断必须按 systematic-debugging + TDD 复现、最小修复、相关回归、提交、无 build restart、新任务重跑，直至没有影响目录内探索的技术阻断。

- [ ] **Step 8: Verify vector knowledge and coverage**

从 Capture `page_detection.navigation_knowledge` 元数据统计：

```text
vector_lookup_attempt_count
match_type_counts.vector
match_type_counts.static
repository_degraded
```

逐项列出 13 个用户目标的 `covered/not_reached/uncatalogued/not_observed`、实际 URL、关键动作和 back 路径。目录内覆盖比例分母为用户目标与当前 catalog 的交集；同时保留原始 13 项状态，避免隐藏 `ranking` 跳过事实。

- [ ] **Step 9: Final verification and status**

Run:

```powershell
git status --short --branch
git log -6 --oneline
```

Expected: 只有用户已有的两个未跟踪辅助文件；没有未提交实现。最终声明必须附测试数量、真实任务 ID/Run ID、Docker 镜像 ID、向量统计和未覆盖原因。
