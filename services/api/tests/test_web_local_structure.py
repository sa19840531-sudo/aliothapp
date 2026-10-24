"""当前 light DOM 的局部命题不能冒充全站产品能力。"""

import json
import shutil
import subprocess

import pytest


def test_actual_census_script_is_bounded_and_does_not_infer_through_unreadable_surfaces():
    from services.api.app.infrastructure.browser.web_native_structure import (
        WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT,
    )

    node = shutil.which("node")
    assert node, "本地 Node 用于执行受控 DOM 上的实际 census 脚本"
    for kind, expected in [
        ("empty", "local_absent"),
        ("video", "present"),
        ("canvas", "unknown"),
        ("iframe", "unknown"),
        ("custom", "unknown"),
        ("large", "unknown"),
    ]:
        setup = (
            "const document={readyState:'complete',documentElement:{},body:{},querySelectorAll(selector){"
            "if(selector==='*')return Array.from({length:KIND==='large'?10001:2},()=>({localName:KIND==='custom'?'x-player':'main',shadowRoot:null}));"
            "return selector===KIND?[{}]:[];}};const performance={timeOrigin:1234};"
        )
        source = (
            "const KIND="
            + json.dumps(kind)
            + ";"
            + setup
            + "process.stdout.write(JSON.stringify("
            + WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT
            + "));"
        )
        result = subprocess.run([node, "-e", source], capture_output=True, text=True, check=True)
        census = json.loads(result.stdout)
        assert census["status"] == expected
        assert census["scope"] == "current_document_light_dom"
        assert census["document_time_origin_ms"] == 1234


@pytest.mark.parametrize(
    "mutation", ["wrong_epoch", "truncated", "missing", "canvas", "not_stable"]
)
def test_local_negative_requires_complete_consistent_current_document(mutation):
    from services.api.app.application.web_local_structure import assess_web_local_structure

    census = {
        "schema_version": "web-native-structure.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "status": "local_absent",
        "document_time_origin_ms": 1234,
        "complete": True,
        "unobservable_reasons": [],
        "native_video_count": 0,
    }
    snapshot = {
        "web_native_structure": census,
        "document": {"time_origin_ms": 1234},
        "element_limit_reached": False,
    }
    if mutation == "wrong_epoch":
        census["document_time_origin_ms"] = 5678
    if mutation == "truncated":
        snapshot["element_limit_reached"] = True
    if mutation == "missing":
        snapshot.pop("web_native_structure")
    if mutation == "canvas":
        census["unobservable_reasons"] = ["canvas"]
    result = assess_web_local_structure(
        snapshot, document_time_origin_ms=1234, stable=mutation != "not_stable"
    )
    assert result["status"] == "unknown"
    assert result["product_capability_status"] == "unknown"


def test_local_absence_does_not_disable_generic_video_rule_and_reuse_identity_is_complete():
    from services.api.app.application.web_local_structure import plan_web_local_structure_rule

    proof = {
        "schema_version": "web-local-structure.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "status": "local_absent",
        "revision_id": "revision-1",
        "revision": 1,
        "material_hash": "a" * 64,
        "position": {"scroll_x": 0, "scroll_y": 0},
        "account_state": "unknown",
        "visible_layer": "business",
        "occurrence_id": "occurrence-1",
        "capture_id": "capture-1",
    }
    declared = {
        "schema_version": "web-local-structure-rule.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "reuse_policy": "same_material_context",
    }
    assert (
        plan_web_local_structure_rule(None, proof, rule_version="1", rule_contract_hash="b" * 64)
        is None
    )
    first = plan_web_local_structure_rule(
        declared, proof, rule_version="1", rule_contract_hash="b" * 64
    )
    assert first["status"] == "not_applicable"
    assert first["rule_contract_hash"] == "b" * 64
    assert first["rule_version"] == "1"
    assert first["declaration"] == declared
    frozen_identity = json.loads(first["identity_json"])
    assert frozen_identity["rule_version"] == "1"
    assert frozen_identity["rule_contract_hash"] == "b" * 64
    from hashlib import sha256

    assert sha256(first["identity_json"].encode("utf-8")).hexdigest() == first["reuse_key"]
    first["declaration"]["scope"] = "forged-global"
    again = plan_web_local_structure_rule(
        declared, proof, rule_version="1", rule_contract_hash="b" * 64
    )
    assert again["declaration"]["scope"] == "current_document_light_dom"
    for key, value in [
        ("material_hash", "c" * 64),
        ("position", {"scroll_x": 1, "scroll_y": 0}),
        ("account_state", "authenticated"),
        ("visible_layer", "popup"),
        ("occurrence_id", "occurrence-2"),
    ]:
        changed = plan_web_local_structure_rule(
            declared, {**proof, key: value}, rule_version="1", rule_contract_hash="b" * 64
        )
        assert changed["reuse_key"] != first["reuse_key"]
