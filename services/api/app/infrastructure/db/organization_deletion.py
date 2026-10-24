"""组织删除结果与关联说明：任何历史关联都保留组织归属。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OrganizationDeletion:
    deleted: bool = False
    associations: dict[str, int] = field(default_factory=dict)

    def association_message(self) -> str:
        return "、".join(
            f"{count} 条{label}" for label, count in self.associations.items() if count
        )
