# 玉衡平台首期规则种子数据表

- 来源：https://docs.google.com/spreadsheets/d/1u2de4EiuEL-BtRZ_D7cd88otbWOnK525CB4IO5bMkcQ/edit?gid=0#gid=0
- 筛选条件：`本期是否实现（青云客） = 是`
- 筛选结果：39 条
- `rule_code` 生成规则：使用在线表格 `序号` 列，格式为 `alioth_{序号}`。
- `page_type` 表示规则主适用页面类型，没有唯一主页面时为 `NULL`；`applicable_page_types_json` 是运行时候选规则匹配的唯一页面范围依据。
- `applicable_page_types_json` 只允许使用：`line_loading`、`home`、`content_list`、`content_detail`、`video_list`、`video_detail`、`profile`、`settings`、`payment`、`post_list`、`post_publish`、`activity`、`startup_ad`。
- toast、弹窗、评论区和播放器等页面内状态应归入所在宿主页面类型；禁止使用 `general`、`global` 等泛指值。规则应优先收敛到真实宿主页面，只有经确认必须覆盖全部可匹配页面时才显式展开页面类型；`line_loading` 不进入规则匹配，不应承载规则。规则步骤中的 `expected_page_type` 必须与单一适用页面一致，没有唯一预期页面时不填写。
- 规则表不再平铺保存在线表格来源字段；来源追溯统一依赖 `qc_ruleset.source_ref` 与规则内容快照。

## qc_ruleset 种子字段

| 字段 | 建议值 |
| --- | --- |
| `ruleset_code` | alioth_first_ruleset |
| `name` | 玉衡平台首期质检规则集 |
| `version` | 1 |
| `status` | enabled |
| `description` | 玉衡平台首期质检规则集 |
| `source_type` | spreadsheet |
| `source_ref` | https://docs.google.com/spreadsheets/d/1u2de4EiuEL-BtRZ_D7cd88otbWOnK525CB4IO5bMkcQ/edit?gid=0#gid=0 |
| `rule_count` | 39 |
| `page_sampling_policy_json` | {"mode":"by_page_type","max_pages":100,"include_page_types":[],"exclude_page_types":[]} |
| `loop_guard_json` | {"max_steps":200,"max_duration_seconds":3600,"max_ai_calls":200,"no_progress_limit":8,"stop_on_error":false} |

## qc_rule 可编辑总表（摘要）

> 完整字段见同目录 CSV：`alioth_first_ruleset_qc_rule_seed.csv`。本版只保留规则运行、匹配、判定和人工复核必要字段。

| rule_code | name | category | executor_type | severity | page_type | applicable_page_types_json | check_point |
| --- | --- | --- | --- | --- | --- | --- | --- |
| alioth_2 | 文案语言规范检查 | text | hybrid | low | content_list | ["content_list"] | 文字应使用半角、无衬线字体、不混用简体繁体、符合中文语法、无方言； 除非产品负责人明确指定的位置允许特例，比如日文视频名称、特定运营活动等； |
| alioth_3 | 图片展示质量检查 | visual | hybrid | high | content_list | ["content_list","content_detail","video_list","video_detail"] | 图片(不含广告)应当高清、不模糊、不拉伸、非tab图片不越出屏幕边界、文字不贴图片边不越图片边、人物比例不失调；若有闪烁效果，闪烁频率应<1.5s/次； |
| alioth_4 | 错别字检查 | text | hybrid | low | content_list | ["content_list"] | 非特殊产品运营需求下，不应有任何原因的错别字，比如“登录”和“登陆”等； |
| alioth_5 | 页面模块空白兜底检查 | visual | hybrid | medium | home | ["home"] | 空状态可以显示「暂无数据」类文字，也可以配插图或引导，但不得出现「加载失败」「请刷新」。加载失败必须有重试或刷新按钮。接口空数组算空状态，报错或超时算加载失败；无法区分时不得判通过或不过。加载中的转圈不在本条判断。 |
| alioth_6 | 技术错误直出检查 | visual | rule_engine | high | home | ["home"] | 不应出现明显技术错误页/错误码直出(如 JSON Parse Error、Null、500)； |
| alioth_7 | 外部链接新窗口跳转检查 | interaction | rule_engine | high | activity | ["activity"] | 外部链接操作前后可信截图不一致即通过；缺少前后截图时证据不足。APP 端先不实现。没有广告位或外链清单时不得猜点。 |
| alioth_8 | 互动状态即时反馈检查 | interaction | rule_engine | medium | NULL | ["content_detail","video_detail","profile","post_list"] | 收藏、点赞或提交后 1 秒内要有状态变化、提示或动效。同一操作连做 3 次结果必须一致。可见元素 2 秒内没有变化算无响应。空白和加载失败不在本条判断。 |
| alioth_9 | 数据变更后刷新检查 | interaction | rule_engine | medium | NULL | ["content_detail","video_detail","profile","post_list"] | 页面存在评论数、点赞数等可见数字指标即通过；未识别到数字指标时证据不足。 |
| alioth_10 | 提示文案易懂性检查 | text | llm | high | profile | ["profile"] | 提示的文字应当柔顺，应当没有互联网任何岗位从业经验的人能够看懂和易于接受的描述； 举例：“仅注册用户才能关注”，“才”是一个不适合在短提示中出现的... |
| alioth_11 | 提示文案正向表达检查 | text | llm | high | NULL | ["profile","payment"] | 提示文字使用正向提示语优于反向提示语，例如“您再赚够500积分即可兑换”优于“您的积分不够兑换该物品”； |
| alioth_12 | 提示文案叹号结尾检查 | text | llm | high | NULL | ["content_detail","video_detail","post_list"] | 提示文字不应使用叹号结尾；提示文字的叹号向表达了强烈的情感仿佛在指责用户，此时无需标点符号或可用句号结尾；喜悦气氛下叹号可保留； |
| alioth_13 | 商品价格一致性检查 | payment | rule_engine | critical | payment | ["content_detail","video_detail","payment"] | 同一商品的所有位置价格应一致； |
| alioth_14 | 优惠价低于原价检查 | payment | rule_engine | high | payment | ["content_detail","video_detail","payment"] | 会员充值等 payment 页面未展示优惠价时直接通过；展示优惠价时，优惠价应低于原价。 |
| alioth_16 | 广告素材比例检查 | ad | hybrid | critical | startup_ad | ["startup_ad"] | 广告媒体在视觉上保持正确的长宽比例； |
| alioth_17 | 广告内容与落地页一致性检查 | ad | hybrid | critical | startup_ad | ["startup_ad"] | 广告内容与广告落地页应内容一致；点击应前往广告落地页； |
| alioth_18 | 广告倒计时准确性检查 | ad | rule_engine | high | startup_ad | ["content_detail","video_detail","startup_ad"] | 页面存在可识别的广告倒计时数字即通过；未识别到倒计时数字时证据不足。 |
| alioth_19 | 跳过按钮可点击区域检查 | ad | rule_engine | medium | startup_ad | ["content_detail","video_detail","startup_ad"] | 允许跳过时，跳过按钮应一次点中，可点击区域应覆盖全部可点击的区域，可点击区域>=UI线框圈定的区域； |
| alioth_20 | 弹窗广告关闭行为检查 | ad | rule_engine | medium | home | ["home","activity"] | 弹窗广告操作前后可信截图不一致即通过；缺少前后截图时证据不足。 |
| alioth_21 | 首页弹窗蒙层点击行为检查 | ad | rule_engine | high | home | ["home"] | 首页弹窗蒙层操作前后可信截图不一致即通过；缺少前后截图时证据不足。 |
| alioth_22 | 首页弹窗连续展示流畅性检查 | ad | rule_engine | medium | home | ["home","activity"] | 关闭一个首页弹窗广告时，应立现下一个，不可卡顿、延迟、稍后载入等； |
| alioth_23 | 广告加载失败兜底检查 | ad | hybrid | high | home | ["home","activity"] | 加载失败时应自动关闭或跳过，不得出现空白蒙板阻挡用户操作； |
| alioth_24 | 启动广告虚假跳过按钮检查 | ad | rule_engine | high | startup_ad | ["home","startup_ad"] | 启动广告页后续进入首页即通过；启动广告仍在旅程末端时延后判定，直接进入首页且未出现启动广告时跳过。 |
| alioth_25 | 播放前广告展示检查 | ad | rule_engine | high | video_detail | ["video_detail"] | 视频播放页截图或结构化快照存在广告特征即通过；未识别到广告特征时证据不足。 |
| alioth_26 | 广告失败后正片播放检查 | ad | rule_engine | high | video_detail | ["video_detail"] | 广告结束或手动关闭后，到正片第一帧不超过 1 秒，按时间戳计算。没有广告记不适用。缺少时间戳时证据不足。 |
| alioth_27 | 恶意链接与外跳诱导检查 | input_risk | hybrid | critical | NULL | ["content_detail","video_detail","post_list"] | 用户输入内容不应包含恶意链接、钓鱼链接或诱导用户跳转至第三方平台的内容； |
| alioth_28 | 政治恐怖与人身攻击内容检查 | input_risk | hybrid | critical | post_list | ["post_list"] | 用户输入内容不应涉及红线人、不应涉及政治、不应涉及恐怖行为；不应对任何主体、个体造成人身攻击； |
| alioth_29 | 用户输入防刷与刷屏检查 | input_risk | rule_engine | critical | NULL | ["content_detail","video_detail","post_list"] | 用户输入行为需具备防刷机制，如短时间内连续发布评论、帖子或消息，应进行频率限制或验证码校验；用户输入内容不应出现3次(不含)以上的刷屏行为； |
| alioth_30 | 成人赌博与私人广告内容检查 | input_risk | hybrid | critical | NULL | ["content_detail","video_detail","post_list","post_publish"] | 用户输入内容不应出现裸聊、约炮、赌博、棋牌、成人及各种私人广告行为，除非该功能区允许； |
| alioth_31 | 评论发布与计数同步检查 | input_risk | rule_engine | critical | NULL | ["content_detail","video_detail","post_list"] | 用户发送评论后，界面在列表首位显示新评论，同时对应的计数(如评论数、观看数等)要实时更新，不应数据不增加或刷新后状态回滚等不合理逻辑； |
| alioth_32 | 涉华战争政治民族内容检查 | input_risk | hybrid | critical | NULL | ["video_list","video_detail","post_list","post_publish"] | 用户输入内容不应出现1921年及以后明确提及中国的战争、政治、民族感情的内容，但中国大陆官方发布的新闻、通讯除外； |
| alioth_34 | 恐怖行为支持内容检查 | input_risk | hybrid | critical | NULL | ["content_list","content_detail","video_list","video_detail","profile","post_list","post_publish"] | 用户输入内容不应出现任何支持、变相支持恐怖行为的内容； |
| alioth_38 | 分类标签大小写一致性检查 | text | rule_engine | high | content_list | ["content_list"] | 分类标签文案中的字母大小写应保持统一，不应出现同类标签中部分使用小写、部分使用大写，导致页面文案格式不一致的情况。 |
| alioth_40 | 下架内容前置标识检查 | text | rule_engine | high | content_list | ["content_list","video_list"] | 视频板块列表中已下架/不可下载的内容须在列表项前置标记"已下架"状态标签或直接隐藏该内容，不应存在在用户点击下载后才提示"该作品已下架"，避免无效操作。 |
| alioth_41 | 二维码与网址有效性检查 | interaction | rule_engine | high | profile | ["profile"] | 我的页面/身份凭证卡模块/弹窗模块中展示的二维码和网址应真实有效，支持正常识别、复制，并确保在第三方浏览器中可成功访问，无失效或误导情况。 |
| alioth_42 | 失效活动展示同步检查 | text | rule_engine | high | activity | ["home","content_list","content_detail","video_list","video_detail","post_list","activity"] | 当前活动页面 URL 返回 HTTP 200 即通过；无法取得状态码时证据不足。 |
| alioth_43 | 播放历史进度恢复检查 | player | rule_engine | high | video_detail | ["content_detail","video_detail"] | 用户进入视频播放界面（短剧，小说等连续界面）播放页时，系统应自动定位至最近观看进度（最近播放集数及时间点）；新用户或无历史记录时默认从第1集起始播... |
| alioth_45 | 上传限制与多选规则展示检查 | input_risk | rule_engine | high | post_publish | ["post_publish"] | 发布帖文页面须展示图片/视频上传的最高限制（数量、大小）、支持格式及选择规则；进入相册后须支持多选操作，或明确告知"仅支持单选"，保证发布流程规则清晰。 |
| alioth_46 | 帖子媒体全屏浏览检查 | interaction | rule_engine | medium | post_list | ["post_list"] | 帖子列表中的图片/视频内容须支持点击进入全屏浏览模式，不应仅支持缩略图预览而无法全屏查看。 |
| alioth_48 | 退出登录入口检查 | interaction | rule_engine | high | settings | ["profile","settings"] | 个人中心或设置页面存在明确的退出登录、退出账号或登出入口即通过，不实际点击退出。 |

## 二批规则：自动质检规范 28 条（2026-08-07 追加，默认停用）

- 来源：`自动质检规范-28条能力映射与实现方案-v2.xlsx`（能力映射与 pass 口径）+ `自动质检规范-样本库.xlsx`（规范原文、优先级、监测端）。
- 追加后 CSV 曾为 67 条（首期 39 + 二批 28）。**2026-08-07 产品口径调整后为 62 条**：广告静音(原68)、关闭按钮对比度(77)、关闭按钮误导文案(78)、18岁成人确认(104)、榜单信息完整性(新增26) 共 5 条「暂不做」已从种子移除（对应 `alioth_spec_68/77/78`、`alioth_37`、`alioth_spec_new_26`，需要时从 git 历史找回）；首页红线人(102)注记「未成年人已在全产品体系中禁止」，未成年红线标记(103)按「关键词已不作为未成年人的检测范围」改为纯视觉口径、不再依赖未成年关键词库。
- 二批留存 23 条全部 `status=disabled`，只入库质检标准与判定配置；按落地顺序（几何断言① → 滚动到底采集 → 截断检测②/采集端截断字段 → 进度条拖动 → 词库接入）逐批启用。
- `check_point` = 规范原文；`judge_config.llm_checks` = v2 方案的 pass 口径；数值阈值放 `judge_config.thresholds`（安全边距 10%、底部空白 15%、列对齐 2px、进度条误差 1s 等）。
- `judge_config` 中引用的 `layout_geometry_assertion_handler`（新①）、`text_truncation_detector_tool`（新②）尚未注册；启用前若 Tool 未落地，运行时按「能力 Tool 尚未注册」返回 insufficient，不会报错。

### rule_code 编号方案

- 在线表格已预留玉衡序号的 7 条（本期标「否」，104→alioth_37 已按 08-07 口径移除）：沿用 `alioth_{玉衡序号}`，正好补齐首期空缺号。

| rule_code | 原规范序号 | name | 启用前置 |
| --- | --- | --- | --- |
| alioth_1 | 1 | +44注册能力检查 | 注册 journey 种子配置 |
| alioth_15 | 63 | 封面与内容相符检查 | 质检部判断标准 |
| alioth_33 | 100 | 英国政治内容检查 | 质检部词库 |
| alioth_35 | 102 | 首页红线人内容检查 | 红线人库（未发布，硬前置） |
| alioth_36 | 103 | 未成年红线标记检查 | 纯视觉口径（08-07 起不依赖关键词库）；命中只转人工不自动判不过。广告不做此检查，只在已经探索到的页面上检测，不专门为它探索页面。 |
| alioth_44 | 新增36 | 进度条时间显示检查 | 证据包白名单补 media 键 + 拖动进度条旅程 |
| alioth_47 | 新增8 | 客服入口可用性检查 | 入口存在即可。支付流程从选中档位到拉起支付之间不得出现客服入口或弹窗。 |

- 无玉衡序号的留存 16 条：`alioth_spec_{原规范序号}` / `alioth_spec_new_{N}`，与自动质检规范的「原质检规范序号」一一对应：
  `alioth_spec_17/18/24/27/31/35/37/38/48/72` 与 `alioth_spec_new_4/20/24/25/27/29`
  （`alioth_spec_68/77/78`、`alioth_spec_new_26` 已按 08-07 口径移除）。
  其中 `alioth_spec_37`（弱网占位页，APP 端）与 `alioth_spec_new_25`（多端一致性）为挂起项，仅入库存标准。

## 字段说明

| CSV 字段 | 对应表字段/用途 | 是否建议你重点改 |
| --- | --- | --- |
| `ruleset_code/ruleset_version` | 导入时定位 `qc_ruleset.ruleset_code/version`，不直接写入 `qc_rule` | 一般不用改 |
| `rule_code` | `qc_rule.rule_code`，按 `alioth_` + 在线表格 `序号` 生成 | 可按最终命名规范改 |
| `name` | `qc_rule.name`，规则短标题 | 重点确认 |
| `description` | `qc_rule.description`，规则说明；当前留空 | 按需补 |
| `category` | `qc_rule.category`，枚举 text/visual/interaction/ad/payment/input_risk/player/compliance | 重点确认 |
| `executor_type` | `qc_rule.executor_type`，枚举 llm/rule_engine/hybrid | 重点确认 |
| `severity` | `qc_rule.severity`，规则默认严重级别 | 重点确认 |
| `status` | `qc_rule.status`，规则启用状态 | 一般不用改 |
| `scope/page_type/applicable_page_types_json/url_pattern` | `qc_rule` 规则匹配范围；`page_type` 是主页面类型，`applicable_page_types_json` 是适用页面类型列表 | 重点确认 |
| `steps_json` | `qc_rule.steps`，到达检查点的操作步骤 | 重点确认 |
| `input_contract_json` | `qc_rule.input_contract`，只保留证据输入契约，不保存来源表字段 | 后续可细化 |
| `check_point` | `qc_rule.check_point`，完整检查点和判定依据 | 重点确认 |
| `judge_config_json` | `qc_rule.judge_config`，只保留判定参数、确定性规则、LLM 检查项和工具引用 | 重点确认 |
| `manual_review_policy_json` | `qc_rule.manual_review_policy`，人工复核触发策略 | 后续可细化 |
| `sort_order` | `qc_rule.sort_order`，默认按在线表格序号生成 | 可改排序 |

## 已清理字段

以下在线表格来源字段不再进入规则种子表：`source_original_id`、`source_order_id`、`source_category`、`source_monitor`、`source_rule_type`、`source_priority`、`source_implementation_hint`、`source_expected_automation`、`source_notes`、`source_h5_pages`、`source_screenshot_url`、`created_by`、`channel`。

## 生成文件

- `qc_ruleset` CSV：`alioth_first_ruleset_qc_ruleset_seed.csv`
- `qc_rule` CSV：`alioth_first_ruleset_qc_rule_seed.csv`

## 2026-09-04 对齐质检规范在线表（61 条）

- 依据：规则表 https://docs.google.com/spreadsheets/d/1ovR5KJ-FtfB714OF08vkxsRJ2gdqvtP_8l3UF9vyH1s/edit?gid=0（09-04 定稿 61 条，已删新增26、新增35；原序号 28 保留）。
- 种子 CSV 改为与表一一对应的 61 条，`rule_count` 62 → 61：
  - 移除表里没有的 3 条：`alioth_1` +44注册能力检查、`alioth_35` 首页红线人内容检查、`alioth_36` 未成年红线标记检查；存量库由迁移 `20260904_0084` 停用（执行记录有外键，只停用不删行）。
  - 补回表里有、种子里没有的 `alioth_39` 榜单数据排序合理性检查（0024 迁移曾随 ranking 页型下线，线上已改绑 video_list 并启用，按库内现行定义回种子）。
  - 二批 20 条改为 `status=enabled` 入库：表内实现批次已标 830，线上库也早已全部启用，种子不再默认停用。
  - `alioth_spec_37` 弱网占位页、`alioth_spec_new_25` 多端一致性仍是挂起项（只入库存标准，判定恒不适用），因表内保留而随表启用；要省 token 可单独停用。
- 表内「严重/优先」两列，库内 `severity` 取的是「优先」那一列（P0→critical、P1→high、P2→medium、P3→low），与表一致，无需改动。
  - 新建 `alioth_spec_28` 页面元素对齐与层次感检查（原序号 28，表内保留）：hybrid，对齐走几何断言①（同行/同列边缘偏差 ≤2px），层次感走 VLM，存疑转人工；适用 home/content_list/content_detail/video_list/video_detail/profile/activity。此前只有 24/31/38/新增24 四条分摊覆盖，没有通用规则。

## 2026-09-08 运营1组巡检表新增 8 条

- 依据：运营1组检测表格 https://docs.google.com/spreadsheets/d/1xdBg6rn8VX5z8tgtaVd3RIp39A5X5gA4lM-DCT2gLWc/edit?gid=0 的 8 项人工巡检（域名检测 / 产品访问 / 播放检测 / 支付链路检测 / 拉单测试 / 分享页 / 官方群跳转 / APP 内封面展示）。2026-09-07 按「先查 61 条规则表，有的复用、没有的新写」拆分，8 项各缺一条能直接判的规则，本次新写。
- `rule_code` 用 `alioth_ops_{N}`（N=巡检项顺序），不占玉衡序号；规则表侧需由规则负责人补 8 行后再对齐编号。`rule_count` 61 → 69。
- 全部 `hybrid`，`check_point` 逐字取巡检表「新增」列的规范内容；`category` 按库内既有枚举就近归类（规则表分类「产品启动规范」库内没有对应值，域名取 interaction、双端打开取 visual）。
- 不需要迁移：存量库重播种子即可插入新行（`python scripts/seed_first_ruleset.py`，compose 的 initialize 会自动跑）；重播会更新 ruleset `snapshot_hash`，要等在跑任务收完再做。

| rule_code | name | category | severity | page_type | applicable_page_types_json | 补采方式 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| alioth_ops_1 | 产品域名可访问检查 | interaction | critical | home | ["home","video_list"] | fresh_observation / manual_only | 产品级：按首次到达首页判定；域名彻底打不开时任务启动即失败，规则记证据不足 |
| alioth_ops_2 | 产品双端可打开检查 | visual | critical | home | ["home","video_list"] | fresh_observation / manual_only | H5 任务判 H5 端、App 任务判 App 端；采集完成仍加载/空白视为未渲染 |
| alioth_ops_3 | 内容可播放可阅读检查 | player | critical | video_detail | ["video_detail","content_detail"] | equivalent_state / fresh_observation | 视频靠 media_state（ready_state/paused/current_time）+ 多帧画面变化；漫画靠图片顺序加载与翻页 |
| alioth_ops_4 | 充值入口进入充值页检查 | payment | high | payment | ["payment","profile","content_detail","video_detail"] | equivalent_state / manual_only | 入口点击后到达充值页/会员中心且档位、价格、支付方式齐全 |
| alioth_ops_5 | 充值拉起支付检查 | payment | critical | payment | ["payment"] | manual_only / exact_state | 有副作用（真实下单）：auth_requirement=authenticated，只验证到拉起支付为止 |
| alioth_ops_6 | 分享链接有效性检查 | interaction | high | NULL | ["content_detail","video_detail","profile","activity"] | fresh_observation / manual_only | 分享落地页属外部页面，只留证不计覆盖 |
| alioth_ops_7 | 官方群入口跳转检查 | interaction | high | NULL | ["home","profile","settings","activity"] | fresh_observation / manual_only | 设备未装聊天应用的「未安装」不判 fail（有链接/二维码替代即可），无入口不适用 |
| alioth_ops_8 | 封面加载完整性检查 | visual | high | content_list | ["home","content_list","video_list","post_list","content_detail","video_detail"] | fresh_observation / equivalent_state | 破图/空白封面/占位不消失/失败无兜底判 fail；失败但有缺省图算兜底 |

## 2026-09-11 规则集按期拆分（1 期 40 / 2 期 21 / 3 期瑞升 12）

- 口径：现在开启的 73 条按期拆成三个规则集，互不重叠：
  - `alioth_first_ruleset`「1期质检规则集」40 条 = 在线规则表实现批次 `730`（玉衡序号规则）。
  - `alioth_phase2_ruleset`「2期质检规则集」21 条 = 实现批次 `830` 的 20 条 + `alioth_spec_28` 页面元素对齐与层次感；存量库里已停用的二批旧规则 `alioth_1`（+44 注册）、`alioth_35`（首页红线人）也挪到这里，保持停用（种子里没有这两条）。
  - `alioth_ruisheng_ruleset`「3期瑞升质检规则集」12 条 = 运营 1 组（瑞升产品）巡检拆出的 `alioth_ops_1`～`alioth_ops_11` + `alioth_36` 未成年红线标记。
- 种子：`alioth_first_ruleset_qc_ruleset_seed.csv` 每行一个规则集（3 行），`alioth_first_ruleset_qc_rule_seed.csv` 的 `ruleset_code` 列标归属；`scripts/seed_first_ruleset.py` 按规则集分组校验 `rule_count`、分别算快照哈希，规则换了规则集时按编码把旧行挪过去（`rule.id` 不变）。
- 存量库：迁移 `20260911_0107` 新建两个规则集并按编码挪规则、1 期改名；执行记录按 `rule_id` 关联，历史判定不受影响。
- 执行：任务仍只锁一个规则集。建任务时选多个（或「全部」）→ 自动建一个组合规则集（`source_type=composite`，`member_ruleset_ids` 记成员，名称如「1期质检规则集 + 2期质检规则集 + 3期瑞升质检规则集」），执行链路通过 `qc_ruleset_scope()` 取成员规则集的规则；组合规则集不在规则配置和规则中心里展示。接口不传规则集时默认跑全部开启的规则集；显式传 `ruleset_code` 的老调用方仍只锁那一个。
