# App 有限探索：生产权威与补采闭环开闸评审计划

> 日期：2026-09-08  
> 状态：**评审合同**；Lab 刀 A／刀 B 已分账落地（见 §2.1），本文仍不打开任何生产 `AUTHORITY_*`。  
> 范围：仅 App（Android Official／ADB）；对齐 [里程碑现况](./2026-09-08-app-bounded-exploration-milestones.md)、[流程对照 HTML](./2026-09-03-exploration-flow-comparison.html)、[有限探索方案 HTML](./2026-09-02-bounded-exploration-optimization-solution.html)、代码合同 `build_production_authority_rollout_checklist()`。  
> 原则：Lab 灰度 ≠ 生产权威；每层 **单独书面批准** 后方可改 env；禁止 silent cutover。

---

## 1. 目标与非目标

### 1.1 目标

按 **M4 → M5 → M6 → M7** 顺序，把「有限探索 HTML」中仍虚线框的生产能力，经真机证据与评审后，显式升为生产权威；其中 M6 补采从「计划影子／Lab 可声明」走向「生产可执行声明」，M7 与 Gate SQL 读图解耦分两步。

### 1.2 非目标（本计划不批准即不做）

- 不默认 `runtime_ready=true`（生产工厂仍硬写 `False`，直至 **M6 评审通过且另开 env**）。
- 不因 M7 authority 自动改 Completion Gate SQL（须独立 `PRODUCTION_GATE_SQL_CONSUMES_GRAPH` 类开关）。
- 不自动派发 Probe（即使 M6 `can_execute=true`，仍须另议 `executes_probe` 策略；首开闸默认保持 `false`）。
- 不打开本路线跳过的 **M3** 产品／规则范围专项。
- 不与 `ALIOTH_APP_LAB_MILESTONE≠off` 同开任一生产 `AUTHORITY_M*`（Worker fail-closed）。
- 不删除退役目录候选、不放宽支付／登录等价门禁。

---

## 2. 开关与顺序（代码已强制）

| 层 | 环境变量 | 打开后生产行为（摘要） | 前置 |
|----|----------|------------------------|------|
| M4 | `ALIOTH_APP_MILESTONE_AUTHORITY_M4=true` | Broker 低风险 `tap`／`swipe`／`back`（非仅 observe） | M0 契约；Lab M4 对账通过 |
| M5 | `…_M5=true` | `schedules_from_graph` 参与优先级 | **必须已开 M4** |
| M6 | `…_M6=true` | 补采计划可声明 `runtime_ready`／`can_execute`（≠自动 Probe） | **必须已开 M5** |
| M7 | `…_M7=true` | Gate **裁决消费图**（顾问／正式 block 口径以当批实现为准） | **必须已开 M6** |
| Gate SQL | `ALIOTH_APP_GATE_SQL_CONSUMES_GRAPH=true` | Completion Gate SQL 读图 | **必须已开 M7**；本开关单独再评 |

顺序违规：`apply_production_authority_fail_closed` 关闭后级，不抬高前级。

回滚：将该层 env 置回未设／`false` 并 **重启 API／Worker**；后级若仍开会被 fail-closed 或须一并关闭。

### 2.1 Lab 刀 A／刀 B（已落地，非生产 AUTHORITY）

| 刀 | 环境变量 | 效果 |
|----|----------|------|
| A | `ALIOTH_APP_LAB_MILESTONE=M6\|M7` → `lab_plan_ready` | 补采计划可声明可执行；`executes_probe` 仍 false |
| B | 另加 `ALIOTH_APP_LAB_EXECUTES_PROBE=true` → `lab_executes_probe` | 才允许关闭前排空／Probe（`lab_supplemental_ready` 仅为别名） |

M6/M7 **默认不开刀 B**。R0 基线禁止刀 B。

---

## 3. 跨层准入（每一层评审前都要勾）

- [ ] Lab 组合根已对该层做过真机对账（`ALIOTH_APP_LAB_MILESTONE` 升到对应层），且 **生产 AUTHORITY 全程为关**。
- [ ] 设备路径为 **ONIRENT** 固定／WS ADB；无 Agent 直连 ADB／Driver／bash。
- [ ] 审计可抽检：Receipt 覆盖、包 allowlist、跨 epoch 坐标重放为 0。
- [ ] 指标键已落盘（见里程碑 `APP_BOUNDED_EXPLORATION_METRIC_KEYS`）：至少覆盖同预算可信覆盖、无效动作比、外跳回源、越权拒绝。
- [ ] 书面批准人／日期／回滚窗口（建议 ≥1 个工作日可瞬时关闸）。
- [ ] 变更仅 env＋重启；**本层评审通过前禁止合入「默认 authority=true」代码**。

---

## 4. 分层验收与回滚

### 4.1 M4 · Broker 生产动作权威

**批准打开**：`ALIOTH_APP_MILESTONE_AUTHORITY_M4=true`

| 必须证明（对齐 checklist） | 建议证据 |
|----------------------------|----------|
| `receipt_coverage_100pct` | 抽样 N 次坐标动作均有 Receipt，且 `arrival_not_implied` |
| `zero_agent_direct_adb` | 审计无 Explorer／Agent 直调 ADB |
| `no_cross_epoch_coordinate_replay` | 滚后／新 Capture 后无旧 `candidate_id` 硬点成功 |
| `dual_executor_forbidden` | 同 Session 无双写 Owner |

**失败判据（任一即拒开或回滚）**：双执行器、跨 epoch 误点进入业务、无 Receipt 仍计到达。

**回滚**：关 M4（及已误开的后级）→ 重启 → 确认 Broker 回到 observe。

---

### 4.2 M5 · 图调度生产权威

**批准打开**：`…_M5=true`（依赖 M4）

| 必须证明 | 建议证据 |
|----------|----------|
| `overlay_external_not_in_business_denominator` | 弹层／外跳不计业务覆盖分母 |
| `external_bypass_no_business_coverage` | browser／store 旁路仅观察＋回源 |

**另验**：图调度不得饿死 `coverage_page_seek`／底栏／关层；Coverage 权威仍未「静默迁图」到 Gate。

**回滚**：关 M5（保留或关 M4 按事故面）→ 重启 → 调度回非图优先。

---

### 4.3 M6 · 补采闭环生产权威（核心）

**批准打开**：`…_M6=true`（依赖 M5）

本层对应 HTML「正式补采尚未开放」的 **第一刀**：生产侧允许补采计划在门禁通过时声明可执行，**仍默认不自动派 Probe**。

| 必须证明 | 建议证据 |
|----------|----------|
| `fresh_cannot_fill_exact` | exact 缺口不得被 fresh 新观察抹平 |
| `probe_not_auto_without_policy` | 无显式策略时 `executes_probe=false`，审计无静默 Probe |
| `capture_series_lineage` | 长页／补帧有父帧血缘；禁坐标宏重放当恢复 |

**补采闭环验收剧本（真机，至少 3 类）**：

1. **就地补采**：同页缺区域／前后态 → 新 Capture → 等价／角色齐 → Rejudge；旧失败不覆盖。  
2. **图路径恢复**：离源后按已验证边回源 → 每边新观察＋重绑 → 到达证明；Hangback **禁止目标不变量自证**。  
3. **高风险拒放**：支付／登录类拒 `equivalent`／`fresh` 滥放；不可恢复 → `evidence_insufficient` 或人工，缺口终态完整。

**明确仍不在本层批准**：

- 生产工厂默认 `SupplementalEvidencePolicy(runtime_ready=True)` 代码改写  
- `executes_probe=true` 全自动挂回  
- 用 after Capture 冒充精准补采成功  

**回滚**：关 M6 → 重启 → 确认生产补采计划 `runtime_ready=false`／`can_execute=false`。

**第二刀（可选，另开评审单）**：在 M6 稳定运行 ≥ 约定窗口后，再评「有界自动 Probe」策略（预算、规则类白名单、熔断）。

---

### 4.4 M7 · Gate 裁决权威（不含 SQL 切读）

**批准打开**：`…_M7=true`（依赖 M6）

| 必须证明 | 建议证据 |
|----------|----------|
| `mutates_completion_gate_sql_false_until_sql_env` | M7 开后 SQL 仍只读旧 `coverage_summary`，直至独立 SQL env |
| `writes_formal_block_reasons_false_until_sql_cutover` | 正式 block_reasons 写入策略与 SQL 切读同批或其后 |

**另验**：工作台图面板与 Lab `lab_gate_*` 预览与生产裁决口径可对账；覆盖／规则／缺口／技术异常分维度报告不混写「通过」。

**回滚**：关 M7 → 重启 → Gate 行为回到影子／旧 coverage。

---

### 4.5 Gate SQL 读图（独立评审，最后）

**批准打开**：`ALIOTH_APP_GATE_SQL_CONSUMES_GRAPH=true`，且 **M7 已开**。

| 必须证明 | 建议证据 |
|----------|----------|
| 影子预览与强制结果一致率达阈值 | 同任务 `would_block_if_enforced` vs 强制后 |
| 误拦／漏拦可解释 | 抽检 block_reasons；无「图节点臆测缩分母」 |
| 回滚演练通过 | 关 SQL env 后 Gate 立即回到旧口径 |

**禁止**：未开 M7 时打开 SQL env（代码会 fail-closed／忽略）。

---

## 5. 评审单模板（每层复制一张）

```text
层：M4 | M5 | M6 | M7 | Gate-SQL
申请人：
真机批次／Run ID：
Lab 对账结论（链接／附件）：
必证项勾选：
残留风险：
批准人／日期：
回滚负责人／窗口：
实际 env 变更：
重启确认时间：
```

---

## 6. 建议推进节奏

| 阶段 | 动作 | 产出 |
|------|------|------|
| R0 | 保持全部 AUTHORITY 关；Lab=`M7` 持续对账 | 指标基线与失败分类 |
| R1 | 仅评 **M4** | 批准单或拒开原因 |
| R2 | M4 稳定后评 **M5** | 同上 |
| R3 | 评 **M6**（补采声明可执行，Probe 仍关） | 闭环三剧本报告 |
| R4 | 评 **M7**（不含 SQL） | Gate 裁决对账 |
| R5 | 单独评 **Gate SQL** | 切读／回滚演练记录 |
| R6（可选） | 有界 `executes_probe` | 另开策略评审 |

任一阶段失败：停在该层，**不跳级**开后级。

---

## 7. 与「现在就能做」的边界

- **现在**：用本文做评审与真机取证；可改文档与 Lab env。  
- **批准某层之后**：仅运维／发布改对应 env＋重启；若需代码配合（例如 Probe 策略），另开实现单且不得顺带默认开闸。  
- **未批准**：禁止 PR 将生产默认 `authority`／`runtime_ready` 改为 true。

---

## 8. 关联

- 里程碑现况：`2026-09-08-app-bounded-exploration-milestones.md`  
- 代码清单：`app_bounded_exploration_milestones.build_production_authority_rollout_checklist`  
- 补采设计：`2026-09-03-exploration-optimization/03-targeted-recapture.md`  
- 专项回归：`docs/专项能力与高频回归规则.md`（Lab 组合根／M6 runtime_ready／禁止 silent 切 Gate）
