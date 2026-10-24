> aliothapp2.0：本产品统一使用通用 Agent 三合同；下文历史录制脚本说明不再作为执行入口。

# 禁果短剧（dark.nvycy.ambpu）产品 Profile

> **主路径**：`execution_policy=product_orchestrator` → `jinguo.py` / `script_jinguo_override`。  
> **无编排／强制 Agent** 时才用下面开屏／弹窗／底栏合同。  
> 一次失败 / 一次修正 → 回写本文件与 `profile.json`。

## 加载纪律

1. 只加载当前 `package_name` 的 Profile。
2. 有专属编排时优先步进脚本；关键落点仍可回写 `taps`。
3. Agent 回落时：**开屏**＝语义进入/跳过＋右上相对；**弹窗**＝Watcher→A→B→C→D「<」×2；**底栏**＝语义＋相对六槽（首页→剧场→视频→社区→福利→我的）。
4. 底栏 `y` 不得超过 `gesture_y_max=2794`。

## Agent 决策提示（仅回落）

1. `load_app_product_profile_tool(package_name)`。
2. 认页后按开屏／弹窗／底栏三合同；禁止把海角默认底栏文案硬套禁果。
3. 失败回写 Profile；不要在 Agent 路径新开固定脚本。
