"""正式 App 质检截图动作 Playbook（参照 untitled 脚本节奏，不导入 Airtest）。

包名／APK 前缀与 app_script_mapping.heuristic_script_choice 对齐：
dx093j*→untitled7；sns-hjsq*→untitled6；sns_51aw*→untitled5；
sns_911bl*→untitled4；sns_91sp*→untitled3；hlwapp*→untitled2；
hlapp*→untitled1；91cg*→untitled；hjll／51pc→Agent Workflow 专用 playbook。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.application.app_script_mapping import heuristic_script_choice
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)


@dataclass(frozen=True, kw_only=True)
class AppCapturePlaybook:
    """参照历史 Airtest 节奏的探索约束（只描述动作，不执行脚本）。"""

    playbook_id: str
    script_ref: str
    match_reason: str
    tabs: tuple[str, ...]
    swipes_per_tab: int
    splash_burst: int
    wait_line_check: bool
    age_gate: bool
    allow_ad_outbound: bool
    max_actions: int
    duration_ms: int
    deadline_seconds: int
    objective_hints: tuple[str, ...]
    # 每栏下滑次数（空则回退 swipes_per_tab）；供课纲教 LLM
    tab_swipe_plan: tuple[tuple[str, int], ...] = ()
    # 全局禁止项／脚本要点（写入 objective 与 script_curriculum）
    forbid_hints: tuple[str, ...] = ()
    script_notes: tuple[str, ...] = ()


_DEFAULT = AppCapturePlaybook(
    playbook_id="default",
    script_ref="none",
    match_reason="未匹配专用包，使用通用默认节奏",
    tabs=("首页", "发现", "福利"),
    swipes_per_tab=4,
    splash_burst=2,
    wait_line_check=True,
    age_gate=True,
    allow_ad_outbound=False,
    max_actions=28,
    duration_ms=600_000,
    deadline_seconds=900,
    objective_hints=(
        "等待线路检测／加载结束（禁止点直连、翻墙、重装）",
        "开屏只点右上跳过，禁止点广告本体",
        "弹层先截图再点中央关闭／X；系统公告点确认但不作为广告证据",
        "底栏只点安全 Tab，禁止点「我的」／充值／下载",
        "每栏：首屏截图→左缘下滑再截图，画面停滞后换栏",
    ),
    forbid_hints=("禁止直连／翻墙／重装／充值／下载", "默认禁止点广告本体进外链"),
)

_BY_SCRIPT: dict[str, AppCapturePlaybook] = {
    "untitled": AppCapturePlaybook(
        playbook_id="91cg",
        script_ref="untitled.py",
        match_reason="91cg／吃瓜",
        tabs=("社区", "片库", "福利", "AI科技"),
        swipes_per_tab=3,
        splash_burst=1,
        wait_line_check=False,
        age_gate=False,
        allow_ad_outbound=True,
        max_actions=36,
        duration_ms=720_000,
        deadline_seconds=1200,
        objective_hints=(
            "严格对齐 untitled.py：等稳 → 中部广告外链 → BACK → "
            "点右上「跳过」(close_1/2/3) → 白圈X+确认",
            "注意：脚本变量名虽叫 close_*，模板图就是「跳过」，必须执行",
            "底栏依次：社区→片库→福利→AI科技（禁止我的）",
            "社区／片库／福利多滑；AI科技少滑",
        ),
        tab_swipe_plan=(("社区", 3), ("片库", 3), ("福利", 3), ("AI科技", 1)),
        forbid_hints=("禁止下载／安装／充值", "外链取证轮次必须有限"),
        script_notes=(
            "untitled.py：wait→ad_outbound→tap_skip_corner（文案+右上坐标，close_*图仅参考）→白圈X+confirm→四栏",
            "证据帧：before_find_ad／chrome_after_ad_click*／back_to_app_*／after_close_*／shequ_*…",
        ),
    ),
    "untitled1": AppCapturePlaybook(
        playbook_id="hlapp",
        script_ref="untitled1.py",
        match_reason="hlapp／黑料不打烊",
        tabs=("首页", "精选", "片库", "直播", "AI科技", "圈子"),
        swipes_per_tab=1,
        splash_burst=1,
        wait_line_check=False,
        age_gate=True,
        allow_ad_outbound=False,
        # 含开屏／浮层关失败重试／详情返回后重巡底栏；120 易在回首页后耗尽仍未真切栏。
        max_actions=240,
        duration_ms=3_600_000,
        deadline_seconds=5400,
        objective_hints=(
            "年龄确认（若有）→开屏本体截图 1 帧→点右上跳过后截图确认（共 2 张）",
            "中间广告：先截后点 X；系统公告只关不存广告图",
            "底栏硬覆盖：首页→精选→片库→直播→AI科技→圈子（禁止我的）",
            "每栏最多下滑 1 次，优先切栏与进详情；缺页不得提前收口",
            "中轴偏上左缘下滑，画面不变即换栏",
            "从详情返回首页后必须继续点未完成底栏，禁止只卡在浮层关层空转",
        ),
        tab_swipe_plan=(
            ("首页", 1),
            ("精选", 1),
            ("片库", 1),
            ("直播", 1),
            ("AI科技", 1),
            ("圈子", 1),
        ),
        forbid_hints=(
            "禁止点广告本体",
            "禁止点「我的」／充值提交／下载／安装／登录提交／发帖提交",
        ),
        script_notes=(
            "对应 untitled1.py 与 hlapp_7_types 分母；"
            "本轮不做我的／设置／充值／发帖，预算留给 7 类必达页",
        ),
    ),
    "hjll": AppCapturePlaybook(
        playbook_id="hjll",
        script_ref="agent_workflow",
        match_reason="hjll／海角乱伦",
        # 3.3.x 实机底栏（录屏／用户确认）：主页／短剧／暗网／AI／社区／我的
        # 「主页」即首页；「短剧」与历史「视频」槽同义（中槽会换皮）
        tabs=("主页", "短剧", "暗网", "AI", "社区", "我的"),
        swipes_per_tab=0,
        splash_burst=1,
        wait_line_check=False,
        age_gate=True,
        allow_ad_outbound=False,
        # 开屏＋多层弹层＋六栏硬覆盖：280 基线再加 50，留给付费遮罩 BACK 与六栏重试
        max_actions=330,
        # 回合墙钟 150 分钟（开屏／弹层／上方分类／六栏）
        duration_ms=9_000_000,
        deadline_seconds=9_000,
        objective_hints=(
            "年龄确认：先点蓝色「我已满18岁」（APP JSON ui_tree_features.age_gate）"
            "→开屏本体截图 1 帧→点右上关闭后截图确认（共 2 张）"
            "→弹窗广告逐个截图并用下方 X 关闭",
            "弹层关完已在主页／首页：当前帧截图即算主页到访，禁止再点主页，立刻切下一栏",
            "底栏硬覆盖顺序（每栏点一次+截图后立刻下栏，不进内容卡）：短剧→暗网→AI→社区→我的",
            "主页／短剧／暗网／AI／社区／我的：到访截图即可，禁止充值／登录提交／下钻详情",
            "缺页优先底栏切栏，不得提前收口；卡在视频详情付费遮罩时先 BACK",
        ),
        tab_swipe_plan=(
            ("主页", 0),
            ("短剧", 0),
            ("暗网", 0),
            ("AI", 0),
            ("社区", 0),
            ("我的", 0),
        ),
        forbid_hints=(
            "禁止点广告本体",
            "禁止点充值提交／下载／安装／登录提交／发帖提交",
            "禁止弹层关完后仍点击已选中的主页／首页",
            "禁止未关完弹窗就切底栏",
            "禁止底栏未巡完前点内容卡／详情／付费墙",
            "禁止一次提升多个底栏导致先点我的",
        ),
        script_notes=(
            "用户验收（对照录屏）：开屏截图+跳过→弹窗逐个截图关下方X→主页只截不点"
            "→短剧→暗网→AI→社区→我的（每栏截图即下栏）；"
            "中槽会换皮时仍按可见底栏标签点，别名互认短剧／视频等",
        ),
    ),
    "pc51": AppCapturePlaybook(
        playbook_id="pc51",
        script_ref="agent_workflow",
        match_reason="51pc／51品茶",
        tabs=("首页", "茶馆", "视频"),
        swipes_per_tab=1,
        splash_burst=2,
        wait_line_check=False,
        age_gate=True,
        allow_ad_outbound=False,
        max_actions=100,
        duration_ms=2_100_000,
        deadline_seconds=3000,
        objective_hints=(
            "年龄确认（若有）→开屏跳过→弹层先关",
            "底栏硬覆盖：首页→茶馆→视频（禁止我的／充值）",
            "茶馆下钻内容详情；视频栏下钻视频详情；缺页不得提前收口",
            "每栏最多下滑 1 次；缺页优先覆盖寻页",
        ),
        tab_swipe_plan=(("首页", 1), ("茶馆", 1), ("视频", 1)),
        forbid_hints=(
            "禁止点广告本体",
            "禁止点「我的」／充值提交／下载／安装／登录提交",
        ),
        script_notes=(
            "对应 pc51_5_types 分母；本轮不做我的／设置／支付／社区／直播",
        ),
    ),
    "porn91": AppCapturePlaybook(
        playbook_id="porn91",
        script_ref="agent_workflow",
        match_reason="91PORN／91porn",
        tabs=("首页", "发现", "视频", "福利", "圈子"),
        swipes_per_tab=1,
        splash_burst=5,
        wait_line_check=False,
        age_gate=True,
        allow_ad_outbound=False,
        max_actions=120,
        duration_ms=1_800_000,
        deadline_seconds=2700,
        objective_hints=(
            "年龄确认（若有）→开屏跳过→弹层先关（勿久等线路）",
            "底栏硬覆盖：首页→发现／精选→视频→福利→圈子（禁止我的）",
            "列表下钻内容／视频详情；缺页优先覆盖寻页",
        ),
        tab_swipe_plan=(
            ("首页", 1),
            ("发现", 1),
            ("视频", 1),
            ("福利", 1),
            ("圈子", 1),
        ),
        forbid_hints=(
            "禁止点广告本体",
            "禁止点「我的」／充值提交／下载／安装／登录提交",
        ),
        script_notes=("对应 generic5 分母；无测号不测我的／充值",),
    ),
    "erbull": AppCapturePlaybook(
        playbook_id="erbull",
        script_ref="agent_workflow",
        match_reason="萝莉岛2／erbulld",
        tabs=("首页", "发现", "视频", "福利", "圈子"),
        swipes_per_tab=1,
        splash_burst=2,
        wait_line_check=False,
        age_gate=True,
        allow_ad_outbound=False,
        max_actions=90,
        duration_ms=1_800_000,
        deadline_seconds=2700,
        objective_hints=(
            "年龄确认（若有）→开屏跳过→弹层先关（勿久等线路）",
            "底栏硬覆盖：首页→发现／精选→视频→福利→圈子（禁止我的）",
            "列表下钻内容／视频详情；缺页优先覆盖寻页",
        ),
        tab_swipe_plan=(
            ("首页", 1),
            ("发现", 1),
            ("视频", 1),
            ("福利", 1),
            ("圈子", 1),
        ),
        forbid_hints=(
            "禁止点广告本体",
            "禁止点「我的」／充值提交／下载／安装／登录提交",
        ),
        script_notes=("对应 generic5 分母；无测号不测我的／充值",),
    ),
    "untitled2": AppCapturePlaybook(
        playbook_id="hlwapp",
        script_ref="untitled2.py",
        match_reason="hlwapp／黑料网",
        tabs=("吃瓜", "AI科技", "社区", "视频", "福利"),
        swipes_per_tab=2,
        splash_burst=2,
        wait_line_check=True,
        age_gate=True,
        allow_ad_outbound=False,
        max_actions=48,
        duration_ms=780_000,
        deadline_seconds=1200,
        objective_hints=(
            "等待线路检测结束（禁止点直连／重装）→开屏最多 2 帧→跳过",
            "中间层先截后关；神器甄选可左缘滑多截再关",
            "误进 Chrome 只 BACK／拉回目标包",
            "底栏：吃瓜→AI科技→社区→视频→福利",
        ),
        tab_swipe_plan=(
            ("吃瓜", 2),
            ("AI科技", 2),
            ("社区", 2),
            ("视频", 2),
            ("福利", 2),
        ),
        forbid_hints=("禁止直连／重装", "禁止下载安装", "禁止点广告本体"),
        script_notes=("对应 untitled2.py：含数据初始化等待与神器甄选阶段",),
    ),
    "untitled3": AppCapturePlaybook(
        playbook_id="sns_91sp",
        script_ref="untitled3.py",
        match_reason="sns_91sp",
        tabs=("首页", "AI科技", "聊天广场", "福利"),
        swipes_per_tab=4,
        splash_burst=2,
        wait_line_check=True,
        age_gate=True,
        allow_ad_outbound=False,
        max_actions=36,
        duration_ms=720_000,
        deadline_seconds=1200,
        objective_hints=(
            "年龄→等线路→开屏→跳过",
            "中间广告先截后点中央 X；系统公告橘确认、不截广告图",
            "底栏：首页→AI科技→聊天广场→福利",
            "偏左上滑，每栏多帧截图",
        ),
        tab_swipe_plan=(("首页", 6), ("AI科技", 1), ("聊天广场", 2), ("福利", 5)),
        forbid_hints=("禁止点我直连", "禁止点广告内容"),
        script_notes=("对应 untitled3.py：首页滑较多，AI科技少滑",),
    ),
    "untitled4": AppCapturePlaybook(
        playbook_id="sns_911bl",
        script_ref="untitled4.py",
        match_reason="sns_911bl",
        tabs=("首页", "看片", "直播", "AI科技", "福利"),
        swipes_per_tab=4,
        splash_burst=3,
        wait_line_check=True,
        age_gate=True,
        allow_ad_outbound=False,
        max_actions=40,
        duration_ms=780_000,
        deadline_seconds=1200,
        objective_hints=(
            "等线路→开屏最多 3→跳过",
            "弹层可多次出现：广告先截后关；公告点左侧确认（禁止一键复制）",
            "底栏：首页→看片→直播→AI科技→福利",
        ),
        tab_swipe_plan=(
            ("首页", 5),
            ("看片", 2),
            ("直播", 2),
            ("AI科技", 1),
            ("福利", 4),
        ),
        forbid_hints=("禁止一键复制", "禁止重新安装", "禁止点广告本体"),
        script_notes=("对应 untitled4.py：公告点左侧确认",),
    ),
    "untitled5": AppCapturePlaybook(
        playbook_id="sns_51aw",
        script_ref="untitled5.py",
        match_reason="sns_51aw",
        tabs=("首页", "吃瓜", "大赛"),
        swipes_per_tab=4,
        splash_burst=2,
        wait_line_check=True,
        age_gate=False,
        allow_ad_outbound=False,
        max_actions=32,
        duration_ms=660_000,
        deadline_seconds=1080,
        objective_hints=(
            "等线路（无年龄）→开屏约 2（禁止点点击开炮）→右上跳过",
            "中间广告先截后关直到首页图墙露出",
            "底栏仅：首页→吃瓜→大赛（跳过 AI科技／我的）",
            "极左缘慢滑，每栏固定下滑并必截",
        ),
        tab_swipe_plan=(("首页", 4), ("吃瓜", 4), ("大赛", 4)),
        forbid_hints=("禁止点击开炮", "禁止 AI科技／我的", "首页禁点图墙图标"),
        script_notes=("对应 untitled5.py：无年龄门；只跑三个安全栏",),
    ),
    "untitled6": AppCapturePlaybook(
        playbook_id="sns-hjsq",
        script_ref="untitled6.py",
        match_reason="sns-hjsq",
        tabs=("主页", "社区", "ACG", "应用"),
        swipes_per_tab=6,
        splash_burst=2,
        wait_line_check=True,
        age_gate=False,
        allow_ad_outbound=False,
        max_actions=48,
        duration_ms=900_000,
        deadline_seconds=1500,
        objective_hints=(
            "等线路→开屏→跳过（可重试到消失）",
            "神器甄选：截→左缘滑→再截→关；公告点「我知道了」不截",
            "底栏：主页→社区→ACG→应用（禁止 AI科技／我的）",
            "左缘下滑次数偏多，尽量多帧保留",
        ),
        tab_swipe_plan=(("主页", 6), ("社区", 4), ("ACG", 10), ("应用", 13)),
        forbid_hints=("禁止 AI科技／我的", "栏内零内容点击", "禁止直连／重装"),
        script_notes=("对应 untitled6.py：应用栏下滑可达十余次",),
    ),
    "untitled7": AppCapturePlaybook(
        playbook_id="dx093j",
        script_ref="untitled7.py",
        match_reason="dx093j",
        tabs=("ACG", "视频", "暗网", "圈子", "广场"),
        swipes_per_tab=4,
        splash_burst=3,
        wait_line_check=False,
        age_gate=False,
        allow_ad_outbound=False,
        max_actions=44,
        duration_ms=840_000,
        deadline_seconds=1320,
        objective_hints=(
            "启动后高速连拍开屏（跳过品牌／灰空壳／烂帧）",
            "公告「知道了」（禁止应用推荐）；图标网格可左缘滑多截再关",
            "主墙无压暗时禁止乱点 X",
            "底栏：ACG→视频→暗网→圈子→广场；暗网「立即开通」只截不点",
        ),
        tab_swipe_plan=(
            ("ACG", 5),
            ("视频", 4),
            ("暗网", 3),
            ("圈子", 5),
            ("广场", 6),
        ),
        forbid_hints=(
            "勿先久等线路",
            "主墙无压暗禁乱点 X",
            "暗网立即开通只截不点",
            "禁止应用推荐",
        ),
        script_notes=("对应 untitled7.py：高速开屏连拍；无跳过优先",),
    ),
    "xingba": AppCapturePlaybook(
        playbook_id="xingba",
        script_ref="agent_workflow",
        match_reason="杏吧／uerma.zqeluk.cwsxx 实机底栏",
        # 实机五栏：视频／吃瓜／交友／应用／我的；每栏必须 click + after 截图。
        tabs=("视频", "吃瓜", "交友", "应用", "我的"),
        swipes_per_tab=2,
        splash_burst=2,
        wait_line_check=False,
        age_gate=False,
        allow_ad_outbound=False,
        max_actions=48,
        duration_ms=900_000,
        deadline_seconds=1500,
        objective_hints=(
            "开屏只点右上「进入／跳过」，禁止点广告本体进外链",
            "弹层可见底栏时优先点未巡检底栏并截图，禁止用重复关层挤掉底栏",
            "底栏硬覆盖：视频→吃瓜→交友→应用→我的；每栏首屏截图后再少滑留证",
            "「我的」只读进入截图，禁止充值／下载／登录提交／发帖提交",
        ),
        tab_swipe_plan=(
            ("视频", 2),
            ("吃瓜", 2),
            ("交友", 2),
            ("应用", 2),
            ("我的", 1),
        ),
        forbid_hints=(
            "禁止点广告本体进外链",
            "禁止充值提交／下载／安装／登录提交／发帖提交",
        ),
        script_notes=(
            "杏吧实机底栏五格全巡；覆盖缺口未完时弹层帧须让路底栏；"
            "允许只读进「我的」截图"
        ),
    ),
}


def resolve_app_capture_playbook(
    *,
    apk_filename: str | None = None,
    package_name: str | None = None,
    app_label: str | None = None,
) -> AppCapturePlaybook:
    """按 APK／包名解析 playbook；未命中专用规则时回退默认节奏。"""
    choice = heuristic_script_choice(
        apk_filename,
        package_name=package_name,
        app_label=app_label,
    )
    mapped = _BY_SCRIPT.get(choice.script_id)
    if mapped is not None:
        return AppCapturePlaybook(
            playbook_id=mapped.playbook_id,
            script_ref=mapped.script_ref,
            match_reason=choice.reason or mapped.match_reason,
            tabs=mapped.tabs,
            swipes_per_tab=mapped.swipes_per_tab,
            splash_burst=mapped.splash_burst,
            wait_line_check=mapped.wait_line_check,
            age_gate=mapped.age_gate,
            allow_ad_outbound=mapped.allow_ad_outbound,
            max_actions=mapped.max_actions,
            duration_ms=mapped.duration_ms,
            deadline_seconds=mapped.deadline_seconds,
            objective_hints=mapped.objective_hints,
            tab_swipe_plan=mapped.tab_swipe_plan,
            forbid_hints=mapped.forbid_hints,
            script_notes=mapped.script_notes,
        )
    if choice.execution_mode != "airtest":
        return AppCapturePlaybook(
            playbook_id=_DEFAULT.playbook_id,
            script_ref=_DEFAULT.script_ref,
            match_reason=choice.reason or _DEFAULT.match_reason,
            tabs=_DEFAULT.tabs,
            swipes_per_tab=_DEFAULT.swipes_per_tab,
            splash_burst=_DEFAULT.splash_burst,
            wait_line_check=_DEFAULT.wait_line_check,
            age_gate=_DEFAULT.age_gate,
            allow_ad_outbound=_DEFAULT.allow_ad_outbound,
            max_actions=_DEFAULT.max_actions,
            duration_ms=_DEFAULT.duration_ms,
            deadline_seconds=_DEFAULT.deadline_seconds,
            objective_hints=_DEFAULT.objective_hints,
            tab_swipe_plan=_DEFAULT.tab_swipe_plan,
            forbid_hints=_DEFAULT.forbid_hints,
            script_notes=_DEFAULT.script_notes,
        )
    return _DEFAULT


def build_playbook_objective(
    playbook: AppCapturePlaybook,
    *,
    journey_steps: list[str] | None = None,
) -> str:
    """生成注入 Explorer 的 objective（动作节奏约束，非固定脚本）。"""
    lines: list[str] = [
        (
            f"按 App 截图 Playbook「{playbook.playbook_id}」探索并多帧截图"
            f"（参照 {playbook.script_ref} 节奏，不执行 Airtest）。"
            f"匹配：{playbook.match_reason}。"
        )
    ]
    if journey_steps:
        lines.append("用户旅程：" + " → ".join(journey_steps) + "。")
    lines.append("阶段优先级：")
    for index, hint in enumerate(playbook.objective_hints, start=1):
        lines.append(f"{index}. {hint}")
    lines.append(
        "底栏顺序："
        + " → ".join(playbook.tabs)
        + f"；每栏约下滑 {playbook.swipes_per_tab} 次并截图。"
    )
    lines.append(
        "硬约束：禁止点下载／安装／充值／一键复制／翻墙直连／重装链接；误进浏览器只 BACK。"
    )
    lines.append(
        "多帧节奏：开屏先 OBSERVE 连拍 → 跳过／关弹层 → 每栏 SCROLL 下滑截图 → 换底栏；"
        "单次点击失败也必须继续下一动作，禁止只停在首屏广告一帧。"
    )
    from services.api.app.application.app_script_curriculum import (
        format_curriculum_objective_section,
    )

    lines.append(format_curriculum_objective_section(playbook))
    if not playbook.allow_ad_outbound:
        lines.append("默认禁止主动点广告本体进外链（只关弹层）。")
    return "\n".join(lines)


def build_playbook_candidates(
    request: UnderstandAppScreenshotRequest,
    playbook: AppCapturePlaybook,
    *,
    reason: str,
    surface_facts: Mapping[str, object] | None = None,
) -> AppVisualAnalysis:
    """视觉理解失败时只生成无固定坐标的安全恢复候选。

    Playbook 只提供探索顺序提示，不能伪造当前截图中并不存在的点击控件。
    """
    if not isinstance(request, UnderstandAppScreenshotRequest):
        raise TypeError("request 必须为 UnderstandAppScreenshotRequest")
    if not isinstance(playbook, AppCapturePlaybook):
        raise TypeError("playbook 必须为 AppCapturePlaybook")

    allowed = set(request.allowed_actions)
    candidates: list[AppVisualCandidate] = []
    summary = (reason or "").strip() or f"playbook={playbook.playbook_id} 探索兜底"

    def _add(
        *,
        name: str,
        action: InteractionOperation,
        bounds: NormalizedBounds | None,
        confidence: float,
        risk: InteractionRiskLevel,
        purpose: str,
        hint: str | None,
    ) -> None:
        if action not in allowed or len(candidates) >= request.max_candidates:
            return
        candidates.append(
            AppVisualCandidate.build(
                source_capture_id=request.capture_id,
                source_image_signature=request.image_signature,
                source_width=request.width,
                source_height=request.height,
                source_orientation=request.orientation,
                semantic_name=name,
                action_type=action,
                normalized_bounds=bounds,
                confidence=confidence,
                risk_level=risk,
                purpose=purpose,
                expected_state_hint=hint,
            )
        )

    # 无视觉事实时禁止生成 CLICK/TAP 和固定坐标，只保留可恢复动作。
    _add(
        name="再次观测当前屏",
        action=InteractionOperation.OBSERVE,
        bounds=None,
        confidence=0.58,
        risk=InteractionRiskLevel.LOW,
        purpose="视觉理解失败后保留当前截图并等待界面稳定",
        hint="observe",
    )
    _add(
        name="向下滑动并重新观测",
        action=InteractionOperation.SCROLL,
        bounds=None,
        confidence=0.56,
        risk=InteractionRiskLevel.LOW,
        purpose="仅执行无目标滚动，动作后必须重新截图理解",
        hint="scroll",
    )
    _add(
        name="系统返回键",
        action=InteractionOperation.BACK,
        bounds=None,
        confidence=0.4,
        risk=InteractionRiskLevel.LOW,
        purpose="误进浏览器／详情时 BACK 回 App",
        hint="back",
    )

    return AppVisualAnalysis.build(
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        width=request.width,
        height=request.height,
        orientation=request.orientation,
        visual_page_type=None,
        visual_summary=summary[:500],
        key_elements=(f"playbook:{playbook.playbook_id}", "script_ref:" + playbook.script_ref),
        candidates=tuple(candidates[: request.max_candidates]),
        provider_id="heuristic-app-vision",
        model_id=f"playbook-{playbook.playbook_id}-v1",
        package_name=request.package_name,
        activity=request.activity,
        surface_facts=surface_facts,
    )
