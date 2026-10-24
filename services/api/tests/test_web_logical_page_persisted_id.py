"""报告聚合优先消费服务器持久身份，脱敏地址不能反向充当去重键。"""

from services.api.app.application.web_evidence_bundle import _public_formal_pages


def test_redacted_identical_urls_preserve_distinct_persisted_spa_identities():
    captures = {
        "one": {
            "capture_id": "one",
            "url": "https://example.test/",
            "logical_page_id": "web-logical:" + "1" * 64,
        },
        "two": {
            "capture_id": "two",
            "url": "https://example.test/",
            "logical_page_id": "web-logical:" + "2" * 64,
        },
    }
    for capture in captures.values():
        capture["logical_page_identity_status"] = "verified"
    pages = _public_formal_pages(set(captures), capture_by_id=captures)
    assert len(pages) == 2
    assert {page["logical_page_id"] for page in pages} == {
        "web-logical:" + "1" * 64,
        "web-logical:" + "2" * 64,
    }


def test_reused_entry_references_original_material_without_duplicate_formal_page():
    from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot

    snapshot = _valid_snapshot()
    snapshot["ledger"].append(
        {
            "menu_entry_id": "alias",
            "status": "logical_target_reused",
            "reused_target_stage": "menu",
            "execution_slot": 2,
            "menu_action_count": 0,
            "detail_action_count": 0,
            "reused_capture_id": "menu-ready:after",
            "reused_logical_page_id": "web-logical:" + "d" * 64,
        }
    )
    result = build_web_evidence_bundle(snapshot)
    assert result["status"] == "evidence_ready"
    assert len(result["formal_pages"]) == 2
    assert result["summary"]["duplicate_formal_url_count"] == 0
    snapshot["ledger"][-1]["reused_logical_page_id"] = "web-logical:" + "f" * 64
    assert build_web_evidence_bundle(snapshot)["status"] == "needs_review"
