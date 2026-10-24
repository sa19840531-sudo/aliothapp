"""规则判定批次的稳定身份。"""

from dataclasses import dataclass

MAX_RULE_JUDGE_BATCH_SIZE = 20


@dataclass(frozen=True)
class RuleExecutionBatchKey:
    """生产计划、运行时调度和持久化认领共用的批次键。"""

    strategy: str
    category: str | None
    session_dependency: str

    @property
    def batch_id(self) -> str:
        """返回可持久化、可审计的稳定批次标识。"""
        category_key = (
            "all"
            if self.strategy == "rule_engine"
            else self.category or "uncategorized"
        )
        return f"{self.strategy}:{category_key}:{self.session_dependency}"


def build_rule_execution_batch_key(
    *,
    strategy: str | None,
    category: str | None,
    session_dependency: str | None,
) -> RuleExecutionBatchKey:
    """按真实执行边界规范化规则批次。

    确定性规则共享本地证据读取和批量写回，不按业务类别拆批；
    LLM 与混合规则仍需按类别隔离对应的能力 Skill。
    """
    normalized_strategy = str(strategy or "").strip()
    normalized_category = str(category or "").strip() or None
    if normalized_strategy == "rule_engine":
        normalized_category = None
    return RuleExecutionBatchKey(
        strategy=normalized_strategy,
        category=normalized_category,
        session_dependency=str(session_dependency or "").strip() or "snapshot",
    )
