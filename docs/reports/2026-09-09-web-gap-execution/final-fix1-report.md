# 最终审查唯一修复波实施记录

2026-09-11，经主控恢复唯一实现者，仅最终审查I1/I2/I3；不是T10第二轮。完整读取final-fix1-brief.md与final-review.md（截断中段已补读），使用接收审查、系统化排查、TDD与完成前验证技能。不派代理、不提交、不操作主库/共享服务/六站。基线HEAD968566da87d0fecbb13c6abebae13d684e656341叠加final-review-fix1-package.md，原dirty及历史报告不覆盖。

## 合同与计划

1. 默认Runtime：真实默认assessor在off/shadow/on构建；仅模型客户端/Agent外部边界替身，复验已有默认PG冻结/冷恢复。
2. live聚合：复用既有六维状态，不另算覆盖；live完整通过与离线静态预检、明确请求子集分开。
3. local_absent：真实revision/selection/Publication到最终Gate，认证冻结局部证明、规则合同与来源；NA-only和混合均保留，unknown/不足不冒称NA。

所有实库命令复用task-10-test-runner.py，实际asyncpg.connect/create_pool仅允许55439/alioth_test_web_v2/alioth_web_gap，head20260910_0103，连接5秒/命令15秒/子进程600秒；凭据仅内存读取，不输出DSN或环境。未结束前不预记GREEN。

## 测试与实现记录

1. I1 RED：`.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_run_policy_snapshot_postgres.py -k 'default_assessor_builds or cold_provider_restores' -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-runtime-red`，exit1，`4 failed, 58 deselected, 2 warnings in 1.87s`。off/shadow/on和既有PG冷恢复均真实build在1080抛UnboundLocalError；不是AST证明或注入assessor。
2. I1最小修改仅删除web_runtime_composition.py内后置重复import一行，复用已有模块工厂。GREEN同runner两文件，selector改 `-k 'not migration and not constraints and not stores'`，basetemp=`final-fix1-runtime-green`，exit0，`60 passed, 2 deselected, 2 warnings in 2.61s`。未跑迁移降级，模型客户端与Agent替身不冒称真实模型；实际默认assessor构造及冻结/冷恢复成立。
3. I2 RED：`.venv/Scripts/python.exe -m pytest services/api/tests/test_web_six_site_shadow_fixtures.py -k 'live_overall_requires or offline_catalog_precheck' -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-live-red`，exit1，`10 failed, 104 deselected, 2 warnings in 1.26s`。其中缺required/缺快照为真实passed=True误报；其余新诊断属性缺失；离线测试误以为dataclass property drift_count包含在asdict输出中，已有exit0正确，修测试检查真实summary/缺站字段，不改离线产品。
4. I2最小接入LiveSixSiteReport的六维状态合取，保留旧验证/人工/正式Gate，新增固定维度失败诊断和requested_targets验收范围；复用既有实际覆盖计算。stdout JSON、Markdown、HTML和main退出码使用同一结论；子集明确selected_sites，不宣称全六站。全文件命令 `.venv/Scripts/python.exe -m pytest services/api/tests/test_web_six_site_shadow_fixtures.py -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-live-green` 原结果exit1，`9 failed, 105 passed, 2 warnings in 3.45s`：CLI合成声明用fixture-only不在已有枚举；仅内存测试改既有adult-v1枚举，不发任何外部请求或真实声明。相同完整命令basetemp改`final-fix1-live-green2`，exit0，`114 passed, 2 warnings in 1.84s`。覆盖完整/缺失/未知/复核/待判定/无人审/未绑定/缺合同/子集及离线预检；所有逐页人审都是合成测试事实。

两条warnings为原Agent Framework SkillResource/AgentFileStore，未屏蔽。组有重叠，不累加。I3正在用原0 census真实通知与Publication/Work到最终CTE复现，尚未记为通过。

5. I3 RED：`.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py services/api/tests/test_web_capability_revision_postgres.py -k 'final_na or final_mixed' -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-local-red`，session30597正常exit1，`2 failed, 4 deselected, 2 warnings in 6.98s`。真实0 census→默认通知持久revision→原Processor冻结local_absent selection；NA-only无Work/InputSet，混合真实消费一个generic Work，最终原CTE的h5_publication_gate.active_or_inconsistent均1而非0。不是声明产品fail，也没有把failure材料改stable；使用原合法interaction序列，浏览器测量/对象/模型仍为受控替身。

当前精确合同缺口：selection未保存rule_version/declaration/rule_contract_hash原输入，NA不建InputSet/Work。已向主控提出仅冻结这几个原输入并在Web专属Gate核验的最小方案，或为了旧历史NA兼容必须跨三个共享Gate入口只读重算原hash/plan；待裁定，不先增生产接口。

以上为当时检查点，随后 Ruling35/36 已批准并实现第一种方案：只追加原规则输入与原 canonical identity_json，未扩大三个共享收口入口、未重算当前可变规则、无迁移。旧缺声明/正文的 NA 保守拒绝，须新 Run 重验，不回填。

6. R35 冻结字段 RED：`.venv/Scripts/python.exe -m pytest services/api/tests/test_web_local_structure.py -k local_absence_does -q -rs --tb=short -p no:cacheprovider`，exit1，`1 failed, 6 deselected, 2 warnings in 0.39s`，真实返回缺 rule_contract_hash。追加 hash/version/declaration 并对缓存返回 deepcopy 后，`.venv/Scripts/python.exe -m pytest services/api/tests/test_web_local_structure.py services/api/tests/test_web_rule_evidence_contract.py -q -rs --tb=short -p no:cacheprovider`，exit0，`41 passed, 2 warnings in 0.73s`。篡改返回声明不污染后续缓存；不是取消缓存或以当前规则替代冻结版本。
7. R36 兼容性只读探测通过同 runner 的 main 身份预检，内联 asyncpg.connect 使用 ALIOTH_TEST_DATABASE_URL（不输出值），timeout=5、command_timeout=5，finally close。执行 SQL `SELECT pg_input_is_valid($1,$2) AS invalid_rejected,pg_input_is_valid($3,$2) AS valid_accepted,encode(digest($3,$4),$5) AS digest`，参数为 `not-json,jsonb,{},sha256,hex`；exit0，invalid_rejected=False、valid_accepted=True、digest=`44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`。函数名列名 invalid_rejected 实为有效性结果，False 表示非法 JSON 被识别；只验证 PG16 既有函数，未创建扩展/函数或迁移。相同第6项 RED 完整命令再跑，exit1，`1 failed, 6 deselected, 2 warnings in 0.37s`，缺 identity_json；随后冻结已有原正文，原字节摘要仍等于既有 reuse_key。
8. I3 首次 Gate GREEN：`.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py services/api/tests/test_web_capability_revision_postgres.py services/api/tests/test_web_local_structure.py -k 'final_na or final_mixed or local_absence_does' -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-local-green`，session98852，exit0，`3 passed, 10 deselected, 2 warnings in 6.82s`。这是原完整 Gate SQL 读取层，尚未覆盖后续真实 finalization。
9. 扩到实际收口的原失败：`.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py services/api/tests/test_web_capability_revision_postgres.py -k 'final_na or final_mixed' -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-local-finalize`，session14370，exit1，`2 failed, 4 deselected, 2 warnings in 12.94s`。11 项 selection 负向在两场景均真正执行 Gate 并拒绝；真实 TaskFinalizationService 的唯一 block_reason 为 EXPLORATION_NOT_TERMINAL，原因是该原能力测试没有正式用例所需的终态探索账本。经主控允许，复用原 formal 夹具补终态 qc_v2_work_item 与封账预算，不修改生产 Gate、不伪造 stable、不缩 required。
10. 修正合法收口夹具后：`.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py services/api/tests/test_web_capability_revision_postgres.py -k 'final_na or final_mixed or unknown' -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-local-finalize2`，session64754，exit0，`3 passed, 4 deselected, 2 warnings in 15.06s`。实际 OutboxRelay/Publication wakeup、consumer Run finish_if_idle、WebInspectionReconciliation、TaskFinalizationService.evaluate_and_seal 全部实际调用；NA-only 零 Work/InputSet，混合一个真实 generic Consumer/Writer/InputSet，unknown 无可信 revision、local 成员 evidence_insufficient、Publication needs_review。sealed 与真实 Gate=true 在外层事务内断言后 rollback；没有在这两个新增 NA 场景调用 ReportGenerationService 或报告 binding，不能把事务内封存验证称持久正式报告完成。
11. 定点格式检查仅四个直接文件：`.venv/Scripts/python.exe -m ruff check services/api/app/infrastructure/db/web_local_absent_gate_sql.py services/api/tests/web_local_absent_gate_pg_support.py services/api/app/application/web_local_structure.py services/api/app/infrastructure/db/web_sequence_completion_gate_sql.py`，最终 exit0，`All checks passed!`。此前新 helper 的格式收尾分别出现 3 errors（2 E501/1 I001）及 1 E501；按 Ruff diff 用 apply_patch 修正，仅新 helper，不格式化共享旧大文件或处理历史 M3。

### I3 验证层级与授权边界

- 11 项新 Gate 实际拒绝：identity_json 非法字符串、对象类型、数组正文；声明标量、越界 scope；规则 hash、版本、revision ID、未知 reason；缺正文/声明旧格式；重新计算合法摘要但替换来源 Capture 的自洽伪证明。均更新本测试 PublicationRule 后执行原完整 Gate SQL，再事务回滚；不是上轮数据库约束先拒的三类，未停用触发器以触达这些负例。
- 当前 Task finalization Run 换为本 Task 另一真实 Run 的额外负例由末版关联组覆盖；源 Run/Epoch/anchor、冻结 proof 与 Manifest ID/hash/Artifact 仍由 Web 专属谓词及原序列 CTE 联合认证。不是对任意 not_applicable reason 放行。
- 冻结 identity_json 最多 16384 字节，先 pg_input_is_valid 再受控 CASE 解析，原 UTF-8 字节 sha256 对既有 reuse_key；与持久 revision/source 交叉比较，哈希自洽不是授权。声明闭集与缓存深复制只影响 Web local structure，普通 stable/H5/App 默认合同不变。
- 浏览器 census/对象材料/模型使用原受控夹具；没有本轮真实 Chrome、远端模型、独立服务或六站访问。终态探索账本是合法测试支撑，不声称真实 Explorer 运行。正式失败序列原两出口 binding 回归独立列在末版组，不借此声称新增 NA 场景已生成报告。

## 末版限定验证与最终净文件

源码暂停后运行如下完整关联命令（600 秒硬上限，连接/命令仍有界），包含真实跨 Run 负例、默认 PG 冷恢复、旧双用途/跨平台库存门禁、原两出口正式绑定及零候选；不重跑全 Web、广组、浏览器或前端构建：

```powershell
.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_run_policy_snapshot_postgres.py::test_pg_cold_provider_restores_v1_and_same_process_new_run_reads_v2 services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_web_local_structure.py services/api/tests/test_web_rule_evidence_contract.py services/api/tests/test_web_capability_revision_postgres.py services/api/tests/test_web_sequence_completion_gate_postgres.py services/api/tests/test_web_report_readiness_formal_postgres.py -q -rs --tb=short -p no:cacheprovider --basetemp D:/project/Python/alioth/final-fix1-final
```

结果待完整退出后追加；不累计重叠数字。未改前端文件，所以本波没有重复既有前端构建，不能写成本波构建通过。

最终结果：session15397 正常 exit0，`223 passed, 2 warnings in 45.59s`，0 failed/0 error/0 skipped。上述等待文字保留为运行中的检查点，不是尚有测试未完成。两警告为已有 Agent Framework 实验接口；末版之后只编辑报告与专项规则，没有修改源码/测试。11 项新 Gate 拒绝与额外真实跨 Run 拒绝均在此组通过；原能力四场景、NA-only/混合/unknown、普通 Web stable/H5/App 缺库存拒绝、同 anchor 双用途及原正式失败两出口/零候选关联回归均保留。没有将重叠组加总，也未重复无关广组。

### 净文件清单与真实入口

相对 final-review-fix1-scope.json 的 UTF-8、CRLF→LF 标准比对，9 个既有文件有净变化；其余原251范围不变。原始字节 Get-FileHash 曾因 CRLF 报出大量假差异，已按包明确的 LF 合同比对纠正，未据此修改任何其他文件。

| 文件 | 本波职责 |
| --- | --- |
| services/api/app/application/web_runtime_composition.py | 删除函数内后置重复 import；实际默认 build 复用模块工厂 |
| services/api/tests/test_web_runtime_composition.py | 未注入 assessor 的 off/shadow/on 实际构造 |
| scripts/web_v2_six_site_shadow.py | live 六维 passed、诊断及显式选站范围；stdout/MD/HTML/退出码 |
| services/api/tests/test_web_six_site_shadow_fixtures.py | 完整/不足/未知/人工/绑定/子集及离线 CLI 受控回归 |
| services/api/app/application/web_local_structure.py | 原冻结身份正文及规则声明返回；缓存嵌套对象深复制 |
| services/api/tests/test_web_local_structure.py | 冻结字段/摘要与缓存污染防护 |
| services/api/app/infrastructure/db/web_sequence_completion_gate_sql.py | 原 NA 分支加入窄 Web 局部不存在认证谓词，旧闭集与其他条件不变 |
| services/api/tests/test_web_capability_revision_postgres.py | 真实 revision→selection→消费→Gate→事务内最终封存，unknown 保守分支 |
| docs/专项能力与高频回归规则.md | 追加默认构造、live 总体判定、局部 NA 最终认证三条 |

新增文件仅三份：

- services/api/app/infrastructure/db/web_local_absent_gate_sql.py：Web 专属最终认证 SQL，不新建数据库对象。
- services/api/tests/web_local_absent_gate_pg_support.py：11 项真实 Gate 负向、跨 Run 与既有正式生命周期支撑；终态探索账本是测试夹具。
- docs/reports/2026-09-09-web-gap-execution/final-fix1-report.md：本唯一新报告。

本轮实际复用但未修改的可执行支撑：docs/reports/2026-09-09-web-gap-execution/task-10-test-runner.py；task-10-fix1-pg-cleanup.py（仅无参数只读模式）；services/api/tests/test_web_v2_formal_report_e2e.py 内原隔离 fixture/队列/仓储支撑。没有新建报告目录执行脚本，没有修改旧 T10 报告/审查包；不派审查代理，独立复审由主控派发。

### 自审与未覆盖范围

I1 真实 RED 后只删一行局部 import，默认 off 未改变；I2 复用现有维度，无第二套覆盖/分母，HTML 继续显式转义；I3 冻结输入与独立持久来源联合认证，非法 JSON CASE、声明类型、旧格式和自洽伪来源均到实际 SQL 拒绝。没有给 NA 伪造 rule_id/InputSet/Work，没有改变 unknown、stable、required、预算、当前身份/租约/封存围栏，H5/App 原条件未放宽，无迁移或新接口。专项规则已同步。

遵循接收审查、系统化排查、TDD 和完成前验证技能：保留真实 RED、分别记录夹具失败，先等末版完整退出再声明通过。未派子代理、未 add/commit/push、更换分支或部署。本次三项阻断均有对应新鲜证据；历史旧广组未全绿和 M1～M4 非阻断 Minor 仍 deferred，没有在本波修复或以 223P 掩盖。六站/真实人工复核未执行，合成人审与模型/浏览器替身不算真实验收；新增 NA 场景只证明事务内 Task sealed，不宣称新报告生成绑定完成。旧缺冻结声明/正文的积压不会自动恢复，需新 Run 验证。

### 清理、校验与 STOP

- `.venv/Scripts/python.exe docs/reports/2026-09-09-web-gap-execution/task-10-fix1-pg-cleanup.py` 无参数，只读身份与剩余实体/连接；exit0，`TASKS []`，`COUNTS {'tasks': 0, 'rulesets': 0, 'sequences': 0, 'other_connections': 0}`，数据库 alioth_test_web_v2、role alioth_web_gap、head20260910_0103。本次未进入该历史脚本的 SET LOCAL replica/删除分支；新增封存用外层 rollback 清理，不停用触发器。测试 fixture 按原精确随机 Task/关联实体清理，查询连接 finally 关闭。
- 原生 PowerShell 对明确绝对路径逐个 Resolve-Path、验证仓库内 final-fix1 名称和无重解析目标/子项后删除：`D:/project/Python/alioth/final-fix1-final`（32 项）及 `D:/project/Python/alioth/final-fix1-runtime-red`（2 项）已不存在。均为可重新生成的测试临时产物，删除不可原地恢复；测试命令/结果已在本报告保存。
- 精确保留 `D:/project/Python/alioth/final-fix1-live-green`、`D:/project/Python/alioth/final-fix1-live-green2`、`D:/project/Python/alioth/final-fix1-live-red`：普通及精确授权上下文仍 Access denied，不能核验内部项或安全删除，因此没有改 ACL/扩大目录清理。这些为本轮离线合成 CLI pytest 临时目录，不是生产附件，可能继续产生 git status 权限警告。
- `git diff --check` 默认仓库设置 exit0，114 行既有 LF→CRLF 警告，无 whitespace 错误。曾尝试只对命令覆盖 `core.autocrlf=false`，将 CRLF 全部误当 trailing whitespace 产生大量截断噪音；该不适配的检查结果未作为真实净差/格式依据，也未修改 Git 配置或文件。随后显式保存默认检查真实 exit0；HEAD 仍为 968566da87d0fecbb13c6abebae13d684e656341。原19 dirty 保留。
- 本轮没有启动独立服务/容器/浏览器；自有 runner 会话均有完整退出，专库 other_connections=0。没有扩大权限枚举或停止身份未知的系统进程；不能据此声称做了全系统进程审计。没有连接主库或修改共享服务。

2026-09-11 本报告末次写入后：源码、测试、格式、专项规则和文档全部停止编辑。DONE_WITH_CONCERNS（上述历史/外部验收、旧格式 NA、三临时目录保留边界），ALL STOP / PG RELEASED。后续仅由主控封包并安排一次限定独立复审。
