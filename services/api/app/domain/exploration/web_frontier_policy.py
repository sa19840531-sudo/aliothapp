"""Web V2 新 Run 的通用 Frontier 冻结预算。"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class WebFrontierPolicy:
    max_pages: int = 100
    max_depth: int = 4
    max_duration_seconds: float = 10_800.0
    max_samples_per_structure: int = 3
    max_query_variants_per_path: int = 3
    max_concurrency_per_origin: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "max_pages",
            "max_depth",
            "max_samples_per_structure",
            "max_query_variants_per_path",
            "max_concurrency_per_origin",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Web frontier_policy.{field_name} 必须为正整数")
        if (
            type(self.max_duration_seconds) not in (int, float)
            or self.max_duration_seconds <= 0
        ):
            raise ValueError("Web frontier_policy.max_duration_seconds 必须为正数")

    @classmethod
    def for_new_run(
        cls,
        *,
        max_duration_seconds: float = 10_800.0,
    ) -> "WebFrontierPolicy":
        """返回当前发布版本的新 Run Frontier 策略。"""
        return cls(max_duration_seconds=max_duration_seconds)

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: object) -> "WebFrontierPolicy":
        if not isinstance(payload, dict) or set(payload) != set(cls.__dataclass_fields__):
            raise ValueError("Web frontier_policy 必须包含完整冻结配置")
        return cls(**payload)
