# H5 流程 InputSet、Judge、正式报告与有界连续观测

日期：2026-10-16。需求分支：`h5-chain-reliability-and-entry-compatibility`。
起点 HEAD：`c9adb731ab1f5209b8ae1d43b8daed9443d71afd`。本轮代码未提交、未部署；现有脏工作区和历史证据保留。

## 1. 完成标准与边界

- 冻结检查点只消费同 Task/Run/Session/Epoch、真实动作及前后 Capture 的最新正式 Manifest，不使用最长路径或截图数量推断流程。
- 复用既有聚合 InputSet、Work、Observation Packet、规则结果和报告持久化；不引入第二条规则执行队列。
- 预期不能复制成实测，证据不足/待复核不能算通过；实际反例产生 `fail`，不是伪造缺证或通过。
- 连续观测在原动作后的采集合同内完成，不重放动作，不新增模型请求或浏览器，不改期限、动作或物理调用预算。
- 只运行隔离数据库和自有禁网浏览器验证，不操作现有 API/Worker/前端、业务数据库或原站任务，不写原表、不发布。

## 2. 确认的问题与修复

| 问题 | 复现及修复 |
| --- | --- |
| 普通 journey 标记的最长路径可被选作流程输入 | `publication-longest-red.xml` 先失败；H5 的 journey 输入必须与唯一冻结规则合同对应的 V2 Plan、全部 bound 尝试及真实动作闭合。无映射、重复场景或部分完成继续不足。 |
| 观察完成可能被误当规则通过 | 新通用 Judge 比较可信快照中的注册事实及冻结预期；未知、错 Epoch、跨实例或缺依赖保持不足。实际暂停后时间前进为 fail。 |
| 连续样本可属于另一个播放器 | `other-instance-red.xml` 先失败；采样与实际 after Capture 的 URL、文档时间源及媒体实例均须一致。 |
| 单个 PNG 被重复使用为两个样本 | `sample-reader-red.xml` 保留；精确附件读取验证归属、大小、内容类型和摘要，两次样本须对应两个实际 PNG 制品引用。相同内容可对应不同采样，但不能冒充两个独立业务页面。 |
| 正式报告投影丢弃流程结果 | `report-consumption-red.xml` 先失败；两条 H5 正式报告绑定路径添加精确 `h5_journeys`，报告 API 保留并核对同 Run/Epoch、缺口、结果一致性和摘要。 |
| 浏览器真实几何字段与测试不一致 | `browser-first.xml` 首次原生 Chromium 失败，实际快照使用 `rect`，旧受控数据是 `bounds`；测量优先 rect、兼容 bounds，不靠重测掩盖。 |
| 屏外视频仍能被测为停止/稳定 | `offscreen-red.xml` 先失败；除了 visible 标签，还须实际几何在视口内至少显示一半，拒绝遮挡、加载、seeking 和截断快照。 |
| 两次观测虚增动作回执数 | `receipt-count-red.xml` 先失败；双样本只支持 screenshot/media_state。动作回执仍来自一次实际动作账本，要求两份回执的合同在规划前阻断。 |

缺少模块的最初红测、测试环境 SQL_ASCII 编码错误及测试 fixture 漏填 session_draining 时间也保留，但不将这些测试环境错误算业务根因。

## 3. 接线与精确绑定

1. `H5AdaptiveJourneyRunner` 在 reserve 前取真实冻结检查点，仅在需要连续观测时为原 after Capture 加入采样制品需求。原参数、source、before Capture、deadline 和 Coordinator 不变；重领 bound/gap 尝试不再执行动作。
2. `journey_input_repository` 重新核对动作 Intent、归属、创建时间、Capture 角色/父链/命令序号、附件闭包、最新 Manifest、页面采集就绪及自然可见展示。生成 `journey-input-contract.v1` 和 selection_hash。
3. 聚合发布原事务内写 InputSet、不可变流程合同、Work、Candidate 与 Outbox。封存前复核最新选择，幂等不覆盖。
4. 聚合执行在既有租约下读取精确 InputSet 合同及可信 UI/采样附件，由 `journey_observation` 路由调用通用检查点 Judge，仍由原完成事务持久化规则结果。
5. Completion Gate 用真实已消费输入/结果计算 `journey_result_gap_count`，不再因“有 V2 Plan”无条件阻断，也不因 observed 就放行。完整实测 fail 是已执行的失败结果，不是业务通过；待复核、缺口、旧输入和最新失败 Manifest 仍阻断。
6. 正式报告绑定重新复核最新输入和结果，冻结 Plan、场景、规则哈希、InputSet ID/哈希、selection_hash、执行 ID 与逐检查点 Capture/Manifest 引用。后续 API 投影不按新页面状态重拼历史结果。

新增迁移 `20261016_0137` 保存 `qc_journey_input_contract`，插入受当前 Epoch 约束，更新禁止覆盖；已有输入时 downgrade 拒绝抹去。只在隔离测试库升级到 head，未迁移业务库。

## 4. 稳定帧与时间观测的确切含义

- 同一原会话、同一次 after Capture 内读取两次原生媒体状态和两张实际 PNG；中间等待 0.6 秒，不点击、不导航、不请求模型。
- 用宿主单调时钟记录 UI 读取后的时间；有效样本间隔 500–5000 毫秒。PNG 保留帧来源 bracket 与摘要，附件读取再次核对实际存储内容。
- `stable_frame` 当前限定原生播放器：同 URL/文档/媒体实例、已加载、自然可见、非 seeking、实际视口几何有效且两次几何一致。不是整个页面逐像素不变，也不声称视频内容停止绘制。
- `media_time_stopped` 须两次 paused=true，实际 current_time 差不超过 0.01 秒；时间前进为 false，无法可靠测量则未知。
- 截图与 DOM 是有界顺序 bracket，不声称浏览器原子快照；只有采集/来源校验通过的样本可消费。
- 两张附件增加只读采集 I/O 和等待，不增加动作/model 调用，不修改 180 分钟、80 动作、200 物理调用上限，也不补算独立页面覆盖。

## 5. 验证记录

原始 XML 和源码指纹目录：`docs/validation/2026-10-16-checkpoint-consumption/`。所有失败版本保留，重跑仅针对已修改代码的隔离回归，未重跑业务验收。

| 范围 | 确认结果 |
| --- | --- |
| 扩展回归，屏外/seeking 修复后 | `final-regression-v2.xml`：1554 passed，0 failed/error/skipped。 |
| 最终回执计数修复后的扩展回归 | `final-regression-v3.xml`：1555 passed，0 failed/error/skipped，201.63秒。 |
| 回执计数、精确绑定、观察、Reader、原生浏览器 | `final-count-safety.xml`：79 passed，含两个真实禁网 Chromium 案例；与扩展套件重叠，不累加。 |
| 隔离 PG 主套件 | `final-postgres-v2.xml`：70 passed。 |
| 最新计数修复与迁移回滚保护后的 PG 回归 | `final-postgres-v3.xml`：50 passed；`final-postgres-v4.xml` 同口径70项 passed，0 failed/error/skipped，不重复累加。 |
| 原生浏览器及相关安全链路 | `final-native.xml`：46 passed；与上述套件部分重叠。 |
| 静态校验 | 本轮相关文件 Ruff 通过，git diff --check 通过。 |

隔离 PG 实际验证 InputSet 幂等发布、Work 领取、真实规则 pass/fail 行、报告精确消费、错误 Run/Epoch、最新 Manifest 失败撤销绑定资格、合同不可改及已有输入禁止回滚。其 UI 读取使用受控桩，不能冒充原站 DOM。

浏览器实际验证采集器在禁网自有 Chromium 中保留两份 PNG 和原生视频状态；媒体是 Canvas captureStream，不是原站影片。空播放器正例/反例均保留，未做原站支付、真实视频详情、模型费用或180分钟长测。

隔离 PG 数据：`/tmp/alioth-journey-contracts-20261016-pg`；端口56436，角色 `alioth_contract_test`，UTF8 库 `alioth_checkpoint_consumption_utf8_test`。SQL_ASCII 初次失败库保留。结束时核对 postmaster.opts 及 PID18472 后，仅停止本轮自有测试 PG，状态已确认无该测试服务器运行；保留全部数据目录，其他服务未停止。

## 6. 剩余阻断与未验证

- 本轮注册的是原生媒体/文档事实的确定性 Judge，不是任意自然语言规则。未注册 surface 页型、network、自定义/私有播放器、超过两样本或新实例恢复测量继续拒绝准入。
- 原示例完整 Plan 包含未注册 surface 条件及两回执义务，不能据本轮局部暂停案例宣称完整示例已验收；必须明确冻结可测量的真实合同，不偷偷改历史 Plan。
- 多场景共享同规则哈希缺明确选择关系时仍不足，不按最长流程合并；缺规则哈希/未绑定规则或部分检查点完成不能生成完整流程 InputSet。
- 依赖失败阻断后续检查点时，未观察的下游仍是缺口，不将整个计划简化为已完成。晚到 Manifest 使旧输入失效时不自动覆盖或重封存；需明确后续 Epoch/复核流程。
- 损坏的源载荷/附件会 fail-closed，部分格式错误仍作为技术异常处理，不承诺全部能自动转为业务证据不足。
- 后台充值无确认只读契约继续阻断，私有高清无原站显式绑定仍不可猜配对；没有靠一律放行或提高预算解决。
- 正式 JSON/报告 API 已消费，未新增前端流程时间线或 HTML 专用展示；本轮未提交/部署/迁移业务库，未证明真实支付和视频覆盖无回退，不能作为发版批准。
