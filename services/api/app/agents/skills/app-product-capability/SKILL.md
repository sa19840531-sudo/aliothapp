---
name: app-product-capability
description: Android APP 只读产品能力事实。按包名解释时序／关窗策略／语义恢复提示；禁止授予绝对坐标或直接设备 Tool；无 Profile 走通用候选探索。
---

# app-product-capability

## 职责（F04／F12）

指导 **App Explorer** 在已有当前帧候选的前提下，只读理解产品差异：

1. 消费 V2 Explorer 输入中的 `app_product_capability_facts`（观察元数据同源）；`grants_absolute_coordinates` 恒为 false。
2. **禁止**把 Profile 中的历史 taps／分辨率／`cloud_back_tap` 当作可执行坐标。
3. **禁止**调用设备 Tool；只从当前帧 `candidate_id` 选择动作，设备副作用只经 Runtime／Coordinator。
4. 无 Profile 或未知包名 → 通用候选探索，不要求新产品必填点位档案。

本 Skill 不写库、不判定规则、不执行动作。

## 何时读取

- `target_platform=app` 且 Explorer 正在选择当前帧 `candidate_id`。
- 会员拦截、播放／详情、底栏、遮罩、支付展示等局面需要语义恢复提示时。

## 强制原则

1. **只读**：能力事实不得覆盖当前帧已验证候选与证据。
2. **无坐标授权**：`grants_absolute_coordinates` 恒为 false；忽略任何绝对点位字段。
3. **开屏 ≠ 弹窗**：开屏用语义「进入／跳过」；弹窗用 Watcher cascade 或语义 BACK，不找圆 ×。
4. **时序提示仅语义**：如「会员拦截 → cloud_back_twice」只影响恢复策略选择，不直接发 ADB。
5. **禁止**为新产品新增固定 tap 脚本主路径。

## 与其他 Skill 的关系

- 并列：`app-workflow`（探索合同）。
- 不替代：绿色规则 Skill、冻结检查点合同。
- 旧 `phase/app-product-orchestration` 与 `app-script-rhythm` **不**再挂入 App Explorer 路由。
