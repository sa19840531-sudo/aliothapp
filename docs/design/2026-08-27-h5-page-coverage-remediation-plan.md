# H5 页面覆盖率低的修复方案与实施计划

## 1. 目标

本次修复面向 `/api/v2/tasks` 与 `/api/v2/exploration-tasks` 的 H5 V2 探索链，解决以下已经由本地 PostgreSQL 运行事实和 `dev_1.5` 代码共同证明的问题：

1. H5 全量任务没有锁定业务页面覆盖目标，运行时 `required_page_types=[]`，与报告侧 Catalog 全量评估口径分裂。
2. 同一业务页弹窗跨 Capture 反复生成新候选，失败重观察持续消耗动作、Capture 和模型 Token。
3. 自定义 SPA 的非链接导航、底栏、页面 Tab 和业务入口不能稳定生成带预期页型的候选。
4. 动作执行成功、页面实际到达、权威 PageTypeAssessment 和最终 Coverage 之间缺少统一验收口径。

本次不把“更多 Capture”“动作返回成功”“Frontier 为空”作为覆盖完成。覆盖完成必须同时满足：覆盖目标已锁定、实际到达证据有效、未覆盖类型有明确终态、Frontier 安全收敛。

## 2. 设计原则

### 2.1 覆盖目标是任务合同，不是报告推测

- H5 任务允许显式提交 `required_page_types`。
- 未显式提交时，`full` 和 `custom` 使用运行时 Catalog 中 `coverage_required=true` 的业务页型作为默认目标。
- `quick` 保持抽样语义，不伪装成 Catalog 全覆盖。
- `line_loading`、`startup_ad`、`guide_page` 等生命周期页不得进入业务覆盖分母。
- 规则适用页型用于规则匹配，不直接替代产品覆盖目标；二者需要分别持久化和展示。

### 2.2 覆盖以实际到达为准

- 候选的 `expected_next_page_type` 只表示探索意图。
- 只有 After Capture 的权威 PageTypeAssessment 与预期一致，候选才可记为已到达。
- `not_applicable` 是适用性结论，不得计入 `covered_page_types`；应单独展示。
- `unknown`、`evidence_insufficient`、认证阻断和安全阻断均不得提升覆盖率。

### 2.3 弹窗身份跨 Capture 稳定

- 弹窗候选的业务身份由来源路由、来源业务页型、obstruction signature 和关闭能力组成。
- Capture ID 仅用于证据与单次幂等，不能让相同弹窗无限获得新的语义身份。
- `POPUP_DISMISSAL_NOT_FOUND` 后只允许一次无副作用重观察；确认弹窗已不存在后抑制同签名关闭候选并恢复 sibling。
- 不同真实弹层、签名漂移弹层和广告墙仍由现有同签名、连续失败、页面总轮次三层预算止损。

### 2.4 SPA 候选必须可解释、可验证、低风险

- 支持可见、同源、可交互的非 `<a>` 导航控件，但必须有已知导航别名或确定性结构。
- 未知文案不得猜测页型；没有可信 `expected_next_page_type` 时不能用来满足覆盖目标。
- 支付、发布、下载、登录等风险边界保持 fail-closed；只允许安全导航到页面，不执行不可逆业务提交。

## 3. 分批实施

### 批次一：覆盖合同与统计口径

1. 给 H5 V2 请求增加覆盖目标快照。
2. 将目标写入任务 `page_sampling_policy.include_page_types` 并传递到 H5 Runtime、Frontier 和 Coverage。
3. 明确 full/custom 默认与 quick 抽样策略。
4. 修正 `covered_page_types`，不再把 `not_applicable` 或生命周期类型计作已覆盖。
5. 增加 API、任务入口、运行时设置和 Coverage 回归测试。

验收：新建 full H5 任务的 `required_page_types` 非空且与当前业务 Catalog 一致；显式子集保持原样；生命周期类型被拒绝；缺失目标使 `completion_ready=false`。

### 批次二：弹窗跨 Capture 去重

1. 将 popup 的 Frontier 语义键从 occurrence candidate ID 调整为稳定弹窗身份。
2. 保留单次 Capture 幂等字段，但不再让相同签名跨 Capture 重生为无限新动作。
3. 验证弹窗自动退场、同签名持续存在、签名漂移和多层真实弹窗四类路径。
4. 保留现有 3/4/8 次止损并验证终态原因可审计。

验收：同路由同签名跨 Capture 只保留一个有效语义候选；自动退场后 sibling 可继续；不同真实弹层仍可逐层处理。

### 批次三：SPA 导航与实际到达

1. 扩展可见、同源、事件绑定的底栏和页面 Tab 候选。
2. 对已知别名补齐 `expected_next_page_type`，未知别名保持不生成或不声明目标。
3. 补齐列表首卡片、个人中心、设置、支付入口的确定性回归样本。
4. 验证同 URL SPA 的页面类型/稳定状态变化，避免“点击成功但实际未到达”。

验收：cp14、h5.bkh、d24、d3 类 DOM 片段能生成可执行候选；popup/unknown 状态仍不绕过安全门；预期与实际不一致时不增加覆盖。

### 批次四：集成验证与 Git 同步

1. 运行定向单测、H5 全链关键回归、格式检查和 `git diff --check`。
2. 使用本地 PostgreSQL/Redis 验证新任务策略快照与运行时解析。
3. 每个批次只白名单暂存本批文件，审查 staged diff 后分别提交。
4. 提交说明明确模块、无数据库迁移、无前端菜单/路由变更及未覆盖风险。
5. 拉取远端 `dev_1.5`，普通 rebase 后推送；最终确认 `HEAD...origin/dev_1.5` 为 `0 0`。

## 4. 完成标准

- 代码层：覆盖目标、Frontier、Capture、Assessment、Coverage 和 Closure 使用同一业务页型合同。
- 测试层：新增回归全部通过，现有 H5 关键测试无回归。
- 数据层：不新增数据库迁移；新任务持久化正确目标，历史任务保持可读但继续显示其空目标事实。
- 前端层：本批不修改菜单或路由；若页面需要展示 `not_applicable/unresolved`，另行排期。
- 真实站点层：代码与本地集成通过不等于外站覆盖验收，修复同步后仍需对目标站点重新跑测并冻结页面、规则、时间和 Token 指标。

## 5. 运行后补充根因与后续批次

首轮真实回归暴露了覆盖合同、弹窗去重和 SPA 候选以外的三类收口问题，因此在原计划后追加以下批次：

### 批次五：Publication 与规则消费收口

1. Publication 完成屏障同时校验期望数量与非终态数量，禁止零行提前完成。
2. `review + session_draining` 下只允许当前 epoch 的规则消费 Run 恢复 Snapshot Work。
3. Judge、正式写回和 Work 终态放入同一个租约心跳作用域，成员缺失和 fencing 冲突有界重试。

### 批次六：Frontier 终态与动作未知结果

1. 空、全失败和 pending 无可执行候选分别形成明确终态原因。
2. `BROWSER_HISTORY_EMPTY` 隔离当前候选并继续 sibling。
3. `ACTION_DEADLINE_EXCEEDED` 使用动作前事实关闭；`ACTION_TIMED_OUT` 作废旧 Session，通过 unknown-action 恢复链重建，禁止重放未知动作。

### 批次七：确定性候选执行与成本硬预算

1. 生产 H5 Explorer 直接执行平台已验签的唯一 selected 候选，删除每个动作前的重复模型选路。
2. quick/custom/full 分别锁定动作和模型调用上限；Token 默认只计量不限制，任务显式配置时才启用附加硬上限。
3. H5 循环在首轮前和需要后继的每轮后检查 Run 级实际用量，调用预算或显式 Token 预算耗尽时保留证据并转人工复核。
4. 使用同步后的 `dev_1.5`、本地 PostgreSQL/Redis 和 `/api/v2/tasks` 重跑全部目标站点；对比修复前页面覆盖、规则覆盖、调用数、Token、耗时、Gate 和错误指纹。
