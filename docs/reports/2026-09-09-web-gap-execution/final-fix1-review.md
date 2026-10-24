**I1 / P1「默认 Runtime 构建必经的局部名称未绑定」— ADDRESSED。** [web_runtime_composition.py:1132](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:1132) 删除了函数内后置重复 import，1080 行默认 assessor 构造重新使用模块级工厂，off/shadow/on 均不再有该局部变量错误。[test_web_runtime_composition.py:1659](D:/project/Python/alioth/services/api/tests/test_web_runtime_composition.py:1659) 实际调用未注入 assessor 的 build，断言真实 `WebExplorationAssessmentAgent` 与 readiness 共用实例；模型 Agent/客户端和外部运行边界为替身，不冒称真实模型调用。已有默认 PG 冻结/冷恢复用例也在末版关联命令中，未以全量注入 assessor 掩盖原问题。

**I2 / P2「完整 live 验收聚合未消费实际覆盖维度，能输出总体通过」— ADDRESSED。** [LiveSixSiteReport.acceptance_failures:284](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:284) 合取原六维成功状态，其中 `actual_coverage` 必须 `complete`；[passed:317](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:317) 同时保留旧 validation、人工和服务端 Gate，并拒绝任何未完成维度。没有第二套 required 计算，也未改变后端合法 `completed_with_gaps` 收口语义。stdout、Markdown、HTML、live 退出码消费同一结论；`requested_targets` 来自既有聚合选站参数，明确 `selected_sites` / `all_six_sites`。无 `--live` 的目录预检仍独立按 drift 判断。[新增受控测试:1871](D:/project/Python/alioth/services/api/tests/test_web_six_site_shadow_fixtures.py:1871) 覆盖完整、缺 required、无可信快照、复核/判定 pending、无人审、未绑定、缺材料合同和子集，经实际 main 断言退出码与输出；[离线测试:1924](D:/project/Python/alioth/services/api/tests/test_web_six_site_shadow_fixtures.py:1924) 保留静态成功。合成人审/请求替身不是外站或真实人工记录。

**I3 / P1「R34 最终序列 Gate 拒绝生产可生成的 local_absent 不适用成员」— ADDRESSED。** [web_sequence_completion_gate_sql.py:137](D:/project/Python/alioth/services/api/app/infrastructure/db/web_sequence_completion_gate_sql.py:137) 对这一明确 NA 原因增加专属认证，而非将任意 NA 或非空序列 ID 放行。仍要求无该规则 InputSet/Work，原其他原因闭集、admissible 闭包、普通 stable/H5/App 和全局 CTE 条件保留。

- **冻结合同：** [web_local_structure.py:127](D:/project/Python/alioth/services/api/app/application/web_local_structure.py:127) 返回原规则 hash/version、完整闭集声明和已经生成的原 canonical identity JSON；104 行对缓存返回深复制，新增测试验证嵌套声明篡改不污染下一次结果。符合 R35/R36，不在最终化阶段重算当前可变规则，也不引入迁移或新材料合同。
- **最终来源认证：** [web_local_absent_gate_sql.py:5](D:/project/Python/alioth/services/api/app/infrastructure/db/web_local_absent_gate_sql.py:5) 联合持久 revision、精确 PASS Manifest、原 UI Artifact、规则集身份与版本/hash；31 行起核对当前 Task/Run/Epoch/anchor、同序列冻结 proof、局部 scope/predicate、零 census、可见层。52 行起对 selected 独立 contract_hash、声明闭集、原 identity 字段/摘要及原帧/Artifact 账本交叉检查。不存在即拒绝，一般产品能力仍 unknown。
- **受控解析：** 26～29 行用类型、2～16384 字节上限、`pg_input_is_valid` 和 CASE 保护正文解析；声明错误类型进入 FALSE。现有实库负向覆盖非法 JSON、错误类型、数组正文、声明标量/越界 scope、单字段版本/hash/revision、未知原因、缺字段旧格式和摘要自洽但来源 Capture 错误，均执行原 Gate 拒绝；另有真实跨 Run 拒绝，不把数据库约束先拒冒称此 SQL 拒绝。
- **实际正常路径：** [test_web_capability_revision_postgres.py:173](D:/project/Python/alioth/services/api/tests/test_web_capability_revision_postgres.py:173) 的 NA-only / NA+admissible 从真实 revision、生产 selection/Publication 出发，混合成员由实际 Consumer/Writer 消费；[web_local_absent_gate_pg_support.py:190](D:/project/Python/alioth/services/api/tests/web_local_absent_gate_pg_support.py:190) 调用真实 `TaskFinalizationService.evaluate_and_seal` 并断言 Gate=true / sealed。零 NA Work/InputSet 合同保留，unknown 走 evidence_insufficient / needs_review。
- **完成层级：** 两个新增 NA 场景的 sealed 在外层事务内断言后 rollback，未调用 ReportGenerationService 或建立新报告 binding；终态探索账本是合法测试支撑，非真实 Explorer 执行。原失败序列的两出口正式 binding 回归在末版组中独立保留，不能把它移借成新增 NA 报告已生成。

## New Breakage in the Fix Diff

**未确认新的 Critical / Important / Minor。** 本波变更未放宽授权、租约、Run/Epoch/父链/封存、48KB、required 分母、unknown 语义或普通 stable/H5/App 条件；专项回归规则追加三条与修复一致。

**非阻断防御边界：** [web_local_absent_gate_sql.py:53](D:/project/Python/alioth/services/api/app/infrastructure/db/web_local_absent_gate_sql.py:53)、62～69 行中的版本/声明与同一 selection 的 identity 正文互比，并用同一 selection 的 reuse_key 校验摘要；最终 SQL 并不独立重新推导“任意联动改写版本或合法 reuse_policy、正文和摘要，但保留旧 selected.contract_hash”的真实性。因此不能把单字段负例概括为抵御任意联动数据库篡改。

**该边界不构成本次已证实的正常业务绕过：** 为回答此疑点，仅补读 [web_sequence_publication_processor.py:93](D:/project/Python/alioth/services/api/app/infrastructure/db/web_sequence_publication_processor.py:93) 至 215 行、合同 hash 生成和 PublicationRule 表约束。生产发布/重试先验证完整规则合同，重新计算包含 input_contract 的 hash，与已冻结 selected.contract_hash 比较；不一致转 needs_review，之后才从认证过的规则参数生成 selection。表本身不提供整份 execution_plan 的不可变证明，但未发现合法 API 或正常 Processor 路径可以产生上述联动伪造组合。独立 revision/Manifest/Artifact/Run 来源认证确实存在，哈希自洽没有替代来源授权。本次未运行管理员任意写库式联动探针，也不宣称该防御深度已验证；如未来新增可写 selection 的入口，应另行评估，不借此扩展本轮修复。

## Checks / 验证范围

- 完整阅读 [final-fix1-brief.md](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-fix1-brief.md)（含 R35/R36）、[final-fix1-report.md](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-fix1-report.md)，重读主计划全部 Global Constraints；原三项发现以 [final-review.md](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review.md) 为权威原文。
- 按 scoped re-review 模板完整阅读 [净差包](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-fix1-review-package.md) 至 EOF：11 净文件（9 旧 + 2 新），LF 实读 53,840 字符，较简报 53,839 多终末换行一个字符；主控核实包 SHA256 `680a341bdff014a9333da27ac6f81510a241b9532badd68bf2e36356182f84e7`。未重新审查累计 251 文件，未重生成 git diff。
- Fix base 为 HEAD `968566da87d0fecbb13c6abebae13d684e656341` 叠加前次完整累计包，不是 T10 起点或 `HEAD..HEAD`。[scope](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-fix1-review-scope.json) / [validation](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-fix1-review-validation.json) 的 11 条净项一致；主控磁盘重放 11 一致、253 范围零漂移、HEAD 未变、diff-check exit 0。审查者读取这些记录，没有冒称自己又执行了磁盘重放或 Git 检查。
- [36 条裁定核验记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/rulings-final-fix1-validation.json) 数量一致，各项原文/来源/代价核验为真；R35/R36 的旧数据成本已落实为受控拒绝/新 Run 重验，未回填旧冻结审计。
- 实施报告保留根因 RED：I1 4F（含默认 PG 冷恢复），I2 首组 10F 中实际误报、缺诊断属性和离线测试自身误断言分别记录，I3 原最终 CTE 两例 2F。后续 CLI 枚举夹具错误、缺终态探索账本导致 EXPLORATION_NOT_TERMINAL 等中间失败没有被倒改为通过。
- 末版直接关联 8 个测试入口的完整命令及输出在实施报告：**223 passed / 0 failed / 0 error / 0 skipped / 2 warnings，45.59s，exit 0**。包含默认 PG 冷恢复、六维 CLI、局部结构/合同、NA 收口/unknown/负向、原 sequence Gate 与正式报告回归；逐项对照新增测试和生产调用，未发现用替身绕过本次三项根因。只接受该命令范围，不累加之前重叠组。
- 实施者四个直接文件的 Ruff 检查 exit 0；源码/测试在末版 223P 后未变，之后仅报告及专项规则。无前端净变更，本波没有重新构建前端，不能声称本波前端新通过。
- 本审查仅只读源码/报告检查并写此新报告；没有运行 pytest/PG/浏览器/网络、没有修改源码/测试/格式/Git/主库/共享服务，没有派子代理。已有测试和生产围栏已回答限定疑点，因此未使用条件性专库探针许可；PG 保持 RELEASED。

## Out-of-Scope Observations / 保留边界

- 没有新增范围外业务发现。原报告 M1～M4（预算端口完整性、依赖告警、历史 lint 债务、序列标题）继续非阻断 deferred，本波未关闭也未升级依赖。
- 原共享 7 项未绿继续保留：H5 图读/投影五项 JSONB 形状、旧 claim FakeConnection 缺 manifest_payload、旧 finalization_retry 租约参数。精确测试名与历史归属证据见 [原报告“共享 7 项未绿”](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/final-review.md)。223P 不是全分支全绿，也不是 clean HEAD 全量对照。
- 六站外部允许范围与真实人工报告复核仍未执行完成；合成 adult-v1 枚举输入、人审对象、模型/浏览器测量/传输替身不构成实际声明或实站验收。不运行不允许采集分支，不将中性/内存场景冒充六站覆盖。
- 新增 NA 场景仅验证真实事务内 sealed 后 rollback，不宣称新 NA 正式报告绑定；旧缺冻结声明/identity 正文的历史 NA 须新 Run 重验，不承诺积压自动恢复。
- 实施报告保留三个无法安全枚举/删除的本轮离线合成 pytest 临时目录 `final-fix1-live-green`、`final-fix1-live-green2`、`final-fix1-live-red`；未改 ACL、未扩大清理。其他精确自有临时产物已删除不可原地恢复，命令结果归档；专库只读清理检查 tasks/rulesets/sequences/other_connections 均零。本审查没有复做清理或主张全系统进程审计。

## Verdict

**Fix round：All findings addressed, no new Critical/Important breakage。** I1、I2、I3 全部 ADDRESSED；本次限定复审范围内没有剩余代码阻断。

**Spec：Approved（限定本波）。Quality：Approved（保留上述边界）。** 修复满足原发现和 R35/R36，默认生产构建、live 总体成功与合法 local_absent 收口已接通；没有通过缩分母、放宽来源/封存或改共享业务来消除失败。

**代码修复通过不等于完整目标验收完成。** 原共享未绿、Minor、真实六站/人工、旧 NA 新 Run 重验及新增 NA 未验证报告 binding 的边界独立保留；本报告不授权提交、合并、部署或外站执行。仅写本新报告，原最终报告未覆盖。**STOP。**
