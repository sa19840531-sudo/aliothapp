"""同任务、同模型的 H5 消费者共享短冷却，拒绝未发送请求不产生用量。"""

import json
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from time import monotonic

import httpx

from services.api.app.agents.runtime.run_context import get_agent_run_context


class H5ModelProviderUnavailable(RuntimeError):
    error_code = "h5_model_provider_unavailable"


@dataclass
class _State:
    failures: int = 0
    retry_at: float = 0
    probing: bool = False
    unavailable: bool = False
    account_unavailable: bool = False


_states: OrderedDict[tuple[str, str, str], _State] = OrderedDict()
_lock = Lock()


def admit(request: httpx.Request) -> tuple[tuple[str, str, str], _State, bool] | None:
    context = get_agent_run_context()
    if context is None or not context.h5_llm_budget or not context.task_id:
        return None
    try:
        model = str(json.loads(request.content).get("model", ""))
    except (ValueError, AttributeError, httpx.RequestNotRead):
        model = ""
    endpoint = str(request.url.copy_with(query=None, username=None, password=None))
    key = (context.task_id, endpoint, model)
    with _lock:
        state = _states.setdefault(key, _State())
        _states.move_to_end(key)
        while len(_states) > 1024:
            _states.popitem(last=False)
        if state.account_unavailable:
            raise H5ModelProviderUnavailable(
                "H5 模型端点返回 402 账户不可用，停止重复请求；未发送请求"
            )
        is_probe = state.failures >= 3
        if is_probe:
            if state.unavailable or monotonic() < state.retry_at or state.probing:
                raise H5ModelProviderUnavailable(
                    "H5 模型端点连续服务故障，停止重复请求；未发送请求"
                )
            state.probing = True
        return key, state, is_probe


def finish(ticket: tuple[tuple[str, str, str], _State, bool] | None, status: int | None) -> None:
    if ticket is None:
        return
    key, state, is_probe = ticket
    with _lock:
        if _states.get(key) is not state:
            return
        if status == 402:
            state.account_unavailable = True
            return
        if state.account_unavailable:
            return
        # 冷却前在途响应不能释放后续探测许可，也不能把旧响应当作探测结论。
        if state.failures >= 3 and not is_probe:
            return
        if is_probe:
            state.probing = False
        if status is not None and 500 <= status < 600:
            state.failures += 1
            if is_probe:
                state.unavailable = True
            if state.failures >= 3:
                state.retry_at = monotonic() + 30
        elif status is not None:
            # 除账户不可用外，业务侧 4xx 不能扩大成任务级模型故障。
            _states.pop(key, None)
        elif is_probe:
            state.retry_at = monotonic() + 30
