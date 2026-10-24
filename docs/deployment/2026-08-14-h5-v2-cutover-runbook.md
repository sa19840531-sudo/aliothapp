# H5 从 V1 切到 V2 的操作手册

| | |
|---|---|
| 适用 | H5 质检链路由 V1 单 Agent 切到 V2 双 Agent（Explorer + Rule Judge） |
| 起草 | 立川（2026-08-14） |
| 状态 | 待本地验证 |
| 前置检查 | `scripts/check_h5_v2_cutover_prerequisites.py` |

## 0. 一句话

**两个开关缺一不可**：`ALIOTH_WORKFLOW_VERSION=v2` 决定任务能否进 V2 路由，
`ALIOTH_H5_V2_MODE` 决定进去之后走哪一档。只调后者不生效。

## 1. 开关矩阵

| WORKFLOW_VERSION | H5_V2_MODE | 实际路由 | 谁在执行 |
|---|---|---|---|
| `v1` | 任意 | `h5` | V1 单 Agent（`H5_V2_MODE` 完全不生效） |
| `v2` | `off` | `h5` | V1 单 Agent |
| `v2` | `shadow` | `h5_shadow` | **V1 权威执行**，V2 只读投影旁路记录 |
| `v2` | `on` | `h5_v2` | V2 双 Agent 正式执行 |

影子期的失败被吞掉、不污染 V1 事实（`worker.py` H5_SHADOW 分支）。

## 2. 本地验证步骤

### 2.1 起本地依赖

```bash
docker compose -f infra/docker-compose.yml up -d alioth-postgres alioth-redis
```

### 2.2 跑迁移到当前头

```bash
python -m alembic -c services/api/migrations/alembic.ini upgrade head
```

当前预期头为 `20260814_0056`（pass 清单证据不可原地变更约束）。
迁移失败立即停止，不要继续起 Worker。

### 2.3 切到影子档

在 `.env.local` 写入（本地开发用，勿提交）：

```bash
ALIOTH_WORKFLOW_VERSION=v2
ALIOTH_H5_V2_MODE=shadow
```

### 2.4 确认前置

```bash
python -m scripts.check_h5_v2_cutover_prerequisites --target shadow
```

`ready=True` 才继续。唯一阻塞项通常是 `WORKFLOW_VERSION` 忘了改。

### 2.5 起 Worker 跑一个真实 H5 任务

```bash
python -m services.api.app.worker
```

观察三件事：

1. **V1 结论正常产出**——影子期 V1 仍是权威，业务结果不应有任何变化；
2. 日志里有 V2 只读投影记录，没有 `H5 Shadow recorder 未配置` 警告；
3. 出现 `H5 V2 只读 Shadow 记录失败` 时说明投影链路有问题，但**不影响 V1 结论**，
   可以继续排查而不必回滚。

### 2.6 核对后再切 on

影子期至少跑通一批真实任务、并人工核对 V2 投影结论与 V1 一致后：

```bash
ALIOTH_H5_V2_MODE=on
python -m scripts.check_h5_v2_cutover_prerequisites --target on
```

脚本会提示 `shadow_observed_before_on` 这项人工确认——它永远不自动通过，
只是提醒不要跳过影子期。

## 3. 回滚

把 `ALIOTH_H5_V2_MODE` 改回 `shadow` 或 `off` 并重启 Worker 即可。
影子期与切换期都不改写 V1 的任何业务事实，回滚不需要数据修复。

## 4. 与证据链灰度的关系

以下两个开关与 H5 切换**相互独立**，可各自推进，前置检查里只报 WARN：

| 开关 | 作用 | 默认 |
|---|---|---|
| `ALIOTH_EVIDENCE_MANIFEST_READ_MODE` | 判定侧按清单读取证据（off/shadow/on） | `off` |
| `ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE` | 证据不全时拦截离场动作（off/shadow/on） | `off` |

建议顺序：先把 H5 切到 V2 跑稳，再单独灰度证据链两档；
同时开多个灰度会让问题无法归因。

> 切 `ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE=on` 前必须确认需求集发布与清单裁决
> 已在采集流程中产出事实（`application/capture_evidence_readiness.py`），
> 否则门禁会因「无 pass 清单／无需求集」拒绝全部离场动作。

## 5. 已完成的本地装配验证（2026-08-14）

不连数据库即可验证的部分已实测通过：

- `WORKFLOW_VERSION=v2` + `MODE=shadow` → 路由 `h5_shadow`，影子记录器可构建
- `WORKFLOW_VERSION=v2` + `MODE=on` → 路由 `h5_v2`，`RuntimeBackedH5WorkflowHandler` 可构建
- `WORKFLOW_VERSION=v1` → 无论 MODE 为何都路由 `h5`（V1）

仍需真实环境验证的：迁移执行、影子期投影落库、V2 双 Agent 端到端执行。
