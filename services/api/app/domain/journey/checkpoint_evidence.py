"""按实际动作账本绑定检查点证据，不把质量待定或重复帧算通过。"""

from datetime import datetime


def bind_checkpoint_evidence(*, scope, action, captures, manifests):
    """输入只能由仓储实际读取；返回原 Manifest 版本，不选择历史 pass。"""
    for key in ("task_id", "session_id", "intent_id"):
        if action.get(key) != scope.get(key):
            raise ValueError("检查点动作归属不一致")
    if (
        action.get("finalization_epoch") != scope.get("epoch")
        or action.get("requested_source_capture_id") != scope.get("source_capture_id")
        or action.get("status") != "completed"
    ):
        raise ValueError("检查点动作未完成或来源不一致")
    if len(captures) != 2 or len(manifests) != 2:
        raise ValueError("检查点必须绑定完整前后 Capture 与 Manifest")
    if captures[0]["id"] == captures[1]["id"]:
        raise ValueError("不能用同一 Capture 拼接动作前后证据")
    signatures = set()
    result = []
    for index, role in enumerate(("before_action", "after_action")):
        capture, manifest = captures[index], manifests[index]
        for key in ("task_id", "agent_run_id", "session_id", "epoch"):
            if capture.get(key) != scope.get(key):
                raise ValueError("检查点 Capture 跨 Task/Run/Session/Epoch")
        if (
            capture.get("role") != role
            or capture.get("status") != "captured"
            or capture.get("action_execution_id") != action.get("action_execution_id")
            or capture.get("command_sequence") != action.get("command_sequence")
            or capture.get("id")
            != action.get("source_capture_id" if index == 0 else "result_capture_id")
            or capture.get("parent_capture_id")
            != (scope["source_capture_id"] if index == 0 else captures[0]["id"])
        ):
            raise ValueError("检查点 Capture 角色、命令序列或父链不一致")
        instant = capture.get("captured_at")
        started, finished = action.get("started_at"), action.get("action_finished_at")
        if (
            any(
                not isinstance(item, datetime) or item.utcoffset() is None
                for item in (instant, started, finished)
            )
            or started > finished
            or (index == 0 and instant > started)
            or (index == 1 and instant < finished)
        ):
            raise ValueError("检查点 Capture 不在真实动作前后时序")
        if (
            manifest.get("task_id") != scope["task_id"]
            or manifest.get("session_id") != scope["session_id"]
            or manifest.get("capture_id") != capture["id"]
            or manifest.get("quality_status") not in ("pending", "pass", "fail")
            or type(manifest.get("manifest_version")) is not int
            or manifest["manifest_version"] <= 0
        ):
            raise ValueError("检查点 Manifest 与 Capture 不一致")
        artifacts = manifest.get("artifacts", ())
        artifact_ids = [item.get("artifact_id") for item in artifacts]
        if (
            not artifacts
            or len(set(artifact_ids)) != len(artifact_ids)
            or set(artifact_ids) != set(capture.get("artifact_ids", ()))
            or any(item.get("role") != role or not item.get("checksum") for item in artifacts)
        ):
            raise ValueError("检查点 Manifest 附件身份或角色不一致")
        signature = tuple(
            sorted(
                item["checksum"].removeprefix("sha256:")
                for item in artifacts
                if item["artifact_type"] == "screenshot"
            )
        )
        result.append(
            {
                "capture_id": capture["id"],
                "role": role,
                "action_execution_id": action["action_execution_id"],
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "quality_status": manifest["quality_status"],
                "artifact_ids": artifact_ids,
                "independent": bool(signature) and signature not in signatures,
            }
        )
        signatures.add(signature)
    return tuple(result)
