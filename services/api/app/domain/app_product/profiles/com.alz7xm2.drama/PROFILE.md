> aliothapp2.0：本产品统一使用通用 Agent 三合同；下文历史录制脚本说明不再作为执行入口。

# 糖果短剧（com.alz7xm2.drama）产品 Profile

> **无专属编排执行器** → `execution_policy=agent_profile_only`，走 Agent 通用合同。

## Agent 合同

1. **开屏／首屏**：语义「进入/跳过」+ 右上相对区；禁止 ENTRY_TAP / cloud_back。
2. **弹窗**：Watcher → A 文案 → B 几何安全 X → C 蒙层 → D 底栏「<」连点 2 次。
3. **底栏**：`semantic_icon_relative`；顺序 **首页 → 剧库 → 投票 → 专题 → 我的**（五等分）。特征见 `docs/data/tangguo_ui_tree_features.json`。

## 加载纪律

1. 只加载本包 Profile。
2. 禁止按 APK 文件名另选固定 Playbook；本包仅挂 UI Tree 特征 playbook_id=`tangguo`。
3. 失败回写本 Profile，不新开 `app_*_script.py`。
