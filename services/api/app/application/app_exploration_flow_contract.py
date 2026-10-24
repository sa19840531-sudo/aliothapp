"""依据实际观察累计启动、遮挡处理与底栏到达证据，不从产品模板生成分母。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.app_semantic_progress import (
    bottom_tab_label_already_visited,
)

APP_FLOW_CONTRACT_VERSION = "app-exploration-flow-contract.v2"
_LEGACY_FLOW_CONTRACT_VERSION = "app-exploration-flow-contract.v1"
APP_FLOW_CONTRACT_KEY = "app_flow_contract"


def popup_phase_blocks_bottom_tabs(
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """弹窗流程未满足时禁止一切底栏（含 Frontier sibling／假 completed）。

    录屏：网格广告未关完前底栏仍可见，但不得点短剧／社区；action 记 completed
    也不算 visited。必须等 popup_satisfied 才允许底栏 seek。

    例外：弹窗轮播关不完且已满覆盖让路预算时放行，避免 sibling 永远改写成关层。
    """

    if not isinstance(coverage_metadata, Mapping):
        return False
    flow = coverage_metadata.get(APP_FLOW_CONTRACT_KEY)
    if not isinstance(flow, Mapping):
        return False
    if flow.get("popup_seen") is not True:
        return False
    if flow.get("popup_satisfied") is True:
        return False
    from services.api.app.application.app_coverage_page_seek import (
        overlay_budget_allows_real_coverage_seek,
    )

    page_type = coverage_metadata.get("business_page_type") or coverage_metadata.get(
        "page_type"
    )
    if overlay_budget_allows_real_coverage_seek(
        page_type=str(page_type) if page_type is not None else None,
        coverage_metadata=coverage_metadata,
    ):
        return False
    return True


def popup_phase_blocks_home_top_category_tabs(
    coverage_metadata: Mapping[str, object] | None,
    analysis: object | None = None,
) -> bool:
    """弹窗流程未满足时禁止主页上方分类。

    真实主页上方栏目只在弹层关完后才出现；未满足时点上方栏等于点穿广告。
    例外：当前帧主页栏／底栏已露出（国庆贴、侧浮层），不得用滞后的
    popup_satisfied=false 卡住课纲「最新」。
    """

    if not popup_phase_blocks_bottom_tabs(coverage_metadata):
        return False
    if analysis is not None:
        return analysis_blocks_home_top_category_tabs(analysis)
    if _home_chrome_visible_from_coverage(coverage_metadata):
        return False
    return True


def analysis_blocks_home_top_category_tabs(analysis: object | None) -> bool:
    """当前帧仍有真挡路弹层时，禁止上方分类。

    真挡路：dialog_kind 广告模态／topmost_ad_popup／无 X 终身卡促销／
    page_state∈{popup,modal,…} 且确有硬广告事实。
    软浮层（右侧分享角标、国庆浮动贴、底飘）即使 Vision 误标 page_state=popup，
    只要主页栏与底栏已露出，不得禁止点「最新」等上方课纲。
    """

    if analysis is None:
        return False
    facts = getattr(analysis, "surface_facts", None)
    if not isinstance(facts, Mapping):
        facts = {}
    dialog_kind = str(facts.get("dialog_kind") or "").strip().casefold()
    hard_visible_dialog = (
        facts.get("popup_visible") is True
        and facts.get("dialog_visible") is True
    )
    hard_dialog = dialog_kind in {
        "ad_popup",
        "promo_popup",
        "promo_ad",
        "modal",
        "dialog",
        "system_dialog",
        "age_verification",
        "age_confirm",
        "age_gate",
        "unlock_paywall",
        "paywall",
        "purchase",
        "vip_unlock",
        "vip_purchase",
        "payment",
        "membership",
        "lifetime_card",
        "vip_promo",
    }
    topmost = facts.get("topmost_ad_popup")
    hard_topmost = isinstance(topmost, Mapping) and bool(topmost)
    hard_promo = False
    try:
        from services.api.app.application.app_visual_close_calibration import (
            is_no_close_auto_dismiss_promo,
            is_payment_or_paywall_surface,
        )

        if is_payment_or_paywall_surface(analysis) or is_no_close_auto_dismiss_promo(
            analysis
        ):
            hard_promo = True
        page_type_pay = str(getattr(analysis, "visual_page_type", None) or "").casefold()
        if page_type_pay == "payment":
            hard_promo = True
    except Exception:  # noqa: BLE001
        pass
    if hard_visible_dialog or hard_dialog or hard_topmost or hard_promo:
        return True

    page_state = str(getattr(analysis, "page_state", "") or "").strip().casefold()
    if page_state not in {
        "startup_ad",
        "splash",
        "countdown",
        "popup",
        "modal",
        "dialog",
        "system_dialog",
    }:
        return False

    # page_state 被标成 popup／modal，但无硬广告事实：若已是主页且栏位／底栏可见，
    # 视为软浮层误标（国庆贴／分享钮），放行上方课纲「最新」。
    page_type = str(
        getattr(analysis, "visual_page_type", None)
        or getattr(analysis, "page_type", None)
        or ""
    ).casefold()
    if page_type == "home" and _home_chrome_visible_for_top_tabs(facts, analysis):
        return False
    return True


def _home_chrome_visible_from_coverage(
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """覆盖元数据已能证明主页栏／底栏露出时，软浮层不得禁上方分类。"""

    if not isinstance(coverage_metadata, Mapping):
        return False
    facts = coverage_metadata.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}
    dummy = type("CoverageAnalysis", (), {"candidates": ()})()
    return _home_chrome_visible_for_top_tabs(facts, dummy)


def _home_chrome_visible_for_top_tabs(
    facts: Mapping[str, object],
    analysis: object,
) -> bool:
    """主页顶栏／底栏已露出：软浮层不得当成挡路上方分类的真弹层。"""

    if facts.get("home_top_selected_is_skip") is True:
        return True
    selected_top = facts.get("home_top_selected_label") or facts.get(
        "pixel_home_top_selected_label"
    )
    if isinstance(selected_top, str) and selected_top.strip():
        return True
    calibration = facts.get("pixel_home_top_category_calibration")
    if isinstance(calibration, Mapping):
        tabs = calibration.get("tabs")
        if isinstance(tabs, (list, tuple)) and tabs:
            return True
        if calibration.get("selected_slot_index") is not None:
            return True
    selected_bottom = facts.get("pixel_selected_labels") or facts.get(
        "selected_bottom_tab_labels"
    )
    if isinstance(selected_bottom, (list, tuple)) and selected_bottom:
        return True
    # 候选池已有上方分类／底栏 seek：栏已露出。
    candidates = getattr(analysis, "candidates", None) or ()
    for item in candidates:
        meta = getattr(item, "metadata", None)
        if not isinstance(meta, Mapping):
            meta = {}
        if meta.get("home_top_category_tab_seek") is True:
            return True
        if meta.get("bottom_navigation_tab_seek") is True:
            return True
        purpose = str(
            getattr(item, "purpose", None) or meta.get("purpose") or ""
        )
        name = str(getattr(item, "semantic_name", None) or getattr(item, "semantic_target", None) or "")
        blob = f"{purpose} {name}"
        if "上方分类" in blob or "底栏" in blob:
            return True
    return False


def popup_phase_blocks_frontier_exhaustion(
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """弹窗流程未满足时禁止 Frontier 排空收口。

    关层候选可能被 sibling 滤光／supersede；队列 pending=0 不等于弹窗已清。
    必须继续签发关层或 OBSERVE 重采，不得 ``app_global_frontier_exhausted``。
    """

    return popup_phase_blocks_bottom_tabs(coverage_metadata)


_STARTUP_STATES = frozenset({"startup_ad", "splash", "countdown"})
_POPUP_STATES = frozenset(
    {
        "popup",
        "modal",
        "overlay",
        "dialog",
        "system_dialog",
        "permission_dialog",
    }
)


def _norm_label(raw: object) -> str:
    text = str(raw or "").strip()
    for prefix in ("底栏「", "底部导航「", "底部Tab", "底部", "底栏"):
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix) :].strip()
    return text.strip("「」[]()（） ").casefold()


def _text_list(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            text = item.strip()
            if text not in out:
                out.append(text)
    return out


def _merge_labels(*groups: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = _norm_label(item)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(str(item).strip())
    return out


def extract_observed_bottom_tab_labels(
    *,
    surface_facts: Mapping[str, Any] | None,
    playbook_tabs: Sequence[str] = (),
    package_name: str | None = None,
) -> list[str]:
    """从当前受信 a11y 底栏抽取可点标签，旧模板参数仅保留调用兼容。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    labels: list[str] = []
    if isinstance(controls, Mapping):
        owner = controls.get("package_name")
        if package_name and isinstance(owner, str) and owner.strip() != package_name:
            return []
        navigation = controls.get("bottom_navigation")
        if isinstance(navigation, (list, tuple)):
            for item in navigation:
                if not isinstance(item, Mapping):
                    continue
                if item.get("action_allowed") is False:
                    continue
                owner = item.get("package_name")
                if package_name and isinstance(owner, str) and owner.strip() != package_name:
                    continue
                label = item.get("label") or item.get("text") or item.get("content_desc")
                if isinstance(label, str) and label.strip():
                    labels.append(label.strip())
    return _merge_labels(labels)


def _bottom_tab_evidence_by_package(
    contract: Mapping[str, object] | None,
    *,
    allowed_packages: Sequence[str],
) -> dict[str, dict[str, list[str]]]:
    """只恢复有目标包来源的底栏证据；旧摘要不能反推控件所属包。"""

    raw = contract.get("bottom_tab_evidence_by_package") if isinstance(contract, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    allowed = {item.strip() for item in allowed_packages if isinstance(item, str) and item.strip()}
    result: dict[str, dict[str, list[str]]] = {}
    for package, evidence in raw.items():
        if (
            not isinstance(package, str)
            or package not in allowed
            or not isinstance(evidence, Mapping)
        ):
            continue
        observed = _text_list(evidence.get("observed_tabs"))
        observed_norm = {_norm_label(item) for item in observed}
        visited = [
            item
            for item in _text_list(evidence.get("visited_tabs"))
            if bottom_tab_label_already_visited(item, observed_norm)
        ]
        result[package] = {"observed_tabs": observed, "visited_tabs": visited}
    return result


def trusted_flow_visited_bottom_labels(
    contract: Mapping[str, object] | None,
    *,
    allowed_packages: Sequence[str],
) -> list[str]:
    """恢复任务已访底栏时使用包内来源账本，不采纳无来源的旧平铺标签。"""

    evidence = _bottom_tab_evidence_by_package(contract, allowed_packages=allowed_packages)
    return _merge_labels(*(item["visited_tabs"] for item in evidence.values()))


def empty_app_flow_contract(
    *,
    playbook_id: str | None = None,
    playbook_tabs: Sequence[str] = (),
    splash_burst: int = 1,
) -> dict[str, object]:
    """初始化空流程契约；正式 Gate 不消费，仅探索终态硬闸。"""

    burst = max(1, int(splash_burst) if isinstance(splash_burst, int) else 1)
    return {
        "protocol_version": APP_FLOW_CONTRACT_VERSION,
        "required": True,
        "playbook_id": None,
        "denominator_source": "current_observation",
        "splash_burst_required": burst,
        "startup_ad_seen": False,
        "startup_ad_frame_count": 0,
        "startup_ad_capture_ids": [],
        "startup_satisfied": True,  # 未见开屏时默认满足
        "popup_seen": False,
        "popup_close_evidenced": False,
        "popup_satisfied": True,  # 未见弹层时默认满足
        "home_arrived": False,
        "required_bottom_tabs": [],
        "observed_bottom_tabs": [],
        "visited_bottom_tabs": [],
        "bottom_tab_evidence_by_package": {},
        "bottom_tabs_satisfied": True,  # 只累计实际观测的底栏
        "required_page_types": [],
        "covered_page_types": [],
        "missing_page_types": [],
        "page_types_satisfied": True,  # 无显式 required 时默认满足
        "active_popup_overlay": None,
        "active_popup_image": None,
        "popup_close_unconfirmed": False,
        "complete": False,
        "missing_reasons": ["home_not_reached"],
        "gate_consumes_flow": False,
    }


def update_app_flow_contract(
    prior: Mapping[str, Any] | None,
    *,
    page_state: str | None,
    page_type: str | None = None,
    overlay_signature: str | None = None,
    surface_facts: Mapping[str, Any] | None = None,
    capture_id: str | None = None,
    package_within_boundary: bool = True,
    stable_business_page: bool = False,
    action_observation_outcome: str | None = None,
    visited_bottom_labels: Sequence[str] = (),
    playbook_id: str | None = None,
    playbook_tabs: Sequence[str] = (),
    splash_burst: int = 1,
    required_page_types: Sequence[str] = (),
    covered_page_types: Sequence[str] = (),
    package_name: str | None = None,
    allowed_packages: Sequence[str] = (),
    image_signature: str | None = None,
) -> dict[str, object]:
    """累计实际观察证据；旧版模板分母在本次更新时改为已观察入口。"""

    base = empty_app_flow_contract(
        playbook_id=playbook_id,
        playbook_tabs=playbook_tabs,
        splash_burst=splash_burst,
    )
    if isinstance(prior, Mapping) and prior.get("protocol_version") in {
        APP_FLOW_CONTRACT_VERSION, _LEGACY_FLOW_CONTRACT_VERSION
    }:
        for key in base:
            if key in prior:
                base[key] = prior[key]  # type: ignore[index]
        if isinstance(splash_burst, int) and splash_burst > 0:
            base["splash_burst_required"] = max(
                int(base.get("splash_burst_required") or 1),
                splash_burst,
            )

    state = (page_state or "unknown").strip().casefold() or "unknown"
    ptype = (page_type or "").strip().casefold()
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}

    # —— 开屏／启动广告截图 ——
    if state in _STARTUP_STATES or ptype in _STARTUP_STATES:
        base["startup_ad_seen"] = True
        base["startup_satisfied"] = False
        if isinstance(capture_id, str) and capture_id.strip():
            captures = _text_list(base.get("startup_ad_capture_ids"))
            if capture_id.strip() not in captures:
                captures.append(capture_id.strip())
            base["startup_ad_capture_ids"] = captures
            base["startup_ad_frame_count"] = len(captures)
    if base.get("startup_ad_seen") is True:
        need = int(base.get("splash_burst_required") or 1)
        frames = int(base.get("startup_ad_frame_count") or 0)
        left_startup = state not in _STARTUP_STATES and ptype not in _STARTUP_STATES
        base["startup_satisfied"] = frames >= need or (frames >= 1 and left_startup)

    # —— 广告／遮挡弹窗 ——
    popup_visible = (
        state in _POPUP_STATES
        or bool(overlay_signature)
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
    )
    if popup_visible:
        base["popup_seen"] = True
        base["popup_satisfied"] = False
    before_overlay = str(base.get("active_popup_overlay") or "").strip() or None
    before_image = str(base.get("active_popup_image") or "").strip() or None
    if popup_visible:
        if overlay_signature:
            base["active_popup_overlay"] = overlay_signature
        if image_signature:
            base["active_popup_image"] = image_signature
    if action_observation_outcome == "obstruction_removed":
        from services.api.app.application.app_schedule_gap_closure import (
            popup_close_is_confirmed,
        )

        confirmed = popup_close_is_confirmed(
            popup_was_visible=base.get("popup_seen") is True,
            before_overlay=before_overlay,
            after_overlay=overlay_signature,
            before_image=before_image,
            after_image=image_signature,
            still_popup=popup_visible,
        )
        base["popup_close_unconfirmed"] = not confirmed
        if confirmed:
            base["popup_close_evidenced"] = True
            base["active_popup_overlay"] = None
            base["active_popup_image"] = None
    if base.get("popup_seen") is True:
        base["popup_satisfied"] = bool(base.get("popup_close_evidenced")) or (
            not popup_visible and package_within_boundary and state == "stable"
        )

    # —— 首页／业务根 ——
    if package_within_boundary and stable_business_page and state == "stable" and not popup_visible:
        base["home_arrived"] = True
    # 底栏已选中主页／首页：即使仍有软遮挡／Vision 降级，也记到家，避免永远 home_not_reached。
    if package_within_boundary and not base.get("home_arrived"):
        selected_now: set[str] = set()
        pixel_selected = facts.get("pixel_selected_labels")
        if isinstance(pixel_selected, (list, tuple)):
            selected_now.update(
                _norm_label(item) for item in pixel_selected if isinstance(item, str)
            )
        controls = facts.get("trusted_accessibility_controls")
        if isinstance(controls, Mapping):
            bottom = controls.get("bottom_navigation")
            if isinstance(bottom, (list, tuple)):
                for item in bottom:
                    if not isinstance(item, Mapping) or item.get("selected") is not True:
                        continue
                    raw = item.get("label") or item.get("text") or item.get("content_desc")
                    if isinstance(raw, str) and raw.strip():
                        selected_now.add(_norm_label(raw))
        selected_now = {item for item in selected_now if item}
        if bottom_tab_label_already_visited("主页", selected_now) or bottom_tab_label_already_visited(
            "首页", selected_now
        ):
            base["home_arrived"] = True

    # —— 底栏仅累计实际观察并集，历史模板分母不得继续注入 ——
    observed_now = (
        extract_observed_bottom_tab_labels(surface_facts=facts, package_name=package_name)
        if package_within_boundary
        else []
    )
    if package_name is not None or allowed_packages:
        # 正式聚合带目标包范围；旧无来源摘要不得在同会话恢复后重新污染分母。
        allowed = tuple(allowed_packages) or ((package_name,) if package_name else ())
        evidence = _bottom_tab_evidence_by_package(prior, allowed_packages=allowed)
        if package_within_boundary and package_name and package_name in allowed:
            current = evidence.setdefault(package_name, {"observed_tabs": [], "visited_tabs": []})
            current["observed_tabs"] = _merge_labels(current["observed_tabs"], observed_now)
            observed_norm = {_norm_label(item) for item in current["observed_tabs"]}
            current["visited_tabs"] = _merge_labels(
                current["visited_tabs"],
                tuple(
                    item
                    for item in visited_bottom_labels
                    if bottom_tab_label_already_visited(item, observed_norm)
                ),
            )
        base["bottom_tab_evidence_by_package"] = evidence
        base["observed_bottom_tabs"] = _merge_labels(
            *(item["observed_tabs"] for item in evidence.values())
        )
        base["visited_bottom_tabs"] = _merge_labels(
            *(item["visited_tabs"] for item in evidence.values())
        )
    else:
        # 兼容不含设备身份的领域调用；正式截图入口必须提供目标包来源。
        base["observed_bottom_tabs"] = _merge_labels(
            _text_list(base.get("observed_bottom_tabs")),
            observed_now,
        )
        if package_within_boundary:
            base["visited_bottom_tabs"] = _merge_labels(
                _text_list(base.get("visited_bottom_tabs")),
                tuple(str(item) for item in visited_bottom_labels if str(item).strip()),
            )
    required = _text_list(base.get("observed_bottom_tabs"))
    base["playbook_id"] = None
    base["denominator_source"] = "current_observation"
    base["required_bottom_tabs"] = required
    visited_norm = {_norm_label(item) for item in _text_list(base.get("visited_bottom_tabs"))}
    required_norm = [_norm_label(item) for item in required]
    if not required_norm:
        # 从未观测到底栏时，不凭模板假设存在待巡检栏。
        base["bottom_tabs_satisfied"] = True
        missing_tabs: list[str] = []
    else:
        missing_tabs = [
            label
            for label, key in zip(required, required_norm, strict=False)
            if not bottom_tab_label_already_visited(key, visited_norm)
        ]
        base["bottom_tabs_satisfied"] = not missing_tabs

    # —— 显式 required_page_types 硬闸（镜像底栏 incomplete）——
    required_pages = _merge_labels(
        _text_list(base.get("required_page_types")),
        tuple(str(item) for item in required_page_types if str(item).strip()),
    )
    covered_pages = _merge_labels(
        _text_list(base.get("covered_page_types")),
        tuple(str(item) for item in covered_page_types if str(item).strip()),
    )
    # 当前帧稳定业务到达：把 page_type 计入 covered。
    if (
        package_within_boundary
        and ptype
        and state == "stable"
        and not popup_visible
        and (
            stable_business_page
            or action_observation_outcome
            in {
                "business_arrived",
                "tab_seek_screenshot_evidenced",
                "obstruction_removed",
            }
        )
    ):
        covered_pages = _merge_labels(covered_pages, (ptype,))
    base["required_page_types"] = required_pages
    base["covered_page_types"] = covered_pages
    if not required_pages:
        base["page_types_satisfied"] = True
        missing_page_types: list[str] = []
    else:
        covered_norm = {_norm_label(item) for item in covered_pages}
        missing_page_types = [
            label
            for label in required_pages
            if _norm_label(label) not in covered_norm
        ]
        base["page_types_satisfied"] = not missing_page_types
    base["missing_page_types"] = missing_page_types

    missing: list[str] = []
    if not base.get("startup_satisfied"):
        missing.append("startup_ad_evidence_missing")
    if not base.get("popup_satisfied"):
        missing.append("popup_evidence_missing")
    if not base.get("home_arrived"):
        missing.append("home_not_reached")
    if not base.get("bottom_tabs_satisfied"):
        missing.append("bottom_tabs_incomplete")
        base["missing_bottom_tabs"] = missing_tabs
    else:
        base["missing_bottom_tabs"] = []
    if not base.get("page_types_satisfied"):
        missing.append("page_types_incomplete")

    base["missing_reasons"] = missing
    base["complete"] = not missing
    base["required"] = True
    base["gate_consumes_flow"] = False
    base["protocol_version"] = APP_FLOW_CONTRACT_VERSION
    return base


def flow_contract_complete(raw: Mapping[str, Any] | None) -> bool:
    """无契约元数据时保持旧行为兼容；有契约则必须 complete。"""

    if not isinstance(raw, Mapping) or not raw:
        return True
    if raw.get("protocol_version") not in {
        APP_FLOW_CONTRACT_VERSION, _LEGACY_FLOW_CONTRACT_VERSION
    }:
        return True
    if raw.get("required") is False:
        return True
    return raw.get("complete") is True


def flow_contract_insufficient_reason(raw: Mapping[str, Any] | None) -> str | None:
    if flow_contract_complete(raw):
        return None
    if not isinstance(raw, Mapping):
        return "flow_contract_incomplete"
    reasons = raw.get("missing_reasons")
    if isinstance(reasons, (list, tuple)) and reasons:
        first = reasons[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return "flow_contract_incomplete"
