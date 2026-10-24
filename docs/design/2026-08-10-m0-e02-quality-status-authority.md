# E02 冻结口径：证据质量状态唯一权威与 quality_gate 退化范围

| | |
|---|---|
| 对应任务 | 任务表 E02（M0，P0） |
| 起草 | 立川（2026-08-10） |
| 评审 | 天枢、青云（见文末留痕表） |
| 状态 | **已冻结 v1**（2026-08-13 三方确认完成，见文末留痕表） |
| 关联文档 | [E01 证据角色取值表](2026-08-10-m0-e01-evidence-role-value-table.md) |
| 契约测试 | `services/api/tests/test_m0_evidence_freeze_contracts.py` |

## 0. 一句话结论

**证据齐全性只在清单（`qc_evidence_manifest.quality_status`）裁决一次；判定侧退到只管「载荷预算」和「按清单投影」，不再判断证据齐不齐。**

现状恰好相反：清单层零生产写入方（空转），真实裁决全部在判定侧。所以本文档冻结的是**目标口径**，第 2 节给出现状差距作为评审依据，第 3 节把每条口径映射到后续任务（E07–E27）。

## 1. 冻结条款（规范性）

### 1.1 唯一权威

- 「本次采集的证据是否齐全」由且仅由**清单写入侧**裁决，结果落在 `qc_evidence_manifest.quality_status`。
- 裁决发生在采集提交时刻（E07 `EvidenceManifestWriter`），**一次裁决，全链路复用**；门禁、补采、封板、判定一律读清单结论，不得重算。
- 除清单写入服务外，任何组件不得写入或推断证据齐全性结论。

### 1.2 取值域（冻结）

| 取值 | 语义 | 约束 |
|---|---|---|
| `pending` | 证据提交在途，齐全性未裁决 | 计入「在途数」，阻止离场与封板 |
| `pass` | 齐全性裁决通过 | 唯一可发布 `evidence-bundle-ready` 的状态（`contracts.py:552` 已强制） |
| `fail` | 齐全性裁决不通过 | 必须携带 `quality_reason`（`contracts.py:181` 已强制）；E15 只读该结果并阻断离场，补采不由 E15 自动发起 |

- 取值域**封闭**：不新增第四态。降级、告警放行等语义一律不进入本字段（见 1.5、1.6）。
- 库层已有 CHECK 约束（`migrations/versions/20260803_0026_v2_control_plane.py:260、275、278`），与契约层一致，保持双锁。

### 1.3 判定侧保留职责 = 载荷预算 + 投影

判定侧（rule_judge / observation_packet / semantic_rule_judge）保留且仅保留：

1. **载荷预算**：packet 字节预算（`observation_packet.py:393`，180KB）、prompt token 预算（`semantic_rule_judge.py:545`，默认 80k）、可见文本/DOM/UI 元素截断上限。预算是判定侧自己的资源约束，与证据齐全性无关。
2. **投影**：把清单指向的附件读出、校验（checksum 比对）、按规则需要投影成 `ObservationPacket`。只读，不改写、不补写原始证据（E20–E23 落地）。

### 1.4 判定侧退出的职责（退化范围）

以下现存行为在改造完成后**全部移除**，过渡期内其结论不作为权威：

| 现状行为 | 位置 | 退化后去向 |
|---|---|---|
| 齐全性裁决 `_evaluate_quality_gate`（required_artifacts 缺失 / visible_text 空 / 截图数不足 / page_state 异常 → fail） | `services/api/app/application/observation_packet.py:351` | 裁决逻辑迁入清单写入侧（E07），以需求集为输入（E14） |
| 三处「非 pass 即阻断为 evidence_insufficient」 | `services/api/app/application/rule_judge.py:505、527、676` | 改为「无 pass 清单 → 拒判」（E22），判定侧不再自己数证据 |
| 采集角色缺失预检 | `rule_judge.py:481-500` | 同上，并入需求集裁决 |
| Rule Engine 旁路硬编码 `quality_gate_status='pass'` 写入 packet | `services/api/app/infrastructure/rule_judge_evidence.py:328` | 删除。判定侧不得写任何门禁状态 |
| 判定侧直查 `qc_evidence_artifact` 取证 | `observation_packet_repository.py:92、182`；`rule_judge_evidence.py:128、244` 等（完整清单见 2.6） | 改为按清单读取（E23 移除直查） |

### 1.5 载荷预算与齐全性分离（本条是新增拆分，评审重点）

现状有两处把**预算超限伪装成证据不足**：

- `observation_packet.py:449-456`：压缩失败 → `quality_gate.missing_sources += "packet_input_budget"`、`status="fail"`
- `semantic_rule_judge.py:585`：prompt 预算不足 → 同样走 evidence_insufficient 出口

冻结口径：**预算超限不是证据不足**。二者必须用不同字段表达——

- 齐全性：清单 `quality_status`（1.2 的三态）。
- 预算/读取结果：判定侧独立表达（并入 E21 的 `ManifestReadDecision` 审计载荷，如 `payload_budget_exceeded`），只进审计，不影响清单结论，不复用 `quality_gate` 字样。
- 预算超限的判定产出为 `inconclusive` 类结果并附独立原因码，不得报「证据不足」。

### 1.6 规则级 quality_gate 语义冻结

- 现状取值域（62 条规则穷举）：`input_contract.quality_gate` 仅两个键——`min_screenshot_count ∈ {0,1}`、`allow_empty_visible_text ∈ {true,false}`。**没有 hard/soft、没有降级策略**；消费点全仓仅 `observation_packet.py:367-378` 一处。
- 冻结语义：规则级 `quality_gate` 与 `required_artifacts` 一样，是**需求声明**，不是裁决器。E11 的 `EvidenceExitRequirementResolver` 把它们收敛为需求集；清单写入侧按需求集裁决（E14）。判定侧不再直接读取规则 `quality_gate` 做阻断。
- 键集封闭：新增键（如未来的最小 DOM 元素数）须先修订本文档再落码。

### 1.7 同名词汇隔离（评审时最容易踩的坑）

仓内现有 **4 套**叫 `quality_status` / `quality_gate_status` 的词汇，本冻结只治理 A、D 两套：

| # | 词汇 | 取值 | 位置 | 处置 |
|---|---|---|---|---|
| A | 清单质量状态 | `pending/pass/fail` | `domain/evidence/contracts.py:34`；`qc_evidence_manifest.quality_status` | **本文档冻结的权威** |
| B | 页面探索覆盖度 | `unknown/in_progress/needs_review/complete` | `application/page_exploration_summary.py:97-105`；前端 `task-insight.ts:125` | 不同概念，不在本冻结范围。**遗留问题**：与 A 同名，建议后续改名 `exploration_status`（非 M0 强制） |
| C | 单页判定聚合 | `in_progress/needs_review/no_applicable_rules/fail/warning/skipped/pass`（另有 `not_started` 变体） | `agents/runtime/page_inspection_memory.py:425、444、4043` | 不同概念，不在本冻结范围。**已知风险**：与 B 同名同场景（页面级 dict），前端类型只声明了 B 的 4 个取值，C 会产出 6 个表外值 |
| D | packet 门禁状态 | `pending/pass/fail` | `qc_observation_packet.quality_gate_status`（`migrations/20260706_0001_core_schema.py:187`） | **降级为非权威**：过渡期只读兼容，判定侧不再写入（1.4）；终态由 E20–E25 迁移后仅作历史审计字段 |

隔离规则：四套词汇的取值**禁止互串**（契约测试 pin 住 A 的解析器拒绝 B/C 词汇）。

## 2. 现状差距（评审背景，均已核对 file:line）

1. **清单侧已开始接线，但仍未全量完成**：App Capture 路径已通过 `app_evidence_manifest.write_app_capture_manifest()` 写入 `qc_evidence_manifest`，并在清单写入侧完成截图最小证据的 `pass/fail` 裁决；Web/H5 采集落盘路径和基于需求集的完整裁决仍待 E07/E08/E09/E14 补齐。
2. **权威方向反了**：清单仓储写入时反查 `qc_observation_packet.quality_gate_status` 做一致性校验（`evidence_manifest_repository.py:231-255`）——清单借判定侧的权威，与目标口径相反。E07 落地后此校验方向需要重议（清单成为源头后不应再反查 packet）。
3. **真实裁决在判定侧**：见 1.4 表格，这是当前唯一在生产运行的齐全性裁决。
4. **门禁三实现并存**：Web = `NoopEvidenceExitGate`（`web_agent_page_capture.py:136-148`，注入于 `:563`，**空门禁属实**，且无测试锁住，对比 Android 有 `test_android_capture_runtime.py:68`）；App = `PostgresEvidenceExitGate`（`app_runtime_composition.py:172`）已接线，且 App Capture 已能写入最小 `pass/fail` Manifest，但完整需求集裁决和 H5/Web 接线未完成；Android demo = 进程内实现硬编码放行（`android_capture_runtime.py:74`）。
5. **生命周期缺口**：reserve/finalize/cancel/expire 的 DB 层完整（`evidence_exit_gate_repository.py:194、328、371`），但 `expire_stale()` 全仓无生产调用方，过期预留只能被动发现——E18 需补定时驱动。
6. **判定侧直查附件表清单**（E23 移除对象，高优先级部分）：`observation_packet_repository.py:92、182`、`rule_judge_evidence.py:128、244`、`ui_snapshot_reader.py:50`、`page_detection_tools.py:470`。展示类直查（`task_repository.py:427、1754`、`task_insight_repository.py` 多处）不在判定链路，是否纳入 E23 由评审定（起草建议：不纳入，另立后续项）。
7. **封板在途计数仅对已写清单的链路生效**：`task_finalization_repository.py:873` 统计 manifest pending 数；App Capture 最小清单写入后可以参与该闸门，Web/H5 仍需完成 Manifest 接线后才能纳入封板约束，需在 E10 回归中覆盖。

### 2.1 E15 落地状态（2026-08-13）

- Evidence Exit Gate 已按 Snapshot v2 冻结最新 Manifest identity/version/hash/quality status/reason 与 RequirementSet identity，历史 Snapshot v1 仍可读取。
- E15 只消费已持久化 Manifest 结论，不调用 E14、不重新统计附件、不修改 Manifest；`fail + RequirementSet` 映射为可审计的 `evidence_insufficient`，pending、控制面未就绪和血缘冲突分别使用独立业务结果，避免把技术故障伪装成证据不足。
- 门禁路径禁止写入 Evidence Request、发布 Supplemental/Probe Work 或触发补采 Capture；恢复补采必须作为独立需求重新评审。
- 本节仅记录代码落地状态，不代表文首三方评审已经完成，也不改变本文“待评审”状态。

## 3. 冻结条款 → 后续任务映射

| 条款 | 落地任务 |
|---|---|
| 1.1 唯一权威、裁决函数 | E07（Writer+裁决）、E08/E09（提交接线） |
| 1.2 三态与在途语义 | E10（幂等/版本测试）、封板闸门回归 |
| 1.3 投影 | E20/E21/E23 |
| 1.4 判定侧退出 | E22（拒判）、E23（移除直查）、E25（切主） |
| 1.5 预算分离 | E21（ManifestReadDecision）、E27（载荷安全测试） |
| 1.6 需求声明化 | E11–E14 |
| 1.7 词汇隔离 | 本次契约测试 pin 住；B/C 改名另立任务 |

## 4. 评审留痕

| 评审人 | 角色 | 结论（通过/有条件通过/驳回） | 意见 | 日期 |
|---|---|---|---|---|
| 天枢 | 采集接线（E08/E09/E17/E18/E26） | 通过 | | 2026-08-13 |
| 青云 | 落库与补采（E04–E06/E15/E16/E24） | 通过 | | 2026-08-13 |
| 立川 | 起草人 | 提交评审 | 2026-08-13 记录三方确认完成，本文转「已冻结 v1」 | 2026-08-10 |

> 已冻结 v1（2026-08-13 起生效）：任何口径变更须提版本并重走三方确认（对应 E03 验收）。

## 5. 冻结条款落地实况（2026-08-13）

| 条款 | 落地 | 位置 |
|---|---|---|
| 1.1 唯一权威 | 已落地：清单写入侧一次性裁决，判定侧只读 | `application/evidence_manifest_writer.py`（E07）+ `evidence_completeness.py`（E14） |
| 1.3 投影（只读、不改写） | 已落地：加载器只持「取清单」「读对象」两个读端口 | `application/manifest_backed_packet.py`（E19） |
| 1.5 预算与齐全性分离 | 词汇已就位：`payload_budget_exceeded` 独立分型，不占用 evidence_insufficient；**两处伪装点尚未拆除**（`observation_packet.py` 压缩失败、`semantic_rule_judge.py` prompt 预算），随 E22 落 | `manifest_backed_observation.py`（E20） |
| 2.1 独立业务结果 | 已落地：读取分型与门禁侧 `EvidenceExitBusinessOutcome` 同名同义五分型，唯清单裁决 fail 占用 evidence_insufficient | `manifest_read_guard.py`（E21） |
| 1.4 判定侧退出 | **进行中**：拒判通路与告警已就位，附件直查移除与齐全性重算拆除随 E22 | 见 1.4 表 |

> 任务编号对照：本文 1.4／3 节成文于 2026-08-10，引用的 E22/E23/E25/E27 为当时编号；
> 现行任务表（gid=1163792142）对应 E21（拒判与告警）、E22（移除直查并按清单投影）、
> E24（切主读取路径）、E26（载荷安全测试）。条款内容不变，仅编号平移。
