import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const ROOT = process.cwd();
const OUTPUT = path.join(ROOT, "docs", "质检平台_规则执行能力映射表.html");
const SOURCE_URL =
  "https://docs.google.com/spreadsheets/d/1u2de4EiuEL-BtRZ_D7cd88otbWOnK525CB4IO5bMkcQ/export?format=csv&gid=0";
const SOURCE_LINK =
  "https://docs.google.com/spreadsheets/d/1u2de4EiuEL-BtRZ_D7cd88otbWOnK525CB4IO5bMkcQ/edit?gid=0#gid=0";

const sourceToRule = new Map([
  ["23", "alioth_2"],
  ["26", "alioth_3"],
  ["30", "alioth_4"],
  ["34", "alioth_5"],
  ["36", "alioth_6"],
  ["40", "alioth_7"],
  ["44", "alioth_8"],
  ["46", "alioth_9"],
  ["49", "alioth_10"],
  ["50", "alioth_11"],
  ["51", "alioth_12"],
  ["53", "alioth_13"],
  ["54", "alioth_14"],
  ["67", "alioth_16"],
  ["69", "alioth_17"],
  ["70", "alioth_18"],
  ["71", "alioth_19"],
  ["73", "alioth_20"],
  ["79", "alioth_21"],
  ["80", "alioth_22"],
  ["81", "alioth_23"],
  ["86", "alioth_24"],
  ["89", "alioth_25"],
  ["90", "alioth_26"],
  ["94", "alioth_27"],
  ["95", "alioth_28"],
  ["96", "alioth_29"],
  ["97", "alioth_30"],
  ["98", "alioth_31"],
  ["99", "alioth_32"],
  ["101", "alioth_34"],
  ["新增12", "alioth_38"],
  ["新增19", "alioth_40"],
  ["新增2", "alioth_41"],
  ["新增23", "alioth_42"],
  ["新增34", "alioth_43"],
  ["新增39", "alioth_45"],
  ["新增5", "alioth_46"],
  ["新增9", "alioth_48"],
]);

const categorySkill = {
  text: "text-rule",
  visual: "visual-rule",
  interaction: "interaction-rule",
  ad: "ad-rule",
  payment: "payment-rule",
  input_risk: "input-risk-rule",
  player: "player-rule",
};

const skillBoundary = {
  text:
    "适用于平台文案、标签与文本语义；Skill 只选择文本采集、词法分析和语义 Judge，不保存规则编号、词表、阈值或例外。",
  visual:
    "适用于已渲染页面的布局、媒体和错误态；Skill 只选择截图、几何与视觉分析 Tool，实际标准来自数据库规则。",
  interaction:
    "适用于链接、按钮、状态变化和媒体预览；Skill 只编排安全单次动作与状态对比，不擅自改变账号或业务数据。",
  ad:
    "适用于广告素材、倒计时、跳过、弹窗、蒙层和广告兜底；Skill 不保存广告规则编号、等待阈值或合并关系。",
  payment:
    "适用于只读价格与权益展示；禁止提交订单、支付、充值或消耗账户资产，比较口径和容差来自数据库。",
  input_risk:
    "适用于已发布 UGC、恶意链接、刷屏和上传规则展示；默认只读，不制造或发布违规内容。",
  player:
    "适用于播放器状态、进度和历史恢复；Skill 选择播放器观测 Tool，时间点、容差和账号前置来自数据库。",
};

const profiles = {
  alioth_2: {
    capability: "文本样式确定性检查 + 语言语义复核",
    precondition: "页面稳定且存在可见平台文案；运营允许的外语内容需由规则例外或页面区域标识。",
    recommended: "hybrid",
    collect: "page_capture_tool；text_style_extractor_tool（新增）；OCR 文本提取",
    judge: "typography_assertion_handler（新增）；semantic_rule_judge_tool（新增）",
    packet: "文本片段、DOM/OCR 来源、元素位置、font-family、字符脚本、页面区域、规则例外。",
    steps: "提取文本及计算样式 → 确定性检查半角/无衬线/简繁混用 → LLM 复核语法、方言和运营例外 → 独立写回。",
    pass: "所有适用文本满足字形、字符和语言要求，或命中数据库明确例外。",
    fail: "给出具体文本片段、元素位置和违反项；不得仅以整页观感判失败。",
    insufficient: "无可见文本、缺少计算样式、OCR 与 DOM 冲突且无法定位。",
    review: "疑似作品名、活动名、方言化运营文案或简繁边界无法确定。",
    account: "无；若文案仅登录后显示则需要只读测试账号。",
    safety: "只读，不点击提交类控件。",
    test: "正例：统一简体无衬线；反例：正文混入繁体/衬线；边界：日文作品名位于允许区域。",
    priority: "P1",
    gap: "当前为纯 LLM，tool_refs 为空；无法读取字体样式，也没有独立语义 Judge，容易把标题外语误报为正文问题。",
    dbChange: "executor_type 改为 hybrid；补 text/style 证据契约、例外区域和两个 tool_refs。",
    batch: "text-static",
  },
  alioth_3: {
    capability: "媒体几何与图像质量检测",
    precondition: "排除广告素材；图片已完成加载，并能取得渲染尺寸或截图区域。",
    recommended: "hybrid",
    collect: "page_capture_tool；media_element_extractor_tool（新增）；multi_frame_capture_tool（新增）",
    judge: "image_quality_analyzer_tool（新增）；visual_rule_judge_tool（新增）",
    packet: "图片自然/渲染尺寸、object-fit、裁切边界、模糊分数、OCR 文字框、人物框、多帧时间戳。",
    steps: "定位非广告图片 → 提取几何和多帧证据 → 计算拉伸/越界/闪烁 → VLM 复核人物失真、遮挡和清晰度。",
    pass: "几何指标达标且视觉复核未发现模糊、变形、越界、文字贴边或异常闪烁。",
    fail: "必须标注具体图片区域、指标或帧序列，并说明违反的单项标准。",
    insufficient: "图片未加载、原始尺寸不可得、截图区域缺失或多帧不足以计算闪烁。",
    review: "艺术化变形、动态特效或裁切是否属于设计意图无法确定。",
    account: "通常无；付费内容封面只观察列表和详情可见部分。",
    safety: "只读；不得为获取图片触发购买或下载。",
    test: "正例：等比清晰封面；反例：人物横向拉伸、标题越界；边界：设计性裁切但核心主体完整。",
    priority: "P1",
    gap: "当前 hybrid 但 tool_refs 为空；没有媒体元素抽取、图像指标或真正 VLM Judge。",
    dbChange: "补媒体证据 schema、图像阈值引用、tool_refs 与视觉 Prompt 版本。",
    batch: "visual-media",
  },
  alioth_4: {
    capability: "上下文纠错",
    precondition: "采集平台自有中文文案；作品名、用户名和 UGC 默认不进入强制错别字判断。",
    recommended: "hybrid",
    collect: "page_capture_tool；visible_text_extractor_tool（新增）",
    judge: "semantic_rule_judge_tool（新增）",
    packet: "文本片段、元素来源、上下文窗口、白名单和运营例外。",
    steps: "文本分区 → LLM 在上下文中逐句判断用字是否正确 → 输出准确片段及建议写法。",
    pass: "未发现经上下文确认的错别字，或候选词位于允许的专有名词区域。",
    fail: "给出原文、建议词、上下文和页面位置。",
    insufficient: "文本来源不明、OCR 置信度低或无法区分平台文案与 UGC。",
    review: "专有名词、网络流行语、产品有意命名或多个正确写法。",
    account: "无；登录后提示文案需要测试账号。",
    safety: "只读。",
    test: "正例：正确使用“登录”；反例：按钮写成“登陆账号”；边界：作品标题有意使用异体字。",
    priority: "P2",
    gap: "当前纯 LLM 且无词典 Tool，整页自由判断可重复性低，也没有平台文案与 UGC 分区。",
    dbChange: "executor_type 改为 hybrid；配置词典版本、白名单来源、文本区域和 Prompt。",
    batch: "text-static",
  },
  alioth_5: {
    capability: "布局空洞检测 + 兜底状态语义复核",
    precondition: "页面已结束首屏加载；骨架屏、懒加载区域和业务允许的空状态必须可识别。",
    recommended: "hybrid",
    collect: "page_capture_tool；layout_region_extractor_tool（新增）；network_console_summary_tool（新增）",
    judge: "blank_module_detector_tool（新增）；visual_rule_judge_tool（新增）",
    packet: "模块边界、可见子元素、空白面积、加载状态、接口/控制台摘要、截图区域、缺省态标识。",
    steps: "分割页面模块 → 计算异常空白和残缺结构 → 结合网络/加载状态 → VLM 区分缺省态、设计留白和故障空白。",
    pass: "无异常空模块；无内容区域被隐藏或展示完整、友好的缺省态。",
    fail: "发现残缺标题/边框、死白、全黑或错误导致的内容空洞，并能定位模块。",
    insufficient: "页面仍在加载、无限滚动尚未稳定、模块边界无法形成或截图不完整。",
    review: "大面积设计留白与内容缺失无法区分，或后端明确返回合法空数据。",
    account: "视页面而定；只读。",
    safety: "不通过刷新风暴或故障注入制造空白。",
    test: "正例：空列表显示缺省图；反例：标题下整块死白；边界：首屏骨架屏尚未结束。",
    priority: "P0",
    gap: "当前 hybrid 无布局分析 Tool 和独立视觉 Judge；页面识别失败时容易被错误地当成规则失败。",
    dbChange: "补页面稳定门禁、模块几何 schema、加载状态和视觉 Prompt。",
    batch: "visual-layout",
  },
  alioth_6: {
    capability: "可信可见文本正则扫描",
    precondition: "页面采集已持久化 visible_text 证据；证据必须属于当前 task/page_capture。",
    recommended: "rule_engine",
    collect: "page_capture_tool；trusted_visible_text_reader（已有，需补空文本回退）",
    judge: "rule_judge_tool → technical_error_text Handler（已有）",
    packet: "可信 visible_text artifact、URL、页面区域、命中模式和原始片段。",
    steps: "校验证据归属 → 读取可见文本原文 → 白名单正则扫描 → 记录命中片段。",
    pass: "可信文本未命中配置的技术错误模式。",
    fail: "命中配置模式，并保留页面原始片段；不得扫描模型总结。",
    insufficient: "visible_text 产物为空、损坏、归属不符或页面只有截图且 OCR 未完成。",
    review: "“500”等普通业务数字与错误码上下文无法区分。",
    account: "无。",
    safety: "只读。",
    test: "正例：页面包含普通商品编号500；反例：显示 Internal Server Error；边界：模型摘要提及 HTTP 500 但页面未出现。",
    priority: "P0",
    gap: "Handler 已接入，但真实任务出现 visible_text 为空导致 TRUSTED_EVIDENCE_INVALID；证据提取链尚未闭环。",
    dbChange: "不改执行器；补 input_contract 的文本回退、上下文边界和 evidence quality gate。",
    batch: "deterministic-text",
  },
  alioth_7: {
    capability: "外链分类与新窗口行为验证",
    precondition: "元素被识别为外部链接；点击不会提交表单、支付或改变账号数据。",
    recommended: "rule_engine",
    collect: "page_capture_tool；link_inventory_tool（新增）；safe_click_observer_tool（新增）",
    judge: "external_navigation_handler（新增）",
    packet: "源 URL、目标 URL、元素选择器、target/rel、点击前后 tab 列表、当前页 URL 变化。",
    steps: "识别外链 → 优先检查 DOM 属性 → 不确定时执行一次安全点击 → 比较新标签页和当前 URL。",
    pass: "外链在新窗口/新标签打开，当前产品页保持原位置。",
    fail: "外链覆盖当前产品页，或点击未到达声明的外部目标。",
    insufficient: "链接被脚本动态劫持且点击受安全策略阻断，或目标无法归类内外部。",
    review: "站内统一域名跳转、App WebView 与系统浏览器边界无法确定。",
    account: "通常无。",
    safety: "只允许安全外链单击；禁止下载、支付和深链唤起。",
    test: "正例：广告在新 tab 打开；反例：当前页被替换；边界：同主域不同子域。",
    priority: "P1",
    gap: "当前标记 hybrid 且 tool_refs 为空；实际上核心结果可确定性观测，Main Agent 批量写回不可靠。",
    dbChange: "executor_type 改为 rule_engine；新增外链分类、点击策略和 Handler。",
    batch: "interaction-navigation",
  },
  alioth_8: {
    capability: "交互即时反馈与持久化状态复核",
    precondition: "具备专用测试账号；目标按钮可逆且不会产生付费、关注骚扰或不可清理数据。",
    recommended: "hybrid",
    collect: "page_capture_tool；state_snapshot_tool（新增）；network_event_summary_tool（新增）",
    judge: "interaction_state_diff_tool（新增）；semantic_rule_judge_tool（新增）",
    packet: "操作前/后/刷新后元素状态、计数、请求结果、账号标识、动作选择器。",
    steps: "记录初始状态 → 执行一次可逆动作 → 检查即时反馈 → 刷新/返回复核持久化 → 必要时恢复初始状态。",
    pass: "即时状态、计数和刷新后状态一致，且网络响应成功。",
    fail: "无反馈、状态回滚、显示与服务端响应矛盾或切页后丢失。",
    insufficient: "未登录、动作不可逆、接口被限流或无法确认初始状态。",
    review: "产品采用延迟一致性且规则未提供允许时延。",
    account: "必需；使用隔离测试账号并记录清理动作。",
    safety: "只执行可逆点赞/收藏/关注；完成后尽量恢复。",
    test: "正例：点赞后刷新仍为已点赞；反例：切页返回后回滚；边界：接口成功但 UI 延迟。",
    priority: "P1",
    gap: "没有状态快照和交互 Judge；当前 hybrid 结果依赖模型根据一次截图猜测。",
    dbChange: "补 journey steps、账号前置、允许时延、清理策略和 tool_refs。",
    batch: "interaction-state",
  },
  alioth_9: {
    capability: "数据变更后的 UI/网络状态差异断言",
    precondition: "数据库规则明确目标动作、变化字段与允许刷新方式；动作可在测试账号安全执行。",
    recommended: "rule_engine",
    collect: "page_capture_tool；state_snapshot_tool（新增）；network_event_summary_tool（新增）",
    judge: "data_refresh_handler（新增）",
    packet: "操作前后 DOM 值、请求响应、局部/整页刷新事件、允许时延。",
    steps: "保存基线 → 执行一次动作 → 监听请求与 DOM → 在规则时限内断言目标字段更新。",
    pass: "数据在允许时限内局部或整页更新，无需退出重进。",
    fail: "请求成功但 UI 不更新，或必须退出重进才能看到变化。",
    insufficient: "动作前置不满足、服务端失败、变化字段未在规则中声明。",
    review: "异步任务型业务允许较长刷新时间但数据库未配置时限。",
    account: "多数场景必需；使用可清理测试数据。",
    safety: "禁止付款、删除和不可逆发布。",
    test: "正例：收藏计数即时增加；反例：退出重进才更新；边界：网络失败时应归执行阻断而非规则失败。",
    priority: "P1",
    gap: "当前 hybrid 无任何 tool_refs；规则其实需要可复现状态差异 Handler。",
    dbChange: "executor_type 改为 rule_engine；补目标字段、时限、动作与恢复步骤。",
    batch: "interaction-state",
  },
  alioth_10: {
    capability: "提示文案可理解性语义评审",
    precondition: "准确捕获实际 toast/提示文本及触发上下文；文本属于平台提示而非 UGC。",
    recommended: "llm",
    collect: "page_capture_tool；transient_message_capture_tool（新增）",
    judge: "semantic_rule_judge_tool（新增）",
    packet: "提示原文、触发动作、页面区域、用户角色、数据库提供的语言准则与示例。",
    steps: "执行允许的最小触发 → 捕获提示 → 基于规则准则做单条语义判断 → 引用原文写回。",
    pass: "普通用户可理解，语气自然且明确说明下一步。",
    fail: "存在内部术语、责备语气、歧义或无法指导用户的表达，并给出具体原因。",
    insufficient: "未捕获提示、触发上下文缺失或文本被截断。",
    review: "品牌语气、行业术语或目标用户群需要业务确认。",
    account: "取决于触发步骤；只允许测试账号。",
    safety: "不为触发提示提交真实交易或违规内容。",
    test: "正例：“注册后即可关注”；反例：“仅注册用户才能关注”；边界：专业功能面向专业用户。",
    priority: "P1",
    gap: "当前纯 LLM 但没有独立 Judge，也缺瞬态提示采集；Main Agent 容易拿整页文本替代目标提示。",
    dbChange: "补 prompt_ref、触发步骤、目标文本 selector 和 manual_review_policy。",
    batch: "text-semantic",
  },
  alioth_11: {
    capability: "正向/反向提示语义分类",
    precondition: "捕获条件不足、兑换或权限类提示及其业务上下文。",
    recommended: "llm",
    collect: "page_capture_tool；transient_message_capture_tool（新增）",
    judge: "semantic_rule_judge_tool（新增）",
    packet: "提示原文、触发原因、目标行为、规则正反例和页面位置。",
    steps: "捕获提示 → 判断是否以可行动的正向表达为主 → 输出原文、分类和理由。",
    pass: "提示说明如何达成目标或下一步行动，不以单纯否定责备用户。",
    fail: "仅表达“不够、不能、失败”等消极结果且无可行动信息。",
    insufficient: "提示文本或触发原因缺失。",
    review: "安全、合规或不可恢复错误必须使用阻断性表达。",
    account: "可能需要积分不足的测试账号。",
    safety: "不兑换、不消费积分。",
    test: "正例：“再获得500积分即可兑换”；反例：“积分不够不能兑换”；边界：账户冻结提示。",
    priority: "P1",
    gap: "当前没有目标提示提取和独立语义 Judge，容易对页面普通说明文案误判。",
    dbChange: "补 prompt_ref、触发条件、业务例外和目标文本来源。",
    batch: "text-semantic",
  },
  alioth_12: {
    capability: "标点确定性扫描 + 情绪语境例外复核",
    precondition: "捕获真实短提示；数据库定义允许保留叹号的喜悦场景。",
    recommended: "hybrid",
    collect: "page_capture_tool；transient_message_capture_tool（新增）",
    judge: "punctuation_assertion_handler（新增）；semantic_rule_judge_tool（新增）",
    packet: "提示原文、末尾标点、触发动作、页面语境、允许例外。",
    steps: "确定性检测叹号结尾 → 未命中直接通过 → 命中时由 LLM 只判断是否属于喜悦例外。",
    pass: "未以叹号结尾，或数据库允许的喜悦语境成立。",
    fail: "非喜悦提示以叹号结尾，并引用完整短提示。",
    insufficient: "提示被截断或缺少触发语境，无法判断例外。",
    review: "营销庆祝、任务完成与责备语气边界不清。",
    account: "视触发步骤而定。",
    safety: "只执行最小可逆触发。",
    test: "正例：“领取成功！”；反例：“积分不足！”；边界：“操作完成！”是否属于喜悦需规则说明。",
    priority: "P1",
    gap: "当前被配置为纯 LLM；可确定部分未下沉，且没有独立语义 Judge 复核例外。",
    dbChange: "executor_type 改为 hybrid；补标点 Handler、例外 Prompt 和提示证据契约。",
    batch: "text-semantic",
  },
  alioth_13: {
    capability: "跨页面商品身份与价格一致性校验",
    precondition: "同一商品可通过商品 ID、权益名、套餐周期等稳定标识关联；比较条件一致。",
    recommended: "hybrid",
    collect: "page_capture_tool；product_price_extractor_tool（新增）；cross_page_fact_store（新增）",
    judge: "price_normalization_tool（新增）；semantic_rule_judge_tool（新增）",
    packet: "商品身份、价格值、币种、单位、周期、会员态、页面位置、采集时间和来源页面。",
    steps: "多页面提取商品事实 → 归一币种/周期/账号态 → 确定性比较 → LLM 仅复核商品是否真正同一。",
    pass: "同一条件下同一商品所有展示价格一致。",
    fail: "稳定关联到同一商品但价格值不一致，并列出两处证据。",
    insufficient: "缺少商品标识、币种/周期不同、账号态不同或只采到一处价格。",
    review: "套餐权益不同、动态促销或地区/渠道定价无法自动归一。",
    account: "通常需要会员/非会员测试账号，但全程只读。",
    safety: "禁止进入提交订单和支付确认。",
    test: "正例：详情与会员中心同价；反例：月卡同条件两处价格不同；边界：月卡与季卡不可直接比较。",
    priority: "P1",
    gap: "当前 hybrid tool_refs 为空；没有商品实体关联、价格抽取和跨页事实存储。",
    dbChange: "补商品 identity contract、归一规则、采集页面集合和 tool_refs。",
    batch: "payment-price",
  },
  alioth_14: {
    capability: "优惠价与原价数值比较",
    precondition: "从同一商品、同一币种和单位中抽取优惠价与原价。",
    recommended: "rule_engine",
    collect: "page_capture_tool；product_price_extractor_tool（新增）",
    judge: "rule_judge_tool → discount_price_compare Handler（已有）",
    packet: "discount_price、original_price、币种、单位、商品身份、元素位置。",
    steps: "抽取并归一两类价格 → 调用已有 Handler 执行小于比较 → 写回原始值与比较结果。",
    pass: "优惠价严格低于原价。",
    fail: "优惠价大于或等于原价，并保留两个原始展示值。",
    insufficient: "任一价格缺失、无法解析、币种/单位不一致或商品身份不一致。",
    review: "折后权益不同、价格区间或“起”字价格无法直接比较。",
    account: "可能需要会员测试账号；只读。",
    safety: "禁止支付。",
    test: "正例：9.9<19.9；反例：优惠价等于原价；边界：¥9.9起与固定原价。",
    priority: "P0",
    gap: "Handler 已存在，但 Agent 需要自行填 discount_price/original_price；缺少可信价格抽取器时会长期阻断。",
    dbChange: "不改执行器；补 price extractor、币种单位校验和 trusted input 来源。",
    batch: "deterministic-price",
  },
  alioth_16: {
    capability: "广告素材比例与视觉失真检测",
    precondition: "已定位广告素材区域，且能区分素材本身与容器裁切/装饰。",
    recommended: "hybrid",
    collect: "page_capture_tool；ad_region_detector_tool（新增）；media_element_extractor_tool（新增）",
    judge: "image_geometry_tool（新增）；visual_rule_judge_tool（新增）",
    packet: "广告区域、自然/渲染尺寸、裁切模式、OCR 文字框、人物框和截图。",
    steps: "识别广告素材 → 计算长宽比和拉伸 → VLM 复核人物、文字、号码是否变形。",
    pass: "素材保持合理比例，关键内容无明显拉伸或畸变。",
    fail: "素材、人物或文字明显失真，并定位广告区域。",
    insufficient: "广告背景与素材边界无法识别，或图片仍在加载。",
    review: "夸张艺术风格、透视设计或动画过渡帧。",
    account: "无。",
    safety: "只读，不点击广告。",
    test: "正例：等比缩放；反例：人物被横向压扁；边界：原图本身为夸张插画。",
    priority: "P1",
    gap: "当前 hybrid 无广告区域检测、几何 Tool 和视觉 Judge。",
    dbChange: "补广告区域证据、几何阈值引用和视觉 Prompt。",
    batch: "ad-visual",
  },
  alioth_17: {
    capability: "广告跳转可达性 + 主题一致性复核",
    precondition: "广告可安全单击，落地页不涉及下载、支付或系统深链。",
    recommended: "hybrid",
    collect: "page_capture_tool；ad_region_detector_tool（新增）；safe_click_observer_tool（新增）",
    judge: "landing_page_probe_tool（新增）；visual_semantic_compare_tool（新增）",
    packet: "广告截图/OCR/链接、点击结果、落地页 URL、首屏截图/OCR、重定向链。",
    steps: "采集广告内容 → 单击并记录落地页 → 校验可达性 → VLM/LLM 比较主题、品牌和承诺一致性。",
    pass: "点击到达有效落地页，主体内容与广告宣传一致。",
    fail: "无跳转、错误落地页、无法访问或主题明显不一致。",
    insufficient: "落地页受登录/地区限制，或深链被安全策略阻断。",
    review: "聚合活动页包含多商品，主题一致但并非单一商品详情。",
    account: "通常无。",
    safety: "禁止下载 APK、唤起外部 App、提交表单或支付。",
    test: "正例：会员广告进入会员页；反例：游戏广告进入无关博彩页；边界：品牌首页。",
    priority: "P1",
    gap: "当前没有点击追踪、重定向链和跨图语义 Judge。",
    dbChange: "补安全点击策略、允许域名、落地页证据和语义 Prompt。",
    batch: "ad-landing",
  },
  alioth_18: {
    capability: "倒计时显示值与单调时钟比较",
    precondition: "倒计时元素可定位且显示连续数字；页面处于前台，无系统休眠。",
    recommended: "rule_engine",
    collect: "multi_frame_capture_tool（新增）；monotonic_timer_tool（新增）",
    judge: "countdown_accuracy_handler（新增）",
    packet: "每帧显示值、单调时间戳、元素位置、页面可见状态和允许误差。",
    steps: "定时采样多帧 → OCR/DOM 读取显示值 → 计算真实间隔与显示差 → 检查卡顿和偷时。",
    pass: "显示递减与真实时间在数据库允许误差内一致。",
    fail: "显示停滞、跳秒、加速或倒计时结束时间明显不一致。",
    insufficient: "元素无法稳定读取、页面失焦或采样帧不足。",
    review: "后台标签页节流、网络恢复或系统性能抖动影响采样。",
    account: "无。",
    safety: "只观察，不点击跳过。",
    test: "正例：5秒倒计时约5秒结束；反例：显示5秒实际8秒；边界：浏览器切后台。",
    priority: "P0",
    gap: "当前 hybrid 且 tool_refs 为空；时间规则不应交由截图语义猜测。",
    dbChange: "executor_type 改为 rule_engine；补采样频率、允许误差和 Handler。",
    batch: "ad-timing",
  },
  alioth_19: {
    capability: "控件视觉区域与真实命中区域验证",
    precondition: "跳过按钮已可用；规则允许安全点击且点击不会产生额外业务副作用。",
    recommended: "rule_engine",
    collect: "page_capture_tool；element_geometry_tool（新增）；safe_hit_test_tool（新增）",
    judge: "clickable_area_handler（新增）",
    packet: "视觉边界、DOM hitbox、采样点击点、命中元素、点击结果和截图。",
    steps: "定位跳过控件 → 比较视觉框与 hitbox → 在代表性边缘点做安全 hit-test/单击 → 验证一次生效。",
    pass: "真实可点击区域覆盖视觉按钮，且一次点击完成跳过。",
    fail: "视觉区域存在不可点击部分、需多次点击或命中广告内容。",
    insufficient: "按钮尚不可用、遮罩拦截或无法安全执行点击。",
    review: "透明扩展热区、动画中的按钮位置变化。",
    account: "无。",
    safety: "仅点击明确跳过控件，不点击广告主体。",
    test: "正例：按钮边缘可点击；反例：文字可见但仅中心小点响应；边界：按钮动画移动。",
    priority: "P0",
    gap: "当前没有 hit-test、几何对比和专用 Handler。",
    dbChange: "executor_type 改为 rule_engine；补控件选择器、采样策略和一次点击预期。",
    batch: "ad-control",
  },
  alioth_20: {
    capability: "广告弹窗真实关闭动作与页面恢复验证",
    precondition: "页面存在可见广告弹窗；必须先识别真实关闭控件，找不到时才允许点击弹窗外遮罩。",
    recommended: "rule_engine",
    collect: "page_capture_tool；ad_modal_detector_tool（新增）；safe_click_observer_tool（新增）",
    judge: "popup_close_behavior_handler（新增）",
    packet: "弹窗/遮罩边界、广告判定事实、关闭目标、点击坐标、前后弹窗状态、URL 与可操作元素。",
    steps: "确认广告弹窗 → 选择真实关闭按钮；无按钮时选 Dialog 外遮罩点 → 单击一次 → 验证弹窗消失且底层页恢复。",
    pass: "广告弹窗关闭，未误跳广告，底层页面恢复可操作。",
    fail: "关闭目标无效、误跳广告、遮罩残留或底层页面仍被阻断。",
    insufficient: "无法确认弹窗是否广告，或没有任何安全关闭路径。",
    review: "关闭控件高度伪装、多个重叠弹窗或弹窗关闭后立即被新弹窗替代。",
    account: "无。",
    safety: "禁止默认点击遮罩中心；遮罩点击必须位于 Dialog 外侧。",
    test: "正例：点击X后弹窗消失；反例：点击“关闭”实际进入广告；边界：无X但支持外侧遮罩关闭。",
    priority: "P0",
    gap: "当前页面识别已有部分关闭逻辑，但规则层没有独立前后状态 Handler，仍由 hybrid 自由写回。",
    dbChange: "executor_type 改为 rule_engine；补 popup facts、点击优先级和前后状态断言。",
    batch: "ad-popup",
  },
  alioth_21: {
    capability: "蒙层外侧点击关闭与防误跳验证",
    precondition: "存在广告 Dialog 与独立遮罩；可计算 Dialog 外的安全点击点。",
    recommended: "rule_engine",
    collect: "page_capture_tool；ad_modal_detector_tool（新增）；safe_click_observer_tool（新增）",
    judge: "overlay_dismissal_handler（新增）",
    packet: "Dialog/遮罩几何、选定外侧点、点击前后 URL/tab、弹窗状态和广告跳转事件。",
    steps: "识别 Dialog 外遮罩区域 → 选择不在元素中心的安全点 → 单击 → 验证关闭且无广告导航。",
    pass: "外侧蒙层点击关闭广告，且未进入广告落地页。",
    fail: "外侧点击进入广告、无响应或遮罩继续阻断。",
    insufficient: "遮罩没有可见外侧区域，或页面结构无法区分 Dialog 与广告主体。",
    review: "产品明确要求蒙层不可关闭但数据库规则未表达例外。",
    account: "无。",
    safety: "点击点必须经过元素命中测试，禁止默认点击屏幕中心。",
    test: "正例：点击左上遮罩关闭；反例：点击遮罩跳广告；边界：全屏广告无外侧区域。",
    priority: "P0",
    gap: "当前 hybrid 无遮罩几何证据和确定性行为 Judge；页面探索动作也不能替代规则判定。",
    dbChange: "executor_type 改为 rule_engine；补遮罩安全点算法和导航断言。",
    batch: "ad-popup",
  },
  alioth_22: {
    capability: "连续广告序列与切换时延检测",
    precondition: "当前确实存在多个连续首页广告；规则提供允许切换时延。",
    recommended: "rule_engine",
    collect: "page_capture_tool；ad_modal_detector_tool（新增）；monotonic_timer_tool（新增）",
    judge: "popup_sequence_handler（新增）",
    packet: "每个弹窗指纹、关闭时间、下一弹窗出现时间、遮罩状态、底层页面可操作性。",
    steps: "识别首个弹窗 → 安全关闭 → 计时等待下一弹窗或页面恢复 → 对序列逐项记录。",
    pass: "关闭后在允许时延内出现下一广告，或序列结束后底层页立即恢复。",
    fail: "长时间空白/卡住、下一广告迟到、遮罩残留或页面不可操作。",
    insufficient: "无法确认是否配置了下一广告，或网络异常导致广告源不可用。",
    review: "广告配置数量未知，无法区分“序列结束”与“下一广告未加载”。",
    account: "无。",
    safety: "仅关闭广告，不点击广告主体。",
    test: "正例：关闭A后立即出现B；反例：关闭A后遮罩卡住；边界：实际只配置一个广告。",
    priority: "P0",
    gap: "当前缺少广告序列事实、计时器和“序列结束”配置，模型会在同一视频列表页反复猜测。",
    dbChange: "executor_type 改为 rule_engine；补序列数量来源、时延和结束条件。",
    batch: "ad-popup",
  },
  alioth_23: {
    capability: "广告资源失败后的遮挡与兜底检查",
    precondition: "只能在隔离环境或允许故障注入时制造广告失败；生产巡检仅观察自然失败。",
    recommended: "hybrid",
    collect: "page_capture_tool；network_fault_injection_tool（新增，隔离环境）；ad_modal_detector_tool（新增）",
    judge: "fallback_state_assertion_tool（新增）；visual_rule_judge_tool（新增）",
    packet: "广告请求结果、故障注入说明、遮罩状态、空白面积、底层页可操作性和截图。",
    steps: "观察或安全注入广告失败 → 等待兜底时限 → 断言弹窗/遮罩关闭 → VLM 复核是否残留空白阻挡。",
    pass: "失败后广告自动关闭/跳过，底层页面可操作。",
    fail: "空白蒙层、死弹窗或加载失败元素持续阻挡。",
    insufficient: "无法制造或确认广告失败，且没有自然失败证据。",
    review: "广告请求重试中，规则未给出最长等待时间。",
    account: "无。",
    safety: "故障注入只允许测试环境；生产不得修改网络响应。",
    test: "正例：广告404后关闭；反例：全屏空白遮罩；边界：短暂重试后成功。",
    priority: "P0",
    gap: "当前无故障注入、网络证据和兜底 Judge；单张截图无法证明加载失败。",
    dbChange: "补环境策略、故障类型、最长等待、网络与视觉 tool_refs。",
    batch: "ad-fallback",
  },
  alioth_24: {
    capability: "真实/伪造跳过控件辨识",
    precondition: "启动广告已渲染，页面上所有疑似“跳过”元素可枚举。",
    recommended: "hybrid",
    collect: "page_capture_tool；ad_control_inventory_tool（新增）；safe_hit_test_tool（新增）",
    judge: "control_behavior_assertion_tool（新增）；visual_rule_judge_tool（新增）",
    packet: "疑似跳过元素文本/图像、层级、hitbox、点击行为、广告区域和前后状态。",
    steps: "枚举跳过控件 → 视觉识别诱导元素 → 对真实候选执行安全 hit-test/单击 → 验证是否离开广告。",
    pass: "不存在诱导性假跳过，真实跳过控件可一次生效。",
    fail: "视觉上像跳过但点击进入广告，或存在多个混淆性跳过入口。",
    insufficient: "控件尚未激活、倒计时未结束或无法安全点击。",
    review: "装饰文字与真实按钮视觉相近但无明确交互暗示。",
    account: "无。",
    safety: "点击前必须确认候选区域，避免广告主体。",
    test: "正例：唯一跳过按钮生效；反例：假“跳过”图案跳广告；边界：倒计时结束前不可点击。",
    priority: "P1",
    gap: "当前 hybrid 无控件清单、hit-test 和视觉/行为联合 Judge。",
    dbChange: "补控件候选 schema、激活条件、行为工具和视觉 Prompt。",
    batch: "ad-control",
  },
  alioth_25: {
    capability: "播放前广告首帧加载性能测量",
    precondition: "网络环境满足规则基线；进入播放器动作和广告首帧事件可观测。",
    recommended: "rule_engine",
    collect: "page_capture_tool；performance_timeline_tool（新增）；network_event_summary_tool（新增）",
    judge: "ad_first_frame_latency_handler（新增）",
    packet: "导航/点击时间、广告请求、首帧时间、网络基线、设备环境和阈值引用。",
    steps: "记录进入播放页时刻 → 监听广告请求与首帧 → 计算加载耗时 → 与数据库阈值比较。",
    pass: "广告首帧在允许时延内呈现，且无明显空白阻断。",
    fail: "在正常网络基线下超过阈值或长期无首帧。",
    insufficient: "网络基线异常、首帧事件不可得或页面未触发广告。",
    review: "第三方广告源临时抖动且无法稳定复现。",
    account: "可能需要可播放内容账号。",
    safety: "只进入播放页，不触发购买。",
    test: "正例：阈值内出现首帧；反例：广告区域长期空白；边界：网络丢包。",
    priority: "P1",
    gap: "当前无 Performance/网络工具，模型仅凭视觉延迟印象无法给出可审计耗时。",
    dbChange: "executor_type 改为 rule_engine；补网络基线、首帧事件、阈值和 Handler。",
    batch: "ad-timing",
  },
  alioth_26: {
    capability: "广告失败后正片播放状态断言",
    precondition: "测试环境允许使广告资源失败；目标正片无需付费解锁。",
    recommended: "rule_engine",
    collect: "network_fault_injection_tool（新增）；player_state_probe_tool（新增）；page_capture_tool",
    judge: "ad_failure_media_fallback_handler（新增）",
    packet: "广告请求失败证据、播放器 readyState/paused/currentTime、首帧、遮罩和允许时限。",
    steps: "制造/捕获广告失败 → 等待兜底 → 探测正片播放器状态 → 断言开始播放且无遮罩。",
    pass: "广告失败后在时限内取消广告并播放正片。",
    fail: "正片未播放、仍停留广告空白或遮罩阻断。",
    insufficient: "无法确认广告失败、正片本身不可播放或需要付费。",
    review: "正片源同时失败，无法归因广告兜底。",
    account: "可能需要普通播放账号。",
    safety: "故障注入仅测试环境；不绕过付费权限。",
    test: "正例：广告404后正片播放；反例：一直黑屏；边界：正片接口也失败。",
    priority: "P0",
    gap: "当前没有故障证据、播放器状态探针和专用 Handler。",
    dbChange: "executor_type 改为 rule_engine；补测试环境约束、播放器字段和兜底时限。",
    batch: "ad-fallback",
  },
  alioth_27: {
    capability: "链接风险检测 + 诱导语义复核",
    precondition: "只扫描已发布内容；不得主动访问已知恶意地址的页面正文。",
    recommended: "hybrid",
    collect: "page_capture_tool；ugc_link_extractor_tool（新增）",
    judge: "safe_url_reputation_tool（新增）；semantic_rule_judge_tool（新增）",
    packet: "文本/二维码 URL、展示位置、域名信誉、重定向摘要、邻近诱导文案、白名单。",
    steps: "提取 URL/二维码 → 安全信誉与结构检查 → LLM 判断站外诱导语义 → 按单个内容项输出。",
    pass: "无恶意/钓鱼链接，或链接位于规则允许的白名单。",
    fail: "链接风险明确或邻近文案诱导用户跳转第三方，保留域名和上下文。",
    insufficient: "短链无法安全展开、信誉服务不可用或内容截图无法解码。",
    review: "新域名无信誉数据、正常社交分享与引流边界不清。",
    account: "可能需要登录后只读访问评论/帖子。",
    safety: "不在真实浏览器打开高风险 URL，不输入或发布测试恶意链接。",
    test: "正例：官方白名单链接；反例：仿冒登录短链；边界：个人主页正常社交链接。",
    priority: "P1",
    gap: "当前 sensitive/link tool_refs 为空，也没有语义 Judge；无法证明恶意性或诱导上下文。",
    dbChange: "补 URL 风险来源、白名单、短链策略和语义 Prompt。",
    batch: "input-link",
  },
  alioth_28: {
    capability: "多类别内容安全分类与上下文复核",
    precondition: "扫描已发布内容；规则提供禁止类别、地域和白名单口径。",
    recommended: "hybrid",
    collect: "page_capture_tool；ugc_content_extractor_tool（新增）",
    judge: "sensitive_word_detection_tool；content_safety_judge_tool（新增）",
    packet: "内容文本/OCR、作者/区域、词库命中、上下文、内容类别、白名单版本。",
    steps: "按内容项提取 → 词库和分类器初筛 → LLM 按规则上下文复核政治/恐怖/人身攻击 → 独立写回。",
    pass: "未命中禁止类别，或命中词在合法上下文/白名单中。",
    fail: "明确命中规则禁止类别，并引用最小必要片段与位置。",
    insufficient: "OCR 低置信、内容被截断、语言无法识别或规则口径缺失。",
    review: "新闻引用、批判性讨论、历史教育和人身攻击边界。",
    account: "只读测试账号。",
    safety: "不得制造、输入、复制或发布违规内容；报告中最小化敏感原文。",
    test: "正例：合法新闻引用；反例：明确攻击/恐怖宣扬；边界：历史讨论。",
    priority: "P0",
    gap: "当前 hybrid tool_refs 为空；没有词库版本、内容项边界或独立安全 Judge。",
    dbChange: "补禁止类别、词库/模型版本、最小引用和强制人审策略。",
    batch: "input-safety",
  },
  alioth_29: {
    capability: "重复内容计数与防滥用结果观测",
    precondition: "默认只分析已发布内容；主动频控测试仅限隔离账号和专用环境。",
    recommended: "rule_engine",
    collect: "page_capture_tool；ugc_sequence_extractor_tool（新增）；rate_limit_observer_tool（新增）",
    judge: "spam_frequency_handler（新增）",
    packet: "内容指纹、作者、时间、连续次数、页面区域、频控提示和测试环境标识。",
    steps: "对已发布内容归一化并计数 → 检查同主体连续刷屏 → 若允许，执行隔离频控用例并观察限制结果。",
    pass: "未超过数据库阈值，或系统在阈值处正确限流/验证码。",
    fail: "同主体超过阈值仍可连续发布，或页面已出现明确刷屏。",
    insufficient: "缺少作者/时间、列表分页不完整，或不允许主动频控测试。",
    review: "模板化合法内容与恶意刷屏难以区分。",
    account: "主动测试必需隔离账号；默认只读无需。",
    safety: "生产巡检禁止连续发帖/评论制造刷屏。",
    test: "正例：第三次触发限流；反例：连续重复发布无限制；边界：不同作者相同短语。",
    priority: "P1",
    gap: "当前 hybrid 无计数/频控 Tool；规则同时包含结果扫描和主动机制测试，需要拆分运行模式。",
    dbChange: "executor_type 改为 rule_engine；补阈值、主体键、只读/主动模式和环境策略。",
    batch: "input-frequency",
  },
  alioth_30: {
    capability: "成人、赌博与私人广告内容安全复核",
    precondition: "只扫描已发布内容；功能区允许的例外必须由数据库明确。",
    recommended: "hybrid",
    collect: "page_capture_tool；ugc_content_extractor_tool（新增）",
    judge: "sensitive_word_detection_tool；content_safety_judge_tool（新增）",
    packet: "内容文本/OCR/缩略图引用、词库命中、区域类型、允许例外和白名单。",
    steps: "逐内容项提取 → 词库/图文分类初筛 → LLM 按功能区例外复核 → 高风险命中强制人审。",
    pass: "无成人、赌博、私人广告等禁止内容，或明确属于允许功能区。",
    fail: "命中禁止类别且上下文成立，保留最小证据。",
    insufficient: "图片内容不可识别、文本被遮挡或功能区例外未配置。",
    review: "擦边内容、棋牌娱乐与赌博、正常联系方式与私人广告边界。",
    account: "只读测试账号。",
    safety: "不生成或发布测试违规内容；敏感截图受控存储。",
    test: "正例：普通棋牌游戏介绍；反例：下注引流广告；边界：艺术人体内容。",
    priority: "P0",
    gap: "当前无词库/多模态分类 Tool 和人审门禁，模型自由输出风险高。",
    dbChange: "补类别体系、功能区例外、证据脱敏和强制复核策略。",
    batch: "input-safety",
  },
  alioth_31: {
    capability: "评论发布、列表位置与计数一致性验证",
    precondition: "具备隔离测试账号和可删除测试评论；规则明确目标评论区。",
    recommended: "rule_engine",
    collect: "page_capture_tool；state_snapshot_tool（新增）；network_event_summary_tool（新增）",
    judge: "comment_state_sync_handler（新增）",
    packet: "测试评论标识、发布响应、列表首项、评论计数、刷新后状态和清理结果。",
    steps: "记录计数 → 发布唯一测试评论 → 验证列表与计数 → 刷新复核 → 删除测试评论并记录清理。",
    pass: "评论立即显示在预期位置，计数正确更新且刷新后不回滚。",
    fail: "发布成功但列表/计数不变、显示位置错误或刷新后消失。",
    insufficient: "无账号、发布被限流、评论审核中或无法安全清理。",
    review: "产品采用审核后展示，但规则未声明审核流程。",
    account: "必需隔离账号和可清理测试内容。",
    safety: "测试文本必须无害、唯一且完成后删除；禁止在真实用户内容下扰动。",
    test: "正例：发布后首位显示且+1；反例：刷新后回滚；边界：评论需审核。",
    priority: "P1",
    gap: "当前 hybrid 无状态 Handler；真实发布属于状态变更，不能由通用页面探索随意执行。",
    dbChange: "executor_type 改为 rule_engine；补测试账号、评论模板、审核模式与清理步骤。",
    batch: "interaction-state",
  },
  alioth_32: {
    capability: "涉华战争政治民族内容语义判定",
    precondition: "只扫描已发布内容；规则提供时间范围、官方来源白名单和内容主体口径。",
    recommended: "hybrid",
    collect: "page_capture_tool；ugc_content_extractor_tool（新增）",
    judge: "sensitive_word_detection_tool；content_safety_judge_tool（新增）",
    packet: "内容片段、时间实体、主体实体、来源域、词库命中、上下文和白名单。",
    steps: "实体/时间抽取 → 词库初筛 → LLM 判断是否满足规则组合条件 → 高风险命中人工复核。",
    pass: "未满足禁止组合条件，或属于数据库认可的官方新闻/通讯例外。",
    fail: "时间、涉华主题和禁止语义同时成立，且非白名单来源。",
    insufficient: "来源无法确认、时间指代含糊或上下文不完整。",
    review: "所有拟失败结论原则上强制人审，避免政治语义误判。",
    account: "只读测试账号。",
    safety: "最小化敏感文本展示；不主动搜索、输入或发布相关内容。",
    test: "正例：官方通讯白名单；反例：明确违反组合条件；边界：历史学术讨论。",
    priority: "P0",
    gap: "当前无实体/时间抽取和白名单工具，单靠模型整页判断不可审计。",
    dbChange: "补组合条件、官方来源白名单、实体抽取和强制人审。",
    batch: "input-safety",
  },
  alioth_34: {
    capability: "恐怖行为支持语义分类",
    precondition: "只分析已发布内容；区分支持、报道、批判和学术讨论。",
    recommended: "hybrid",
    collect: "page_capture_tool；ugc_content_extractor_tool（新增）",
    judge: "sensitive_word_detection_tool；content_safety_judge_tool（新增）",
    packet: "内容片段、词库命中、立场/语气分类、内容类型、上下文和证据引用。",
    steps: "内容项抽取 → 敏感词/实体初筛 → LLM 判断是否表达支持或变相支持 → 命中强制人审。",
    pass: "未表达支持，或属于报道、批判、教育等合法上下文。",
    fail: "明确支持、赞扬、招募或合理化恐怖行为。",
    insufficient: "上下文缺失、引用对象不明或图片文字不可识别。",
    review: "所有拟失败结论强制人审。",
    account: "只读测试账号。",
    safety: "不生成、传播或重复完整敏感内容。",
    test: "正例：批判性新闻报道；反例：明确赞扬支持；边界：引用他人言论进行反驳。",
    priority: "P0",
    gap: "当前无安全分类器、立场判断 Prompt 和强制复核闭环。",
    dbChange: "补立场标签、内容类型、脱敏证据和强制人审。",
    batch: "input-safety",
  },
  alioth_38: {
    capability: "同类标签字母大小写集合断言",
    precondition: "页面已识别分类标签集合，且能区分专有品牌词与普通分类词。",
    recommended: "rule_engine",
    collect: "page_capture_tool；label_group_extractor_tool（新增）",
    judge: "label_case_consistency_handler（新增）",
    packet: "标签组 ID、原始文本、归一值、页面位置和品牌白名单。",
    steps: "提取同类标签组 → 对字母片段归一 → 比较大小写模式 → 输出不一致项。",
    pass: "同一标签组大小写模式一致，或命中专有词白名单。",
    fail: "同组出现可归一为同类但大小写模式不一致的标签。",
    insufficient: "无法形成同类标签组，或文本主要来自图片且 OCR 不可靠。",
    review: "品牌词、缩写与普通单词混排。",
    account: "无。",
    safety: "只读。",
    test: "正例：VIP/TV 统一；反例：同组同时出现vip和VIP；边界：iOS与IOS品牌写法。",
    priority: "P2",
    gap: "当前 hybrid tool_refs 为空；这是低成本确定性规则，不需要通用 LLM 批量判断。",
    dbChange: "executor_type 改为 rule_engine；补标签分组、品牌白名单和 Handler。",
    batch: "deterministic-text",
  },
  alioth_40: {
    capability: "下架内容前置状态与点击结果一致性",
    precondition: "列表项可识别；只允许进入详情或触发非下载型检查，不实际下载。",
    recommended: "rule_engine",
    collect: "page_capture_tool；content_item_state_extractor_tool（新增）；safe_click_observer_tool（新增）",
    judge: "offline_content_visibility_handler（新增）",
    packet: "列表项标识、前置状态标签、可见性、详情结果、下载入口状态和前后 URL。",
    steps: "扫描列表状态 → 对疑似不可用项安全进入详情 → 检查是否“点击后才提示下架” → 返回列表。",
    pass: "已下架内容被隐藏或在列表前置明确标识。",
    fail: "列表正常展示，进入/点击后才提示下架或不可下载。",
    insufficient: "无法确认内容是否已下架，或详情需要支付/下载才能得知。",
    review: "临时网络错误与真实下架状态无法区分。",
    account: "可能需要内容访问账号。",
    safety: "禁止实际下载、购买或播放付费内容。",
    test: "正例：列表显示“已下架”；反例：进入后才提示；边界：网络暂时失败。",
    priority: "P1",
    gap: "当前 hybrid 无内容身份、列表/详情关联和状态 Handler。",
    dbChange: "executor_type 改为 rule_engine；补列表项 identity、详情检查步骤和状态断言。",
    batch: "content-state",
  },
  alioth_41: {
    capability: "二维码/网址提取、解码与安全可达性验证",
    precondition: "二维码或网址位于身份卡/弹窗等规则适用区域；只做安全 HEAD/GET 探测。",
    recommended: "rule_engine",
    collect: "page_capture_tool；qr_url_extractor_tool（新增）",
    judge: "rule_judge_tool → qr_url_validation Handler（已有）",
    packet: "二维码截图区域、解码 URL、展示文本、可复制值、重定向链、HTTP 与浏览器兼容摘要。",
    steps: "定位二维码/网址 → 解码/提取 → 校验复制值一致 → 调用已有 URL Handler → 记录重定向与可达性。",
    pass: "可识别、可复制且目标安全可达，展示值与实际值一致。",
    fail: "无法解码、网址失效、误导跳转或第三方浏览器不可访问。",
    insufficient: "截图分辨率不足、登录墙/地区限制或安全策略禁止访问。",
    review: "需要登录、一次性链接或内网地址。",
    account: "通常需要只读测试账号进入身份凭证区域。",
    safety: "不执行目标页表单，不下载文件；高风险域名仅安全探测。",
    test: "正例：二维码与展示网址一致且200；反例：解码404；边界：302到登录页。",
    priority: "P0",
    gap: "Handler 已存在，但真实任务没有从截图/DOM生成 judge_input.url，导致 TRUSTED_EVIDENCE_INVALID/缺字段阻断。",
    dbChange: "不改执行器；新增 qr_url_extractor、复制能力证据和可达性策略。",
    batch: "deterministic-link",
  },
  alioth_42: {
    capability: "入口展示与活动真实状态跨页一致性",
    precondition: "活动入口和详情页可安全访问；活动状态在页面或接口中可观测。",
    recommended: "hybrid",
    collect: "page_capture_tool；activity_state_extractor_tool（新增）；safe_click_observer_tool（新增）",
    judge: "cross_page_state_assertion_tool（新增）；semantic_rule_judge_tool（新增）",
    packet: "入口活动标识、入口状态、详情状态、有效期、接口摘要、页面位置和采集时间。",
    steps: "提取入口活动 → 进入详情 → 比较已结束/暂停/下线状态 → 判断入口应隐藏还是同步标识。",
    pass: "失效活动入口被隐藏，或入口与详情同步展示失效状态。",
    fail: "入口仍作为有效内容展示，进入后才发现已结束/暂停/下线。",
    insufficient: "活动身份无法关联、状态仅后端可见或采集跨越状态切换时间。",
    review: "活动结束后保留回顾页是否属于允许产品策略。",
    account: "视活动页权限而定。",
    safety: "不报名、不领取奖励、不提交活动数据。",
    test: "正例：已结束入口带状态；反例：点击后才显示已结束；边界：活动回顾入口。",
    priority: "P1",
    gap: "当前没有活动 identity、跨页状态和有效期工具，单页截图不能完成规则。",
    dbChange: "补活动 identity、允许回顾策略、跨页步骤和 tool_refs。",
    batch: "content-state",
  },
  alioth_43: {
    capability: "播放历史写入与恢复位置断言",
    precondition: "专用测试账号、可播放免费内容；规则明确最近集数/时间点容差和新用户行为。",
    recommended: "rule_engine",
    collect: "page_capture_tool；player_state_probe_tool（新增）；session_journey_runner_tool（新增）",
    judge: "playback_resume_handler（新增）",
    packet: "账号、内容 ID、播放集数/currentTime、离开时间、重新进入后的集数/currentTime、容差。",
    steps: "记录初始历史 → 播放到可识别时间点 → 离开并重新进入 → 探测恢复集数/时间 → 必要时清理测试历史。",
    pass: "有历史时恢复到允许容差内；无历史时从第一集起始。",
    fail: "恢复错误集数/时间、始终从头或无历史却跳到其他进度。",
    insufficient: "播放器不可探测、内容付费、历史同步延迟或账号已有不可清理历史。",
    review: "跨设备同步、片头跳过或内容版本变化导致时间偏移。",
    account: "必需隔离测试账号，最好支持清空播放历史。",
    safety: "不绕过付费；仅播放免费片段并控制时长。",
    test: "正例：第3集120秒恢复；反例：重进回到第1集；边界：片头自动跳过。",
    priority: "P1",
    gap: "当前 hybrid 无播放器探针和跨会话 Journey，截图无法证明历史恢复。",
    dbChange: "executor_type 改为 rule_engine；补内容 ID、容差、账号与清理步骤。",
    batch: "player-history",
  },
  alioth_45: {
    capability: "上传规则说明与文件选择能力检查",
    precondition: "进入发布页但不实际上传或发布；文件选择器可安全打开并取消。",
    recommended: "rule_engine",
    collect: "page_capture_tool；upload_contract_extractor_tool（新增）；safe_file_dialog_probe_tool（新增）",
    judge: "upload_rule_visibility_handler（新增）",
    packet: "数量/大小/格式说明、单选/多选属性、input accept/multiple、页面位置和对话框探测结果。",
    steps: "提取页面说明 → 检查 input 属性 → 可选地打开并立即取消文件对话框 → 断言限制和选择规则明确。",
    pass: "最高数量、大小、格式和单/多选规则明确展示，控件能力与说明一致。",
    fail: "关键信息缺失、说明与 input 属性矛盾，或宣称多选但控件不支持。",
    insufficient: "原生文件选择器不可探测，或上传入口需要额外权限。",
    review: "限制信息通过帮助链接展示是否算满足规则。",
    account: "可能需要发布权限账号。",
    safety: "不选择真实文件、不上传、不发布。",
    test: "正例：明确“最多9张、单张10MB、支持JPG/PNG”；反例：无任何限制说明；边界：说明位于帮助弹窗。",
    priority: "P2",
    gap: "当前 hybrid 无上传契约抽取和安全文件对话框探针。",
    dbChange: "executor_type 改为 rule_engine；补所需字段、展示位置和 file input 断言。",
    batch: "input-upload",
  },
  alioth_46: {
    capability: "媒体缩略图到全屏容器的单次交互断言",
    precondition: "帖子媒体可见且点击不会产生播放付费、下载或外部跳转。",
    recommended: "rule_engine",
    collect: "page_capture_tool；media_element_extractor_tool（新增）；safe_click_observer_tool（新增）",
    judge: "media_fullscreen_handler（新增）",
    packet: "媒体元素、点击前后容器几何、fullscreen API 状态、弹层角色、URL 和截图。",
    steps: "定位帖子媒体 → 单击一次 → 检查原生 fullscreen 或全屏预览容器 → 关闭并恢复。",
    pass: "进入可用的全屏/近全屏浏览模式，并可安全退出。",
    fail: "点击无反馈、仅保持缩略图或进入错误页面。",
    insufficient: "媒体不可点击、加载失败或浏览器禁止 fullscreen。",
    review: "产品使用沉浸式大图弹层但未调用原生 fullscreen API。",
    account: "可能需要只读社区账号。",
    safety: "不点赞、评论、下载或分享。",
    test: "正例：点击进入大图预览；反例：点击无反应；边界：近全屏弹层。",
    priority: "P2",
    gap: "当前 hybrid 无媒体容器前后状态 Handler，结果可确定性下沉。",
    dbChange: "executor_type 改为 rule_engine；补全屏定义、退出动作和 Handler。",
    batch: "interaction-media",
  },
  alioth_48: {
    capability: "登录态下账号登出入口存在性检查",
    precondition: "使用已登录测试账号进入设置/我的页面；只检查入口，不执行登出。",
    recommended: "rule_engine",
    collect: "page_capture_tool；account_control_extractor_tool（新增）",
    judge: "logout_entry_presence_handler（新增）",
    packet: "登录态、页面类型、候选元素文本/角色/位置、可见性和禁用状态。",
    steps: "确认已登录 → 遍历设置/账号区域 → 查找明确登出/退出登录入口 → 校验可见且可交互。",
    pass: "存在明确、可见、可交互的登出入口。",
    fail: "已登录且完整检查账号设置后仍无入口，或入口永久禁用。",
    insufficient: "未登录、设置页未探索完整或入口在无法访问的二级页面。",
    review: "入口仅通过系统菜单、长按或隐蔽手势提供。",
    account: "必需已登录测试账号。",
    safety: "禁止点击登出，避免破坏后续质检会话。",
    test: "正例：设置页显示退出登录；反例：账号区无任何登出入口；边界：入口位于账号安全二级页。",
    priority: "P1",
    gap: "当前 hybrid 无账号控件抽取；存在性规则无需 LLM 自由判断。",
    dbChange: "executor_type 改为 rule_engine；补登录态门禁、探索范围和 Handler。",
    batch: "interaction-account",
  },
};

function parseCsv(text) {
  const rows = [];
  let row = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        value += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(value);
      value = "";
    } else if (char === "\n") {
      row.push(value.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      value = "";
    } else {
      value += char;
    }
  }
  if (value || row.length) {
    row.push(value.replace(/\r$/, ""));
    rows.push(row);
  }
  const [headers, ...body] = rows;
  return body
    .filter((item) => item.some((cell) => cell !== ""))
    .map((item) =>
      Object.fromEntries(headers.map((header, index) => [header, item[index] ?? ""])),
    );
}

function readDatabaseRules() {
  const sql = `
    SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
    FROM (
      SELECT rule_code, name, category, executor_type, severity, applicable_page_types,
             steps, input_contract, judge_config, manual_review_policy,
             check_point, description, sort_order
      FROM qc_rule
      WHERE status = 'enabled'
      ORDER BY sort_order, rule_code
    ) AS t;
  `;
  const output = execFileSync(
    "docker",
    [
      "exec",
      "alioth-postgres",
      "psql",
      "-U",
      "alioth",
      "-d",
      "alioth",
      "-t",
      "-A",
      "-c",
      sql,
    ],
    { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
  ).trim();
  return JSON.parse(output);
}

function text(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join("、");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function escapeHtml(value) {
  return text(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function cell(value, className = "") {
  return `<td class="${className}">${escapeHtml(value).replaceAll("\n", "<br>")}</td>`;
}

function tag(value, className = "") {
  return `<span class="tag ${className}">${escapeHtml(value)}</span>`;
}

function currentToolSummary(rule) {
  const handler = rule.judge_config?.engine_config?.handler;
  const refs = rule.judge_config?.tool_refs ?? [];
  if (handler) {
    return `${handler}；${refs.length ? refs.join("、") : "tool_refs 未配置"}`;
  }
  return refs.length ? refs.join("、") : "tool_refs=[]；无独立 Judge Handler";
}

function currentLogic(rule) {
  const handler = rule.judge_config?.engine_config?.handler;
  if (handler) {
    return `candidate_rules_tool → rule_judge_tool → Observation Packet 质量门禁 → ${handler} → Judge 内原子持久化（绑定 observation_packet_id 与 execution_id）`;
  }
  const deterministicTools =
    rule.judge_config?.capability_config?.deterministic_tools ?? [];
  const preTools =
    rule.executor_type === "hybrid" && deterministicTools.length
      ? `${deterministicTools.join(" + ")} → `
      : "";
  return `candidate_rules_tool → rule_judge_tool → Observation Packet 质量门禁 → ${preTools}独立语义 Judge → Judge 内原子持久化（绑定 observation_packet_id 与 execution_id）`;
}

function compliance(rule) {
  const handler = rule.judge_config?.engine_config?.handler;
  const refs = Array.isArray(rule.judge_config?.tool_refs)
    ? rule.judge_config.tool_refs
    : [];
  const requiredArtifacts = Array.isArray(rule.input_contract?.required_artifacts)
    ? rule.input_contract.required_artifacts
    : [];
  const semanticReady = refs.some((item) =>
    [
      "semantic_rule_judge_tool",
      "visual_rule_judge_tool",
      "content_safety_judge_tool",
    ].includes(item),
  );
  const deterministicTools =
    rule.judge_config?.capability_config?.deterministic_tools;
  const hybridReady =
    rule.executor_type !== "hybrid" ||
    (Array.isArray(deterministicTools) && deterministicTools.length > 0);
  const routeReady =
    refs.includes("rule_judge_tool") ||
    (rule.executor_type !== "rule_engine" && semanticReady);
  const packetReady =
    requiredArtifacts.includes("ui_snapshot") &&
    requiredArtifacts.includes("screenshot");
  if (!routeReady || (rule.executor_type === "rule_engine" && !handler)) {
    return {
      code: "noncompliant",
      label: "不合规",
      reason: "缺少统一 Judge 路由或已注册的确定性 Handler。",
    };
  }
  if (!packetReady || !hybridReady) {
    return {
      code: "partial",
      label: "部分合规",
      reason: "统一 Judge 已接入，但证据契约或 Hybrid 确定性能力配置仍不完整。",
    };
  }
  return {
    code: "compliant",
    label: "合规",
    reason:
      "数据库规则经统一 Rule Judge 消费可信 Observation Packet；Judge 在同一 Tool 内原子写回结论、observation_packet_id 与 execution_id。",
  };
}

function buildRow(rule, source, profile, index) {
  const check = compliance(rule);
  const skill = categorySkill[rule.category] ?? `${rule.category}-rule`;
  const implementation =
    rule.executor_type === "rule_engine"
      ? "已实现专用确定性 Handler；缺少旅程或目标事实时返回 evidence_insufficient，不猜测通过。"
      : rule.executor_type === "hybrid"
        ? "已实现确定性预处理 + 独立多模态语义 Judge；低置信度进入人工复核。"
        : "已实现独立多模态语义 Judge；Main Agent 不再直接组织规则结论。";
  const searchText = [
    rule.rule_code,
    rule.name,
    skill,
    source["分类"],
    source["规范内容"],
    profile.capability,
    profile.gap,
  ].join(" ");
  return `<tr
    data-search="${escapeHtml(searchText.toLowerCase())}"
    data-category="${escapeHtml(rule.category)}"
    data-current="${escapeHtml(rule.executor_type)}"
    data-recommended="${escapeHtml(profile.recommended)}"
    data-compliance="${check.code}"
    data-priority="${escapeHtml(profile.priority)}">
    ${cell(index + 1, "sticky sticky-1 mono")}
    ${cell(source["原质检规范序号"], "sticky sticky-2 mono")}
    <td class="sticky sticky-3"><b class="mono">${escapeHtml(rule.rule_code)}</b></td>
    <td class="sticky sticky-4"><b>${escapeHtml(rule.name)}</b></td>
    <td>${tag(skill, "skill")}</td>
    <td>${tag(rule.category, "category")}</td>
    ${cell(source["分类"])}
    ${cell(source["规范内容"], "wide")}
    ${cell(source["类型"])}
    ${cell(source["严重/优先"])}
    ${cell(source["监测端"])}
    ${cell(rule.applicable_page_types, "mono")}
    <td>${tag(rule.executor_type, `exec exec-${rule.executor_type}`)}</td>
    ${cell(currentToolSummary(rule), "mono")}
    ${cell(currentLogic(rule), "flow")}
    <td>${tag(check.label, check.code)}<div class="small">${escapeHtml(check.reason)}</div></td>
    ${cell(profile.gap, "issue")}
    ${cell(profile.capability)}
    ${cell(profile.precondition)}
    <td>${tag(profile.recommended, `exec exec-${profile.recommended}`)}</td>
    ${cell(profile.collect, "mono")}
    ${cell(profile.judge, "mono")}
    ${cell(profile.packet)}
    ${cell(profile.steps, "flow")}
    ${cell(profile.pass, "pass")}
    ${cell(profile.fail, "fail")}
    ${cell(profile.insufficient)}
    ${cell(profile.review)}
    ${cell(profile.account)}
    ${cell(profile.safety)}
    ${cell(profile.test)}
    <td>${tag(profile.priority, `priority ${profile.priority.toLowerCase()}`)}</td>
    ${cell(implementation)}
    ${cell(profile.dbChange)}
    ${cell(profile.batch, "mono")}
    ${cell(skillBoundary[rule.category] ?? "以 candidate_rules_tool 返回的数据库规则为准。")}
  </tr>`;
}

function summaryCount(rules, key) {
  return rules.reduce((result, rule) => {
    const value = rule[key];
    result[value] = (result[value] ?? 0) + 1;
    return result;
  }, {});
}

const sourceResponse = await fetch(SOURCE_URL);
if (!sourceResponse.ok) {
  throw new Error(`无法读取质检规则表：HTTP ${sourceResponse.status}`);
}
const sourceRows = parseCsv(await sourceResponse.text()).filter(
  (row) => row["本期是否实现（青云客）"] === "是",
);
const databaseRules = readDatabaseRules();
const databaseByCode = new Map(databaseRules.map((rule) => [rule.rule_code, rule]));
const sourceByRule = new Map();
for (const row of sourceRows) {
  const ruleCode = sourceToRule.get(row["原质检规范序号"]);
  if (ruleCode) sourceByRule.set(ruleCode, row);
}

const errors = [];
for (const [sourceId, ruleCode] of sourceToRule) {
  if (!databaseByCode.has(ruleCode)) errors.push(`数据库缺少 ${ruleCode}`);
  if (!sourceByRule.has(ruleCode)) errors.push(`源表缺少 ${sourceId} → ${ruleCode}`);
  if (!profiles[ruleCode]) errors.push(`能力方案缺少 ${ruleCode}`);
}
if (databaseRules.length !== 40) errors.push(`启用规则数量不是40，实际为${databaseRules.length}`);
if (sourceRows.length !== 40) errors.push(`本期实现规则数量不是40，实际为${sourceRows.length}`);
if (errors.length) throw new Error(errors.join("\n"));

const rows = databaseRules.map((rule, index) =>
  buildRow(rule, sourceByRule.get(rule.rule_code), profiles[rule.rule_code], index),
);
const currentCounts = summaryCount(databaseRules, "executor_type");
const recommendedCounts = Object.values(profiles).reduce((result, profile) => {
  result[profile.recommended] = (result[profile.recommended] ?? 0) + 1;
  return result;
}, {});
const commit = execFileSync("git", ["rev-parse", "--short", "HEAD"], {
  cwd: ROOT,
  encoding: "utf8",
}).trim();
const generatedAt = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Asia/Shanghai",
}).format(new Date());

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>玉衡质检平台 · 40条规则执行能力映射表</title>
<style>
  :root{
    --bg:#ffffff;--text:#1f2937;--muted:#6b7280;--border:#e5e7eb;--head:#f8fafc;
    --card:#f9fafb;--accent:#4338ca;--accent-bg:#eef2ff;--ok:#047857;--warn:#b45309;
    --bad:#b91c1c;--blue:#1d4ed8;--sticky:#ffffff;
  }
  @media(prefers-color-scheme:dark){
    :root{--bg:#0f1420;--text:#e5e7eb;--muted:#9ca3af;--border:#30384a;--head:#1a2130;
      --card:#161d2b;--accent:#c7d2fe;--accent-bg:#312e81;--ok:#34d399;--warn:#fbbf24;
      --bad:#f87171;--blue:#60a5fa;--sticky:#111827}
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.65 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  .wrap{width:100%;padding:28px 20px 56px}
  h1{font-size:23px;margin:0 0 5px}
  .sub,.small{color:var(--muted);font-size:12px}
  .source{margin:10px 0 16px;padding:10px 12px;border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:8px;background:var(--card)}
  a{color:var(--blue)}
  .cards{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:10px;margin:14px 0}
  .card{border:1px solid var(--border);border-radius:9px;background:var(--card);padding:11px 13px}
  .card b{display:block;font-size:20px}.card span{color:var(--muted);font-size:12px}
  .toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0 10px}
  input,select,button{height:34px;border:1px solid var(--border);border-radius:7px;background:var(--bg);color:var(--text);padding:0 10px;font:inherit}
  input{min-width:300px;flex:1}
  button{cursor:pointer}button:hover{border-color:var(--accent)}
  .visible-count{color:var(--muted);margin-left:auto}
  .table-shell{height:calc(100vh - 285px);min-height:520px;overflow:auto;border:1px solid var(--border);border-radius:10px}
  table{border-collapse:separate;border-spacing:0;min-width:10500px;font-size:12.5px}
  th,td{padding:9px 10px;border-right:1px solid var(--border);border-bottom:1px solid var(--border);vertical-align:top;min-width:150px;max-width:330px;white-space:normal;overflow-wrap:anywhere}
  th{position:sticky;top:0;z-index:20;background:var(--head);color:var(--muted);font-size:12px;text-align:left;white-space:nowrap}
  tbody tr:hover td{background:color-mix(in srgb,var(--accent-bg) 35%,var(--bg))}
  .sticky{position:sticky;background:var(--sticky);z-index:6}
  th.sticky{z-index:30;background:var(--head)}
  .sticky-1{left:0;min-width:56px;max-width:56px}
  .sticky-2{left:56px;min-width:90px;max-width:90px}
  .sticky-3{left:146px;min-width:112px;max-width:112px}
  .sticky-4{left:258px;min-width:180px;max-width:180px;box-shadow:2px 0 0 var(--border)}
  .wide{min-width:430px;max-width:520px}.flow{min-width:360px}.mono{font-family:Consolas,"SFMono-Regular",monospace}
  .tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;line-height:18px;white-space:nowrap;border:1px solid var(--border)}
  .category,.skill{color:var(--accent);background:var(--accent-bg);border-color:transparent}
  .exec-rule_engine{color:var(--ok)}.exec-llm{color:var(--blue)}.exec-hybrid{color:var(--warn)}
  .compliant{color:var(--ok);border-color:var(--ok)}.partial{color:var(--warn);border-color:var(--warn)}.noncompliant{color:var(--bad);border-color:var(--bad)}
  .priority.p0{color:var(--bad);border-color:var(--bad)}.priority.p1{color:var(--warn);border-color:var(--warn)}.priority.p2{color:var(--blue);border-color:var(--blue)}
  .issue,.fail{color:var(--bad)}.pass{color:var(--ok)}
  .legend{color:var(--muted);font-size:12px;margin:8px 0}
  footer{margin-top:16px;color:var(--muted);font-size:12px}
  @media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.table-shell{height:65vh}input{min-width:100%}}
</style>
</head>
<body>
<main class="wrap">
  <h1>玉衡（Alioth）质检平台 · 40条规则执行能力映射大表</h1>
  <div class="sub">一张表查看当前实现、合规缺口、推荐执行器、所需 Tools、证据契约、判定标准、复核条件和测试用例。生成时间：${escapeHtml(generatedAt)}</div>
  <div class="source">
    <b>事实源边界：</b>Google 表格是业务规范上游，数据库 <span class="mono">qc_ruleset/qc_rule</span> 是运行时唯一事实源；
    Skill 仅说明适用场景和允许调用的 Tools，不保存规则编号、阈值或通过/失败标准。<br>
    业务源：<a href="${SOURCE_LINK}" target="_blank" rel="noreferrer">质检规则表 gid=0</a>；
    综合方案参考：用户提供的《质检平台_综合方案表.html》（34 方案、7 Tool、14 Skill）；
    数据库快照：本地启用规则；代码基线：<span class="mono">${escapeHtml(commit)}</span>。
  </div>
  <section class="cards">
    <div class="card"><b>40</b><span>本期启用规则</span></div>
    <div class="card"><b>${currentCounts.rule_engine ?? 0} / ${currentCounts.llm ?? 0} / ${currentCounts.hybrid ?? 0}</b><span>当前 rule_engine / llm / hybrid</span></div>
    <div class="card"><b>${currentCounts.rule_engine ?? 0}</b><span>当前确定性 Handler 规则</span></div>
    <div class="card"><b>${recommendedCounts.rule_engine ?? 0} / ${recommendedCounts.llm ?? 0} / ${recommendedCounts.hybrid ?? 0}</b><span>建议 rule_engine / llm / hybrid</span></div>
    <div class="card"><b>1</b><span>唯一主表，无需跳页</span></div>
  </section>
  <div class="toolbar">
    <input id="search" type="search" placeholder="搜索 rule_code、规则名称、Skill、规范或能力缺口">
    <select id="category"><option value="">全部分类</option>${Object.keys(categorySkill).map((item) => `<option>${item}</option>`).join("")}</select>
    <select id="current"><option value="">全部当前执行器</option><option>rule_engine</option><option>llm</option><option>hybrid</option></select>
    <select id="recommended"><option value="">全部推荐执行器</option><option>rule_engine</option><option>llm</option><option>hybrid</option></select>
    <select id="compliance"><option value="">全部合规状态</option><option value="compliant">合规</option><option value="partial">部分合规</option><option value="noncompliant">不合规</option></select>
    <select id="priority"><option value="">全部实施优先级</option><option>P0</option><option>P1</option><option>P2</option></select>
    <button id="reset" type="button">重置</button>
    <button id="csv" type="button">导出当前筛选 CSV</button>
    <span class="visible-count">显示 <b id="visible">40</b> / 40</span>
  </div>
  <div class="legend">红色“问题/失败”文字用于定位能力缺口，不代表该业务规则当前已命中；推荐执行器是建设建议，发布前仍需通过规则评审和黄金用例校准。</div>
  <div class="table-shell">
    <table id="mapping">
      <thead><tr>
        <th class="sticky sticky-1">#</th>
        <th class="sticky sticky-2">原规范序号</th>
        <th class="sticky sticky-3">rule_code</th>
        <th class="sticky sticky-4">规则名称</th>
        <th>当前 Skill name</th>
        <th>DB category</th>
        <th>原业务分类</th>
        <th>规范内容</th>
        <th>类型</th>
        <th>严重/优先</th>
        <th>监测端</th>
        <th>适用 page_types</th>
        <th>当前 executor</th>
        <th>当前 Handler / tool_refs</th>
        <th>当前实现逻辑（简化）</th>
        <th>当前合规性</th>
        <th>实施前能力缺口（改造依据）</th>
        <th>能力类型</th>
        <th>触发场景/前置条件</th>
        <th>推荐 executor</th>
        <th>需要的采集 Tools</th>
        <th>需要的判定 Tools / Handler</th>
        <th>Observation Packet 必填事实</th>
        <th>建议执行步骤</th>
        <th>通过标准</th>
        <th>失败标准</th>
        <th>证据不足条件</th>
        <th>人工复核条件</th>
        <th>账号/测试数据要求</th>
        <th>安全边界</th>
        <th>正例/反例/边界用例</th>
        <th>实施优先级</th>
        <th>建议建设状态</th>
        <th>数据库配置改动</th>
        <th>建议批次组</th>
        <th>Skill 职责边界</th>
      </tr></thead>
      <tbody id="mapBody">${rows.join("\n")}</tbody>
    </table>
  </div>
  <footer>该 HTML 为规划与评审快照，不参与运行时规则判定。任何规则发布必须经过数据库规则集版本化、差异审核和任务锁定。</footer>
</main>
<script>
  const controls = ["search","category","current","recommended","compliance","priority"].map(id => document.getElementById(id));
  const tableRows = [...document.querySelectorAll("#mapBody tr")];
  function applyFilters(){
    const query = document.getElementById("search").value.trim().toLowerCase();
    const filters = {
      category: document.getElementById("category").value,
      current: document.getElementById("current").value,
      recommended: document.getElementById("recommended").value,
      compliance: document.getElementById("compliance").value,
      priority: document.getElementById("priority").value
    };
    let visible = 0;
    for(const row of tableRows){
      const matchText = !query || row.dataset.search.includes(query);
      const matchSelect = Object.entries(filters).every(([key,value]) => !value || row.dataset[key] === value);
      row.hidden = !(matchText && matchSelect);
      if(!row.hidden) visible += 1;
    }
    document.getElementById("visible").textContent = visible;
  }
  controls.forEach(control => control.addEventListener("input", applyFilters));
  document.getElementById("reset").addEventListener("click", () => {
    controls.forEach(control => control.value = "");
    applyFilters();
  });
  document.getElementById("csv").addEventListener("click", () => {
    const headers = [...document.querySelectorAll("#mapping thead th")].map(cell => cell.innerText.trim());
    const data = tableRows.filter(row => !row.hidden).map(row => [...row.cells].map(cell => cell.innerText.trim()));
    const csv = [headers, ...data].map(row => row.map(value => '"' + value.replaceAll('"','""') + '"').join(",")).join("\\r\\n");
    const blob = new Blob(["\\ufeff", csv], {type:"text/csv;charset=utf-8"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "质检平台_规则执行能力映射表.csv";
    link.click();
    URL.revokeObjectURL(link.href);
  });
</script>
</body>
</html>`;

function validateGeneratedHtml(value) {
  const failures = [];
  const headerBlock = value.match(/<thead>([\s\S]*?)<\/thead>/)?.[1] ?? "";
  const headerCount = (headerBlock.match(/<th\b/g) ?? []).length;
  const rowBlocks = [...value.matchAll(/<tr\s+data-search=[\s\S]*?<\/tr>/g)].map(
    (match) => match[0],
  );
  if (headerCount !== 36) failures.push(`主表表头应为36列，实际为${headerCount}`);
  if (rowBlocks.length !== 40) failures.push(`主表数据应为40行，实际为${rowBlocks.length}`);
  rowBlocks.forEach((row, index) => {
    const cellCount = (row.match(/<td\b/g) ?? []).length;
    if (cellCount !== 36) failures.push(`第${index + 1}行应为36列，实际为${cellCount}`);
  });
  for (const id of [
    "search",
    "category",
    "current",
    "recommended",
    "compliance",
    "priority",
    "reset",
    "csv",
    "visible",
    "mapBody",
  ]) {
    if (!value.includes(`id="${id}"`)) failures.push(`缺少交互元素 #${id}`);
  }
  const scripts = [...value.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) {
    failures.push(`页面脚本数量应为1，实际为${scripts.length}`);
  } else {
    try {
      new Function(scripts[0][1]);
    } catch (error) {
      failures.push(`页面脚本语法错误：${error.message}`);
    }
  }
  if (value.includes(">undefined<") || value.includes("[object Object]")) {
    failures.push("页面存在未序列化的数据占位值");
  }
  if (failures.length) throw new Error(failures.join("\n"));
}

validateGeneratedHtml(html);
await mkdir(path.dirname(OUTPUT), { recursive: true });
await writeFile(OUTPUT, html, "utf8");
console.log(
  JSON.stringify(
    {
      output: OUTPUT,
      sourceRules: sourceRows.length,
      databaseRules: databaseRules.length,
      currentCounts,
      recommendedCounts,
    },
    null,
    2,
  ),
);
