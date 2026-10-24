# 全分支最终审查待核对项

2026-09-11追加状态：下文保留原任务阶段的承接记录，累计审查现已完成，逐项裁决见[最终审查](final-review.md)。其三项Important已完成单次修复及223P限定回归，原审查者[限定复审](final-fix1-review.md)确认全部ADDRESSED、无新阻断；不倒改下文历史等待/失败文字。新增NA正例仅验证事务内封存，非新增报告绑定；旧无冻结声明/原身份正文NA须新Run重验。原7项未绿、四项Minor与六站/人工未执行范围保留，完整新证据见[修复记录](final-fix1-report.md)。联动修改selection正文/声明/摘要的任意数据库写入未测试；生产Processor有完整规则hash围栏，未发现正常入口绕过，不将单字段负向扩大为任意改库防御证明。

本文件整理截至 T9 Approved 的审查遗留与验证边界，不替代各任务原始审查、不表示 T10 已完成。后续最终审查需读取各任务净增量和最终源码，不以累计测试数量判断覆盖率。

## 已确认但尚未关闭的低优先级项

| 来源 | 项目 | 当前状态 |
| --- | --- | --- |
| task-8-review.md，M1/P3 | WebExplorationBudgetLedgerPort 未完整声明 `record_discovery` 和 discovery entry 的 `reserve_detail` 合同 | I1 复修仅同步 claim 返回及直接依赖的 entry 联合类型；其余仍待核对，不宣称全部修复 |
| task-8-review.md，M2/P3 | Agent Framework 实验性及 Starlette/httpx/anyio 弃用告警 | 既有依赖告警，未升级/屏蔽；与新产品失败区分 |
| task-9-review.md，M1 | 4项框架告警及本项新文件159项E501 | format检查与忽略E501的定向lint通过不等于全lint通过；不为本项升级共享依赖 |
| task-9-review.md，M2 | 能力revision测试required夹具含home | fix1已改为content_list/content_detail且复审确认；保留独立4P复验，不将原89P/1F组倒改为全绿 |

## 必须保留的验收边界

- T1～T9 的最终任务审查为 Approved；已关闭的 Important 不因本表重复列为未修复。T9 的 I1～I4 在fix1关闭、后续N1在fix2关闭，详见两次限定复审；T10状态以progress.md最新记录为准。
- T9 fix2最终105纯P；PG原46P/1F（新增历史different用check_point在冻结Packet先拒）后，仅该用例改为合法severity完整合同差异、实际两条模型判断→Writer拒合并定点1P，生产未再改。保留各原输出，不累加或倒改组结论。已修部分提交/terminal_error逐条来源、当前批次中stable-only成员、历史独立批次精确回溯；不能将批次failed粗映射为全体规则产品fail，T10需消费逐条状态。
- T6 的共享 H5 图读侧五项 JSONB 字符串失败只验证了相关源码相对该项起点和 HEAD 未变，没有另起干净 HEAD 全量重跑，不扩大为“所有 H5 失败都是旧问题”。不越过用户 Web 范围修复 H5。
- T9 的 `test_inspection_work_item_repository.py::test_claim_uses_skip_locked_and_session_owner_gate` 曾因既有 FakeConnection 给补采查询返回普通规则行，缺 `manifest_payload` 而失败。原实现者以内存替换 HEAD 仓储实现＋当前 fixture 定点同样复现，严格地说不是 clean HEAD 全量回归。主控随后用 T9 基线重建比较，整个测试文件的 LF SHA 两侧均为 `3bd7ef4925b62e525b3a9d7a3e736c832c31451720c5ff7cf672d8b26d5f0580`、差异为空，确认 fixture/测试函数未在本项变更。该只读对比不是新增测试通过；最终仍单列实际未绿范围，不为本项改旧 H5 补采解析。
- Windows 默认 Playwright Chromium 缺失导致的 skip、隔离 PG 未启用导致的 skip、实际 Chrome 与实际 PG 通过分别列出，不把跳过算通过。
- 浏览器受控 HTML、模型替身、提交替身、离场 Gate 替身等测试边界按各任务报告保留；局部真实 PG/Chrome 通过不代表 HTTP 提交到 Publication/Judge/InputSet/正式报告全部贯通。
- T8 首次独立真实链覆盖 href、无 href 新 URL、无 href 同 URL 拒绝和默认禁用；I1 复修后的真实 PG 和纯测试范围已单列，没有声称整组从零 Chrome 又重跑。
- T9 新增序列若只支持 snapshot/llm，最终报告必须明确执行器支持矩阵；拒绝未支持的确定性多帧配置不是已支持该功能。合法失败材料不等于稳定页面、业务通过或 required 覆盖。
- T9 首批能力负向限定完整可枚举的局部原生结构；一般产品能力仍未知。规则复用裁定限定显式允许、同一封存InputSet与完整上下文等价的重复消费，跨Capture/跨页面静态素材复用尚未启用，保留旧频率。最终审查应核对真实缓存命中/失效、授权先于复用、当前来源审计以及交付是否如实说明剩余设计范围；不能将计划hash或局部复用解释成全部规则频率策略完成。
- T9 早期两项真实默认 readiness 出口到 PG Writer 的闭链用例通过，后来发现新规则夹具缺少 `manual_review_policy.min_confidence`，实际正式状态为 error。原校验正确拒绝，不能把这些旧结果解释为业务 pass 落库。最终须核对合法夹具后的重跑及 Attempt/正式结果/Packet 状态断言；后续若已补证，以新鲜结果为准，同时保留此历史边界修正。
- T10 正式报告入口是报告中心，不能只测遗留 ReportDetailView 就声称任务列表和任务详情按钮均经过真实路由验证。
- T9最终138文件组实际为13 failed /2252 passed /23 skipped /17 errors，非全绿。Consumer独立11项通过，其中两条日志失败可由原迁移env的fileConfig禁用logger复现，10ms超时用例独立通过尚不足以证明组合原因；Web迁移head适配后的22项定点另列。旧formal DSN守卫17E及其他明确旧夹具失败详见T9最终路径/hash证据，不能将起点相同或局部替代测试解释成完整正式终态链已通过。
- 原 `test_inspection_work_item_postgres_integration.py::test_finalization_retry_quarantines_identical_error_and_admin_recovers` 缺 `lease_token/recovery_generation/expected_status` 已在 `task-9-report.md:272` 记录，原测试与 `task_finalization_repository.py` 全文相对精确T9起点未变；T10共享组再次失败，不能因首次在本轮简表补列就称新增生产回归，也不能当通过。两个明确 Web retry 参数化已由 T10 补齐真实冻结策略/Recorder 身份和仅该两用例 repeatable_read 外层夹具，定点 2P/15deselected/2.98s，见 task-10-report.md 第30项。此后共享原组仍保留9F/855P，而非重写全绿；7项实际未绿范围继续保留。
- 六站目录含不允许采集的内容分支；未安全执行的外站范围明确列为未执行或阻断，不由中性 HTML 测试冒充全站覆盖，不伪造成人声明或人工报告复核。
- T10 自然中性实采额外暴露严格门禁接线自锁、bootstrap 首帧无前驱选样、48KB语义事实预算不足投影漏绑定；主控已在 rulings-summary.md 的第29～31项裁定中纳入最小 Web 修复，并链接原 progress.md。原 T1～T9任务级批准不代替这些后续跨项增量的审查；T10报告应保留每次真实RED与最终对应用例结果。48KB边界不放宽，较小中性正常样例通过不能代表原超界材料已正常判定。
- 初始用户未提交改动和本轮改动应按基线区分；不自动 add/commit/push，不覆盖历史封存报告，不迁移主库。
- T10旧stable默认读取没有Packet持久Manifest-ID或序列InputSet绑定；允许真实Capture/Artifact/hash与同Capture PASS Manifest分层对账，但不能称为封存清单精确绑定。新技术Manifest与既有shadow/on路径兼容应以测试隔离组合的实际结果说明，不能全局切模式掩盖默认合同。此裁定见rulings-summary.md第32项；交互/预算不足序列仍要求原严格逐规则材料绑定。
- T10首审I1实际覆盖零发现/阻断误报complete、I2生产readiness出口原因丢失进入第一轮复修，未提前关闭。Minor N1既有告警、N2成功交互序列也显示在“失败序列材料”固定标题下暂缓处理，最终审查逐项裁决；不得将成功序列标题当产品失败事实。
- T10 fix1新增生产收口缺口：合法failure序列已发布/消费完成，h5_publication_gate仍无条件要求stable qualified库存，NULL与1不等导致正式Gate永久等待。Ruling34批准必要Web专属认证分支，仍保留普通Web stable/H5及其他批次/成员/孤立Work/终态检查。最终需核对逐规则InputSet/合同/Manifest与真实同Run/Epoch来源，不能只看非空sequence ID即豁免。此后task_finalization_repository.py可能首次被本项修改；前述旧finalization_retry失败的全文件“相对T9起点未变”证据只适用于该追加修复之前，不可冒称最终整文件仍未变。具体新回归以fix1末版结果为准。
