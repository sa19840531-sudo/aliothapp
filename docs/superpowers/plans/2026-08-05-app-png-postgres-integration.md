# App PNG 截图与真实 PostgreSQL 集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 App Workflow 中接入可替换的 Official App Adapter，把星语返回的 PNG 二进制安全落入对象存储和 PostgreSQL，并让双 Worker、租约过期、重试和 APP_READY 交接都保持单一事实源。

**Architecture:** App Worker 先在 PostgreSQL 创建/复用 Android Starting Session 并获取 Bootstrap Lease，随后才允许任何 Adapter/Driver 调用；租约由独立续期器维护，所有外部调用前后都检查租约。Official Adapter 只负责把 API 响应映射为 `AppBootstrapPort + SurfaceDriver`，PNG 交给现有 `AppCapturePersistenceService` 写对象存储，PostgreSQL 事务只保存 Capture、Artifact、App Capture 和 Bootstrap 引用。生产 Worker 通过 `AppWorkflowDispatcher -> RuntimeBackedAppWorkflowHandler -> AppV2Runtime` 进入 App 分支，任何未配置的 Official Adapter 都 fail-closed，不回退 H5 Main Agent。

**Tech Stack:** Python 3.12、FastAPI、`asyncpg`/PostgreSQL、Alembic、`httpx`、Redis 唤醒、现有 MinIO/本地对象存储、pytest、Ruff。

## Global Constraints

- PNG 原文只进入现有对象存储；`qc_capture`、`qc_evidence_artifact`、`qc_app_capture`、`qc_app_visual_observation` 只保存元数据和引用，不新增 PostgreSQL `bytea` 列。
- App Capture 只允许 `screenshot` 和 `action_trace`；不得产生 DOM、HTML、UI Tree、Visible Text 或 OCR 字段。
- `qc_page_capture` 继续作为 Web/H5 Capture 事实表，本计划不修改其字段语义或写入路径。
- `AppReadyHandoff`、Redis、Agent Working Memory 只携带 `CaptureRef`、artifact id 和元数据，禁止携带 PNG、APK、Cookie、Token、密码或 Driver 句柄。
- 同一 `AppV2Runtime` 实例同时提供 `AppBootstrapPort` 和 `SurfaceDriver`；Explorer、Rule Judge、Agent 不能导入或直接调用 Driver。
- `qc_task` 继续使用现有统一终态；不新增 App 专用任务终态或第二套规则执行状态。
- App 任务必须锁定 `workflow_version=v2`；`ALIOTH_APP_RUNTIME_PROVIDER=fake` 在 production 环境拒绝启动，`official` 未完成配置时 Run 明确失败。
- 真实 PostgreSQL 测试只连接显式设置的隔离库 `ALIOTH_TEST_DATABASE_URL`，缺少该变量时跳过，禁止连接开发或生产库。
- 所有新增注释使用中文，文件使用 UTF-8；提交说明以 `[AI编程]` 开头，并注明数据库迁移、前端同步和测试风险。

---

### Task 1: 冻结 App Bootstrap Lease 与稳定 Session 身份

**Files:**
- Modify: `services/api/app/application/app_bootstrap_state.py`
- Modify: `services/api/app/domain/interaction/surface_driver.py`
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/tests/test_app_qc_settings.py`
- Create: `services/api/tests/test_app_bootstrap_lease_contracts.py`

**Interfaces:**
- Produces `RenewAppBootstrapLeaseCommand` 和 `AppBootstrapRepository.renew(command: RenewAppBootstrapLeaseCommand) -> AppBootstrapSnapshot`。
- Produces `app_primary_surface_ref(session_id: str) -> str`，返回稳定的 `app-surface:<session_id>`，作为 PostgreSQL Surface Ref 和 Adapter 对外逻辑身份；外部云设备句柄不得从该函数泄漏。
- `WorkflowSettings` 增加 `official_app_base_url: str | None`、`official_app_timeout_seconds: float`、`official_app_max_screenshot_bytes: int`，默认值分别为 `None`、`60.0`、`20 * 1024 * 1024`；路径模板通过环境变量读取，不把未确认的星语路径写死在领域层。

- [ ] **Step 1: 写租约命令和稳定身份的失败测试**

```python
def test_renew_command_rejects_non_positive_seconds() -> None:
    with pytest.raises(ValueError, match="lease_seconds"):
        RenewAppBootstrapLeaseCommand(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="app-build:1",
            lease_owner="worker-a",
            lease_token="00000000-0000-0000-0000-000000000001",
            lease_seconds=0,
        )


def test_app_primary_surface_ref_is_stable_and_non_secret() -> None:
    value = app_primary_surface_ref("session-1")
    assert value == "app-surface:session-1"
    assert "token" not in value and "password" not in value
```

- [ ] **Step 2: 运行契约测试确认当前实现失败**

Run: `python -m pytest services/api/tests/test_app_bootstrap_lease_contracts.py services/api/tests/test_app_qc_settings.py -q`

Expected: FAIL，原因是续租命令、稳定 Surface Ref 和 Official 配置字段尚未存在。

- [ ] **Step 3: 实现最小契约**

在 `app_bootstrap_state.py` 中新增不可变命令，复用现有 `_validate_identity` 和 `_require_positive`。在 `AppBootstrapRepository` Protocol 中加入：

```python
async def renew(
    self,
    command: RenewAppBootstrapLeaseCommand,
) -> AppBootstrapSnapshot: ...
```

在 `surface_driver.py` 中新增 `app_primary_surface_ref`，仅做输入校验和确定性拼接。配置读取必须校验 URL 非空时去除首尾空格、超时大于 0、最大截图字节数大于 0；不合法配置在 `get_settings()` 阶段抛出 `ValueError`。

- [ ] **Step 4: 运行契约测试确认通过**

Run: `python -m pytest services/api/tests/test_app_bootstrap_lease_contracts.py services/api/tests/test_app_qc_settings.py -q`

Expected: PASS。

- [ ] **Step 5: 提交契约变更**

```powershell
git add services/api/app/application/app_bootstrap_state.py services/api/app/domain/interaction/surface_driver.py services/api/app/core/config.py services/api/tests/test_app_bootstrap_lease_contracts.py services/api/tests/test_app_qc_settings.py
git commit -m "[AI编程] feat: 冻结 App Bootstrap 租约契约" -m "涉及模块：App Workflow 配置与租约命令；数据库迁移：无；前端菜单/路由：无需同步；测试风险：尚未覆盖真实 PostgreSQL 续租竞态。"
```

### Task 2: 在首次外部调用前获取租约并实现独立续期

**Files:**
- Create: `services/api/app/application/app_bootstrap_lease.py`
- Modify: `services/api/app/infrastructure/db/app_bootstrap_repository.py`
- Modify: `services/api/app/application/app_workflow.py`
- Modify: `services/api/app/infrastructure/app_runtime/fake.py`
- Modify: `services/api/tests/test_app_qc_workflow.py`
- Modify: `services/api/tests/test_app_bootstrap_repository.py`
- Create: `services/api/tests/test_app_bootstrap_lease_guard.py`

**Interfaces:**
- `AppBootstrapLeaseGuard(repository, command, renew_interval_seconds)`: `start()`, `assert_healthy()`, `stop()`，以及异步上下文管理器 `__aenter__/__aexit__`；续租失败保存原始异常，下一次 `assert_healthy()` 必须抛出 `AppBootstrapLeaseLostError`。
- `PostgresAppBootstrapRepository.renew()` 只在 owner、token、非终态且现有 expiry 大于 `now()` 时更新 `workflow_lease_expires_at`，不递增 `row_version`，从而不破坏当前 Workflow 的乐观版本。
- `AppQcWorkflow.run()` 的外部调用顺序固定为：`register_starting(数据库)` → `get_or_create(数据库租约)` → `describe_capabilities` → `start_session` → `ensure_installed/open_app/capture`；每次 Driver/API 调用前后调用 `assert_healthy()`。

- [ ] **Step 1: 写调用顺序、续租和旧 Worker 拒绝测试**

测试 Fake Repository/Driver 的事件列表，断言第一项外部事件之前已经出现 `starting_registered` 和 `bootstrap_claimed`；续租器能定期调用 `renew`；模拟 `renew` 抛出异常后，Driver 调用之后的 Capture 写入被拒绝。另加两个 Worker 使用不同 owner/token 的测试，旧 token 的 `transition/fail/cancel` 必须抛出 `AppBootstrapPersistenceError`。

- [ ] **Step 2: 运行测试确认当前顺序失败**

Run: `python -m pytest services/api/tests/test_app_qc_workflow.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_app_bootstrap_lease_guard.py -q`

Expected: FAIL，现有 `AppQcWorkflow` 在 `get_or_create` 前调用 `describe_capabilities/start_session`，Repository 也没有 `renew()`。

- [ ] **Step 3: 实现 PostgreSQL 续租和 Lease Guard**

在 `app_bootstrap_repository.py` 中使用带 owner/token/expiry 条件的单条 `UPDATE ... RETURNING`；更新失败统一抛出 `AppBootstrapPersistenceError("App Bootstrap Workflow 租约已失效")`。Lease Guard 使用 `asyncio.create_task` 按 `max(1, renew_interval_seconds)` 周期续租，`stop()` 取消任务并等待完成，不吞掉续租异常。

在 `AppQcWorkflow` 中先构造 `SurfaceSessionSpec`，使用 `app_primary_surface_ref(command.session_id)` 调用现有 `register_starting`，随后调用 `get_or_create`。只有成功拿到 Bootstrap Snapshot 后才创建 Lease Guard、校验 capability 和调用 Driver。Driver 返回的 `SurfaceSessionRef.primary_surface_ref` 必须等于稳定逻辑引用，否则抛出 `SESSION_IDENTITY_MISMATCH`。终态 Bootstrap 直接从已持久化 Capture 恢复，不再触发任何外部调用；租约丢失后不得继续调用 `close_session` 或写入 Capture。

- [ ] **Step 4: 运行单元测试、Ruff 和架构边界测试**

Run: `python -m pytest services/api/tests/test_app_qc_workflow.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_app_bootstrap_lease_guard.py services/api/tests/test_app_architecture_boundaries.py -q`
Run: `ruff check services/api/app/application/app_bootstrap_lease.py services/api/app/application/app_workflow.py services/api/app/infrastructure/db/app_bootstrap_repository.py`

Expected: PASS；任何 `Driver` 调用前都能在事件断言中观察到有效租约。

- [ ] **Step 5: 提交租约顺序修复**

```powershell
git add services/api/app/application/app_bootstrap_lease.py services/api/app/infrastructure/db/app_bootstrap_repository.py services/api/app/application/app_workflow.py services/api/app/infrastructure/app_runtime/fake.py services/api/tests/test_app_qc_workflow.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_app_bootstrap_lease_guard.py
git commit -m "[AI编程] fix: 将 App Bootstrap 租约提前到外部调用前" -m "涉及模块：App Workflow、Bootstrap Repository、Fake Runtime；数据库迁移：无；前端菜单/路由：无需同步；测试风险：外部云设备调用无法在单元测试中真实中断。"
```

### Task 3: 实现 PNG-only Official App Adapter

**Files:**
- Create: `services/api/app/infrastructure/app_runtime/official.py`
- Create: `services/api/app/infrastructure/app_runtime/official_api.py`
- Modify: `services/api/app/infrastructure/app_runtime/factory.py`
- Modify: `services/api/app/core/config.py`
- Create: `services/api/tests/test_official_app_runtime.py`
- Create: `services/api/tests/test_official_app_api_contract.py`

**Interfaces:**
- `OfficialAppApiClient` Protocol：
  - `start_session(spec: SurfaceSessionSpec) -> OfficialSessionResponse`
  - `install(request: AppInstallRequest) -> OfficialInstallResponse`
  - `open(request: AppOpenRequest) -> OfficialOpenResponse`
  - `capture_png(session: SurfaceSessionRef, profile: CaptureProfile) -> OfficialCaptureResponse`
  - `execute(session: SurfaceSessionRef, intent: ActionIntent, action_execution_id: str) -> OfficialActionResponse`
  - `close(session: SurfaceSessionRef, reason: str) -> None`
- `OfficialCaptureResponse` 必须包含 `content_type: str`、`png_bytes: bytes`、`width: int`、`height: int`、`orientation: str`、`package_name: str | None`、`activity: str | None`、`declared_sha256: str | None`；响应对象不得包含 DOM/UI Tree/OCR 字段。
- `OfficialAppRuntime` 同时实现 `AppBootstrapPort` 和 `SurfaceDriver`；`capture()` 只返回 `RawArtifactSet(artifacts=(RawArtifact("screenshot", png_bytes),), metadata=...)`，`execute()` 只接受 `ActionIntent`。

- [ ] **Step 1: 写响应映射和 PNG 安全校验失败测试**

使用 Fake `OfficialAppApiClient` 覆盖：合法 `image/png` 和 PNG 文件头可生成 `RawArtifactSet`；错误 MIME、错误 PNG 文件头、空数据、宽高非正数、超过配置上限、声明 SHA-256 与本地计算值不一致都抛出结构化 `CAPTURE_INVALID`；安装/打开结果保留 request id 和 retryable；响应中添加未知字段不进入 Runtime metadata。

- [ ] **Step 2: 运行测试确认 Adapter 尚未注册**

Run: `python -m pytest services/api/tests/test_official_app_runtime.py services/api/tests/test_official_app_api_contract.py -q`

Expected: FAIL，原因是 Official API Protocol、Runtime 和 Factory provider 尚未存在。

- [ ] **Step 3: 实现 API seam、Runtime 映射和 Factory 注册**

`official_api.py` 提供 `OfficialAppApiClient` 以及基于 `httpx.AsyncClient` 的实现：base URL、超时、认证头和各操作 path 均来自 `WorkflowSettings`，请求 id 使用内部幂等键，响应体 Capture 必须按二进制读取并限制最大字节数。`official.py` 只做协议映射、身份校验和 PNG 校验，不写 PostgreSQL、不写 Redis、不创建 Agent；外部设备句柄保存在 API client 私有对象中，返回给上层的 `SurfaceSessionRef` 永远使用 `app_primary_surface_ref(session_id)`。

将 `AppRuntimeFactory.build("official")` 改为要求显式注入 `OfficialAppApiClient`；缺少 base URL、client 或认证配置时抛出 `AppRuntimeConfigurationError`，不得退回 Fake/H5。Fake Runtime 的 Session Ref 同步改为稳定逻辑引用。

- [ ] **Step 4: 运行 Adapter 契约、边界和 Ruff 测试**

Run: `python -m pytest services/api/tests/test_official_app_runtime.py services/api/tests/test_official_app_api_contract.py services/api/tests/test_surface_capability_manifest.py services/api/tests/test_app_architecture_boundaries.py -q`
Run: `ruff check services/api/app/infrastructure/app_runtime/official.py services/api/app/infrastructure/app_runtime/official_api.py services/api/app/infrastructure/app_runtime/factory.py`

Expected: PASS；Capability Manifest 只声明 screenshot/action_trace 和已实现 ActionIntent，不声明 DOM/CSS/XPath/Appium。

- [ ] **Step 5: 提交 Adapter**

```powershell
git add services/api/app/infrastructure/app_runtime/official.py services/api/app/infrastructure/app_runtime/official_api.py services/api/app/infrastructure/app_runtime/factory.py services/api/app/core/config.py services/api/tests/test_official_app_runtime.py services/api/tests/test_official_app_api_contract.py services/api/tests/test_surface_capability_manifest.py
git commit -m "[AI编程] feat: 增加 PNG-only Official App Adapter" -m "涉及模块：App Runtime Adapter、星语 API client、Capability Manifest；数据库迁移：无；前端菜单/路由：无需同步；测试风险：真实星语地址和凭证仍由部署环境注入。"
```

### Task 4: 固化 PNG 对象存储与 PostgreSQL Capture 原子写入

**Files:**
- Modify: `services/api/app/application/app_capture_persistence.py`
- Modify: `services/api/app/infrastructure/db/app_capture_repository.py`
- Modify: `services/api/app/infrastructure/db/app_action_capture_repository.py`
- Modify: `services/api/app/infrastructure/storage/local_evidence_store.py`
- Modify: `services/api/app/infrastructure/storage/minio_evidence_store.py`
- Modify: `services/api/tests/test_app_capture_persistence.py`
- Modify: `services/api/tests/test_app_capture_repository.py`
- Create: `services/api/tests/test_app_png_storage_contract.py`

**Interfaces:**
- `AppCapturePersistenceService.persist_bootstrap()` 和 `persist_action_capture()` 继续接收 `RawArtifactSet`，返回现有 `PersistedAppCapture`；服务层是唯一允许把 PNG 原文传给 `EvidenceArtifactStore` 的位置。
- `PostgresAppCaptureRepository.save_bootstrap_capture()` 在一个事务中写 `qc_capture`、`qc_evidence_artifact`、`qc_app_capture`，并以 owner/token/row-version 条件更新 `qc_app_bootstrap.bootstrap_capture_id`；重复 capture 返回已存在记录，不重复插入。
- 对象存储失败不写数据库；数据库事务失败必须删除本次新对象并保留结构化清理日志；清理失败不能把 Run 标记为成功。

- [ ] **Step 1: 写 PNG 二进制、补偿和幂等测试**

测试断言：对象存储收到的 bytes 与 API 返回完全一致；`sha256`、文件大小、宽高、orientation、image signature 写入元数据；数据库失败后对象被删除；同一 `session_id + capture role + image hash` 重放只得到一条 Capture、Artifact 和 App Capture；metadata 中不存在 `dom/html/ui_tree/ocr/visible_text`。

- [ ] **Step 2: 运行现有 App Capture 测试确认失败点**

Run: `python -m pytest services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py services/api/tests/test_app_png_storage_contract.py -q`

Expected: 新增 PNG contract 测试在缺少严格 MIME/签名、Bootstrap lease 条件或幂等断言时失败。

- [ ] **Step 3: 实现并复用现有持久化链**

保留 `EvidenceArtifactStore` 的 local/MinIO 工厂，不新增数据库二进制列。将 PNG 校验集中到 `AppCapturePersistenceService`，Repository 只接收已验证的 `PersistedEvidenceArtifact`；Bootstrap 和 action before/after Capture 使用同一事务模型和相同 artifact checksum 规则。所有 SQL 的 owner/token/row-version 条件失败统一转换为可识别的 stale lease 异常。

- [ ] **Step 4: 运行 App/Web Capture 回归**

Run: `python -m pytest services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py services/api/tests/test_app_png_storage_contract.py services/api/tests/test_page_capture_persistence.py -q`
Run: `ruff check services/api/app/application/app_capture_persistence.py services/api/app/infrastructure/db/app_capture_repository.py services/api/app/infrastructure/db/app_action_capture_repository.py`

Expected: PASS；Web/H5 `qc_page_capture` 测试结果不变。

- [ ] **Step 5: 提交持久化实现**

```powershell
git add services/api/app/application/app_capture_persistence.py services/api/app/infrastructure/db/app_capture_repository.py services/api/app/infrastructure/db/app_action_capture_repository.py services/api/app/infrastructure/storage services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py services/api/tests/test_app_png_storage_contract.py
git commit -m "[AI编程] feat: 固化 App PNG 对象存储与 Capture 事务" -m "涉及模块：App Capture、Evidence Artifact、对象存储；数据库迁移：无，复用现有 0034-0036；前端菜单/路由：无需同步；测试风险：对象存储与 PostgreSQL 不是单一物理事务，依赖补偿清理。"
```

### Task 5: 将生产 Worker 接入 AppWorkflowRouter -> AppV2Runtime

**Files:**
- Create: `services/api/app/application/app_runtime_composition.py`
- Modify: `services/api/app/application/app_workflow_dispatch.py`
- Modify: `services/api/app/worker.py`
- Create: `services/api/tests/test_app_runtime_composition.py`
- Modify: `services/api/tests/test_app_workflow_dispatch.py`
- Modify: `services/api/tests/test_worker_app_route.py`

**Interfaces:**
- `build_app_workflow_handler(settings: Settings, worker_instance_id: str, official_client: OfficialAppApiClient | None = None) -> AppWorkflowHandler`：生产组合根唯一创建 `AppV2Runtime` 依赖，不向 Explorer/Rule Judge 暴露 Driver。
- `PostgresAppRuntimeProvider.build(record, settings) -> AppV2Runtime`：从 `_task_target.app` 读取 `platform_build_ref`、device profile，使用 `AppRuntimeFactory`、现有 PostgreSQL repositories、现有 object store 和 Explorer queue 组装 `AppV2RuntimeDependencies`。
- `ProductionAppV2RuntimeExecutor.run(runtime, *, record, settings) -> RunResult`：构造 `RunAppWorkflowCommand`，执行 `bootstrap`，将 APP_READY Capture 交给现有 Screenshot Understanding/Exploration Work 发布链，再按 `run_one_turn(task_id, agent_run_id)` 消费到 Explorer terminal；返回现有 `RunResult`，不创建 Main Agent。

- [ ] **Step 1: 写生产路由和组合根失败测试**

构造 App `AgentRunRecord`，注入 Fake provider，断言 `AppWorkflowDispatcher.dispatch()` 调用的是 `AppV2RuntimeExecutor`；用 monkeypatch 让 `build_default_main_agent` 抛出异常，App Run 仍不调用该函数。构造 H5 record，断言既有 Main Agent 路径不变。测试 `main()` 创建每个 `_worker_loop` 时都传入同一个非空 App handler。

- [ ] **Step 2: 运行路由测试确认当前生产入口未接 Handler**

Run: `python -m pytest services/api/tests/test_app_runtime_composition.py services/api/tests/test_app_workflow_dispatch.py services/api/tests/test_worker_app_route.py -q`

Expected: FAIL，现有 `main()` 启动 `_worker_loop` 时没有传入 `app_workflow_handler`。

- [ ] **Step 3: 实现组合根、Executor 和 main 注入**

组合根按 `settings.workflow.app_runtime_provider` 选择 Fake/Official；生产环境 provider 为 Fake 时在启动阶段失败。Executor 使用 `session_id=f"app:{record.task_id}"` 的确定性身份、`record.run_id` 作为 `agent_run_id`、`worker_instance_id` 作为 lease owner、每次 Run 新生成 UUID lease token；任务取消交给现有 `AppWorkflowCancellation`。Bootstrap 失败映射为现有 `RunResult` 的 `INFRASTRUCTURE` 错误，不能写成 completed。

在 `worker.main()` 中构造一次 handler，并将其显式传给所有 `_worker_loop`；`_process_app_one()` 只调用 Dispatcher，不允许添加通用 Main Agent fallback。保留现有 Redis Run 状态和统一终态写回。

- [ ] **Step 4: 运行组合、路由和全量 App 单元测试**

Run: `python -m pytest services/api/tests/test_app_runtime_composition.py services/api/tests/test_app_workflow_dispatch.py services/api/tests/test_worker_app_route.py services/api/tests/test_app_qc_workflow.py services/api/tests/test_app_screenshot_exploration_cycle.py -q`
Run: `ruff check services/api/app/application/app_runtime_composition.py services/api/app/application/app_workflow_dispatch.py services/api/app/worker.py`

Expected: PASS；代码搜索不能发现 App 分支调用 `build_default_main_agent` 或 `run_main_agent`。

- [ ] **Step 5: 提交生产入口**

```powershell
git add services/api/app/application/app_runtime_composition.py services/api/app/application/app_workflow_dispatch.py services/api/app/worker.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_app_workflow_dispatch.py services/api/tests/test_worker_app_route.py
git commit -m "[AI编程] feat: 将生产 App Worker 接入 AppV2Runtime" -m "涉及模块：Worker、App Workflow Dispatcher、Runtime 组合根；数据库迁移：无；前端菜单/路由：无需同步；测试风险：真实星语调用仍需独立联调环境。"
```

### Task 6: 增加真实 PostgreSQL 单 Worker、双 Worker 与租约过期集成测试

**Files:**
- Create: `services/api/tests/test_app_postgres_integration.py`
- Modify: `services/api/tests/test_app_capture_repository.py`
- Modify: `services/api/tests/test_app_bootstrap_repository.py`
- Modify: `services/api/migrations/README.md`

**Interfaces:**
- 测试入口复用现有 `ALIOTH_TEST_DATABASE_URL` 跳过约定；测试开始前执行 `alembic upgrade head`，测试结束删除本次生成的 `qc_task`，级联清理 Session、Bootstrap、Capture 和 Observation。
- 测试对象存储使用 `tmp_path` 下的 `LocalEvidenceArtifactStore`，查询 PostgreSQL 的 Artifact `storage_path/checksum/size_bytes`，从对象文件读取 PNG 原文比对。
- 所有并发断言以 PostgreSQL 返回结果为准，不读取 Redis 判断完成。

- [ ] **Step 1: 写真实数据库测试和确定性种子函数**

在测试文件内定义 `_seed_app_task(connection, ids)`、`_seed_verified_build(connection, ids)`、`_starting_spec(ids)` 和 `_valid_png()`；至少覆盖：

```python
async def test_postgres_app_bootstrap_capture_persists_png_atomically() -> None: ...
async def test_postgres_two_workers_only_one_bootstrap_lease_and_capture() -> None: ...
async def test_postgres_expired_lease_rejects_old_worker_and_allows_new_worker() -> None: ...
async def test_postgres_replayed_capture_and_visual_observation_are_idempotent() -> None: ...
```

断言单 Worker 只产生一条 `qc_capture`、一条 `qc_evidence_artifact`、一条 `qc_app_capture`；查询不到任何 PNG `bytea` 列；双 Worker 只有一个 `get_or_create` 成功，另一个收到 `AppBootstrapPersistenceError`；旧 token 的 transition、Capture commit 和 APP_READY 更新均失败；新 token 完成后旧 Worker 不能覆盖其 `bootstrap_capture_id`。

- [ ] **Step 2: 先在无数据库环境验证测试会安全跳过**

Run: `python -m pytest services/api/tests/test_app_postgres_integration.py -q`

Expected: 未设置 `ALIOTH_TEST_DATABASE_URL` 时全部测试显示 skipped，不得尝试连接默认数据库。

- [ ] **Step 3: 实现数据库集成种子、事务断言和补偿清理**

复用现有迁移产生的字段和 Repository，不新增测试专用 schema。双 Worker 用 `asyncio.gather` 同时调用不同 owner/token；租约过期测试用 SQL 将 `workflow_lease_expires_at` 设置为 `now() - interval '1 second'`，避免固定 sleep 造成抖动。对象存储目录在测试结束后断言没有未引用 PNG 文件，若有则记录 orphan 指标并清理。

- [ ] **Step 4: 在隔离 PostgreSQL 上运行真实集成**

Run: `$env:ALIOTH_TEST_DATABASE_URL='postgresql://.../alioth_test'; python -m alembic -c services/api/alembic.ini upgrade head; python -m pytest services/api/tests/test_app_postgres_integration.py -q`

Expected: 全部 PASS；若数据库不可用，测试失败而不是静默通过。

- [ ] **Step 5: 提交真实 PostgreSQL 测试**

```powershell
git add services/api/tests/test_app_postgres_integration.py services/api/tests/test_app_capture_repository.py services/api/tests/test_app_bootstrap_repository.py services/api/migrations/README.md
git commit -m "[AI编程] test: 增加 App PNG 真实 PostgreSQL 双 Worker 验收" -m "涉及模块：App Bootstrap、Capture、Visual Observation 集成测试；数据库迁移：仅验证现有 0034-0037，无新增迁移；前端菜单/路由：无需同步；测试风险：需要隔离 PostgreSQL 和本地对象存储。"
```

### Task 7: 星语接口联调门禁、观测指标与最终验收

**Files:**
- Create: `services/api/tests/test_official_app_star_contract.py`
- Modify: `services/api/app/infrastructure/app_runtime/official_api.py`
- Modify: `services/api/app/infrastructure/app_runtime/official.py`
- Modify: `docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md`
- Modify: `docs/superpowers/specs/2026-08-05-app-png-postgres-integration-design.md`

**Interfaces:**
- 星语联调测试只在 `ALIOTH_OFFICIAL_APP_API_BASE_URL`、认证变量和 `ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED=true` 同时存在时运行；凭证不入库、不写测试文件、不进入日志。
- Adapter 日志只记录 `task_id/session_id/request_id/capture_id/artifact_id/checksum/size`，不得记录 PNG、APK URL、Token 或设备句柄；对象补偿失败记录 `app_capture_orphan_total`，租约丢失记录 `app_bootstrap_lease_lost_total`。
- 部署文档增加 Official provider、PNG 上限、隔离 PostgreSQL 测试变量和对象存储清理策略；明确不需要前端菜单/路由变更。

- [ ] **Step 1: 写星语契约测试**

用 `httpx.MockTransport` 固定响应安装成功、打开成功和 `Content-Type: image/png` 的真实 PNG bytes；断言请求携带内部幂等键、响应映射为 `RawArtifactSet`，并断言错误 MIME/错误签名/超限响应被拒绝。真实联调测试只验证外部 API 合同，不写 PostgreSQL。

- [ ] **Step 2: 运行 Mock 契约测试确认当前缺少门禁**

Run: `python -m pytest services/api/tests/test_official_app_star_contract.py -q`

Expected: FAIL，原因是 HTTP client 的 path/认证/PNG 响应门禁和可观测字段尚未固定。

- [ ] **Step 3: 实现联调门禁和脱敏日志**

将请求 path、认证头名、超时和最大响应字节数由配置注入；HTTP client 使用流式读取并在超过上限时立即关闭响应。日志采用结构化字段白名单；异常消息只保留错误码和 request id。更新部署文档和设计文档中的验收命令。

- [ ] **Step 4: 执行最终验证**

Run: `python -m pytest services/api/tests -q`
Run: `ruff check services/api/app services/api/tests`
Run: `git diff --check`
Run: `python -m alembic -c services/api/alembic.ini heads`

Expected: 现有 H5、Evidence、Inspection、Rule Judge、Journey 回归全部通过；App 单元测试通过；真实 PostgreSQL 在设置变量时通过；Alembic 只有单一 head；代码中不存在 App -> H5 Main Agent 的隐式 fallback。

- [ ] **Step 5: 提交验收门禁**

```powershell
git add services/api/tests/test_official_app_star_contract.py services/api/app/infrastructure/app_runtime/official_api.py services/api/app/infrastructure/app_runtime/official.py docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md docs/superpowers/specs/2026-08-05-app-png-postgres-integration-design.md
git commit -m "[AI编程] chore: 完成 App Official 联调与验收门禁" -m "涉及模块：星语 API 联调、脱敏观测、部署文档；数据库迁移：无；前端菜单/路由：无需同步；测试风险：真实设备和星语服务的网络故障仍需发布环境监控。"
```

## 交付顺序与阻断判定

1. 先完成 Task 1-2；如果无法证明外部 Driver 之前已经持有有效 Bootstrap Lease，后续 Adapter 和集成测试不得进入生产。
2. Task 3-4 完成后，PNG 必须可从对象存储按 artifact id 读取，PostgreSQL 三张事实表和 Bootstrap 引用必须在同一事务中保持一致。
3. Task 5 完成后，生产 App Run 必须经过 `AppWorkflowRouter -> AppV2Runtime`；`AppWorkflowDispatcher` 没有 handler 或 Official provider 缺配置时只能失败，不能进入通用 H5 Agent。
4. Task 6 是上线阻断验收：未设置隔离 PostgreSQL 时可跳过；设置后单 Worker、双 Worker、租约过期、重放和对象补偿用例必须全部通过。
5. Task 7 只在星语 API 合同和认证变量齐备时打开真实联调；在此之前使用 Mock Contract + Fake Runtime 验证，Fake 不能作为 production provider。

## 自审清单

- 设计文档中的 PNG 对象存储、PostgreSQL 元数据、租约顺序、续期、过期拒写、双 Worker、重放幂等、`qc_page_capture` 不变和 fail-closed 路由均有对应 Task。
- 计划未新增数据库迁移，因为现有 0034-0037 已提供所需表和公共 Capture FK；只对 Repository 增加租约续期 SQL。
- 所有跨任务类型已在 Interfaces 中固定：`RenewAppBootstrapLeaseCommand`、`AppBootstrapRepository.renew`、`AppBootstrapLeaseGuard`、`OfficialAppApiClient`、`build_app_workflow_handler` 和 `ProductionAppV2RuntimeExecutor`。
- PNG 原文不会进入 Redis、Agent 上下文、日志或 PostgreSQL `bytea`；只有对象存储和补偿清理路径处理 bytes。
