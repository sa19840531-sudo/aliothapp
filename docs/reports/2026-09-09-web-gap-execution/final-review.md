# Web 六类缺口修复：最终累计代码审查

审查日期：2026-09-11。结论：**Ready to merge? No — 需关闭下列 3 项 Important。** 未确认 Critical；既有低优先级债务、共享 7 项未绿和六站真实目标验收缺口分别保留，不能互相抵销。

本报告采用 `superpowers/requesting-code-review/code-reviewer.md` 的全分支审查结构。审查者未修改源代码、测试、格式、Git/index/分支、数据库或共享服务，仅写入本报告；未再派审查子代理。

## 范围、版本与证据方法

- 实现范围：Web 默认 readiness、业务分类与图身份、Run 冻结策略、SPA 身份、预算和探索补图、真实序列 Publication/Work/Attempt/InputSet/Writer、正式 Gate 与新旧报告兼容；对照主计划全部 Global Constraints、相关专项和 R1～R34 裁定，不以任务级批准代替跨组件验证。
- 起点与当前 HEAD 均为 `968566da87d0fecbb13c6abebae13d684e656341`，没有新提交。因此不使用 `HEAD..HEAD` 空差异作为审查对象。
- 完整阅读 [最终累计包](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review-fix1-package.md) 的全部 251 文件、归一化 LF 后 2,241,031 字符，连续覆盖 0～2,241,031；对曾截断的输出补读，包含测试和初始 19 个 dirty 背景。另读 [scope](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review-fix1-scope.json) / [validation](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review-fix1-validation.json)：251 条、名称一致、changed/mismatches 为 0。主控完成全量磁盘重放且确认无漂移；审查者没有把该重放冒充自己的测试。
- 完整读取 [brief](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review-brief.md)、[carryover](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review-carryover.md)、[34 项裁定](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/rulings-summary.md)、[证据索引](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/review-evidence-index.md)、[主计划](D:/project/Python/alioth/docs/superpowers/plans/2026-09-09-web-design-gap-remediation.md) 与所需专项。疑点按任务净增量及基线归因，不把所有初始背景归为本轮新增。
- 不重跑已有同版套件。仅对 I2 执行主控明确批准的内存定点，见附录完整输入/命令/输出；无文件写入、网络或 PG。I1、I3 为当前源码可达路径证明，并与现有真实测试的覆盖边界交叉核对。

## Strengths

1. 领域边界总体保持清晰：readiness 的技术稳定、四类业务分类、页面图身份、实际 required 覆盖、规则结果和正式收口没有被合并成一个布尔值；网络状态未知也没有等同于零请求。
2. Run 快照冻结覆盖规则、分类/发现策略、预算等运行合同；SPA 的 `#` / `#!`、查询参数策略、可信别名和逻辑目标身份有明确边界。探索默认关闭，预算没有因重试或补图被悄悄重置。
3. 新载荷 Gate 与持久化 Manifest 的技术资格分层；同连接任务/离场锁、追加审计、最新未完成需求及 bootstrap 首帧规则有实测和负向证据。对 draining/sealed 收口没有以“修复”为名放宽新载荷的活跃授权。
4. 序列接入真实 Work、Attempt 和 Consumer，先租约/授权再模型与缓存；逐规则 InputSet 绑定精确 Manifest、合同和来源，Writer 保守处理各成员结果，不把批次错误粗映射成全体产品失败。48KB 上限未放宽，超界仍诚实不足。
5. 新旧报告读取/导出采用分层兼容、只读投影和安全输出；报告中心与任务详情入口有当前路由单元覆盖。Web 专属迁移和收口分支保留共享合同，而非整体绕开 H5/App Gate。

## Issues

### Critical（Must Fix）

未确认 Critical。以下为有具体生产路径或交付误报影响的 Important，不将既有告警、标题问题和未执行外站验收升级为安全漏洞或数据丢失。

### Important（Should Fix，阻断本轮交付）

#### I1 / P1：默认 Runtime 构建必经的局部名称未绑定

**位置：** [web_runtime_composition.py:1071](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:1071)、[1080](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:1080)、[1135](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:1135)。

同一构建函数在后面的 `mode != off` 分支内部再次 `import build_evidence_artifact_store`，使该名称在整个函数作用域成为局部变量。默认 `_assessment_agent` 未注入时，前面的 `WebExplorationAssessmentAgent(...)` 在 1080 行先读取该局部变量，尚未执行后置 import，抛出 `UnboundLocalError`。模块顶部已有同名 import 不能改变 Python 的局部作用域规则；即使 mode 为 off、后置分支不执行，也仍受影响。

**可达条件和影响：** 有效的规则/运行策略、目录与模型客户端完成前置构建后，正常生产默认 assessor 路径即触发；不要求启用探索或 full 模式。Web Runtime 无法初始化，后续探索/检测无法开始。注入 assessor 的测试路径会绕过这一读点，因此其通过不能证明默认生产构建正常。

**验证边界：** 当前 `test_web_runtime_composition.py:1621`、`:1791` 附近用例注入了 assessor。已有 [test_web_run_policy_snapshot_postgres.py:183](D:/project/Python/alioth/services/api/tests/test_web_run_policy_snapshot_postgres.py:183) 的默认 assessor 冻结/冷恢复测试，在 237～256 行仅替换模型客户端和 Agent，存在可暴露本回归的真实默认构建路径；不能说仓库完全没有相关测试。但较早通过记录不能覆盖之后引入的后置 import，末版 T10 所选组也不能证明该路径通过。本审查未重新运行该 PG 测试，结论依据当前函数的确定性语言语义。

**建议：** 移除函数内重复 import，统一使用已有模块绑定；保持原技术 Gate 和默认 off 合同。验证默认 assessor 在 off 与启用模式下的实际构建，并恢复使用上述默认路径测试，避免只给所有测试注入 assessor 来掩盖问题。

#### I2 / P2：完整 live 验收聚合未消费实际覆盖维度，能输出总体通过

**位置：** [web_v2_six_site_shadow.py:297](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:297)，关联 [web_acceptance_dimensions.py:79](D:/project/Python/alioth/scripts/web_acceptance_dimensions.py:79)。准确对象名为 `LiveSixSiteReport.passed`。

六维投影已正确将缺失快照标为 `unverified`、缺少 required 类型标为 `incomplete`；但总体 `passed` 只合取旧 `live_validation_passed`、人工复核、服务端 completion Gate。旧聚合的检查集中在已观察页面的分类、截图、inspection、pending 和重复项，不要求实际 required 覆盖完整。于是各站 `actual_coverage` 全部 incomplete/unverified，仍可总体通过。

**合法可达路径：** 这不只是随意制造相互矛盾的对象。服务端 [task_finalization_repository.py:1990](D:/project/Python/alioth/services/api/app/infrastructure/db/task_finalization_repository.py:1990) 接受 `complete` 或 `completed_with_gaps`，随后还按自身合同校验缺口；worker 的 3634～3660 行也处理该合法终态。现有 [test_web_report_readiness_formal_postgres.py:440](D:/project/Python/alioth/services/api/tests/test_web_report_readiness_formal_postgres.py:440)、[467](D:/project/Python/alioth/services/api/tests/test_web_report_readiness_formal_postgres.py:467) 已提供 coverage 有缺口、服务端正式 Gate 为真且有绑定的真实 PG 路径证据。因此“正式流程允许带缺口结束”与“六站完整验收成功”本来就是不同语义。通过旧检查的已观察页面、真实完整人工复核及该合法服务端结果可以组合，当前脚本未再检查缺失 required。

**影响：** [stdout 输出:2382](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:2382)、Markdown 1897 行、HTML 2190 行和 [live 退出码:2577](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:2577) 消费总体通过，导致交付摘要/自动化退出码误报。**这是验收脚本总体通过误报，不是后端业务 Gate 被绕过，也不证明已经执行过六个真实站点。** 附录内存探针得到两组 `passed=True`，分别对应六站 actual 全部 unverified / 全部 incomplete。

**阶段与开关建议：**

- 不带 `--live` 的现有离线分支使用 Catalog `drift_count == 0`（2582 行），它与 `LiveSixSiteReport.passed` 并不共用同一聚合。保留“静态预检成功”及其正常退出语义，不要求离线预检拥有外站证据或人工复核。
- `--live` 分支输出完整 live 验收结果时，至少应将实际覆盖 complete 纳入成功条件，并统一检查所有适用六维结果、旧验证、人工复核与正式 Gate/绑定。未执行/无快照保持 unverified，不能默认为通过。
- `--interactive-report-review` 只补充人工维度；不选择它时，不能伪造人工通过。`--site-id` 允许子集，输出应明确验收的是请求子集，不能因子集通过宣称全部六站通过。
- 当前脚本没有 `--phase` 开关；如保留局部检查模式，应显式表达阶段/检查状态并区分阶段成功与完整验收成功，不宜用 full 的要求使合法静态模式永久失败。

验证应保留“离线目录正常仍成功”、live 缺失/阻断/incomplete 不总体通过、合法全部 complete 才通过的分支差异；不必为了复现误报操作真实外站。

#### I3 / P1：R34 最终序列 Gate 拒绝生产可生成的 local_absent 不适用成员

**位置：** [web_sequence_completion_gate_sql.py:133](D:/project/Python/alioth/services/api/app/infrastructure/db/web_sequence_completion_gate_sql.py:133)，关联 [web_local_structure.py:118](D:/project/Python/alioth/services/api/app/application/web_local_structure.py:118)、[web_sequence_publication_processor.py:174](D:/project/Python/alioth/services/api/app/infrastructure/db/web_sequence_publication_processor.py:174)。

最终 Gate 的 `not_applicable` 原因白名单包含 channel、aggregate、URL、role、unclassified、page_type 等排除，但遗漏生产局部结构判定生成的 `WEB_LOCAL_NATIVE_STRUCTURE_LOCAL_ABSENT`。Publication Processor 会核对同 Capture 能力 revision 和序列不可变证明，使用局部计划写入 selection，对该 NA 成员不创建 Work；若同时存在可执行通用规则，则同序列还可正常发布/消费其 Work。

**可达条件和影响：** Web V2 full/enforced 的 snapshot/llm 序列，冻结规则显式要求对应 local_structure predicate；当前文档完整可枚举 light DOM 的原生结构 census 为零，形成合法 local_absent NA。该成员经真实生产判定排除后，最终 Gate 却因原因不在白名单而判不一致。无论 NA-only 还是与 admissible 规则混合，都可能永久阻挡正式绑定、进入重试/隔离，而不是产品规则失败。该路径可来自既有 Run 或绕过 I1 的已注入 Runtime，不能因 I1 当前更早失败就视为不可达。

**跨组件证据：** [test_web_capability_revision_postgres.py:70](D:/project/Python/alioth/services/api/tests/test_web_capability_revision_postgres.py:70) 的 0/1 原生结构用例，通过默认通知/真实 PG revision、Processor、实际消费和 Writer，断言 local NA 与 generic admissible（131～137 行）及正式结果（179～186 行）；它没有继续验证最终 Gate。R34 定点负向覆盖了伪绑定、错 Run/Epoch 等，却未覆盖这类合法 NA 正向。

全局 CTE 在 [task_finalization_repository.py:2157](D:/project/Python/alioth/services/api/app/infrastructure/db/task_finalization_repository.py:2157) 对 Web V2 已 published/not_applicable 序列调用新 predicate，2592～2597 行将 active/inconsistent 计入 `nonterminal_enforced_work_item_count`。因此不是只读解释问题，也没有后续全局分支抵消。原批次、成员、孤立 Work、Session 和普通 stable/H5 检查仍同时存在；不应通过放松这些检查修复。

**建议：** 在最终 Gate 中认证合法 local_absent NA 的同一冻结 capability revision、scope、predicate、规则合同及来源，然后允许这类真实 NA；不能简单允许任意 reason_code 或非空 sequence ID。补充 NA-only、NA+admissible 到实际 finalization 的正向，以及伪造/未知/跨 Run 负向。保持 required 覆盖、普通 Web stable/H5 和其余全局 CTE 不变。本项依据源码与既有真实路径交叉证明，未新开 PG 执行。

### Minor（Nice to Have，不单独阻断）

| 编号 | 位置、问题与可达影响 | 建议/裁决 |
| --- | --- | --- |
| M1 / P3 | [WebExplorationBudgetLedgerPort:80](D:/project/Python/alioth/services/api/app/application/web_exploration_budget_ledger.py:80) 未声明实际调用的 `record_discovery`，`:98` 的 `reserve_detail` 仍为静态 entry 合同。PG 具体实现已有方法，当前运行不因此失败；替代实现及静态检查无法依赖完整端口。 | 补齐端口与 discovery entry 联合类型；T8 claim 类型修复没有关闭全部协议债务。不扩大静态菜单业务枚举。 |
| M2 / P3 | [task-9-review.md:62](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/task-9-review.md:62) 及各原输出保留 Agent Framework 实验性、Starlette/httpx/anyio 弃用告警；不是产品失败。 | 独立跟踪依赖维护，不在本轮升级或屏蔽共享依赖。T8 M2、T9 M1 告警及 T10 N1 保留。 |
| M3 / P3 | 同一证据记录 T9 新文件 159 项 E501；忽略 E501 的定向 lint/format 通过不等于全 lint 通过。T10 后续四个新文件全 Ruff 通过不自动关闭其他 T9 文件债务。 | 明确有限检查范围，后续窄范围治理；本审查未重新跑全 lint，不能宣称当前全库精确仍为 159 条。 |
| M4 / P3 | [WebReportExplanationPanel.vue:32](D:/project/Python/alioth/app/workstation/src/components/WebReportExplanationPanel.vue:32) 固定标题“失败序列材料”也包住成功交互序列。可造成读者误解，但底层结果/状态未因此变成失败。 | 后续改为中性标题或按类型标注；T10 N2 暂缓可接受。 |

## 重点交接核对结果

| 交接 | 最终裁决 |
| --- | --- |
| 新 payload 活跃授权 ↔ draining/sealed 关闭 | 未发现应放宽授权的缺陷。真实关闭走 `capture_runtime.close_session`，先 draining、`seal_for_close` 再关闭/释放；数据库核对已持久化需求与 pending 状态，不是在关闭期间发起一笔新的活跃 payload 授权。保留新 payload 的 active 要求，不能为兼容关闭而接受 sealed Session 任意新材料。 |
| 分类 ↔ 图 ↔ SPA ↔ 实际覆盖 | 业务类型未知不抹掉稳定页面身份，图存在不代表 required 已覆盖；SPA 可信来源与身份策略保持冻结。脚本局部六维显示已修正，但总体仍有 I2。 |
| 预算 ↔ 补图来源 ↔ discovery | 保留预算消费/审计和重复授权边界；无可信目标的交互未知结果没有被自动当作新页面或盲点点击成功。默认禁用与首轮真实 Chrome 的覆盖不能推成全部变体永久验证。 |
| Run 冻结 ↔ 规则/Manifest/InputSet/Writer | 合同、逐规则证据、规则可复用范围、当前审计来源和 lease 检查总体连贯；一般产品缺失依旧 unknown，不把局部 DOM absent 推广为产品能力不存在。 |
| R34 ↔ 原全局 CTE | 不是以 sequence ID 非空就豁免：同 Run/Epoch/anchor/Session、输入集合、合同、Manifest/Artifact 等认证保留，原普通 Web stable/H5 和孤立 Work 等仍约束。但 local_absent 合法 NA 的交集遗漏形成 I3。 |
| 默认 Runtime ↔ 新 Gate 组装 | 新 Gate 本身局部测试不足以保护默认 assessor 依赖，I1 阻断正常初始化。 |
| 新报告 ↔ 旧 stable 默认读取 | 旧默认路径没有持久 Packet Manifest-ID/序列 InputSet，允许 Capture/Artifact/hash 与同 Capture PASS Manifest 分层对账，不冒称精确封存绑定；新序列仍严格逐规则绑定。旧报告不被回填为新事实，入口覆盖主要为 API/路由替身单元测试。 |

## Carryover 逐项裁决

以下逐项对应 carryover 的四条低优先级项和全部验收边界；“保留”不是通过，也不把已关闭任务发现重复算作本次 Important。

| 项 | 遗留/边界 | 最终裁决与阻断性 |
| --- | --- | --- |
| C01 | T8 M1 端口不完整 | 未关闭，见 M1；Minor，不单独阻断。 |
| C02 | T8 M2 依赖告警 | 保留，见 M2；不当作产品失败。 |
| C03 | T9 M1 四项告警及 159 E501 记录 | 保留，见 M2/M3；不能宣称全 lint/零告警。 |
| C04 | T9 M2 required 夹具含 home | fix1 已改为 content_list/content_detail，当前夹具吻合；独立 4P 关闭该项，保留原 89P/1F 组记录。 |
| C05 | T1～T9 Approved，T9 I1～I4/N1 | 任务级已关闭，不重开；T10 首审 I1/I2 也已在 fix1 按局部范围关闭。本次 I1～I3 为累计交接发现，不由任务批准免审。 |
| C06 | T9 fix2 105 纯 P，PG 46P/1F 后合法 severity 合同差异定点 1P | 保留各组原结论；check_point 早拒不冒充 Writer 拒合并。生产来源逐条状态、stable-only、历史独立批次回溯修复有效，不粗映射全部产品 fail。 |
| C07 | T6 共享 H5 图五项 JSONB 字符串失败 | 仍未绿。相关源码基线/HEAD 未变证据仅支持归属边界，不是 clean HEAD 全量通过；不越界修 H5。 |
| C08 | 旧 claim FakeConnection 缺 manifest_payload | 仍未绿；内存 HEAD 实现+当前 fixture 同样复现及测试文件 LF SHA `3bd7ef4925b62e525b3a9d7a3e736c832c31451720c5ff7cf672d8b26d5f0580` 两侧一致，支持非本轮 fixture 变更，不等同 clean HEAD 全回归。 |
| C09 | 默认 Chromium 缺失、PG 未启用的 skip | 跳过与实际 Chrome/PG 通过分别保留；skip 不计通过。 |
| C10 | 受控 HTML、模型/提交/离场 Gate 替身 | 局部真实 Chrome/PG 增强了代码合同证据，仍不等于六站 HTTP→Publication/Judge/InputSet→真实模型→正式报告全链。 |
| C11 | T8 href/no-href/default-disabled 实链及复修范围 | 首轮真实 Chrome 覆盖有效；I1 复修的 PG/纯测试不能改写成整组 Chrome 重跑。 |
| C12 | T9 执行器支持矩阵 | 新序列只支持 snapshot/llm；未支持确定性多帧配置明确拒绝，不等于已实现。合法失败材料不等于稳定页、业务 pass 或 required 覆盖。作为明确范围保留。 |
| C13 | 局部结构负向、规则复用范围 | 完整可枚举当前文档 light DOM 的 native absent 有限有效；一般产品能力 unknown。仅显式允许的同封存 InputSet/完整上下文等价复用，授权在缓存前；跨 Capture/页面静态素材复用和全部频率策略未启用。最终 Gate 有 I3，不能视为此交接已闭合。 |
| C14 | 早期默认 readiness→Writer 两例规则夹具缺 min_confidence | 原正式 error 是正确拒绝，不倒改成业务 pass。T10 后续合法夹具和 Attempt/正式结果/Packet 断言提供新鲜的窄范围补证，保留旧输出。 |
| C15 | 正式入口应为报告中心 | 已覆盖实际 ReportCenter 与任务详情入口逻辑，不只是旧 ReportDetailView；但 API/路由 mock 单元测试不是浏览器真实人工入口端到端。 |
| C16 | T9 138 文件 13F/2252P/23skip/17E | 不称全绿。Consumer 独立 11P、Web 迁移 head 后 22 项补证各自保留；两条 logger 失败可由旧 fileConfig 禁用复现，10ms 独立通过不足以确证组合失败原因，旧 DSN 守卫等边界仍在。 |
| C17 | 旧 finalization_retry 租约参数及两条 Web retry | 旧一项仍未绿；原“整文件相对 T9 起点未变”只适用于 R34 前，不用于当前最终文件。两条 Web retry 定点 2P/15 deselected/2.98s 关闭限定夹具问题，不把共享原 9F/855P 倒改全绿。 |
| C18 | 六站不允许采集内容分支、成人声明和人工复核 | 未安全执行的范围继续未执行/阻断；没有伪造成人声明、人审或以中性 HTML 冒充六站覆盖。独立阻断“真实六站已验收”的交付主张。 |
| C19 | R29 自锁、R30 bootstrap、R31 48KB 漏绑定 | 属已授权最小 Web 增量，RED→GREEN 与源码相符；保留严格 Gate、bootstrap 规则和 48KB 上限。较小中性样例通过不说明原超界样例业务通过。 |
| C20 | 初始 19 dirty 与本轮、仓库/主库安全 | 累计审查涵盖背景但按净差归因；未 add/commit/push、未覆盖历史封存报告、未迁移主库。本次仅写最终报告。 |
| C21 | R32 旧 stable 默认读取与新绑定 | 接受显式兼容边界：真实 Capture/Artifact/hash 与同 Capture PASS Manifest 分层对账，不声称旧 Packet 有精确 Manifest-ID。测试隔离 shadow→on 不是全局切模式或默认合同改写。 |
| C22 | T10 首审 I1 实际覆盖显示 / I2 readiness 原因 | fix1 的局部修复已成立，不重复标为原问题未修；新增 I2 是聚合没有消费已经正确的六维状态，I3 是追加 R34 的合法 NA 交接。 |
| C23 | T10 N1/N2 | 告警与“失败序列材料”标题分别保留 M2/M4；Minor，不单独阻断，不将标题误称真实产品失败。 |
| C24 | R34 专属最终 Gate 与最新回归 | 精确来源/合同/Manifest 认证及普通 stable/H5 约束保留，不能按非空 sequence ID 豁免。I3 说明合法 local_absent NA 尚未完整接入；阻断本轮交付。 |

## 测试证据与 Cannot verify

### 已有证据的准确读法

- [task-10-report.md:54](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/task-10-report.md:54) 原全 Web 组为 **5F / 1892P / 0skip / 4 warnings，558.36s**。后续迁移/闭链等定点修复分别关闭对应失败，不能把旧整组倒写为全绿，也不能证明晚于该组的 I1 不存在。
- [task-10-report.md:83](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/task-10-report.md:83) 共享组为 **9F / 855P / 0skip / 4 warnings，30.29s**；其中两条 Web retry 后续定点通过，以下 7 条仍未绿。
- T10 中性真实 Chrome/PG 五场景为 **5P / 0skip / 4 warnings，90.68s**，覆盖实际 Coordinator、PG 对象、Manifest、Consumer/Writer；模型、目录、传输/提交等仍有替身。前端较早 4 文件 40P、任务详情 28P、末次报告中心 7P/build exit 0 是不同检查范围，不累计为同版完整覆盖。
- [task-10-report.md:277](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/task-10-report.md:277) 起的 fix1 末版记录：格式前 9 文件 **224P / 2 warnings / 29.59s / exit 0**；四个新文件修整 138 E501、8 I001 后全 Ruff 通过；格式后真实 PG 定点 **5P / 2 warnings / 15.87s / exit 0**；前端报告中心 **7P**、build **exit 0**。这些是所列当前版本范围的证据，不是全分支零失败证明。
- 独立 API/Worker/PG/Redis/storage health ready 探针和一次独立远端模型探针分别成立，但不等于同一条六站 HTTP→Worker→远端模型→正式报告链路已经运行。
- 本审查额外执行仅附录内存探针；没有重新开专库、运行 Chrome、调用外站/模型或更改任何安全声明。

### 共享 7 项未绿（逐项保留）

| 测试 | 实际边界 |
| --- | --- |
| `test_page_graph_read_postgres.py::test_real_graph_tree_reads_quality_and_inspection_without_changing_coverage` | H5 图 JSONB `state_payload` 字符串形状失败。 |
| `test_page_graph_read_postgres.py::test_real_h5_frontier_targets_remain_unvisited_and_block_completion` | 同上，不越界修复。 |
| `test_page_graph_read_postgres.py::test_real_recovery_query_builds_continuous_routes_and_excludes_failed_edges` | 同上。 |
| `test_page_graph_read_postgres.py::test_task_graph_query_enforces_tenant_scope_and_authoritative_run_pointer` | 同上。 |
| `test_page_graph_projection_scope_postgres.py::test_real_detection_writer_promotes_local_state_route_only_after_loaded_observation` | 同上；不能扩大为所有 H5 失败均已证明是旧问题。 |
| `test_inspection_work_item_repository.py::test_claim_uses_skip_locked_and_session_owner_gate` | 旧 FakeConnection 补采行缺 `manifest_payload`；未绿且不是 clean HEAD 全量证明。 |
| `test_inspection_work_item_postgres_integration.py::test_finalization_retry_quarantines_identical_error_and_admin_recovers` | 旧调用缺 `lease_token/recovery_generation/expected_status`；R34 后仓储整文件已变，历史归属证据不能冒充最终文件未变。 |

### Cannot verify / 尚未完成的目标验收

1. 六个真实站点的全部允许范围、实际类型覆盖、真实浏览器任务完成及人工报告核查未全部执行；涉及不允许采集分支的范围不能执行，更不能通过替身证明。真实目标验收必须另行给出安全范围、执行结果和实际人工记录。
2. 没有同一末版的全分支全绿记录，也没有 clean HEAD 全量对照可把所有共享失败自动归旧。当前确认 I1～I3 需修复后做针对性验证；原输出继续保留。
3. 真实 PG/Chrome/对象/存储/传输局部链路不能证明真实站点的远端模型语义正确率，也不能证明替身之外的整条正式产品链已完成。
4. 确定性多帧执行器、广义产品能力 absent、跨页面/跨 Capture 素材复用、全部规则频率策略和 Judge 主动补采不在已实现/已启用范围，不以拒绝或保守 unknown 代替其完成声明。
5. 前端 API/路由替身测试和构建成功不能替代真实用户点击报告中心/任务详情后的全链人工验收。
6. 本审查不验证主库迁移、真实部署或共享服务运行状态。实现者报告的专用测试资源清理有独立范围和残留说明；本审查未新增此类资源，PG 55439 保持释放。

## Recommendations

1. 在冻结基线上统一处理 I1～I3，分别保留默认 Runtime、阶段感知验收聚合、合法 NA 最终 Gate 的回归证据；不要为修复而放宽活跃授权、required 覆盖、48KB、逐规则绑定或原全局 CTE。
2. 沿用已有实际默认构建测试和真实 PG 最终 Gate 支撑，增加准确遗漏的交接，不以更多注入替身掩盖正常构建。聚合修复可用内存合法响应验证，不需要执行不允许的外站内容。
3. 分开公布“代码合同修复完成”“所列测试通过”“真实六站验收完成”；保留共享 7 项未绿及人工/外站缺口。Minor 单独跟踪，不趁本轮升级依赖或改共享 H5/App 行为。
4. 修复具有复用价值的交接问题后，由实施者按仓库规则同步对应专项回归约束；本只读审查不自行修订规则或源码。

## Assessment

**Ready to merge? No。** 默认生产 Runtime 构建受 I1 阻断，合法局部不适用序列受 I3 阻挡正式收口，完整 live 验收又可被 I2 误报总体通过；三项均需先修复并验证。

- **代码合同：Needs fixes。** 大部分领域/权限/冻结/审计交接已成立，但不是所有关键正常路径可用。
- **测试：有限范围有新鲜通过证据，非全绿。** 保留各原失败/跳过/定点结果，不累加替代覆盖结论。
- **真实目标验收：未完成。** 六站外部与真实人工范围独立保留，代码修好也不能自动关闭。

源代码、测试、格式和共享环境继续 **STOP**，本报告没有授权合并、提交、部署或外站执行。

## 附录：I2 唯一新增内存定点

主控明确授权后，以 PowerShell 变量 `$reviewProbe` 保存以下源码，再执行 `.venv/Scripts/python.exe -B -c $reviewProbe`。只使用已有测试夹具与内存对象；没有写文件、连接 DB、提交任务或调用网络。`fixture-human` 是合成测试记录，绝非真实人工验收。

```python
from dataclasses import replace
from unittest.mock import patch
import services.api.tests.test_web_six_site_shadow_fixtures as fixture
from scripts.web_v2_six_site_shadow import _apply_manual_report_review

captured = []
original = fixture._aggregate_live_results

def capture(*args, **kwargs):
    report = original(*args, **kwargs)
    captured.append(report)
    return report

with patch.object(fixture, '_aggregate_live_results', capture):
    fixture.test_live_aggregate_accepts_only_complete_catalog_ledger()

report = captured[0]

def review(site):
    return (True, True, tuple(dict(
        task_id=site.task_id,
        run_id=site.run_id,
        logical_page_id=page['logical_page_id'],
        reviewer='fixture-human',
        reviewed_at='2026-09-11T00:00:00Z',
        reviewed=True,
    ) for page in site.logical_page_assessments))

reviewed = _apply_manual_report_review(report, reviewer=review)
print('missing_coverage', dict(
    passed=reviewed.passed,
    live=reviewed.live_validation_passed,
    manual=reviewed.manual_report_review_passed,
    gate=reviewed.server_completion_gate_passed,
    actual=[s.acceptance_dimensions['actual_coverage']['status']
            for s in reviewed.sites],
))
sites = tuple(replace(site, coverage_snapshot=dict(
    task_id=site.task_id,
    agent_run_id=site.run_id,
    coverage_snapshot_id='fixture-snapshot',
    snapshot_hash='a' * 64,
    finalization_epoch=0,
    required_page_types=['content_detail', 'content_list',
                         'video_detail', 'video_list'],
    observed_page_types=['content_detail'],
    missing_page_types=['content_list', 'video_detail', 'video_list'],
    unrecognized_page_types=[],
    coverage_status='completed_with_gaps',
)) for site in reviewed.sites)
gap_report = original(sites)
print('missing_required', dict(
    passed=gap_report.passed,
    validation_errors=gap_report.validation_errors,
    actual=[s.acceptance_dimensions['actual_coverage']['status']
            for s in gap_report.sites],
    missing=[s.acceptance_dimensions['actual_coverage']['missing']
             for s in gap_report.sites],
))
```

实际退出码 0，约 3.7s；导入时另有两项 Agent Framework ExperimentalWarning。业务输出如下：

```text
missing_coverage {'passed': True, 'live': True, 'manual': True, 'gate': True, 'actual': ['unverified', 'unverified', 'unverified', 'unverified', 'unverified', 'unverified']}
missing_required {'passed': True, 'validation_errors': (), 'actual': ['incomplete', 'incomplete', 'incomplete', 'incomplete', 'incomplete', 'incomplete'], 'missing': [3, 3, 3, 3, 3, 3]}
```

该探针证明聚合遗漏；其业务可达性由正文所列服务端 `completed_with_gaps` 正式收口路径支撑，而非把合成对象直接当成已执行的外站端到端结果。
