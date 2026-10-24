"""检查点只使用已校验快照的原生事实；不是最终规则 Judge。"""

import math

_MEASURED_FACTS = frozenset(
    {
        "player_visible",
        "media_instance_id",
        "paused",
        "playing",
        "media_ready",
        "current_time",
        "document_time_origin_ms",
        "fullscreen_active",
        "observation_complete",
        "stable_frame",
        "loaded_frame",
        "media_time_stopped",
        "payment_display_visible",
        "payment_plan_selected",
    }
)


def supports_checkpoint(checkpoint):
    # 未接线的测量义务先阻断准入，不能消耗模型和动作后再发现无法判定。
    return (
        not set(checkpoint.entry_condition).intersection(
            {
                "observation_complete",
                "stable_frame",
                "loaded_frame",
                "media_time_stopped",
                "payment_plan_selected",
            }
        )
        and all(
            set(conditions) <= _MEASURED_FACTS
            for conditions in (
                checkpoint.entry_condition,
                checkpoint.expected,
                checkpoint.exit_condition,
            )
        )
        and all(
            requirement.minimum_count <= 2
            and (requirement.minimum_count == 1 or requirement.capture_phase == "after_action")
            and (
                requirement.minimum_count == 1
                or set(requirement.artifact_types)
                <= {
                    "screenshot",
                    "media_state",
                }
            )
            and "network" not in requirement.artifact_types
            for requirement in checkpoint.evidence_requirements
        )
    )


def checkpoint_facts(snapshot, *, surface=None):
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("collection_error")
        or snapshot.get("capture_degraded")
        or snapshot.get("budget_truncated")
        or snapshot.get("element_limit_reached")
        or snapshot.get("truncated")
        or snapshot.get("loading") is True
    ):
        return {}
    facts = {"surface": surface}
    from services.api.app.application.h5_probe_targets import (
        payment_plan_structure,
        visible_probe_element,
    )

    if any(
        isinstance(item, dict)
        and payment_plan_structure(item)
        and visible_probe_element(item, snapshot)
        for item in snapshot.get("elements", ())
    ):
        facts["payment_display_visible"] = True
    videos = [
        item
        for item in snapshot.get("elements", ())
        if isinstance(item, dict)
        and item.get("tag") == "video"
        and item.get("h5_media_instance_id")
        and isinstance(item.get("media"), dict)
    ]
    if len(videos) == 1:
        video = videos[0]
        media = video["media"]
        if type(video.get("visible")) is bool:
            facts["player_visible"] = video["visible"]
        facts["media_instance_id"] = video["h5_media_instance_id"]
        if type(media.get("paused")) is bool:
            facts["paused"] = media["paused"]
            facts["playing"] = not media["paused"]
        ready = media.get("ready_state")
        dimensions = (media.get("video_width"), media.get("video_height"))
        if (
            type(ready) is int
            and 0 <= ready <= 4
            and all(
                type(value) in (int, float) and math.isfinite(value) and value >= 0
                for value in dimensions
            )
        ):
            facts["media_ready"] = ready >= 2 and all(value > 0 for value in dimensions)
        current_time = media.get("current_time")
        if type(current_time) in (int, float) and math.isfinite(current_time) and current_time >= 0:
            facts["current_time"] = current_time
    document = snapshot.get("document") or {}
    origin = document.get("time_origin_ms")
    if type(origin) in (int, float) and math.isfinite(origin) and origin > 0:
        facts["document_time_origin_ms"] = origin
    state = snapshot.get("document_state") or {}
    if state.get("fullscreen_active") is False:
        facts["fullscreen_active"] = False
    elif (
        state.get("fullscreen_active") is True
        and state.get("fullscreen_media_instance_id") == facts.get("media_instance_id")
        and (
            (
                state.get("fullscreen_surface_kind") == "native_video"
                and state.get("fullscreen_tag") == "video"
            )
            or (
                state.get("fullscreen_surface_kind") == "custom_wrapper"
                and state.get("fullscreen_tag") not in {None, "video", "img", "body", "html"}
            )
        )
        and facts.get("player_visible") is True
        and facts.get("media_ready") is True
    ):
        # 截断 DOM 或图片全屏不能被当作当前唯一视频实例的原生全屏。
        facts["fullscreen_active"] = True
    return facts


def conditions_match(conditions, facts):
    # 未注册字段和缺观测保持未知，不能把模型摘要或 expected 复制成事实。
    return all(
        key in facts and type(value) is type(facts[key]) and facts[key] == value
        for key, value in conditions.items()
    )


class H5CheckpointObserver:
    def __init__(self, *, snapshots, plans):
        self._snapshots = snapshots
        self._plans = plans

    async def entry_matches(self, *, work, checkpoint):
        if self._snapshots is None:
            raise RuntimeError("检查点缺可信快照读取器，禁止执行")
        if (
            work.frontier.observation.page_state != "stable"
            or work.frontier.observation.overlay_signature is not None
        ):
            return False
        # 没有只读契约，不为“未创建订单”注册猜测性测量器。
        if not supports_checkpoint(checkpoint):
            return False
        snapshot = await self._snapshots.read_ui_snapshot(
            task_id=work.task_id,
            page_capture_id=work.source_capture_id,
        )
        facts = checkpoint_facts(dict(snapshot), surface=work.frontier.observation.page_type)
        if checkpoint_needs_series(checkpoint) and (
            facts.get("player_visible") is not True or facts.get("media_ready") is not True
        ):
            return False
        return bool(facts and conditions_match(checkpoint.entry_condition, facts))

    async def result_matches(
        self, *, plan_id, checkpoint_id, captures, manifests, action_receipt=None
    ):
        if len(captures) != 2 or len(manifests) != 2:
            return False
        plan = await self._plans.get_execution_plan(execution_plan_id=plan_id)
        if plan is None:
            return False
        checkpoint = next(
            (item for item in plan.checkpoints if item.checkpoint_id == checkpoint_id), None
        )
        if (
            checkpoint is None
            or not supports_checkpoint(checkpoint)
            or any(item["quality_status"] != "pass" for item in manifests)
        ):
            return False
        from services.api.app.application.checkpoint_judgement import judge_checkpoint

        samples = ()
        if checkpoint_needs_series(checkpoint):
            reader = getattr(self._snapshots, "read_checkpoint_observation", None)
            if not callable(reader):
                return False
            payload = await reader(
                task_id=captures[-1]["task_id"], page_capture_id=captures[-1]["id"]
            )
            samples = (payload or {}).get("samples", ())
        snapshots = [
            await self._snapshots.read_ui_snapshot(
                task_id=capture["task_id"],
                page_capture_id=capture["id"],
            )
            for capture in captures
        ]
        return (
            judge_checkpoint(
                checkpoint,
                before=dict(snapshots[0]),
                after=dict(snapshots[1]),
                manifests=manifests,
                series=samples,
                action_receipt=action_receipt,
            )["status"]
            == "pass"
        )


def checkpoint_needs_series(checkpoint):
    return bool(
        {"stable_frame", "loaded_frame", "media_time_stopped"}.intersection(
            set(checkpoint.expected) | set(checkpoint.exit_condition)
        )
    ) or any(item.minimum_count > 1 for item in checkpoint.evidence_requirements)


def checkpoint_needs_payment_receipt(checkpoint):
    return "payment_plan_selected" in (set(checkpoint.expected) | set(checkpoint.exit_condition))
