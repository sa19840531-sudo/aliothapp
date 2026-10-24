# domain/app_product — APP 产品 Profile

按包名沉淀产品知识。执行路由：

1. **有专属编排执行器**（`execution_policy=product_orchestrator` + `*_script_override`）→ 产品编排主路径  
2. **没有** → Agent 通用合同（开屏／弹窗／底栏见下表）

## 关窗／底栏分型（Agent 路径硬规则；有编排时由产品脚本主导）

| 场景 | 策略 | 做法 | 禁止 |
|------|------|------|------|
| **开屏／首屏广告** | `startup_skip` → `vision_skip` | 语义图标＋相对位置；标记仅限 **进入／跳过／skip／entry／enter**；默认右上相对区；**满 2 张截图证据必须点进入／跳过** | `cloud_back`／`cloud_back_twice`、keyevent 4、弹层下方／右上 X、脚本 ENTRY_TAP、满 2 帧后继续干等 |
| **弹窗广告** | `popup_close_policy=watcher_cascade` | **Watcher** → **A 文案** → **B 几何安全 X** → **C 蒙层空白** → **D 底栏「<」连点两下** | keyevent 4、中部假 X、脚本 `POPUP_CLOSE_*`、当成开屏进入、D 档只点一下 |
| **底栏（禁果／糖果等）** | `bottom_nav_policy=semantic_icon_relative` | 语意图标／文案＋相对等分槽（禁果六栏：首页→剧场→视频→社区→福利→我的；糖果五栏：首页→剧库→投票→专题→我的） | 绝对 px 当主路径、海角默认文案硬套、点系统手势条／云手机「<」当业务 Tab |
| **官方公告** | `notice_ack` → `vision_tap` | 点语义「我知道了／知道了」（有框）；开屏阶段遇真弹窗／公告须收口转 popup_chain | 当开屏进入；无确认钮时再走 cascade |
| **会员拦截** | 同弹窗 `watcher_cascade` | 同上；D 档 **连点两下**「<」 | 两下之间截图／sleep、keyevent 4 |

开屏与弹窗 **不得共用** 关闭候选池：开屏阶段不得选「关闭弹层下方X」；弹窗阶段不得走开屏进入／跳过（除非文案本身是弹窗「跳过」关钮）。

## 约定

- 目录：`profiles/<package_name>/PROFILE.md` + `profile.json`
- **一次任务只加载一个包**
- `execution_policy`：`product_orchestrator`（有脚本）／`agent_profile_only`（无脚本或强制 Agent）
- 新产品默认 Agent；**不要**再为新产品新开 `app_*_script.py`，除非明确要专属编排
- 一次失败 / 一次修正 → **回写同一 Profile**
- 学习主存为 Profile 文件（可进仓库、可人工改）

## 加载

- `load_profile(package_name)` / `AppProductProfile.to_agent_summary()`
- Tool：`load_app_product_profile_tool`（F04：只读能力事实；不含绝对坐标／taps）
- Explorer 注入：`app_product_capability_facts`（与 Tool 同源；不得覆盖当前帧候选）
- 包名匹配大小写不敏感（如 `dark.NVYCy.AmBPu` ↔ `dark.nvycy.ambpu`）

## 决策

见 [orchestration.py](orchestration.py) / [popup_close.py](popup_close.py)：

- 开屏/首屏 → `vision_skip`（语义进入/跳过 + 右上相对区 / `app-startup-ad-skip.v1`）
- 常规弹窗仍在 → `popup_watcher_cascade`（A→B→C→D `cloud_back_twice`）
- 会员拦截类型 → 同一 cascade
- 底栏/详情/钱包 → `key_taps`
