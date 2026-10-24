# H5 V2 Explorer Workflow 实施审核

日期：2026-08-11

审核对象：`codex/h5-v2-explorer-workflow` 隔离分支。

## 结论

H5 `workflow_version=v2` 的目标架构需要走独立 H5 V2 Explorer Agent；直接复用 App Explorer 或继续由通用 Main Agent 混合执行都不符合领域隔离、证据链和权限边界要求。

本次实现已形成可切换的 H5 V2 主链：路由、H5 专用 Explorer 装配、Web Runtime、首次导航双采、动作轮双采、after Capture 页面重判、Frontier 后继、租约循环、Probe 排空、Rule Judge 交接和 Completion Gate 收口。未引入数据库迁移，也未修改前端菜单或路由。

自动登录审核结论：不支持账号密码、Cookie/Token、验证码或 MFA 自动登录。仅允许关闭登录提示、选择游客入口，或在受控 Session 已有登录态时继续只读探索；否则记录 `requires_auth` 并进入阻断/人工复核。

## 架构符合性

| 审核项 | 结果 | 证据摘要 |
|---|---|---|
| H5 V2 是否走专用 Explorer Agent | 通过 | `build_default_h5_explorer_agent()` 只加载 H5/页面探索/导航边界 Skill |
| 是否错误加载 App Workflow Skill | 通过 | H5 Bootstrap 不加载 `app-workflow`、`app-script-rhythm` |
| H5 V2 是否可能静默回退 Main Agent | 通过 | Worker 在构建 Main Agent 前分流；Handler 缺失 fail closed |
| 是否支持 off/shadow/on | 通过 | 任务锁定版本与部署模式共同决定路由；显式 v1 始终保持 v1 |
| Shadow 是否执行第二次页面动作 | 通过 | Shadow 只消费不可变 WorkingMemory 审计快照并写决策投影 |
| Explorer 是否直接持有 Driver/数据库/Judge | 通过 | Explorer 只输出 ActionIntent/局部终态；组合根持有适配器和仓储 |
| 初始页面是否双采 | 通过 | 受信任 NAVIGATE 产生 before/after Capture，初始 before 使用合法根引用 |
| 每次动作是否双采并重判页面 | 通过 | `ExplorerTurnExecutor` 经 Capture Coordinator；后继只从 after Capture 检测生成 |
| screenshot/DOM/URL 是否覆盖操作前后 | 通过 | Capture Profile 固定 screenshot/DOM/visible_text/ui_snapshot；URL 写 Capture 页面元数据；after 增加 action_trace |
| 是否自动深度/广度探索 | 通过 | H5 Skill 规定“有界深度优先 + 覆盖缺口驱动广度补齐”；来源状态候选身份、已执行候选历史和目标绝对深度共同阻止返回后重复点击与深度重复累加 |
| Work 后继是否保持租约和谱系 | 通过 | 父 lease 下原子提交后继；校验 task/session/source Capture/parent_work_id |
| Explorer 终态是否直接完成 Task | 通过 | 单轮只关闭 Work；Worker 仍排空 Inspection/Rule/Probe 并经过 Completion Gate |
| 自动凭据登录 | 不允许 | 需要凭据、验证码或 MFA 时输出 `requires_auth` |

## 审核发现

### 高优先级

无已知高优先级架构缺陷。

### 中优先级残余风险

1. 当前验证以单元/契约测试为主，未在本环境连接真实 PostgreSQL、Redis 和 Playwright 浏览器执行完整端到端任务。生产切换 `on` 前必须至少完成一次真实 H5 首跳、详情下钻、返回、Probe、Session 关闭和 Completion Gate 演练。
2. `shadow` 采用不可变 V1 WorkingMemory 的 V2 决策投影，不运行第二个具备动作能力的 Agent。这满足“单动作源”安全要求，但准确率门禁仍依赖生产 Shadow 样本量、结论一致率和高风险误放行统计。
3. 当前 Transition 通过前后 Capture 谱系、稳定 `state_version`、visited states、`executed_candidate_ids` 和后继 Work 表达；候选身份已纳入来源状态，目标绝对深度也不再重复加一，但尚无独立的 H5 Transition 实体。生产观测仍需特别检查“动作成功但页面无变化”和错误跳转是否稳定落入 no_progress/复核，而非错误增加覆盖。

### 低优先级残余风险

1. 页面候选依赖现有 H5 Page Detection 的 `candidate_actions` 和 UI Snapshot 语义质量；同 URL 多状态、遮挡与异步渲染仍需用真实站点样本持续回归。
2. `requires_auth` 不会自动补凭据，因此登录后页面覆盖率可能低于人工登录基线；报告应把它呈现为覆盖阻断而不是质检通过。

## 数据与权限审核

- 不新增数据库表；复用 Surface Session、Capture/Artifact、Action Execution、Explorer/Probe Work、Inspection Work、Rule Execution、Pipeline Shadow 和 Task Finalization 事实。
- PostgreSQL 是事实源，Redis 只承担唤醒和短期投影。
- DOM、截图和 Base64 不进入 Shadow 摘要；Pipeline Shadow 会再次清理敏感大载荷字段。
- H5 Explorer 无浏览器 Tool、数据库 Tool、Rule Judge Tool 或报告写回 Tool。
- 账号、密码、Cookie、Token、验证码和 MFA 不进入 Explorer 输入、Detection 输入摘要或 Shadow 记录。

## 验证状态

- H5 路由、Explorer Bootstrap、检测适配、首轮 Workflow、动作轮、Runtime、Shadow 和 Worker 分流核心测试：52 passed。
- Capture、Web Runtime、Page Detection、Page Type Assessment、Explorer Turn、Task Finalization 与 Completion Gate 关键回归：380 passed。
- 本次变更 Python 文件 Ruff 检查通过；`git diff --check` 通过。
- 相关架构边界测试通过；完整架构边界测试仍有 2 个仓库基线失败，原因是主分支缺少 `mobile_demo_agent_ad_capture.py` 与 `mobile_demo_ai_driver.py`，与本次 H5 修改无关。
- 全仓 Ruff 仍有 715 个历史问题；当前运行环境未安装 mypy。上述两项未伪装为本次通过项。
- 未连接真实 PostgreSQL、Redis 和 Playwright 浏览器执行端到端任务，仍按中优先级残余风险处理。

## 切流建议

1. 默认保持 `off`，先在生产等价环境运行 `shadow` 并积累门禁样本。
2. Shadow 达到样本量与准确率门禁，且无越权动作、证据链断裂、Session 并发写或不可解释 Transition 后，再小流量切换 `on`。
3. `on` 阶段监控 `requires_auth`、blocked、no_progress、retry_wait、evidence_failed、Session close failure 和 Completion Gate block reason。
4. 出现高风险误动作、after Capture 缺失、旧 Worker 续写、错误完成或 Gate 绕过时立即切回 `off`。
