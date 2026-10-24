"""绿色过程读侧的不可变证据对象；不接受模型声明充当采集事实。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

GREEN_PROCESS_SCOPE_KEYS = frozenset(
    {
        "green_interaction_stability.v1",
        "green_temporal_rules.v1",
    }
)
_READ_AUTHORITY = object()


@dataclass(frozen=True)
class GreenOriginal:
    """已由清单、归属、字节大小和摘要共同确认的原件。"""

    artifact_id: str
    artifact_type: str
    capture_id: str
    checksum: str
    payload: bytes = field(repr=False)

    def object_payload(self):
        try:
            value = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class GreenCapture:
    """同一冻结输入成员的 Capture 和全部原件，清单质量只在原清单侧裁定。"""

    capture_id: str
    session_id: str
    role: str
    action_id: str | None
    command_sequence: int | None
    parent_capture_id: str | None
    captured_at: object
    manifest_id: str
    manifest_version: int
    originals: tuple[GreenOriginal, ...]


@dataclass(frozen=True)
class GreenAction:
    """正式动作台账所证实的执行事实，不等同业务成功。"""

    action_id: str
    session_id: str
    source_capture_id: str
    result_capture_id: str
    command_sequence: int
    started_at: object
    finished_at: object
    intent_payload: bytes = field(repr=False)


@dataclass(frozen=True)
class GreenEvidenceSet:
    """只读加载器的结果；普通映射或任意构造对象不会获得可信读取身份。"""

    task_id: str
    run_id: str
    epoch: int
    input_set_id: str
    input_hash: str
    scope_key: str
    fingerprint: str = ""
    captures: tuple[GreenCapture, ...] = ()
    actions: tuple[GreenAction, ...] = ()
    issues: tuple[str, ...] = ()
    target_platform: str | None = None
    _authority: object = field(default=None, repr=False, compare=False)

    @property
    def verified_read(self):
        return self._authority is _READ_AUTHORITY


def _verified_evidence_set(**values):
    """仅供服务端只读仓储在完成身份与原件核验后使用。"""
    return GreenEvidenceSet(**values, _authority=_READ_AUTHORITY)
