# 2026-08-06 aliothapp 审查整改对照报告

> 对照文件：`f:\2026-08-06-qingyunke-sa19840531-commit-review.md`  
> 整改仓库：`F:\aliothapp`  
> 整改日期：2026-08-06（Asia/Shanghai）  
> 范围：审查第一～二批中 **已落地在 aliothapp 的 Demo／云手机／sa 侧问题**；AppV2／`qc_capture` SQL／Completion Gate **未整包搬迁**（仓库缺 `app_v2_runtime`／`official_api`）。

## 一、结论

本轮已在 `aliothapp` 完成审查指出的 **匿名 Demo 入口、Agent 伪成功、Matrix 造假、MoreLogin／CloudPhone 半成品** 修复，并补齐定向测试（**32 passed**）。

仍须遵守审查总结论：

- **不要**开启 `ALIOTH_APP_QC_ENABLED` 正式 App V2 流量（aliothapp 尚未具备完整 PG 动作链／Completion Gate）。
- **不要**在生产 Gateway 暴露 `mobile-demo`；本机联调必须显式 `ALIOTH_MOBILE_DEMO_ENABLED=1` 且 loopback（或隔离网放行开关）。
- 与 `aliothdev1.5@9914323` 合流前，先做 CaptureRef 消费者对齐，再处理 `qc_capture` SQL 与 Completion Gate。

## 二、逐条对照

| 审查项 | 适用于 aliothapp？ | 本轮状态 | 证据 |
| --- | --- | --- | --- |
| **Blocker-1** `qc_page_capture` vs `qc_capture` | 否（缺 AppV2／正式动作仓储） | **未修／合流后** | 无 `app_runtime_composition`、`action_execution_repository` App 链 |
| **Blocker-2** 匿名 mobile-demo | 是 | **已修** | `mobile_demo_gate.py`；`router.py` 条件挂载；Artifact `to_dict()` 去掉 `storage_path`；`test_mobile_demo_gate.py` |
| **P1-1** `page_capture_id` / CaptureRef | 部分 | **门禁已加；协议未迁** | 全仓仍 `CaptureReference.page_capture_id`（自洽）；`test_capture_field_consistency.py` 防合流后 AttributeError |
| **P1-2** Completion Gate | 否 | **未修／合流后** | 缺 App Worker Inspection／Gate |
| **P1-3** 截图幂等键 | 否（正式 Official 链） | **未修／合流后** | — |
| **P1-4** Session reattach | 否 | **未修／合流后** | — |
| **P1-5** Agent 有截图即 succeeded | 是 | **已修** | `_run_agent_workflow_ad_capture_stub` 仅 `done` 且无失败 → succeeded；否则 incomplete／failed；`test_agent_ad_capture_terminal.py` |
| **P1-6** Matrix 复制第一台 | 是 | **已修** | Agent 路由显式 failed；Airtest 矩阵仍逐 profile；`test_mobile_demo_matrix.py` |
| **P1-7** Demo Adapter 生产化 | 部分 | **未宣称完成** | Runtime 旁路已收口属既有；完整 Gate／多 Worker 证据源仍属合流项 |
| **第六节 MoreLogin** 分页／auth／bool／powerOn | 是 | **已修** | `morelogin_adb.py`；`test_morelogin_adb_refresh.py` |
| **第六节** 任意 ADB 回退 | 是 | **已修** | `cloud_phone.ensure_ready` 去掉任意 `host:port` 回退 |
| **第六节／半成品** install／wm | 是 | **已修** | `install_apk` no-streaming／push+pm；`prepare_app_for_script` wm reset+keep；超时非法值容错；`test_cloud_phone_install.py` |
| **P2** Ruff／大仓膨胀／提交规范 | 部分 | **本轮未清 Ruff 全量** | 聚焦阻断／P1；P2 另排期 |

## 三、关键代码变更

- [`services/api/app/api/v1/mobile_demo_gate.py`](../services/api/app/api/v1/mobile_demo_gate.py)（新）
- [`services/api/app/api/v1/router.py`](../services/api/app/api/v1/router.py)
- [`services/api/app/api/v1/mobile_demo.py`](../services/api/app/api/v1/mobile_demo.py)（Agent 终态、Matrix 拒绝）
- [`services/api/app/api/v1/mobile_demo_agent_ad_capture.py`](../services/api/app/api/v1/mobile_demo_agent_ad_capture.py)（达步数上限文案）
- [`services/api/app/application/app_screenshot_artifacts.py`](../services/api/app/application/app_screenshot_artifacts.py)
- [`services/api/app/infrastructure/device/morelogin_adb.py`](../services/api/app/infrastructure/device/morelogin_adb.py)
- [`services/api/app/infrastructure/device/cloud_phone.py`](../services/api/app/infrastructure/device/cloud_phone.py)
- [`.cursor/skills/alioth-mobile-demo/scripts/restart_demo_api.ps1`](../../.cursor/skills/alioth-mobile-demo/scripts/restart_demo_api.ps1)（本机重启写入 `ALIOTH_MOBILE_DEMO_ENABLED=1` + bind host）
- [`infra/env/.env.cloud-phone.example`](../../infra/env/.env.cloud-phone.example)（`AUTO_POWER_ON` 默认示例改为 0）

## 四、本轮验证

```text
pytest services/api/tests/test_mobile_demo_gate.py \
  services/api/tests/test_agent_ad_capture_terminal.py \
  services/api/tests/test_capture_field_consistency.py \
  services/api/tests/test_mobile_demo_matrix.py \
  services/api/tests/test_cloud_phone_install.py \
  services/api/tests/test_morelogin_adb_refresh.py -q
```

结果：**32 passed**（2026-08-06 本地执行）。

## 五、与 aliothdev1.5 合流建议顺序

1. **CaptureRef 消费者清单**：将 `android_capture_runtime`／`mobile_demo_ai_driver`／`mobile_demo_agent_ad_capture` 与 tip `9914323` 对齐（或保留旧协议但同一批次改完）。
2. 合并／引入 AppV2 后修复 **Blocker-1**：Action／Evidence Gate 以 `qc_capture` 为主表。
3. App Worker：**Explorer 终态 ≠ Task 完成**；接 Inspection／Rule Judge／Completion Gate（P1-2）。
4. Official 截图幂等键按 `action_execution_id + phase`（P1-3）；Worker reattach（P1-4）。
5. 真实 PostgreSQL App 动作全链路不再 skipped 后再评估 `ALIOTH_APP_QC_ENABLED`。

## 六、本机 Demo 使用注意

```powershell
# restart_demo_api.ps1 已设 ENABLED=1 且默认 BindHost=127.0.0.1
# 若手工起 uvicorn，至少：
$env:ALIOTH_MOBILE_DEMO_ENABLED = "1"
$env:ALIOTH_API_BIND_HOST = "127.0.0.1"
# 云手机自动开机需显式：
$env:ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON = "1"
```

生产 Compose／公网 Gateway：**不得**设置 `ALIOTH_MOBILE_DEMO_ENABLED=1`，也不得 `ALLOW_NON_LOOPBACK`。
