"""aliothapp2.0：所有产品仅使用通用 Agent，旧执行策略不能重新启用固定脚本。"""

from collections.abc import Mapping


def product_fixed_script_enabled() -> bool:
    """保留兼容接口；环境变量也不能重新启用产品脚本。"""
    return False


def profile_allows_product_orchestrator(package_name: str | None) -> bool:
    """产品档案仅提供识别信息，不再授予脚本执行权。"""
    return False


def uses_agent_generic_contract(package_name: str | None) -> bool:
    """所有已知及未知产品统一使用通用 Agent。"""
    return True


def try_product_script_override(work):
    """旧调用方兼容入口：永远不创建脚本动作，也不导入执行器。"""
    return None


def is_fixed_script_parameters(parameters: Mapping[str, object]) -> bool:
    """拒绝续跑队列与历史 Frontier 中携带的产品固定脚本指令。"""
    candidate_id = str(parameters.get("candidate_id") or "").casefold()
    if any(marker in candidate_id for marker in (
        "-script:", "recording-script", "-orchestrated-",
    )):
        return True
    for key, value in parameters.items():
        name = str(key).casefold()
        if value and (name.endswith("_orchestrated")
                      or name.endswith("_script") or "recording_script" in name):
            return True
    return False
