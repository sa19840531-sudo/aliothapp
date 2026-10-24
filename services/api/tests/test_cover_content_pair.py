"""封面与正文必须是同一条内容，广告图和列表补采不能混进去。"""

from services.api.app.application.rule_capability_hybrid import run_hybrid_capability_tools

CONFIG = {
    "batch_group": "cover-content",
    "deterministic_tools": ["visual_semantic_compare_tool"],
}


def _result(payload):
    results = run_hybrid_capability_tools(packet_payload=payload, capability_config=CONFIG)
    return results[0]


def test_h5_list_recapture_does_not_count_as_opening_the_cover():
    result = _result(
        {
            "facts": {
                "action_log": [
                    {
                        "action": "observe",
                        "page_type": "video_list",
                        "source_capture_id": "list-1",
                        "semantic_target": "同一封面",
                    }
                ]
            }
        }
    )
    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["cover_content_list_observation_only"]


def test_ad_material_is_rejected_from_the_cover_content_set():
    result = _result(
        {
            "facts": {
                "cover_content_materials": {
                    "cover": {
                        "capture_id": "cover-1",
                        "content_key": "影片甲",
                        "material_kind": "ad",
                    },
                    "content": {
                        "capture_id": "body-1",
                        "page_type": "video_detail",
                        "content_key": "影片甲",
                        "material_kind": "playback",
                    },
                }
            }
        }
    )
    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["cover_content_ad_material_mixed"]


def test_same_content_cover_and_playback_are_one_set():
    result = _result(
        {
            "facts": {
                "action_log": [
                    {
                        "action": "click",
                        "semantic_target": "影片甲",
                        "content_key": "影片甲",
                        "source_capture_id": "cover-1",
                        "result_capture_id": "play-1",
                        "source_page_type": "video_list",
                        "result_page_type": "video_detail",
                        "result_material_kind": "playback",
                    }
                ]
            }
        }
    )
    assert result["status"] == "ok"
    assert result["facts"]["complete"] is True
    assert result["facts"]["cover_capture_id"] == "cover-1"
    assert result["facts"]["content_capture_id"] == "play-1"
