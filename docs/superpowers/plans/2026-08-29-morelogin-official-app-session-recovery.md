# MoreLogin Official App Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复 Docker 内 Official App Session 的 MoreLogin 设备控制通道，并让任务在提交前可检测、失败后可在观察室定位到真实阶段。

**Architecture:** 运行侧优先使用 MoreLogin Open API，避免 Docker 依赖 Windows 桌面客户端；确需 Local API 时通过 `host.docker.internal:40000` 访问。代码侧在 MoreLogin HTTP 边界生成受控原因码，经 Xingyu Official API 和 Official Adapter 透传到 App Failure Fact；工作台把故障展示在实际 `bootstrap` 阶段。

**Tech Stack:** Python 3.12、FastAPI、httpx/urllib、PostgreSQL、Redis、Docker Compose、Vue 3、TypeScript、Vitest、pytest

**Spec:** `docs/专项能力与高频回归规则.md`

## Global Constraints

- 数据库固定使用 `postgresql+asyncpg://alioth:***@alioth-postgres:5432/alioth`，Redis 固定使用 `redis://alioth-redis:6379/0`。
- MoreLogin API ID、API Key 与 Official App 共享令牌只能进入 gitignore 的本机环境文件，禁止写入仓库和日志。
- 本机 API 继续绑定 `127.0.0.1:8000`；新增 Compose 覆盖不得引入 `0.0.0.0` 端口。
- App 正式链使用 `ALIOTH_WORKFLOW_VERSION=v2`、`ALIOTH_APP_RUNTIME_PROVIDER=official`、`ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres`。
- 注释和文档使用 UTF-8 中文；不引入数据库迁移，不新增前端菜单或路由。
- 真实云手机验证会产生开机计费；只在配置、鉴权和只读状态检查通过后执行任务。

## Root Cause Baseline

- 失败任务：`44655c12-6fb0-4b69-858c-361154436f97`。
- 失败阶段：`bootstrap`，原因码 `OFFICIAL_HTTP_503`。
- 当前模式：`ALIOTH_MORELOGIN_API_MODE=auto`；API ID/Key 未成对配置，因此解析为 `local`。
- Docker 内地址：`http://host.docker.internal:40000`；宿主机端口 40000 不可达。
- 结果：Official `/v1/sessions` 返回 503，任务在 APK 安装、页面采集和 LLM 调用前终止。

---

### Task 1: 固化安全的本地 App QC Compose 覆盖

**Files:**
- Create: `infra/docker-compose.app-qc.local.yml`
- Modify: `infra/env/.env.local.example`
- Test: `services/api/tests/test_production_deployment_files.py`

**Interfaces:**
- Consumes: `infra/docker-compose.yml` 中的 API、Worker、PostgreSQL、Redis 服务。
- Produces: 不新增公网端口、显式注入 Official/MoreLogin/设备池配置的本地覆盖文件。

- [ ] **Step 1: 写失败的部署文件测试**

```python
def test_app_qc_local_overlay_is_loopback_safe_and_uses_shared_token() -> None:
    text = Path("infra/docker-compose.app-qc.local.yml").read_text(encoding="utf-8")
    assert "0.0.0.0" not in text
    assert "ALIOTH_OFFICIAL_APP_SHARED_TOKEN" in text
    assert "ALIOTH_MORELOGIN_API_MODE" in text
    assert "ALIOTH_MORELOGIN_API_ID" in text
    assert "ALIOTH_MORELOGIN_API_KEY" in text
    assert "host.docker.internal:40000" in text
    assert "alioth-postgres:5432/alioth" in text
    assert "alioth-redis:6379/0" in text
```

- [ ] **Step 2: 运行测试并确认缺少覆盖文件而失败**

Run: `pytest services/api/tests/test_production_deployment_files.py -q`

Expected: FAIL，指出 `infra/docker-compose.app-qc.local.yml` 不存在。

- [ ] **Step 3: 创建最小安全覆盖文件**

```yaml
x-alioth-app-qc-env: &alioth-app-qc-env
  ALIOTH_DATABASE_URL: postgresql+asyncpg://alioth:alioth-local-password@alioth-postgres:5432/alioth
  ALIOTH_REDIS_URL: redis://alioth-redis:6379/0
  ALIOTH_WORKFLOW_VERSION: v2
  ALIOTH_APP_RUNTIME_PROVIDER: official
  ALIOTH_APP_QC_ENABLED: "true"
  ALIOTH_APP_DEVICE_LEASE_BACKEND: postgres
  ALIOTH_DEVICE_BACKEND: cloud_phone
  ALIOTH_DEVICE_POOL_SLOTS: ${ALIOTH_DEVICE_POOL_SLOTS}
  ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS: ${ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS}
  ALIOTH_MORELOGIN_API_MODE: ${ALIOTH_MORELOGIN_API_MODE:-open}
  ALIOTH_MORELOGIN_OPEN_API: ${ALIOTH_MORELOGIN_OPEN_API:-https://api.morelogin.com}
  ALIOTH_MORELOGIN_API_ID: ${ALIOTH_MORELOGIN_API_ID:-}
  ALIOTH_MORELOGIN_API_KEY: ${ALIOTH_MORELOGIN_API_KEY:-}
  ALIOTH_MORELOGIN_LOCAL_API: http://host.docker.internal:40000
  ALIOTH_MORELOGIN_ADB_AUTO_REFRESH: "1"
  ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON: "1"
  ALIOTH_CLOUD_PHONE_ADB_LOOPBACK_HOST: host.docker.internal
  ANDROID_HOME: /opt/android-sdk
  ANDROID_SDK_ROOT: /opt/android-sdk

services:
  alioth-api:
    environment:
      <<: *alioth-app-qc-env
      ALIOTH_XINGYU_OFFICIAL_ENABLED: "1"
      ALIOTH_XINGYU_OFFICIAL_TOKEN: ${ALIOTH_OFFICIAL_APP_SHARED_TOKEN:?必须配置 Official App 共享令牌}
    extra_hosts:
      - "host.docker.internal:host-gateway"

  alioth-worker:
    environment:
      <<: *alioth-app-qc-env
      ALIOTH_OFFICIAL_APP_AUTH_TOKEN: ${ALIOTH_OFFICIAL_APP_SHARED_TOKEN:?必须配置 Official App 共享令牌}
      ALIOTH_OFFICIAL_APP_API_BASE_URL: http://alioth-api:8000
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

- [ ] **Step 4: 在环境样例中补齐非敏感说明**

在 `infra/env/.env.local.example` 增加：

```dotenv
# 使用 64 位以上随机字符串；真实值禁止提交。
ALIOTH_OFFICIAL_APP_SHARED_TOKEN=
# Docker 推荐显式 open；只有宿主机 MoreLogin Local API 在线时才使用 local。
ALIOTH_MORELOGIN_API_MODE=open
```

- [ ] **Step 5: 运行部署文件测试**

Run: `pytest services/api/tests/test_production_deployment_files.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add infra/docker-compose.app-qc.local.yml infra/env/.env.local.example services/api/tests/test_production_deployment_files.py
git commit -m "[AI编程] fix(app-runtime): 固化本地Official与MoreLogin配置；无数据库迁移；无需菜单路由；真实云手机仍需验收"
```

### Task 2: 为 MoreLogin 连接失败生成稳定原因码

**Files:**
- Modify: `services/api/app/infrastructure/device/morelogin_http.py`
- Modify: `services/api/app/application/xingyu_runtime/service.py`
- Test: `services/api/tests/test_morelogin_adb_refresh.py`
- Test: `services/api/tests/test_xingyu_official.py`

**Interfaces:**
- Produces: `MoreLoginTransportError(reason_code, transport_kind)`。
- Consumes: Xingyu `start_session` 捕获该错误并返回相同受控 `error_code`，不回传 URL、凭据或第三方响应正文。

- [ ] **Step 1: 写失败测试**

```python
def test_local_connection_failure_has_stable_reason_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "local")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionRefusedError()))
    with pytest.raises(http.MoreLoginTransportError) as captured:
        http.post_morelogin_json("http://127.0.0.1:40000/api/cloudphone/page", {})
    assert captured.value.reason_code == "MORELOGIN_LOCAL_API_UNREACHABLE"
    assert captured.value.transport_kind == "local"
```

同时在 `test_xingyu_official.py` 断言 `/v1/sessions` 返回 503 时：

```python
assert response.json()["error_code"] == "MORELOGIN_LOCAL_API_UNREACHABLE"
assert "40000" not in response.json()["message"]
```

- [ ] **Step 2: 运行两个定向测试并确认失败**

Run: `pytest services/api/tests/test_morelogin_adb_refresh.py services/api/tests/test_xingyu_official.py -q`

Expected: FAIL，当前只存在 `MoreLoginAdbError`，Session 只返回 `SESSION_START_FAILED`。

- [ ] **Step 3: 实现受控传输错误**

```python
class MoreLoginTransportError(MoreLoginAdbError):
    """MoreLogin 传输不可用；仅暴露稳定原因码和传输类型。"""

    def __init__(self, *, reason_code: str, transport_kind: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.transport_kind = transport_kind
```

在 `_http_json` 的网络异常分支按 `morelogin_transport_kind(url)` 映射：

```python
kind = morelogin_transport_kind(url)
code = "MORELOGIN_LOCAL_API_UNREACHABLE" if kind == "local" else "MORELOGIN_OPEN_API_UNREACHABLE"
raise MoreLoginTransportError(reason_code=code, transport_kind=kind) from exc
```

- [ ] **Step 4: 在 Session 边界保留原因码**

```python
except MoreLoginTransportError as exc:
    raise XingyuOfficialError(
        "MoreLogin 设备控制通道不可用",
        error_code=exc.reason_code,
        retryable=True,
        status_code=503,
        request_id=request_id,
    ) from exc
```

- [ ] **Step 5: 运行定向测试**

Run: `pytest services/api/tests/test_morelogin_adb_refresh.py services/api/tests/test_xingyu_official.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add services/api/app/infrastructure/device/morelogin_http.py services/api/app/application/xingyu_runtime/service.py services/api/tests/test_morelogin_adb_refresh.py services/api/tests/test_xingyu_official.py
git commit -m "[AI编程] fix(app-runtime): 分类MoreLogin传输失败；无数据库迁移；无需菜单路由；第三方错误正文不透传"
```

### Task 3: Official Adapter 安全透传上游原因码

**Files:**
- Modify: `services/api/app/infrastructure/app_runtime/official_api.py`
- Test: `services/api/tests/test_official_app_api_contract.py`

**Interfaces:**
- Consumes: Xingyu Official JSON 错误体中的 `error_code`、`retryable`、`request_id`。
- Produces: 仅允许白名单错误码进入 `OfficialAppApiError`；未知值仍降级为 `OFFICIAL_HTTP_<status>`。

- [ ] **Step 1: 写失败测试**

```python
async def test_start_session_preserves_allowlisted_morelogin_reason_code() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            503,
            json={
                "error_code": "MORELOGIN_LOCAL_API_UNREACHABLE",
                "retryable": True,
                "request_id": "req-safe",
                "message": "受控消息",
            },
        )
    )
    with pytest.raises(OfficialAppApiError) as captured:
        await _client(transport).start_session(_spec())
    assert captured.value.error_code == "MORELOGIN_LOCAL_API_UNREACHABLE"
    assert captured.value.request_id == "req-safe"
```

另加未知错误码用例，断言仍为 `OFFICIAL_HTTP_503`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `pytest services/api/tests/test_official_app_api_contract.py -q`

Expected: FAIL，当前 `_http_status_error` 忽略 JSON 错误体。

- [ ] **Step 3: 实现有界解析**

```python
_ALLOWED_REMOTE_ERROR_CODES = {
    "MORELOGIN_LOCAL_API_UNREACHABLE",
    "MORELOGIN_OPEN_API_UNREACHABLE",
    "MORELOGIN_AUTH_FAILED",
    "MORELOGIN_DEVICE_NOT_FOUND",
    "SESSION_START_FAILED",
}
```

`_http_status_error` 只读取小型 JSON 对象，并仅采用白名单 `error_code` 与合法 `request_id`；不得把响应 `message` 写入运行时异常。

- [ ] **Step 4: 运行测试**

Run: `pytest services/api/tests/test_official_app_api_contract.py services/api/tests/test_official_app_runtime.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/infrastructure/app_runtime/official_api.py services/api/tests/test_official_app_api_contract.py
git commit -m "[AI编程] fix(app-adapter): 受控透传Official上游原因码；无数据库迁移；无需菜单路由；未知错误保持通用码"
```

### Task 4: 增加提交前 App Runtime Readiness 检查

**Files:**
- Modify: `services/api/app/api/v1/workstation/system.py`
- Test: `services/api/tests/test_system_status.py`
- Modify: `app/workstation/src/services/system.api.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskCreateView.vue`
- Test: `app/workstation/src/modules/tasks/pages/TaskCreateView.test.ts`

**Interfaces:**
- Produces: `GET /api/v1/workstation/system/app-runtime-readiness`。
- Response: `{ready, provider, transport_mode, reason_code, checked_at}`；不返回 URL、API ID、Key、令牌或 ADB 授权码。

- [ ] **Step 1: 写 API 失败测试**

```python
def test_app_runtime_readiness_reports_local_api_unreachable(test_client, monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "local")
    monkeypatch.setattr(system, "probe_morelogin_readiness", lambda: (False, "MORELOGIN_LOCAL_API_UNREACHABLE"))
    response = test_client.get("/api/v1/workstation/system/app-runtime-readiness")
    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["reason_code"] == "MORELOGIN_LOCAL_API_UNREACHABLE"
```

- [ ] **Step 2: 实现只读检查端点**

端点通过 `asyncio.to_thread` 执行最多 3 秒的只读 `cloudphone/page` 检查；Open API 校验 OAuth 与目标 envId 可见，Local API 校验客户端端口和目标 envId 可见。结果缓存 15 秒，避免前端轮询冲击第三方 API。

- [ ] **Step 3: 前端在创建 App 任务前阻断不可运行状态**

```ts
export interface AppRuntimeReadiness {
  ready: boolean
  provider: 'official'
  transport_mode: 'open' | 'local'
  reason_code: string | null
  checked_at: string
}
```

当 `ready=false` 时禁用 App 提交按钮，并展示：`MoreLogin 设备通道不可用（<reason_code>），请先恢复后再提交。`

- [ ] **Step 4: 运行后端与前端测试**

Run: `pytest services/api/tests/test_system_status.py -q`

Run: `npm test -- --run src/modules/tasks/pages/TaskCreateView.test.ts`（工作目录 `app/workstation`）

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/api/app/api/v1/workstation/system.py services/api/tests/test_system_status.py app/workstation/src/services/system.api.ts app/workstation/src/modules/tasks/pages/TaskCreateView.vue app/workstation/src/modules/tasks/pages/TaskCreateView.test.ts
git commit -m "[AI编程] feat(app-runtime): 提交前检查设备通道；无数据库迁移；前端复用现有任务路由；外部API需实连验收"
```

### Task 5: 观察室按真实阶段显示技术失败

**Files:**
- Modify: `app/workstation/src/modules/tasks/components/AppStagePanel.vue`
- Test: `app/workstation/src/modules/tasks/components/AppStagePanel.test.ts`

**Interfaces:**
- Consumes: `app_exploration.technical_failure.phase/reason_code/error_type/recovery_state`。
- Produces: Bootstrap 故障在第 02 卡显示，Evidence 故障才在第 05 卡显示；顶部状态仍显示“技术失败”。

- [ ] **Step 1: 写失败测试**

```ts
it('将 bootstrap 技术失败显示在 Bootstrap 卡片', () => {
  const wrapper = mount(AppStagePanel, {
    props: {
      stage: stage({
        status: 'technical_failed',
        terminal_reason_code: 'MORELOGIN_LOCAL_API_UNREACHABLE',
        technical_failure: {
          phase: 'bootstrap',
          reason_code: 'MORELOGIN_LOCAL_API_UNREACHABLE',
          error_type: 'MoreLoginTransportError',
          recovery_state: 'pending',
        },
      }),
    },
  })
  expect(wrapper.get('[data-testid="bootstrap-stage"]').text()).toContain('MoreLogin 本机接口不可达')
  expect(wrapper.get('[data-testid="evidence-validation-stage"]').text()).not.toContain('MoreLogin 本机接口不可达')
})
```

- [ ] **Step 2: 给阶段卡增加稳定 test id 与错误码文案映射**

```ts
const failureLabels: Record<string, string> = {
  MORELOGIN_LOCAL_API_UNREACHABLE: 'MoreLogin 本机接口不可达',
  MORELOGIN_OPEN_API_UNREACHABLE: 'MoreLogin Open API 不可达',
  MORELOGIN_AUTH_FAILED: 'MoreLogin Open API 鉴权失败',
  MORELOGIN_DEVICE_NOT_FOUND: 'MoreLogin 云手机不存在或无权访问',
}
```

Bootstrap 卡仅渲染 `technical_failure.phase === 'bootstrap'`；Evidence 卡仅渲染 evidence/index/seal 阶段故障。

- [ ] **Step 3: 运行组件测试**

Run: `npm test -- --run src/modules/tasks/components/AppStagePanel.test.ts`（工作目录 `app/workstation`）

Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add app/workstation/src/modules/tasks/components/AppStagePanel.vue app/workstation/src/modules/tasks/components/AppStagePanel.test.ts
git commit -m "[AI编程] fix(workstation): 按真实阶段展示App技术失败；无数据库迁移；无需菜单路由；旧任务通用码继续兼容"
```

### Task 6: 配置恢复与端到端验收

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Runtime-only, ignored: `infra/env/.env.local`

**Interfaces:**
- Consumes: Task 1 的安全 Compose 覆盖、MoreLogin Open API 凭据或可达的 Local API。
- Produces: 同一 APK 至少建立 Session、安装并采集一个页面；失败时观察室显示具体受控原因码。

- [ ] **Step 1: 选择唯一传输路径**

推荐 Open API，在 `infra/env/.env.local` 设置真实值：

```dotenv
ALIOTH_MORELOGIN_API_MODE=open
ALIOTH_MORELOGIN_OPEN_API=https://api.morelogin.com
ALIOTH_MORELOGIN_API_ID=<MoreLogin 开发者 API ID>
ALIOTH_MORELOGIN_API_KEY=<MoreLogin 开发者 API Key>
ALIOTH_OFFICIAL_APP_SHARED_TOKEN=<至少 64 位随机字符串>
```

若没有 Open API 凭据，则启动 Windows MoreLogin 客户端并开启 Local API，再设置：

```dotenv
ALIOTH_MORELOGIN_API_MODE=local
ALIOTH_OFFICIAL_APP_SHARED_TOKEN=<至少 64 位随机字符串>
```

Local 路径必须同时满足宿主机 `127.0.0.1:40000` 和容器 `host.docker.internal:40000` 可访问。

- [ ] **Step 2: 校验 Compose 并构建重启**

```powershell
docker compose --env-file infra/env/.env.local -f infra/docker-compose.yml -f infra/docker-compose.app-qc.local.yml config
docker compose --env-file infra/env/.env.local -f infra/docker-compose.yml -f infra/docker-compose.app-qc.local.yml build alioth-api alioth-worker
docker compose --env-file infra/env/.env.local -f infra/docker-compose.yml -f infra/docker-compose.app-qc.local.yml up -d --force-recreate alioth-api alioth-worker
```

Expected: API/Worker 使用同一新镜像；API 健康，PostgreSQL healthy，Redis `PONG`。

- [ ] **Step 3: 只读验证 MoreLogin 模式和目标设备**

```powershell
docker exec alioth-worker python -c "from services.api.app.infrastructure.device.morelogin_http import resolve_morelogin_api_mode; print(resolve_morelogin_api_mode())"
docker exec alioth-worker python -c "from services.api.app.infrastructure.device.morelogin_adb import fetch_morelogin_phone_status; s=fetch_morelogin_phone_status('1716966721435348'); print({'env_id':s.env_id,'env_status':s.env_status,'enable_adb':s.enable_adb})"
```

Expected: 第一条输出选定模式；第二条只输出非敏感设备状态，不抛连接、鉴权或设备不存在错误。

- [ ] **Step 4: 运行回归测试**

```powershell
pytest services/api/tests/test_morelogin_device_routing.py services/api/tests/test_morelogin_adb_refresh.py services/api/tests/test_xingyu_official.py services/api/tests/test_official_app_api_contract.py services/api/tests/test_official_app_runtime.py services/api/tests/test_system_status.py services/api/tests/test_app_device_lease.py services/api/tests/test_device_wait.py services/api/tests/test_app_qc_workflow.py services/api/tests/test_production_deployment_files.py -q
```

```powershell
Set-Location app/workstation
npm test -- --run src/modules/tasks/components/AppStagePanel.test.ts src/modules/tasks/pages/TaskCreateView.test.ts
```

Expected: 全部 PASS。

- [ ] **Step 5: 用已上传 APK 重跑 V2 探索任务**

复用：

```text
app-build:d0759e3d-e96f-419c-8fda-58a290a83065
package_name=com.qiku.id
device=cloud_phone:qa-a
```

验收条件：

- `/api/v2/exploration-tasks` 返回新 `task_id/run_id`。
- App Stage 离开 `bootstrap`，产生 installed package、页面节点或 Capture。
- 不再出现 `OFFICIAL_HTTP_503` 或 `MORELOGIN_*_UNREACHABLE`。
- 页面时间线至少 1 个页面；Evidence Index 按任务终态生成或明确给出证据缺口。
- `qc_llm_usage_event`、页面覆盖和规则结果按实际执行记录，不允许伪造非零值。

- [ ] **Step 6: 更新专项回归规则并提交**

在 `docs/专项能力与高频回归规则.md` 记录：Open/Local 模式选择、Docker 地址差异、提交前 readiness、受控原因码、观察室阶段归属和真实设备验收边界。

```bash
git add docs/专项能力与高频回归规则.md
git commit -m "[AI编程] docs(app-runtime): 固化MoreLogin连通性回归；无数据库迁移；无需菜单路由；真实云手机计费验收已注明"
```

## Self-Review

- Spec coverage: 覆盖本地 PostgreSQL/Redis、Official Runtime、MoreLogin Open/Local 两种模式、共享令牌、提交前检查、错误码、观察室定位和端到端验收。
- Placeholder scan: 尖括号只代表必须由用户安全提供且禁止入库的真实凭据，不属于实现占位代码。
- Type consistency: `MoreLoginTransportError.reason_code` 从 HTTP 边界进入 Xingyu 错误体，再由 Official Adapter 白名单读取，最终对应 `technical_failure.reason_code` 和前端文案映射。
- Migration/menu: 无数据库迁移；不新增前端菜单和路由。

