# E01 冻结口径：证据角色取值表

| | |
|---|---|
| 对应任务 | 任务表 E01（M0，P0） |
| 起草 | 立川（2026-08-10） |
| 评审 | 天枢、青云（见文末留痕表） |
| 状态 | **已冻结 v1**（2026-08-13 三方确认完成，见文末留痕表） |
| 关联文档 | [E02 质量状态唯一权威](2026-08-10-m0-e02-quality-status-authority.md) |
| 契约测试 | `services/api/tests/test_m0_evidence_freeze_contracts.py` |

## 0. 范围与一句话结论

本表冻结的是**证据采集角色（capture role）**一套词汇：一次采集/一份附件在动作时间线里扮演什么角色（动作前基线、动作后结果、探针重放……）。

**冻结基线 = 现状穷尽盘点**：10 个供给角色 + 7 名 3 文法的需求角色 + 2 个持久层占位值。冻结含义是**取值域封闭**——新增取值必须先修订本表再落码；表内标注的「废弃/预留」处置在评审确认后由后续任务执行，不在 M0 动代码。

以下同名概念与本表**无关**，盘点时已逐一排除：ARIA/DOM 元素角色（`page_detection_tools.py` 等的 `role="dialog"` 选择器）、LLM 消息角色（`{"role":"system"}`）、用户权限角色（`qc_user.role`）、附件 payload 内部键（`page_capture.py:799` 的 `capture_role:"page_coverage"`、`:1022` 的 `scroll_terminal`——它们在附件内容里，不是采集角色）。

## 1. role 的三层落点与继承链

| 层 | 字段 | 位置 | 现状 |
|---|---|---|---|
| 运行时采集层（**词汇源头**） | `CaptureDirective.role` / `CaptureRef.role` | `domain/interaction/captures.py:41、137、236` | 仅校验非空，无值域约束 |
| 持久层 | `qc_capture.capture_role` 列；`qc_page_capture.capture_role` 列；`metadata->>'capture_role'` | 迁移 `20260804_0034:29`；`20260803_0029:306`；写入 `page_capture_repository.py:64` | **三处口径不一致**（缺陷 D3/D4） |
| 域契约层 | `ArtifactReference.role` | `domain/evidence/contracts.py:48` | 生产代码从不构造（缺陷 D2），唯一读取方 `evidence_exit_gate_repository.py:575` |

继承链（冻结原则）：`CaptureDirective.role` → 采集落库 → 附件继承所属 capture 的角色（现状实现：`rule_judge_evidence.py:195` `capture_role=role_by_capture[capture_id]`）→ E07 起进入清单 `ArtifactReference.role`。**全链路一套词汇**，按「供给角色」（采集端产什么）和「需求角色」（规则端要什么）两个子表管理，需求→供给的归一由 E11 Resolver 负责（条款 3.3）。

## 2. 取值表（冻结基线，核心交付物）

### 2.1 供给角色（运行时产生，10 个）

| 取值 | 语义 | 主要产生方 | 附着 artifact_type | 主要消费方 | 处置 |
|---|---|---|---|---|---|
| `primary` | 规则判定主采集帧（"当前页面"） | 默认值：`page_capture_persistence.py:92`、`page_capture_tools.py:217、655`、`web_agent_page_capture.py:638` | screenshot+action_trace（按需 +dom/visible_text/ui_snapshot） | `rule_judge.py:1505、2670`、各 evaluator 默认值、`capture_plan.py:62` | **保留** |
| `before_action` | 动作前基线帧 | Web+App 通用 profile `capture_profiles.py:42、59`；Web Agent 双采 `web_agent_page_capture.py:642`；Android `android_capture_runtime.py:358、631` | Web 四件套 / App screenshot+action_trace | `capture_coordinator.py:323、499`；App 白名单 `app_interaction_runtime.py:251`；幂等索引 `20260806_0038:34` | **保留** |
| `after_action` | 动作后结果帧 | `capture_profiles.py:46、63`；`android_capture_runtime.py:632` | 同上（Web 按动作追加 temporal_trace/har） | 同上；`app_screenshot_exploration.py:431` | **保留** |
| `bootstrap` | App 装机/启动首帧（绑定 `qc_app_bootstrap`） | `app_workflow.py:853-854`；SQL `app_capture_repository.py:197` | screenshot | `app_capture_persistence.py:99`；`app_capture_repository.py:76`；`surface_session_repository.py:306` | **保留** |
| `probe_before` | 补采探针动作前帧 | `capture_profiles.py:92`；`maf_agent.py:422` | screenshot+action_trace | 仅 `capture_coordinator.py:499` 一致性校验，**无业务读取方** | **保留**（与 probe_result_replay 成对；见 D5） |
| `probe_result_replay` | 探针重放结果帧（证明 Evidence Request 已满足） | `capture_profiles.py:97`；`maf_agent.py:427` | App: screenshot+ui_tree+action_trace / Web: 主采集集+action_trace | `evidence_supplement_repository.py:118-119`（读 `metadata->>'capture_role'`） | **保留** |
| `shadow_candidate` | 影子管线双采对照帧 | `page_capture_tools.py:537`（唯一） | 与主采集同 spec | **只写不读**（影子比较按 artifact_id 关联） | **保留**，标注只写不读 |
| `refreshed` | 同 URL 重载后帧 | `page_inspection_memory.py:2026` | screenshot+action_trace | `rule_transition_evaluators.py:154、175、233`；seed 规则 ×2 | **保留** |
| `exploration_terminal` | 探索收口最后一页（不再执行新探索动作） | `AppExplorationPersistenceGate` 截止前复制最后一帧 | screenshot+ui_tree（继承末帧） | Evidence Index 谱系；#gate 完成条件 | **保留**（2026-08-11 按页面探索 v2 #gate 增补） |
| `evidence_insufficient` | 探索收口证据缺口帧（不触发补采） | 同上，不足路径 | 同上 | Evidence Index 谱系；#gate 不足条件 | **保留**（2026-08-11 按页面探索 v2 #gate 增补） |

> **2026-08-11 实况更新**：`98e3352` 起 App 探针 Profile（`capture_profiles.py for_app_probe`）改发 `before_action`/`after_action`（缺陷 D5 按「对齐持久化白名单」方式处置），`44cc316` 同日统一禁用页面探索补采。`probe_before`/`probe_result_replay` 自此暂无生产方，取值保留为**预留**（词汇不变，不算取值变更）；补采链路恢复时再启用。契约测试已同步。
>
> **2026-08-11 #gate 增补**：按《页面探索 v2》§5.2／#gate 增加收口 Role `exploration_terminal`、`evidence_insufficient`。只确认结束页或缺口，禁止派生 Probe／Inspection／新 Exploration Work。`qc_capture` CHECK 同步扩列。

### 2.2 需求角色（规则契约 `required_capture_roles` 声明，7 名 + 3 文法）

| 取值 | 语义 | seed 使用量 | 消费方 | 处置 |
|---|---|---|---|---|
| `primary` | 主采集帧（同 2.1） | ×22 | 同 2.1 | **保留** |
| `before` | 操作前页面（**注意：不是 `before_action`**，见 D1） | ×7 | `rule_transition_evaluators.py:152、160`；`task_progress.py:444` | **保留为需求简名**，E11 归一（条款 3.3） |
| `after` | 操作后页面 | 0（evaluator 默认值） | `rule_transition_evaluators.py:153、155`；`task_progress.py:445` | 同上 |
| `source` | 来源页面 | ×1 | `rule_policy_evaluators.py:308、357`；`task_progress.py:448` | **保留** |
| `refreshed` | 刷新后页面 | ×2 | 同 2.1 | **保留** |
| `landing` | 点击落地页 | 0 | 仅白名单 `page_inspection_memory.py:1908`，**无标签、无 evaluator、无 seed** | **建议废弃**（决策点 P5） |
| `fault` | 模拟断网后页面 | 0 | 仅标签 `task_progress.py:447`，**无产生方** | **预留**（网络故障类规则启用时激活） |
| `attempt_N`（文法 `^attempt_[1-9]\d*$`） | 防刷第 N 次尝试 | attempt_1/2/3 各 ×1 | `rule_policy_evaluators.py:205、246-260`；`task_progress.py:453` | **保留** |
| `location_N`（文法 `^location_[1-9]\d*$`） | 价格展示位置 N | location_1 ×1 | `rule_policy_evaluators.py:45、73`；`task_progress.py:454` | **保留** |
| `entry_N`（文法 `^entry_[1-9]\d*$`） | 内容消费入口 N | 0 | `rule_policy_evaluators.py:316、362`，**无产生方** | **预留** |

### 2.3 持久层占位值（2 个，均非业务角色）

| 取值 | 现状 | 处置 |
|---|---|---|
| `observation` | `qc_capture` 触发器对所有 Web 采集硬编码写入（`20260804_0034:164`，回填 `:83`），**无任何读取方**。后果：Web 真实角色永远进不了 `qc_capture.capture_role` 列，只在 metadata 里 | 决策点 **P1**：保留占位，或改触发器写真实角色（则 CHECK 白名单须扩到供给全集） |
| `supplemental` | 仅存在于 CHECK 白名单（`20260804_0034:45`），**零写入、零读取** | **建议废弃**（决策点 P2；补采语义已由 probe_* 承担） |

## 3. 冻结条款（规范性）

- **3.1 词汇封闭**：证据角色取值域 = 2.1 供给 10 值 ∪ 2.2 需求 7 名 + 3 文法 ∪ 2.3 占位 2 值。新增取值（含新文法）必须先修订本表、重走三方确认，再落码。E07 清单写入服务起，`ArtifactReference.role` 强制校验 ∈ 本表。
- **3.2 命名文法**：统一为 `^[a-z][a-z0-9_]{0,31}$`（小写字母开头，≤32 字符）。现状三套校验不一致（`page_capture_tools.py:152` ≤64、`rule_judge_evidence.py:19` 与 `rule_transition_evaluators.py:19` ≤32、列宽 varchar(32)/varchar(128)），按 32 收敛（决策点 P3）。
- **3.3 供给/需求分治**：规则里的需求角色是**声明**，采集端的供给角色是**事实**。二者的等价映射（`before`⇄`before_action`、`after`⇄`after_action`，以及「需求可由复用既有采集满足」的规则）由 E11 需求集 Resolver 统一实现，其他组件不得各自做映射（现状没有映射，导致缺陷 D1）。映射方向见决策点 P4。
- **3.4 附件角色继承**：附件不自带独立角色，恒等于所属 capture 的供给角色；清单（E07）逐附件记录该角色，门禁（E14）用它对照需求集。

## 4. 已知缺陷（随表移交评审，不在 M0 修）

| # | 缺陷 | 后果 | 建议去向 |
|---|---|---|---|
| D1 | 写端 `before_action`/`after_action` vs 规则端 `before`/`after` 拼写分裂 | `state_transition_assertion` 永远拿不到 Runtime 双采帧；`evidence_exit_gate_repository.py:579` 需求差集必然缺失 | E11 归一映射（立川） |
| D2 | `ArtifactReference.role` 全库无生产写入方（唯一读取 `evidence_exit_gate_repository.py:575`） | 门禁 role 校验在生产上恒拿空集 | E07 落地即消（立川） |
| D3 | `qc_capture.capture_role` 列与 `metadata->>'capture_role'` 双轨：Web 列恒 `observation`/metadata 有真值，App 列有真值/metadata 无键 | `evidence_supplement_repository.py:118` 只能命中 Web；`task_insight_repository.py:230` 只对 App 有意义 | 与 D4 同治，归 M1 落库改造评审（青云） |
| D4 | `qc_capture` CHECK 白名单仅 5 值（`20260804_0034:42-47`），与实际供给词汇 8 值冲突 | 直接写 `qc_capture.capture_role` 的新代码会撞约束 | 同上（青云） |
| D5 | App 探针角色被 `AppCaptureWriter` 白名单拒绝（`app_interaction_runtime.py:251` 只认 before/after_action；`app_capture_persistence.py:80` 同） | **App 补采探针必然 ValueError**，影响 E15/E16 补采闭环 | E08 前须修（天枢/青云） |
| D6 | role 长度校验三套（64/32/32；varchar 32/128） | 33-64 字符角色可通过 Tool 校验并落库，但判定阶段被拒 | 条款 3.2 收敛（立川） |

**artifact_type 侧顺带发现**（盘点副产物，与本表相邻、影响 M1）：

| # | 发现 | 后果 | 建议去向 |
|---|---|---|---|
| C1 | App 的 `ui_tree` 不在 `qc_evidence_artifact` CHECK 白名单（`20260721_0015:29-35` 仅 13 值） | App probe/action 带 ui_tree 落库**必然违反约束** | 青云 E04-E06 一并处理 |
| C2 | Web 应用层白名单（`page_capture_persistence.py:8-20`）缺 `ocr_text/media_state/ai_io/report`，而 seed `optional_artifacts` 里 `ocr_text`×48 | 规则声明的可选证据在 Web 上永远采不出来 | 评审定：修剪规则词汇 or 扩白名单 |
| C3 | 别名归一不全：`capture_plan.py:55` 只归一 `dom_snapshot`，`network_log`×16 不归一；门禁差集（`evidence_exit_gate_repository.py:577`）完全不做归一 | 需求对账时同物异名误判缺失 | E11 统一归一（立川） |

## 5. 附录：artifact_type 词汇基线（顺带冻结）

- **取值域（18）**：DB CHECK 13 值 `screenshot, dom, visible_text, ocr_text, har, console_log, media_state, ai_io, report, action_trace, ui_snapshot, temporal_trace, page_coverage`（`20260721_0015:29-35`）+ App 运行时制品 `ui_tree, view_hierarchy, device_log, network_trace, fault_artifact`。
- **别名表（读入时归一）**：`dom_snapshot→dom`、`network_log→har`、`network_events→har`（现状实现 `observation_packet.py:46-50`）。
- **seed 使用统计**：required_artifacts 6 值（ui_snapshot×46、screenshot×44、dom×39、visible_text×29、action_trace×6、har×1）；optional_artifacts 11 值（含别名 dom_snapshot×19、network_log×16，需按别名表归一）。

## 6. 评审决策点（评审会议程）

| # | 决策点 | 起草建议 |
|---|---|---|
| P1 | `observation` 占位：保留，还是改触发器写真实角色并扩 CHECK | 改触发器（列与 metadata 双轨是 D3 根因），随 M1 一并做 |
| P2 | `supplemental` 废弃 | 废弃，从 CHECK 移除 |
| P3 | 长度上限统一 32 | 同意，收敛 `page_capture_tools.py:152` |
| P4 | 需求→供给映射方向：保留 `before`/`after` 简名由 E11 映射，还是规则词汇全改 `before_action`/`after_action` | 保留简名 + E11 映射（改 seed 需重发 34 规则包，代价大） |
| P5 | `landing` 废弃、`fault`/`entry_N` 标预留 | 同意 |

## 7. 评审留痕

| 评审人 | 角色 | 结论（通过/有条件通过/驳回） | 意见 | 日期 |
|---|---|---|---|---|
| 天枢 | 采集接线（E08/E09/E17/E18/E26） | 通过 | | 2026-08-13 |
| 青云 | 落库与补采（E04-E06/E15/E16/E24） | 通过 | | 2026-08-13 |
| 立川 | 起草人 | 提交评审 | 2026-08-13 记录三方确认完成，本表转「已冻结 v1」 | 2026-08-10 |

> 已冻结 v1（2026-08-13 起生效）：任何取值变更须提版本并重走三方确认（对应 E03 验收）。

## 8. 冻结后待处理事项

| # | 事项 | 依据 | 状态 |
|---|---|---|---|
| V1 | Capture Profile 声明了 `fault_artifact`／`network_trace`／`view_hierarchy`／`device_log` 四个 App 运行时 artifact_type | 已纳入附录 5 取值域；生产词汇表和契约测试同步封闭 | **已裁决**：作为 App 运行时有界制品纳入 E01 词汇基线；不等价于 Web DOM/OCR |

> 条款 3.1 生效实况：`ArtifactReference.role` 的强制校验已于 2026-08-13 在 E07 清单写入侧落地
> （`domain/evidence/vocabulary.py` `normalize_manifest_capture_role`，校验集合 = 供给 10 值 ∪ 持久层占位 2 值）。
