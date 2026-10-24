"""Web V2 账本、Capture、Action 与附件的严格只读证据包。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from services.api.app.application.web_bootstrap_evidence import trusted_web_bootstrap_actions
from services.api.app.application.web_discovery_audit import (
    WEB_DISCOVERY_AUDIT_REASONS,
    web_discovery_is_audit_only,
)
from services.api.app.application.web_evidence_admission import (
    assess_web_capture_evidence_admission,
)
from services.api.app.application.web_home_baseline_validation import (
    persisted_home_baseline_has_valid_shape,
)
from services.api.app.application.web_menu_lineage_validation import (
    persisted_menu_lineages_are_valid,
)
from services.api.app.application.web_readiness_contracts import (
    DEFAULT_WEB_READINESS_PROFILE,
    WebReadinessProfile,
)
from services.api.app.application.web_submenu_expansion import (
    persisted_menu_page_is_submenu_expansion,
    persisted_ordinary_menu_page_has_valid_shape,
    persisted_submenu_expansion_has_valid_shape,
)
from services.api.app.domain.exploration import WEB_DETAIL_PAGE_TYPES, WEB_LIST_PAGE_TYPES

_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLIC_ARTIFACT_TYPES = frozenset(
    {"screenshot", "dom", "visible_text", "ui_snapshot", "action_trace"}
)
_SUCCESS_LEDGER_STATUSES = frozenset(
    {
        "home_baseline",
        "menu_page_captured",
        "detail_captured",
        "logical_target_reused",
        "cross_origin_blocked",
        "menu_limit_skipped",
    }
)
_CAPTURE_PURPOSE_PRIORITY = {
    "web_page_ready_baseline": 10,
    "web_popup_cleared_recapture": 20,
    "web_primary_content_evidence": 30,
}


def build_web_evidence_bundle(
    snapshot: Mapping[str, object],
    *,
    readiness_profile: WebReadinessProfile | None = DEFAULT_WEB_READINESS_PROFILE,
    policy_error: str | None = None,
) -> dict[str, object]:
    """校验证据闭环并只投影固定白名单字段。"""

    reasons: set[str] = set()
    if readiness_profile is None or policy_error is not None:
        reasons.add(policy_error or "WEB_POLICY_HISTORY_MISSING")
    ledger = _mapping_rows(snapshot.get("ledger"))
    captures = _mapping_rows(snapshot.get("captures"))
    actions = _mapping_rows(snapshot.get("actions"))
    artifacts = _mapping_rows(snapshot.get("artifacts"))
    if any(rows is None for rows in (ledger, captures, actions, artifacts)):
        return _empty_review_bundle(snapshot)
    assert ledger is not None and captures is not None
    assert actions is not None and artifacts is not None

    if not _budget_is_valid(snapshot, ledger):
        reasons.add("budget_conflict")
    if not _ledger_closure_is_valid(snapshot, ledger):
        reasons.add("ledger_closure_invalid")
    if not persisted_menu_lineages_are_valid(ledger):
        reasons.add("lineage_invalid")

    capture_by_id = _unique_index(captures, "capture_id")
    action_by_id = _unique_index(actions, "action_execution_id")
    if capture_by_id is None or action_by_id is None:
        reasons.add("evidence_incomplete")
        capture_by_id = capture_by_id or {}
        action_by_id = action_by_id or {}

    bootstrap_actions = trusted_web_bootstrap_actions(snapshot, capture_by_id, action_by_id)
    bootstrap_before_ids = {action_by_id[key]["source_capture_id"] for key in bootstrap_actions}
    for row in ledger:
        if row.get("status") == "logical_target_reused":
            original = capture_by_id.get(_text(row.get("reused_capture_id")))
            if (
                original is None
                or _text(original.get("logical_page_id")) != _text(row.get("reused_logical_page_id"))
                or _reused_target_prefix(row) is None
            ):
                reasons.add("logical_target_reuse_invalid")
    formal_capture_references = _formal_ready_capture_references(ledger)
    formal_capture_ids = set(formal_capture_references)
    primary_supplement_ids, primary_supplements_valid = _trusted_primary_content_supplement_ids(
        formal_capture_ids,
        capture_by_id=capture_by_id,
        action_by_id=action_by_id,
        snapshot=snapshot,
    )
    if not primary_supplements_valid:
        reasons.add("evidence_incomplete")
    formal_capture_ids.update(primary_supplement_ids)
    for capture_id in formal_capture_ids:
        capture = capture_by_id.get(capture_id, {})
        if capture.get("logical_page_identity_status") != "verified":
            reasons.add(_text(capture.get("logical_page_identity_status")) or "WEB_LOGICAL_ID_UNTRUSTED")

    required_capture_ids = _ledger_capture_ids(ledger) | primary_supplement_ids
    required_action_ids = _ledger_action_ids(ledger)
    if not _expand_action_closure(
        required_capture_ids,
        required_action_ids,
        capture_by_id=capture_by_id,
        action_by_id=action_by_id,
        bootstrap_actions=bootstrap_actions,
    ):
        reasons.add("evidence_incomplete")

    if not all(
        _capture_identity_is_valid(
            capture_by_id.get(capture_id),
            snapshot=snapshot,
            allow_bootstrap_blank=capture_id in bootstrap_before_ids,
        )
        for capture_id in required_capture_ids
    ):
        reasons.add("evidence_incomplete")

    if not _ledger_actions_are_valid(
        ledger,
        snapshot=snapshot,
        capture_by_id=capture_by_id,
        action_by_id=action_by_id,
        required_action_ids=required_action_ids,
        bootstrap_actions=bootstrap_actions,
    ):
        reasons.add("evidence_incomplete")
    if not _ledger_arrivals_are_trusted(ledger, action_by_id=action_by_id):
        reasons.add("untrusted_arrival")

    # 重复观察虽不再分类或充当主材料，仍须独立通过本次 readiness/准入。
    ready_observation_ids = {
        _text(row.get(key))
        for row in ledger
        for key in ("menu_ready_capture_id", "detail_ready_capture_id")
        if _text(row.get(key))
    }
    if not all(
        readiness_profile is not None
        and _capture_is_formally_admitted(capture_by_id.get(capture_id), profile=readiness_profile)
        for capture_id in formal_capture_ids | ready_observation_ids
    ):
        reasons.add("evidence_incomplete")

    if not _required_artifacts_are_valid(
        required_capture_ids,
        capture_by_id=capture_by_id,
        artifacts=artifacts,
    ):
        reasons.add("evidence_incomplete")

    public_captures = _public_captures(
        required_capture_ids,
        capture_by_id=capture_by_id,
        artifacts=artifacts,
    )
    home_count = sum(str(row.get("status") or "") == "home_baseline" for row in ledger)
    menu_count = sum(int(row.get("menu_action_count") or 0) for row in ledger)
    detail_count = sum(str(row.get("status") or "") == "detail_captured" for row in ledger)
    duplicate_formal_url_count = _duplicate_formal_url_count(
        formal_capture_references,
        capture_by_id=capture_by_id,
    )
    if duplicate_formal_url_count:
        reasons.add("duplicate_formal_url")
    bundle: dict[str, object] = {
        "task_id": str(snapshot.get("task_id") or ""),
        "agent_run_id": str(snapshot.get("agent_run_id") or ""),
        "site_id": str(snapshot.get("site_id") or ""),
        "menu_limit": snapshot.get("menu_limit"),
        "status": "needs_review" if reasons else "evidence_ready",
        "reason_codes": sorted(reasons),
        "summary": {
            "home_count": home_count,
            "menu_count": menu_count,
            "detail_count": detail_count,
            "duplicate_formal_url_count": duplicate_formal_url_count,
        },
        "entries": [_public_ledger_entry(row) for row in ledger],
        "captures": public_captures,
        "formal_pages": _public_formal_pages(
            formal_capture_ids if readiness_profile is not None and policy_error is None else set(),
            capture_by_id=capture_by_id,
        ),
    }
    coverage = _public_coverage(snapshot)
    if coverage is not None:
        bundle["coverage"] = coverage
    return bundle


def _public_coverage(snapshot: Mapping[str, object]) -> dict[str, object] | None:
    """只暴露冻结快照与 Gap 计数，拒绝透传 DOM、URL 或判定原文。"""

    coverage = snapshot.get("coverage_snapshot")
    gap_summary = snapshot.get("coverage_gap_summary")
    if not isinstance(coverage, Mapping):
        return None
    summary = gap_summary if isinstance(gap_summary, Mapping) else {}
    return {
        "snapshot_id": str(coverage.get("snapshot_id") or ""),
        "snapshot_hash": str(coverage.get("snapshot_hash") or ""),
        "finalization_epoch": int(coverage.get("finalization_epoch") or 0),
        "normalization_version": str(coverage.get("normalization_version") or ""),
        "coverage_status": str(coverage.get("coverage_status") or ""),
        "required_page_types": _public_text_list(coverage.get("required_page_types")),
        "observed_page_types": _public_text_list(coverage.get("observed_page_types")),
        "missing_page_types": _public_text_list(coverage.get("missing_page_types")),
        "unrecognized_page_types": _public_text_list(coverage.get("unrecognized_page_types")),
        "gap_rule_count": int(summary.get("candidate_rule_count") or 0),
        "gap_terminal_rule_count": int(summary.get("terminal_rule_count") or 0),
        "evidence_insufficient_count": int(summary.get("evidence_insufficient_count") or 0),
    }


def _public_text_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _budget_is_valid(
    snapshot: Mapping[str, object],
    ledger: list[Mapping[str, object]],
) -> bool:
    menu_limit = snapshot.get("menu_limit")
    if ("menu_limit" not in snapshot or not ledger
        or (menu_limit is not None and (type(menu_limit) is not int or menu_limit <= 0))):
        return False
    slots = [row["execution_slot"] for row in ledger if row.get("execution_slot") is not None]
    if any(type(slot) is not int or slot <= 0 for slot in slots):
        return False
    if len(slots) != len(set(slots)) or (
        menu_limit is not None and (len(slots) > menu_limit or any(slot > menu_limit for slot in slots))
    ):
        return False
    executed = sum(int(row.get("menu_action_count") or 0) for row in ledger)
    executed_slots = sum(row.get("execution_slot") is not None and int(row.get("menu_action_count") or 0) == 1 for row in ledger)
    if executed != executed_slots or (menu_limit is not None and executed > menu_limit):
        return False
    return True


def _ledger_closure_is_valid(
    snapshot: Mapping[str, object], ledger: list[Mapping[str, object]],
) -> bool:
    """非成功终态与证据形状仍需复核，但不能误报为数值额度冲突。"""
    execution_ledger = [row for row in ledger if not web_discovery_is_audit_only(row)]
    statuses = [str(row.get("status") or "") for row in execution_ledger]
    if any(status not in _SUCCESS_LEDGER_STATUSES for status in statuses):
        return False
    if statuses.count("home_baseline") != 1:
        return False
    normalization_version = _snapshot_normalization_version(snapshot)
    if normalization_version is None:
        return False
    return all(
        _ledger_shape_is_valid(
            row,
            normalization_version=normalization_version,
        )
        for row in execution_ledger
    )


def _snapshot_normalization_version(snapshot: Mapping[str, object]) -> str | None:
    top_level_value = snapshot.get("normalization_version")
    top_level = (
        top_level_value.strip()
        if isinstance(top_level_value, str) and top_level_value.strip()
        else None
    )
    coverage = snapshot.get("coverage_snapshot")
    coverage_version = None
    if isinstance(coverage, Mapping):
        value = coverage.get("normalization_version")
        if isinstance(value, str) and value.strip():
            coverage_version = value.strip()
    if top_level is not None and coverage_version is not None:
        return top_level if top_level == coverage_version else None
    return top_level or coverage_version


def _ledger_shape_is_valid(
    row: Mapping[str, object],
    *,
    normalization_version: str,
) -> bool:
    status = str(row.get("status") or "")
    menu_count = int(row.get("menu_action_count") or 0)
    detail_count = int(row.get("detail_action_count") or 0)
    if status == "home_baseline":
        return persisted_home_baseline_has_valid_shape(
            row,
            normalization_version=normalization_version,
        )
    if status in {"menu_limit_skipped", "cross_origin_blocked"}:
        return (
            menu_count == 0
            and detail_count == 0
            and _absent(
                row,
                "execution_slot",
                "menu_action_execution_id",
                "menu_probe_capture_id",
                "menu_ready_capture_id",
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
            )
        )
    if status == "logical_target_reused":
        return (
            _present(row, "reused_capture_id", "reused_logical_page_id")
            and menu_count in {0, 1} and detail_count in {0, 1}
            and (menu_count == 1 or detail_count == 0)
            and (menu_count == 0 or _present(row, "menu_action_execution_id", "menu_probe_capture_id", "menu_ready_capture_id"))
            and (menu_count == 1 or _absent(row, "menu_intent_id", "menu_source_capture_id", "menu_action_execution_id", "menu_probe_capture_id", "menu_ready_capture_id"))
            and (detail_count == 1 or _absent(row, "detail_intent_id", "detail_source_capture_id", "detail_action_execution_id", "detail_probe_capture_id", "detail_ready_capture_id"))
            and (detail_count == 0 or _present(row, "detail_action_execution_id", "detail_probe_capture_id", "detail_ready_capture_id"))
        )
    if status == "menu_page_captured":
        if persisted_menu_page_is_submenu_expansion(row):
            return persisted_submenu_expansion_has_valid_shape(row)
        return (
            persisted_ordinary_menu_page_has_valid_shape(row)
            and row.get("execution_slot") is not None
            and _present(
                row,
                "menu_intent_id",
                "menu_source_capture_id",
                "menu_action_execution_id",
                "menu_probe_capture_id",
                "menu_ready_capture_id",
            )
            and row.get("menu_probe_capture_id") != row.get("menu_ready_capture_id")
            and _absent(
                row,
                "detail_intent_id",
                "detail_source_capture_id",
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
                "detail_page_type",
            )
        )
    if status != "detail_captured" or menu_count != 1:
        return False
    if row.get("execution_slot") is None or not _present(
        row,
        "menu_intent_id",
        "menu_source_capture_id",
        "menu_action_execution_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
    ):
        return False
    if row.get("menu_probe_capture_id") == row.get("menu_ready_capture_id"):
        return False
    page_type = str(row.get("menu_page_type") or "")
    if page_type in WEB_DETAIL_PAGE_TYPES:
        return detail_count == 0 and _absent(
            row,
            "detail_intent_id",
            "detail_source_capture_id",
            "detail_action_execution_id",
            "detail_probe_capture_id",
            "detail_ready_capture_id",
        )
    return (
        page_type in WEB_LIST_PAGE_TYPES
        and detail_count == 1
        and _present(
            row,
            "detail_intent_id",
            "detail_source_capture_id",
            "detail_action_execution_id",
            "detail_probe_capture_id",
            "detail_ready_capture_id",
        )
        and row.get("detail_probe_capture_id") != row.get("detail_ready_capture_id")
        and str(row.get("detail_page_type") or "") in WEB_DETAIL_PAGE_TYPES
    )


def _expand_action_closure(
    capture_ids: set[str],
    action_ids: set[str],
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
    action_by_id: Mapping[str, Mapping[str, object]],
    bootstrap_actions: frozenset[str] = frozenset(),
) -> bool:
    valid = True
    while True:
        before = (len(capture_ids), len(action_ids))
        for capture_id in tuple(capture_ids):
            capture = capture_by_id.get(capture_id)
            if capture is None:
                valid = False
                continue
            action_id = _text(capture.get("action_execution_id"))
            if action_id:
                action_ids.add(action_id)
        for action_id in tuple(action_ids):
            action = action_by_id.get(action_id)
            if action is None:
                valid = False
                continue
            for key in (
                "requested_source_capture_id",
                "source_capture_id",
                "result_capture_id",
            ):
                capture_id = _text(action.get(key))
                if capture_id:
                    capture_ids.add(capture_id)
                elif not (key == "requested_source_capture_id" and action_id in bootstrap_actions):
                    valid = False
        if before == (len(capture_ids), len(action_ids)):
            return valid


def _ledger_actions_are_valid(
    ledger: list[Mapping[str, object]],
    *,
    snapshot: Mapping[str, object],
    capture_by_id: Mapping[str, Mapping[str, object]],
    action_by_id: Mapping[str, Mapping[str, object]],
    required_action_ids: set[str],
    bootstrap_actions: frozenset[str] = frozenset(),
) -> bool:
    valid = True
    expected_by_id: dict[str, tuple[str, str, str]] = {}
    for row in ledger:
        if int(row.get("menu_action_count") or 0) == 1:
            expected_by_id[_text(row.get("menu_action_execution_id"))] = (
                _text(row.get("menu_intent_id")),
                _text(row.get("menu_source_capture_id")),
                _text(row.get("menu_probe_capture_id")),
            )
        if int(row.get("detail_action_count") or 0) == 1:
            expected_by_id[_text(row.get("detail_action_execution_id"))] = (
                _text(row.get("detail_intent_id")),
                _text(row.get("detail_source_capture_id")),
                _text(row.get("detail_probe_capture_id")),
            )
    for action_id in required_action_ids:
        expected = expected_by_id.get(action_id)
        if not _completed_action_is_valid(
            action_by_id.get(action_id),
            snapshot=snapshot,
            capture_by_id=capture_by_id,
            expected_intent=(expected[0] if expected else None),
            expected_requested=(expected[1] if expected else None),
            expected_result=(expected[2] if expected else None),
            bootstrap=action_id in bootstrap_actions,
        ):
            valid = False
    for row in ledger:
        submenu_expanded = persisted_submenu_expansion_has_valid_shape(row)
        for prefix in ("menu", "detail"):
            probe_id = _text(row.get(f"{prefix}_probe_capture_id"))
            ready_id = _text(row.get(f"{prefix}_ready_capture_id"))
            if not probe_id or not ready_id:
                continue
            probe = capture_by_id.get(probe_id)
            ready = capture_by_id.get(ready_id)
            if (
                not (submenu_expanded and prefix == "menu")
                and (
                probe_id == ready_id
                or probe is None
                or ready is None
                or _text(probe.get("action_execution_id"))
                == _text(ready.get("action_execution_id"))
                )
            ):
                valid = False
        for key in (
            "menu_probe_capture_id",
            "menu_ready_capture_id",
            "detail_probe_capture_id",
            "detail_ready_capture_id",
        ):
            capture_id = _text(row.get(key))
            if not capture_id:
                continue
            capture = capture_by_id.get(capture_id)
            if (
                capture is None
                or _text(capture.get("capture_role")) != "after_action"
                or not _text(capture.get("action_execution_id"))
            ):
                valid = False
        reused_prefix = _reused_target_prefix(row)
        for prefix in ("menu", "detail"):
            ready_id = _text(row.get(f"{prefix}_ready_capture_id"))
            if prefix == reused_prefix:
                # 类型只能来自认证的原分类，不给 fresh 观察抄写旧类型或身份。
                original = capture_by_id.get(_text(row.get("reused_capture_id")))
                if (
                    original is None
                    or original.get("logical_page_identity_status") != "verified"
                    or (
                        _text(row.get(f"{prefix}_page_type"))
                        and _text(original.get("page_type")) != _text(row.get(f"{prefix}_page_type"))
                    )
                    or (prefix == "detail" and original.get("page_type") not in WEB_DETAIL_PAGE_TYPES)
                    or ready_id == _text(row.get("reused_capture_id"))
                ):
                    valid = False
            elif ready_id and not (submenu_expanded and prefix == "menu"):
                ready = capture_by_id.get(ready_id)
                if ready is None or _text(ready.get("page_type")) != _text(
                    row.get(f"{prefix}_page_type")
                ):
                    valid = False
        if reused_prefix == "detail" and (
            row.get("menu_page_type") not in WEB_LIST_PAGE_TYPES
            or (row.get("detail_page_type") is not None and row.get("detail_page_type") not in WEB_DETAIL_PAGE_TYPES)
        ):
            valid = False
    return valid


def _ledger_arrivals_are_trusted(
    ledger: Sequence[Mapping[str, object]],
    *,
    action_by_id: Mapping[str, Mapping[str, object]],
) -> bool:
    """只有菜单和详情正式导航动作必须形成可信业务到达。"""

    for row in ledger:
        submenu_expanded = persisted_submenu_expansion_has_valid_shape(row)
        for prefix in ("menu", "detail"):
            if int(row.get(f"{prefix}_action_count") or 0) != 1:
                continue
            action = action_by_id.get(_text(row.get(f"{prefix}_action_execution_id")))
            if (
                not (submenu_expanded and prefix == "menu")
                and (
                action is None
                or _text(action.get("transition_proof")) != "trusted"
                or action.get("counts_as_business_arrival") is not True
                )
            ):
                return False
    return True


def _reused_target_prefix(row: Mapping[str, object]) -> str | None:
    """详情类型来自详情结果；动作次数仅描述是否已经操作，不能决定复用阶段。"""
    if row.get("status") != "logical_target_reused":
        return None
    stage = row.get("reused_target_stage")
    detail_count = int(row.get("detail_action_count") or 0)
    if stage == "menu" and detail_count == 0:
        return "menu"
    if stage == "detail_before_action" and detail_count == 0:
        return "detail"
    if stage == "detail_after_action" and detail_count == 1:
        return "detail"
    return None


def _formal_ready_capture_references(
    ledger: Sequence[Mapping[str, object]],
) -> list[str]:
    references = [
        capture_id
        for row in ledger
        if row.get("status") != "logical_target_reused"
        for key in ("menu_ready_capture_id", "detail_ready_capture_id")
        if not (
            key == "menu_ready_capture_id"
            and persisted_submenu_expansion_has_valid_shape(row)
        )
        if (capture_id := _text(row.get(key)))
    ]
    for row in ledger:
        if row.get("status") == "logical_target_reused":
            reused = _text(row.get("reused_capture_id"))
            if reused and reused not in references:
                references.append(reused)
            # 详情动作前/后复用都保留真实新列表；菜单自身重复则只保留观察。
            menu_ready = _text(row.get("menu_ready_capture_id"))
            if (
                _reused_target_prefix(row) == "detail"
                and row.get("menu_page_type") in WEB_LIST_PAGE_TYPES
                and menu_ready
                and menu_ready not in references
            ):
                references.append(menu_ready)
    return references


def _trusted_primary_content_supplement_ids(
    baseline_capture_ids: set[str],
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
    action_by_id: Mapping[str, Mapping[str, object]],
    snapshot: Mapping[str, object],
) -> tuple[set[str], bool]:
    """只接纳与正式基线同逻辑页、且声明父血缘的主体区补图。"""

    result: set[str] = set()
    valid = True
    for capture_id, capture in capture_by_id.items():
        if _text(capture.get("capture_purpose")) != "web_primary_content_evidence":
            continue
        lineage = capture.get("primary_content_capture")
        parent_capture_id = (
            _text(lineage.get("parent_capture_id")) if isinstance(lineage, Mapping) else ""
        )
        parent = capture_by_id.get(parent_capture_id)
        if (
            (parent_capture_id not in baseline_capture_ids and capture_id not in baseline_capture_ids)
            or parent is None
            or not _captures_share_logical_url(capture, parent)
            or _text(lineage.get("direct_parent_capture_id")) != _text(capture.get("parent_capture_id"))
            or not _primary_scroll_chain_reaches_baseline(
                capture_id, parent_capture_id, capture_by_id=capture_by_id, action_by_id=action_by_id,
                snapshot=snapshot,
            )
        ):
            valid = False
            continue
        result.add(capture_id)
        # unknown 的一次补判可让 ledger 指向主体图，原首屏仍是同逻辑页材料。
        result.add(parent_capture_id)
    return result, valid


def _primary_scroll_chain_reaches_baseline(
    capture_id: str,
    baseline_id: str,
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
    action_by_id: Mapping[str, Mapping[str, object]],
    snapshot: Mapping[str, object],
) -> bool:
    """原首屏通过最多两次真实动作的 requested→before→after 追溯，不信任自报父链。"""
    current = capture_id
    epoch = snapshot.get("finalization_epoch")
    if type(epoch) is not int or epoch < 0:
        return False
    for _ in range(2):
        capture = capture_by_id.get(current, {})
        action_id = _text(capture.get("action_execution_id"))
        action = action_by_id.get(action_id, {})
        before_id = _text(action.get("source_capture_id"))
        before = capture_by_id.get(before_id, {})
        requested_id = _text(action.get("requested_source_capture_id"))
        requested = capture_by_id.get(requested_id, {})
        if (
            not action_id or current != _text(action.get("result_capture_id"))
            or action.get("action_type") != "scroll"
            or action.get("web_capture_stage") != "web_primary_content_evidence"
            or action.get("target_mode") != "web_primary_content_candidate"
            or action.get("finalization_epoch") != epoch
            or any(
                row.get("finalization_epoch") != epoch or row.get("page_finalization_epoch") != epoch
                or not _capture_identity_is_valid(row, snapshot=snapshot)
                for row in (capture, before, requested)
            )
            or capture.get("parent_capture_id") != before_id
            or before.get("parent_capture_id") != requested_id
            or _text(before.get("action_execution_id")) != action_id
        ):
            return False
        if requested_id == baseline_id:
            return True
        current = requested_id
    return False


def _capture_logical_id(capture: Mapping[str, object]) -> str:
    stored = _text(capture.get("logical_page_id"))
    if capture.get("logical_page_identity_status") == "verified" and re.fullmatch(r"web-logical:[0-9a-f]{64}", stored):
        return stored
    raise ValueError("WEB_LOGICAL_ID_UNTRUSTED")


def _captures_share_logical_url(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    try:
        return _capture_logical_id(left) == _capture_logical_id(right)
    except ValueError:
        return False


def _duplicate_formal_url_count(
    capture_references: Sequence[str],
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
) -> int:
    urls: list[str] = []
    for capture_id in capture_references:
        capture = capture_by_id.get(capture_id)
        if capture is None:
            continue
        try:
            urls.append(_capture_logical_id(capture))
        except ValueError:
            continue
    return len(urls) - len(set(urls))


def _capture_is_formally_admitted(
    capture: Mapping[str, object] | None,
    *,
    profile: WebReadinessProfile,
) -> bool:
    if capture is None:
        return False
    admission = capture.get("web_evidence_admission")
    metrics = capture.get("pipeline_metrics")
    if not isinstance(metrics, Mapping):
        return False
    current_admission = assess_web_capture_evidence_admission(
        metrics,
        capture_role=str(capture.get("capture_role") or ""),
        profile=profile,
    )
    return bool(
        isinstance(admission, Mapping)
        and admission.get("kind") == "business_page"
        and admission.get("can_project_business_node") is True
        and admission.get("can_enter_formal_bundle") is True
        and current_admission.can_enter_formal_bundle
    )


def _public_formal_pages(
    capture_ids: set[str],
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    """同一逻辑 URL 只生成一个正式时间线节点，观察语义仍全部保留。"""

    pages: dict[str, dict[str, object]] = {}
    for capture_id in sorted(capture_ids):
        capture = capture_by_id.get(capture_id)
        if capture is None:
            continue
        try:
            page_key = _capture_logical_id(capture)
        except ValueError:
            continue
        page = pages.setdefault(
            page_key,
            {
                "logical_page_id": page_key,
                "logical_page_url": _redacted_url(_text(capture.get("url"))),
                "primary_capture_id": capture_id,
                "capture_purpose": _text(capture.get("capture_purpose")) or None,
                "page_type": _text(capture.get("page_type")) or None,
                "observation_capture_ids": [],
                "product_capabilities": [],
                "entry_status": None,
                "visible_layer": None,
            },
        )
        page["observation_capture_ids"].append(capture_id)
        current_primary = capture_by_id.get(_text(page.get("primary_capture_id")))
        if current_primary is None or _formal_capture_priority(capture) > _formal_capture_priority(
            current_primary
        ):
            page["primary_capture_id"] = capture_id
            page["capture_purpose"] = _text(capture.get("capture_purpose")) or None
            page["page_type"] = _text(capture.get("page_type")) or page["page_type"]
        if page["page_type"] is None and capture.get("page_type"):
            # 已知页附件不复制分类；展示沿用同身份基线的既有判定。
            page["page_type"] = _text(capture.get("page_type"))
        capability_assessment = capture.get("web_product_capability_assessment")
        if isinstance(capability_assessment, Mapping):
            capabilities = _public_text_list(capability_assessment.get("capabilities"))
            page["product_capabilities"] = sorted(
                set(page["product_capabilities"]) | set(capabilities)
            )
            page["entry_status"] = _text(capability_assessment.get("entry_status")) or None
            page["visible_layer"] = _text(capability_assessment.get("visible_layer")) or None
    return list(pages.values())


def _completed_action_is_valid(
    action: Mapping[str, object] | None,
    *,
    snapshot: Mapping[str, object],
    capture_by_id: Mapping[str, Mapping[str, object]],
    expected_intent: str | None,
    expected_requested: str | None,
    expected_result: str | None,
    bootstrap: bool = False,
) -> bool:
    if action is None:
        return False
    if (
        _text(action.get("task_id")) != _text(snapshot.get("task_id"))
        or _text(action.get("session_id")) != _text(snapshot.get("session_id"))
        or _text(action.get("status")) != "completed"
        or action.get("error_code") is not None
        or not _present(action, "started_at", "action_finished_at", "evidence_finished_at")
    ):
        return False
    if expected_intent is not None and _text(action.get("intent_id")) != expected_intent:
        return False
    requested_id = _text(action.get("requested_source_capture_id"))
    source_id = _text(action.get("source_capture_id"))
    result_id = _text(action.get("result_capture_id"))
    if expected_requested is not None and requested_id != expected_requested:
        return False
    if expected_result is not None and result_id != expected_result:
        return False
    requested = capture_by_id.get(requested_id)
    before = capture_by_id.get(source_id)
    after = capture_by_id.get(result_id)
    if (requested is None and not bootstrap) or before is None or after is None:
        return False
    action_id = _text(action.get("action_execution_id"))
    sequence = action.get("command_sequence")
    return (
        _text(before.get("capture_role")) == "before_action"
        and _text(after.get("capture_role")) == "after_action"
        and _text(before.get("action_execution_id")) == action_id
        and _text(after.get("action_execution_id")) == action_id
        and before.get("session_command_sequence") == sequence
        and after.get("session_command_sequence") == sequence
        and before.get("parent_capture_id") == (None if bootstrap else requested_id)
        and after.get("parent_capture_id") == source_id
    )


def _capture_identity_is_valid(
    capture: Mapping[str, object] | None,
    *,
    snapshot: Mapping[str, object],
    allow_bootstrap_blank: bool = False,
) -> bool:
    if capture is None:
        return False
    if (
        _text(capture.get("task_id")) != _text(snapshot.get("task_id"))
        or _text(capture.get("agent_run_id")) != _text(snapshot.get("agent_run_id"))
        or _text(capture.get("surface_session_id")) != _text(snapshot.get("session_id"))
        or _text(capture.get("surface_kind")) != "web"
        or _text(capture.get("capture_status")) != "captured"
        or not (allow_bootstrap_blank and capture.get("url") == "about:blank"
                or _redacted_url(_text(capture.get("url"))))
    ):
        return False
    mirror_fields = (
        ("task_id", "page_task_id"),
        ("agent_run_id", "page_agent_run_id"),
        ("surface_session_id", "page_surface_session_id"),
        ("parent_capture_id", "page_parent_capture_id"),
        ("action_execution_id", "page_action_execution_id"),
        ("capture_role", "page_capture_role"),
        ("session_command_sequence", "page_session_command_sequence"),
        ("capture_status", "page_capture_status"),
    )
    return all(capture.get(common) == capture.get(page) for common, page in mirror_fields)


def _required_artifacts_are_valid(
    capture_ids: set[str],
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
    artifacts: list[Mapping[str, object]],
) -> bool:
    by_capture: dict[str, list[Mapping[str, object]]] = {}
    for artifact in artifacts:
        by_capture.setdefault(_text(artifact.get("capture_id")), []).append(artifact)
    for capture_id in capture_ids:
        capture = capture_by_id.get(capture_id)
        rows = by_capture.get(capture_id, [])
        valid_rows = [row for row in rows if _artifact_is_valid(row)]
        baseline_types = {
            _text(row.get("artifact_type"))
            for row in valid_rows
            if _text(row.get("source")) == "baseline"
        }
        if not {"screenshot", "dom"}.issubset(baseline_types):
            return False
        if capture is None:
            return False
        if (
            _text(capture.get("capture_role")) == "after_action"
            and "action_trace" not in baseline_types
        ):
            return False
    return True


def _artifact_is_valid(row: Mapping[str, object]) -> bool:
    artifact_type = _text(row.get("artifact_type"))
    content_type = _text(row.get("content_type"))
    return (
        artifact_type in _PUBLIC_ARTIFACT_TYPES
        and _text(row.get("lifecycle_status")) in {"active", "archived"}
        and _text(row.get("source")) in {"baseline", "rule_supplement"}
        and bool(_CHECKSUM_PATTERN.fullmatch(_text(row.get("checksum"))))
        and (artifact_type != "screenshot" or content_type == "image/png")
    )


def _public_captures(
    capture_ids: set[str],
    *,
    capture_by_id: Mapping[str, Mapping[str, object]],
    artifacts: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_capture: dict[str, list[Mapping[str, object]]] = {}
    for artifact in artifacts:
        capture_id = _text(artifact.get("capture_id"))
        if capture_id in capture_ids and _artifact_is_valid(artifact):
            by_capture.setdefault(capture_id, []).append(artifact)
    result: list[dict[str, object]] = []
    for capture_id in sorted(capture_ids):
        capture = capture_by_id.get(capture_id)
        if capture is None:
            continue
        public_artifacts = [
            {
                "artifact_id": _text(row.get("artifact_id")),
                "artifact_type": _text(row.get("artifact_type")),
                "content_type": _text(row.get("content_type")),
                "checksum": _text(row.get("checksum")),
            }
            for row in sorted(
                by_capture.get(capture_id, []),
                key=lambda item: (_text(item.get("artifact_type")), _text(item.get("artifact_id"))),
            )
        ]
        result.append(
            {
                "capture_id": capture_id,
                "parent_capture_id": capture.get("parent_capture_id"),
                "url": _redacted_url(_text(capture.get("url"))),
                "role": _text(capture.get("capture_role")),
                "capture_purpose": _text(capture.get("capture_purpose")) or None,
                "primary_content_capture": _public_primary_content_capture(
                    capture.get("primary_content_capture")
                ),
                "page_type": _text(capture.get("page_type")) or None,
                "action_execution_id": _text(capture.get("action_execution_id")) or None,
                "artifacts": public_artifacts,
            }
        )
    return result


def _formal_capture_priority(capture: Mapping[str, object]) -> tuple[int, int, str]:
    purpose = _text(capture.get("capture_purpose"))
    sequence = capture.get("session_command_sequence")
    numeric_sequence = (
        sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0
    )
    return (
        _CAPTURE_PURPOSE_PRIORITY.get(purpose, 0),
        numeric_sequence,
        _text(capture.get("capture_id")),
    )


def _public_primary_content_capture(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None

    def coverage(name: str) -> float | None:
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return None
        return max(0.0, min(float(raw), 1.0))

    scroll_count = value.get("scroll_count")
    return {
        "parent_capture_id": _text(value.get("parent_capture_id")) or None,
        "direct_parent_capture_id": _text(value.get("direct_parent_capture_id")) or None,
        "scroll_count": (
            scroll_count
            if isinstance(scroll_count, int)
            and not isinstance(scroll_count, bool)
            and scroll_count >= 0
            else 0
        ),
        "before_ad_coverage": coverage("before_ad_coverage"),
        "after_ad_coverage": coverage("after_ad_coverage"),
    }


def _public_ledger_entry(row: Mapping[str, object]) -> dict[str, object]:
    """只投影报告关联所需的稳定 ID、计数和页型。"""

    keys = (
        "menu_entry_id",
        "menu_path",
        "decision",
        "execution_slot",
        "error_code",
        "menu_source_capture_id",
        "status",
        "menu_action_count",
        "menu_action_execution_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
        "menu_page_type",
        "detail_action_count",
        "detail_action_execution_id",
        "detail_probe_capture_id",
        "detail_ready_capture_id",
        "detail_page_type",
        "reused_capture_id",
        "reused_logical_page_id",
        "parent_menu_entry_id",
        "parent_logical_target_identity",
        "menu_depth",
        "exploration_path",
        "logical_target_identity",
        "diagnostic_type",
        "menu_interaction_outcome",
    )
    result = {key: row.get(key) for key in keys}
    result["canonical_url"] = _redacted_url(str(row.get("candidate_url") or ""))
    result["ad_landing_evidence"] = public_web_ad_landing_evidence(row.get("ad_landing_evidence"))
    if row.get("source_kind") == "runtime_discovered":
        result["source_kind"] = "runtime_discovered"
        result["discovery_source_capture_id"] = row.get("discovery_source_capture_id")
        if row.get("error_code") in WEB_DISCOVERY_AUDIT_REASONS:
            result["discovery_reason"] = row["error_code"]
    from services.api.app.application.web_primary_content_diagnostic import (
        primary_content_diagnostics,
    )

    diagnostics = primary_content_diagnostics(row.get("primary_content_diagnostics"))
    if diagnostics:
        result["primary_content_diagnostics"] = diagnostics
    if row.get("navigation_handoff_error") == "WEB_NAVIGATION_CURRENT_UNTRUSTED":
        result["navigation_handoff_error"] = "WEB_NAVIGATION_CURRENT_UNTRUSTED"
    if (
        _text(row.get("status")) == "detail_captured"
        and int(row.get("detail_action_count") or 0) == 0
        and _text(row.get("menu_page_type")) in WEB_DETAIL_PAGE_TYPES
    ):
        result["detail_ready_capture_id"] = row.get("menu_ready_capture_id")
        result["detail_page_type"] = row.get("menu_page_type")
    return result


def public_web_ad_landing_evidence(value: object) -> dict[str, object]:
    """广告只公开固定状态、脱敏 URL 与附件身份，不输出错误正文或凭据。"""
    if not isinstance(value, Mapping) or not value:
        return {}
    result = {
        key: item for key in ("status", "reason_code", "candidate_kind", "page_role", "page_type",
                              "capture_id", "logical_page_id")
        if isinstance(item := value.get(key), str) and re.fullmatch(r"[A-Za-z0-9_:\-]{1,160}", item)
    }
    for key in ("source_url", "landing_url"):
        result[key] = _redacted_url(str(value.get(key) or "")) or None
    if isinstance(value.get("fail_closed"), bool):
        result["fail_closed"] = value["fail_closed"]
    raw_refs = value.get("artifact_refs")
    raw_refs = raw_refs if isinstance(raw_refs, Mapping) else {}
    roles = {"source_dom", "source_screenshot", "landing_dom", "landing_screenshot"}
    result["artifact_refs"] = {
        key: item for key, item in raw_refs.items()
        if key in roles and isinstance(item, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", item)
    }
    raw_ids = value.get("artifact_ids")
    result["artifact_ids"] = [
        item for item in raw_ids if isinstance(item, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", item)
    ] if isinstance(raw_ids, (list, tuple)) else []
    return result


def _redacted_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parsed.port
        netloc = f"{hostname}:{port}" if port is not None else hostname
        return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", "", ""))
    except ValueError:
        return ""


def _ledger_capture_ids(ledger: Sequence[Mapping[str, object]]) -> set[str]:
    keys = (
        "reused_capture_id",
        "menu_source_capture_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
        "detail_source_capture_id",
        "detail_probe_capture_id",
        "detail_ready_capture_id",
    )
    return {_text(row.get(key)) for row in ledger for key in keys if _text(row.get(key))}


def _ledger_action_ids(ledger: Sequence[Mapping[str, object]]) -> set[str]:
    keys = ("menu_action_execution_id", "detail_action_execution_id")
    return {_text(row.get(key)) for row in ledger for key in keys if _text(row.get(key))}


def _unique_index(
    rows: Sequence[Mapping[str, object]],
    key: str,
) -> dict[str, Mapping[str, object]] | None:
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        value = _text(row.get(key))
        if not value or value in result:
            return None
        result[value] = row
    return result


def _mapping_rows(value: object) -> list[Mapping[str, object]] | None:
    if not isinstance(value, list | tuple):
        return None
    if not all(isinstance(row, Mapping) for row in value):
        return None
    return [row for row in value if isinstance(row, Mapping)]


def _present(row: Mapping[str, object], *keys: str) -> bool:
    return all(_text(row.get(key)) for key in keys)


def _absent(row: Mapping[str, object], *keys: str) -> bool:
    return all(not _text(row.get(key)) for key in keys)


def _text(value: object) -> str:
    return str(value or "").strip()


def _empty_review_bundle(snapshot: Mapping[str, object]) -> dict[str, object]:
    return {
        "task_id": _text(snapshot.get("task_id")),
        "agent_run_id": _text(snapshot.get("agent_run_id")),
        "site_id": _text(snapshot.get("site_id")),
        "status": "needs_review",
        "reason_codes": ["evidence_incomplete", "ledger_closure_invalid"],
        "summary": {
            "home_count": 0,
            "menu_count": 0,
            "detail_count": 0,
            "duplicate_formal_url_count": 0,
        },
        "entries": [],
        "captures": [],
    }


__all__ = ["build_web_evidence_bundle"]
