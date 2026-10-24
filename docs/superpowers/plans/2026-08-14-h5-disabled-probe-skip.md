# H5 补采关闭时跳过 Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在补采能力关闭或运行时未就绪时，让 H5 V2 完成主探索后安全跳过 Probe Work 领取，同时保留底层补采门禁和能力可用时的既有 Probe 行为。

**Architecture:** `build_h5_workflow_handler` 作为生产组合根，从当前不可变补采策略的 `CLAIM_PROBE` 决策派生布尔能力并注入 `ProductionH5V2RuntimeExecutor`。执行器只在该能力为真时进入 Probe 排空循环；Repository、Turn Runner 和补采策略继续失败关闭，不捕获或吞掉底层门禁异常。

**Tech Stack:** Python 3.12、FastAPI、pytest/pytest-asyncio、Docker Compose、PostgreSQL、Redis、Playwright H5 Runtime。

## Global Constraints

- 当前分支固定为 `dev_1.5`。
- 不开启 `ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED`，不修改 `runtime_ready=False` 的生产策略。
- 容器验证只允许 `docker compose ... restart` 或 `up -d --no-build`，本轮不得重新构建镜像。
- 不引入数据库迁移，不修改前端菜单或路由。
- 所有新增注释和运行日志使用中文，所有文件保持 UTF-8。
- 不修改 `ExplorerWorkRepository`、`H5ExplorationTurnRunner` 的 Probe 拒绝语义。
- 自动登录凭据和访问 Token 只在进程内存中使用，不打印、不写文件、不提交 Git。
- 不删除或覆盖当前工作区中与本任务无关的未跟踪文件。

---

### Task 1: 用失败测试锁定 Probe 能力边界

**Files:**
- Modify: `services/api/tests/test_h5_runtime_composition.py:179-359`

**Interfaces:**
- Consumes: `ProductionH5V2RuntimeExecutor(worker_instance_id: str, *, probe_claim_enabled: bool, ...)`
- Produces: 关闭时不调用 `ExplorerWorkType.PROBE`、开启时仍调用 Probe 且共享总动作预算的可执行合同。

- [ ] **Step 1: 为关闭路径写失败测试**

在 `test_executor_runs_initial_detection_turn_loop_probe_and_orderly_close` 之后增加：

```python
@pytest.mark.asyncio
async def test_executor_skips_probe_when_probe_claim_is_disabled(caplog) -> None:
    module = _runtime_module()
    calls: list[ExplorerWorkType] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command
            return None

        async def run_one_turn(self, *, task_id, agent_run_id, work_type):
            del task_id, agent_run_id
            calls.append(work_type)
            if work_type is ExplorerWorkType.PROBE:
                raise AssertionError("补采不可用时不得领取 Probe Work")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    with caplog.at_level("INFO"):
        result = await module.ProductionH5V2RuntimeExecutor(
            worker_instance_id="worker-h5",
            probe_claim_enabled=False,
        ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert calls == [ExplorerWorkType.EXPLORATION]
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert "跳过 Probe Work 领取" in caplog.text
```

- [ ] **Step 2: 运行关闭路径测试并确认它因接口尚不存在而失败**

Run:

```powershell
docker exec alioth-api pytest services/api/tests/test_h5_runtime_composition.py::test_executor_skips_probe_when_probe_claim_is_disabled -q
```

Expected: FAIL，错误包含 `unexpected keyword argument 'probe_claim_enabled'`。

- [ ] **Step 3: 显式锁定能力开启时的原有行为**

把已有两个 Probe 正向测试中的构造改为显式开启：

```python
module.ProductionH5V2RuntimeExecutor(
    worker_instance_id="worker-h5",
    probe_claim_enabled=True,
    now=lambda: run_started_at,
)
```

以及：

```python
module.ProductionH5V2RuntimeExecutor(
    worker_instance_id="worker-h5",
    probe_claim_enabled=True,
)
```

分别保留原断言：完整循环为 `EXPLORATION, EXPLORATION, PROBE`，共享预算测试的 `step_count == 3`。

- [ ] **Step 4: 运行执行器相关测试并确认仍为红灯**

Run:

```powershell
docker exec alioth-api pytest services/api/tests/test_h5_runtime_composition.py -k "executor and (probe or action_cap)" -q
```

Expected: FAIL，仅因 `probe_claim_enabled` 尚未实现；不得出现测试夹具或导入错误。

---

### Task 2: 在组合根注入策略能力并最小实现跳过逻辑

**Files:**
- Modify: `services/api/app/application/h5_runtime_composition.py:1-15,1380-1551`
- Modify: `services/api/tests/test_h5_runtime_composition.py:425-437`

**Interfaces:**
- Consumes: `build_current_supplemental_evidence_policy(enabled: bool)` 与 `SupplementalEvidenceOperation.CLAIM_PROBE`。
- Produces: `ProductionH5V2RuntimeExecutor(..., probe_claim_enabled: bool = False)`；生产组合根按策略决策注入该值。

- [ ] **Step 1: 为生产组合根写失败测试**

在现有 handler 组合测试后增加：

```python
@pytest.mark.parametrize("enabled", [False, True])
def test_h5_handler_disables_probe_claim_while_supplemental_runtime_is_unavailable(
    enabled: bool,
) -> None:
    module = _runtime_module()
    handler = module.build_h5_workflow_handler(
        Settings.model_validate(
            {
                "workflow": {"h5_v2_mode": "on"},
                "supplemental_evidence": {"enabled": enabled},
            }
        ),
        "worker-h5",
        provider=SimpleNamespace(build=None),
    )

    assert handler._executor._probe_claim_enabled is False
```

该参数化用例同时证明“配置关闭”和“配置打开但当前生产运行时仍未就绪”都不会进入 Probe 领取。

- [ ] **Step 2: 运行组合根测试并确认失败**

Run:

```powershell
docker exec alioth-api pytest services/api/tests/test_h5_runtime_composition.py::test_h5_handler_disables_probe_claim_while_supplemental_runtime_is_unavailable -q
```

Expected: FAIL，现有执行器没有 `_probe_claim_enabled`。

- [ ] **Step 3: 实现不可变能力注入和显式跳过日志**

在模块顶部增加标准日志与策略导入：

```python
import logging

from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    build_current_supplemental_evidence_policy,
)

logger = logging.getLogger(__name__)
```

扩展执行器构造函数并保存只读布尔快照：

```python
def __init__(
    self,
    *,
    worker_instance_id: str,
    probe_claim_enabled: bool = False,
    now: Callable[[], datetime] | None = None,
    retry_wait_seconds: float = _DEFAULT_RETRY_WAIT_SECONDS,
    max_retry_reclaims: int = _DEFAULT_MAX_RETRY_RECLAIMS,
) -> None:
    if not isinstance(probe_claim_enabled, bool):
        raise TypeError("probe_claim_enabled 必须为布尔值")
    # 保留现有 worker_instance_id 与重试配置校验。
    self._probe_claim_enabled = probe_claim_enabled
```

在 Exploration 循环结束、Probe 循环开始前增加：

```python
if not self._probe_claim_enabled:
    logger.info("补采能力不可用，H5 主探索结束后跳过 Probe Work 领取")
    return outcome, consumed
```

在 `build_h5_workflow_handler` 中仅对默认生产执行器派生能力，调用方显式注入 `executor` 时保持原合同：

```python
if executor is None:
    supplemental_policy = build_current_supplemental_evidence_policy(
        enabled=settings.supplemental_evidence.enabled,
    )
    probe_claim_enabled = supplemental_policy.decide(
        SupplementalEvidenceOperation.CLAIM_PROBE
    ).allowed
    executor = ProductionH5V2RuntimeExecutor(
        worker_instance_id=worker_instance_id,
        probe_claim_enabled=probe_claim_enabled,
    )
```

并让返回值使用 `executor=executor`。不要在此处捕获 `SupplementalEvidenceDisabledError`，也不要改变策略的 `runtime_ready`。

- [ ] **Step 4: 运行目标文件全部测试**

Run:

```powershell
docker exec alioth-api pytest services/api/tests/test_h5_runtime_composition.py -q
```

Expected: PASS。

- [ ] **Step 5: 运行补采策略与 H5 入口关联回归**

Run:

```powershell
docker exec alioth-api pytest services/api/tests/test_config_env_loading.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py -q
```

Expected: PASS；补采默认关闭、配置开启但运行时未就绪、底层 Probe 门禁失败关闭的测试均保持通过。

- [ ] **Step 6: 提交实现和测试**

```powershell
git add services/api/app/application/h5_runtime_composition.py services/api/tests/test_h5_runtime_composition.py
git commit -m "[AI编程] fix: H5补采关闭时跳过Probe领取" -m "涉及模块：H5 V2生产组合根与运行循环；数据库迁移：无；前端菜单/路由同步：无需；未覆盖风险：真实第三方站点、LLM与Playwright长链需容器联合验收。"
```

---

### Task 3: 固化专项回归规则并完成无构建联合验收

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`（文件末尾新增独立章节）
- Runtime-only: `.alioth-h5-v2.override.yml`（既有本地覆盖文件，只用于 Compose，不提交）

**Interfaces:**
- Consumes: Task 2 已通过测试的 H5 执行器能力边界。
- Produces: 可复用回归规则、真实任务 ID/Run ID、Worker 终态、探索页面清单和网站质检结果。

- [ ] **Step 1: 增加专项回归规则**

在 `docs/专项能力与高频回归规则.md` 末尾追加：

```markdown
## 2026-08-14：H5 主探索与 Probe 补采必须按能力边界解耦

- **主探索不依赖补采**：补采总开关关闭，或已配置但生产运行时未就绪时，H5 V2 在主 Exploration Work 收敛后必须直接结束消费，不得尝试领取 Probe Work，也不得因此把已完成的主探索降级为技术失败。
- **组合根单向注入**：生产组合根只能从不可变补采策略的 `CLAIM_PROBE` 决策派生执行器能力；禁止通过捕获门禁异常、仓储返回空结果或临时打开配置绕过策略。Repository、Turn Runner、执行与证据挂接层继续对补采失败关闭。
- **兼容与预算**：能力允许时保持 Exploration 后排空 Probe 的既有顺序，且 Exploration 与 Probe 必须共享任务锁定的总动作预算；能力不可用时不产生 Probe claim、publish、capture 或 attach 副作用。
- **回归验收**：至少覆盖补采关闭跳过、配置开启但运行时未就绪跳过、能力允许时 Probe 行为保留、共享动作预算，以及真实 PostgreSQL/Redis/Playwright Worker 在不启用补采条件下完成 H5 主探索。真实联合验收必须列出访问页面、动作、质检终态及残余外部依赖故障。
```

- [ ] **Step 2: 运行格式和差异检查**

Run:

```powershell
git diff --check
git diff -- services/api/app/application/h5_runtime_composition.py services/api/tests/test_h5_runtime_composition.py docs/专项能力与高频回归规则.md
```

Expected: `git diff --check` 退出码为 0；差异中没有凭据、Token、补采开关开启或无关文件。

- [ ] **Step 3: 提交专项规则**

```powershell
git add docs/专项能力与高频回归规则.md
git commit -m "[AI编程] doc: 固化H5主探索与Probe解耦回归规则" -m "涉及模块：H5专项回归文档；数据库迁移：无；前端菜单/路由同步：无需；未覆盖风险：真实补采闭环仍未就绪且本次不启用。"
```

- [ ] **Step 4: 无构建重启 API 和 Worker 并验证运行配置**

Run:

```powershell
docker compose -f infra/docker-compose.yml -f .alioth-h5-v2.override.yml up -d --no-build --no-deps --force-recreate alioth-api alioth-worker
docker inspect alioth-api --format '{{range .Config.Env}}{{println .}}{{end}}' | Select-String '^ALIOTH_H5_V2_MODE=on$|^ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED='
docker inspect alioth-worker --format '{{range .Config.Env}}{{println .}}{{end}}' | Select-String '^ALIOTH_H5_V2_MODE=on$|^ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED='
```

Expected: 两个容器均显示 `ALIOTH_H5_V2_MODE=on`；补采变量缺失或为 false，绝不能为 true。

- [ ] **Step 5: 自动登录并提交新的完整质检任务**

使用 UTF-8 读取本地测试用户迁移，从既有种子中解析账号与测试明文密码；脚本不得输出 `$login`、`$token` 或 Authorization Header：

```powershell
$ErrorActionPreference = 'Stop'
$seed = Get-Content -LiteralPath 'services/api/migrations/versions/20260709_0002_add_qc_user.py' -Encoding UTF8
$credentialLine = $seed | Where-Object { $_ -match '^\s*\(' -and $_ -match "admin" } | Select-Object -First 1
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

Expected: HTTP 2xx，并只输出新 `task_id`、`run_id` 和初始状态。

- [ ] **Step 6: 观察 Worker 直至终态并核对没有 Probe 门禁技术失败**

Run:

```powershell
docker logs --since 10m alioth-worker 2>&1 | Select-String $created.task_id,'跳过 Probe Work 领取','SUPPLEMENTAL_EVIDENCE','technical_failed','completed'
$detail = Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:8000/api/v2/tasks/{0}" -f $created.task_id) -Headers $headers
$detail | ConvertTo-Json -Depth 12
```

每 10 秒重复只读状态查询，最长等待 15 分钟。Expected: Worker 出现跳过 Probe 的中文日志；不得再因 `CLAIM_PROBE` / `ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED` 进入 `technical_failed`。若出现新的独立代码缺陷，先保存脱敏日志证据，再按 `superpowers:systematic-debugging` 和 `superpowers:test-driven-development` 建立失败测试、最小修复、重启并重新创建任务；不得复用失败 Run，也不得开启补采。

- [ ] **Step 7: 提取质检结果与真实探索页面清单**

先从任务详情和报告 API 提取可用结果，再用数据库只读查询交叉核对 Capture/Frontier，不把 `about:blank` 当成业务页面：

```powershell
$report = Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:8000/api/v2/tasks/{0}/report" -f $created.task_id) -Headers $headers
$report | ConvertTo-Json -Depth 20
docker exec alioth-postgres psql -U alioth -d alioth -c "SELECT DISTINCT location, page_type, created_at FROM h5_frontier_snapshots WHERE task_id = '$($created.task_id)' ORDER BY created_at;"
docker exec alioth-postgres psql -U alioth -d alioth -c "SELECT url, purpose, created_at FROM captures WHERE task_id = '$($created.task_id)' ORDER BY created_at;"
```

如果实际表/列名与迁移后的 schema 不一致，先用 `\dt`、`\d <table>` 和仓储 SQL 做只读确认后调整查询，禁止修改数据。最终交付必须逐项列出：任务与 Run 标识、终态、质量状态、规则通过/失败/证据不足数量、每个访问 URL、页面类型/目的、执行动作、外部 Page Detection 500 等残余依赖故障；不得把候选但未执行的页面当成已探索页面。

- [ ] **Step 8: 最终验证工作区与提交边界**

Run:

```powershell
docker exec alioth-api pytest services/api/tests/test_config_env_loading.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py -q
git status --short --branch
git log -3 --oneline
```

Expected: 目标测试全部 PASS；分支仍为 `dev_1.5`；只包含本计划的两个实现/文档提交和此前已确认的设计提交；`.alioth-h5-v2.override.yml` 仍为本地未跟踪运行文件且不含凭据。

