# App Workflow Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不接入正式星语 HTTP API 的前提下，用 Fake App Runtime 建立可恢复的 Android APK 安装、打开、首屏截图和 `APP_READY` 交接闭环，并固化 `app-workflow` 运行时 Skill。

**Architecture:** `AppQcWorkflow` 是确定性应用服务，不创建 Agent；它只依赖 `AppBootstrapPort`、`SurfaceDriver`、Bootstrap/Capture/Session 仓储和 `AppReadyPublisher`。PostgreSQL 保存 Bootstrap 阶段和幂等事实，Fake Provider 实现与未来 Official Adapter 相同的端口；`app-workflow` Skill 只描述运行时边界，不能替代数据库状态机或直接调用 Driver。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/Alembic、Microsoft Agent Framework、pytest、Ruff。

## Global Constraints

- H5 继续使用既有 Playwright Workflow；本计划不得改变 Web/H5 Driver 行为。
- App 第一阶段只支持 Android、单设备 Session、Screenshot 和 Action Trace，不支持 DOM、Visible Text、UI Tree、OCR、input、XPath、CSS 或 Appium locator。
- APK、截图正文、鉴权信息和设备句柄不得进入 Redis 或 Agent 上下文；跨 Worker 只传引用。
- Explorer 和 Rule Judge 不得导入 Driver、Fake/Official Provider 或设备客户端。
- `qc_task` 继续使用 `queued/running/review/done/failed/canceled`，不得增加 App 专用任务终态。
- `qc_page_capture` 继续作为 Web/H5 明细事实；App 只写 `qc_capture/qc_app_capture`。
- Feature Flag 默认关闭；`ALIOTH_APP_RUNTIME_PROVIDER` 只允许 `fake/official`。
- 正式 Worker 路由在 Screenshot Exploration 可消费 `AppReadyHandoff` 前保持关闭；本计划不把 `APP_READY` 伪装成任务完成。

---

## File Structure

- `services/api/app/agents/skills/app-workflow/SKILL.md`：App 任务级运行流程与 Agent 禁止项，不保存具体规则内容。
- `services/api/app/domain/interaction/app_workflow.py`：Bootstrap 阶段、快照、错误与 Ready Handoff 版本化合同。
- `services/api/app/application/app_bootstrap_state.py`：Bootstrap Repository 与事务提交端口。
- `services/api/app/application/app_capture_persistence.py`：截图校验、Artifact 存储和 App Capture 提交。
- `services/api/app/application/app_workflow.py`：确定性、可重放的 App Workflow 状态机。
- `services/api/app/application/app_workflow_router.py`：按持久化任务目标和版本选路，不读取消息文本猜测平台。
- `services/api/app/infrastructure/app_runtime/fake.py`：可注入故障的 Fake `AppBootstrapPort + SurfaceDriver`。
- `services/api/app/infrastructure/db/app_bootstrap_repository.py`：Bootstrap 检查点、Session 激活和 Ready Outbox 的 PostgreSQL 实现。
- `services/api/app/infrastructure/db/app_capture_repository.py`：公共 Capture、App Capture 和 Evidence Artifact 的原子写入。
- `services/api/migrations/versions/20260804_0035_app_workflow_runtime.py`：Bootstrap 表和公共 Capture FK 迁移。

---

### Task 1: 固化 app-workflow 运行时 Skill

**Files:**
- Create: `services/api/app/agents/skills/app-workflow/SKILL.md`
- Create: `services/api/tests/test_app_workflow_skill.py`
- Modify: `services/api/app/agents/skills/loader.py`
- Modify: `services/api/tests/test_skills_loader.py`
- Modify: `services/api/tests/test_capability_manifest.py`

**Interfaces:**
- Consumes: `TaskTargetPlatform.APP`、`AppBootstrapPort`、`SurfaceDriver`、`CaptureCommitted`、`AppReadyHandoff`。
- Produces: Agent Skills name `app-workflow`；`build_skills_context_provider(sub_dirs=("app-workflow",))` 可扫描；Capability Manifest 自动包含该 Skill。

- [x] **Step 1: 写 Skill 合同失败测试**

```python
def test_app_workflow_skill_freezes_runtime_boundaries() -> None:
    text = APP_WORKFLOW_SKILL.read_text(encoding="utf-8")
    assert "name: app-workflow" in text
    assert "install → open → bootstrap capture → APP_READY" in text
    assert "PostgreSQL" in text
    assert "ActionIntent" in text
    assert "不得调用 Driver" in text
    assert "DOM" in text and "UI Tree" in text
```

同时更新 Loader 与 Manifest 期望集合，使测试在 Skill 文件和扫描入口不存在时失败。

- [x] **Step 2: 运行并确认失败**

Run: `python -m pytest services/api/tests/test_app_workflow_skill.py services/api/tests/test_skills_loader.py services/api/tests/test_capability_manifest.py -q`

Expected: FAIL，缺少 `app-workflow/SKILL.md` 或 Manifest 项。

- [x] **Step 3: 编写最小 Skill 与扫描入口**

Skill 必须明确：安装/打开由确定性 Workflow 发起；Agent 只在 `APP_READY` 后选择已有候选目标；数据库是状态事实源；截图正文不进 Redis；正式 API 缺失时使用 Fake Port；不得把 Skill 文本当成规则或状态。

- [x] **Step 4: 验证 Skill**

Run: `python -m pytest services/api/tests/test_app_workflow_skill.py services/api/tests/test_skills_loader.py services/api/tests/test_capability_manifest.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/agents/skills/app-workflow/SKILL.md services/api/app/agents/skills/loader.py services/api/tests/test_app_workflow_skill.py services/api/tests/test_skills_loader.py services/api/tests/test_capability_manifest.py
git commit -m "[AI编程] feat: 增加 App Workflow 运行时 Skill" -m "涉及模块：Agent Skill、Capability Manifest；数据库迁移：无；前端菜单/路由：无需同步；测试风险：Skill 只冻结流程边界，不代表正式星语 API 已接入。"
```

### Task 2: 定义可恢复 App Workflow 合同

**Files:**
- Create: `services/api/app/domain/interaction/app_workflow.py`
- Modify: `services/api/app/domain/interaction/__init__.py`
- Create: `services/api/tests/test_app_workflow_contracts.py`

**Interfaces:**
- Produces: `AppBootstrapStage`、`AppBootstrapSnapshot`、`AppReadyHandoff`、`AppWorkflowFailure`、`APP_WORKFLOW_PROTOCOL_VERSION="app-workflow.v1"`。
- Stage: `validating/acquiring_session/resolving_build/installing/installed/opening/opened/bootstrap_capturing/app_ready/failed/canceled`。

- [x] **Step 1: 写版本化合同失败测试**

测试必须覆盖阶段序列、失败快照必须携带 `error_code`、`app_ready` 必须携带 `bootstrap_capture_id`、未知 JSON 字段可忽略、旧协议版本拒绝。

- [x] **Step 2: 运行并确认合同不存在**

Run: `python -m pytest services/api/tests/test_app_workflow_contracts.py -q`

Expected: FAIL with import error。

- [x] **Step 3: 实现不可变合同**

```python
@dataclass(frozen=True, kw_only=True)
class AppReadyHandoff:
    task_id: str
    agent_run_id: str
    session_id: str
    capture: CaptureRef
    capability_manifest: SurfaceCapabilityManifest
    bootstrap_row_version: int
```

`AppReadyHandoff` 不含图片字节、APK 路径、Driver handle 或 lease token。

- [x] **Step 4: 运行测试和 Ruff**

Run: `python -m pytest services/api/tests/test_app_workflow_contracts.py services/api/tests/test_app_bootstrap_contracts.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/domain/interaction/app_workflow.py services/api/app/domain/interaction/__init__.py services/api/tests/test_app_workflow_contracts.py
git commit -m "[AI编程] feat: 冻结 App Workflow 恢复合同" -m "涉及模块：interaction domain；数据库迁移：无；前端菜单/路由：无需同步；测试风险：仅定义状态合同，真实恢复由后续仓储实现。"
```

### Task 3: 实现可注入故障的 Fake App Runtime

**Files:**
- Create: `services/api/app/infrastructure/app_runtime/__init__.py`
- Create: `services/api/app/infrastructure/app_runtime/fake.py`
- Create: `services/api/tests/test_fake_app_runtime.py`

**Interfaces:**
- Consumes: `AppBootstrapPort`、`SurfaceDriver`、`AppInstallRequest`、`AppOpenRequest`、`SurfaceSessionSpec`、`ActionIntent`。
- Produces: `FakeAppRuntime`、`FakeAppScenario`、`FakeAppRuntimeError`。

- [x] **Step 1: 写 Fake Runtime 失败测试**

覆盖同一 `session_id + sha256` 重复安装返回 `reused_existing=true`、未安装不得打开、未打开不得截图、Capture 只有 PNG screenshot、只允许 click/scroll/back/observe、input 返回 `ACTION_UNSUPPORTED`、场景可注入 `DEVICE_BUSY/INSTALL_FAILED/OPEN_FAILED/CAPTURE_FAILED/SESSION_CRASHED`。

- [x] **Step 2: 运行并确认实现不存在**

Run: `python -m pytest services/api/tests/test_fake_app_runtime.py -q`

Expected: FAIL with import error。

- [x] **Step 3: 实现内存 Fake Provider**

Fake 只在进程内保存设备模拟状态，所有返回使用稳定 request/action id；截图使用固定有效 PNG，并在 metadata 中返回 package、activity、width、height、orientation 和 image signature。Capability 使用 `build_android_screenshot_only_manifest()`。

- [x] **Step 4: 运行合同和架构边界测试**

Run: `python -m pytest services/api/tests/test_fake_app_runtime.py services/api/tests/test_surface_capability_manifest.py services/api/tests/test_app_architecture_boundaries.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/infrastructure/app_runtime services/api/tests/test_fake_app_runtime.py
git commit -m "[AI编程] feat: 实现 Fake App Runtime" -m "涉及模块：App Runtime Adapter；数据库迁移：无；前端菜单/路由：无需同步；测试风险：Fake 只验证青云状态机，不代表真实设备兼容性。"
```

### Task 4: 增加 Bootstrap 检查点与公共 Capture FK 迁移

**Files:**
- Create: `services/api/migrations/versions/20260804_0035_app_workflow_runtime.py`
- Create: `services/api/tests/test_app_workflow_runtime_migration.py`
- Modify: `docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md`

**Interfaces:**
- Consumes: migration `20260804_0034`、`qc_surface_session`、`qc_capture`。
- Produces: `qc_app_bootstrap`；Surface Session/Ref、Action Execution 与 V2 Work Item 的公共 Capture FK。

- [x] **Step 1: 写迁移不变量失败测试**

断言 Bootstrap 每 Session 唯一、stage 白名单、attempt 非负、`row_version` 乐观锁、Ready 时必须有 Capture、失败时必须有 error；现有 `current_capture_id/requested_source_capture_id/source_capture_id/result_capture_id` 从 `qc_page_capture` 改为 `qc_capture`，列名保持兼容。

- [x] **Step 2: 运行并确认 0035 缺失**

Run: `python -m pytest services/api/tests/test_app_workflow_runtime_migration.py services/api/tests/test_production_deployment_files.py::test_deployment_guide_uses_current_single_migration_head -q`

Expected: FAIL。

- [x] **Step 3: 实现 Additive 迁移**

迁移先验证公共父 Capture 回填完整，再替换 FK；不删除列、不改名、不触碰 `qc_page_capture` Web 字段。Downgrade 在存在 App Bootstrap/App Capture 运行数据时拒绝破坏性回滚。

- [x] **Step 4: 运行迁移结构测试**

Expected: PASS，Head 为 `20260804_0035`。

- [x] **Step 5: 提交**

```powershell
git add services/api/migrations/versions/20260804_0035_app_workflow_runtime.py services/api/tests/test_app_workflow_runtime_migration.py docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md
git commit -m "[AI编程] feat: 增加 App Bootstrap 检查点" -m "涉及模块：App Workflow schema、公共 Capture FK；数据库迁移：新增 0035；前端菜单/路由：无需同步；测试风险：真实大表 FK 重建需在预发评估锁时长。"
```

### Task 5: 实现 Bootstrap 与 Starting Session 仓储

**Files:**
- Create: `services/api/app/application/app_bootstrap_state.py`
- Create: `services/api/app/infrastructure/db/app_bootstrap_repository.py`
- Modify: `services/api/app/application/surface_sessions.py`
- Modify: `services/api/app/infrastructure/db/surface_session_repository.py`
- Create: `services/api/tests/test_app_bootstrap_repository.py`
- Modify: `services/api/tests/test_surface_session_repository.py`

**Interfaces:**
- Produces: `AppBootstrapRepository.get_or_create()`、`transition()`、`fail()`；`SurfaceSessionRepository.register_starting()`、`activate_from_bootstrap()`。
- `transition` 必须接收 `expected_stage`、`expected_row_version` 和 Workflow lease identity，旧 Worker 更新返回冲突。

- [x] **Step 1: 写 SQL 行为失败测试**

Fake pool 断言所有推进使用 expected stage/version；安装和打开 request id/attempt 持久化；`activate_from_bootstrap()` 在一个事务内更新 Bootstrap `app_ready`、Session `active/current_capture_id` 和 Outbox。

- [x] **Step 2: 运行并确认失败**

Run: `python -m pytest services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_surface_session_repository.py -q`

- [x] **Step 3: 实现仓储与 Web 兼容方法**

保留现有 `register_active()` 给 H5；App 只调用 `register_starting()`，Bootstrap Capture 提交前不得获得普通 Session Command Permit。

- [x] **Step 4: 运行仓储测试和 Ruff**

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/application/app_bootstrap_state.py services/api/app/infrastructure/db/app_bootstrap_repository.py services/api/app/application/surface_sessions.py services/api/app/infrastructure/db/surface_session_repository.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_surface_session_repository.py
git commit -m "[AI编程] feat: 持久化 App Bootstrap 恢复状态" -m "涉及模块：App Bootstrap、Surface Session；数据库迁移：使用 0035；前端菜单/路由：无需同步；测试风险：未接真实 PostgreSQL 的并发压测。"
```

### Task 6: 持久化截图专用 App Capture

**Files:**
- Create: `services/api/app/application/app_capture_persistence.py`
- Create: `services/api/app/infrastructure/db/app_capture_repository.py`
- Create: `services/api/tests/test_app_capture_persistence.py`
- Create: `services/api/tests/test_app_capture_repository.py`

**Interfaces:**
- Produces: `AppCapturePersistenceService.persist_bootstrap()`、`PersistedAppCapture`、`AppCaptureRepository.save_bootstrap_capture()`。
- Consumes: `RawArtifactSet`、`EvidenceArtifactStore`、`CaptureRef`。

- [x] **Step 1: 写截图校验与补偿失败测试**

要求且仅允许一个 Screenshot；验证 PNG signature、非零尺寸、SHA-256 和最大字节数；存储成功但数据库失败时删除已写对象；数据库事务原子写 `qc_capture/qc_app_capture/qc_evidence_artifact`。

- [x] **Step 2: 运行并确认实现不存在**

Run: `python -m pytest services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py -q`

- [x] **Step 3: 实现最小持久化链**

Evidence Artifact 暂时继续使用兼容列 `page_capture_id` 存同 UUID，0035 保证其 FK 指向公共 Capture；不得给 App 写伪 URL、DOM 或 Visible Text。

- [x] **Step 4: 运行 App/Web Capture 回归**

Run: `python -m pytest services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py services/api/tests/test_page_capture_persistence.py -q`

- [x] **Step 5: 提交**

```powershell
git add services/api/app/application/app_capture_persistence.py services/api/app/infrastructure/db/app_capture_repository.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py
git commit -m "[AI编程] feat: 持久化 App 首屏 Capture" -m "涉及模块：App Capture、Evidence Artifact；数据库迁移：使用 0035；前端菜单/路由：无需同步；测试风险：截图尺寸解析第一期仅覆盖 PNG。"
```

### Task 7: 实现确定性 AppQcWorkflow 与恢复

**Files:**
- Create: `services/api/app/application/app_workflow.py`
- Create: `services/api/tests/test_app_qc_workflow.py`
- Modify: `services/api/app/application/app_bootstrap_state.py`
- Modify: `services/api/app/infrastructure/db/app_bootstrap_repository.py`
- Modify: `services/api/app/application/app_capture_persistence.py`
- Modify: `services/api/app/infrastructure/db/app_capture_repository.py`
- Modify: `services/api/app/application/platform_builds.py`
- Modify: `services/api/app/infrastructure/db/platform_build_repository.py`
- Modify: `services/api/tests/test_app_bootstrap_repository.py`
- Modify: `services/api/tests/test_platform_build_repository.py`

**Interfaces:**
- Produces: `AppQcWorkflow.run(command: RunAppWorkflowCommand) -> AppReadyHandoff`。
- Consumes: Platform Build reader、Surface Session repository、Bootstrap repository、`AppBootstrapPort`、`SurfaceDriver`、App Capture persistence、`AppReadyPublisher`。

- [x] **Step 1: 写状态机失败测试**

覆盖成功顺序 `start_session → install → open → capture → commit ready → publish handoff`；安装/打开/截图失败不发布；从 installed/opened/capturing 恢复不重复已确认副作用；重复 Ready 唤醒不重复写 Capture；取消关闭 Session；Capability 不满足时在建 Session 前失败。

- [x] **Step 2: 运行并确认 Workflow 不存在**

Run: `python -m pytest services/api/tests/test_app_qc_workflow.py -q`

- [x] **Step 3: 实现单步推进循环**

每次外部调用前后均持久化阶段；外部超时保留当前 attempt 和幂等键，重跑先读数据库；最多一次状态跃迁由一条 repository 命令完成。`APP_READY` 只产生 Handoff，不写 `qc_task=done`。

- [x] **Step 4: 运行故障矩阵**

Run: `python -m pytest services/api/tests/test_app_qc_workflow.py services/api/tests/test_fake_app_runtime.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_app_capture_persistence.py -q`

- [x] **Step 5: 提交**

```powershell
git add services/api/app/application/app_workflow.py services/api/tests/test_app_qc_workflow.py services/api/app/application/app_bootstrap_state.py services/api/app/infrastructure/db/app_bootstrap_repository.py services/api/app/application/app_capture_persistence.py services/api/app/infrastructure/db/app_capture_repository.py services/api/app/application/platform_builds.py services/api/app/infrastructure/db/platform_build_repository.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_platform_build_repository.py
git commit -m "[AI编程] feat: 实现可恢复 App QC Workflow" -m "涉及模块：App Workflow Runtime；数据库迁移：使用 0035；前端菜单/路由：无需同步；测试风险：本提交只交接 APP_READY，Screenshot Exploration 尚未接入正式 Worker。"
```

### Task 8: 增加路由与 Provider 组合根但保持生产门禁

**Files:**
- Create: `services/api/app/application/app_workflow_router.py`
- Create: `services/api/app/infrastructure/app_runtime/factory.py`
- Create: `services/api/tests/test_app_workflow_router.py`
- Create: `services/api/tests/test_app_runtime_factory.py`
- Modify: `services/api/app/core/config.py`

**Interfaces:**
- Produces: `WorkflowRoute.H5/APP`、`AppRuntimeFactory.build(provider)`。
- Router 只消费持久化 `TaskTarget` 和锁定 `workflow_version`；Factory 的 `official` 在 Adapter 未实现时返回明确配置错误。

- [x] **Step 1: 写路由与 Provider 失败测试**

覆盖 H5→旧 Workflow、App+V2→AppQcWorkflow、App+V1 拒绝；fake 可构建；official 缺少实现明确失败；mobile_demo 永不成为 Provider。

- [x] **Step 2: 运行并确认失败**

- [x] **Step 3: 实现组合根**

不修改 `worker._process_one()`，防止 APP_READY 被误当任务终态。只向下一阶段提供可注入的 Runtime Builder。

- [x] **Step 4: 运行提交/路由/配置回归**

Run: `python -m pytest services/api/tests/test_app_workflow_router.py services/api/tests/test_app_runtime_factory.py services/api/tests/test_app_task_submission.py services/api/tests/test_app_qc_settings.py -q`

- [x] **Step 5: 提交**

```powershell
git add services/api/app/application/app_workflow_router.py services/api/app/infrastructure/app_runtime/factory.py services/api/app/core/config.py services/api/tests/test_app_workflow_router.py services/api/tests/test_app_runtime_factory.py
git commit -m "[AI编程] feat: 增加 App Workflow 路由与 Provider 工厂" -m "涉及模块：Workflow Router、Runtime composition；数据库迁移：无新增；前端菜单/路由：无需同步；测试风险：正式 Worker 路由将在截图探索可消费 Handoff 后启用。"
```

### Task 9: Runtime 验证与下一阶段交接

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/plans/2026-08-04-app-workflow-runtime.md`
- Modify: `services/api/tests/test_app_architecture_boundaries.py`

**Interfaces:**
- Produces: 已验证的 `AppReadyHandoff`，供 Screenshot Exploration 计划消费。

- [x] **Step 1: 扩充静态边界门禁**

阻止 `agents/explorer` 和 `agents/rule_judge` 导入 `app_runtime/app_workflow/app_bootstrap_repository`，阻止 Fake Provider 出现在 production 默认路由之外。

- [x] **Step 2: 更新专项规则**

记录 Bootstrap 不是 ActionIntent、Session 必须在首屏 CaptureCommitted 后激活、Ready 不是任务终态、Skill 不是状态事实源。

- [x] **Step 3: 运行 Runtime 目标测试**

Run: `python -m pytest services/api/tests/test_app_workflow_skill.py services/api/tests/test_app_workflow_contracts.py services/api/tests/test_fake_app_runtime.py services/api/tests/test_app_workflow_runtime_migration.py services/api/tests/test_app_bootstrap_repository.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py services/api/tests/test_app_qc_workflow.py services/api/tests/test_app_workflow_router.py services/api/tests/test_app_runtime_factory.py services/api/tests/test_app_architecture_boundaries.py -q`

- [x] **Step 4: 运行变更 Ruff 与全量测试**

Run: 对 `dev_1.5...HEAD` 的 Python 文件运行 Ruff，再执行 `python -m pytest -q --basetemp '.pytest_cache/app-workflow-runtime-final'`。

- [x] **Step 5: 提交验证文档**

```powershell
git add docs/专项能力与高频回归规则.md docs/superpowers/plans/2026-08-04-app-workflow-runtime.md services/api/tests/test_app_architecture_boundaries.py
git commit -m "[AI编程] test: 固化 App Workflow Runtime 回归门禁" -m "涉及模块：App Runtime、专项回归文档；数据库迁移：无新增；前端菜单/路由：无需同步；测试风险：正式星语 API、视觉理解与真实设备链路仍待后续阶段。"
```

### Verification 2026-08-04

- Runtime 目标测试：`68 passed`。
- `dev_1.5...HEAD` 全部 Python 变更：Ruff PASS。
- API 全量回归：`1854 passed, 2 skipped`。
- Production Worker：未导入 `AppQcWorkflow`、`AppRuntimeFactory` 或 `FakeAppRuntime`，App 路由保持关闭。

## Self-Review Result

- Spec coverage：本计划覆盖总设计的 App Bootstrap Checkpoint、Fake Provider、Screenshot Capture、Session 激活、幂等恢复、Provider/Workflow 路由和 Runtime Skill；截图理解、Explorer 候选、Inspection 能力门禁和工作台继续由第三阶段计划实现。
- Placeholder scan：所有接口、失败分支和测试命令均有明确归属；正式 API 缺失被建模为 `official` 明确配置错误，而不是空实现。
- Type consistency：外部安装/打开统一使用 `AppBootstrapPort`，设备交互统一使用 `SurfaceDriver`，持久化交接统一使用 `CaptureRef` 和 `AppReadyHandoff`，App 阶段不使用 `CaptureReference.page_capture_id`。
- Safety check：本计划不启用生产 Worker App 路由，不会把仅完成 Bootstrap 的 Run 标记为任务完成。
