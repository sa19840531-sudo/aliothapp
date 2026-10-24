> aliothapp2.0：本产品统一使用通用 Agent 三合同；下文历史录制脚本说明不再作为执行入口。

# MaMa短剧（com.dzu4.app）产品 Profile

> **主路径**：`execution_policy=product_orchestrator` → `mama.py` / `script_mama_override`。  
> 通用 Agent 只负责启动、把截图记入证据并验证；**不得**重判 mama 点位。  
> 知识来源：`.../untitled.air/mama.air/mama.py`（工作区副本：`services/api/resources/recording/mama.py`）。

## 分辨率与云手机

- 逻辑分辨率：`1440 × 3040`
- 云手机底部导航栏最右侧「<」返回（||| / Home / < 的右边那个；不是弹窗圆 ×，**不是** `keyevent 4`）：`(1120, 2955)`
- 底栏手势带：`y/3040 >= 0.92` 时抬到 `y=2794`

## 产品编排边界

- 触控、等待、前台回复与顺序全部留在 `mama.py`（含 `press_cloud_back_twice` 同一条 adb、中间不截图）。
- Runtime 按 `mama.py` 逐步回放；坐标来自脚本，不读视觉框改点。
- Agent / Skill 不得凭包名另拼点击课纲；`ALIOTH_APP_EXECUTION_MODE=agent` 可强制 Agent。

## 关键时序：会员拦截

**场景**：试看结束，「开通会员 / 暂不开通」。

1. `mama.py` 的 `press_cloud_back_twice()`（同一条 `input tap ; input tap`）。
2. **两下中间禁止** `sleep` / `screenshot` / `capture_screen`。
3. 完成后再截图校验（第 6、7 张）。

**失败签名**：第 7 张仍在播放；或 7/8 播放循环。

## 底栏「我」

- `me_tab` ≈ `(1260, 2794)`（四等分最右格）。
- **禁止** `(989, 2794)`（专题带）。

## 详情页左上角返回

- `detail_page_back` ≈ `(99, 221)`。
- **禁止** `(411, 150)`（封面棕色区打空）。

## 金币钱包

- 选「300 / ¥30」：`coin_300` ≈ `(263, 1430)`；**禁止** `(495, 1356)`。
- 钱包页返回：`wallet_page_back` ≈ `(79, 216)`；**禁止** `(403, 137)`。

## 弹窗（仅 Agent 强制模式）

当 `ALIOTH_APP_EXECUTION_MODE=agent` 时走通用合同：Watcher → A 文案 → B 几何安全 X → C 蒙层 → D 底栏「<」×2。  
产品编排主路径不按弹窗重选点。

## Agent 职责（编排开启时）

1. 启动 / 进入产品编排（Runtime 注入 `script_mama_override`）。
2. 每步后截图留证。
3. 核对脚本交出的截图（缺图记验证失败，**不**改由 Agent 重选点）。
