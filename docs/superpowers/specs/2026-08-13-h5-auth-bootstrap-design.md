# H5 主动登录 Bootstrap Workflow 设计

日期：2026-08-13
状态：已实现并通过代码级回归，待真实 PostgreSQL + Playwright 联调
范围：仅 H5 V2，覆盖 `full_inspection` 与 `exploration_only`

## 1. 背景与目标

当前 H5 V2 能识别登录墙、关闭登录弹窗、选择游客入口，并在确需账号密码时输出 `requires_auth`，但不能主动登录。其直接结果是：需要登录后才能访问的 Profile、会员内容或个人业务页面无法进入，探索终态只能是阻断或人工复核。

本次增加独立的 `H5 Authentication Bootstrap` 前置阶段。在正式根 Frontier 创建前，Workflow 主动进入“我的/Profile/账户”页面判断登录态；未登录时识别登录入口，切换到密码登录模式，由受控 Runtime 使用预配置账号自动填写手机号和密码并提交。登录成功后回到任务原始 URL，再开始正式 H5 探索。

目标如下：

1. H5 任务启动后主动确认登录态，而不是等探索过程中碰到登录墙。
2. 已登录时不重复登录；未登录时自动执行一次受控密码登录。
3. 默认验证码登录但提供“密码登录/账号登录”切换入口时，允许切换后登录。
4. 仅支持短信、图形验证码、MFA、扫码或人工授权时不自动处理验证码，转游客探索并保留 `needs_review` 语义。
5. LLM 只识别页面状态与控件语义，手机号和密码只在受控 Runtime 内存中可见。
6. 登录失败后继续探索游客可访问范围，但任务不能标记为完整覆盖。
7. 登录阶段具备可恢复、可审计、幂等和敏感信息不落盘的完整边界。

## 2. 范围边界

### 2.1 包含

- H5 V2 `full_inspection` 与 `exploration_only` 共用的登录前置阶段。
- 主动寻找 Profile 入口、判断登录态、寻找登录入口和切换密码登录模式。
- 使用配置文件中的 global 账号，及按任务目标域名配置的覆盖账号。
- 精确域名优先、global 兜底的配置解析规则。
- 手机号框、密码框和登录按钮的结构化识别与受控 Runtime 执行。
- 登录成功、已登录、验证码阻断、配置缺失、登录失败和状态不明的终态语义。
- 登录步骤审计、敏感证据清理、失败后游客探索和最终 `needs_review` 门禁。
- H5 Workflow、专项回归规则和纯文本 Workflow 文档同步更新。

### 2.2 不包含

- App、Web 桌面端或 H5 V1 登录能力。
- 自动读取、发送或识别短信、邮件、图形验证码。
- MFA、扫码登录、设备确认或人工授权自动化。
- 后台管理页面、数据库凭证库、凭证轮换 UI 或用户在任务创建时输入账号密码。
- 跨域 SSO、第三方 OAuth 登录、社交账号登录。
- 注册、找回密码、绑定手机号、退出登录或账号注销。
- 将通用 Explorer 的安全边界放宽为可自由输入或提交表单。

## 3. 已确认的产品决策

1. 本次只覆盖 H5。
2. 任务创建接口不接收手机号和密码，用户无需填写登录信息。
3. 账号来自 Worker 侧配置，前期不建设后台凭证库。
4. 先支持 global 全局账号，同时预留按 H5 任务目标域名覆盖的配置。
5. 账号选择顺序为：任务目标精确域名配置优先，未命中使用 global；二者都没有时不填写任何表单。
6. 登录在正式探索之前主动执行。
7. Profile 页未登录时，由 LLM 从当前页面候选中识别登录入口；Runtime 校验并点击进入登录页。
8. 若默认展示验证码登录，但存在“密码登录/账号登录”入口，允许切换。
9. 若只能使用验证码、MFA、扫码或人工授权，则不自动登录。
10. 登录失败后继续游客范围探索，最终固定为 `incomplete/needs_review`。

## 4. 方案选择

### 4.1 采用：独立 H5 Authentication Bootstrap

登录发生在初始导航之后、正式根 Frontier 创建之前。登录阶段拥有独立协议、状态机和审计步骤；登录成功或失败收口后，Workflow 都受控返回任务原始 URL，再根据新采集创建根 Frontier。

选择原因：

- 登录与正式页面探索职责分离。
- 登录前产生的导航和采集不会污染正式覆盖率与页面谱系。
- 密码不需要进入 Explorer、ActionIntent 或模型上下文。
- 登录失败、崩溃恢复和任务终态可以由独立账本约束。
- 与当前 H5 V2 的 `bootstrap -> detection -> frontier -> work` 顺序兼容，只需在 detection/frontier 前插入认证阶段。

### 4.2 不采用：只扩展探索中的 `auth_gate`

现有 `auth_gate` 适合处理探索途中遇到的登录墙，但无法保证任务开始时主动进入 Profile；登录前后 Frontier、覆盖率和恢复分支也会混在一起。因此它保留为探索期间的兜底门禁，不作为主动登录主链。

### 4.3 不采用：预制已登录 Session

预制 Session 无法满足“自动进入登录页面并填写配置账号”的要求，也无法验证登录页面和账号有效性；Session 失效后仍需本次设计中的登录状态机。

## 5. 总体架构

新增四个职责单一的组件：

### 5.1 `H5AuthBootstrapService`

应用层编排器，负责：

- 在初始 H5 导航后启动认证阶段；
- 调用页面分析器获得结构化页面状态和候选；
- 选择“进入 Profile、进入登录页、切换密码模式、提交凭证、重新观察、返回首页”等下一步；
- 将每一步写入 Surface Phase 账本；
- 输出不含凭证的 `H5AuthBootstrapResult`；
- 禁止直接访问 Playwright、数据库密码正文或 LLM 客户端。

### 5.2 `H5AuthPageAnalyzer`

页面状态分析器，复用当前 H5 Capture、DOM、可见文本、UI Snapshot 与 LLM 页面理解能力，输出严格结构化结果：

- `auth_state`：`logged_in`、`logged_out`、`login_page`、`verification_only`、`unknown`；
- Profile 入口候选；
- 登录入口候选；
- 密码登录切换候选；
- 手机号输入框候选；
- 密码输入框候选；
- 登录提交按钮候选；
- 登录失败提示与置信度；
- 来源 Capture ID 和稳定候选 ID。

分析器不得接收、读取或返回手机号、密码、Cookie、Token 和浏览器存储。页面中的自然语言仍视为不可信数据，不能改变 Workflow 策略。

### 5.3 `H5CredentialConfigResolver`

配置解析器只根据任务原始 URL 的规范化 hostname 选择凭证引用：

1. 精确域名配置；
2. global 配置；
3. 无配置。

编排器只能得到 `profile_scope=domain|global|none` 与不透明的 profile ref，不能得到手机号和密码正文。

### 5.4 `H5SecretInputRuntime`

唯一可以解析 profile ref 并读取手机号/密码正文的基础设施适配器，负责：

- 在有效 Surface Session Lease 与 Session Command Permit 下执行；
- 将稳定候选 ID 重新绑定为当前页面唯一输入框和按钮；
- 在同一受控调用内填写手机号、填写密码、点击登录并等待有限时间；
- 生成不含正文、长度和哈希的脱敏动作结果；
- 在任何持久化采集前完成敏感字段清理或遮盖；
- 不通过通用 `InteractionOperation.INPUT` 和普通 ActionIntent 传递秘密。

## 6. 配置设计

### 6.1 启用方式

新增部署配置：

```text
ALIOTH_H5_AUTH_ENABLED=true
ALIOTH_H5_AUTH_CONFIG_PATH=/run/secrets/alioth-h5-auth.json
```

默认 `ALIOTH_H5_AUTH_ENABLED=false`，关闭时保持现有 H5 V2 行为。启用时 `ALIOTH_H5_AUTH_CONFIG_PATH` 必须显式配置且文件必须可读；文件不存在、JSON 非法或字段不完整属于部署错误，Worker 组合根构建失败，不静默回退。

`.env.example` 只记录变量名和假值示例，不保存真实手机号、密码或本机绝对秘密路径。真实配置文件不得提交 Git，部署时必须限制为 Worker 进程账号可读。

### 6.2 文件结构

```json
{
  "schema_version": "h5-auth-config.v1",
  "global": {
    "phone": "<GLOBAL_TEST_PHONE>",
    "password": "<GLOBAL_TEST_PASSWORD>"
  },
  "domains": {
    "h5.example.com": {
      "phone": "<DOMAIN_TEST_PHONE>",
      "password": "<DOMAIN_TEST_PASSWORD>"
    }
  }
}
```

规则如下：

- `global` 可选，`domains` 可为空对象，但启用后至少存在一份完整账号。
- 每份账号必须同时提供非空 `phone` 与 `password`，禁止部分覆盖。
- 域名键必须是规范化 hostname，不含 scheme、路径、query、fragment 或端口。
- 域名键不支持 `*`、父域继承或后缀匹配；只做大小写无关的精确匹配。
- Pydantic 模型禁止未知字段，密码字段使用秘密类型且不参与普通 repr、日志和 model dump。
- 配置在每个 H5 V2 Runtime 构建时加载并校验，且发生在创建浏览器 Session 之前；任务、Redis 消息和数据库只保存 profile scope/ref，不保存正文。

### 6.3 跨域限制

凭证在任务启动时只根据原始目标 hostname 选择一次。global 只是账号选择兜底，不是跨站授权：

- 只有当前登录页面 hostname 与任务原始 hostname 完全一致时才允许填写。
- 登录过程跳转到其他 hostname 时，本版本立即停止凭证填写并输出 `auth_page_cross_hostname` 或 `secret_submit_cross_hostname`。
- 不根据重定向后的 hostname 重新选择另一个域名账号，防止恶意跳转探测或窃取配置凭证。
- 后续若要支持可信 SSO，必须新增显式的目标域到登录域授权关系，不得复用 global 绕过此限制。

## 7. 领域合同与状态机

协议版本为 `h5-auth-bootstrap.v1`，账本 phase key 为 `h5_auth_bootstrap`。

### 7.1 结果合同

`H5AuthBootstrapResult` 包含：

- `status`：`disabled`、`already_authenticated`、`authenticated`、`guest_only` 或 `failed`；
- `attempted`；
- `credential_scope`：`domain`、`global` 或 `null`；
- `reason_code`；
- 最终安全 Capture 引用；
- 是否要求 `needs_review`。

task ID、agent run ID、session ID、原始 hostname 与协议版本由命令和 Surface Phase 账本持有，不在结果对象重复投影。登录提交次数由 `credential_submit_planned/result` 审计步骤确定，当前协议最多一次。

合同中禁止出现手机号、密码、输入长度、输入哈希、Cookie、Token、selector 和浏览器句柄。

### 7.2 主状态流程

```text
初始导航与安全采集
  -> 分析当前页面
  -> 已是明确登录页：进入登录页分支
  -> 否则寻找“我的/Profile/账户”候选
  -> 受控点击 Profile
  -> 分析 logged_in / logged_out / login_page / unknown
  -> logged_in：authenticated(already_logged_in)
  -> logged_out：识别并点击登录入口
  -> login_page：检查密码输入模式
  -> 验证码页且有密码模式入口：受控切换
  -> 仅验证码/MFA/扫码：guest_only(unsupported_login_challenge)
  -> 唯一手机号框 + 唯一密码框 + 唯一提交按钮
  -> 解析 domain/global profile ref
  -> Secret Runtime 填写并提交一次
  -> 有界等待并安全重新采集
  -> 验证登录结果
  -> 成功：authenticated(login_succeeded)
  -> 失败或不确定：guest_only + needs_review
  -> 受控返回任务原始 URL
  -> 对返回后的安全 Capture 做正式页面识别
  -> 创建根 Frontier 和首条 Exploration Work
```

### 7.3 登录成功判定

不能只以 URL 变化、点击成功或 HTTP 2xx 作为登录成功。至少同时满足：

- 登录表单和认证遮挡不再可见；
- Profile 页面出现稳定的已登录用户态、账户入口或其他正向登录信号；
- 不存在“账号或密码错误”“登录失败”“验证码 required”等失败信号；
- 登录后 Capture 可安全持久化并完成页面识别。

若信号冲突或置信度不足，提交前结果为 `guest_only/<page_reason>`，提交后结果为 `failed/login_not_confirmed`，均不得猜测已登录。

### 7.4 重试与幂等

- 默认每个任务最多提交一次账号密码，避免错误 global 账号触发锁定。
- 单次 Bootstrap 最多读取 10 次认证页面、执行 4 次普通认证导航；密码提交仍固定最多一次，不允许无限等待或循环点击。
- 观察步骤以来源 Capture ID、导航步骤以本轮序号与来源 Capture ID 形成稳定 step key；提交计划、提交结果和返回根页面使用固定 step key。
- Worker 在提交前崩溃可从最近安全 Capture 恢复。
- Worker 在提交后、验证前崩溃时，恢复后先重新观察当前登录态；禁止盲目再次提交密码。
- 已写终态的 phase run 不得重新打开，旧 lease、旧 row version 和旧 session command 不得覆盖新结果。

## 8. Workflow 数据流更新

当前流程：

```text
H5 初始导航 -> 初始页面识别 -> 根 Frontier -> Exploration Work
```

更新后：

```text
H5 初始导航
  -> H5 Authentication Bootstrap
  -> 返回原始目标 URL
  -> 根页面安全采集
  -> 页面识别与 Assessment
  -> 根 Frontier
  -> Exploration Work
```

`H5ExplorationWorkflow.run()` 不直接包含所有登录细节，只依赖 `H5AuthBootstrapPort`。组合根负责注入配置解析器、页面分析器、Surface Phase 仓储和 Secret Runtime。

登录前置阶段复用统一 `before_action` / `after_action` Capture 角色，以保持 Capture Coordinator 和数据库角色合同稳定。认证归属由 `h5_auth_bootstrap` Surface Phase 的 source/result Capture 引用确定：认证初始 Capture 与除 `return_to_task_root` 外的中间 result Capture 只用于登录审计，不进入正式页面覆盖、规则匹配和 Evidence Handoff 主页面集合。根 Frontier 只使用 `return_to_task_root` 返回原始 URL 后的安全 after Capture。

根 Frontier metadata 保存脱敏摘要：

```json
{
  "h5_auth": {
    "attempted": true,
    "status": "authenticated",
    "reason_code": "password_login_verified",
    "credential_scope": "global",
    "needs_review": false
  }
}
```

该摘要供当前 Workflow 与 Worker 做终态投影；本阶段不新增公开 API 或工作台字段。PostgreSQL Surface Phase 账本仍是事实源，后续如需展示只能投影该脱敏摘要。

## 9. 敏感信息与证据边界

### 9.1 禁止进入的通道

手机号和密码正文，以及可用于离线验证密码的派生值，不得进入：

- LLM prompt、响应或 Tool 参数；
- Explorer Work、ActionIntent、候选 metadata；
- `task_config`、`_task_target`、Redis 消息；
- PostgreSQL phase context、decision payload、action execution；
- 日志、异常、metrics、trace、HAR、console log；
- Screenshot、DOM、visible text、UI snapshot、报告和 API 响应；
- 输入长度、明文哈希或密码哈希。

通用 Web INPUT 当前会把 `input_text` 放入参数并记录长度/哈希，因此认证凭证不得复用该路径。

### 9.2 Secret Runtime 采集纪律

1. 填写前可以持久化不含凭证的登录页面 Capture。
2. 从开始填写到点击提交完成之间，不进行普通 Screenshot、DOM、visible text、UI snapshot、HAR 或 console 持久化。
3. 成功跳离登录表单后，方可按普通安全 Capture 采集结果页。
4. 登录失败、超时或表单仍可见时，必须先清空手机号框和密码框并读回验证为空，之后才允许生成后续安全 Capture。
5. 字段无法确认清空时只保留填写前证据和 Surface Phase 的 `secret_fields_scrub_failed` 原因，立即关闭 Session；不得为了证据完整性泄露凭证。
6. Runtime trace 只允许记录字段角色、候选 ID、步骤状态和脱敏错误码，不记录 selector、正文、长度或哈希。
7. 为避免提交请求的延迟响应或页面脚本在窗口结束后泄露秘密，执行过 Secret Runtime 的浏览器 Session 在剩余生命周期内持续静默 console/network 观察器；正式页面仍采集 Screenshot、DOM、visible text 与 UI Snapshot。

### 9.3 配置安全

- 配置文件不得提交 Git。
- 开发、测试和生产使用不同测试账号。
- global 账号只能是低权限、无真实用户数据的专用测试账号。启用 global 的 Worker 必须只接受组织已授权的 H5 质检目标；允许任意公网 URL 的通用 Worker 禁止启用 global。
- 文件权限只授予 Worker 进程账号。
- 真实密码不写入 `.env.example`、测试 fixture 或文档。
- 生产环境后续迁移到凭证库时，`H5CredentialConfigResolver` 接口保持不变，只替换 Provider。

## 10. 审计与持久化

本次复用现有 `qc_surface_phase_run` 与 `qc_surface_phase_step`，不新增数据库表，也不引入数据库迁移。

phase run context 仅保存：

- 原始目标 hostname；
- profile scope/ref；
- 最大步骤和最大登录次数；
- 当前步骤与尝试计数；
- schema/protocol version。

每个 phase step 保存：

- 页面状态与 reason code；
- 来源和结果安全 Capture ID；
- 选中的稳定候选 ID；
- 普通点击动作的 action execution ID；
- Secret Runtime 的脱敏执行状态；
- 支撑判定的安全 artifact ID。

Secret Runtime 填写动作不伪装为普通 `qc_action_execution`，其 phase step 的 `action_execution_id` 可为空；动作事实由 phase step 的协议化脱敏 payload 记录。若未来需要统一 Action Execution，必须先为 secret action 建立不会序列化秘密的专用合同。

工作台不新增菜单或路由。任务详情可通过现有 Run usage/详情投影展示：已登录、自动登录成功、游客降级、验证码阻断或登录失败；不得展示手机号、配置文件路径和 profile ref 正文。

## 11. 异常处理与终态语义

| 场景 | 自动动作 | Auth 结果 | 最终任务语义 |
| --- | --- | --- | --- |
| Profile 明确已登录 | 不填写、不提交 | `already_authenticated/already_authenticated` | 正常探索，可按既有门禁完成 |
| 精确域名账号可用 | 自动密码登录 | `authenticated/password_login_verified` | 正常探索，可按既有门禁完成 |
| 未命中域名但 global 可用 | 自动密码登录 | `authenticated/password_login_verified` | 正常探索，可按既有门禁完成 |
| 无任何账号配置 | 不填写 | `guest_only/credential_not_configured` | 游客探索，最终 `needs_review` |
| 找不到 Profile 或登录入口 | 不猜测控件 | `guest_only/auth_state_unknown` 或 `profile_login_entry_unavailable` | 游客探索，最终 `needs_review` |
| 仅验证码/MFA/扫码 | 不处理验证码 | `guest_only/unsupported_login_challenge` | 游客探索，最终 `needs_review` |
| 跳转到其他 hostname | 不发送凭证 | `failed/auth_page_cross_hostname` 或 `secret_submit_cross_hostname` | 游客探索，最终 `needs_review` |
| 输入框或按钮不唯一 | 不填写 | `guest_only/*_ambiguous` | 游客探索，最终 `needs_review` |
| 账号密码错误 | 不重复提交 | `failed/login_not_confirmed` | 游客探索，最终 `needs_review` |
| 提交后结果不确定 | 重新观察但不重填 | `failed/login_not_confirmed` | 游客探索，最终 `needs_review` |
| 敏感字段无法确认清空 | 关闭当前 Session | Surface Phase `failed/secret_fields_scrub_failed` | Runtime `error/needs_review` |
| Session/浏览器不可恢复 | 关闭或标记 lost | Runtime `error/needs_review` | `error/needs_review` |
| 用户取消任务 | 停止新动作并关闭 Session | `canceled/workflow_canceled` | `canceled` |

游客降级后，Workflow 尽力受控返回任务原始 URL，再创建根 Frontier。若返回原始 URL 也失败，则无法满足“继续游客探索”，任务直接以 `incomplete/needs_review` 收口。

### 11.1 Completion Gate

- `authenticated` 不替代任何既有证据、规则或 Completion Gate 条件。
- `guest_only` 或 `failed` 必须传播 `needs_review=true`。
- 即使 Explorer 局部返回 `completed`，只要 Auth Bootstrap 为 `guest_only/failed`，Task/Run 也不能变成完整成功或生成 sealed Report。
- `exploration_only` 同样以 incomplete/needs_review 收口，但仍保留游客可访问页面的探索结果。

## 12. API 与前端影响

任务提交 API 不新增 `authentication.phone`、`authentication.password` 或其他凭证字段。现有 H5 请求保持兼容。

本阶段不新增后端公开字段、前端菜单或路由。认证脱敏摘要仅保存在根 Frontier metadata 与 Surface Phase 事实链中；未来增加详情投影时，旧任务缺失该字段应解释为“未提供摘要”，不得反推登录失败。

本轮不修改前端代码，也不新增菜单或路由。Worker 将脱敏摘要写入现有 Run usage，详情 API 保持向后兼容地透传该可选字段；前端展示另行拆分为后续任务，不属于本次完成门禁。

## 13. 测试设计

### 13.1 配置合同

- enabled=false 保持当前流程。
- enabled=true 但 path 缺失、文件不存在、JSON 非法、schema 版本错误或账号字段不完整时 fail closed。
- 精确域名覆盖 global。
- 大小写和 hostname 规范化后精确匹配。
- 不做后缀、父域或通配符匹配。
- 重定向到其他 hostname 时禁止填写。
- 配置模型 repr、dump 和异常不出现密码。

### 13.2 页面分析与状态机

- 初始页已经是登录页。
- Profile 入口存在并可进入。
- Profile 明确已登录。
- Profile 未登录并存在登录入口。
- 验证码模式可切换到密码模式。
- 仅验证码、MFA 或扫码。
- 手机号框、密码框、提交按钮缺失或多义。
- 登录失败提示、成功用户态和冲突信号。
- 候选来自旧 Capture 时拒绝执行。
- 重新观察和登录提交预算耗尽。

### 13.3 Secret Runtime 安全测试

- LLM 请求和响应中不出现手机号、密码。
- ActionIntent、队列、phase payload、日志、异常、trace 中不出现正文、长度或哈希。
- 填写与提交之间不持久化普通 artifacts。
- 失败页面先脱敏再落证据。
- 脱敏失败时原始 artifacts 被丢弃。
- Session Permit、lease、command sequence 和 deadline 失效时拒绝填写。
- 同一 step 重放不产生第二次登录提交。

测试不使用真实 global 密码；使用随机 canary secret，并对测试进程捕获到的日志、JSON、数据库参数和 artifacts 做全局扫描，发现 canary 即失败。

### 13.4 Workflow 集成

- Auth Bootstrap 未终态前不发布根 Exploration Work。
- 登录成功后受控返回原始 URL，并以新 Capture 创建根 Frontier。
- 已登录时不执行 Secret Runtime。
- 登录失败、无配置或验证码阻断后仍发布游客根 Work，metadata 带 `needs_review=true`。
- `full_inspection` 与 `exploration_only` 均走同一认证主链。
- guest-only 情况下 Explorer completed 不能覆盖任务 needs_review。
- 取消、崩溃恢复和提交后不确定状态不重复发送密码。

### 13.5 Playwright 验收页

至少提供以下本地隔离页面：

1. Profile 已登录；
2. Profile 未登录，标准手机号密码表单；
3. 默认验证码、可切换密码登录；
4. 仅验证码；
5. 错误密码并展示失败提示；
6. 登录成功后返回首页；
7. 登录跳转到其他 hostname；
8. 两个相似登录按钮或输入框导致多义；
9. 登录失败页保留输入值，用于验证证据脱敏。

真实目标站点、真实 PostgreSQL/Redis/对象存储和浏览器联调未完成前，只能宣称代码与隔离 E2E 闭环，不能宣称生产登录能力已验收。

## 14. 预计代码影响

主要修改或新增位置：

- `services/api/app/core/config.py`：H5 Auth 配置模型和加载校验。
- `services/api/app/domain/exploration/`：Auth Bootstrap 协议、状态、观察与结果合同。
- `services/api/app/application/h5_auth_bootstrap.py`：应用编排与恢复逻辑。
- `services/api/app/application/h5_exploration_workflow.py`：在根识别/Frontier 前接入认证端口。
- `services/api/app/application/h5_runtime_composition.py`：注入配置、分析器、账本与 Secret Runtime。
- `services/api/app/infrastructure/browser/`：专用 Secret Input 与安全采集适配器。
- `services/api/app/infrastructure/db/surface_phase_repository.py`：仅在通用仓储能力缺口存在时做无秘密扩展。
- `services/api/tests/`：配置、领域、Runtime、安全、Workflow 和 Playwright 回归。
- `.env.example`：只增加开关和配置路径说明。
- `docs/workflows/h5-v2-explorer-workflow.txt`：更新最终业务 Workflow 与自动登录边界。
- `docs/专项能力与高频回归规则.md`：增加登录与秘密不落盘的高频回归规则。

本设计复用既有 Surface Phase 数据表，数据库迁移：无。前端菜单/路由：无新增。

## 15. 验收标准

实现完成必须同时满足：

1. H5 V2 启用认证配置后，在正式探索前主动检查 Profile 登录态。
2. 已登录不会重复登录。
3. 未登录且密码模式可用时，Runtime 自动填写配置手机号和密码并点击登录。
4. 精确域名配置优先于 global，global 仅作为任务原始 hostname 的账号兜底。
5. 仅验证码/MFA/扫码时不处理验证码，继续游客探索并最终 needs_review。
6. 登录失败继续游客范围，但不能形成完整成功或 sealed Report。
7. 登录成功后返回原始 URL，根 Frontier 不包含认证阶段的临时页面谱系。
8. 密码及其长度/哈希不出现在模型、任务、Redis、数据库、日志、证据、报告和 API 响应中。
9. 崩溃恢复和重复消费不会重复提交密码。
10. 单元、合同、集成、安全扫描和隔离 Playwright 场景全部通过。
11. Workflow 文档与专项回归规则同步更新。

## 16. 实施顺序

1. 配置合同与 secret 泄露 canary 测试。
2. Auth Bootstrap 领域状态机和 Surface Phase 账本适配。
3. 页面分析结构化合同与候选识别。
4. 专用 Secret Input Runtime 与安全采集/脱敏。
5. H5 Workflow 主链接线和根 Frontier 终态传播。
6. Worker/详情投影与 Completion Gate 门禁。
7. 文档、专项回归规则、Playwright 验收与完整审查。

每个步骤先写失败测试，再做最小实现；完成后执行静态检查、相关测试、秘密 canary 扫描和最终 diff 审查。
