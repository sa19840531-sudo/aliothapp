"""Web 长页稳定的冻结资源上限；不接受无限预算。"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, kw_only=True)
class WebFullPageStabilizationPolicy:
    max_document_height: int = 3000
    scroll_offset_px: int = 1920
    max_scroll_steps: int = 80
    stable_rounds: int = 2
    lazy_load_timeout_ms: int = 8000

    def __post_init__(self):
        for name, maximum in (
            ("max_document_height", 262144),
            ("scroll_offset_px", 262144),
            ("max_scroll_steps", 100),
            ("stable_rounds", 10),
            ("lazy_load_timeout_ms", 30000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"长页策略 {name} 必须为 1..{maximum} 的整数")

    def to_payload(self):
        return asdict(self)

    @classmethod
    def from_payload(cls, payload):
        if set(payload) != set(cls().to_payload()):
            raise ValueError("长页稳定策略必须包含完整冻结配置")
        return cls(**dict(payload))
