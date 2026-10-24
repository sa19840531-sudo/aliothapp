"""Web 当前执行合同与历史任务隔离标识。"""

from collections.abc import Mapping

CURRENT_WEB_RUNTIME_VERSION = "web-runtime.v2"
WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED = "WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"


def is_legacy_web_execution(task_config: Mapping[str, object]) -> bool:
    target = task_config.get("_task_target")
    return (
        isinstance(target, Mapping)
        and target.get("platform") == "web"
        and task_config.get("web_runtime_version") != CURRENT_WEB_RUNTIME_VERSION
    )
