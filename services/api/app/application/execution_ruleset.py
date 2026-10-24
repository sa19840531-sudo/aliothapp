"""任务要跑的规则集落成一个「执行规则集」。

任务、工作项、发布记录都只锁一个规则集（ID + 版本 + 快照哈希），这套防护不动：
只选一个规则集时就用它本身；选了多个（或「全部」）时，用一个组合规则集当执行规则集，
``member_ruleset_ids`` 记成员，执行链路通过 ``qc_ruleset_scope()`` 取成员规则集的规则。
组合规则集的编码由成员 ID 决定（同样的组合复用同一行），快照哈希由成员快照哈希算出，
成员重播种子后组合的哈希跟着变，漂移校验照常生效。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

COMPOSITE_RULESET_CODE_PREFIX = "alioth_combo_"
COMPOSITE_RULESET_SOURCE_TYPE = "composite"


class TaskRulesetSelectionError(ValueError):
    """所选规则集不能组成一次任务（没开启、不存在或有同一条规则）。"""


@dataclass(frozen=True)
class RulesetMember:
    """参与组合的一个启用规则集。"""

    ruleset_id: str
    ruleset_code: str
    version: int
    name: str
    snapshot_hash: str | None
    rule_count: int


@dataclass(frozen=True)
class CompositeRulesetSpec:
    """组合规则集的身份：编码、名称、成员与快照哈希。"""

    ruleset_code: str
    name: str
    description: str
    member_ruleset_ids: tuple[str, ...]
    snapshot_hash: str
    rule_count: int


def normalize_ruleset_codes(codes: Sequence[str] | None) -> tuple[str, ...] | None:
    """去空白、去重（保持顺序）；空列表等同没选，由调用方按「全部」处理。"""

    if codes is None:
        return None
    normalized: list[str] = []
    for code in codes:
        value = str(code or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized) or None


def composite_snapshot_hash(members: Sequence[tuple[str, str | None]]) -> str:
    """按成员 ID 排序后对（成员 ID，成员快照哈希）取哈希；成员内容一变组合就变。"""

    payload = sorted(
        [str(ruleset_id), str(snapshot_hash or "")] for ruleset_id, snapshot_hash in members
    )
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def composite_ruleset_spec(members: Sequence[RulesetMember]) -> CompositeRulesetSpec:
    """多个启用规则集 → 组合规则集。``members`` 按展示顺序传入（名称按这个顺序拼）。"""

    if len(members) < 2:
        raise TaskRulesetSelectionError("组合规则集至少要有两个成员")
    member_ids = tuple(sorted(member.ruleset_id for member in members))
    identity = hashlib.sha256(",".join(member_ids).encode("utf-8")).hexdigest()[:16]
    names = " + ".join(member.name for member in members)
    return CompositeRulesetSpec(
        ruleset_code=f"{COMPOSITE_RULESET_CODE_PREFIX}{identity}",
        name=names[:255],
        description="同时执行的规则集：" + "、".join(
            f"{member.name}（{member.ruleset_code} v{member.version}）" for member in members
        ),
        member_ruleset_ids=member_ids,
        snapshot_hash=composite_snapshot_hash(
            [(member.ruleset_id, member.snapshot_hash) for member in members]
        ),
        rule_count=sum(max(member.rule_count, 0) for member in members),
    )
