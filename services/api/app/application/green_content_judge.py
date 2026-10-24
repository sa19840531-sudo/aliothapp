"""102三风险独立语义判定；受控政策与范围材料缺失时不发明标准。"""

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import replace

from services.api.app.application.green_rule_evidence import green_page_preflight
from services.api.app.application.judgment_wording_policy import sanitize_semantic_judge_output

_RISKS = ("minor_safety", "political_safety", "terrorism_safety")


def validate_content_policies(value):
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - set(_RISKS):
        raise ValueError("102受控政策风险类型无效")
    result = {}
    for key, policy in value.items():
        if (
            not isinstance(policy, Mapping)
            or set(policy) != {"policy_id", "version", "text", "sha256"}
            or not all(isinstance(item, str) and item.strip() for item in policy.values())
            or hashlib.sha256(policy["text"].encode("utf-8")).hexdigest() != policy["sha256"]
        ):
            raise ValueError("102受控政策身份、版本或正文摘要不完整")
        result[key] = dict(policy)
    return result


def validate_content_scope_contract(value):
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"id", "regions", "ad_contract_id", "content_capture_roles"}
        or not all(
            isinstance(value[key], str) and value[key].strip() for key in ("id", "ad_contract_id")
        )
        or not isinstance(value["regions"], Mapping)
        or not value["regions"]
        or not all(isinstance(key, str) and key.strip() for key in value["regions"])
        or not isinstance(value["content_capture_roles"], list)
        or not value["content_capture_roles"]
        or any(
            not isinstance(role, str) or role not in {"content_only", "content_safety"}
            for role in value["content_capture_roles"]
        )
    ):
        raise ValueError("102范围须冻结完整目标区域及专用内容原件角色")
    if any(not _covered(rect, [rect]) for rect in value["regions"].values()):
        raise ValueError("102冻结范围矩形无效")
    return copy.deepcopy(dict(value))


async def judge_green_content(*, bundle, definition, packet, views, semantic_judge):
    outcome = green_page_preflight("102", bundle)
    policies = validate_content_policies(definition.judge_config.get("green_content_policies"))
    scope, content_ids = _scope(definition.judge_config.get("green_content_scope_contract"), views)
    outcome["subitems"][-1] = scope
    images = tuple(image for image in packet.image_payloads if image[0] in content_ids)
    for index, risk in enumerate(_RISKS):
        if risk not in policies or not images or semantic_judge is None:
            continue
        policy = policies[risk]
        payload = {
            "schema_version": packet.packet_payload.get("schema_version", "observation-packet.v1"),
            "identity": copy.deepcopy(packet.packet_payload.get("identity", {})),
            "rule": copy.deepcopy(packet.packet_payload.get("rule", {})),
            "page": {"page_capture_id": packet.page_capture_id},
            "facts": {"content_artifact_ids": sorted(content_ids)},
        }
        payload.setdefault("rule", {})["check_point"] = (
            f"仅检查{risk}，按受控政策{policy['policy_id']}版本{policy['version']}独立判断。"
            "不得扩大到其他风险，不猜精确年龄；年龄、政策或语义不确定必须复核。\n" + policy["text"]
        )
        payload["rule"]["criteria"] = {"llm_checks": [payload["rule"]["check_point"]]}
        selected = replace(
            packet,
            packet_payload=payload,
            image_payloads=images,
            evidence_artifact_ids=tuple(sorted(content_ids)),
        )
        result = await semantic_judge.judge(
            packet=selected,
            tool_results=[
                {
                    "tool_name": "green_controlled_content_policy",
                    "readonly": True,
                    "risk": risk,
                    "policy_id": policy["policy_id"],
                    "version": policy["version"],
                    "sha256": policy["sha256"],
                    "scope_complete": not scope["missing"],
                }
            ],
        )
        result = sanitize_semantic_judge_output(result)
        refs = set(result.evidence_refs) | {
            ref for finding in result.findings for ref in finding.evidence_refs
        }
        if not refs or not refs.issubset(content_ids):
            outcome["subitems"][index]["missing"].append("risk_evidence_reference_invalid:" + risk)
            continue
        status = result.conclusion
        if (
            status == "warning"
            or result.conflicts
            or result.review_reason
            or (
                status in {"pass", "fail"}
                and result.confidence < definition.manual_review_policy.get("min_confidence", 0.75)
            )
        ):
            status = "manual_review"
        missing = list(result.missing_evidence)
        if missing and status == "pass":
            status = "evidence_insufficient"
        outcome["subitems"][index] = {
            "key": risk,
            "status": status,
            "evaluated": status != "evidence_insufficient",
            "confidence": result.confidence if status != "evidence_insufficient" else None,
            "reason": result.review_reason or result.reasoning_summary,
            "missing": missing,
            "evidence_refs": sorted(refs),
            "policy_reference": {key: policy[key] for key in ("policy_id", "version", "sha256")},
            "findings": [finding.model_dump(mode="json") for finding in result.findings],
            "conflicts": list(result.conflicts),
            "review_reason": result.review_reason,
        }
    items = outcome["subitems"]
    missing = list(dict.fromkeys(gap for item in items for gap in item["missing"]))
    states = {item["status"] for item in items}
    status = (
        "fail"
        if "fail" in states
        else "manual_review"
        if "manual_review" in states
        else "evidence_insufficient"
        if missing or "evidence_insufficient" in states
        else "pass"
    )
    outcome.update(
        execution_status=status,
        missing_evidence=missing,
        confidence=0.0
        if status == "evidence_insufficient"
        else min(
            (
                item["confidence"]
                for item in items
                if item["status"] == status and item["confidence"] is not None
            ),
            default=0.0,
        ),
        reason="102按三类受控政策独立判读，范围与其他缺项分别保留",
    )
    return outcome


def _scope(contract, views):
    item = {
        "key": "content_scope",
        "status": "evidence_insufficient",
        "evaluated": False,
        "confidence": None,
        "reason": "102范围覆盖和广告排除尚缺受控绑定",
        "missing": ["frozen_content_scope_contract"],
        "evidence_refs": [],
    }
    if validate_content_scope_contract(contract) is None:
        return item, set()
    scopes = [
        (view, view.get("content_scope"))
        for view in views
        if not view.issues and isinstance(view.get("content_scope"), Mapping)
    ]
    if len(scopes) != 1:
        return item, set()
    view, scope = scopes[0]
    exclusion = scope.get("advertisement_exclusion", {})
    content = scope.get("content_artifact_ids", [])
    owners, roles = view.get("artifact_capture_ids", {}), view.get("capture_roles", {})
    excluded = exclusion.get("excluded_artifact_ids") if isinstance(exclusion, Mapping) else None
    if (
        scope.get("scope_contract_id") != contract["id"]
        or not isinstance(exclusion, Mapping)
        or exclusion.get("contract_id") != contract["ad_contract_id"]
        or not isinstance(exclusion.get("source"), str)
        or exclusion["source"] not in {"platform_ad_metadata", "frozen_ad_selector"}
        or not isinstance(content, list)
        or not content
        or not all(
            isinstance(ref, str)
            and ref in view.get("png_artifact_ids", ())
            and roles.get(owners.get(ref)) in contract["content_capture_roles"]
            for ref in content
        )
        or not isinstance(excluded, list)
        or not all(isinstance(ref, str) and ref in view.artifact_ids for ref in excluded)
        or set(content) & set(excluded)
        or set(content) | set(excluded) != set(view.get("png_artifact_ids", ()))
    ):
        return item, set()
    regions = scope.get("regions")
    if (
        not isinstance(regions, list)
        or not all(isinstance(row, Mapping) for row in regions)
        or not all(isinstance(row.get("id"), str) and row["id"] for row in regions)
        or len({row["id"] for row in regions}) != len(regions)
        or {row["id"] for row in regions} != set(contract["regions"])
    ):
        return item, set(content)
    gaps = []
    for region in regions:
        if region.get("required_rect") != contract["regions"][region["id"]] or not _covered(
            contract["regions"][region["id"]], region.get("covered_rects")
        ):
            gaps.append("content_region_coverage:" + str(region.get("id")))
    item.update(
        status="evidence_insufficient" if gaps else "pass",
        evaluated=not gaps,
        confidence=None if gaps else 1.0,
        missing=gaps,
        evidence_refs=content,
        reason="102原定内容区域已完整覆盖且广告排除来源已核验"
        if not gaps
        else "102已核对内容原件与广告排除，部分区域仍缺覆盖",
    )
    return item, set(content)


def _covered(required, rectangles):
    import math

    def rect(value):
        if not isinstance(value, Mapping):
            return None
        parts = tuple(value.get(key) for key in ("x", "y", "width", "height"))
        try:
            valid = all(type(v) in {int, float} and math.isfinite(v) for v in parts)
        except (OverflowError, ValueError):
            valid = False
        return parts if valid and min(parts[2:]) > 0 else None

    target = rect(required)
    if target is None or not isinstance(rectangles, list):
        return False
    rows = [rect(value) for value in rectangles]
    if any(row is None for row in rows):
        return False
    x, y, width, height = target
    ys = sorted(
        {
            y,
            y + height,
            *(max(y, min(y + height, v)) for row in rows for v in (row[1], row[1] + row[3])),
        }
    )
    for top, bottom in zip(ys, ys[1:], strict=False):
        intervals = sorted(
            (row[0], row[0] + row[2]) for row in rows if row[1] <= top and row[1] + row[3] >= bottom
        )
        cursor = x
        for left, right in intervals:
            if left > cursor:
                break
            cursor = max(cursor, right)
        if cursor < x + width:
            return False
    return True
