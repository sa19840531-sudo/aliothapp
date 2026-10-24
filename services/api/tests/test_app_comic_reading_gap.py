"""APP 漫画阅读缺同章节翻页和加载结果时，记证据不足，不判失败。"""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from services.api.app.application.observation_packet import (
    ObservationPacket,
    _compact_runtime_entry,
    _load_verified_script_screenshots,
)
from services.api.app.application.rule_judge import (
    _APP_COMIC_TURN_GAP,
    _APP_EPISODE_SWITCH_GAP,
    _APP_GROUP_GAP,
    _APP_SHARE_CHAIN_GAP,
    _agent_verifies_script_screenshots,
    _app_episode_switch_complete,
    _app_official_group_gap,
    _app_same_chapter_reading_observed,
    _app_share_chain_complete,
)


def _packet(**facts: object) -> ObservationPacket:
    shots = (("a", b"png", "image/png"), ("b", b"png", "image/png"))
    return ObservationPacket(
        observation_packet_id="pkt-1",
        task_id="task-1",
        page_capture_id="cap-1",
        rule_id="rule-1",
        rule_code="alioth_ops_10",
        evidence_artifact_ids=(),
        packet_payload={
            "page": {"surface_kind": "android"},
            "facts": facts,
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
        image_payloads=shots,
    )


def test_app_comic_without_same_chapter_turn_is_the_named_gap() -> None:
    assert _app_same_chapter_reading_observed(_packet(visible_text="漫画正文")) is False
    assert _APP_COMIC_TURN_GAP == "缺同章节正文的翻页或继续阅读操作及实际加载结果。"


def test_same_chapter_scroll_plus_later_shots_can_be_judged() -> None:
    packet = _packet(
        action_trace=[{"action": "scroll", "semantic_target": "继续阅读"}],
    )
    assert _app_same_chapter_reading_observed(packet) is True


def test_script_swipe_and_persisted_shots_can_be_judged() -> None:
    packet = _packet(
        script_recording={
            "verified": True,
            "shots": [
                {"name": "09", "artifact_id": "art-09"},
                {"name": "10", "artifact_id": "art-10"},
            ],
            "operations": [{"kind": "swipe", "shot_after": "10"}],
        },
    )
    assert _app_same_chapter_reading_observed(packet) is True


def test_turn_without_a_loaded_followup_stays_a_gap() -> None:
    packet = ObservationPacket(
        observation_packet_id="pkt-1",
        task_id="task-1",
        page_capture_id="cap-1",
        rule_id="rule-1",
        rule_code="alioth_ops_10",
        evidence_artifact_ids=(),
        packet_payload={
            "facts": {"action_trace": [{"operation": "swipe", "purpose": "翻页"}]},
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    assert _app_same_chapter_reading_observed(packet) is False


def test_app_share_without_the_full_chain_is_the_named_gap() -> None:
    packet = _packet(action_trace=[{"action": "click", "semantic_target": "分享"}])
    assert _app_share_chain_complete(packet) is False
    assert _APP_SHARE_CHAIN_GAP == (
        "缺分享链接生成、外部浏览器打开及源内容对应的完整操作链。"
    )


def test_complete_share_chain_can_be_judged() -> None:
    packet = _packet(
        action_trace=[
            {
                "action": "share",
                "semantic_target": "复制链接",
                "share_url": "https://example.com/s/1",
                "source_content": "content-1",
            },
            {"action": "open", "semantic_target": "外部浏览器", "content_id": "content-1"},
        ],
    )
    assert _app_share_chain_complete(packet) is True


def test_home_only_without_group_entry_is_the_named_gap() -> None:
    packet = _packet(page_type="home", visible_text="推荐")
    packet.packet_payload["page"]["page_type"] = "home"
    assert _app_official_group_gap(packet) is True
    assert _APP_GROUP_GAP.startswith("只查首页或个人中心就判无群入口")


def test_seen_group_entry_without_address_is_the_same_gap() -> None:
    packet = _packet(visible_text="官方群", action_trace=[])
    packet.packet_payload["page"]["page_type"] = "settings"
    assert _app_official_group_gap(packet) is True


def test_clicked_group_entry_with_address_can_be_judged() -> None:
    packet = _packet(
        action_trace=[
            {
                "action": "click",
                "semantic_target": "官方群",
                "result": "https://t.me/example",
            }
        ],
    )
    packet.packet_payload["page"]["page_type"] = "settings"
    assert _app_official_group_gap(packet) is False


def test_episode_one_only_is_the_named_gap() -> None:
    packet = _packet(visible_text="第1集", media_state={"ready_state": 2})
    assert _app_episode_switch_complete(packet) is False
    assert _APP_EPISODE_SWITCH_GAP == "缺同一剧集从第1集切到第2集的操作、集数变化及起播证据。"


def test_script_screenshots_are_agent_evidence_like_hjll() -> None:
    recording = {
        "verified": True,
        "shots": [
            {"name": "01", "artifact_id": "shot-01", "path": r"F:\secret\01.png"},
            {"name": "29", "artifact_id": "shot-29"},
        ],
        "operations": [{"kind": "swipe", "shot_after": "29"}],
    }
    compact = _compact_runtime_entry(
        {"action": "observe", "script_recording": recording},
        kind="action",
    )
    assert "path" not in str(compact["script_recording"])
    assert compact["script_recording"]["shots"] == [
        {"name": "01", "artifact_id": "shot-01"},
        {"name": "29", "artifact_id": "shot-29"},
    ]
    packet = replace(
        _packet(action_log=[compact]),
        image_payloads=(
            ("shot-01", b"\x89PNG", "image/png"),
            ("shot-29", b"\x89PNG", "image/png"),
        ),
    )
    assert _agent_verifies_script_screenshots(packet) is False
    unverified = replace(
        _packet(
            action_log=[
                {
                    "script_recording": {
                        "verified": False,
                        "shots": [{"name": "36", "artifact_id": "shot-36"}],
                    }
                }
            ]
        ),
        image_payloads=(("shot-36", b"\x89PNG", "image/png"),),
    )
    assert _agent_verifies_script_screenshots(unverified) is False
    missing = replace(packet, image_payloads=(("other", b"\x89PNG", "image/png"),))
    assert _agent_verifies_script_screenshots(missing) is False


def test_verified_script_screenshots_load_into_agent_images() -> None:
    payload = {
        "facts": {
            "action_log": [
                {
                    "script_recording": {
                        "verified": True,
                        "shots": [{"name": "01", "artifact_id": "shot-01"}],
                    }
                }
            ]
        }
    }

    class _Reader:
        async def read_png(self, artifact_id: str):
            return object(), b"\x89PNG\r\n\x1a\n" + artifact_id.encode()

    with patch(
        "services.api.app.application.app_screenshot_artifacts.get_app_screenshot_artifact_service",
        return_value=_Reader(),
    ):
        loaded = asyncio.run(_load_verified_script_screenshots(payload))
    assert loaded == (("shot-01", b"\x89PNG\r\n\x1a\nshot-01"),)


def test_unverified_script_shots_still_load_and_a_missing_file_is_skipped() -> None:
    payload = {
        "facts": {
            "action_log": [
                {
                    "script_recording": {
                        "verified": False,
                        "shots": [
                            {"name": "01", "artifact_id": "shot-01"},
                            {"name": "02", "artifact_id": "missing"},
                        ],
                    }
                }
            ]
        }
    }

    class _Reader:
        async def read_png(self, artifact_id: str):
            if artifact_id == "missing":
                raise FileNotFoundError(artifact_id)
            return object(), b"\x89PNG\r\n\x1a\n" + artifact_id.encode()

    with patch(
        "services.api.app.application.app_screenshot_artifacts.get_app_screenshot_artifact_service",
        return_value=_Reader(),
    ):
        loaded = asyncio.run(_load_verified_script_screenshots(payload))
    assert loaded == (("shot-01", b"\x89PNG\r\n\x1a\nshot-01"),)


def test_episode_switch_with_playback_can_be_judged() -> None:
    packet = _packet(
        action_trace=[
            {"action": "click", "semantic_target": "第1集", "result": "起播"},
            {"action": "click", "semantic_target": "第2集", "result": "第2集 current_time"},
        ],
    )
    assert _app_episode_switch_complete(packet) is True
