# App QC Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保持 H5/Playwright 行为兼容的前提下，建立 App 任务目标、版本化能力清单、APK 构建制品、Bootstrap 端口和 Web/App 通用 Capture 身份，为后续 App Workflow Runtime 提供稳定且可恢复的基础合同。

**Architecture:** 业务入口用 `TaskTargetPlatform` 区分 `h5/app/web`，运行时仍使用 `SurfaceKind.WEB/ANDROID/IOS`；APK 通过独立 `qc_platform_build` 管理，任务只保存 `platform_build_ref`。Capture 采用 `qc_capture` 公共父实体与 `qc_page_capture/qc_app_capture` 平台子实体，迁移阶段保留既有 Web 字段和查询，避免一次性重写现有页面质检链。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、PostgreSQL、Alembic、asyncpg、pytest、pytest-asyncio、python-multipart。

## Global Constraints

- H5 继续走现有 Playwright Workflow；`TaskTargetPlatform.H5/WEB` 映射 `SurfaceKind.WEB`，`APP` 映射 `SurfaceKind.ANDROID`。
- `target_platform=app` 时 `target_url` 必须为空且必须提供 `task_config.app.platform_build_ref`；H5/Web 必须提供 HTTP(S) URL。
- `ALIOTH_WORKFLOW_VERSION=v1|v2`、`ALIOTH_APP_QC_ENABLED=false|true`、`ALIOTH_APP_RUNTIME_PROVIDER=fake|official`、`ALIOTH_MOBILE_DEMO_SMOKE_ENABLED=false|true` 是唯一 Feature Flag；production 禁止 `mobile_demo` provider。
- App 第一阶段只接受 Android APK，只承诺 Screenshot 与 Action Trace；DOM、UI Tree、Visible Text、OCR、Input 都是 `unsupported`。
- Explorer 不得访问 Playwright、ADB、Appium、Airtest 或外部星语 Client；Judge 不得访问任何 Driver。
- `qc_page_capture` 继续作为 Web/H5 明细事实表；`qc_capture` 是公共身份事实；不得引入第二套任务终态或规则执行状态。
- APK 和截图均校验类型、大小与 SHA-256；Token、Cookie、密码、签名密钥、设备句柄和二进制不得进入 Agent/Redis 上下文。
- 所有新增注释和错误消息使用中文 UTF-8；每个文件保持单一职责。

## File Map

- `services/api/app/domain/task/targets.py`：任务平台、目标校验与 Surface 映射。
- `services/api/app/domain/task/platform_build.py`：APK 构建制品身份和值对象。
- `services/api/app/domain/interaction/capabilities.py`：版本化 Capability Manifest。
- `services/api/app/domain/interaction/app_bootstrap.py`：安装/打开稳定端口，不含 HTTP DTO。
- `services/api/app/domain/interaction/captures.py`：新增通用 `CaptureRef`，保留旧 `CaptureReference` 兼容。
- `services/api/app/application/capture_profiles.py`：按任务平台确定采集产物。
- `services/api/app/application/platform_builds.py`：APK 流式校验、存储与数据库编排。
- `services/api/app/infrastructure/storage/local_platform_build_store.py`：本地 APK 存储实现。
- `services/api/app/infrastructure/db/platform_build_repository.py`：`qc_platform_build` 仓储。
- `services/api/app/api/v1/app_builds.py`：鉴权后的 multipart APK 上传入口。
- `services/api/migrations/versions/20260804_0033_app_task_target_and_build.py`：任务目标与 APK 制品表。
- `services/api/migrations/versions/20260804_0034_common_capture_identity.py`：公共 Capture、App Capture 与 Web 回填。

---

### Task 1: 冻结任务目标与 Feature Flag

**Files:**
- Create: `services/api/app/domain/task/targets.py`
- Modify: `services/api/app/domain/task/__init__.py`
- Modify: `services/api/app/core/config.py`
- Test: `services/api/tests/test_task_target_contracts.py`
- Test: `services/api/tests/test_app_qc_settings.py`

**Interfaces:**
- Consumes: `SurfaceKind` from `services.api.app.domain.interaction.enums`。
- Produces: `TaskTargetPlatform`、`TaskTarget`、`AppTaskConfig`、`WorkflowSettings` 和 `TaskTarget.surface_kind`。

- [x] **Step 1: 写任务目标失败测试**

```python
def test_app_target_requires_build_and_forbids_url() -> None:
    with pytest.raises(TaskTargetValidationError, match="platform_build_ref"):
        TaskTarget(platform=TaskTargetPlatform.APP, target_url=None, app=None)
    with pytest.raises(TaskTargetValidationError, match="target_url 必须为空"):
        TaskTarget(
            platform=TaskTargetPlatform.APP,
            target_url="app://demo",
            app=AppTaskConfig(platform_build_ref="app-build:01JABC"),
        )


def test_h5_and_app_map_to_stable_surface_kinds() -> None:
    assert TaskTarget(platform=TaskTargetPlatform.H5, target_url="https://example.test").surface_kind is SurfaceKind.WEB
    assert TaskTarget(
        platform=TaskTargetPlatform.APP,
        app=AppTaskConfig(platform_build_ref="app-build:01JABC"),
    ).surface_kind is SurfaceKind.ANDROID
```

- [x] **Step 2: 运行测试并确认失败**

Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_task_target_contracts.py services/api/tests/test_app_qc_settings.py -q`

Expected: FAIL，提示 `services.api.app.domain.task.targets` 不存在。

- [x] **Step 3: 实现最小领域合同与配置**

```python
class TaskTargetPlatform(StrEnum):
    H5 = "h5"
    APP = "app"
    WEB = "web"


@dataclass(frozen=True, kw_only=True)
class AppTaskConfig:
    platform_build_ref: str
    package_name: str | None = None
    device_backend: str = "cloud_phone"
    device_profile_id: str = "default"


@dataclass(frozen=True, kw_only=True)
class TaskTarget:
    platform: TaskTargetPlatform
    target_url: str | None = None
    app: AppTaskConfig | None = None

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.ANDROID if self.platform is TaskTargetPlatform.APP else SurfaceKind.WEB
```

`WorkflowSettings` 精确字段为：

```python
class WorkflowSettings(BaseModel):
    version: Literal["v1", "v2"] = "v1"
    app_qc_enabled: bool = False
    app_runtime_provider: Literal["fake", "official"] = "fake"
    mobile_demo_smoke_enabled: bool = False
```

`get_settings()` 分别读取 `ALIOTH_WORKFLOW_VERSION`、`ALIOTH_APP_QC_ENABLED`、`ALIOTH_APP_RUNTIME_PROVIDER`、`ALIOTH_MOBILE_DEMO_SMOKE_ENABLED`；非法版本或 provider 回退为 `v1/fake`。

- [x] **Step 4: 运行目标与配置测试**

Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_task_target_contracts.py services/api/tests/test_app_qc_settings.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/domain/task services/api/app/core/config.py services/api/tests/test_task_target_contracts.py services/api/tests/test_app_qc_settings.py
git commit -m "[AI编程] feat: 冻结 App 任务目标与 Workflow 开关" -m "涉及模块：task domain、core config；数据库迁移：无；前端菜单/路由：无需同步；测试风险：仅覆盖配置解析和目标不变量，运行时路由在后续计划覆盖。"
```

### Task 2: 升级版本化 Capability Manifest

**Files:**
- Create: `services/api/app/domain/interaction/capabilities.py`
- Modify: `services/api/app/domain/interaction/surface_driver.py`
- Modify: `services/api/app/domain/interaction/__init__.py`
- Modify: `services/api/app/infrastructure/browser/surface_driver.py`
- Modify: `services/api/tests/test_surface_driver_adapter.py`
- Test: `services/api/tests/test_surface_capability_manifest.py`

**Interfaces:**
- Consumes: `SurfaceKind`、`InteractionValidationError`、`freeze_mapping`。
- Produces: `CAPABILITY_MANIFEST_PROTOCOL_VERSION = "surface-capabilities.v1"`、`CapabilityStatus`、`CapabilityDescriptor`、`SurfaceCapabilityManifest.require_ready(names)`。

- [x] **Step 1: 写 Manifest 合同失败测试**

```python
def test_app_manifest_rejects_missing_required_ready_capability() -> None:
    manifest = SurfaceCapabilityManifest(
        adapter_id="xingyu.fake",
        adapter_version="1.0",
        surface_kind=SurfaceKind.ANDROID,
        platform="android",
        backends=("fake",),
        actions={"click": CapabilityDescriptor(status=CapabilityStatus.READY)},
        observations={"screenshot": CapabilityDescriptor(status=CapabilityStatus.READY)},
        artifacts={"screenshot": CapabilityDescriptor(status=CapabilityStatus.READY)},
    )
    with pytest.raises(CapabilityMismatchError, match="install"):
        manifest.require_ready(("session", "install", "open", "click", "scroll", "back", "screenshot", "close"))


def test_unsupported_input_is_explicit() -> None:
    manifest = build_android_screenshot_only_manifest(adapter_id="fake", adapter_version="1")
    assert manifest.actions["input"].status is CapabilityStatus.UNSUPPORTED
    assert manifest.observations["ui_tree"].status is CapabilityStatus.UNSUPPORTED
```

- [x] **Step 2: 运行测试并确认旧结构不满足合同**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_surface_capability_manifest.py -q`

Expected: FAIL，提示 `CapabilityDescriptor` 无法导入。

- [x] **Step 3: 实现描述符、Manifest 与 Playwright 映射**

```python
class CapabilityStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    STUB = "stub"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, kw_only=True)
class CapabilityDescriptor:
    status: CapabilityStatus
    target_modes: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    notes: str | None = None
```

`SurfaceCapabilityManifest` 使用 `actions/observations/artifacts: Mapping[str, CapabilityDescriptor]`，并提供 `require_ready()`；Playwright Adapter 将现有动作标为 ready，将 Screenshot/DOM/Visible Text/UI Snapshot 标为 ready，不改变 Driver 执行行为。

- [x] **Step 4: 运行 Manifest 和现有 Web Adapter 测试**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_surface_capability_manifest.py services/api/tests/test_surface_driver_adapter.py services/api/tests/test_surface_runtime_contracts.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/domain/interaction services/api/app/infrastructure/browser/surface_driver.py services/api/tests/test_surface_capability_manifest.py services/api/tests/test_surface_driver_adapter.py
git commit -m "[AI编程] feat: 版本化 Surface Capability Manifest" -m "涉及模块：interaction domain、Playwright adapter；数据库迁移：无；前端菜单/路由：无需同步；测试风险：正式 App Adapter 尚未接入，先由 Fake Provider 验证。"
```

### Task 3: 增加 App Bootstrap、通用 CaptureRef 与平台采集策略

**Files:**
- Create: `services/api/app/domain/interaction/app_bootstrap.py`
- Modify: `services/api/app/domain/interaction/captures.py`
- Modify: `services/api/app/domain/interaction/__init__.py`
- Create: `services/api/app/application/capture_profiles.py`
- Test: `services/api/tests/test_app_bootstrap_contracts.py`
- Test: `services/api/tests/test_capture_profile_policy.py`

**Interfaces:**
- Consumes: `TaskTargetPlatform`、`SurfaceSessionRef`、`CaptureProfile`。
- Produces: `AppInstallRequest/Result`、`AppOpenRequest/Result`、`AppBootstrapPort`、`CaptureRef`、`CaptureKind`、`CaptureProfilePolicy.for_platform(platform)`。

- [x] **Step 1: 写 Bootstrap 与截图策略失败测试**

```python
def test_app_capture_profile_never_requests_dom_or_text() -> None:
    profile = CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP)
    requested = set(profile.before.artifact_types) | set(profile.after.artifact_types)
    assert requested == {"screenshot", "action_trace"}
    assert requested.isdisjoint({"dom", "visible_text", "ui_snapshot", "ocr_text"})


def test_bootstrap_idempotency_keys_are_stable() -> None:
    request = AppInstallRequest(
        task_id="task-1",
        session_id="session-1",
        platform_build_ref="app-build:01JABC",
        sha256="a" * 64,
        package_name="com.example.target",
    )
    assert request.idempotency_key == f"session-1:{'a' * 64}"
```

- [x] **Step 2: 运行测试并确认合同尚不存在**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_bootstrap_contracts.py services/api/tests/test_capture_profile_policy.py -q`

Expected: FAIL，提示 App Bootstrap 类型无法导入。

- [x] **Step 3: 实现稳定端口与 CaptureRef**

```python
class AppBootstrapPort(Protocol):
    async def ensure_installed(self, request: AppInstallRequest) -> AppInstallResult: ...
    async def open_app(self, request: AppOpenRequest) -> AppOpenResult: ...


@dataclass(frozen=True, kw_only=True)
class CaptureRef:
    task_id: str
    session_id: str
    capture_id: str
    surface_kind: SurfaceKind
    capture_kind: CaptureKind
    role: str
    artifact_ids: tuple[str, ...] = ()
    parent_capture_id: str | None = None
    action_execution_id: str | None = None
    version: str = "capture-ref.v1"
```

保留现有 `CaptureReference(page_capture_id=...)` 和 `page-capture.v1` 解析，直到旧 Web 调用全部迁移；新 App/V2 代码只能依赖 `CaptureRef`。

- [x] **Step 4: 运行新合同与旧 Capture 序列化测试**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_bootstrap_contracts.py services/api/tests/test_capture_profile_policy.py services/api/tests/test_interaction_contracts.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/domain/interaction services/api/app/application/capture_profiles.py services/api/tests/test_app_bootstrap_contracts.py services/api/tests/test_capture_profile_policy.py
git commit -m "[AI编程] feat: 增加 App Bootstrap 与通用 Capture 合同" -m "涉及模块：interaction domain、capture policy；数据库迁移：无；前端菜单/路由：无需同步；测试风险：仅冻结端口，外部传输和恢复状态机在 Runtime 计划覆盖。"
```

### Task 4: 增加任务目标与 APK 构建制品迁移

**Files:**
- Create: `services/api/migrations/versions/20260804_0033_app_task_target_and_build.py`
- Modify: `services/api/tests/test_production_deployment_files.py`
- Modify: `docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md`
- Test: `services/api/tests/test_app_qc_foundation_migrations.py`

**Interfaces:**
- Consumes: Alembic head `20260803_0032`。
- Produces: `qc_task.target_platform`、nullable `target_url`、`qc_platform_build`、任务目标 Check Constraint。

- [x] **Step 1: 写迁移文本与单一 Head 失败测试**

```python
def test_app_task_target_migration_freezes_shape() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "20260803_0032"' in text
    assert "CREATE TABLE qc_platform_build" in text
    assert "ADD COLUMN target_platform" in text
    assert "target_platform IN ('h5', 'app', 'web')" in text
    assert "target_url IS NULL" in text
    assert "task_config #>> '{app,platform_build_ref}'" in text
```

- [x] **Step 2: 运行并确认迁移文件缺失**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_qc_foundation_migrations.py services/api/tests/test_production_deployment_files.py::test_deployment_guide_uses_current_single_migration_head -q`

Expected: FAIL，提示迁移文件不存在。

- [x] **Step 3: 实现可回滚的增量迁移**

迁移必须：先以 `h5` 默认值增加 `target_platform`；移除 `target_url NOT NULL`；建立 `qc_platform_build`（`sha256` 唯一，`status` 仅 `uploading/verified/rejected/deleted`）；添加 `ck_qc_task_target_shape`；创建 `(platform,status,created_at)` 索引。`downgrade()` 按 FK/约束/索引/表/列逆序删除，并在恢复 `target_url NOT NULL` 前拒绝存在 App 任务的降级。

- [x] **Step 4: 运行迁移测试**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_qc_foundation_migrations.py services/api/tests/test_production_deployment_files.py::test_deployment_guide_uses_current_single_migration_head -q`

Expected: PASS，迁移 Head 为 `20260804_0033`。

- [x] **Step 5: 提交**

```powershell
git add services/api/migrations/versions/20260804_0033_app_task_target_and_build.py services/api/tests/test_app_qc_foundation_migrations.py docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md
git commit -m "[AI编程] feat: 增加 App 任务目标与 APK 制品表" -m "涉及模块：task schema、platform build schema；数据库迁移：新增 0033；前端菜单/路由：无需同步；测试风险：需在集成环境执行真实 PostgreSQL upgrade/downgrade。"
```

### Task 5: 实现 APK 构建制品上传纵切面

**Files:**
- Modify: `pyproject.toml`
- Create: `services/api/app/domain/task/platform_build.py`
- Create: `services/api/app/application/platform_builds.py`
- Create: `services/api/app/infrastructure/storage/local_platform_build_store.py`
- Create: `services/api/app/infrastructure/db/platform_build_repository.py`
- Create: `services/api/app/api/v1/app_builds.py`
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/api/v1/router.py`
- Test: `services/api/tests/test_platform_build_service.py`
- Test: `services/api/tests/test_platform_build_repository.py`
- Test: `services/api/tests/test_app_builds_api.py`

**Interfaces:**
- Consumes: authenticated user dependency、asyncpg pool、`WorkflowSettings.app_qc_enabled`。
- Produces: `POST /api/v1/app-builds` multipart 字段 `file`，响应 `platform_build_ref/status/original_filename/sha256/size_bytes`。
- Produces: `PlatformBuildStore.stage(chunks, max_size_bytes) -> StagedPlatformBuild`、`commit(staged) -> str`、`discard(staged) -> None`。
- Produces: `PlatformBuildRepository.find_verified_by_sha256(sha256)`、`create_upload(...)`、`mark_verified(...)`、`mark_rejected(...)`。

- [x] **Step 1: 添加 multipart 依赖并写失败测试**

`pyproject.toml` 增加 `"python-multipart>=0.0.9,<1.0"`。服务测试覆盖：非 `.apk`、非 ZIP/APK 文件头、超过配置上限、重复 SHA-256 复用 verified 构建、写入失败不留下 verified 记录。

```python
def test_upload_verified_apk_returns_stable_reference(client) -> None:
    response = client.post(
        "/api/v1/app-builds",
        files={"file": ("target.apk", b"PK\x03\x04payload", "application/vnd.android.package-archive")},
    )
    assert response.status_code == 201
    assert response.json()["platform_build_ref"].startswith("app-build:")
    assert response.json()["status"] == "verified"
```

- [x] **Step 2: 安装新增依赖并确认测试失败**

Run: `& 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pip install --target '.pytest_cache/task5-packages' 'python-multipart>=0.0.9,<1.0'`

Run: `$env:PYTHONPATH='.pytest_cache/task5-packages;D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_platform_build_service.py services/api/tests/test_app_builds_api.py -q`

Expected: FAIL，提示平台构建服务与路由不存在。

- [x] **Step 3: 实现流式限制、校验、存储与仓储**

```python
class PlatformBuildService:
    async def upload_apk(
        self,
        *,
        original_filename: str,
        chunks: AsyncIterable[bytes],
        created_by: str,
    ) -> PlatformBuild:
        _validate_apk_filename(original_filename)
        staged = await self._store.stage(chunks, max_size_bytes=self._max_size_bytes)
        try:
            _validate_apk_magic(staged.header)
            existing = await self._repository.find_verified_by_sha256(staged.sha256)
            if existing is not None:
                await self._store.discard(staged)
                return existing
            pending = await self._repository.create_upload(
                original_filename=original_filename,
                build_id=pending.build_id,
                size_bytes=staged.size_bytes,
                created_by=created_by,
            )
            storage_path = await self._store.commit(staged)
            return await self._repository.mark_verified(
                build_id=pending.build_id,
                storage_path=storage_path,
            )
        except Exception as exc:
            await self._store.discard(staged)
            await self._repository.mark_rejected(
                sha256=staged.sha256,
                reason=type(exc).__name__,
            )
            raise
```

实现必须逐块累计 SHA-256 和字节数，前四字节只接受 ZIP magic `PK\x03\x04/PK\x05\x06/PK\x07\x08`，默认上限 512 MiB；本地路径固定为 `<sha256[:2]>/<sha256>.apk`，先写同目录临时文件再原子替换。数据库状态顺序固定为 `uploading -> verified`，任何校验失败写 `rejected` 并删除临时文件；API 不接收客户端提供的 `storage_path/sha256/status`。

- [x] **Step 4: 运行服务、API、鉴权和路径穿越测试**

Run: `$env:PYTHONPATH='.pytest_cache/task5-packages;D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_platform_build_service.py services/api/tests/test_platform_build_repository.py services/api/tests/test_app_builds_api.py services/api/tests/test_auth_api.py -q`

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add pyproject.toml services/api/app/domain/task/platform_build.py services/api/app/application/platform_builds.py services/api/app/infrastructure/storage/local_platform_build_store.py services/api/app/infrastructure/db/platform_build_repository.py services/api/app/core/config.py services/api/app/api/v1/app_builds.py services/api/app/api/v1/router.py services/api/tests/test_platform_build_service.py services/api/tests/test_platform_build_repository.py services/api/tests/test_app_builds_api.py
git commit -m "[AI编程] feat: 实现 APK 构建制品上传" -m "涉及模块：platform build domain/application/storage/API；数据库迁移：依赖 0033；前端菜单/路由：后续工作台计划同步；测试风险：APK 包元数据解析暂不承诺，正式 package/version 以后续安装结果校验。"
```

### Task 6: 扩展统一任务提交入口并保持 H5 兼容

**Files:**
- Modify: `services/api/app/api/v1/tasks.py`
- Modify: `services/api/app/application/task_submission.py`
- Modify: `services/api/app/infrastructure/db/task_repository.py`
- Modify: `services/api/app/application/task_query.py`
- Modify: `services/api/tests/test_task_submission.py`
- Modify: `services/api/tests/test_task_repository.py`
- Test: `services/api/tests/test_app_task_submission.py`

**Interfaces:**
- Consumes: `TaskTarget`、`WorkflowSettings`、`qc_platform_build` verified lookup。
- Produces: 单一 `POST /api/v1/tasks` 同时接受 H5/App；队列只携带引用和目标快照。

- [x] **Step 1: 写 API/Service 失败测试**

```python
def test_app_task_rejected_when_feature_flag_is_off(client) -> None:
    response = client.post("/api/v1/tasks", json={
        "target_platform": "app",
        "target_url": None,
        "task_config": {"app": {"platform_build_ref": "app-build:01JABC"}, "workflow_version": "v2"},
    })
    assert response.status_code == 409
    assert response.json()["code"] == "APP_QC_DISABLED"


async def test_app_queue_payload_contains_reference_not_apk_bytes(service, queue) -> None:
    result = await service.submit_task(target=APP_TARGET, ruleset_code="alioth_first_ruleset", ruleset_version=1, channel="api", priority=100, task_config={"workflow_version": "v2"}, page_sampling_policy={}, loop_guard={})
    record = await queue.get(result.run_id)
    assert record.task_config["_task_target"]["app"]["platform_build_ref"] == "app-build:01JABC"
    assert "apk" not in json.dumps(record.task_config).lower()
```

- [x] **Step 2: 运行并确认旧 URL-only 合同失败**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_task_submission.py services/api/tests/test_task_submission.py services/api/tests/test_task_repository.py -q`

Expected: FAIL，旧 `SubmitTaskRequest` 要求非空 `target_url` 且不接受 `target_platform`。

- [x] **Step 3: 最小改造统一入口**

API 默认 `target_platform="h5"`，因此旧 H5 请求无需改动。Service/Repository 接受 `target: TaskTarget`；DB 写入 `target_platform`、nullable `target_url`，App 创建前锁定 `workflow_version=v2` 并确认 build 为 `verified`。队列新增 `_task_target` 快照；仅 H5/Web 写 `_trusted_target_url`。任务消息对 App 输出“目标平台：app、构建制品引用”，不输出 URL、存储路径或二进制。

- [x] **Step 4: 运行 App 与 H5 提交回归**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_task_submission.py services/api/tests/test_task_submission.py services/api/tests/test_task_repository.py -q`

Expected: PASS；旧 H5 测试无需修改请求体即可通过。

- [x] **Step 5: 提交**

```powershell
git add services/api/app/api/v1/tasks.py services/api/app/application/task_submission.py services/api/app/infrastructure/db/task_repository.py services/api/app/application/task_query.py services/api/tests/test_app_task_submission.py services/api/tests/test_task_submission.py services/api/tests/test_task_repository.py
git commit -m "[AI编程] feat: 统一提交 H5 与 App 质检任务" -m "涉及模块：tasks API/application/repository/query；数据库迁移：依赖 0033；前端菜单/路由：工作台暂未开放 App 表单；测试风险：App Worker 路由尚未实现，Feature Flag 默认关闭。"
```

### Task 7: 增加公共 Capture 身份与 App Capture 子表

**Files:**
- Create: `services/api/migrations/versions/20260804_0034_common_capture_identity.py`
- Modify: `services/api/tests/test_app_qc_foundation_migrations.py`
- Test: `services/api/tests/test_common_capture_migration.py`
- Modify: `docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md`

**Interfaces:**
- Consumes: migration `20260804_0033`、现有 `qc_page_capture.id`。
- Produces: `qc_capture`、`qc_app_capture`、历史 Web Capture 原 UUID 回填、Web 父子同步约束。

- [x] **Step 1: 写迁移不变量失败测试**

```python
def test_common_capture_migration_backfills_existing_page_ids() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "20260804_0033"' in text
    assert "CREATE TABLE qc_capture" in text
    assert "CREATE TABLE qc_app_capture" in text
    assert "SELECT page.id" in text
    assert "REFERENCES qc_capture(id)" in text
    assert "DEFERRABLE INITIALLY DEFERRED" in text
```

- [x] **Step 2: 运行并确认迁移缺失**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_common_capture_migration.py -q`

Expected: FAIL，迁移文件不存在。

- [x] **Step 3: 实现 Additive 公共身份迁移**

`qc_capture` 精确状态只允许 `pending/captured/failed/skipped`；`surface_kind` 只允许 `web/android/ios`；`capture_role` 至少允许 `bootstrap/before_action/after_action/observation/supplemental`。先创建父表，再按 `qc_page_capture.id` 回填 `surface_kind='web'`，随后将 `qc_page_capture.id` 添加延迟 FK 到父表；新增 Web 双写触发器，保证旧写路径仍在同一事务建立父行。`qc_app_capture.capture_id` 是引用父表的主键，只有 Screenshot/Package/Activity/尺寸/方向/签名/设备快照，不增加 DOM、UI Tree 或 Visible Text 列。

本次迁移不删除任何旧 `page_capture_id` 列；通用表迁移到 `capture_id` 在 Runtime 计划按仓储逐一完成，防止一个 DDL 同时破坏全部旧查询。

- [x] **Step 4: 运行迁移 Head 与结构测试**

Run: `$env:PYTHONPATH='D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_common_capture_migration.py services/api/tests/test_app_qc_foundation_migrations.py services/api/tests/test_production_deployment_files.py::test_deployment_guide_uses_current_single_migration_head -q`

Expected: PASS，Head 为 `20260804_0034`。

- [x] **Step 5: 提交**

```powershell
git add services/api/migrations/versions/20260804_0034_common_capture_identity.py services/api/tests/test_common_capture_migration.py services/api/tests/test_app_qc_foundation_migrations.py
git commit -m "[AI编程] feat: 增加公共 Capture 与 App Capture 表" -m "涉及模块：capture schema；数据库迁移：新增 0034 并回填 Web Capture；前端菜单/路由：无需同步；测试风险：真实大表回填锁时长需在预发压测，旧 FK 本阶段保持不变。"
```

### Task 8: Foundation 全量验证与专项回归规则

**Files:**
- Create: `services/api/tests/test_app_architecture_boundaries.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/plans/2026-08-04-app-qc-foundation.md`

**Interfaces:**
- Consumes: Tasks 1-7 的所有合同和迁移。
- Produces: 可供 App Workflow Runtime 计划依赖的已验证 Foundation 基线。

- [x] **Step 1: 增加静态边界回归测试**

在 `services/api/tests/test_app_architecture_boundaries.py` 中解析 AST，断言 `agents/explorer` 不导入 `playwright/appium/airtest/mobile_demo`，`agents/rule_judge` 不导入 `surface_driver/mobile_demo/app_runtime`，并断言 App Profile 不包含 DOM/Text/UI Tree。

- [x] **Step 2: 运行静态、lint 与目标测试**

Run: `$env:PYTHONPATH='.packages;D:\\Project\\SSC\\alioth\\.venv\\Lib\\site-packages'; & 'C:\\Users\\40389\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe' -m pytest services/api/tests/test_app_architecture_boundaries.py services/api/tests/test_task_target_contracts.py services/api/tests/test_surface_capability_manifest.py services/api/tests/test_app_bootstrap_contracts.py services/api/tests/test_capture_profile_policy.py services/api/tests/test_platform_build_service.py services/api/tests/test_app_builds_api.py services/api/tests/test_app_task_submission.py services/api/tests/test_common_capture_migration.py -q`

Expected: PASS。

Run: 对 `dev_1.5` 以来新增或修改的全部 Python 文件执行 Ruff；同时记录全目录基线问题。

Expected: 本次变更 PASS。全目录检查在 `dev_1.5` 未修改文件中存在 33 个既有 Ruff 问题，不纳入本次功能改动。

- [x] **Step 3: 更新专项规则文档**

写入以下不可回归规则：H5 URL-only 兼容；App 仅 Screenshot/Action Trace；APK 只传引用；Explorer/Judge 禁止 Driver；`qc_page_capture` Web 明细不被替换；Feature Flag 默认关闭；App Runtime 只允许 `fake/official`。

- [x] **Step 4: 运行全量测试**

Run: `$env:PYTHONPATH='.packages;D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q --basetemp '.pytest_cache\app-qc-foundation'`

Result: `1782 passed, 2 skipped`，相较现有 `1742 passed, 2 skipped` 基线新增 40 个用例且无回归。

- [x] **Step 5: 提交验证文档**

```powershell
git add docs/专项能力与高频回归规则.md docs/superpowers/plans/2026-08-04-app-qc-foundation.md services/api/tests/test_app_architecture_boundaries.py
git commit -m "[AI编程] test: 固化 App QC Foundation 回归门禁" -m "涉及模块：architecture boundary、专项回归文档；数据库迁移：无新增；前端菜单/路由：无需同步；测试风险：正式星语 HTTP API 未提供，尚未覆盖真实设备兼容性。"
```

## Self-Review Result

- Spec coverage：Foundation 覆盖 Task Target、Feature Flag、Capability、App Bootstrap Port、Platform Build、通用 Capture/App Capture 和 H5 兼容；Workflow 状态机、FakeApp、Session 激活、Screenshot Understanding、Explorer 与工作台明确留给后续两个计划。
- Placeholder scan：计划没有未定义的 TODO/TBD，也没有以“同前”替代接口、测试或错误处理。
- Type consistency：业务平台始终使用 `TaskTargetPlatform`，Runtime 始终使用 `SurfaceKind`；通用引用统一为 `CaptureRef.capture_id`，旧 Web 兼容引用保留为 `CaptureReference.page_capture_id`；APK 入口和任务队列统一使用 `platform_build_ref`。
