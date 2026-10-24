"""E01 冻结的证据词汇表（生产侧唯一来源）。

冻结文档：docs/design/2026-08-10-m0-e01-evidence-role-value-table.md
- 条款 3.1 词汇封闭：新增取值必须先修订冻结文档并重走三方确认，再改本表
- 条款 3.3 供给/需求分治：需求角色→供给角色的等价映射仅经 E11 Resolver 使用本表实现
- 附录 5：artifact_type 别名归一（缺陷 C3 的统一收口）

契约测试以独立字面量钉住本表：services/api/tests/test_evidence_exit_requirement_resolver.py
"""

from __future__ import annotations

import re


class EvidenceVocabularyError(ValueError):
    """证据词汇超出 E01 冻结取值域。"""


# E01 取值表 2.1：供给角色（采集端产生的事实）
FROZEN_SUPPLY_CAPTURE_ROLES = frozenset(
    {
        "primary",
        "before_action",
        "after_action",
        "bootstrap",
        "probe_before",
        "probe_result_replay",
        "shadow_candidate",
        "refreshed",
        "exploration_terminal",
        "evidence_insufficient",
    }
)

# #gate 收口 Role：只记录结束页／缺口，不派生新探索或补采。
TERMINAL_CAPTURE_ROLES = frozenset(
    {
        "exploration_terminal",
        "evidence_insufficient",
    }
)

# E01 取值表 2.3：持久层占位值（非业务角色，处置见决策点 P1/P2）
PERSISTENCE_PLACEHOLDER_CAPTURE_ROLES = frozenset({"observation", "supplemental"})

# E01 条款 3.1 + 3.4：附件角色恒等于所属 capture 的供给角色，
# 清单写入侧（E07 起）按本集合强制校验 ArtifactReference.role
MANIFEST_CAPTURE_ROLES = (
    FROZEN_SUPPLY_CAPTURE_ROLES | PERSISTENCE_PLACEHOLDER_CAPTURE_ROLES
)

# E01 取值表 2.2：需求角色（规则契约声明），7 名 + 3 文法
FROZEN_REQUIREMENT_ROLE_NAMES = frozenset(
    {"primary", "before", "after", "source", "refreshed", "landing", "fault"}
)
REQUIREMENT_ROLE_PATTERNS = (
    re.compile(r"^attempt_[1-9]\d*$"),
    re.compile(r"^location_[1-9]\d*$"),
    re.compile(r"^entry_[1-9]\d*$"),
)

# E01 条款 3.2：命名文法
CAPTURE_ROLE_GRAMMAR = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# E01 附录 5：artifact_type 词汇基线（DB CHECK 13 值 + App 运行时 ui_tree，见缺陷 C1）
FROZEN_ARTIFACT_TYPES = frozenset(
    {
        "screenshot",
        "dom",
        "visible_text",
        "ocr_text",
        "har",
        "console_log",
        "media_state",
        "ai_io",
        "report",
        "action_trace",
        "ui_snapshot",
        "temporal_trace",
        "page_coverage",
        "ui_tree",
        "view_hierarchy",
        "device_log",
        "network_trace",
        "fault_artifact",
        "checkpoint_observation",
    }
)

# E01 附录 5：别名表（读入时归一）
ARTIFACT_TYPE_ALIASES: dict[str, str] = {
    "dom_snapshot": "dom",
    "network_log": "har",
    "network_events": "har",
}

# E01 条款 3.3 + 决策点 P4：需求简名与供给角色的等价映射（保留简名，由 E11 归一）
REQUIREMENT_TO_SUPPLY_ROLE_MAP: dict[str, str] = {
    "before": "before_action",
    "after": "after_action",
}


def normalize_artifact_type(value: str) -> str:
    """别名归一并校验 artifact_type 属于冻结词汇。"""
    text = str(value).strip()
    normalized = ARTIFACT_TYPE_ALIASES.get(text, text)
    if normalized not in FROZEN_ARTIFACT_TYPES:
        raise EvidenceVocabularyError(f"artifact_type {value!r} 不在 E01 冻结词汇内")
    return normalized


def normalize_manifest_capture_role(value: str) -> str:
    """E01 条款 3.1：清单附件角色必须落在取值表（供给 10 值 + 持久层占位 2 值）内。"""
    text = str(value).strip()
    if not CAPTURE_ROLE_GRAMMAR.match(text) or text not in MANIFEST_CAPTURE_ROLES:
        raise EvidenceVocabularyError(f"capture_role {value!r} 不在 E01 冻结词汇内")
    return text


def is_frozen_requirement_role(value: str) -> bool:
    """需求角色是否属于 E01 取值表 2.2（7 名 + 3 文法）。"""
    if value in FROZEN_REQUIREMENT_ROLE_NAMES:
        return True
    return any(pattern.match(value) for pattern in REQUIREMENT_ROLE_PATTERNS)


def normalize_requirement_capture_role(value: str) -> str:
    """校验需求角色并按条款 3.3 归一为供给词汇（before→before_action、after→after_action）。"""
    text = str(value).strip()
    if not CAPTURE_ROLE_GRAMMAR.match(text) or not is_frozen_requirement_role(text):
        raise EvidenceVocabularyError(f"required_capture_role {value!r} 不在 E01 冻结词汇内")
    return REQUIREMENT_TO_SUPPLY_ROLE_MAP.get(text, text)
