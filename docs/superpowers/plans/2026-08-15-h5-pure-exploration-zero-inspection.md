# H5 Pure Exploration and Zero-Inspection Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 H5 Explorer 只依据页面 Observation、动态 Frontier、页面类型覆盖缺口、安全边界和预算执行探索，并把规则判定完全留给探索封存后的独立 Rule Judge，同时提供可查询的页面导航向量审计与真实覆盖验收。

**Architecture:** H5 生产入口使用唯一专用 Skill、专用纯探索提示词和只接受 `ExplorationWork` 的合同；H5 Turn Runner、Runtime 和组合根删除所有 Probe/补证路径，并改用只发布 Exploration Work 的专用编排器。页面导航别名保留 Catalog → DB exact → pgvector 的知识检索顺序，把命中来源、降级和实际候选影响写入页面判定审计；探索封存后由 Worker 外层发布 Inspection Work，独立 Rule Judge 执行规则。

**Tech Stack:** Python 3.12、FastAPI、Microsoft Agent Framework、PostgreSQL/pgvector、Redis Streams、Playwright、pytest/pytest-asyncio、Docker Compose。

## Global Constraints

- 后端固定使用 Python FastAPI，数据库固定使用 PostgreSQL，AI 框架固定使用 Microsoft Agent Framework。
- 所有新增注释与日志使用中文，文件保持 UTF-8。
- H5 Explorer 不得读取 ruleset、规则覆盖、Rule Judge 结果、`ProbeWork`、`EvidenceRequest`、Inspection Work 或报告状态。
- H5 Explorer 只接受 `ExplorationWork`，只输出 `ActionIntent` 或 `ExplorationTerminal`。
- 页面覆盖只由真实 Capture、Catalog 页面类型、Frontier、安全边界和预算决定；未执行候选不得计为已探索页面。
- 支付、发布和设置页面允许安全进入，但禁止确认支付、提交发布、删除、授权或其他高副作用动作。
- App Explorer 与 App Probe 合同不在本次删除范围，必须保持回归通过。
- 不引入数据库迁移，不修改前端菜单或路由。
- Docker 联合验收不得执行 build；worktree 源码通过 bind mount 重新创建 API/Worker 容器时使用 `up -d --no-build --force-recreate`。
- 认证凭据、Cookie、Token 和 Authorization Header 不得打印、写文件或提交 Git。
- 不删除、覆盖或暂存当前工作区中的无关未跟踪文件。
- 修复具有复用价值的问题后同步更新 `docs/专项能力与高频回归规则.md`。

---

### Task 1: 建立 H5 唯一 Skill 与纯探索系统提示词

**Files:**
- Create: `services/api/app/agents/explorer/h5_instructions.py`
- Modify: `services/api/app/agents/explorer/maf_agent.py:89-132,331-350`
- Modify: `services/api/app/agents/explorer/bootstrap.py:28-41`
- Modify: `services/api/app/agents/skills/loader.py:144-198`
- Modify: `services/api/app/agents/skills/h5-explorer-workflow/SKILL.md`
- Modify: `services/api/tests/test_h5_explorer_bootstrap.py:26-80`
- Create: `services/api/tests/test_h5_explorer_instructions.py`

**Interfaces:**
- Consumes: `ExplorerDecisionModel.model_json_schema()`、`build_h5_explorer_context_provider()`、`MafExplorerAgent` 的既有结构化候选选择能力。
- Produces: `build_h5_explorer_system_instructions(*, output_schema: Mapping[str, Any], required_workflow_skill: str) -> str`；H5 Bootstrap 只请求 `("h5-explorer-workflow",)`。

- [ ] **Step 1: 写 H5 Skill 单一加载失败测试**

把 `test_default_h5_explorer_excludes_app_workflow_skills` 的核心断言改为：

```python
assert captured["required_workflow_skills"] == ("h5-explorer-workflow",)
assert captured["system_instructions_builder"] is h5_instructions.build_h5_explorer_system_instructions
```

把 Provider 测试改为只允许唯一 Skill：

```python
workflow = await provider.load_skill("h5-explorer-workflow")
assert "动态 Frontier" in workflow
for forbidden_skill in ("page-exploration", "navigation-boundary-skill", "app-workflow"):
    with pytest.raises(ValueError, match="不存在"):
        await provider.load_skill(forbidden_skill)
```

- [ ] **Step 2: 写纯探索提示词和 Skill 禁止语义失败测试**

在新文件中加入：

```python
from services.api.app.agents.explorer.h5_instructions import (
    build_h5_explorer_system_instructions,
)
from services.api.app.agents.explorer.models import ExplorerDecisionModel
from services.api.app.agents.skills.loader import build_h5_explorer_context_provider

FORBIDDEN = (
    "ProbeWork",
    "EvidenceRequest",
    "Rule Judge",
    "ruleset",
    "规则覆盖",
    "pass",
    "fail",
    "manual_review",
    "evidence_insufficient",
    "报告",
)

async def test_h5_prompt_and_skill_are_pure_exploration() -> None:
    prompt = build_h5_explorer_system_instructions(
        output_schema=ExplorerDecisionModel.model_json_schema(),
        required_workflow_skill="h5-explorer-workflow",
    )
    skill = await build_h5_explorer_context_provider().load_skill(
        "h5-explorer-workflow"
    )

    for marker in FORBIDDEN:
        assert marker.casefold() not in prompt.casefold()
        assert marker.casefold() not in skill.casefold()
    for required in ("页面类型覆盖缺口", "动态 Frontier", "ActionIntent", "ExplorationTerminal"):
        assert required in prompt + skill
```

- [ ] **Step 3: 运行新测试并确认红灯**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_explorer_instructions.py
```

Expected: FAIL；现有 Bootstrap 仍加载三个 Skill，`h5_instructions.py` 尚不存在。

- [ ] **Step 4: 创建 H5 专用纯探索提示词**

`h5_instructions.py` 使用以下稳定结构，不引用任何禁止语义：

```python
"""H5 Explorer 的纯页面探索系统提示词。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

H5_EXPLORER_SYSTEM_PROMPT_VERSION = "h5-explorer-system.v2.0-pure-exploration"

def build_h5_explorer_system_instructions(
    *,
    output_schema: Mapping[str, Any],
    required_workflow_skill: str,
) -> str:
    return (
        f"系统提示词版本：{H5_EXPLORER_SYSTEM_PROMPT_VERSION}。\n"
        "你只负责 H5 页面探索。输入只包含平台提供的只读 Observation、动态 Frontier、"
        "页面类型覆盖缺口、安全边界、预算和截止时间。\n"
        "每轮只能逐字选择当前可执行候选的 candidate_id，输出一条 ActionIntent；"
        "只有当前分支确实无法继续时才输出 ExplorationTerminal。\n"
        "不得执行浏览器动作，不得生成 Locator、坐标、脚本、SQL 或运行时身份。\n"
        "排序只依据页面结构信息增益、尚未观察的页面类型、动作可逆性、风险和预算。\n"
        f'首次模型决策前必须完整加载 {required_workflow_skill}。\n'
        "只返回下列 JSON Schema 中的一个对象：\n"
        + json.dumps(output_schema, ensure_ascii=False, indent=2, sort_keys=True)
    )
```

- [ ] **Step 5: 让 MafExplorerAgent 支持显式提示词构造器**

在构造函数增加：

```python
system_instructions_builder: Callable[..., str] = build_explorer_system_instructions,
```

把它传给 `_build_framework_agent`；该函数增加同名参数并改为：

```python
instructions=system_instructions_builder(
    output_schema=ExplorerDecisionModel.model_json_schema(),
    required_workflow_skill=required_workflow_skill,
),
```

App 默认不传该参数，保持现有系统提示词；H5 Bootstrap 显式传 `build_h5_explorer_system_instructions`。

- [ ] **Step 6: 收紧 H5 Skill Provider 和 Skill 正文**

`build_h5_explorer_context_provider()` 改为：

```python
return build_skills_context_provider(
    sub_dirs=("h5-explorer-workflow",),
    source_id="h5-explorer-workflow",
)
```

Skill 正文只保留页面探索顺序、页面类型覆盖缺口、Frontier、状态版本、安全边界、预算、Capture 引用和局部终态。`validate_h5_explorer_workflow_skill_content()` 的必需标记改为：

```python
required_markers = (
    "页面类型覆盖缺口",
    "动态 Frontier",
    "ActionIntent",
    "ExplorationTerminal",
    "不得直接调用 Playwright",
)
forbidden_markers = (
    "ProbeWork",
    "EvidenceRequest",
    "Rule Judge",
    "ruleset",
    "规则覆盖",
    "manual_review",
    "evidence_insufficient",
    "报告",
)
```

发现禁止标记时抛出 `H5ExplorerWorkflowSkillConfigurationError("h5-explorer-workflow Skill 包含非探索语义：...")`。

- [ ] **Step 7: 运行目标测试和 App 提示词兼容测试**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_explorer_instructions.py services/api/tests/test_v2_agent_instructions.py services/api/tests/test_maf_explorer_agent.py
```

Expected: PASS；H5 只加载唯一 Skill，App 现有 Explorer 行为不变。

- [ ] **Step 8: 提交 H5 课程与提示词**

```powershell
git add -- services/api/app/agents/explorer/h5_instructions.py services/api/app/agents/explorer/maf_agent.py services/api/app/agents/explorer/bootstrap.py services/api/app/agents/skills/loader.py services/api/app/agents/skills/h5-explorer-workflow/SKILL.md services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_explorer_instructions.py
git commit -m "[AI编程] refactor: 纯化H5探索课程与提示词" -m "涉及模块：H5 Explorer Skill、提示词与生产组装；数据库迁移：无；前端菜单/路由同步：无需；未覆盖测试风险：真实LLM候选选择待联合验收。"
```

---

### Task 2: 删除 H5 Runtime 与编排层的 Probe 语义

**Files:**
- Create: `services/api/app/agents/explorer/h5_contracts.py`
- Create: `services/api/app/application/orchestration/exploration_service.py`
- Modify: `services/api/app/application/orchestration/__init__.py`
- Modify: `services/api/app/application/orchestration/maf_workflow.py`
- Modify: `services/api/app/application/h5_exploration_turn.py:45-50,113-180,578-705,724-728,877-884`
- Modify: `services/api/app/application/h5_runtime_composition.py:100-190,195-280,288-316,1248-1342,1390-1596`
- Modify: `services/api/tests/test_h5_exploration_turn.py:179-190,320-425`
- Modify: `services/api/tests/test_h5_runtime_composition.py:180-530`
- Create: `services/api/tests/test_h5_pure_exploration_architecture.py`

**Interfaces:**
- Consumes: `ExplorationWorkPublisher`、`StartExplorationCommand`、`build_start_exploration_workflow()`、`prepare_exploration_work()`。
- Produces: `H5ExplorerAgent.decide_next(work: ExplorationWork)`；`ExplorationOrchestrator.start_exploration()` / `prepare_exploration()`；无 `work_type` 参数的 `H5RuntimePort.run_one_turn()`。

- [ ] **Step 1: 写 H5 只接受 ExplorationWork 的失败测试**

新增合同测试：

```python
import inspect

from services.api.app.agents.explorer.h5_contracts import H5ExplorerAgent
from services.api.app.application import h5_exploration_turn, h5_runtime_composition

def test_h5_runtime_has_no_probe_contract() -> None:
    sources = "\n".join(
        inspect.getsource(module)
        for module in (h5_exploration_turn, h5_runtime_composition)
    )
    for marker in (
        "ProbeWork",
        "PostgresProbeWorkPublisher",
        "ExplorerWorkType.PROBE",
        "probe_claim_enabled",
        "SupplementalEvidenceOperation",
        "build_current_supplemental_evidence_policy",
    ):
        assert marker not in sources

def test_h5_explorer_contract_accepts_only_exploration_work() -> None:
    annotation = inspect.signature(H5ExplorerAgent.decide_next).parameters["work"].annotation
    assert annotation == "ExplorationWork"
```

- [ ] **Step 2: 把 H5 Runner 取消测试改为只取消 exploration**

修改 `_CancellationQueue.cancel_nonterminal`：

```python
assert tuple(item.value for item in work_types) == ("exploration",)
self.cancelled_work_types.add("exploration")
return 1
```

测试名改为 `test_h5_runner_cancels_exploration_before_claim`，最终断言：

```python
assert queue.cancelled_work_types == {"exploration"}
```

- [ ] **Step 3: 把 Executor 测试改为不存在 Probe 分支**

所有 Fake Runtime 的方法改为：

```python
async def run_one_turn(self, *, task_id, agent_run_id):
    ...
```

删除“开启 Probe”“关闭时跳过 Probe”“共享 Exploration/Probe 预算”的旧测试，新增：

```python
@pytest.mark.asyncio
async def test_executor_consumes_only_exploration_until_terminal() -> None:
    calls: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=3)

        async def start(self, command):
            return None

        async def run_one_turn(self, *, task_id, agent_run_id):
            calls.append("exploration")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            return None

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5"
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert calls == ["exploration"]
    assert result.working_memory.step_count == 1
```

- [ ] **Step 4: 运行边界测试并确认红灯**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py
```

Expected: FAIL；H5 模块仍包含 Probe 类型、publisher、策略开关和工作类型参数。

- [ ] **Step 5: 创建纯探索 Agent 与编排合同**

`h5_contracts.py`：

```python
"""H5 纯探索 Agent 的输入输出端口。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.domain.exploration import ExplorationTerminal, ExplorationWork
from services.api.app.domain.interaction import ActionIntent

class H5ExplorerAgent(Protocol):
    async def decide_next(
        self,
        work: ExplorationWork,
    ) -> ActionIntent | ExplorationTerminal: ...
```

`exploration_service.py` 创建只依赖 Exploration publisher 的 `ExplorationOrchestrator`，公开：

```python
class ExplorationOrchestrator:
    def __init__(self, *, exploration_publisher: ExplorationWorkPublisher) -> None:
        self._exploration_publisher = exploration_publisher

    async def start_exploration(self, command: StartExplorationCommand) -> ExplorationWork:
        workflow = build_start_exploration_workflow(self._exploration_publisher)
        result = await workflow.run(command, include_status_events=True)
        return require_exploration_work_output(result)

    async def prepare_exploration(self, command: StartExplorationCommand) -> ExplorationWork:
        return prepare_exploration_work(command)
```

为避免调用私有函数，把 `maf_workflow.py` 中现有 `_require_single_idle_output` 的 Exploration 专用包装导出为 `require_exploration_work_output(result)`，并由原 `MicrosoftAgentFrameworkQcWorkflow.start_exploration()` 同步复用。

- [ ] **Step 6: 纯化 H5 Turn Runner**

- 删除 `ProbeWork` 导入。
- `run_one()` 删除 `work_type` 参数。
- `ClaimExplorerWorkCommand.work_type` 固定为 `ExplorerWorkType.EXPLORATION`。
- 非终态一律准备 Exploration successor，删除 `not isinstance(lease.work, ProbeWork)`。
- `cancel_nonterminal()` 只传 `(ExplorerWorkType.EXPLORATION,)`。
- `_validate_h5_work()` 只接受 `ExplorationWork`。
- `_failure_terminal(... work: ExplorationWork, ...)` 删除联合类型。

- [ ] **Step 7: 纯化 H5 Runtime 与组合根**

- `H5RuntimePort.run_one_turn()` 和 `H5V2Runtime.run_one_turn()` 删除 `work_type` 参数。
- `PostgresH5RuntimeProvider` 使用 `ExplorationOrchestrator`，只注入 `PostgresExplorationWorkPublisher`。
- 删除 `PostgresProbeWorkPublisher`、supplemental policy 导入和所有 Probe 日志。
- `ProductionH5V2RuntimeExecutor.__init__()` 删除 `probe_claim_enabled`。
- `_consume()` 只保留 Exploration 循环，调用 `runtime.run_one_turn(task_id=..., agent_run_id=...)`。
- `build_h5_workflow_handler()` 不读取 `settings.supplemental_evidence`，直接构造纯探索 Executor。
- `H5CompletionAwareExplorer` 的 delegate 类型改为 `H5ExplorerAgent`。

- [ ] **Step 8: 运行 H5、共享编排和 App Probe 回归**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_agent_architecture.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py
```

Expected: PASS；H5 无 Probe，App Probe 与共享控制面仍通过。

- [ ] **Step 9: 提交纯探索运行时**

```powershell
git add -- services/api/app/agents/explorer/h5_contracts.py services/api/app/application/orchestration/exploration_service.py services/api/app/application/orchestration/__init__.py services/api/app/application/orchestration/maf_workflow.py services/api/app/application/h5_exploration_turn.py services/api/app/application/h5_runtime_composition.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_pure_exploration_architecture.py
git commit -m "[AI编程] refactor: 移除H5探索质检补采编排" -m "涉及模块：H5 Explorer合同、Turn Runner、Runtime与探索编排；数据库迁移：无；前端菜单/路由同步：无需；未覆盖测试风险：真实PostgreSQL租约与浏览器长链待联合验收。"
```

---

### Task 3: 增加页面导航向量命中、降级与候选影响审计

**Files:**
- Modify: `services/api/app/domain/page/nav_alias.py:31-133,218-234`
- Modify: `services/api/app/agents/tools/page_detection_tools.py:317-331,1422-1436,1800-1921,3452-3471,3535-3551,3554-3587,4260-4413`
- Modify: `services/api/app/application/h5_exploration_observation.py:250-325`
- Modify: `services/api/app/infrastructure/db/page_capture_repository.py:206-271`
- Modify: `services/api/tests/test_page_nav_alias_authority.py`
- Modify: `services/api/tests/test_page_detection_tools.py:4400-4510`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `services/api/tests/test_page_capture_persistence.py`

**Interfaces:**
- Consumes: `PageNavAliasRepositoryProtocol`、`PageNavAliasMatch`、页面识别 `CandidateAction`。
- Produces: `PageNavAliasResolution`、`PageNavAliasResolver.resolve_with_audit()`、页面判定 `navigation_knowledge` 与分来源 metrics、Frontier candidate metadata 中的导航知识来源。
- Persists: `qc_page_capture.metadata.page_detection.navigation_knowledge`，供真实任务验收查询向量命中与降级事实。

- [ ] **Step 1: 写 Resolver 降级审计失败测试**

加入：

```python
class VectorFailingRepository:
    async def find_confirmed_exact(self, normalized_text):
        return None

    async def find_confirmed_vector_match(self, **kwargs):
        raise RuntimeError("pgvector unavailable")

async def test_vector_failure_returns_degraded_audit_without_raising() -> None:
    resolver = PageNavAliasResolver(repository=VectorFailingRepository())

    resolution = await resolver.resolve_with_audit("神秘短剧")

    assert resolution.match is None
    assert resolution.repository_degraded is True
    assert resolution.degradation_stage == "vector"
    assert resolution.degradation_code == "page_nav_alias_repository_unavailable"
```

- [ ] **Step 2: 写页面判定向量影响失败测试**

使用返回 `PageNavAliasResolution(match=PageNavAliasMatch(... match_type="vector"))` 的 Fake Resolver，构造带“神秘短剧”底栏的 DOM，断言：

```python
assert result["metrics"]["nav_alias_vector_match_count"] == 1
assert result["metrics"]["nav_alias_exact_match_count"] == 0
assert result["navigation_knowledge"]["repository_degraded"] is False
assert result["navigation_knowledge"]["matches"] == [
    {
        "canonical_page_type": "content_list",
        "confidence": 0.93,
        "match_type": "vector",
        "used_for_page_type": False,
        "candidate_targets": ["神秘短剧"],
    }
]
vector_actions = [
    item for item in result["candidate_actions"]
    if item.get("navigation_knowledge_source") == "vector"
]
assert vector_actions[0]["expected_next_page_type"] == "content_list"
```

- [ ] **Step 3: 写 Frontier 元数据传递失败测试**

在 H5 observation 测试中向原始 candidate action 加入：

```python
"navigation_knowledge_source": "vector",
"navigation_knowledge_confidence": 0.93,
```

并断言生成的 `CandidateAction.metadata` 保留同名字段。

- [ ] **Step 3A: 写页面判定向量审计持久化失败测试**

在 `test_page_capture_persistence.py` 中用 Fake connection 捕获 SQL 参数并解析导航知识 JSON：

```python
updated = await repository.update_detection_result(
    task_id=task_id,
    agent_run_id=run_id,
    page_capture_id=capture_id,
    page_type="content_list",
    page_state="stable",
    detection_status="recognized",
    confidence=0.93,
    catalog_version="catalog-v1",
    catalog_sha256="catalog-sha",
    navigation_knowledge={
        "repository_degraded": False,
        "matches": [{"canonical_page_type": "content_list", "match_type": "vector"}],
    },
)
assert updated is True
assert json.loads(connection.fetchval_args[-1])["matches"][0]["match_type"] == "vector"
```

- [ ] **Step 4: 运行向量目标测试并确认红灯**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_page_nav_alias_authority.py services/api/tests/test_page_detection_tools.py -k "nav_alias or vector" services/api/tests/test_h5_exploration_observation.py
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_page_capture_persistence.py
```

Expected: FAIL；当前 Resolver 没有结构化审计，页面判定只记录总命中数。

- [ ] **Step 5: 实现无敏感数据的 Resolution 合同**

在 `nav_alias.py` 增加：

```python
@dataclass(frozen=True)
class PageNavAliasResolution:
    match: PageNavAliasMatch | None
    repository_degraded: bool = False
    degradation_stage: str | None = None
    degradation_code: str | None = None
```

`resolve_with_audit()` 保持静态 → exact → vector 顺序；仓储异常返回固定 `degradation_code`，异常详情只写服务日志。现有 `resolve()` 委托该方法并只返回 `.match`，保持调用方兼容。

- [ ] **Step 6: 为页面判定生成向量审计与影响字段**

- `_resolve_visible_nav_alias_matches()` 同时返回 match map 和 resolution audit。
- `CandidateAction` 增加可选字段：

```python
navigation_knowledge_source: str | None = None
navigation_knowledge_confidence: float | None = Field(default=None, ge=0, le=1)
```

- 可见底栏与页内 Tab 候选由别名解析生成时填入上述字段。
- `result_payload["metrics"]` 增加 static/exact/vector 三类计数。
- `result_payload["navigation_knowledge"]` 只保存 canonical page type、置信度、匹配类型、是否用于当前页面类型、候选 target 和固定降级码，不保存异常堆栈或凭据。
- `h5_exploration_observation.py` 把两个可选字段传入 Frontier candidate metadata。
- `PostgresPageCaptureRepository.update_detection_result()` 增加 `navigation_knowledge: Mapping[str, Any] | None = None`，以 `$13::jsonb` 写入 `metadata.page_detection.navigation_knowledge`。
- `persist_page_detection_for_current_run()` 把 `result.get("navigation_knowledge")` 传给仓储，不改变现有页面判定字段。

- [ ] **Step 7: 运行页面判定、缓存和 H5 Frontier 回归**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_page_nav_alias_authority.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_runtime_composition.py
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_page_capture_persistence.py
```

Expected: PASS；同一 DOM/截图但向量快照不同仍不能错误复用缓存。

- [ ] **Step 8: 提交向量审计**

```powershell
git add -- services/api/app/domain/page/nav_alias.py services/api/app/agents/tools/page_detection_tools.py services/api/app/application/h5_exploration_observation.py services/api/tests/test_page_nav_alias_authority.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py
git add -- services/api/app/infrastructure/db/page_capture_repository.py services/api/tests/test_page_capture_persistence.py
git commit -m "[AI编程] feat: 审计H5页面导航向量应用" -m "涉及模块：页面导航别名、页面判定与H5 Frontier；数据库迁移：无；前端菜单/路由同步：无需；未覆盖测试风险：真实pgvector命中分布待联合验收。"
```

---

### Task 4: 固化架构回归与专项规则

**Files:**
- Modify: `services/api/tests/test_h5_pure_exploration_architecture.py`
- Modify: `services/api/tests/test_v2_agent_architecture.py`
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Consumes: Tasks 1-3 的 H5 Prompt、Skill、合同、Runtime 和向量审计字段。
- Produces: 可长期回归的 H5 零质检语义门禁与专项规则。

- [ ] **Step 1: 完善 AST/源码架构门禁**

门禁必须分别检查：

```python
H5_ONLY_SKILL = ("h5-explorer-workflow",)
H5_FORBIDDEN_RUNTIME_NAMES = {
    "ProbeWork",
    "PostgresProbeWorkPublisher",
    "SupplementalEvidenceOperation",
    "build_current_supplemental_evidence_policy",
}
H5_FORBIDDEN_SKILL_TERMS = {
    "EvidenceRequest",
    "Rule Judge",
    "ruleset",
    "规则覆盖",
    "manual_review",
    "evidence_insufficient",
    "报告",
}
```

并保留“Explorer 终态不直接标记 Task 完成”“Rule Judge 经独立 inspection consumer 调用”的既有架构断言。

- [ ] **Step 2: 运行架构测试**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_v2_agent_architecture.py services/api/tests/test_inspection_work_item_consumer.py services/api/tests/test_worker_h5_v2_finalization.py
```

Expected: PASS。

- [ ] **Step 3: 更新专项回归规则**

在文档末尾新增“2026-08-15：H5 Explorer 必须保持纯探索、零质检语义”，明确：

- H5 只加载唯一专用 Skill。
- H5 Prompt/合同/Runner/Runtime 不存在 Probe、补证和规则语义。
- 页面类型覆盖不读取 ruleset，只认真实 Capture。
- 页面导航向量只映射 Catalog 页面类型，并必须记录命中来源与候选影响。
- 探索封存后才发布 Inspection Work；独立 Judge 不反向补采。
- 真实验收禁止用 `not_found`、未执行候选或安全阻断冒充观察覆盖。

- [ ] **Step 4: 运行格式与相关回归**

Run:

```powershell
git diff --check
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_explorer_instructions.py services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_page_nav_alias_authority.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_inspection_work_item_consumer.py services/api/tests/test_worker_h5_v2_finalization.py
```

Expected: PASS，`git diff --check` 返回 0。

- [ ] **Step 5: 提交架构门禁和专项规则**

```powershell
git add -- services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_v2_agent_architecture.py docs/专项能力与高频回归规则.md
git commit -m "[AI编程] test: 固化H5纯探索架构门禁" -m "涉及模块：H5架构回归与专项规则；数据库迁移：无；前端菜单/路由同步：无需；未覆盖测试风险：真实站点可达页面由后续联合验收确认。"
```

---

### Task 5: 完成全量验证、无构建容器切换与真实任务验收

**Files:**
- Runtime only: `.alioth-h5-v2.override.yml`
- Runtime evidence: Docker logs、API 响应和 PostgreSQL 只读查询；不得写入仓库凭据文件。
- Update on reusable fix only: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Consumes: Tasks 1-4 的全部提交、现有 Docker image `alioth-api:local`、API `/api/v2/tasks`、PostgreSQL 表 `qc_page_capture`、`qc_h5_page_type_assessment`、`qc_v2_work_item`、`qc_inspection_work_item`、`qc_inspection_work_item_rule`、`qc_rule_execution`。
- Produces: 新 `task_id/run_id`、向量应用审计、真实页面清单、独立规则执行清单、覆盖率和阻断修复记录。

- [ ] **Step 1: 运行目标与广覆盖回归**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest -q services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_explorer_instructions.py services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_page_nav_alias_authority.py services/api/tests/test_page_detection_tools.py services/api/tests/test_inspection_work_item_consumer.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_v2_agent_architecture.py
```

Expected: PASS。

- [ ] **Step 2: 运行静态检查**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check services/api/app/agents/explorer services/api/app/agents/skills/loader.py services/api/app/application/h5_exploration_turn.py services/api/app/application/h5_runtime_composition.py services/api/app/application/orchestration services/api/app/domain/page/nav_alias.py services/api/app/agents/tools/page_detection_tools.py services/api/app/application/h5_exploration_observation.py services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_explorer_instructions.py services/api/tests/test_h5_pure_exploration_architecture.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_page_nav_alias_authority.py services/api/tests/test_h5_exploration_observation.py
git diff --check
```

Expected: PASS，且没有凭据或无关文件进入 diff。

- [ ] **Step 3: 用 worktree bind mount 无构建重建 API/Worker 容器**

从当前 worktree 根执行：

若 runtime-only override 不存在，先用 `apply_patch` 创建未跟踪文件，固定内容为：

```yaml
services:
  alioth-api:
    environment:
      ALIOTH_H5_V2_MODE: "on"
  alioth-worker:
    environment:
      ALIOTH_H5_V2_MODE: "on"
```

```powershell
if (-not (Test-Path -LiteralPath '.alioth-h5-v2.override.yml')) {
    throw '缺少运行期 H5 V2 override；在 worktree 根创建仅包含 API/Worker ALIOTH_H5_V2_MODE=on 的未跟踪文件后重试。'
}
docker compose -f infra/docker-compose.yml -f .alioth-h5-v2.override.yml up -d --no-build --no-deps --force-recreate alioth-api alioth-worker
```

Expected: 使用现有 image，容器 bind mount 指向当前 worktree；不得出现 build 输出。

- [ ] **Step 4: 验证容器和 H5 V2 配置**

Run:

```powershell
docker inspect alioth-api --format '{{json .Mounts}}'
docker inspect alioth-worker --format '{{json .Mounts}}'
docker inspect alioth-api --format '{{range .Config.Env}}{{println .}}{{end}}' | Select-String '^ALIOTH_H5_V2_MODE=on$'
docker inspect alioth-worker --format '{{range .Config.Env}}{{println .}}{{end}}' | Select-String '^ALIOTH_H5_V2_MODE=on$'
Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/health'
```

Expected: `/workspace` 指向当前 worktree，两个容器均为 H5 V2 on，健康检查成功。

- [ ] **Step 5: 安全登录并提交用户指定任务**

使用本地种子解析测试账号；脚本不得输出 `$login`、`$token` 或 `$headers`：

```powershell
$ErrorActionPreference = 'Stop'
$seed = Get-Content -LiteralPath 'services/api/migrations/versions/20260709_0002_add_qc_user.py' -Encoding UTF8
$credentialLine = $seed | Where-Object { $_ -match '^\s*\(' -and $_ -match 'admin' } | Select-Object -First 1
$fields = [regex]::Matches($credentialLine, "'([^']*)'")
$login = @{ username = $fields[0].Groups[1].Value; password = $fields[2].Groups[1].Value } | ConvertTo-Json
$auth = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/auth/login' -ContentType 'application/json' -Body $login
$token = if ($auth.access_token) { $auth.access_token } else { $auth.data.access_token }
$headers = @{ Authorization = "Bearer $token" }
$body = @{
  task_name = '测试1'
  target = @{ platform = 'h5'; url = 'https://35.194.167.63:85/' }
  ruleset_code = 'alioth_first_ruleset'
  ruleset_version = 1
  channel = 'workbench'
  priority = 100
  product = @{
    department_name = '本地测试'
    product_code = 'LOCAL-E2E'
    product_name = '本地完整链路测试'
  }
  scan_mode = 'full'
  journey_steps = @()
  remark = 'H5 V2 完整链路测试'
} | ConvertTo-Json -Depth 8
$created = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v2/tasks' -Headers $headers -ContentType 'application/json' -Body $body
$created | Select-Object task_id, run_id, status
```

Expected: HTTP 2xx，只显示新 `task_id`、`run_id` 和初始状态。

- [ ] **Step 6: 持续观察直至完整终态**

每 10 秒查询任务详情和报告，同时过滤 Worker 当前任务日志；单次 Run 最长观察 15 分钟，若仍有进展则继续，不因模型等待提前中止：

```powershell
$detail = Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:8000/api/v2/tasks/{0}" -f $created.task_id) -Headers $headers
$report = Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:8000/api/v2/tasks/{0}/report" -f $created.task_id) -Headers $headers
docker logs --since 20m alioth-worker 2>&1 | Select-String $created.task_id,$created.run_id,'nav_alias','vector','H5','inspection'
```

Expected: 探索封存、Inspection Work、Rule Judge 和报告均进入终态；日志不得出现 H5 Probe 领取或补证分支。

- [ ] **Step 7: 用只读 SQL 提取真实探索、向量和规则事实**

先检查实际 schema，再执行按 task/run 过滤的只读查询：

```powershell
docker exec alioth-postgres psql -U alioth -d alioth -c "\d qc_page_capture"
docker exec alioth-postgres psql -U alioth -d alioth -c "\d qc_h5_page_type_assessment"
docker exec alioth-postgres psql -U alioth -d alioth -c "\d qc_v2_work_item"
docker exec alioth-postgres psql -U alioth -d alioth -c "\d qc_inspection_work_item"
docker exec alioth-postgres psql -U alioth -d alioth -c "\d qc_inspection_work_item_rule"
docker exec alioth-postgres psql -U alioth -d alioth -c "\d qc_rule_execution"
```

执行固定只读查询并保留完整输出：

```powershell
$taskId = [string]$created.task_id
$runId = [string]$created.run_id

docker exec alioth-postgres psql -U alioth -d alioth -P pager=off -c "SELECT id, page_seq, url, page_type, page_state, capture_status, capture_role, captured_at, metadata->'page_detection'->'navigation_knowledge' AS navigation_knowledge FROM qc_page_capture WHERE task_id = '$taskId'::uuid AND agent_run_id = '$runId'::uuid ORDER BY page_seq, created_at;"
docker exec alioth-postgres psql -U alioth -d alioth -P pager=off -c "SELECT assessment_id, session_id, page_capture_id, detection_status, page_type, page_state, status, reason_codes, assessment_payload->'catalog_version' AS catalog_version, created_at FROM qc_h5_page_type_assessment WHERE task_id = '$taskId'::uuid AND agent_run_id = '$runId'::uuid ORDER BY created_at;"
docker exec alioth-postgres psql -U alioth -d alioth -P pager=off -c "SELECT work_id, work_type, session_id, branch_id, source_capture_id, status, attempt_count, last_error_code, terminal_payload, created_at, finished_at FROM qc_v2_work_item WHERE task_id = '$taskId'::uuid ORDER BY created_at;"
docker exec alioth-postgres psql -U alioth -d alioth -P pager=off -c "SELECT count(*) AS forbidden_h5_probe_count FROM qc_v2_work_item WHERE task_id = '$taskId'::uuid AND work_type = 'probe';"
docker exec alioth-postgres psql -U alioth -d alioth -P pager=off -c "SELECT work.id AS work_item_id, work.page_capture_id, work.status AS work_status, work.strategy, work.category, item.rule_code, item.status AS rule_status, item.rule_execution_id, item.last_error_code FROM qc_inspection_work_item AS work JOIN qc_inspection_work_item_rule AS item ON item.work_item_id = work.id WHERE work.task_id = '$taskId'::uuid ORDER BY work.created_at, item.ordinal;"
docker exec alioth-postgres psql -U alioth -d alioth -P pager=off -c "SELECT execution.page_capture_id, rule.rule_code, execution.execution_status, execution.judge_strategy, execution.confidence, execution.severity, execution.result_summary, execution.evidence_artifact_ids, execution.error_code, execution.finished_at FROM qc_rule_execution AS execution JOIN qc_rule AS rule ON rule.id = execution.rule_id WHERE execution.task_id = '$taskId'::uuid AND execution.agent_run_id = '$runId'::uuid ORDER BY execution.created_at;"
```

Expected: 不把 `about:blank`、未执行候选或失败动作猜测当作已探索页面。

- [ ] **Step 8: 计算并报告 14 类页面覆盖**

固定清单：

```text
line_loading, startup_ad, home, content_list, content_detail, video_list,
video_detail, ranking, profile, settings, payment, post_list, post_publish, activity
```

逐类给出 `observed/not_found/requires_auth/blocked`，并计算：

```text
实际观察覆盖率 = observed / 14
可达覆盖率 = observed / 经证据证明实际存在且允许安全进入的页面类型数
```

14 类均可达时，完成阈值为至少 13/14；网站确实缺失或被不可绕过安全边界阻断时展示证据并调整可达分母。

- [ ] **Step 9: 对仍可修复阻断执行调试循环**

出现代码缺陷时必须按以下固定循环：

1. 读取任务、Run、Capture、Frontier、向量审计和 Worker 脱敏日志。
2. 使用 `superpowers:systematic-debugging` 确认根因。
3. 使用 `superpowers:test-driven-development` 写能复现根因的失败测试。
4. 实施最小修复并运行目标/相关回归。
5. 使用 Step 3 的 `--no-build --force-recreate` 重新加载 worktree 源码。
6. 创建全新任务复验，不复用失败 Run。

只要仍存在仓库内可修复的探索阻断就继续；外部站点缺页、认证限制或不可控服务故障保留为客观限制。

- [ ] **Step 10: 最终验证和提交运行期发现的复用修复**

Run:

```powershell
git diff --check
git status --short --branch
git log --oneline --decorate -8
```

Expected: 所有实现提交均包含模块、数据库迁移、前端同步和未覆盖风险说明；无凭据、Token、运行日志或无关未跟踪文件进入提交。
