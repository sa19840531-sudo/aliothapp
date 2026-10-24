---
name: app-script-rhythm
description: Use when an Alioth App QC Explorer must follow untitled*.py derived screenshot and action curricula (splash burst, skip, close popup, tab scroll multi-frame) without executing Airtest scripts.
compatibility: Alioth App Workflow Runtime v2
metadata:
  version: "1.2.0"
---

# app-script-rhythm

## 目的

> **路由（2026-10-22）**：有产品专属编排且 Profile≠`agent_profile_only` → `*_script_override` 主路径；无编排才走 Agent（开屏语义相对／弹窗 Watcher cascade／底栏语义相对）。`ALIOTH_APP_EXECUTION_MODE=agent` 可强制 Agent。失败修正优先回写 Profile。

让 Explorer **学习**历史人工脚本（`untitled.py`～`untitled7.py`）的截图与关键动作节奏（提示词知识）。  
Skill / Playbook / `script_curriculum` 只描述节奏，**禁止导入或执行 Airtest 脚本**。  
运行时对 Android／iOS 优先按 Product Profile 与通用动态探索选动作；本课纲仅作节奏参考。  
**铁律：若仍读课纲，阶段须与对应 untitled*.py 主流程一致；看模板图与调用点，勿被变量名误导（例如 close_1/2/3 图为「跳过」）。**

包名／APK 前缀映射：

| APK 前缀 | 脚本 | playbook_id |
|----------|------|-------------|
| `91cg*` | `untitled.py` | 91cg |
| `hlapp*` | `untitled1.py` | hlapp |
| `hlwapp*` | `untitled2.py` | hlwapp |
| `sns_91sp*` | `untitled3.py` | sns_91sp |
| `sns_911bl*` | `untitled4.py` | sns_911bl |
| `sns_51aw*` | `untitled5.py` | sns_51aw |
| `sns-hjsq*` | `untitled6.py` | sns-hjsq |
| `dx093j*` | `untitled7.py` | dx093j |

## 学习规则（每轮必读）

1. 阅读 `objective`、`script_curriculum.phases`、`script_curriculum.current_phase_hint`、`forbid_hints`、`script_notes`。
2. **按 phases 顺序推进**；优先完成 `current_phase_hint`，再进入下一阶段。
3. 每轮**只能**从 `candidates` 选一个 `candidate_id`；不得自造坐标。
4. 成功标准是**多帧证据**：不是只截一张开屏广告。
5. 同一跳过／关闭连续无效 → 改选 `scroll`／下一底栏／`observe`。
6. 误入浏览器／外链 → `back`，回 App 后继续未完成课纲。
7. `purpose` 必须写明正在推进的 `phase_id`（例如 `推进 tab:社区`）。
8. 课纲未完成且仍有 scroll／tab 候选时，**不得**提前 `terminal`。

## 分包装要点（对照脚本）

### 91cg / untitled.py
等稳 OBSERVE（`before_find_ad`）→ 中部广告外链（点广告→落地截→连续 BACK）→ **点右上「跳过」**（脚本已改：UI文案+右上坐标为主，原 `close_1/2/3` 图仅参考）→ 白圈 X＋橘色确认 → 社区／片库／福利多滑 → AI科技少滑。  
证据帧：`before_find_ad` → `chrome_after_ad_click*` → `back_to_app_*` → `after_close_*` → `shequ_*`／`pianku_*`／`fuli_*`／`ai_tech_*`。

### hlapp / untitled1.py
年龄门（可有）→ 开屏零触控最多 3 帧 → **右上跳过** → 中弹层先截后 X → 首页→精选→片库→直播→AI科技→圈子。

### hlwapp / untitled2.py
年龄 → **等线路**（禁直连）→ 开屏 2 → **跳过** → **等数据初始化** → 弹层／神器甄选 → 吃瓜→AI科技→社区→视频→福利。

### sns_91sp / untitled3.py
年龄 → 等线路 → 开屏 2 → **跳过** → 中广告先截后 X → 公告只确认 → 首页(6)／AI(1)／聊天广场(2)／福利(5)。

### sns_911bl / untitled4.py
等线路 → 开屏最多 3 → **跳过** → 弹层可多轮 → 公告**左侧确认**（禁一键复制）→ 首页→看片→直播→AI科技→福利。

### sns_51aw / untitled5.py
无年龄 → 等线路 → 开屏（**禁点击开炮**）→ **跳过** → 关中弹层 → **仅**首页／吃瓜／大赛。

### sns-hjsq / untitled6.py
等线路 → 开屏 → **跳过** → 弹层／我知道了 → 主页(6)／社区(4)／ACG(10)／应用(13)。

### dx093j / untitled7.py
**高速开屏连拍**（勿先久等线路）→ **无跳过** → 弹层／知道了（主墙无压暗禁乱点 X）→ ACG／视频／暗网／圈子／广场；暗网「立即开通」**只截不点**。

## 推荐阶段动作

| 阶段语义 | 倾向候选 | 动作 |
|----------|----------|------|
| 开屏／等待 | splash／wait／observe | observe |
| 跳过 | 右上跳过／tap_skip | click |
| 关弹层／确认 | close／X／确认 | click |
| 外链取证（仅 91cg） | 中部广告／ad_outbound | click → back |
| 神器甄选 | shenqi／左缘滑 | observe／scroll／click |
| 栏内多帧 | 滑动／swipe | scroll |
| 换栏 | 切换底栏「…」 | click |
| 恢复 | 返回键 | back |

## 输出纪律

- `kind=action`：合法 `candidate_id` + 含 `phase_id` 的短 `purpose`。
- completed 只能由平台 `objective_satisfied` 触发，模型不得自行宣布课纲完成。
