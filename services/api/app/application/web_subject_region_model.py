"""Web 主体区域结构事实，审计只保存有界选择器与语义记号。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WebSubjectRegionFacts:
    primary_subject_count: int = 0
    primary_subject_kind: str = "unknown"
    primary_article_body_count: int = 0
    primary_article_text_length: int = 0
    primary_player_count: int = 0
    inline_article_media_count: int = 0
    repeated_primary_card_count: int = 0
    secondary_recommendation_card_count: int = 0
    breadcrumb_count: int = 0
    detail_navigation_source_count: int = 0
    player_vendor_tokens: tuple[str, ...] = ()
    selector_summaries: tuple[tuple[str, tuple[str, ...]], ...] = ()
    token_summaries: tuple[tuple[str, tuple[str, ...]], ...] = ()
