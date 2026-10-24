# App 探索与 ADB 操作对照（研发旁注）

本文件供研发查阅，**不是** Agent 运行时必读 Skill。正式 Explorer 纪律以同目录上级 [`SKILL.md`](../SKILL.md) 为准。

## 平台强制 vs Skill 指导

| 主题 | 平台强制（代码拒绝） | Skill 指导（决策纪律） |
|------|----------------------|------------------------|
| 坐标来源 | 无合法候选／点则不执行；禁止裸 Driver | 只选 `candidate_id`，不猜 point |
| 包边界 | 外部包业务点击失败 | 外跳后选 back／恢复 |
| 整屏蒙层 | UIA 整屏 Dismiss 不取中心 | 勿把广告本体当关闭 |
| 滑动后旧框 | 清障可容差重绑；业务遇 `POST_SCROLL_REBIND_REQUIRED` | 必须等新 Capture 再选；禁止硬点旧 id |
| 系统弹窗／入口层 | 高风险允许／支付等拒绝推进 | 先清障；`APP_READY`≠业务首页 |
| 返回流程 | 外跳／恢复路径受控 | 语义 `back`，以新帧验收 |
| letterbox／safe_area | 宽高比差过大走 letterbox；黑边／系统栏外点拒绝 | 视觉点须标 `capture_px`＋真实截图尺寸 |
| 旋转 | 可证明时仿射；否则审计 `rotation_unproven` | 勿假设横竖已校正；当前帧截图通常已是显示方向 |
| 同帧观察 | `capture_same_frame` 共享 epoch；非内核原子 | 禁止用旧会话树证明新截图 |
| surface_tech | UIA 有界推断，权威 false | 无树 → unknown，勿产品画像硬套 |
| 长图／视口 | 序列血缘；拼接派生禁升格 | 主证据用 `viewport_primary`；禁伪完整长页 |
| 广告外跳取证 | 包边界＋回源终态；一生一次 | 源→落地→回源三联；外页不计覆盖 |
| 收银台长链 | 外部支付只恢复 | 店内逐步 `checkout_chain_step`；高风险不推进 |
| 连续拖动 | （平台计划有界） | `player_scrub_series` 拖前／中／后；禁单帧冒充 |

## 证据角色合同（旁注）

正式角色码与校验见 `services/api/app/application/app_evidence_role_contract.py`（`app-evidence-role.v1`）。Skill 正文 §7–§10 与之对齐。

## 页面观察（卡片：整理 ADB 页面观察能力）

统一要拿的五类事实与通道见清单 §2（**非**单一 API，探索主事实是截图）：

1. 前台 App（package／activity）
2. 页面截图（PNG＋像素宽高）
3. 屏幕尺寸（`wm size`／display metadata）
4. 方向（由截图高宽推导，非 sensor）
5. 可用界面结构（UIA／View Hierarchy；有结构 vs 无结构矩阵见清单）

Native／WebView／Flutter 有结构／无结构分别能拿到什么 → 清单 §2.1／§2.4。

## 页面操作（卡片：整理 ADB 页面操作能力）

四动作唯一入口 + 租约／目标 App 绑定 + 坐标换算见清单 §3：

| 动作 | 入口 | 绑定 |
|------|------|------|
| tap／click | `AdbDeviceOps.tap` | Permit + `allowed_packages` + 动作前身份 |
| swipe／scroll | `AdbDeviceOps.swipe` | 同上 |
| back | `AdbDeviceOps.back` | 包外仅恢复 |
| type_text | `AdbDeviceOps.type_text` | 同上；Explorer 默认不选 |

坐标：`capture_px`／`canonical_px` → `map_point_to_device`（letterbox／safe_area／可证明旋转仿射）；黑边内与系统栏外 **拒绝**；无权威 rotation 时标 `rotation_unproven`。

## 现况清单

完整观察矩阵、四动作契约、坐标空间、Manifest 诚实度与缺口见：

`docs/design/2026-09-07-adb-android-observation-ops-inventory.md`
