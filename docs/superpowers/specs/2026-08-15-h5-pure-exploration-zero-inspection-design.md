# H5 纯探索与零质检语义设计

## 1. 背景与目标

当前 H5 V2 已经由 Explorer Agent 执行页面探索、Rule Judge Agent 执行规则判定，但 H5 Explorer 生产路径仍加载带有规则覆盖语义的共享 Skill，并保留 `ProbeWork`、`EvidenceRequest`、Probe publisher 和 Probe 消费合同。这使“判定 Agent 已拆分”成立，却无法证明 H5 探索阶段已经达到纯探索、零质检语义。

本次目标是把 H5 Explorer 收敛为单一职责组件：只依据页面 Observation、动态 Frontier、页面类型覆盖缺口、安全边界、预算和截止时间选择下一步探索动作。探索封存后，独立质检链路消费不可变证据并执行规则，质检结果不得反向改变本次 H5 探索。

完成代码改造后，只重启现有 Docker 容器，不重新构建镜像；随后提交真实 H5 任务，持续观察页面导航向量检索、Worker 探索、独立规则判定和页面类型覆盖。对仍可修复的探索阻断执行失败测试、最小修复、回归测试、无构建重启和新任务复验，直至闭环。

## 2. 明确边界

### 2.1 H5 Explorer 允许的输入

- 当前页面的只读 Observation 和持久化 Capture 引用。
- 当前动态 Frontier 及其中由平台生成的候选动作。
- Catalog 页面类型、已观察页面类型和页面类型覆盖缺口。
- 页面结构、可见导航语义、状态版本和历史访问事实。
- Host、Route、操作类型、副作用、安全和认证边界。
- 最大动作数、最大深度、剩余时长和截止时间。
- 页面导航别名的静态、数据库精确或向量解析结果。

### 2.2 H5 Explorer 禁止的输入与职责

- ruleset、rule code、规则优先级、规则覆盖率和候选规则。
- Rule Judge 结果以及 `pass`、`fail`、`warning`、`manual_review`、`evidence_insufficient` 等判定。
- `ProbeWork`、`EvidenceRequest` 和任何质检补采请求。
- Inspection Work、规则写回、报告生成和任务整体完成判定。
- 直接浏览器 Driver、数据库写入、Locator、坐标、脚本或高副作用操作。

### 2.3 独立质检链路

H5 Explorer 终态只代表探索封存。Worker 外层在封存后读取不可变 Capture、页面类型评估和动作审计，经证据交接门禁发布 Inspection Work。Rule Judge Agent 通过独立 inspection stream 消费工作项、形成规则结果并由 Writer 持久化。该链路不得向已封存的 H5 Explorer 发布 Probe，也不得把补证需求转为本次探索动作。

## 3. 方案选择

### 3.1 采用方案：H5 生产路径专用化

H5 使用专用 Skill、专用系统提示词、只接受 `ExplorationWork` 的 Agent 合同和无 Probe 的运行时组合。App 与其他仍需补证能力的路径继续使用共享 Explorer/Probe 合同，不纳入本次删除范围。

该方案能从生产组装、类型合同、提示词和运行循环四层证明 H5 零质检语义，同时避免扩大到 App 的无关重构。

### 3.2 未采用方案

- 全局删除共享 Explorer 的 Probe：边界最彻底，但会同时改变 App 和既有 V2 合同，回归面过大。
- 只清理提示词和 Skill：无法消除 Probe publisher、Probe claim 和运行合同，不能形成代码级保证。

## 4. 架构与组件

```text
H5 Explorer
  输入：Observation、Frontier、页面类型覆盖缺口、安全边界、预算
  输出：ActionIntent 或 ExplorationTerminal
  禁止：规则集、规则覆盖、ProbeWork、EvidenceRequest、Judge、报告

探索封存
  └─ 不可变 Capture / 页面类型评估 / 动作审计
       └─ Inspection Work Publisher
            └─ 独立 Rule Judge Agent
                 └─ 规则结果 Writer / 报告
```

### 4.1 H5 Explorer Skill

`h5-explorer-workflow` 成为 H5 Explorer 唯一加载的 Skill。它负责：

- 优先处理瞬态页面、遮挡和低风险可逆恢复。
- 依据页面类型覆盖缺口、结构信息增益、候选可逆性和预算排序。
- 从当前 Frontier 逐次选择一个候选，不生成 Driver 参数。
- 以页面和状态事实形成 `completed`、`budget_exhausted`、`bounded`、`no_progress` 或 `blocked` 局部终态。

H5 Bootstrap 不再加载 `phase/page-exploration` 与 `navigation-boundary-skill`，避免继承其中的规则驱动和质检写回语义。H5 所需的页面探索顺序与安全边界统一固化在专用 Skill 中。

### 4.2 H5 专用提示词与 Agent 合同

H5 生产入口使用不包含 `ProbeWork`、`EvidenceRequest`、Rule Judge、规则结果和报告语义的专用系统提示词。H5 Agent 协议的输入类型只允许 `ExplorationWork`，输出只允许平台可验证的 `ActionIntent` 或 `ExplorationTerminal`。

共享 `MafExplorerAgent` 可继续服务 App。若复用其底层结构化输出解析能力，H5 必须通过独立适配器和独立提示词暴露纯探索端口，不得把共享 Probe 合同带入生产组装。

### 4.3 H5 Turn Runner 与 Runtime

- H5 Turn Runner 只领取 `ExplorerWorkType.EXPLORATION`，只接受 `ExplorationWork`。
- 删除 H5 组合根中的 `PostgresProbeWorkPublisher`。
- 删除 H5 Executor 的 `probe_claim_enabled`、Probe 策略派生和 Probe 排空循环。
- H5 Runtime 不依赖 supplemental evidence 配置，也不记录“跳过 Probe”的运行分支。
- Explorer 局部终态不得直接把整个质检 Task/Run 宣告完成。

### 4.4 Worker 阶段协调

Worker 可以在外层按“探索封存 → 证据交接 → Inspection Work → Rule Judge → 报告”的阶段顺序协调同一任务，但探索阶段和质检阶段通过不可变持久化合同连接。质检消费者可以与探索 Worker 运行在同一进程，也可以独立部署；两者必须是不同 Agent 和不同工作队列语义。

## 5. 页面导航向量检索

页面导航别名向量只属于页面探索知识，不属于质检知识。它可以把可见导航文案映射为 Catalog canonical page type，辅助生成导航候选或纠正当前页面类型；不得返回规则内容、读取 ruleset，或通过规则优先级影响探索排序。

### 5.1 权威关系

- `page_type_catalog.json` 是页面类型与正式别名的唯一权威源。
- PostgreSQL/pgvector 只保存 Catalog 投影和已确认导航别名索引。
- 向量结果只有能投影回当前 Catalog 正式定义时才允许使用。
- 静态别名优先，其次数据库精确匹配，最后才是达到相似度阈值的向量匹配。

### 5.2 可观测性

每次页面识别记录以下安全审计字段：

- `nav_alias_static_match_count`。
- `nav_alias_exact_match_count`。
- `nav_alias_vector_match_count`。
- 向量命中的 canonical page type、置信度和匹配类型。
- 向量结果实际影响的页面类型纠偏或候选动作身份。
- 仓储不可用时的 `vector_repository_degraded` 和静态降级原因。

原始凭据、Cookie、Token 和非必要页面正文不得进入这些审计字段。

## 6. 页面覆盖口径

目标页面类型为：

`line_loading`、`startup_ad`、`home`、`content_list`、`content_detail`、`video_list`、`video_detail`、`ranking`、`profile`、`settings`、`payment`、`post_list`、`post_publish`、`activity`。

每个页面类型最终只能处于以下状态之一：

- `observed`：存在真实持久化 Capture，页面判定成功。
- `not_found`：探索已覆盖相关入口但无证据表明网站存在该页面。
- `requires_auth`：存在入口，但认证边界阻断且不能安全完成认证。
- `blocked`：存在入口，但安全、运行时或外部依赖阻断实际进入。

候选但未执行的页面、`about:blank`、失败动作推测和模型自然语言声明均不得计为 `observed`。

最终同时报告：

- 实际观察覆盖率：`observed 数 / 14`。
- 可达覆盖率：`observed 数 / 经证据证明实际存在且允许安全进入的页面类型数`。

14 类均可达时，约 90% 的验收下限为真实观察至少 13 类。若网站缺少某类页面或受不可绕过边界限制，必须展示证据并调整可达分母，不能伪造覆盖。

## 7. 安全、失败与恢复

### 7.1 构造失败关闭

自动化架构测试扫描 H5 Bootstrap、Skill、提示词、Agent 合同、Turn Runner 和组合根。发现 ruleset、规则覆盖、Rule Judge、Probe、EvidenceRequest、规则结果或报告能力时，H5 生产构造必须失败。

### 7.2 向量仓储降级

pgvector 或别名仓储不可用时记录 `vector_repository_degraded`，回退 Catalog 静态别名继续探索。向量故障不能触发质检补采，也不能单独终止任务。

### 7.3 探索故障

浏览器丢失、Capture 失败、页面未稳定和候选失效继续使用现有有界恢复。恢复失败只能形成探索终态，不得调用 Judge 或 Probe。

可以安全进入支付、发布和设置页面，但禁止确认支付、提交发布、删除、授权或其他高副作用动作。不得为提高覆盖率放宽安全门禁。

### 7.4 迭代修复纪律

每次真实任务失败先保存脱敏的 Task、Run、Capture、Frontier、向量审计和 Worker 日志。确认属于代码缺陷后，按“失败测试 → 最小修复 → 回归测试 → 无构建重启 → 新任务复验”处理；不得复用失败 Run。

外部站点缺页、认证限制和不可控外部服务故障不通过伪造数据修复，最终列为客观限制。只要仍存在仓库内可修复的探索阻断，就继续修复循环。

## 8. 测试设计

### 8.1 自动化测试

- H5 Bootstrap 只加载 `h5-explorer-workflow`。
- H5 提示词、Skill、合同和 Runtime 不含任何禁止语义或能力。
- H5 Agent 与 Turn Runner 拒绝非 `ExplorationWork`。
- H5 组合根不存在 Probe publisher、Probe claim 开关和 Probe 消费循环。
- App Explorer/Probe 现有测试保持通过，证明改动仅隔离 H5。
- 页面导航别名覆盖静态、数据库精确、vector 和仓储降级四条路径。
- 审计字段能证明向量结果是否实际影响页面类型或导航候选。
- Explorer 终态不能绕过证据交接、Inspection Work、Rule Judge 和完成门禁。

### 8.2 Docker 无构建验证

Compose 已把仓库挂载到 API/Worker 的 `/workspace`。代码与测试通过后执行：

```powershell
docker compose -f infra/docker-compose.yml -f .alioth-h5-v2.override.yml restart alioth-api alioth-worker
```

禁止执行 `build`。重启后检查容器健康、H5 V2 开关和 Worker 消费循环。

### 8.3 真实任务

通过本地认证后，向 `http://127.0.0.1:8000/api/v2/tasks` 提交用户指定的 H5 完整扫描请求。只输出新任务的 `task_id`、`run_id` 和非敏感状态；认证凭据与 Token 不打印、不写文件。

持续轮询 API、Worker、PostgreSQL 和 Redis，直到探索、Inspection Work、Rule Judge 和报告进入终态。每次代码修复后必须创建全新任务验证。

## 9. 最终交付

最终结果至少包含：

- 任务与 Run 标识、终态和完整执行时间线。
- 向量库是否查询、命中或降级，以及实际影响的页面候选或类型纠偏。
- 每个真实 Capture 的 URL、页面类型、进入动作、离开动作和终态。
- 14 类页面的 `observed/not_found/requires_auth/blocked` 状态。
- 实际观察覆盖率与可达覆盖率。
- 按页面列出的独立 Rule Judge 实际执行规则及通过、失败、证据不足结果。
- 所有阻断、根因、代码修复、复测任务和剩余外部限制。

只有自动化测试通过、H5 零质检语义门禁通过、真实任务收口且不存在仍可修复的探索阻断，才宣告本次任务完成。

## 10. 变更范围

- 不引入数据库迁移，除非实现阶段发现现有审计字段无法持久化且用户另行批准。
- 不修改前端菜单或路由。
- 不删除 App 的 Probe 能力。
- 不覆盖当前工作区中的未跟踪文件。
- 具有复用价值的边界规则同步更新 `docs/专项能力与高频回归规则.md`。
