# H5 补采关闭时安全跳过 Probe 设计

## 1. 背景

`dev_1.5` 的 H5 V2 完整质检在主探索完成后，无条件进入 Probe Work 领取循环。当前补采策略明确保持关闭，且生产组合根将补采运行时标记为未就绪。因此 H5 Runtime 调用 `CLAIM_PROBE` 时触发安全门禁，任务以 `technical_failed` 收口，无法继续生成完整质检结果。

本设计不启用补采、不放宽补采安全策略，只修复 H5 编排层在能力不可用时仍尝试领取 Probe Work 的错误。

## 2. 目标与非目标

### 2.1 目标

- 补采关闭或运行时未就绪时，H5 主探索不得调用 Probe Work 领取。
- H5 主探索、页面动作、页面采集、规则判定与任务收口保持原有流程。
- 补采策略、Repository 防线和 Probe 执行防线继续 fail-closed。
- 日志明确记录 Probe 阶段因能力不可用而跳过。
- 修复后通过同一 H5 V2 请求重新质检，并观察 worker 到终态。

### 2.2 非目标

- 不设置 `ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED=true`。
- 不修改 `SupplementalEvidencePolicy.runtime_ready=False` 的生产基线。
- 不让 Repository 在补采关闭时静默返回“无工作”。
- 不改变 App V2 的 Probe、补采或设备运行语义。
- 不引入数据库迁移，不修改前端菜单或路由。

## 3. 方案选择

采用编排层显式门禁方案：H5 Workflow Handler 在进入 Probe 循环前读取一个不可变的 `probe_claim_enabled` 能力标志。该标志由当前补采策略对 `CLAIM_PROBE` 的决策结果派生，而不是直接读取环境变量。

未采用以下方案：

- 捕获 `SupplementalEvidenceDisabledError` 并伪装成“无 Probe”，因为这会吞掉错误调用。
- 修改 Repository 在关闭时返回 `None`，因为这会削弱跨入口的安全防线。
- 把生产补采运行时标记为就绪，因为当前补采闭环尚未实现。

## 4. 架构与数据流

### 4.1 组合根

`build_h5_workflow_handler(...)` 使用 `build_current_supplemental_evidence_policy(...)` 构造当前策略，并通过 `decide(CLAIM_PROBE).allowed` 得到 `probe_claim_enabled`。

该值传入 H5 Workflow Handler，成为当前 worker 生命周期内不可变的运行能力快照。

### 4.2 H5 消费循环

H5 主探索循环保持不变：持续领取 `ExplorerWorkType.EXPLORATION`，直到没有后续工作、达到动作预算或进入终态。

主探索结束后：

1. `probe_claim_enabled=false`：记录一次跳过日志，直接返回主探索的最后结果和已消费动作数。
2. `probe_claim_enabled=true`：继续执行现有 Probe Work 领取循环。

即使编排层允许 Probe，Repository 和 Turn Runner 仍必须再次执行补采策略校验，形成纵深防御。

## 5. 错误与终态语义

- Probe 不可用不是 H5 主探索失败，不得转换为 `technical_failed`。
- 主探索自身的浏览器、证据、规则或 Completion Gate 错误仍按原逻辑失败。
- 若错误代码绕过编排层直接领取 Probe，底层策略仍抛出强类型安全异常。
- 跳过 Probe 后若仍有非法非终态 Probe Work，Completion Gate 应继续阻断，不能伪造完成。

## 6. 测试设计

按 TDD 增加最小回归测试：

1. `probe_claim_enabled=false` 时，H5 Handler 完成 Exploration 消费后不调用 `ExplorerWorkType.PROBE`，并返回主探索结果。
2. `probe_claim_enabled=true` 时，保留原有 Probe 消费行为。
3. Repository 与策略层已有的补采关闭拒绝测试继续通过，证明安全门禁未被削弱。
4. 运行相关 H5 Runtime、补采策略和 worker 组合测试，确认无 App 语义回归。

## 7. 运行验证

实现完成后：

1. 运行定向测试与相关回归测试。
2. 保持 `ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED` 关闭。
3. 仅重启现有 Docker 容器，不 build。
4. 自动登录获取 token，不输出凭据或 token。
5. 重新提交原始 `POST /api/v2/tasks` 请求。
6. 持续观察任务状态、worker 日志、页面时间线、规则执行和报告。
7. 若出现新的独立根因，先补充失败测试，再做最小修复并重新开始质检。

## 8. 完成标准

- H5 主探索不再因 `claim_probe` 在补采关闭时失败。
- worker 保持健康，任务能够越过此前失败点。
- 最终汇报每次 task/run 标识、终态、质检结果和实际探索页面。
- 若任务仍失败，报告新的直接根因和已保存证据，不把技术失败描述为业务不通过。
