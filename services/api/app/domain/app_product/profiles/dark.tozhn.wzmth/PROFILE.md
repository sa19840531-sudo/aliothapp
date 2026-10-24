> aliothapp2.0：本产品统一使用通用 Agent 三合同；下文历史录制脚本说明不再作为执行入口。

# 欲漫色（dark.tozhn.wzmth）产品 Profile

> 编排知识给 **Agent** 按包加载。原固定脚本 override **默认已关闭**。
> 一次失败 / 一次修正 → 回写本文件与 `profile.json`，勿再扩展产品 `*_script.py`。

## 加载纪律

1. 只加载当前 `package_name` 的 Profile，不要背全产品坐标。
2. 关键场景（底栏「我」、详情返回、会员时序等）用 `taps` / `timing_rules`。
3. **弹窗关闭 = Watcher cascade**：`popup_close_policy=watcher_cascade：A文案→B几何安全X→C蒙层→可选单次「<」
4. 底栏 `y` 不得超过 `gesture_y_max=2794`。

## Agent 决策提示

1. `load_app_product_profile_tool(package_name)`。
2. 认页：会员拦截 / 弹窗遮罩 / 详情 / 底栏 / 外跳浏览器。
3. 弹窗仍在 → `app_cloud_back_tool`（优先文案/几何X，兜底单次「<」）；会员拦截若有 `timing_rules`/`cloud_back_twice` 则严格执行。
4. 失败只回写本 Profile 一个键，不新开脚本。
