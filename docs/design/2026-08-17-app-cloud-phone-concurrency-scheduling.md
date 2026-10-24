# App 云手机并发调度设计

> 版本：v1.0  
> 日期：2026-08-17  
> 状态：**设计冻结（排期第 54 行）**  
> 负责人：星语  
> 范围：正式 App 质检主链（`AppQcWorkflow` → Official / MoreLogin 云手机）  
> 关联：
> - 迁移 `20260814_0058_app_device_resource_lease`
> - `services/api/app/application/device_pool.py`
> - `services/api/app/infrastructure/db/device_lease_repository.py`
> - `docs/专项能力与高频回归规则.md`（2026-08-14 设备租约分层）
> - 排期后续：第 55～58 行（多机选路／忙时等待／双线回归）

---

## 1. 问题与目标

### 1.1 现状（已落地）

| 能力 | 现状 |
| --- | --- |
| Session 内串行 | `PostgresSessionCommandGate` + Bootstrap lease |
| 跨 Worker 设备互斥 | PostgreSQL `qc_device_resource` + `qc_device_lease` + fencing |
| Bootstrap 前 claim | `AppQcWorkflow` 在 `ACQUIRING_SESSION` 前 `claim_device_lease_with_wait`；池满等待或超时，不得安装 |
| 生产后端开关 | `ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres` + `ALIOTH_DEVICE_POOL_SLOTS` 种子 |
| 过期回收 | Worker 周期 `recover_expired`；终态 Session 释放；非终态超时 quarantine |

### 1.2 仍缺（本设计要冻结边界，后续行落地）

| 缺口 | 表现 | 排期 |
| --- | --- | --- |
| 多机选路 | MoreLogin／ADB 仍读单一 `ALIOTH_MORELOGIN_CLOUDPHONE_ID`；租约 `device_ref` 未驱动实连 | 第 56 行 |
| 池满等待 | **第 57 行已落地仓库闭环**：`claim_device_lease_with_wait`；超时 `DEVICE_WAIT_TIMEOUT`；concurrency 超槽只告警 | 第 57 行 |
| 双线验收 | **第 58 行已落地仓库闭环**：`test_app_cloud_phone_dual_line.py` 覆盖 §8.2；真实双机冒烟仍须另跑 | 第 58 行 |

### 1.3 目标能力（MVP：先验 2 线）

1. N 台云手机登记为库存；任意时刻一台机最多一个活跃租约。  
2. 任务 claim 成功后，**实连只使用该租约绑定的 envId／ADB**。  
3. 池满时任务**有界等待**，不得盲目 powerOn／install。  
4. 双 Worker × 双任务并行：各自截图与时间线可区分；单线故障不拖死另一线。

非目标（本轮不做）：矩阵真并行、运维大盘、跨机房、抢占高优任务。

---

## 2. 职责分层（禁止互相替代）

```text
App Run / 队列领取
    │
    ├─ Bootstrap lease     ：谁跑这个 Session 的安装／开跑
    ├─ Device lease        ：哪台物理／云手机被占用（本设计核心）
    └─ Session Command Gate：同一 Session 内动作串行
```

- Redis 只做唤醒，**不得**作为设备库存货源。  
- `InProcessDevicePool` 仅 Fake／本地无 PG 池；生产必须 postgres。  
- 全局 env `ALIOTH_MORELOGIN_CLOUDPHONE_ID` 仅作**单机回退**；多机路径禁止再依赖它作为唯一选机源。

---

## 3. 资源与租约模型

### 3.1 库存行 `qc_device_resource`

| 字段 | 语义 |
| --- | --- |
| `device_ref` | 稳定键，形如 `cloud_phone:<profile>` |
| `backend` | `cloud_phone`／`ldplayer`／… |
| `profile_id` | 逻辑档位名 |
| `capabilities` | JSONB，云手机见下节 |
| `health_state` | `available`／`claimed`／`recovering`／`quarantined`／`disabled` |
| `fencing_epoch` | 每次成功 claim 递增；写入动作须比对 |

**云手机 `capabilities` 约定（冻结）：**

```json
{
  "vendor": "morelogin",
  "env_id": "1716966721435348",
  "adb_endpoint": null,
  "adb_auth_secret_ref": "env:ALIOTH_CLOUD_PHONE_ADB_AUTH_SLOT_1",
  "notes": "optional"
}
```

- `env_id`：MoreLogin 云手机 ID（必填，多机路径）。  
- `adb_endpoint`：可选缓存；**每次开跑以 MoreLogin API 刷新为准**，不得写死过期 IP:port。  
- 密钥不进表明文；用 secret ref 或按 slot 读环境变量。

种子来源：运维配置 `ALIOTH_DEVICE_POOL_SLOTS` + 扩展映射（见 §6），写入／upsert `qc_device_resource`。

### 3.2 租约行 `qc_device_lease`

沿用 0058：`lease_id + lease_owner + lease_token + fencing_token + state`。  
活跃态：`claimed`／`active`／`releasing`／`recovering` 对 `device_ref` 唯一。

状态机（与 08-14 规则一致）：

```text
available --claim--> claimed --renew--> active
active|claimed --正常结束--> releasing --> released --> available
任意占用 --超时--> recovering --健康检查通过--> available
任意占用 --关闭不确定／失联--> quarantined（人工或恢复任务解除）
```

### 3.3 与 Surface Session 绑定

Session 保存 `device_lease_id` + `device_lease_fencing_token`。  
Capture／动作写回须校验 fencing；旧 Worker 零行失败关闭。

---

## 4. 选机路由（第 56 行实现依据）

### 4.1 Claim 策略（已有 + 增补）

现有 SQL 行为保留：

1. 同 `task_id + session_id` 已有活跃租约 → 幂等返回（同 owner）或拒绝。  
2. `preferred_device_ref` 若空闲 → 精确命中。  
3. 否则：`enabled + health_state=available` + 无活跃租约，`FOR UPDATE SKIP LOCKED` 取一台。  
4. 无空闲 → `DEVICE_POOL_EXHAUSTED`（无 preferred）或 `DEVICE_BUSY`（有 preferred 且不可用）。

**增补（多机）：**

- preferred 忙时允许回落其它**同 backend**空闲机（可配置 `ALIOTH_DEVICE_CLAIM_FALLBACK=1`，默认开）。  
- 仅匹配 `backend=cloud_phone` 的任务不得落到 `ldplayer` 槽位。

### 4.2 实连带入（关键缺口）

Claim 成功后，下游必须携带租约上下文，而不是读全局 env：

| 环节 | 要求 |
| --- | --- |
| Session options | 写入 `device_ref`、`device_lease_id`、`morelogin_env_id`（来自 capabilities） |
| `morelogin_adb.refresh` | **入参 env_id = 租约绑定**；禁止再 `resolve_morelogin_cloudphone_id()` 作为多机唯一来源 |
| `CloudPhoneController.ensure_ready` | 使用租约刷新后的 endpoint／auth |
| 单机回退 | 仅当库存只有 1 台且 capabilities 缺 env_id 时，允许读全局 `ALIOTH_MORELOGIN_CLOUDPHONE_ID` |

### 4.3 错误码（调度语义）

| error_code | 含义 | retryable | Worker 行为（第 57 行） |
| --- | --- | --- | --- |
| `DEVICE_POOL_EXHAUSTED` | 无空闲机 | true | 有界等待后再 claim，不算任务业务失败 |
| `DEVICE_BUSY` | preferred 占用且不允许回落 | true | 同上或换 preferred |
| `DEVICE_LEASE_CLAIM_RACE` | fencing 竞态 | true | 立即短退避重试 claim |
| `DEVICE_WAIT_TIMEOUT` | 有界等待耗尽 | false | 失败关闭，未领取租约，未开机安装 |
| `DEVICE_LEASE_ALREADY_OWNED` | Session 已绑其它租约 | false | 失败关闭 |
| `APP_DEVICE_LEASE_LOST` | 续租失败 | true | 中止设备操作，走恢复／隔离 |

---

## 5. 池满等待语义（第 57 行已落地）

### 5.1 原则

- **先有设备租约，再 Bootstrap／powerOn／install**。  
- 池满不得消耗云手机开机时长。  
- 等待是**调度态**，不是探索失败。

### 5.2 MVP 行为

1. Worker 领取 App 任务后、进入 `AppQcWorkflow` 前或 claim 失败时：  
   - 若 `DEVICE_POOL_EXHAUSTED`／`DEVICE_BUSY`：进入 `waiting_for_device`（任务日志／insight 可观测）。  
2. 退避：指数退避，上限 `ALIOTH_DEVICE_WAIT_MAX_SECONDS`（建议默认 600）。  
3. 超过上限：任务 `failed`／`review`，`error_code=DEVICE_WAIT_TIMEOUT`，**retryable=false**。  
4. claim 成功后清除等待标记，进入既有 Bootstrap。  
5. `worker.concurrency` 配置建议 ≤ 启用中的 cloud_phone 槽位数；超出时依赖等待，不依赖互抢。

### 5.3 明确不做（MVP）

- 工作台完整设备运维 UI（第 60 行后置）。  
- 跨任务优先级插队。

---

## 6. 配置与种子

| 变量 | 用途 |
| --- | --- |
| `ALIOTH_APP_DEVICE_LEASE_BACKEND` | 生产必须 `postgres` |
| `ALIOTH_DEVICE_POOL_SLOTS` | 当前库存 `cloud_phone:qa-a,cloud_phone:qa-b` |
| `ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS` | `1716966721435348,1721139888752705`（qa-a 第一台、qa-b=CP-2；第 56 行落地） |
| `ALIOTH_DEVICE_CLAIM_FALLBACK` | preferred 忙是否回落，默认 `1` |
| `ALIOTH_DEVICE_WAIT_MAX_SECONDS` | 池满等待上限，默认 `600` |
| `ALIOTH_MORELOGIN_*` | Open API（`https://api.morelogin.com` + OAuth2）或 Local API；多机时按租约 env_id 调用。服务器零客户端必须配 `ALIOTH_MORELOGIN_API_ID`／`API_KEY`。 |

进程启动或运维命令：按 slots + env 映射 upsert `qc_device_resource`。

---

## 7. 与 Bootstrap／Closure 边界

| 事件 | 设备租约 |
| --- | --- |
| Bootstrap 成功推进 | renew → `active`，守卫覆盖安装／开跑 |
| APP_READY／Explorer／Final Observation | 守卫仍在；失租约则取消操作 |
| Closure 正常完成 | 条件 `release` |
| Session LOST／FAILED／关闭异常 | `quarantine`，原因入审计 |
| 租约过期 | 仅进 `recovering`，健康检查后才 `available` |

禁止用 Bootstrap lease 或 Session Gate 代替设备占用。

---

## 8. 验收口径（第 58 行；≥2 线）

### 8.1 必备前置

1. alembic ≥ `20260814_0058`，存在 ≥2 条 `cloud_phone` 资源且 `capabilities.env_id` 不同。  
2. MoreLogin 客户端在线，两台机能同时 ADB。  
3. `ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres`，Worker concurrency ≥ 2。

### 8.2 通过标准

| 项 | 标准 |
| --- | --- |
| 互斥 | 双任务同时 claim 得到不同 `device_ref`／`env_id` |
| 实连 | 两边截图／时间线均可区分，且绑定各自机位 |
| 释放 | 结束后两台 `health_state` 回到可领取 |
| 故障隔离 | 人为弄挂一台后，另一任务仍能跑完或按 quarantine 收敛，不双杀 |
| 池满 | 人为占满两台后再提交第三任务：进入等待或超时失败，不出现双任务同 `env_id` |

未跑真实双机时，交付说明必须写：**仓库／单测闭环 ≠ 生产双线验收**。

仓库闭环回归：`pytest services/api/tests/test_app_cloud_phone_dual_line.py -q`。

实连冒烟（会 powerOn，会计费）：`python scripts/smoke_cloud_phone_dual_line.py`。

---

## 9. 落地顺序（对接排期）

| 顺序 | 排期 | 交付 |
| --- | --- | --- |
| 0 | **第 54 行（本文）** | 设计冻结 |
| 1 | **第 55 行（已落地）** | PG 设备池 `claim/renew/release`；过期进入 `recovering`；终态 Session 回收 available；非终态超时 quarantine；Bootstrap 前跨进程互斥；池满 `DEVICE_POOL_EXHAUSTED`/`DEVICE_BUSY` 不得安装 |
| 2 | **第 56 行（已落地仓库闭环）** | MoreLogin／CloudPhone 按租约 `capabilities.env_id` 选机；`powerOn`／刷新 ADB 绑定租约；全域 `ALIOTH_MORELOGIN_CLOUDPHONE_ID` 仅单机回退 |
| 3 | **第 57 行（已落地仓库闭环）** | Worker 有界等待 + concurrency 超槽告警（不改并发数） |
| 4 | **第 58 行（已落地仓库闭环）** | 2×2 冒烟与回归（单测矩阵；生产实连另验） |
| 后置 | 第 59～60 行 | N 线扩稳、工作台可视化 |

---

## 10. 决策摘要

1. **设备占用以 PG 租约为唯一事实源**；进程内池不得宣称生产并发安全。  
2. **多线 = 库存多 env + 租约驱动实连**；不是提高 Worker concurrency。  
3. **池满先等设备，再开机安装**。  
4. **MVP 先验 2 线**；N 线与可视化单独立项。

---

## 11. 排期回写建议（第 54 行）

| 字段 | 建议 |
| --- | --- |
| 状态 | **已完成**（设计） |
| 预估工期 | 1.5 工作日（实际以本文为准） |
| 天枢备注 | 见 `docs/design/2026-08-17-app-cloud-phone-concurrency-scheduling.md`；第 55～58 行仓库闭环已落地，生产双机实连仍须另验 |
