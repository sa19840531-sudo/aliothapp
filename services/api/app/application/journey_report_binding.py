"""冻结流程结果的报告投影校验；不从最新页面重新拼接历史报告。"""

from copy import deepcopy

from services.api.app.domain.journey.validation import stable_hash


def verified_journey_report(binding, *, task_id, agent_run_id, epoch, source_epoch=None):
    if not isinstance(binding, dict):
        raise ValueError("流程报告绑定格式无效")
    material = {key: value for key, value in binding.items() if key != "snapshot_hash"}
    if (
        binding.get("schema_version") != "h5-journey-report.v1"
        or binding.get("task_id") != task_id
        or binding.get("agent_run_id") != agent_run_id
        or type(binding.get("finalization_epoch")) is not int
        or binding["finalization_epoch"] != epoch
        or (
            source_epoch is not None
            and binding.get("source_finalization_epoch", binding.get("finalization_epoch"))
            != source_epoch
        )
        or (
            "source_finalization_epoch" in binding
            and (
                type(binding["source_finalization_epoch"]) is not int
                or binding["finalization_epoch"]
                not in (
                    binding["source_finalization_epoch"],
                    binding["source_finalization_epoch"] + 1,
                )
            )
        )
        or type(binding.get("gap_count")) is not int
        or binding["gap_count"] != 0
        or binding.get("snapshot_hash") != stable_hash(material)
        or not isinstance(binding.get("results"), list)
        or not binding["results"]
    ):
        raise ValueError("流程报告归属、Epoch、缺口或摘要无效")
    for result in binding["results"]:
        checkpoints = result.get("checkpoints", ())
        if (
            not checkpoints
            or any(item.get("status") not in ("pass", "fail") for item in checkpoints)
            or result.get("execution_status")
            != ("pass" if all(item["status"] == "pass" for item in checkpoints) else "fail")
        ):
            raise ValueError("待复核或不一致检查点不能投影为正式流程通过")
    return deepcopy(binding)
