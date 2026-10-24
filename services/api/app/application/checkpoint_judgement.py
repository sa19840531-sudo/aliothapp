"""通用检查点 Judge：只比较已绑定的实际测量，不生成预期事实。"""

import math

from services.api.app.application.h5_checkpoint_observation import (
    checkpoint_facts,
    conditions_match,
)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def measure_checkpoint_series(samples):
    """两个真实动作后样本证明有界窗口，不把前后动作帧算连续观测。"""
    if not isinstance(samples, (list, tuple)) or len(samples) != 2:
        return {}
    facts = []
    geometries = []
    frames = []
    for sample in samples:
        if not isinstance(sample, dict):
            return {}
        snapshot = sample.get("snapshot", {})
        presentation = sample.get("presentation", {})
        if (
            presentation.get("topmost_layer_kind") != "page"
            or presentation.get("base_page_visibility") != "visible"
            or presentation.get("mutation_mode") != "natural"
        ):
            return {}
        if snapshot.get("truncated") or snapshot.get("loading") is True:
            return {}
        current = checkpoint_facts(snapshot)
        origin = sample.get("frame_origin", {})
        digest = sample.get("screenshot_sha256")
        if (
            current.get("player_visible") is not True
            or current.get("media_ready") is not True
            or not _finite(current.get("current_time"))
            or not _finite(current.get("document_time_origin_ms"))
            or not isinstance(digest, str)
            or len(digest) != 64
            or origin.get("status") != "bracketed"
            or origin.get("screenshot_sha256") != digest
            or not snapshot.get("url")
            or not _finite(sample.get("observed_at_ms"))
        ):
            return {}
        for bracket in (origin.get("before", {}), origin.get("after", {})):
            if (
                bracket.get("url") != snapshot["url"]
                or bracket.get("time_origin_ms") != current["document_time_origin_ms"]
            ):
                return {}
        video = next(item for item in snapshot["elements"] if item.get("tag") == "video")
        display = video.get("h5_player_presentation")
        if display is not None and (
            display.get("visible") is not True or display.get("clear") is not True
        ):
            return {}
        if video.get("media", {}).get("seeking") is not False:
            return {}
        bounds = video.get("rect") or video.get("bounds", {})
        if not all(_finite(bounds.get(key)) for key in ("x", "y", "width", "height")):
            return {}
        if bounds["width"] <= 0 or bounds["height"] <= 0:
            return {}
        viewport = snapshot.get("viewport", {})
        if not all(_finite(viewport.get(key)) and viewport[key] > 0 for key in ("width", "height")):
            return {}
        visible_width = max(
            0, min(viewport["width"], bounds["x"] + bounds["width"]) - max(0, bounds["x"])
        )
        visible_height = max(
            0, min(viewport["height"], bounds["y"] + bounds["height"]) - max(0, bounds["y"])
        )
        if visible_width * visible_height < bounds["width"] * bounds["height"] / 2:
            return {}
        frame = sample.get("video_frame")
        region = sample.get("video_region_sha256")
        if (
            not isinstance(frame, dict)
            or frame.get("status") != "presented"
            or frame.get("media_instance_id") != current.get("media_instance_id")
            or type(frame.get("presented_frames")) is not int
            or frame["presented_frames"] <= 0
            or type(frame.get("resource_generation")) is not int
            or frame["resource_generation"] <= 0
            or not all(_finite(frame.get(key)) and frame[key] > 0 for key in ("width", "height"))
            or frame.get("width") != video["media"].get("video_width")
            or frame.get("height") != video["media"].get("video_height")
            or not _finite(frame.get("media_time"))
            or abs(frame["media_time"] - current["current_time"]) > 0.15
            or not isinstance(region, str)
            or len(region) != 64
        ):
            return {}
        frames.append(frame)
        geometries.append(bounds)
        facts.append(current)
    elapsed = samples[1]["observed_at_ms"] - samples[0]["observed_at_ms"]
    if (
        not 500 <= elapsed <= 5000
        or samples[0]["snapshot"]["url"] != samples[1]["snapshot"]["url"]
        or any(
            facts[0].get(key) != facts[1].get(key)
            for key in ("media_instance_id", "document_time_origin_ms")
        )
        or frames[0]["resource_generation"] != frames[1]["resource_generation"]
        or frames[1]["presented_frames"] < frames[0]["presented_frames"]
    ):
        return {}
    paused = all(item.get("paused") is True for item in facts)
    if not paused and frames[1]["presented_frames"] <= frames[0]["presented_frames"]:
        return {}
    content_stable = (
        samples[0]["video_region_sha256"] == samples[1]["video_region_sha256"]
        if paused
        else frames[1]["presented_frames"] > frames[0]["presented_frames"]
    )
    return {
        "stable_frame": geometries[0] == geometries[1] and content_stable,
        "loaded_frame": True,
        "media_time_stopped": all(item.get("paused") is True for item in facts)
        and abs(facts[1]["current_time"] - facts[0]["current_time"]) <= 0.01,
        "sample_count": 2,
        "elapsed_ms": elapsed,
    }


def judge_checkpoint(checkpoint, *, before, after, manifests, series=(), action_receipt=None):
    """缺证据为不足，已测量的反例为失败；观察完成不等于通过。"""
    if len(manifests) != 2:
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "status": "evidence_insufficient",
            "checks": {},
            "missing": ["latest_manifest_not_pass"],
            "measured": {},
        }
    missing = []
    previous, current = checkpoint_facts(before), checkpoint_facts(after)
    from services.api.app.application.h5_payment_checkpoint import payment_checkpoint_facts

    if current:
        current.update(payment_checkpoint_facts(
            before=before, after=after, action_receipt=action_receipt,
        ))
    measured = measure_checkpoint_series(series)
    if measured and any(
        sample["snapshot"].get("url") != after.get("url")
        or any(
            checkpoint_facts(sample["snapshot"]).get(key) != current.get(key)
            for key in ("media_instance_id", "document_time_origin_ms")
        )
        for sample in series
    ):
        measured = {}
        missing.append("post_action_sample_scope_mismatch")
    current.update(measured)
    current["observation_complete"] = True
    if len(manifests) != 2 or any(item.get("quality_status") != "pass" for item in manifests):
        missing.append("latest_manifest_not_pass")
    if (
        not previous
        or not current
        or not before.get("url")
        or before.get("url") != after.get("url")
        or previous.get("document_time_origin_ms") is None
        or previous.get("document_time_origin_ms") != current.get("document_time_origin_ms")
        or previous.get("media_instance_id") != current.get("media_instance_id")
        or not conditions_match(checkpoint.entry_condition, previous)
    ):
        missing.append("entry_or_instance_unverified")
    if checkpoint.side_effect_policy != "read_only" or "order_created" in checkpoint.expected:
        missing.append("readonly_contract_missing")
    for requirement in checkpoint.evidence_requirements:
        index = 0 if requirement.capture_phase == "before_action" else 1
        kinds = {item.get("artifact_type") for item in manifests[index].get("artifacts", ())}
        kinds.add("action_receipt")
        if requirement.minimum_count > 1 and (
            "checkpoint_observation" not in kinds
            or not set(requirement.artifact_types) <= {"screenshot", "media_state"}
        ):
            missing.append("multi_sample_contract:" + requirement.role)
        count = measured.get("sample_count", 1) if index == 1 else 1
        if not set(requirement.artifact_types) <= kinds or requirement.minimum_count > count:
            missing.append("evidence_role:" + requirement.role)
    if any(
        key in checkpoint.exit_condition
        and (
            type(value) is not type(checkpoint.exit_condition[key])
            or value != checkpoint.exit_condition[key]
        )
        for key, value in checkpoint.expected.items()
    ):
        missing.append("contradictory_expected_and_exit")
    expected = {**dict(checkpoint.expected), **dict(checkpoint.exit_condition)}
    missing.extend("measurement:" + key for key in expected if key not in current)
    checks = {
        key: conditions_match({key: value}, current)
        for key, value in expected.items()
        if key in current
    }
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "status": "evidence_insufficient"
        if missing
        else "pass"
        if all(checks.values())
        else "fail",
        "checks": checks,
        "missing": missing,
        "measured": measured,
    }
