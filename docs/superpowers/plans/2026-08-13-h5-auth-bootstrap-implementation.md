# H5 Auth Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 H5 V2 正式探索前主动检查 Profile 登录态，按“精确域名优先、global 兜底”的配置自动执行密码登录，并把验证码/失败等降级结果可靠传播为 `needs_review`，同时确保凭证及其衍生信息不进入通用交互与证据链路。

**Architecture:** 在 `H5ExplorationWorkflow` 的首次导航与根页面识别之间插入独立 `H5AuthBootstrapService`。普通页面跳转/点击继续复用受审计的 `CaptureCoordinator`，手机号、密码和提交动作只能通过专用 `H5SecretInputRuntime`，后者在同一浏览器 Session 锁内解析受信 UI Snapshot、读取秘密、填写、提交并清空仍存在的字段；认证状态写入既有 Surface Phase 账本，根 Frontier 只携带无秘密摘要。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、asyncio、Playwright、PostgreSQL Surface Phase repository、pytest/pytest-asyncio。

## Global Constraints

- 仅覆盖 H5 V2，不改 App 流程，不新增前端菜单或路由。
- 登录方式只支持手机号加密码；短信/图形验证码、MFA、扫码均不得自动处理。
- 凭证由平台配置直接自动填写，每个任务不接收用户输入的密码。
- 配置解析优先级固定为精确 hostname > `global`，禁止通配符、后缀匹配和跨 hostname 使用。
- `global` 仅允许已授权的任务原始 hostname 使用；账号应为低权限测试账号。
- 每个任务最多提交一次密码；提交后异常恢复必须先重新观察，不得盲目重复提交。
- 登录失败或无法登录时继续游客探索，但最终状态必须是 `needs_review`，不得形成完整成功或 sealed Report。
- 手机号、密码以及秘密长度、摘要、selector、输入痕迹不得进入模型上下文、ActionIntent、任务配置、Redis、数据库、日志、证据、报告或 API 响应。
- 认证输入字段有值期间禁止采集；提交后若表单仍存在，必须先清空字段再允许普通 Capture。
- 复用 `qc_surface_phase_run` / `qc_surface_phase_step`，数据库迁移为无。
- 所有新增注释使用中文 UTF-8；文件保持单一职责。

---

## File Structure

- `services/api/app/core/h5_auth_config.py`：环境开关、JSON 配置校验、精确域名/global 解析和秘密读取边界。
- `services/api/app/domain/exploration/h5_auth_bootstrap.py`：认证状态、观察、无秘密结果与页面动作合同。
- `services/api/app/application/h5_auth_page_analyzer.py`：从 Page Detection 与受信 UI Snapshot 生成确定性认证观察。
- `services/api/app/application/h5_auth_bootstrap.py`：状态机编排、一次提交门禁、回根页和审计端口。
- `services/api/app/application/h5_secret_input.py`：专用 Secret Runtime 的命令、结果和端口，不接受明文秘密。
- `services/api/app/infrastructure/browser/h5_secret_input_runtime.py`：读取配置秘密、解析唯一 selector、持锁执行秘密表单。
- `services/api/app/infrastructure/browser/page_capture.py`：在已有 Playwright Session 上增加原子秘密填写/提交/清空能力。
- `services/api/app/application/h5_exploration_workflow.py`：在根 Frontier 前调用认证 Bootstrap，并传播无秘密摘要。
- `services/api/app/application/h5_runtime_composition.py`：生产依赖注入和 Worker 终态门禁。
- `services/api/tests/test_h5_auth_config.py`：配置优先级、拒绝非法域名与 repr 脱敏。
- `services/api/tests/test_h5_auth_page_analyzer.py`：Profile、已登录、登录入口、密码模式和验证码阻断识别。
- `services/api/tests/test_h5_auth_bootstrap.py`：状态机、一次提交、回根页和降级语义。
- `services/api/tests/test_h5_secret_input_runtime.py`：专用秘密通路、安全清空与 canary 不泄露。
- `services/api/tests/test_h5_exploration_workflow.py`：Bootstrap 插入顺序与根 Frontier 元数据。
- `services/api/tests/test_h5_runtime_composition.py`：生产装配与 `needs_review` 终态传播。
- `.env.example`、`docs/workflows/h5-v2-explorer-workflow.txt`、`docs/专项能力与高频回归规则.md`：配置与回归约束。

### Task 1: 配置合同与秘密引用

**Files:**
- Create: `services/api/app/core/h5_auth_config.py`
- Modify: `services/api/app/core/config.py`
- Modify: `.env.example`
- Test: `services/api/tests/test_h5_auth_config.py`

**Interfaces:**
- Produces: `H5AuthSettings(enabled: bool, config_path: Path | None)`。
- Produces: `H5CredentialProfileRef(scope: Literal["domain", "global"], hostname: str)`；对象不含凭证。
- Produces: `FileH5CredentialConfigResolver.resolve_ref(hostname: str) -> H5CredentialProfileRef | None`。
- Produces: `FileH5CredentialConfigResolver.read_secret(ref: H5CredentialProfileRef) -> H5CredentialSecret`；`H5CredentialSecret` 的 `repr`/`str` 不显示明文。

- [ ] **Step 1: 写配置失败测试**

  覆盖精确域名覆盖 global、无精确项时回落 global、大小写和尾点 hostname 规范化、拒绝通配符/带 scheme 或端口的 key、禁用时不读取文件、缺字段启动失败，以及 `repr/str/model_dump` 不暴露 canary `AUTH_CANARY_PHONE_8917` 和 `AUTH_CANARY_PASSWORD_6241`。

- [ ] **Step 2: 运行测试并确认红灯**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_config.py -q`

  Expected: FAIL，原因是 `services.api.app.core.h5_auth_config` 尚不存在。

- [ ] **Step 3: 实现最小配置边界**

  使用 Pydantic `SecretStr` 保存秘密，JSON 顶层只接受 `schema_version=h5-auth-config.v1`、`global` 和 `domains`。域名 key 先 `rstrip('.')` 再 `idna`/小写规范化；只允许单个 hostname。`Settings` 新增 `h5_auth`，从 `ALIOTH_H5_AUTH_ENABLED` 和 `ALIOTH_H5_AUTH_CONFIG_PATH` 构建；开关关闭时路径可空，打开时文件必须存在且合同合法。

- [ ] **Step 4: 运行配置测试和现有环境加载测试**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_config.py services/api/tests/test_config_env_loading.py -q`

  Expected: PASS。

- [ ] **Step 5: 提交配置合同**

  Commit message: `[AI编程] feat: 增加H5认证域名配置；模块：H5 Auth Config；数据库迁移：无；前端菜单路由：无；测试风险：生产配置文件权限需部署验证`

### Task 2: 认证领域状态与页面分析器

**Files:**
- Create: `services/api/app/domain/exploration/h5_auth_bootstrap.py`
- Modify: `services/api/app/domain/exploration/__init__.py`
- Create: `services/api/app/application/h5_auth_page_analyzer.py`
- Test: `services/api/tests/test_h5_auth_page_analyzer.py`

**Interfaces:**
- Produces: `H5AuthStatus`，值为 `disabled/checking/already_authenticated/authenticated/guest_only/failed`。
- Produces: `H5AuthPageKind`，值为 `unknown/profile_authenticated/profile_guest/login_password/login_switchable/login_challenge`。
- Produces: `H5AuthPageObservation(page_kind, profile_target, login_target, password_mode_target, phone_target, password_target, submit_target, reason_code)`。
- Produces: `H5AuthBootstrapResult(status, final_capture, credential_scope, reason_code, attempted, needs_review)`；序列化输出不含 selector 或秘密。
- Produces: `H5AuthPageAnalyzer.analyze(detection_result: Mapping[str, Any], ui_snapshot: Mapping[str, Any]) -> H5AuthPageObservation`。

- [ ] **Step 1: 写分析器失败测试**

  构造最小 Page Detection 和 UI Snapshot，分别断言：首页“我的”候选、Profile 已登录头像/退出登录、Profile 未登录“登录”、默认验证码但存在“密码登录”、仅验证码/MFA/扫码、完整手机号/密码表单、相似输入框多义和未知页面。断言任何观察对象都没有 `phone/password/selector` 字段。

- [ ] **Step 2: 运行测试并确认红灯**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_page_analyzer.py -q`

  Expected: FAIL，原因是领域类型和分析器尚不存在。

- [ ] **Step 3: 实现确定性分析**

  候选动作只读取 `action/target/target_keywords/purpose`，按中文和英文同义词分类 Profile、登录和密码模式按钮；输入框只读取受信 Snapshot 的 `tag/type/role/autocomplete/name/placeholder/aria_label/text`。每个语义角色必须恰好匹配一个元素，否则返回 `unknown` 或 `login_challenge`，不得把 selector 放入观察合同。

- [ ] **Step 4: 运行分析器与既有页面识别测试**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_page_analyzer.py services/api/tests/test_page_detection_tool.py -q`

  Expected: PASS。

- [ ] **Step 5: 提交领域与分析器**

  Commit message: `[AI编程] feat: 定义H5认证状态与页面分析；模块：H5 Auth Domain；数据库迁移：无；前端菜单路由：无；测试风险：真实站点文案变体需持续补充样本`

### Task 3: 专用 Secret Input Runtime

**Files:**
- Create: `services/api/app/application/h5_secret_input.py`
- Create: `services/api/app/infrastructure/browser/h5_secret_input_runtime.py`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Test: `services/api/tests/test_h5_secret_input_runtime.py`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Produces: `H5SecretInputCommand(task_id, session_id, source_capture_id, target_hostname, credential_ref, phone_semantic_target, password_semantic_target, submit_semantic_target, deadline)`；命令无明文秘密。
- Produces: `H5SecretInputResult(submitted: bool, fields_scrubbed: bool, hostname: str, reason_code: str)`。
- Produces: `H5SecretInputRuntime.execute(command: H5SecretInputCommand) -> H5SecretInputResult`。
- Produces: `UiSnapshotSemanticTargetResolver.resolve_unique_selector(...)`，只返回 Runtime 内部 selector，不持久化。
- Produces: `PlaywrightPageCaptureBrowser.submit_secret_form(...)`，在同一 Session 锁内完成 hostname 校验、填入、点击、等待和表单残留清空。

- [ ] **Step 1: 写秘密通路失败测试**

  用 fake snapshot、credential resolver 和 fake browser 断言：命令不含秘密；精确解析三个唯一目标；跨 hostname 在填入前拒绝；一次调用按 phone→password→submit 顺序执行；提交失败和表单仍存在时清空两个字段；结果、异常、日志记录、`ActionIntent`、证据 payload 中均找不到两个 canary 及其长度/sha256。

- [ ] **Step 2: 运行测试并确认红灯**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_secret_input_runtime.py -q`

  Expected: FAIL，原因是专用 Runtime 尚不存在。

- [ ] **Step 3: 实现原子秘密提交**

  `H5SecretInputRuntime` 先从 source capture 读取受信 Snapshot，再解析唯一 selector，最后在 `SessionCommandGate.hold(...)` 内读取 `SecretStr.get_secret_value()` 并立即调用浏览器方法。浏览器方法不得构造 `PageCaptureRequest`、`ActionIntent` 或 action trace；异常消息只返回固定错误码；`finally` 中对仍附着的输入框执行空字符串 fill。后续 Capture 只能在 Runtime 返回且 `fields_scrubbed=True` 后发生。

- [ ] **Step 4: 运行秘密、安全和浏览器相关测试**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_secret_input_runtime.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_capture_tool.py -q`

  Expected: PASS；输出中不出现 canary。

- [ ] **Step 5: 提交 Secret Runtime**

  Commit message: `[AI编程] feat: 增加H5密码登录秘密通路；模块：Browser Secret Runtime；数据库迁移：无；前端菜单路由：无；测试风险：真实Playwright登录控件兼容性待联调`

### Task 4: Auth Bootstrap 状态机与 Surface Phase 审计

**Files:**
- Create: `services/api/app/application/h5_auth_bootstrap.py`
- Modify: `services/api/app/infrastructure/db/surface_phase_repository.py`（仅在现有通用接口无法直接适配时）
- Test: `services/api/tests/test_h5_auth_bootstrap.py`

**Interfaces:**
- Consumes: Task 1 的 credential ref、Task 2 的 analyzer/result、Task 3 的 secret runtime。
- Produces: `H5AuthBootstrapPort.run(command: RunH5WorkflowCommand, initial_capture: CaptureRef) -> H5AuthBootstrapResult`。
- Produces: `NoopH5AuthBootstrap`，关闭开关时返回 `disabled` 且不发出动作。
- Produces: `SurfacePhaseH5AuthAudit`，phase key 固定 `h5_auth_bootstrap`，step payload 只写状态、原因码、capture id、hostname 和 credential scope。

- [ ] **Step 1: 写状态机失败测试**

  使用 fake detector/navigation/secret runtime/audit 覆盖：已登录不提交；首页主动点击 Profile；Profile 未登录点击登录；验证码页切密码模式；完整表单仅提交一次；仅验证码降级；错误密码降级；崩溃恢复看到 `attempted=True` 时只观察；跨域停止；每条路径最后导航回原始 URL；所有审计 payload 无秘密/selector。

- [ ] **Step 2: 运行测试并确认红灯**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_bootstrap.py -q`

  Expected: FAIL，原因是状态机服务尚不存在。

- [ ] **Step 3: 实现有限状态循环**

  最大普通导航步数固定为 4，密码提交预算固定为 1。每次普通点击后以 `CaptureCoordinator` 获得新 capture 并重新 detect/analyze；提交后只在字段已清空时进行 OBSERVE。状态结束后导航回 `command.target_url`，返回最终根 capture；任何可恢复失败映射 `guest_only` 或 `failed` 且 `needs_review=True`。

- [ ] **Step 4: 接入通用 Surface Phase 账本**

  `start_run/record_step/finish_run` 只持久化固定枚举和引用，禁止保存 candidate 原文、selector、手机号、密码、长度或摘要。重复启动按 task/session/phase 幂等读取已提交事实，避免重复提交。

- [ ] **Step 5: 运行状态机与账本测试**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_bootstrap.py services/api/tests/test_surface_phase_repository.py services/api/tests/test_surface_phase_explorer.py -q`

  Expected: PASS。

- [ ] **Step 6: 提交状态机**

  Commit message: `[AI编程] feat: 编排H5主动登录状态机；模块：H5 Auth Bootstrap；数据库迁移：无、复用Surface Phase；前端菜单路由：无；测试风险：分布式崩溃恢复需真实PG验证`

### Task 5: H5 Workflow 主链与终态传播

**Files:**
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/application/app_workflow_dispatch.py`（仅当 `RunResult` 需要增加无秘密 metadata）
- Test: `services/api/tests/test_h5_exploration_workflow.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`
- Test: `services/api/tests/test_worker_h5_v2_finalization.py`

**Interfaces:**
- `H5ExplorationWorkflow.__init__` 新增 `auth_bootstrap: H5AuthBootstrapPort`。
- `H5ExplorationResult` 新增 `auth_result: H5AuthBootstrapResult`。
- 根 Frontier `lineage_metadata["h5_auth"]` 只包含 `status/credential_scope/reason_code/attempted/needs_review`。
- `ProductionH5V2RuntimeExecutor` 保存 `runtime.start()` 的 auth result；`guest_only/failed` 强制返回 `ExecutionOutcome.NEEDS_REVIEW`，即使 Explorer 局部完成。

- [ ] **Step 1: 写 Workflow/Worker 失败测试**

  断言调用顺序为 bootstrap navigation→auth bootstrap→root detect→frontier；root source capture 是认证结束回根页 capture；关闭认证行为与现状一致；`guest_only/failed` 强制 needs_review；`already_authenticated/authenticated/disabled` 不覆盖 Explorer 原终态；输出 JSON 不含 canary 或 selector。

- [ ] **Step 2: 运行测试并确认红灯**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_finalization.py -q`

  Expected: FAIL，原因是 Workflow 尚未注入认证结果。

- [ ] **Step 3: 接入 Workflow 和 Frontier**

  首次导航完成后立即调用 auth bootstrap；只对 `auth_result.final_capture` 做正式根识别。认证临时页面的 capture 不进入根 branch lineage，根 lineage 只带无秘密认证摘要。

- [ ] **Step 4: 接入生产组合和 Completion Gate**

  `PostgresH5RuntimeProvider` 根据 `settings.h5_auth.enabled` 组装 noop 或生产实现，共享 Web Runtime 的 SessionCommandGate、snapshot reader 和浏览器。Executor 将认证降级转换成 `needs_review`，并在异常/关闭路径保留原有行为。

- [ ] **Step 5: 运行 H5 全链相关测试**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_finalization.py -q`

  Expected: PASS。

- [ ] **Step 6: 提交主链集成**

  Commit message: `[AI编程] feat: 接入H5主动登录与终态门禁；模块：H5 Workflow/Worker；数据库迁移：无；前端菜单路由：无；测试风险：真实队列恢复和报告封存待集成环境验证`

### Task 6: 文档与高频回归规则

**Files:**
- Modify: `docs/workflows/h5-v2-explorer-workflow.txt`
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/specs/2026-08-13-h5-auth-bootstrap-design.md`（仅在实现合同与设计存在已确认偏差时）

- [ ] **Step 1: 更新 Workflow 文档**

  写清 ACTIVE Profile 检查、已登录短路、登录入口跳转、密码模式切换、专用秘密填写、验证码降级、回根页和 root Frontier 的顺序；标注只支持 H5 和每任务一次提交。

- [ ] **Step 2: 更新专项回归规则**

  增加“秘密不得进入通用 ActionIntent/Capture”“有值期间禁止采集”“精确域名优先/global 兜底”“跨域禁止”“auth 降级强制 needs_review”“恢复先观察不重提”的必测清单。

- [ ] **Step 3: 扫描文档一致性**

  Run: `rg -n "H5Auth|h5_auth|主动登录|验证码|needs_review|Secret" docs services/api/app services/api/tests .env.example`

  Expected: 配置名、phase key、状态枚举和终态语义一致，无旧流程宣称根探索先于认证。

- [ ] **Step 4: 提交文档**

  Commit message: `[AI编程] doc: 更新H5主动登录Workflow与回归规则；模块：H5 Explorer；数据库迁移：无；前端菜单路由：无；测试风险：真实站点验收矩阵尚需部署环境执行`

### Task 7: 完整验证、秘密扫描与合并

**Files:**
- Review: all files changed from `dev_1.5...codex/h5-auth-bootstrap`

- [ ] **Step 1: 运行格式、静态检查和目标测试**

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check services/api/app services/api/tests`

  Run: `$env:PYTHONPATH='D:\Project\SSC\alioth\.venv\Lib\site-packages'; & 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_auth_config.py services/api/tests/test_h5_auth_page_analyzer.py services/api/tests/test_h5_secret_input_runtime.py services/api/tests/test_h5_auth_bootstrap.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_surface_phase_repository.py services/api/tests/test_surface_phase_explorer.py -q`

  Expected: 全部 PASS；若仓库未安装 ruff，记录环境限制并使用项目现有 lint 命令。

- [ ] **Step 2: 执行秘密 canary 与结构扫描**

  Run: 使用仅存在于本地验证环境的已知示例凭证值扫描 `services/api/app`、`docs` 与 `.env.example`；扫描命令和结果不得把值本身写入仓库。

  Expected: 生产代码、配置示例、文档中无真实示例秘密；canary 只允许存在于隔离安全测试源码，测试生成的日志/序列化/证据输出不得命中。

- [ ] **Step 3: 仔细审查 diff**

  Run: `git diff --check dev_1.5...HEAD`

  Run: `git diff --stat dev_1.5...HEAD; git diff --name-status dev_1.5...HEAD`

  逐文件验证：无秘密落盘、无无界循环、无跨域、无重复提交、无认证临时页面进入根 Frontier、无 App/前端意外改动、无数据库迁移。

- [ ] **Step 4: 提交最终审查修正**

  仅当审查产生修改时提交：`[AI编程] fix: 收紧H5认证安全与回归；模块：H5 Auth Bootstrap；数据库迁移：无；前端菜单路由：无；测试风险：真实目标站点仍需部署验收`

- [ ] **Step 5: 在主工作区合并，不推送**

  先确认主工作区仍在 `dev_1.5` 且用户未跟踪文件不受影响，再运行 `git merge --no-ff codex/h5-auth-bootstrap`。合并后重跑目标测试和 `git status --short --branch`；不得运行 `git push`。
