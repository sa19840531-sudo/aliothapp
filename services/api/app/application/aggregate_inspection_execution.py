"""聚合 Work 的领取、证据读取、确定性判定与终态合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.application.rule_capability_facts import modal_elements
from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.domain.evidence.presentation import (
    PresentationAssessment,
    TopmostLayerKind,
)
from services.api.app.domain.protocol import stable_protocol_hash


class AggregateWakeupClaimDisposition(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    STALE = "stale"
    BUSY = "busy"


@dataclass(frozen=True, kw_only=True)
class AggregateExecutionLease:
    work_item_id: str
    task_id: str
    evidence_input_set_id: str
    input_hash: str
    rule_id: str
    rule_code: str
    rule_contract_hash: str
    scope: str
    scope_key: str
    finalization_epoch: int
    consumer_agent_run_id: str
    lease_token: str
    attempt_no: int
    row_version: int


@dataclass(frozen=True, kw_only=True)
class AggregateWakeupClaim:
    disposition: AggregateWakeupClaimDisposition
    lease: AggregateExecutionLease | None = None


@dataclass(frozen=True, kw_only=True)
class AggregateJudgeContext:
    lease: AggregateExecutionLease
    input_set: EvidenceInputSet
    observation_packets: Mapping[str, Mapping[str, Any]]
    # 成员清单在捕获提交时封存，彼时判定包尚未产生，聚合成员的包引用
    # 结构性为空；此处按 capture 维度兜底提供该捕获最新 PASS 观察包。
    capture_packets: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    journey_input: Mapping[str, Any] | None = None
    rule_payload: Mapping[str, Any] = field(default_factory=dict)
    green_evidence: Any | None = None


@dataclass(frozen=True, kw_only=True)
class AggregateJudgement:
    execution_status: str
    result_summary: str
    problem_desc: str | None
    confidence: float | None
    review_status: str | None
    review_reason: str | None
    error_code: str | None
    wrapper_payload: Mapping[str, Any]
    evidence_artifact_ids: tuple[str, ...]

    @property
    def result_hash(self) -> str:
        return stable_protocol_hash(self.to_result_payload())

    def to_result_payload(self) -> dict[str, Any]:
        return {
            "execution_status": self.execution_status,
            "result_summary": self.result_summary,
            "problem_desc": self.problem_desc,
            "confidence": self.confidence,
            "review_status": self.review_status,
            "review_reason": self.review_reason,
            "error_code": self.error_code,
            "wrapper_payload": dict(self.wrapper_payload),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
        }


class AggregateInspectionExecutionRepository(Protocol):
    async def claim_for_wakeup(
        self,
        *,
        work_item_id: str,
        task_id: str,
        message_version: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> AggregateWakeupClaim: ...

    async def load_judge_context(
        self,
        *,
        lease: AggregateExecutionLease,
    ) -> AggregateJudgeContext: ...

    async def complete_judgement(
        self,
        *,
        context: AggregateJudgeContext,
        judgement: AggregateJudgement,
    ) -> None: ...

    async def retry_or_fail(
        self,
        *,
        lease: AggregateExecutionLease,
        error: Exception,
        retry_delay_seconds: int,
    ) -> None: ...


class AggregateInspectionExecutor:
    """消费一条权威聚合 Work；所有异常先持久化重试/失败再 ACK。"""

    def __init__(
        self,
        *,
        repository: AggregateInspectionExecutionRepository,
        lease_owner: str,
        lease_seconds: int,
        retry_delay_seconds: int,
    ) -> None:
        if not lease_owner.strip() or lease_seconds <= 0 or retry_delay_seconds <= 0:
            raise ValueError("聚合 Consumer 租约与重试配置无效")
        self._repository = repository
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds

    async def execute_wakeup(
        self,
        *,
        work_item_id: str,
        task_id: str,
        message_version: int,
    ) -> bool:
        claim = await self._repository.claim_for_wakeup(
            work_item_id=work_item_id,
            task_id=task_id,
            message_version=message_version,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if claim.disposition in {
            AggregateWakeupClaimDisposition.COMPLETED,
            AggregateWakeupClaimDisposition.STALE,
        }:
            return True
        if claim.disposition is AggregateWakeupClaimDisposition.BUSY:
            return False
        if claim.lease is None:
            raise RuntimeError("聚合 Work claim 缺少租约")
        try:
            context = await self._repository.load_judge_context(lease=claim.lease)
            judgement = evaluate_aggregate_context(context)
            await self._repository.complete_judgement(
                context=context,
                judgement=judgement,
            )
        except Exception as exc:  # noqa: BLE001 - 必须先落重试/失败事实
            await self._repository.retry_or_fail(
                lease=claim.lease,
                error=exc,
                retry_delay_seconds=self._retry_delay_seconds,
            )
        return True


def evaluate_aggregate_context(context: AggregateJudgeContext) -> AggregateJudgement:
    """执行本版本已注册的有界聚合策略；不支持的策略统一证据不足。"""

    if context.lease.scope_key in {"green_interaction_stability.v1", "green_temporal_rules.v1"}:
        from services.api.app.application.green_rule_evidence import (
            evaluate_green_aggregate_context,
        )

        return evaluate_green_aggregate_context(context)
    if context.lease.scope == "journey" and context.lease.scope_key == "journey_observation":
        from services.api.app.application.journey_rule_judge import evaluate_journey_context

        return evaluate_journey_context(context)
    if context.lease.scope_key == "toast_style":
        return _evaluate_toast_style(context)
    if context.lease.scope_key == "home_popup_sequence":
        return _evaluate_home_popup_sequence(context)
    if context.lease.scope_key == "official_group_entry":
        return _evaluate_official_group_entry(context)
    return _insufficient(
        context,
        code="AGGREGATE_STRATEGY_EVIDENCE_INSUFFICIENT",
        reason=f"聚合策略 {context.lease.scope_key} 尚无可自动下结论的有界事实适配器",
    )


def _member_packets(
    context: AggregateJudgeContext,
    member: Any,
) -> list[Mapping[str, Any]]:
    """读取成员可信观察包：显式引用优先，无引用时按 capture 兜底。"""
    packets = [
        packet
        for packet_id in member.observation_packet_ids
        if isinstance(packet := context.observation_packets.get(packet_id), Mapping)
    ]
    if packets:
        return packets
    fallback = context.capture_packets.get(member.capture_id)
    if isinstance(fallback, Mapping):
        return [fallback]
    return []


def _evaluate_toast_style(context: AggregateJudgeContext) -> AggregateJudgement:
    from services.api.app.application.toast_samples import valid_toast_sample

    observed_monitor_count = 0
    signatures: list[dict[str, Any]] = []
    seen_events: set[tuple[str, str]] = set()
    for packet in (
        packet
        for member in context.input_set.members
        for packet in _member_packets(context, member)
    ):
        facts = packet.get("facts") if isinstance(packet, Mapping) else None
        facts = facts if isinstance(facts, Mapping) else {}
        actions = facts.get("action_log")
        actions = actions if isinstance(actions, list) else []
        for action_index, action in enumerate(actions):
            if not isinstance(action, Mapping):
                continue
            observation = action.get("transient_message_observation")
            observation = observation if isinstance(observation, Mapping) else {}
            if observation.get("status") == "observed":
                observed_monitor_count += 1
            else:
                continue
            messages = action.get("transient_messages")
            messages = messages if isinstance(messages, list) else []
            action_key = str(action.get("action_execution_id") or action.get("intent_id")
                             or f"{id(packet)}:{action_index}")
            seen_roots: set[str] = set()
            for message in messages[:50]:
                if not isinstance(message, Mapping) or not valid_toast_sample(message):
                    continue
                event_key = (
                    str(message.get("observation_id") or action_key), str(message["event_id"]),
                )
                root_key = stable_protocol_hash({
                    "text": message["text"], "rect": message["rect"],
                    "first_seen_ms": message.get("first_seen_ms"),
                })
                if event_key in seen_events or root_key in seen_roots:
                    continue
                seen_events.add(event_key)
                seen_roots.add(root_key)
                style = message.get("style")
                style = style if isinstance(style, Mapping) else {}
                signatures.append(
                    {
                        "position_zone": message.get("position_zone"),
                        "font_size": style.get("font_size"),
                        "text_color": style.get("text_color"),
                        "background_color": style.get("background_color"),
                        "background_opacity": style.get("background_opacity"),
                    }
                )
    if observed_monitor_count <= 0:
        return _insufficient(
            context,
            code="TOAST_MONITOR_NOT_OBSERVED",
            reason="InputSet 成员没有可验证的瞬态提示观察窗口",
        )
    if len(signatures) < 2:
        return _insufficient(
            context,
            code="TOAST_STYLE_SAMPLE_INSUFFICIENT",
            reason="Toast 样式一致性至少需要两个真实提示样本",
        )
    normalized = {stable_protocol_hash(item) for item in signatures}
    passed = len(normalized) == 1
    artifacts = _artifact_ids(context.input_set)
    return AggregateJudgement(
        execution_status="pass" if passed else "fail",
        result_summary=(
            "任务内 Toast 样式字段一致" if passed else "任务内 Toast 的位置或视觉样式字段不一致"
        ),
        problem_desc=None if passed else "检测到多个不同的 Toast 样式签名",
        confidence=1.0,
        review_status=None,
        review_reason=None,
        error_code=None,
        wrapper_payload={
            "protocol_version": "aggregate-observation-packet.v1",
            "scope": context.lease.scope,
            "scope_key": context.lease.scope_key,
            "input_set_id": context.input_set.input_set_id,
            "member_count": len(context.input_set.members),
            "observed_monitor_count": observed_monitor_count,
            "sample_count": len(signatures),
            "style_signatures": signatures,
            "uniform": passed,
        },
        evidence_artifact_ids=artifacts,
    )


_POPUP_PAGE_STATE_MARKERS = ("popup", "modal", "overlay", "dialog", "弹窗", "遮罩")


def _frame_shows_popup(packet: Mapping[str, Any]) -> bool:
    """这一帧是否有弹窗盖在页面上：页面识别给出的弹窗状态最先，其次展示层评估，类名识别兜底。

    页面识别说是弹窗状态就算弹窗：09-22 本地黑料网三帧首页都是弹窗状态，采集元数据却声明
    「没有遮挡」，只看展示层会把整条弹窗链判成「首页帧没有弹窗」。

    App 的弹窗是视觉识别的，节点类名里没有 dialog / popup 字样，只能靠采集侧声明的
    遮挡（is_obstructed 已并入展示层评估）；H5 / Web 的类名识别会把播放器遮罩当弹窗，
    结构命中测试确认最上层就是页面时以它为准（2026-09-22 问题表：这条规则在首页
    一律记「未出现连续弹窗」）。
    """
    page = packet.get("page") if isinstance(packet, Mapping) else None
    page_state = str(page.get("page_state") or "").casefold() if isinstance(page, Mapping) else ""
    if any(marker in page_state for marker in _POPUP_PAGE_STATE_MARKERS):
        return True
    facts = packet.get("facts") if isinstance(packet, Mapping) else None
    facts = facts if isinstance(facts, Mapping) else {}
    presentation = PresentationAssessment.from_dict(facts.get("presentation"))
    if presentation is not None:
        if presentation.occluding_layer_present:
            return True
        if (
            presentation.topmost_layer_kind is TopmostLayerKind.PAGE
            and presentation.hit_test_status == "available"
        ):
            return False
    return bool(modal_elements(dict(packet)))


def _evaluate_home_popup_sequence(context: AggregateJudgeContext) -> AggregateJudgement:
    """首页弹窗连续展示：链内逐帧在场，清空后短窗内不得再冒迟到弹窗。

    证据粒度=成员捕获帧（每轮清障一帧）。链首段应连续 modal 在场；
    链清空后紧随的观察帧再次出现 modal 即判定「稍后载入」失败。
    """
    rounds: list[dict[str, Any]] = []
    for member in sorted(context.input_set.members, key=lambda item: item.page_sequence):
        packet_found = False
        modal_present = False
        for packet in _member_packets(context, member):
            packet_found = True
            if _frame_shows_popup(packet):
                modal_present = True
        if packet_found:
            rounds.append(
                {
                    "page_sequence": member.page_sequence,
                    "capture_id": member.capture_id,
                    "modal_present": modal_present,
                }
            )
    if not rounds:
        return _insufficient(
            context,
            code="POPUP_SEQUENCE_MEMBER_INSUFFICIENT",
            reason="InputSet 成员缺少可读的观察包，无法判定弹窗连续性",
        )
    chain_length = 0
    for item in rounds:
        if not item["modal_present"]:
            break
        chain_length += 1
    if chain_length == 0:
        return _insufficient(
            context,
            code="POPUP_SEQUENCE_CHAIN_ABSENT",
            reason="任务内首页帧未捕获到弹窗在场，无法判定连续展示",
        )
    if chain_length < 2:
        return _insufficient(
            context,
            code="POPUP_SEQUENCE_CHAIN_TOO_SHORT",
            reason="首页弹窗链不足两帧，无法判定「关闭即现下一个」",
        )
    late_frames = [
        item for item in rounds[chain_length : chain_length + 2] if item["modal_present"]
    ]
    artifacts = _artifact_ids(context.input_set)
    wrapper = {
        "protocol_version": "aggregate-observation-packet.v1",
        "scope": context.lease.scope,
        "scope_key": context.lease.scope_key,
        "input_set_id": context.input_set.input_set_id,
        "member_count": len(context.input_set.members),
        "rounds": rounds,
        "chain_length": chain_length,
        "late_frame_count": len(late_frames),
    }
    if late_frames:
        return AggregateJudgement(
            execution_status="fail",
            result_summary="首页弹窗链清空后又出现迟到弹窗",
            problem_desc=(
                "弹窗链在第 "
                + str(chain_length)
                + " 帧后清空，但随后的观察帧再次出现弹窗，属于「稍后载入」断档"
            ),
            confidence=1.0,
            review_status=None,
            review_reason=None,
            error_code=None,
            wrapper_payload=wrapper,
            evidence_artifact_ids=artifacts,
        )
    return AggregateJudgement(
        execution_status="pass",
        result_summary="首页弹窗按帧连续展示且清空后无迟到弹窗",
        problem_desc=None,
        confidence=1.0,
        review_status=None,
        review_reason=None,
        error_code=None,
        wrapper_payload=wrapper,
        evidence_artifact_ids=artifacts,
    )


# 官方群入口：入口文案关键词与加群方式（群链接域名 / 应用协议）关键词。
_OFFICIAL_GROUP_ENTRY_KEYWORDS: tuple[str, ...] = (
    "官方群",
    "官方群组",
    "官方交流群",
    "官方社群",
    "官方频道",
    "加群",
    "加入群",
    "入群",
    "粉丝群",
    "电报群",
    "telegram",
    "tg群",
    "potato",
)
_OFFICIAL_GROUP_LINK_TOKENS: tuple[str, ...] = (
    "t.me/",
    "telegram.me/",
    "telegram.org/",
    "tg://",
    "potato://",
    "potato.im/",
    "pt.im/",
)
# 断定「整个产品没有入口」前，至少要采到过这些页面之一（入口通常在首页浮标或个人中心）。
_OFFICIAL_GROUP_ENTRY_HOME_PAGES: frozenset[str] = frozenset({"home", "profile"})
# 图标入口没有可见文字，线索在页面结构里：链接地址、图片地址、无障碍标签、App 节点的
# content-desc（2026-09-22 海角乱伦 App/H5 两次判「没入口」都只看了可见文字）。
_OFFICIAL_GROUP_STRUCTURE_KEYS: frozenset[str] = frozenset(
    {"text", "aria_label", "content_desc", "name", "alt", "title", "href", "src"}
)
_OFFICIAL_GROUP_STRUCTURE_MAX_DEPTH = 8
_OFFICIAL_GROUP_STRUCTURE_MAX_PARTS = 4000


def _structural_entry_text(facts: Mapping[str, Any]) -> str:
    """把 DOM 摘要与 App 界面快照里能说明「这是什么入口」的字段拼成一段文本。"""
    parts: list[str] = []
    for key in ("dom_summary", "ui_snapshot"):
        _collect_structural_text(facts.get(key), parts, depth=0)
    return "\n".join(parts)


def _collect_structural_text(value: Any, parts: list[str], *, depth: int) -> None:
    if depth > _OFFICIAL_GROUP_STRUCTURE_MAX_DEPTH:
        return
    if len(parts) >= _OFFICIAL_GROUP_STRUCTURE_MAX_PARTS:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str):
                if str(key) in _OFFICIAL_GROUP_STRUCTURE_KEYS and item.strip():
                    parts.append(item)
            else:
                _collect_structural_text(item, parts, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_structural_text(item, parts, depth=depth + 1)


def _contains_any(text: str, tokens: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for token in tokens:
        if token.lower() in lowered:
            return token
    return None


def _action_target_text(action: Mapping[str, Any]) -> str:
    """把动作里所有能说明「点的是什么」的字段拼成一段文本。"""
    parts: list[str] = []
    for key in ("semantic_target", "purpose", "selector"):
        value = action.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("target_before", "target_after"):
        value = action.get(key)
        if isinstance(value, Mapping):
            parts.append(
                " ".join(str(item) for item in value.values() if isinstance(item, str | int))
            )
    return " ".join(parts)


def _action_landing_urls(action: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("after_url",):
        value = action.get(key)
        if isinstance(value, str):
            urls.append(value)
    new_pages = action.get("new_page_urls")
    if isinstance(new_pages, list):
        urls.extend(str(item) for item in new_pages if str(item).strip())
    navigation = action.get("navigation")
    if isinstance(navigation, Mapping):
        after = navigation.get("after_url")
        if isinstance(after, str):
            urls.append(after)
        pages = navigation.get("new_page_urls")
        if isinstance(pages, list):
            urls.extend(str(item) for item in pages if str(item).strip())
    return urls


def _evaluate_official_group_entry(context: AggregateJudgeContext) -> AggregateJudgement:
    """官方群入口整任务判一次：有入口就看跳转或加群方式；首页和个人中心都采过却哪都没有才判不过。

    证据粒度=成员捕获帧的可见文字与动作日志：入口=可见文字命中官方群关键词；跳转=点击了
    带关键词的目标后落到群链接或唤起聊天应用协议；加群方式=页面可见文字里直接出现群链接。
    看得见入口但没有点击材料记证据不足（留缺口，按规则声明重新观察补采），不判不过。
    """
    entries: list[dict[str, Any]] = []
    jumps: list[dict[str, Any]] = []
    links_shown: list[dict[str, Any]] = []
    sampled_page_types: set[str] = set()
    for member in sorted(context.input_set.members, key=lambda item: item.page_sequence):
        for packet in _member_packets(context, member):
            page = packet.get("page") if isinstance(packet, Mapping) else None
            page = page if isinstance(page, Mapping) else {}
            page_type = str(page.get("page_type") or "")
            if page_type:
                sampled_page_types.add(page_type)
            facts = packet.get("facts") if isinstance(packet, Mapping) else None
            facts = facts if isinstance(facts, Mapping) else {}
            visible_text = str(facts.get("visible_text") or "")
            structural_text = _structural_entry_text(facts)
            keyword_source = "visible_text"
            keyword = _contains_any(visible_text, _OFFICIAL_GROUP_ENTRY_KEYWORDS)
            if keyword is None:
                keyword_source = "structure"
                keyword = _contains_any(structural_text, _OFFICIAL_GROUP_ENTRY_KEYWORDS)
            if keyword is not None:
                entries.append(
                    {
                        "capture_id": member.capture_id,
                        "page_type": page_type,
                        "keyword": keyword,
                        "source": keyword_source,
                    }
                )
            link_source = "visible_text"
            link_token = _contains_any(visible_text, _OFFICIAL_GROUP_LINK_TOKENS)
            if link_token is None:
                link_source = "structure"
                link_token = _contains_any(structural_text, _OFFICIAL_GROUP_LINK_TOKENS)
            if link_token is not None:
                links_shown.append(
                    {
                        "capture_id": member.capture_id,
                        "page_type": page_type,
                        "token": link_token,
                        "source": link_source,
                    }
                )
            actions = facts.get("action_log")
            actions = actions if isinstance(actions, list) else []
            for action in actions:
                if not isinstance(action, Mapping):
                    continue
                target_text = _action_target_text(action)
                if _contains_any(target_text, _OFFICIAL_GROUP_ENTRY_KEYWORDS) is None:
                    continue
                landing = _action_landing_urls(action)
                landed_token = next(
                    (
                        token
                        for url in landing
                        if (token := _contains_any(url, _OFFICIAL_GROUP_LINK_TOKENS)) is not None
                    ),
                    None,
                )
                transition = str(action.get("transition_kind") or "")
                if landed_token is not None or "external" in transition.lower():
                    jumps.append(
                        {
                            "capture_id": member.capture_id,
                            "page_type": page_type,
                            "landing_token": landed_token,
                            "transition_kind": transition or None,
                            "action_execution_id": action.get("action_execution_id"),
                        }
                    )
    artifacts = _artifact_ids(context.input_set)
    wrapper: dict[str, Any] = {
        "protocol_version": "aggregate-observation-packet.v1",
        "scope": context.lease.scope,
        "scope_key": context.lease.scope_key,
        "input_set_id": context.input_set.input_set_id,
        "member_count": len(context.input_set.members),
        "sampled_page_types": sorted(sampled_page_types),
        "entries": entries,
        "jumps": jumps,
        "links_shown": links_shown,
    }
    if entries and (jumps or links_shown):
        return AggregateJudgement(
            execution_status="pass",
            result_summary="官方群入口可用：点击后跳转到群或页面给出了加群方式",
            problem_desc=None,
            confidence=1.0,
            review_status=None,
            review_reason=None,
            error_code=None,
            wrapper_payload={**wrapper, "outcome": "entry_usable"},
            evidence_artifact_ids=artifacts,
        )
    if entries:
        return _insufficient(
            context,
            code="OFFICIAL_GROUP_ENTRY_NOT_CLICKED",
            reason="页面上看得见官方群入口，但没有点击入口后的跳转或加群方式材料",
        )
    if not _OFFICIAL_GROUP_ENTRY_HOME_PAGES.issubset(sampled_page_types):
        return _insufficient(
            context,
            code="OFFICIAL_GROUP_ENTRY_PAGES_NOT_SAMPLED",
            reason="首页和个人中心尚未全部采到，不能断定产品没有官方群入口",
        )
    return AggregateJudgement(
        execution_status="fail",
        result_summary="整个产品未发现官方群入口",
        problem_desc=(
            "已采集 "
            + "、".join(sorted(sampled_page_types))
            + " 页面，可见文字和页面结构里都没有官方群、加群等入口；产品必须提供官方群入口"
        ),
        confidence=1.0,
        review_status=None,
        review_reason=None,
        error_code=None,
        wrapper_payload={**wrapper, "outcome": "entry_missing"},
        evidence_artifact_ids=artifacts,
    )


def _insufficient(
    context: AggregateJudgeContext,
    *,
    code: str,
    reason: str,
) -> AggregateJudgement:
    return AggregateJudgement(
        execution_status="evidence_insufficient",
        result_summary="聚合规则证据不足，无法形成自动结论",
        problem_desc=reason,
        confidence=None,
        review_status="pending",
        review_reason=reason,
        error_code=code,
        wrapper_payload={
            "protocol_version": "aggregate-observation-packet.v1",
            "scope": context.lease.scope,
            "scope_key": context.lease.scope_key,
            "input_set_id": context.input_set.input_set_id,
            "member_count": len(context.input_set.members),
            "evidence_insufficient_code": code,
        },
        evidence_artifact_ids=_artifact_ids(context.input_set),
    )


def _artifact_ids(input_set: EvidenceInputSet) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            artifact_id for member in input_set.members for artifact_id in member.artifact_ids
        )
    )
