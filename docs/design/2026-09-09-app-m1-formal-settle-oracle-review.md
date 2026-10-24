# App M1：正式 Settle Oracle 开闸评审合同

> 日期：2026-09-09  
> 状态：**评审合同**；代码可写 `formal_settle_oracle` 与 `settle_enforcement_preview`，**本文件不批准**生产默认 `AUTHORITY_M1=true`，也不批准 Completion Gate SQL 消费 Settle。  
> 范围：仅 App（Android Official／ADB）；对齐 [`05-capture-readiness.md`](./2026-09-03-exploration-optimization/05-capture-readiness.md)、[`2026-09-07-adb-android-observation-ops-inventory.md`](./2026-09-07-adb-android-observation-ops-inventory.md) §2.7、[`2026-09-08-app-production-authority-rollout-review.md`](./2026-09-08-app-production-authority-rollout-review.md)。  
> 原则：影子／预览 ≠ 生产 Gate 权威；单独书面批准后方可改 env；禁止 silent cutover。

---

## 1. 目标与非目标

### 1.1 目标

经真机证据与书面评审后，显式打开 **`ALIOTH_APP_MILESTONE_AUTHORITY_M1=true`**，使：

- `formal_settle_oracle=true`（诊断／Settle 影子合同升级为「正式 Oracle 结论」语义）
- coverage 写入可审计 `settle_enforcement_preview`（`would_block_business_evidence`／`would_block_reasons`）
- 探索侧可对照预览与真机行为，为后续（另案）Gate 消费做准备

### 1.2 非目标（未批准即不做）

- **不**将 `gate_consumes_settle` 设为 `true`
- **不**改 Completion Gate SQL 读 Settle／稳定评估放行
- **不**把仓库默认 `authority.M1` 改为 `true`
- **不**因缺网伪造 `network_stable=true`，或把「无请求」当空闲
- **不**把 APP_READY／开屏／弹层帧计为业务首页到达

---

## 2. 开关

| 项 | 环境变量／字段 | 打开后行为 | 本轮是否批准默认打开 |
|----|----------------|------------|----------------------|
| M1 权威 | `ALIOTH_APP_MILESTONE_AUTHORITY_M1=true` | `formal_settle_oracle=true`；写出 enforcement preview | **否**（须书面批准） |
| Gate 消费 Settle | （代码字段 `gate_consumes_settle`） | Completion／Gate 读 Settle 放行 | **否**（另开评审） |
| Gate SQL | `ALIOTH_APP_GATE_SQL_CONSUMES_GRAPH` 等 | 与本层无关 | 不在本层范围 |

回滚：将该 env 置回未设／`false` 并 **重启 API／Worker**；确认诊断回到 `formal_settle_oracle=false`。

与 Lab 组合根：`ALIOTH_APP_LAB_MILESTONE≠off` 时 **不得**同开任一生产 `AUTHORITY_M*`（既有 fail-closed）。

---

## 3. 跨层准入（开闸前勾选）

- [ ] 真机批次已跑过含开屏／弹层／稳定业务页／缺网场景，coverage 可见 `app_page_stability_assessment` 与 `app_m1_settle_audit_bundle`
- [ ] 抽检：`network_stable` 恒为 false（无 HTTPS 生命周期证明时）；`capability_gaps` 含 `network_lifecycle_unavailable`
- [ ] 抽检：身份三明治漂移帧 `ready_for_business_evidence=false`
- [ ] 抽检：连续帧摘要变化时 `stability_status=transitional`，不得升业务稳定证据
- [ ] 抽检：`app_ready_equals_business_home=false`；开屏／弹层不得 `counts_as_business_home_arrival`
- [ ] `settle_enforcement_preview.gate_consumes_settle=false`；Completion Gate SQL 未改读
- [ ] 书面批准人／日期／回滚窗口（建议 ≥1 个工作日可瞬时关闸）
- [ ] **批准前禁止**合入「默认 AUTHORITY_M1=true」代码

---

## 4. 必证项与失败判据

| 必须证明 | 建议证据 |
|----------|----------|
| `no_forged_network_stable` | 缺网任务 metadata 中 `network_stable=false` + 缺口码 |
| `app_ready_not_business_home` | 入口层合同字段恒 false |
| `identity_drift_blocks_ready` | 前后包／Activity 漂移 → ready false |
| `frame_change_is_transitional` | 连续 `image_signature` 变化 → transitional |
| `overlay_startup_not_stable_business` | startup_ad／popup 不得 ready_for_business_evidence |
| `preview_not_gate_sql` | preview 存在且 `gate_consumes_settle=false`；Gate SQL 未消费 |

**失败判据（任一即拒开或回滚）**：伪造网络稳定；Gate SQL／`gate_consumes_settle` 被顺带打开；默认 authority 被改 true。

---

## 5. 评审单模板

```text
层：M1 Settle Oracle
申请人：
真机批次／Run ID：
必证项勾选：
残留风险：
批准人／日期：
回滚负责人／窗口：
实际 env 变更：ALIOTH_APP_MILESTONE_AUTHORITY_M1=
重启确认时间：
明确未批准：gate_consumes_settle / Gate SQL
```

---

## 6. 与「现在就能做」的边界

- **现在**：用本文评审与真机取证；代码可写 formal／preview；env 默认关。
- **批准之后**：仅运维改 `AUTHORITY_M1` + 重启；不得顺带改 Gate SQL。
- **Gate 消费 Settle**：须另开评审单，对标生产权威文 §Gate SQL 独立批准节奏。

---

## 7. 关联

- 实现：`app_capture_readiness_diagnosis.build_m1_settle_enforcement_preview`
- 里程碑：`app_bounded_exploration_milestones`（`AUTHORITY_M1`）
- 专项规则：`docs/专项能力与高频回归规则.md`（2026-09-09 M1 正式评审）
