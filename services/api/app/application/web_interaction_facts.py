"""Web 动作结算共用的自然主体事实；不读取模型声明或执行浏览器动作。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import urljoin

from services.api.app.application.web_interaction_precision import (
    WebInteractionEvidenceMode,
    WebInteractionOutcome,
    WebInteractionPlan,
    WebInteractionPostcondition,
)
from services.api.app.application.web_logical_page import canonical_web_logical_page_url
from services.api.app.domain.exploration.web_exploration import (
    web_obstruction_state_fingerprint,
)
from services.api.app.domain.interaction import ActionIntent


@dataclass(frozen=True, kw_only=True)
class WebInteractionCaptureFacts:
    url: str
    content_identity: str | None = None
    subject_urls: tuple[str, ...] = ()
    business_layer_visible: bool = False
    readiness_accepted: bool = False
    identity_verified: bool = False
    evidence_mode: WebInteractionEvidenceMode = WebInteractionEvidenceMode.NATURAL
    visible_layers: tuple[str, ...] = ()
    candidate_layers: tuple[tuple[str, str], ...] = ()
    obstruction_content_identity: str | None = None
    business_fact_identities: tuple[str, ...] = ()


def web_capture_content_facts(
    snapshot: Mapping[str, object],
    *,
    url: str,
) -> WebInteractionCaptureFacts:
    """只从可见非广告业务主体提取身份；列表/推荐链接不证明详情对象。"""
    raw = snapshot.get("elements")
    elements = [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    visible = [item for item in elements if item.get("visible") is True]
    blocked = snapshot.get("human_verification_present") is True or any(
        item.get("web_obstruction_layer_structural") is True
        or item.get("web_human_verification_present") is True
        for item in visible
    )
    business = []
    stable_business_facts = []
    subjects = []
    media_objects = 0
    for item in visible:
        tokens = " ".join(
            str(item.get(key) or "").casefold()
            for key in (
                "selector",
                "class_tokens",
                "ancestor_class_tokens",
            )
        )
        if any(
            item.get(key) is True
            for key in (
                "is_advertisement",
                "within_web_navigation",
                "within_footer",
                "within_breadcrumb",
                "within_pagination",
            )
        ):
            continue
        if any(
            token in tokens
            for token in ("recommend", "related", "footer", "sponsor", "advertisement")
        ):
            continue
        tag = str(item.get("tag") or "").casefold()
        if (
            tag in {"main", "article", "video", "audio"}
            or item.get("within_repeated_structure") is True
            # 与现有页型结构标记对齐；可见正文不因此获得同 URL 对象身份。
            or any(
                marker in tokens
                for marker in (
                    "content-detail",
                    "post-content",
                    "entry-content",
                    "article-body",
                    "detail-content",
                    "video-player",
                    "video-js",
                    "player-root",
                    "playback",
                    "detail__play",
                    "episode-grid",
                    "episode-list",
                )
            )
        ):
            business_fact = {
                key: item.get(key)
                for key in ("tag", "text", "href", "src", "selector", "dom_fingerprint")
            }
            business.append(business_fact)
            if (
                not item.get("web_obstruction_layer")
                and not _dangerous_business_control(item, tokens=tokens)
            ):
                stable_business_facts.append(business_fact)
        # 唯一主媒体自身的资源地址有对象语义；普通锚点和正文文字没有。
        if tag in {"video", "audio"} and item.get("within_repeated_structure") is not True:
            media_objects += 1
            resource = str(item.get("src") or "").strip()
            if resource:
                try:
                    subjects.append(canonical_web_logical_page_url(urljoin(url, resource)))
                except ValueError:
                    # blob/data 媒体仍可见，但不能提供规范业务对象身份。
                    pass
    return WebInteractionCaptureFacts(
        url=url,
        content_identity=(
            sha256(
                json.dumps(business, sort_keys=True, ensure_ascii=False, default=str).encode(
                    "utf-8"
                )
            ).hexdigest()
            if business
            else None
        ),
        subject_urls=tuple(subjects) if len(subjects) == 1 and media_objects == 1 else (),
        business_layer_visible=bool(business) and not blocked,
        visible_layers=tuple(
            sorted(
                {
                    str(item["web_obstruction_layer"])
                    for item in visible
                    if item.get("web_obstruction_layer")
                }
            )
        ),
        candidate_layers=tuple(
            (str(item.get("selector") or ""), str(item["web_obstruction_layer"]))
            for item in visible
            if item.get("web_obstruction_layer")
        ),
        obstruction_content_identity=sha256(
            json.dumps(
                {
                    "elements": [
                        dict(item)
                        for item in visible
                        if item.get("web_obstruction_layer")
                    ],
                    "state_fingerprint": web_obstruction_state_fingerprint(snapshot),
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        business_fact_identities=tuple(
            sorted(
                {
                    sha256(
                        json.dumps(
                            item,
                            sort_keys=True,
                            ensure_ascii=False,
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    for item in stable_business_facts
                }
            )
        ),
    )


def web_business_identity_stable(
    before: WebInteractionCaptureFacts,
    after: WebInteractionCaptureFacts,
) -> bool:
    """允许无遮挡后新增保留事实，但禁止旧业务事实丢失、变更或跨 URL。"""

    try:
        same_url = canonical_web_logical_page_url(
            before.url
        ) == canonical_web_logical_page_url(after.url)
    except ValueError:
        return False
    if not same_url:
        return False
    if before.content_identity and before.content_identity == after.content_identity:
        return True
    before_facts = set(before.business_fact_identities)
    after_facts = set(after.business_fact_identities)
    return bool(before_facts and before_facts.issubset(after_facts))


def _dangerous_business_control(item: Mapping[str, object], *, tokens: str) -> bool:
    tag = str(item.get("tag") or "").strip().casefold()
    if tag not in {"a", "button", "input", "form", "select", "option"}:
        return False
    text = str(item.get("text") or "").strip().casefold()
    semantics = f"{tokens} {text}"
    return any(
        marker in semantics
        for marker in (
            "登录",
            "注册",
            "支付",
            "购买",
            "充值",
            "下载",
            "授权",
            "login",
            "register",
            "sign-in",
            "signup",
            "payment",
            "purchase",
            "download",
            "authorize",
        )
    )


def web_single_action_plan(
    intent: ActionIntent, postcondition: WebInteractionPostcondition
) -> WebInteractionPlan:
    """仅构造事后结算视图；不驱动执行，原意图截止时间与动作授权均不变。"""
    remaining = (intent.deadline - datetime.now(intent.deadline.tzinfo)).total_seconds()
    return WebInteractionPlan(
        intent=intent,
        source_capture_id=intent.source_capture_id,
        source_epoch=intent.source_capture_id,
        target_ref=intent.semantic_target,
        max_scroll_steps=0,
        deadline_seconds=max(0.001, min(300.0, remaining)),
        postcondition=postcondition,
    )


def web_command_accepted(receipt: object) -> bool:
    """动作输入成功与后续证据或租约失败可以同时发生。"""
    return getattr(receipt, "execution_outcome", None) == "succeeded"


def web_receipt_allows_arrival(receipt: object) -> bool:
    return (
        web_command_accepted(receipt)
        and getattr(receipt, "status", None) == "succeeded"
        and getattr(receipt, "error_code", None) is None
    )


def web_incomplete_interaction(
    receipt: object, after_capture_id: str | None, reason: str
) -> WebInteractionOutcome:
    """副作用已经发生时先留下回执；未知效果不能被填成成功。"""
    return WebInteractionOutcome(
        command_accepted=web_command_accepted(receipt),
        effect_observed=False,
        postcondition_satisfied=False,
        transition_arrived=False,
        after_capture_id=after_capture_id,
        failure_reason=reason,
    )


def web_target_exposed(snapshot: Mapping[str, object], *, selector: str) -> bool:
    """确认该步原目标在当前自然视口曝光，正文减少或 URL 相等不是曝光证明。"""
    viewport = snapshot.get("viewport")
    raw = snapshot.get("elements")
    if not isinstance(viewport, Mapping) or not isinstance(raw, list):
        return False
    matching = [
        item for item in raw if isinstance(item, Mapping) and item.get("selector") == selector
    ]
    if len(matching) != 1 or matching[0].get("visible") is not True:
        return False
    rect = matching[0].get("rect")
    if not isinstance(rect, Mapping):
        return False
    try:
        x, y, width, height = (float(rect[key]) for key in ("x", "y", "width", "height"))
        return (
            width > 0
            and height > 0
            and 0 <= x + width / 2 < float(viewport["width"])
            and 0 <= y + height / 2 < float(viewport["height"])
        )
    except (KeyError, TypeError, ValueError):
        return False
