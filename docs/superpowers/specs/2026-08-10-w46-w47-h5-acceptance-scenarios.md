# W46/W47 H5 页面探索验收场景设计

> 日期：2026-08-10
> 状态：设计完成，待 W15/W16/W29 接线后执行自动化验收
> 适用分支：`dev_1.5`

## 1. 目标

W46/W47 用于验证 H5 页面探索链路是否真正满足业务目标：给定一个产品 URL 后，平台能够按可审计步骤完成页面打开、线路加载、启动广告处理、连续弹窗处理、页面识别、页面探索、规则适配、证据留存和报告闭环。

本设计只定义验收场景、输入输出、证据要求和判定口径，不在当前阶段新增数据库迁移，也不假设 W15/W16 阶段机持久化和 W29 证据预检已完成。

## 2. 范围边界

| 工作项 | 本文定位 | 当前可完成内容 | 后续依赖 |
| --- | --- | --- | --- |
| W46 | H5 主链路验收场景矩阵 | 定义业务路径、前置数据、执行步骤、预期阶段结果 | W15/W16 阶段持久化、W17-W20 运行时接线 |
| W47 | H5 验收执行与判定闭环 | 定义验收报告结构、通过/失败口径、证据核对清单 | W29 证据预检、W40-W45 工作台详情 API |

非目标：

- 不实现新的 Browser Driver。
- 不实现阶段机落库。
- 不改规则判定逻辑。
- 不把验收通过伪造成生产链路已完成。

## 3. 被验收链路

```text
提交产品 URL
→ 创建任务与 Agent Run
→ 打开入口 URL
→ 线路加载页识别与等待
→ 启动广告倒计时阶段处理
→ 系统弹窗或浏览器权限弹窗处理
→ 连续业务弹窗处理
→ 稳定业务页面识别
→ 候选动作探索
→ 页面类型评估
→ 规则候选匹配与规则判定
→ 证据预检
→ 报告生成
→ 工作台展示验收结果
```

当前 W17-W20 已具备纯领域合同：

| 阶段 | 当前合同 | 主要输入 | 主要输出 |
| --- | --- | --- | --- |
| W17 启动广告倒计时 | `startup-ad-countdown-phase.v1` | 启动广告可见性、倒计时、关闭控件、稳定页标记、证据 ID | `waiting_countdown`、`ready_to_close`、`verifying_close`、`completed`、`blocked` |
| W18 连续弹窗 | `popup-chain-phase.v1` | 弹窗可见性、顶部弹窗签名、关闭控件、稳定页标记、证据 ID | `ready_to_close`、`verifying_close`、`completed`、`blocked` |
| W19 认证门禁 | `auth-gate-phase.v1` | 登录门禁、游客入口、关闭控件、稳定页标记、证据 ID | `ready_to_dismiss`、`requires_auth`、`dismissed`、`not_present`、`blocked` |
| W20 系统弹窗 | `system-dialog-phase.v1` | 系统弹窗类型、处理策略、操作控件、稳定页标记、证据 ID | `ready_to_handle`、`verifying_handle`、`handled`、`not_present`、`blocked` |

## 4. 验收数据准备

| 数据项 | 要求 | 说明 |
| --- | --- | --- |
| 产品 URL | 至少 3 个稳定样本 | 覆盖无广告、倒计时广告、多层弹窗三类入口。 |
| 规则集 | 固定 `ruleset_code` 与版本 | 验收期间禁止切换规则集，避免规则变更影响结果。 |
| 页面类型目录 | 固定目录版本和 SHA | 页面类型评估必须记录目录版本与 `catalog_sha256`。 |
| 账号策略 | 明确是否允许登录 | 默认不登录；遇到必须登录页面应进入 `requires_auth` 或人工复核。 |
| 证据存储 | MinIO 或本地对象存储可用 | 每个关键阶段必须能查到截图或语义证据 Artifact。 |
| LLM 配置 | 固定模型与单价 | 验收报告需要记录模型、Token 和成本。 |

## 5. W46 场景矩阵

### 5.1 主链路场景

| 场景 ID | 场景名称 | 输入特征 | 关键验收点 | 通过标准 |
| --- | --- | --- | --- | --- |
| W46-H5-001 | 普通首页直达 | URL 打开后直接进入稳定首页 | 页面识别、规则匹配、报告生成 | 任务终态为成功；至少一个稳定业务页面被识别；报告可查询。 |
| W46-H5-002 | 线路加载后进入首页 | 入口先出现线路加载页 | 线路加载等待、跳转后重新采集 | 不把线路加载页误判为最终首页；最终稳定页有独立 Capture。 |
| W46-H5-003 | 启动广告倒计时 | 入口出现倒计时广告 | W17 等待倒计时、关闭、关闭后确认 | 关闭前后均有证据；倒计时未结束前不点击禁用关闭控件。 |
| W46-H5-004 | 单层业务弹窗 | 首页出现可关闭弹窗 | W18 识别弹窗、关闭、确认稳定页 | 弹窗关闭动作有前后 Capture；稳定页不再带弹窗遮挡。 |
| W46-H5-005 | 连续多层弹窗 | 关闭一个弹窗后继续出现新弹窗 | W18 逐层关闭和预算控制 | 每层弹窗有唯一签名或证据；超过预算进入明确阻断而非假完成。 |
| W46-H5-006 | 系统权限弹窗 | 浏览器或系统弹出权限确认 | W20 策略处理 | 策略为允许/拒绝/忽略之一；不得随机点击高风险控件。 |
| W46-H5-007 | 认证门禁 | 页面要求登录或出现登录遮挡 | W19 识别门禁、尝试游客入口或转人工 | 不绕过登录；无游客入口时进入 `requires_auth` 或人工复核。 |
| W46-H5-008 | 底部导航探索 | 稳定页存在底部 Tab | Frontier 动作、来源页恢复、页面评估 | 每个可探索 Tab 产生动作记录和结果 Capture；无效点击不计为覆盖。 |
| W46-H5-009 | 详情页下钻返回 | 列表卡片进入详情页后返回 | 页面拓扑、来源恢复 | 详情页和来源列表页均有节点；返回后继续探索剩余入口。 |
| W46-H5-010 | 高风险入口拦截 | 页面存在充值、购买、上传、发布入口 | Action Policy | 高风险动作被拦截；报告显示拦截原因和证据。 |
| W46-H5-012 | 长页面分段采集 | 业务内容超过一个视口 | 原始分段、滚动位置、DOM 派生信息 | 每段材料可独立追溯；不得用拼接长图替代原始证据。 |
| W46-H5-013 | JS 与 SPA 导航入口 | 可点击入口没有静态 href | 动态候选、动作后页面验证 | 真实入口可被发现；只有新 Observation 能证明到达。 |
| W46-H5-014 | 多视口采集一致性 | 同一入口在多个移动视口运行 | 视口身份、响应式状态隔离 | 不同视口材料不混入同一份正式证据。 |
| W46-H5-015 | 外部跳转隔离与回源 | 动作打开外站标签或原标签跳出 | source、landing、return、标签页隔离 | 外部页面不增加业务覆盖；回源失败进入明确阻断。 |
| W46-H5-016 | 任务取消与资源释放 | 探索过程中取消任务 | 停止新动作、保留事实、释放会话 | 取消后无新增采集，任务不得显示为成功。 |
| W46-H5-017 | 证据缺口定向补采 | 已有规则结果缺少指定材料 | 缺口身份、恢复路径、新 Capture、追加结果 | 不重放历史坐标，不覆盖原始事实。 |

### 5.2 异常和恢复场景

| 场景 ID | 场景名称 | 输入特征 | 关键验收点 | 通过标准 |
| --- | --- | --- | --- | --- |
| W46-H5-101 | 首次采集失败可重试 | `goto` 或采集瞬时失败 | 有界重试 | 重试次数可见；失败后不写入伪页面结果。 |
| W46-H5-102 | 页面长时间不稳定 | 动态加载或遮挡反复出现 | 预算与阻断 | 达到预算后输出 `blocked` 或人工复核，不显示 completed。 |
| W46-H5-103 | 浏览器会话崩溃 | 页面操作中断 | 会话恢复或失败收口 | 恢复成功继续验收；恢复失败保留中断阶段和错误原因。 |
| W46-H5-104 | 证据缺失 | Artifact 写入失败或丢失 | W29 证据预检 | 无证据不得发布页面规则结果；报告显示证据不足。 |
| W46-H5-105 | 模型返回不完整 | LLM 未给出结构化动作或状态 | 合同校验与降级 | 不能进入假完成；保留失败工具调用和降级原因。 |

## 6. W47 验收判定闭环

W47 不只看任务是否 `done`，必须从任务、Run、页面、阶段、规则、证据、报告七个维度共同判定。

任务时间线与报告中心必须调用同一个 H5 终态投影，并使用同一 `task_id + agent_run_id` 下的 Work、Frontier、Coverage、PageTypeAssessment、Evidence Inventory、Inspection Closure 和 Action 身份集合。`complete`、`quality_status`、`requires_review`、`terminal_reason` 不得由两个读模型分别推算。

| 维度 | 必查字段或对象 | 通过标准 |
| --- | --- | --- |
| 任务 | `qc_task.status`、`report_status`、`failure_reason` | 终态与报告状态一致；失败任务必须有原因。 |
| Run | `qc_agent_run.status`、`termination_reason`、`usage`、成本 | Run 终态与任务终态一致；Token 和成本可追溯。 |
| 页面 | `qc_page_capture`、页面类型识别结果 | 稳定页面、遮挡页面、补采页面均能按 ID 追溯。 |
| 阶段 | W17-W20 决策结果 | 每次阶段动作都有 `source_capture_id` 和 `evidence_artifact_ids`。 |
| 规则 | `qc_rule_execution` | 正常 pass/fail/warning/manual_review 必须绑定页面证据。 |
| 证据 | `qc_evidence_artifact` | 关键截图、DOM 摘要、OCR 或可见文本证据可打开。 |
| 报告 | 工作台报告接口 | 报告与数据库聚合一致，不额外制造 bootstrap 结果。 |

## 7. 单场景验收记录模板

| 字段 | 示例 | 说明 |
| --- | --- | --- |
| scenario_id | `W46-H5-003` | 场景唯一编号。 |
| product_code | `ALIOTH-H5-E2E-STARTUP-AD` | 验收产品编码。 |
| target_url | `https://example.test/` | 被测入口。 |
| ruleset | `alioth_first_ruleset@1` | 固定规则集。 |
| expected_surface_phases | `line_loading,startup_ad,home` | 预期阶段路径。 |
| expected_page_types | `line_loading,home` | 预期业务页面类型。 |
| expected_rule_scope | `startup_ad,home` | 预期触发规则范围。 |
| pass_criteria | 关闭前后证据齐全，最终进入稳定首页 | 人可读验收口径。 |
| required_artifacts | `before_action_screenshot,after_action_screenshot,visible_text` | 验收所需的证据角色；运行时必须映射到真实 Capture 和 `evidence_artifact_ids`，不是新增数据库 Artifact 类型。 |
| risk_notes | 禁止提前点击禁用关闭按钮 | 风险说明。 |

## 8. 自动化落点

W15/W16/W29 完成后，W46/W47 应转化为以下自动化资产：

| 自动化资产 | 建议路径 | 内容 |
| --- | --- | --- |
| 验收样本清单 | `docs/data/h5_acceptance_scenarios.csv` | 场景 ID、URL、预期阶段、预期页面类型、规则集。 |
| 后端集成测试 | `services/api/tests/test_h5_acceptance_closure.py` | 使用固定夹具验证任务、阶段、规则、证据、报告一致性。 |
| 报告校验脚本 | `scripts/verify_h5_acceptance_run.py` | 给定 `task_id` 和 `agent_run_id` 输出通过/失败明细。 |
| 批次执行脚本 | `scripts/run_h5_acceptance_matrix.py` | 串行创建任务、等待终态、读取任务/Run/规则/报告/Timeline/工作项并逐场景保存快照。 |
| 页面行为夹具 | `scripts/serve_h5_acceptance_fixtures.py` | 在本机提供确定性的线路加载、开屏、弹窗链、导航、长页和外跳页面，并通过 Manifest 声明能力。 |
| 工作台验收说明 | `docs/superpowers/plans/` | 记录手动验收步骤、截图要求和异常处理。 |

批次执行默认只允许访问本机 Alioth 服务。CSV 中的 `example.test` 是场景占位地址，执行前必须通过 `--target-map` 提供真实地址，或通过 `--fixture-base-url` 指向可访问的确定性夹具站。非本机服务必须显式增加 `--allow-non-loopback`，避免误向测试或生产环境批量创建任务。

该脚本负责真实任务的创建、等待、取数和闭环校验，不在生产 API 中注入故障。`W46-H5-016` 由执行器在首批事实落库后调用正式任务取消接口，并校验任务/Run 终态、事实静默、Surface 资源释放和质检工作收口。`W46-H5-017` 使用受控遮挡夹具：先从内容列表进入详情并形成遮挡，正式规则链产生目标规则 `alioth_12` 的 `after_close + active_session` 请求且完成选路后才开放关闭控件，任务或 Run 提前终止则立即失败。`W46-H5-101` 由夹具在同一批次首次文档请求时断开连接，第二次返回稳定页面；执行器校验持久化重试次数和最终 Capture 证据。`W46-H5-102` 使用持续变更 DOM 的确定性页面夹具，验收器要求 Task/Run/Timeline/Report 以 `review`、`failed/incomplete` 和未完成摘要一致收口，同时核对稳定等待采样、阻断原因、截图以及没有正常规则结果。`W46-H5-103` 的任务时间线已经提供 `browser_recovery` 审计，验收器会检查有界 attempt、独立恢复 Session、状态等价、崩溃前动作和恢复截图；但故障本身仍须在隔离环境由受控驱动触发。`W46-H5-104` 使用 Inventory、Rule Execution、Publication 与 Completion Gate 共同形成 `h5-evidence-preflight.v1`，任务时间线和最终报告必须读取同一预检结论；对象缺失时禁止正常规则结果和 Inspection Work 发布。`W46-H5-105` 为 Explorer 输出异常提供稳定错误码：唯一安全且已验签的候选可以生成带 `h5-explorer-decision-fallback.v1` 审计的确定性 Action，否则 Work 进入有界重试。任务级验收通过 `h5-model-contract-audit.v1` 核对降级原因、Action 完成状态和结果截图。`W46-H5-017/103-105` 涉及补采、浏览器崩溃、存储失败或模型异常，不能拿普通线上 URL 冒充这些故障场景已经通过。

```bash
python scripts/serve_h5_acceptance_fixtures.py \
  --host 0.0.0.0 \
  --port 9080

python scripts/run_h5_acceptance_matrix.py \
  --base-url http://127.0.0.1:8000 \
  --fixture-base-url http://127.0.0.1:9080 \
  --fixture-target-base-url http://host.docker.internal:9080 \
  --scenario-id W46-H5-001 \
  --username admin \
  --password-env ALIOTH_ACCEPTANCE_PASSWORD
```

需要验证 H5 补采消费端时，Worker 必须改用本地隔离入口
`python -m services.api.app.h5_probe_lab_worker`。该入口要求非生产环境、
`ALIOTH_WORKER_ROLES=explorer`、`ALIOTH_H5_V2_MODE=on`、
`ALIOTH_WEB_V2_MODE=off`、`ALIOTH_APP_QC_ENABLED=false`；任一条件不满足即拒绝启动。
普通 Worker 和生产组合根继续使用默认未就绪策略，不能通过设置
`ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED=true` 绕过运行时验收。

本地补采验收只保留一个 `h5_probe_lab_worker`。不得再启动普通 Worker，也不得用
`worker.main(h5_supplemental_evidence_policy=...)` 手工启动额外 Judge Worker；注入策略只对
H5 Runtime 组合根生效，其他质检消费者仍按默认失败关闭策略运行，额外进程会抢先领取工作项并把
补采请求改写为 `SUPPLEMENTAL_EVIDENCE_NOT_READY`。运行时异常或 `W46-H5-017` 系统门禁失败后，
批次执行器立即停止创建后续任务，剩余场景记为 `not_executed`，待根因修复后从门禁场景重跑。

`--fixture-base-url` 由宿主机执行器访问，用于读取 Manifest；`--fixture-target-base-url` 会写入任务，必须能从 Worker 的浏览器运行环境访问。Alioth 直接运行在宿主机时两者可使用同一个 `http://127.0.0.1:9080`。夹具监听 `0.0.0.0` 只用于本机容器联调，验收结束后应停止服务，不对外长期开放。

页面行为夹具当前覆盖 `W46-H5-001/002/003/004/005/007/008/009/010/011/012/013/014/015/017/101/102`。`W46-H5-006/016/017/103/104/105` 分别还需要浏览器权限、任务取消、补采、浏览器崩溃、证据存储失败或模型合同失败驱动器执行。`W46-H5-017` 的受控驱动只有在正式规则链产生并完成选路的 `active_session + required_interaction` Evidence Request 后才开放关闭控件；普通隐藏内容不能代替定向补采。场景必须同时证明 Evidence Request 已满足、产生 `probe_result_replay` Capture 且补采请求全部收口。批次脚本在创建任务前读取 `/_manifest`；能力不满足时整批失败，不产生伪验收任务。

批次同时包含普通夹具场景和外部受控驱动场景时，普通场景继续串行执行；
Manifest 已声明但当前未提供的受控驱动场景以 `blocked` 写入批次结果，并且不会创建
业务任务。Manifest 完全未声明的未知能力仍在预检阶段阻止整批执行。`blocked` 不计为
通过，批次返回非零退出码，待对应负责人提供受控驱动后再单独重跑。

## 9. 当前可执行验收检查

在 W15/W16/W29 尚未完成前，只允许做以下轻量检查：

1. W17-W20 单元测试必须全绿。
2. PageTypeAssessment 合同测试必须全绿。
3. 验收场景样本必须能映射到现有页面类型目录。
4. 所有预期通过的阶段都必须声明需要的 `page_capture_id` 和 `evidence_artifact_ids`。
5. 验收报告不得把 `blocked`、`requires_auth`、`evidence_insufficient` 显示为成功。

## 10. 完成定义

W46/W47 设计完成的标准：

- 场景覆盖普通首页、线路加载、启动广告、连续弹窗、系统弹窗、认证门禁、导航探索、详情返回、高风险拦截和异常恢复。
- 每个场景有明确输入特征、关键验收点和通过标准。
- 验收闭环同时检查任务、Run、页面、阶段、规则、证据和报告。
- 明确当前阶段不能伪造运行时能力，后续接 W15/W16/W29 后再做自动化执行。
