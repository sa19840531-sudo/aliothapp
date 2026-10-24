"""App 截图脚本动作课纲：把 untitled*.py 节奏教给 Explorer LLM（不执行 Airtest）。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.application.app_capture_playbook import AppCapturePlaybook

_PLAYBOOK_ID_RE = re.compile(r"Playbook「([^」]+)」")
APP_CURRICULUM_PROGRESS_VERSION = "app-curriculum-progress.v1"
APP_CURRICULUM_OBSERVATION_VERSION = "app-curriculum-observation.v1"
APP_CURRICULUM_ACTION_VERSION = "app-curriculum-action.v1"
_TAP_SKIP_SEMANTIC_MARKERS = ("tap_skip", "skip", "跳过", "进入", "entry", "enter")
_TAP_SKIP_EXCLUSION_MARKERS = (
    "关闭弹窗",
    "关闭当前遮挡",
    "关闭遮挡",
    "close popup",
    "close overlay",
    "弹层",
    "广告本体",
    "广告内容",
    "广告链接",
    "广告区域",
    "广告图片",
    "广告卡片",
    "点击广告",
    "ad body",
    "ad content",
    "ad link",
    "ad area",
    "ad image",
    "ad card",
    "ad banner",
    "advertisement body",
    "advertisement content",
    "advertisement link",
    "click ad",
    "ad_body",
    "ad_content",
    "ad_link",
)
_GENERIC_CLOSE_TARGETS = frozenset({"关闭", "close", "dismiss", "x", "×"})


@dataclass(frozen=True, kw_only=True)
class AppCurriculumProgress:
    """随 Exploration Work 持久化的 App 课纲单调进度。"""

    playbook_id: str
    phase_ids: tuple[str, ...]
    phase_action_requirements: tuple[tuple[str, int], ...]
    phase_cursor: int = 0
    completed_phase_ids: tuple[str, ...] = ()
    current_phase_action_count: int = 0
    # 旧 v1 JSON 不含该字段时按空集合读取；它只记录当前帧已成功见证的未来阶段。
    witnessed_phase_ids: tuple[str, ...] = ()
    protocol_version: str = APP_CURRICULUM_PROGRESS_VERSION

    def __post_init__(self) -> None:
        _require_text(self.playbook_id, "playbook_id")
        phase_ids = _unique_text_tuple(self.phase_ids, "phase_ids")
        requirements = tuple(self.phase_action_requirements)
        if len(requirements) != len(phase_ids):
            raise ValueError("phase_action_requirements 必须覆盖全部 phase_ids")
        seen: set[str] = set()
        for phase_id, required_actions in requirements:
            _require_text(phase_id, "phase_action_requirements.phase_id")
            if phase_id in seen or phase_id not in phase_ids:
                raise ValueError("phase_action_requirements 包含重复或未知阶段")
            if (
                isinstance(required_actions, bool)
                or not isinstance(required_actions, int)
                or required_actions <= 0
            ):
                raise ValueError("阶段 required_actions 必须为正整数")
            seen.add(phase_id)
        if tuple(item[0] for item in requirements) != phase_ids:
            raise ValueError("phase_action_requirements 必须与 phase_ids 同序")
        if (
            isinstance(self.phase_cursor, bool)
            or not isinstance(self.phase_cursor, int)
            or not 0 <= self.phase_cursor <= len(phase_ids)
        ):
            raise ValueError("phase_cursor 超出课纲范围")
        completed = _sequence_of_text(self.completed_phase_ids, "completed_phase_ids")
        if completed != phase_ids[: self.phase_cursor]:
            raise ValueError("completed_phase_ids 必须是 phase_cursor 之前的有序前缀")
        witnessed = _sequence_of_text(self.witnessed_phase_ids, "witnessed_phase_ids")
        phase_indexes = {phase_id: index for index, phase_id in enumerate(phase_ids)}
        if any(phase_id not in phase_indexes for phase_id in witnessed):
            raise ValueError("witnessed_phase_ids 包含未知阶段")
        if any(phase_indexes[phase_id] < self.phase_cursor for phase_id in witnessed):
            raise ValueError("witnessed_phase_ids 不能包含已完成阶段")
        if tuple(sorted(witnessed, key=phase_indexes.__getitem__)) != witnessed:
            raise ValueError("witnessed_phase_ids 必须按课纲顺序排列")
        if (
            isinstance(self.current_phase_action_count, bool)
            or not isinstance(self.current_phase_action_count, int)
            or self.current_phase_action_count < 0
        ):
            raise ValueError("current_phase_action_count 必须为非负整数")
        if self.complete:
            if self.current_phase_action_count != 0:
                raise ValueError("课纲完成后 current_phase_action_count 必须为零")
        elif self.current_phase_action_count >= self.current_phase_required_actions:
            raise ValueError("当前阶段动作计数必须小于完成阈值")
        if self.protocol_version != APP_CURRICULUM_PROGRESS_VERSION:
            raise ValueError("AppCurriculumProgress protocol_version 不受支持")
        object.__setattr__(self, "phase_ids", phase_ids)
        object.__setattr__(self, "phase_action_requirements", requirements)
        object.__setattr__(self, "completed_phase_ids", completed)
        object.__setattr__(self, "witnessed_phase_ids", witnessed)

    @property
    def complete(self) -> bool:
        return self.phase_cursor == len(self.phase_ids)

    @property
    def current_phase_id(self) -> str | None:
        return None if self.complete else self.phase_ids[self.phase_cursor]

    @property
    def current_phase_required_actions(self) -> int:
        if self.complete:
            return 0
        return self.phase_action_requirements[self.phase_cursor][1]

    @property
    def required_action_count(self) -> int:
        return sum(item[1] for item in self.phase_action_requirements)

    def advance(self, *, phase_id: str, action_succeeded: bool) -> AppCurriculumProgress:
        """只按当前阶段的成功动作推进；失败动作不得消耗阶段进度。"""

        if self.complete:
            return self
        if phase_id != self.current_phase_id:
            raise ValueError("ActionIntent 课纲阶段与持久化 phase_cursor 不一致")
        if not action_succeeded:
            return self
        next_count = self.current_phase_action_count + 1
        if next_count < self.current_phase_required_actions:
            return AppCurriculumProgress(
                playbook_id=self.playbook_id,
                phase_ids=self.phase_ids,
                phase_action_requirements=self.phase_action_requirements,
                phase_cursor=self.phase_cursor,
                completed_phase_ids=self.completed_phase_ids,
                current_phase_action_count=next_count,
                witnessed_phase_ids=self.witnessed_phase_ids,
            )
        next_cursor = self.phase_cursor + 1
        next_progress = AppCurriculumProgress(
            playbook_id=self.playbook_id,
            phase_ids=self.phase_ids,
            phase_action_requirements=self.phase_action_requirements,
            phase_cursor=next_cursor,
            completed_phase_ids=self.phase_ids[:next_cursor],
            current_phase_action_count=0,
            witnessed_phase_ids=self.witnessed_phase_ids,
        )
        if next_progress.current_phase_id == "tap_skip":
            return next_progress.consume_witnessed_current_phase(phase_id="tap_skip")
        return next_progress

    def record_phase_witness(
        self,
        *,
        phase_id: str,
        action_succeeded: bool,
    ) -> AppCurriculumProgress:
        """记录成功动作对尚未到达阶段的见证，不能直接推进当前游标。"""

        if self.complete or not action_succeeded:
            return self
        phase_id = _require_text(phase_id, "phase_id")
        phase_indexes = {item: index for index, item in enumerate(self.phase_ids)}
        target_index = phase_indexes.get(phase_id)
        if target_index is None:
            raise ValueError("见证阶段不在 AppCurriculumProgress 中")
        if target_index <= self.phase_cursor or phase_id in self.witnessed_phase_ids:
            return self
        witnessed = tuple(
            sorted(
                (*self.witnessed_phase_ids, phase_id),
                key=phase_indexes.__getitem__,
            )
        )
        return AppCurriculumProgress(
            playbook_id=self.playbook_id,
            phase_ids=self.phase_ids,
            phase_action_requirements=self.phase_action_requirements,
            phase_cursor=self.phase_cursor,
            completed_phase_ids=self.completed_phase_ids,
            current_phase_action_count=self.current_phase_action_count,
            witnessed_phase_ids=witnessed,
        )

    def consume_witnessed_current_phase(self, *, phase_id: str) -> AppCurriculumProgress:
        """到达已见证阶段时消费一次见证并单调推进。"""

        phase_id = _require_text(phase_id, "phase_id")
        if self.complete or phase_id != self.current_phase_id:
            return self
        if phase_id not in self.witnessed_phase_ids:
            return self
        next_cursor = self.phase_cursor + 1
        return AppCurriculumProgress(
            playbook_id=self.playbook_id,
            phase_ids=self.phase_ids,
            phase_action_requirements=self.phase_action_requirements,
            phase_cursor=next_cursor,
            completed_phase_ids=self.phase_ids[:next_cursor],
            current_phase_action_count=0,
            witnessed_phase_ids=tuple(
                item for item in self.witnessed_phase_ids if item != phase_id
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "playbook_id": self.playbook_id,
            "phase_ids": list(self.phase_ids),
            "phase_action_requirements": {
                phase_id: required_actions
                for phase_id, required_actions in self.phase_action_requirements
            },
            "phase_cursor": self.phase_cursor,
            "completed_phase_ids": list(self.completed_phase_ids),
            "current_phase_action_count": self.current_phase_action_count,
            "witnessed_phase_ids": list(self.witnessed_phase_ids),
            "current_phase_id": self.current_phase_id,
            "complete": self.complete,
            "required_action_count": self.required_action_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppCurriculumProgress:
        phase_ids = _sequence_of_text(payload.get("phase_ids"), "phase_ids")
        raw_requirements = payload.get("phase_action_requirements")
        if not isinstance(raw_requirements, Mapping):
            raise ValueError("phase_action_requirements 必须为对象")
        requirements = tuple((phase_id, raw_requirements.get(phase_id)) for phase_id in phase_ids)
        return cls(
            playbook_id=payload.get("playbook_id"),
            phase_ids=phase_ids,
            phase_action_requirements=requirements,
            phase_cursor=payload.get("phase_cursor", 0),
            completed_phase_ids=_sequence_of_text(
                payload.get("completed_phase_ids", ()),
                "completed_phase_ids",
            ),
            current_phase_action_count=payload.get("current_phase_action_count", 0),
            witnessed_phase_ids=_sequence_of_text(
                payload.get("witnessed_phase_ids", ()),
                "witnessed_phase_ids",
            ),
            protocol_version=payload.get("protocol_version"),
        )


def build_app_curriculum_progress(
    playbook: AppCapturePlaybook,
    *,
    active_tabs: Sequence[str] | None = None,
) -> AppCurriculumProgress:
    """按 Playbook 与页面抽样范围冻结必达阶段和动作阈值。"""

    if not isinstance(playbook, AppCapturePlaybook):
        raise TypeError("playbook 必须为 AppCapturePlaybook")
    selected_tabs = (
        set(_unique_text_tuple(active_tabs, "active_tabs"))
        if active_tabs is not None
        else set(playbook.tabs)
    )
    tab_swipes = dict(
        playbook.tab_swipe_plan or tuple((tab, playbook.swipes_per_tab) for tab in playbook.tabs)
    )
    phases = []
    requirements: list[tuple[str, int]] = []
    for phase in build_script_curriculum_phases(playbook):
        phase_id = str(phase["phase_id"])
        if phase_id == "recover_back":
            continue
        if phase_id.startswith("tab:"):
            tab = phase_id.split(":", 1)[1]
            if tab not in selected_tabs:
                continue
            required_actions = 1 + int(tab_swipes.get(tab, playbook.swipes_per_tab))
        elif phase_id == "splash_burst":
            required_actions = max(1, playbook.splash_burst)
        elif phase_id == "ad_outbound":
            # 外链点击后，跨包边界只允许 BACK；BACK 作为恢复动作不消耗下一阶段。
            required_actions = 1
        elif phase_id == "shenqi":
            required_actions = 3
        else:
            required_actions = 1
        phases.append(phase_id)
        requirements.append((phase_id, required_actions))
    return AppCurriculumProgress(
        playbook_id=playbook.playbook_id,
        phase_ids=tuple(phases),
        phase_action_requirements=tuple(requirements),
    )


def curriculum_action_matches_current_phase(
    *,
    playbook: AppCapturePlaybook,
    progress: AppCurriculumProgress,
    action_type: str,
    semantic_terms: Sequence[str],
) -> bool:
    """确定当前成功动作是否真的满足课纲阶段，禁止任意动作冒充阶段进度。"""

    if progress.playbook_id != playbook.playbook_id:
        raise ValueError("AppCurriculumProgress 与 Playbook 不一致")
    if progress.complete:
        return False
    operation = _require_text(action_type, "action_type").casefold()
    terms = " ".join(
        _require_text(item, "semantic_terms").casefold()
        for item in semantic_terms
        if isinstance(item, str) and item.strip()
    )
    phase_by_id = {str(item["phase_id"]): item for item in build_script_curriculum_phases(playbook)}
    phase_id = progress.current_phase_id
    phase = phase_by_id.get(phase_id or "")
    if phase is None:
        raise ValueError("AppCurriculumProgress 包含 Playbook 未定义阶段")

    if phase_id is not None and phase_id.startswith("tab:"):
        tab_name = phase_id.split(":", 1)[1].casefold()
        if progress.current_phase_action_count == 0:
            return operation == "click" and (tab_name in terms or phase_id.casefold() in terms)
        return operation == "scroll"
    if phase_id == "splash_burst":
        return operation == "observe"
    if phase_id == "shenqi":
        expected_operations = ("observe", "scroll", "click")
        return operation == expected_operations[progress.current_phase_action_count]

    if phase_id == "tap_skip":
        # 不同视觉提供方可能返回 tap_skip、skip 或中文「跳过」；只接受
        # 明确的跳过语义和 click，不能把任意弹层关闭动作计入该阶段。
        return operation == "click" and _has_explicit_tap_skip_semantics(terms)

    prefer_actions = {
        str(item).strip().casefold()
        for item in phase.get("prefer_actions", ())
        if str(item).strip()
    }
    if operation not in prefer_actions:
        return False
    return any(
        _curriculum_hint_matches(
            hint=str(item),
            operation=operation,
            semantic_terms=terms,
        )
        for item in phase.get("prefer_hints", ())
    )


def candidate_explicitly_targets_tap_skip(
    *,
    action_type: str,
    risk_level: str,
    semantic_target: str,
    purpose: str,
    expected_state_hint: str | None = None,
) -> bool:
    """只用成功候选自身字段识别可提前见证的跳过动作。"""

    if not isinstance(action_type, str) or action_type.strip().casefold() != "click":
        return False
    if not isinstance(risk_level, str) or risk_level.strip().casefold() != "low":
        return False
    normalized_target = (
        semantic_target.strip().casefold() if isinstance(semantic_target, str) else ""
    )
    if normalized_target in _GENERIC_CLOSE_TARGETS:
        return False
    candidate_terms = (
        semantic_target if isinstance(semantic_target, str) else "",
        purpose if isinstance(purpose, str) else "",
        expected_state_hint if isinstance(expected_state_hint, str) else "",
    )
    return _has_explicit_tap_skip_semantics(" ".join(candidate_terms).casefold())


def extract_playbook_id_from_objective(objective: str | None) -> str | None:
    """从 playbook objective 文本解析 playbook_id。"""
    if not isinstance(objective, str) or not objective.strip():
        return None
    match = _PLAYBOOK_ID_RE.search(objective)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def _phase(
    phase_id: str,
    title: str,
    must_do: str,
    *,
    prefer_hints: list[str],
    prefer_actions: list[str],
    forbid: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "phase_id": phase_id,
        "title": title,
        "must_do": must_do,
        "prefer_hints": prefer_hints,
        "prefer_actions": prefer_actions,
    }
    if forbid:
        item["forbid"] = forbid
    return item


def _tab_phases(
    playbook: AppCapturePlaybook,
    *,
    extra_forbid: str = "",
    allow_profile_entry: bool = False,
) -> list[dict[str, Any]]:
    """按每栏下滑次数展开底栏多帧阶段。"""
    plan = playbook.tab_swipe_plan or tuple((tab, playbook.swipes_per_tab) for tab in playbook.tabs)
    # allow_profile_entry：允许只读进入「我的」以覆盖 profile／settings／payment，仍禁止支付提交。
    base_forbid = (
        "禁止充值提交／下载／安装／登录提交"
        if allow_profile_entry
        else "禁止点「我的」／充值／下载／安装"
    )
    forbid = base_forbid + (f"；{extra_forbid}" if extra_forbid else "")
    phases: list[dict[str, Any]] = []
    for tab, swipes in plan:
        phases.append(
            _phase(
                f"tab:{tab}",
                f"底栏「{tab}」多帧",
                (
                    f"切换底栏「{tab}」→ 首屏截图 → 左缘／中轴下滑约 {swipes} 次，"
                    "每次下滑后截图；画面停滞再换下一栏"
                ),
                prefer_hints=[f"tab:{tab}", "swipe_left_edge", "scroll"],
                prefer_actions=["click", "scroll", "observe"],
                forbid=forbid,
            )
        )
    return phases


def _phases_91cg(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """严格对齐 untitled.py 主流程。

    1 等稳截图 → 2 中部广告外链 → 3 点右上「跳过」(脚本变量 close_1/2/3，模板图即跳过)
    → 4 白圈X+橘色确认 → 5 社区/片库/福利/AI科技。
    """
    return [
        _phase(
            "wait_stable",
            "等待稳定后截图",
            "启动后等待约 12s，OBSERVE 保留 before_find_ad；此阶段禁止点击",
            prefer_hints=["splash_burst", "observe", "wait_stable"],
            prefer_actions=["observe"],
            forbid="此阶段禁止乱点内容区",
        ),
        _phase(
            "ad_outbound",
            "中部广告外链取证",
            (
                "依序匹配中部广告位(ad_1/2/3/4)，命中即点击→落地页截图→连续 BACK 回 App；"
                "找不到则 OBSERVE 留证后继续"
            ),
            # 勿把 back 放进 prefer_hints：缺 ad_outbound 候选时会误抬「系统返回键」直接 FAILED
            prefer_hints=["ad_outbound", "observe"],
            prefer_actions=["click", "observe"],
            forbid="禁止下载／安装；外链轮次必须有限",
        ),
        _phase(
            "tap_skip",
            "点右上「跳过」关广告层",
            (
                "对齐 untitled.py 的 close_1/close_2/close_3："
                "模板图就是右上「跳过」，外链返回后必须 touch 跳过并截 after_close；"
                "禁止当成没有跳过"
            ),
            prefer_hints=["tap_skip", "skip"],
            prefer_actions=["click"],
            forbid="禁止点广告本体；本阶段只点右上跳过",
        ),
        _phase(
            "close_popup",
            "白圈关闭与确认",
            "白圈透明底 X 用中下坐标连点；再点橘色确认（对齐 untitled.py）",
            prefer_hints=["close_popup_x", "confirm"],
            prefer_actions=["click"],
        ),
        *_tab_phases(playbook),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器／外链详情时只 BACK，回到目标 App 后继续未完成栏",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_hlapp(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled1.py：对齐 hlapp_7_types——年龄→开屏→跳过→弹层→六栏→两详情。

    刻意不含我的／设置／充值／发帖：与业务分母一致，避免预算耗在不计入覆盖的页。
    """
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "仅首次安装：有黄框／年龄文案才点确认；无则静默跳过",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告本体",
        ),
        _phase(
            "splash_burst",
            "开屏本体",
            f"零触控截取开屏本体约 {playbook.splash_burst} 帧；此期间禁止任何点击",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "开屏本体截完后只点右上「跳过」一次，再截图确认离开开屏",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "中间广告与公告",
            "中间广告先截后点 X；系统公告只关／确认，不作广告证据",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
            forbid="禁止点广告内容",
        ),
        *_tab_phases(playbook, allow_profile_entry=False),
        _phase(
            "content_detail",
            "内容详情",
            "在精选／内容列表打开一个普通内容卡片，保留详情首帧",
            prefer_hints=["content_detail", "内容详情", "打开内容", "精选"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "content_detail_back",
            "返回内容列表",
            "用系统 BACK 返回列表，不点详情内推荐位",
            prefer_hints=["back", "返回内容列表"],
            prefer_actions=["back"],
        ),
        _phase(
            "video_detail",
            "视频详情",
            "在片库打开一个视频卡片，保留详情首帧",
            prefer_hints=["video_detail", "视频详情", "打开视频", "片库"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "video_detail_back",
            "返回片库",
            "用系统 BACK 返回片库列表",
            prefer_hints=["back", "返回片库"],
            prefer_actions=["back"],
        ),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器只 BACK，回到 App 后继续未完成栏",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_hjll(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """海角三栏：年龄→开屏→跳过→弹层→首页／视频／海角→两详情。"""
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "仅首次安装：有黄框／年龄文案才点确认；无则静默跳过",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告本体",
        ),
        _phase(
            "splash_burst",
            "开屏本体",
            f"零触控截取开屏本体约 {playbook.splash_burst} 帧；此期间禁止任何点击",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "开屏本体截完后只点右上「跳过」／「关闭」一次，再截图确认离开开屏",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "中间广告与公告",
            "中间广告先截后点 X；系统公告只关／确认",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
            forbid="禁止点广告内容",
        ),
        *_tab_phases(playbook, allow_profile_entry=True),
        _phase(
            "content_detail",
            "内容详情",
            "在首页内容区打开一个普通内容卡片，保留详情首帧",
            prefer_hints=["content_detail", "内容详情", "打开内容", "首页"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "content_detail_back",
            "返回首页",
            "用系统 BACK 返回，不点详情内推荐位",
            prefer_hints=["back", "返回首页"],
            prefer_actions=["back"],
        ),
        _phase(
            "video_detail",
            "视频详情",
            "在视频栏打开一个视频卡片，保留详情首帧",
            prefer_hints=["video_detail", "视频详情", "打开视频", "视频"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "video_detail_back",
            "返回视频栏",
            "用系统 BACK 返回视频列表",
            prefer_hints=["back", "返回视频"],
            prefer_actions=["back"],
        ),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器只 BACK，回到 App 后继续未完成栏",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_pc51(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """51品茶三栏：年龄→开屏→跳过→弹层→首页／茶馆／视频→两详情。"""
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "仅首次安装：有黄框／年龄文案才点确认；无则静默跳过",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告本体",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"零触控连拍最多约 {playbook.splash_burst} 帧开屏（去重）；此期间禁止任何点击",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上「跳过」一次，再截图确认离开开屏",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "中间广告与公告",
            "中间广告先截后点 X；系统公告只关／确认",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
            forbid="禁止点广告内容",
        ),
        *_tab_phases(playbook, allow_profile_entry=False),
        _phase(
            "content_detail",
            "内容详情",
            "在茶馆列表打开一个普通内容卡片，保留详情首帧",
            prefer_hints=["content_detail", "内容详情", "打开内容", "茶馆"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "content_detail_back",
            "返回茶馆",
            "用系统 BACK 返回茶馆列表",
            prefer_hints=["back", "返回茶馆"],
            prefer_actions=["back"],
        ),
        _phase(
            "video_detail",
            "视频详情",
            "在视频栏打开一个视频卡片，保留详情首帧",
            prefer_hints=["video_detail", "视频详情", "打开视频", "视频"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "video_detail_back",
            "返回视频栏",
            "用系统 BACK 返回视频列表",
            prefer_hints=["back", "返回视频"],
            prefer_actions=["back"],
        ),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器只 BACK，回到 App 后继续未完成栏",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_generic5(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """通用五栏成人内容：清障→底栏→两详情；关闭线路久等。"""
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "有年龄门则确认；无则静默跳过",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告本体",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"零触控连拍最多约 {playbook.splash_burst} 帧开屏",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上「跳过」一次",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "弹层关闭",
            "中间广告先截后点 X；公告只关",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
            forbid="禁止点广告内容",
        ),
        *_tab_phases(playbook, allow_profile_entry=False),
        _phase(
            "content_detail",
            "内容详情",
            "在发现／精选／首页打开内容卡片",
            prefer_hints=["content_detail", "内容详情", "打开内容", "发现", "精选"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "content_detail_back",
            "返回列表",
            "用系统 BACK 返回",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
        _phase(
            "video_detail",
            "视频详情",
            "在视频栏打开视频卡片",
            prefer_hints=["video_detail", "视频详情", "打开视频", "视频"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值提交",
        ),
        _phase(
            "video_detail_back",
            "返回视频栏",
            "用系统 BACK 返回",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器只 BACK",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_hlwapp(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled2.py：年龄→等线路→开屏2→跳过→初始化→弹层／神器→五栏。"""
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "若有年龄门则确认；过程图可不作广告证据",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告",
        ),
        _phase(
            "wait_line",
            "等待线路检测",
            "零触控等待线路检测结束",
            prefer_hints=["wait_line_check", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点直连／翻墙／重装",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"开屏最多约 {playbook.splash_burst} 帧；有跳过文案时可提前结束连拍",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上跳过，等待画面变化",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "wait_init",
            "等待数据初始化",
            "跳过后若仍「数据初始化」则零触控 OBSERVE 等待结束",
            prefer_hints=["wait_line_check", "observe"],
            prefer_actions=["observe"],
        ),
        _phase(
            "close_popup",
            "中间弹层",
            "中间层先截后关；可多轮",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
        ),
        _phase(
            "shenqi",
            "神器甄选",
            "神器甄选：截图→左缘下滑再截→关闭；勿点下载安装",
            prefer_hints=["shenqi", "swipe_left_edge", "close_popup_x"],
            prefer_actions=["observe", "scroll", "click"],
            forbid="禁止下载／安装",
        ),
        *_tab_phases(playbook),
        _phase(
            "content_detail",
            "内容详情",
            "只打开一个非广告、非下载的普通内容卡片，保留详情首帧",
            prefer_hints=["content_detail", "内容详情", "打开内容"],
            prefer_actions=["click"],
            forbid="禁止点广告本体／下载／安装／充值",
        ),
        _phase(
            "content_detail_back",
            "返回内容列表",
            "用系统 BACK 返回上一个 App 内列表，不点详情内推荐位",
            prefer_hints=["back", "返回内容列表"],
            prefer_actions=["back"],
        ),
        _phase(
            "profile",
            "个人中心",
            "只点底栏「我的」／个人中心并保留首帧，不进入任何充值或安装入口",
            prefer_hints=["profile", "tab:我的", "个人中心", "我的"],
            prefer_actions=["click"],
            forbid="禁止充值／支付／下载／安装／登录提交",
        ),
        _phase(
            "recover_back",
            "误入恢复",
            "误进 Chrome 只 BACK／拉回目标包后继续课纲",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_sns_91sp(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled3.py：年龄→线路→开屏2→跳过→中广告→公告确认→四栏。"""
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "可选年龄确认；绝不点广告本体",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告",
        ),
        _phase(
            "wait_line",
            "等待线路检测",
            "零触控等待线路结束",
            prefer_hints=["wait_line_check", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点我直连",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"开屏倒数期间只截约 {playbook.splash_burst} 帧",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "右上跳过一次，仅进入 App",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "中间广告",
            "中间广告先截后点下方 X；可多轮",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
            forbid="禁止点广告内容",
        ),
        _phase(
            "system_notice",
            "系统公告",
            "系统公告不截广告图，只点确认",
            prefer_hints=["confirm", "close_popup_x"],
            prefer_actions=["click"],
        ),
        *_tab_phases(playbook),
        _phase(
            "recover_back",
            "误入恢复",
            "误进外链只 BACK",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_sns_911bl(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled4.py：年龄→线路→开屏3→跳过→弹层多轮→左侧确认→五栏。"""
    return [
        _phase(
            "age_gate",
            "年龄确认",
            "可选年龄确认",
            prefer_hints=["age_gate"],
            prefer_actions=["click", "observe"],
            forbid="勿点广告",
        ),
        _phase(
            "wait_line",
            "等待线路检测",
            "零触控等待线路结束",
            prefer_hints=["wait_line_check", "observe"],
            prefer_actions=["observe"],
            forbid="禁止重新安装",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"开屏连拍最多约 {playbook.splash_burst} 帧",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "右上跳过",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "弹层多轮",
            "广告先截后关 X，可多次出现；切栏后若再弹仍先截后关",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
        ),
        _phase(
            "system_notice",
            "系统公告",
            "公告点左侧「确认」；禁止一键复制",
            prefer_hints=["confirm"],
            prefer_actions=["click"],
            forbid="禁止一键复制",
        ),
        *_tab_phases(playbook),
        _phase(
            "recover_back",
            "误入恢复",
            "误进外链只 BACK",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_sns_51aw(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled5.py：无年龄；禁点击开炮；仅首页／吃瓜／大赛。"""
    return [
        _phase(
            "wait_line",
            "等待线路检测",
            "零触控等待线路结束",
            prefer_hints=["wait_line_check", "observe"],
            prefer_actions=["observe"],
            forbid="禁止直连／重装",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"开屏约 {playbook.splash_burst} 帧；禁止点「点击开炮」与广告本体",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止「点击开炮」／广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上跳过",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "中间弹层",
            "中间弹层先截后点中央 X，直到首页图墙露出",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
        ),
        *_tab_phases(playbook, extra_forbid="跳过 AI科技／我的；首页禁点图墙图标"),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器只 BACK",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_sns_hjsq(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled6.py：线路→开屏→跳过可重试→弹层／我知道了→四栏高下滑。"""
    return [
        _phase(
            "wait_line",
            "等待线路检测",
            "零触控等待线路结束",
            prefer_hints=["wait_line_check", "observe"],
            prefer_actions=["observe"],
            forbid="禁止直连／重装",
        ),
        _phase(
            "splash_burst",
            "开屏连拍",
            f"开屏约 {playbook.splash_burst} 帧",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="禁止点广告本体",
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上跳过；可重试到跳过消失；栏目前若仍像开屏再跳过一次",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        ),
        _phase(
            "close_popup",
            "中间弹层与公告",
            "弹层先截后关 X；公告点「我知道了」不截广告图",
            prefer_hints=["close_popup_x", "confirm"],
            prefer_actions=["click", "observe"],
        ),
        *_tab_phases(playbook, extra_forbid="禁止 AI科技／我的；栏内零内容点击"),
        _phase(
            "recover_back",
            "误入恢复",
            "误进 Chrome 只 BACK",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_dx093j(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """untitled7.py：高速开屏连拍；无跳过优先；主墙禁乱点 X；五栏。"""
    return [
        _phase(
            "splash_burst",
            "高速开屏连拍",
            (
                f"启动后几乎不等线路，高速 OBSERVE 连拍开屏（约保留≤{playbook.splash_burst} 帧）；"
                "跳过品牌／灰空壳／烂帧"
            ),
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
            forbid="勿先长时间等线路（会错过开屏）",
        ),
        _phase(
            "close_popup",
            "中间弹层与图标网",
            ("弹层：截→只点 X／知道了；图标网格可上滑／左缘滑多截再关；主墙无压暗时禁止乱点 X"),
            prefer_hints=["close_popup_x", "swipe_left_edge"],
            prefer_actions=["click", "scroll", "observe"],
            forbid="禁止应用推荐；主墙无遮罩禁乱点 X",
        ),
        *_tab_phases(
            playbook,
            extra_forbid="暗网「立即开通」只截不点；误进浏览器 BACK",
        ),
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器只 BACK，回到 App 后继续未完成栏",
            prefer_hints=["back"],
            prefer_actions=["back"],
        ),
    ]


def _phases_generic(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """未命中专用包时的通用课纲。"""
    phases: list[dict[str, Any]] = []
    if playbook.wait_line_check:
        phases.append(
            _phase(
                "wait_line",
                "等待线路／加载",
                "零触控 OBSERVE 等待加载结束",
                prefer_hints=["wait_line_check", "observe"],
                prefer_actions=["observe"],
                forbid="禁止点直连、翻墙、重装",
            )
        )
    if playbook.age_gate:
        phases.append(
            _phase(
                "age_gate",
                "年龄确认",
                "若有年龄门则点击确认",
                prefer_hints=["age_gate"],
                prefer_actions=["click"],
            )
        )
    if playbook.splash_burst > 0:
        phases.append(
            _phase(
                "splash_burst",
                "开屏连拍",
                f"至少 OBSERVE／截取约 {playbook.splash_burst} 帧开屏",
                prefer_hints=["splash_burst", "observe"],
                prefer_actions=["observe"],
                forbid="禁止点广告本体",
            )
        )
    phases.append(
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上「跳过」；跳过后再截图确认",
            prefer_hints=["tap_skip"],
            prefer_actions=["click"],
        )
    )
    phases.append(
        _phase(
            "close_popup",
            "关闭弹层",
            "中间广告先截后点中央 X；系统公告点确认但不作广告证据",
            prefer_hints=["close_popup_x"],
            prefer_actions=["click", "observe"],
        )
    )
    if playbook.allow_ad_outbound:
        phases.append(
            _phase(
                "ad_outbound",
                "外链广告取证（有限）",
                "仅限允许时：点中部广告→落地截图→BACK 回 App",
                prefer_hints=["ad_outbound", "observe"],
                prefer_actions=["click", "back", "observe"],
            )
        )
    phases.extend(_tab_phases(playbook))
    phases.append(
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器／外链详情时只 BACK",
            prefer_hints=["back"],
            prefer_actions=["back"],
        )
    )
    return phases


def _phases_xingba(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """杏吧：开屏截图 → 跳过 → 弹层 → 五格底栏硬覆盖（含只读「我的」）。"""

    phases: list[dict[str, Any]] = [
        _phase(
            "splash_burst",
            "开屏连拍",
            f"零触控连拍最多约 {playbook.splash_burst} 帧开屏（去重）；此期间禁止任何点击",
            prefer_hints=["splash_burst", "observe"],
            prefer_actions=["observe"],
        ),
        _phase(
            "tap_skip",
            "跳过开屏",
            "只点右上「进入／跳过」；禁止点广告本体",
            prefer_hints=["tap_skip", "进入"],
            prefer_actions=["click"],
            forbid="禁止点广告本体进外链",
        ),
        _phase(
            "close_popup",
            "关闭弹层（有限）",
            "可见底栏未巡完时优先切底栏；关层失败不得反复点广告中心",
            prefer_hints=["close_popup_x", "tab:"],
            prefer_actions=["click", "observe"],
            forbid="禁止点广告本体进外链",
        ),
    ]
    phases.extend(_tab_phases(playbook, allow_profile_entry=True))
    phases.append(
        _phase(
            "recover_back",
            "误入恢复",
            "误进浏览器／外链详情时只 BACK／warm_restore",
            prefer_hints=["back", "warm_restore"],
            prefer_actions=["back"],
        )
    )
    return phases


_PHASE_BUILDERS = {
    "91cg": _phases_91cg,
    "hlapp": _phases_hlapp,
    "hjll": _phases_hjll,
    "pc51": _phases_pc51,
    "porn91": _phases_generic5,
    "erbull": _phases_generic5,
    "hlwapp": _phases_hlwapp,
    "sns_91sp": _phases_sns_91sp,
    "sns_911bl": _phases_sns_911bl,
    "sns_51aw": _phases_sns_51aw,
    "sns-hjsq": _phases_sns_hjsq,
    "dx093j": _phases_dx093j,
    "xingba": _phases_xingba,
}


def build_script_curriculum_phases(playbook: AppCapturePlaybook) -> list[dict[str, Any]]:
    """按包专属 playbook 展开有序课纲阶段（供 LLM 学习选候选）。"""
    if not isinstance(playbook, AppCapturePlaybook):
        raise TypeError("playbook 必须为 AppCapturePlaybook")
    builder = _PHASE_BUILDERS.get(playbook.playbook_id, _phases_generic)
    return builder(playbook)


# 91cg／untitled.py 历史证据帧名（供 LLM 学习「成功长什么样」，不执行 Airtest）
_EXPECTED_SHOTS_91CG: tuple[str, ...] = (
    "before_find_ad",
    "chrome_after_ad_click*",
    "back_to_app_*",
    "after_close_*",
    "shequ_top/scroll_*/bottom",
    "pianku_top/scroll_*/bottom",
    "fuli_top/scroll_*/bottom",
    "ai_tech_top/scroll_1",
)


def build_script_curriculum_payload(
    playbook: AppCapturePlaybook,
    *,
    progress: AppCurriculumProgress | None = None,
) -> dict[str, Any]:
    """按独立 phase_cursor 生成 Explorer 课纲，禁止借用页面深度推进阶段。"""

    progress = progress or build_app_curriculum_progress(playbook)
    if progress.playbook_id != playbook.playbook_id:
        raise ValueError("AppCurriculumProgress 与 Playbook 不一致")
    phase_by_id = {str(item["phase_id"]): item for item in build_script_curriculum_phases(playbook)}
    missing = [phase_id for phase_id in progress.phase_ids if phase_id not in phase_by_id]
    if missing:
        raise ValueError("AppCurriculumProgress 包含 Playbook 未定义阶段")
    phases = [phase_by_id[phase_id] for phase_id in progress.phase_ids]
    current = _current_phase_payload(
        phase_by_id[progress.current_phase_id] if progress.current_phase_id is not None else None,
        progress=progress,
    )
    tab_plan = playbook.tab_swipe_plan or tuple(
        (tab, playbook.swipes_per_tab) for tab in playbook.tabs
    )
    payload: dict[str, Any] = {
        "playbook_id": playbook.playbook_id,
        "script_ref": playbook.script_ref,
        "match_reason": playbook.match_reason,
        "learning_rule": (
            "这是从历史 Airtest 脚本（untitled*.py）抽象的动作课纲，不是可执行脚本。"
            "你必须像在「学习」该脚本：按 phases 顺序推进多帧截图；"
            "每轮只能从 candidates 选 candidate_id；"
            "禁止只截开屏广告就停止；"
            "单次点击失败后改选 SCROLL／下一栏／OBSERVE，不要反复空点同一跳过；"
            "purpose 中写明正在推进的 phase_id。"
            "运行时会按 current_phase_hint.prefer_hints 确定性推进，避免只停开屏。"
        ),
        "tabs": list(playbook.tabs),
        "tab_swipe_plan": [{"tab": tab, "swipes": swipes} for tab, swipes in tab_plan],
        "swipes_per_tab": playbook.swipes_per_tab,
        "splash_burst": playbook.splash_burst,
        "forbid_hints": list(playbook.forbid_hints),
        "script_notes": list(playbook.script_notes),
        "phases": phases,
        "current_phase_hint": current,
        "phase_progress": progress.to_dict(),
    }
    if playbook.playbook_id == "91cg":
        payload["expected_evidence_shots"] = list(_EXPECTED_SHOTS_91CG)
        payload["learning_rule"] = (
            str(payload["learning_rule"])
            + " 成功证据应对齐 expected_evidence_shots。"
            + " close_1/2/3 模板图就是右上「跳过」，外链返回后必须执行 tap_skip。"
        )
    return payload


def format_curriculum_objective_section(playbook: AppCapturePlaybook) -> str:
    """写入 objective 文本的课纲段落，供模型在无 structured 字段时仍能学习。"""
    phases = build_script_curriculum_phases(playbook)
    lines = [
        f"脚本动作课纲（参照 {playbook.script_ref}／{playbook.playbook_id}，必须按序多帧推进，禁止只停开屏）：",
    ]
    for index, phase in enumerate(phases, start=1):
        forbid = phase.get("forbid")
        extra = f"（禁止：{forbid}）" if forbid else ""
        lines.append(f"{index}. [{phase['phase_id']}] {phase['title']}：{phase['must_do']}{extra}")
    if playbook.script_notes:
        lines.append("脚本要点：" + "；".join(playbook.script_notes))
    if playbook.forbid_hints:
        lines.append("全局禁止：" + "；".join(playbook.forbid_hints))
    return "\n".join(lines)


def resolve_playbook_by_id(playbook_id: str | None) -> AppCapturePlaybook | None:
    """按 playbook_id 回查已注册 playbook（供 Explorer 从 objective 还原课纲）。"""
    if not playbook_id:
        return None
    from services.api.app.application.app_capture_playbook import (
        _BY_SCRIPT,
        _DEFAULT,
    )

    if playbook_id == _DEFAULT.playbook_id:
        return _DEFAULT
    for item in _BY_SCRIPT.values():
        if item.playbook_id == playbook_id:
            return item
    return None


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _unique_text_tuple(values: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} 必须为字符串序列")
    result: list[str] = []
    for item in values:
        value = _require_text(item, field_name)
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError(f"{field_name} 不能为空")
    return tuple(result)


def _sequence_of_text(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} 必须为字符串序列")
    return _unique_text_tuple(value, field_name) if value else ()


def _has_explicit_tap_skip_semantics(terms: str) -> bool:
    if not any(_contains_tap_skip_marker(terms, marker) for marker in _TAP_SKIP_SEMANTIC_MARKERS):
        return False
    return not any(marker in terms for marker in _TAP_SKIP_EXCLUSION_MARKERS)


def _contains_tap_skip_marker(terms: str, marker: str) -> bool:
    if marker == "跳过":
        return marker in terms
    return re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", terms) is not None


def _curriculum_hint_matches(
    *,
    hint: str,
    operation: str,
    semantic_terms: str,
) -> bool:
    token = hint.strip().casefold()
    if not token:
        return False
    if token in {"back", "observe"}:
        return operation == token
    if token == operation:
        return True
    return token in semantic_terms


def _current_phase_payload(
    phase: Mapping[str, Any] | None,
    *,
    progress: AppCurriculumProgress,
) -> dict[str, Any] | None:
    if phase is None:
        return None
    result = dict(phase)
    if progress.current_phase_id is not None and progress.current_phase_id.startswith("tab:"):
        tab_name = progress.current_phase_id.split(":", 1)[1]
        if progress.current_phase_action_count == 0:
            result["prefer_hints"] = [progress.current_phase_id, tab_name]
            result["prefer_actions"] = ["click"]
        else:
            result["prefer_hints"] = ["swipe_left_edge", "scroll"]
            result["prefer_actions"] = ["scroll"]
    elif progress.current_phase_id == "shenqi":
        operation = ("observe", "scroll", "click")[progress.current_phase_action_count]
        result["prefer_actions"] = [operation]
    return result
