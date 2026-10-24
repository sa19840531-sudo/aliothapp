"""有界原生结构命题及显式规则计划，不推断产品级能力不存在。"""

import json
from copy import deepcopy
from functools import lru_cache

from services.api.app.domain.protocol import stable_protocol_hash

LOCAL_SCOPE = "current_document_light_dom"
LOCAL_PREDICATE = "native_video_element_present"


def validate_web_local_structure_rule(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "scope",
        "predicate",
        "reuse_policy",
    }:
        raise ValueError("Web 局部结构规则必须使用完整闭集合同")
    if (
        value["schema_version"] != "web-local-structure-rule.v1"
        or value["scope"] != LOCAL_SCOPE
        or value["predicate"] != LOCAL_PREDICATE
        or value["reuse_policy"] not in {"per_occurrence", "same_material_context"}
    ):
        raise ValueError("Web 局部结构规则包含不支持的命题、作用域或复用策略")
    return dict(value)


def assess_web_local_structure(snapshot, *, document_time_origin_ms, stable):
    census = snapshot.get("web_native_structure") if isinstance(snapshot, dict) else None
    result = {
        "schema_version": "web-local-structure.v1",
        "scope": LOCAL_SCOPE,
        "predicate": LOCAL_PREDICATE,
        "status": "unknown",
        "product_capability_status": "unknown",
        "limitations": [
            "仅本次当前文档light DOM，不代表全站或其他状态",
            "不推断画外内容、自绘播放器或未观察的嵌入文档",
        ],
        "reason_code": "WEB_LOCAL_STRUCTURE_UNPROVEN",
    }
    if not isinstance(census, dict):
        return result
    if (
        not stable
        or census.get("schema_version") != "web-native-structure.v1"
        or census.get("scope") != LOCAL_SCOPE
        or census.get("predicate") != LOCAL_PREDICATE
        or census.get("complete") is not True
        or census.get("unobservable_reasons") != []
        or snapshot.get("element_limit_reached") is True
        or snapshot.get("media_truncated") is True
        or snapshot.get("truncated") is True
        or snapshot.get("collection_error")
        or snapshot.get("document", {}).get("time_origin_ms") != document_time_origin_ms
        or census.get("document_time_origin_ms") != document_time_origin_ms
        or type(census.get("native_video_count")) is not int
        or census["native_video_count"] < 0
    ):
        return result
    status = "present" if census["native_video_count"] else "local_absent"
    if census.get("status") != status:
        return result
    return {
        **result,
        "status": status,
        "reason_code": "WEB_LOCAL_NATIVE_STRUCTURE_" + status.upper(),
        "document_time_origin_ms": document_time_origin_ms,
        "native_video_count": census["native_video_count"],
    }


def plan_web_local_structure_rule(declaration, proof, *, rule_version, rule_contract_hash):
    declaration = validate_web_local_structure_rule(declaration)
    if declaration is None:
        return None
    identity = {
        "declaration": declaration,
        "rule_version": rule_version,
        "rule_contract_hash": rule_contract_hash,
        **{
            key: proof.get(key)
            for key in (
                "revision_id",
                "revision",
                "material_hash",
                "position",
                "account_state",
                "visible_layer",
                "occurrence_id",
                "capture_id",
                "scope",
                "predicate",
                "status",
            )
        },
    }
    frozen = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if declaration["reuse_policy"] == "same_material_context":
        return deepcopy(_cached_plan(frozen))
    return _build_plan(frozen)


@lru_cache(maxsize=512)
def _cached_plan(frozen):
    return _build_plan(frozen)


def _build_plan(frozen):
    identity = json.loads(frozen)
    valid = identity["scope"] == LOCAL_SCOPE and identity["predicate"] == LOCAL_PREDICATE
    status = identity["status"] if valid else "unknown"
    return {
        "status": "not_applicable"
        if status == "local_absent"
        else ("admissible" if status == "present" else "evidence_insufficient"),
        "reason_code": "WEB_LOCAL_NATIVE_STRUCTURE_" + str(status).upper(),
        "revision_id": identity["revision_id"],
        "scope": LOCAL_SCOPE,
        "predicate": LOCAL_PREDICATE,
        "reuse_key": stable_protocol_hash(identity),
        "rule_contract_hash": identity["rule_contract_hash"],
        "rule_version": identity["rule_version"],
        "declaration": identity["declaration"],
        "identity_json": frozen,
    }
