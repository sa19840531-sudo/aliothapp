# APP 产品专属编排交给 Agent（Profile + 提示词）

日期：2026-10-21  
仓库：`F:\aliothapp`  
状态：Phase 1 已落地；Phase 2 默认关闭产品固定脚本 override

## 目标

- **不再**为每个 APP 维护产品专属固定编排（`app_*_script.py` 的 `*_script_override`、外部 Airtest 产品脚本）作为主路径。
- 产品差异沉淀为 **按包名的 Product Profile**，由 Agent Skill / 提示词驱动。
- 对齐整合方案 **M-A5**。

## Phase 1

| 交付 | 路径 |
| --- | --- |
| Profile | `services/api/app/domain/app_product/profiles/<package>/` |
| 决策辅助 | `domain/app_product/orchestration.py` |
| Skill | `agents/skills/phase/app-product-orchestration/SKILL.md` |
| 设备 Tool | `agents/tools/app_device_tools.py` |
| APP 路由挂载 | `workflow_skill_routing` 增加 `phase`；`app-workflow` / `qc-workflow` 调度说明 |

MaMa（`com.dzu4.app`）Profile 已承接原 `mama.py` 全量落点与课纲提示（会员双 back、`me_tab`、详情/钱包返回、`coin_300` 等）。  
`mama.py` 标 deprecated；Agent 默认路径，禁止再 `airtest run mama.py`。

## Phase 2（切流）

环境变量 `ALIOTH_APP_EXECUTION_MODE`：

| 值 | 行为 |
| --- | --- |
| 未设置 / `agent` / `profile` | **默认**：`product_fixed_script_enabled()=False`；Explorer 不走 `*_script_override` |
| `legacy_script` / `airtest` | 仍允许产品固定脚本 override（对照回放） |

入口：`application/app_product_orchestration_policy.py` → `try_product_script_override`。  
`resolve_product_orchestrator_id` **仍保留**包名→playbook 映射，供 UI Tree 特征；**不**再等同于「必须跑固定脚本」。

## Agent 学习约定

1. 失败一次 → 回写同一包 `PROFILE.md` / `profile.json`。
2. 不写新产品固定 tap 脚本。
3. 关键时序（如双 back）中间禁止 `screenshot` / `sleep`。
4. **按包按需加载**：Agent 只加载当前 `package_name` 的 Profile 摘要（`to_agent_summary` / `key_taps`），不要背全产品全量坐标。
5. **常规关窗 = 云手机 back**：`popup_close_policy=cloud_back`；点系统可见 back，**不识别 ×**；会员拦截可用 `cloud_back_twice`。
6. **开屏/首屏 = 语义 + 相对位置**：`startup_skip` →「进入/跳过」+ 右上相对区；禁止用 back 代替进入、禁止脚本 `ENTRY_TAP`。
7. 迁入 Profile 的是关键场景落点（底栏「我」、详情返回、会员时序等），不是整条 Airtest 录制序列。
