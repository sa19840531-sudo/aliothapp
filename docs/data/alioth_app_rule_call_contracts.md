# Alioth APP 专用规则调用契约（独立文件）

> **独立产出**：不与 Web/H5 种子 CSV 混写、不回写原表。

- JSON：[`alioth_app_rule_call_contracts.json`](./alioth_app_rule_call_contracts.json)
- 来源只读：`alioth_first_ruleset_qc_rule_seed.csv`
- 录屏对照：`螢幕錄製 2026-09-10 172817.mp4`（海角 hjll）

## 与混用种子的区别

| 项 | 原种子（混用） | 本文件（APP 专用） |
| --- | --- | --- |
| `target_clients` | WEB + APP + H5 | 仅 `APP` |
| 结构证据 | `dom` / `ui_snapshot` | `ui_tree`（可选 `view_hierarchy`） |
| 位置字段 | `page.url` | `page.package_name` + `page_type` |
| 网络/日志 | `har` / `console_log` | `network_trace` / `device_log`；Capture 另投影同帧 `har`（`format=network_events`，无代理时空 entries） |
| 连续帧 | `temporal_trace`（timed_capture） | 显式请求时真实连拍写 `temporal_trace`（默认 3@1s；开屏类 12@1s）；默认 after 不含 |
| 步骤动作 | `navigate` / CSS `click` | `open_screen` / `tap`（语义定位） |

## 海角乱伦 UI Tree 特征（`ui_tree_features` / `hjll_ui_tree_features.json`）

- **产品**：海角乱伦（`playbook_id=hjll`，包名 `dark.wwcskrpojs.ebjhyebjbl`）
- **独立特征 JSON**：[`hjll_ui_tree_features.json`](./hjll_ui_tree_features.json)（按包名／课纲加载）
- **契约内嵌／目录**：本文件 `ui_tree_features` 与 `ui_tree_features_by_playbook.hjll`

`flow_order`：年龄门 → 开屏广告 → 弹窗下方 X → 主页只截 → 短剧（见解锁层左上角 CLICK）→ 暗网 → AI → 社区 → 我的

## 黑料不打烊 UI Tree 特征（`hlapp_ui_tree_features.json`）

- **产品**：黑料不打烊（`playbook_id=hlapp`）
- **APK**：`F:\apptest\黑料不打烊app\hlapp_1.8.9_260827_7.apk`
- **包名**：`top.qkjzg.zfiufm`（旧马甲 `com.edxuf.euloop`、`uk.frtju.nnxsaf`）
- **独立特征 JSON**：[`hlapp_ui_tree_features.json`](./hlapp_ui_tree_features.json)
- **脚本对照**：`untitled1.py`
- **2026-09-15 qa-c 实采**：`172.104.172.119:30907`（Override `1080x2280`）；证据目录 `.tmp_hlapp_uitree_collect`

`flow_order`：

1. **age_gate**：先截图 → 点「我年满18岁-进入」（中心约 `0.50,0.71`；禁止「我未满18岁-离开」）
2. **startup_ad**：零触控连拍最多 3 帧 → 只点一次右上「跳过」（中心约 `0.88,0.08`）
3. **popup_ads**：系统公告／中间广告先截后点下方空心白圆 X（系统公告 X 约 `0.50,0.735`，勿用 `0.85` 防进详情）
4. **home_top_category_tabs**：首页 → 热门吃瓜 → 反差女神 → 今日看料 → 禁播动漫 → 网赚（弹窗未清禁止点）
5. **bottom_tabs**（七格仅点前六）：首页 → 精选 → 片库 → 直播 → AI科技 → 圈子（**禁止我的**；y≈0.945）

底栏匹配只走 `_filter_tabs_to_next_playbook_only`（按产品特征 JSON）；`_pick_next_playbook_bottom_tab` 只委派。

## 调用形状

每条规则的 `call.evidence_request` 可直接给 APP Observation Packet / Probe 组装用。

重新导出：

```bash
python scripts/_export_app_rule_call_contracts.py
```

注意：导出脚本会**保留**已有 `ui_tree_features`（含录屏海角流程），不会用空默认覆盖。
