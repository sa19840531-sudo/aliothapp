"""共享交互事实的有限数值、窗口与完整状态规范化。"""

import json
import math
from collections.abc import Mapping


def _canonical(value):
    def plain(item):
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("事实键必须为字符串")
            return {key: plain(part) for key, part in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(part) for part in item]
        return item

    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False, allow_nan=False)


def _window(value):
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("clock_id"), str)
        and value["clock_id"]
        and _time(value.get("start_ms"))
        and _time(value.get("end_ms"))
        and value["start_ms"] < value["end_ms"]
        and value.get("complete") is True
    )


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _ids(value, *, allow_empty=False):
    return (
        isinstance(value, (list, tuple))
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item for item in value)
        and len(set(value)) == len(value)
    )
