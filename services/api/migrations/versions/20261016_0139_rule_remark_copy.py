"""补上红线、空状态、外链和广告衔接的备注口径。"""

import json

from alembic import op
from sqlalchemy import text

revision = "20261016_0139"
down_revision = "20261016_0138"
branch_labels = None
depends_on = None

_APPLY = text(
    """
    UPDATE qc_rule
    SET check_point = :check_point,
        judge_config = jsonb_set(
            jsonb_set(
                CASE
                    WHEN CAST(:rule_text AS text) IS NULL THEN judge_config
                    ELSE jsonb_set(
                        judge_config,
                        '{engine_config,rule_text}',
                        to_jsonb(CAST(:rule_text AS text)),
                        true
                    )
                END,
                '{llm_checks}',
                CAST(:llm_checks AS jsonb),
                true
            ),
            '{deterministic_rules}',
            CAST(:deterministic_rules AS jsonb),
            true
        ),
        updated_at = now()
    WHERE rule_code = :rule_code
    """
)

_UPDATES = {
    "alioth_36": {
        "check_point": "除用户主动触发的生成式AI内容外，符合以下任意条件之一的，标记红线内容，交由运营团队处置： (1)封面、图片、视频等视觉内容明确表达有18岁以下； (2)看起来像红线人；关键词已不作为未成年人的检测范围（2026-08-07 口径变更）。广告不做此检查。此规则只在页面探索已经到达的页面上检测，不专门为它探索页面。",
        "llm_checks": [
            "只看封面、图片、视频等视觉内容（用户主动触发的生成式 AI 内容除外），不使用关键词库：扫描范围内零命中未成年视觉特征与「看起来像红线人」特征即 pass。",
            "视觉内容明确表达有 18 岁以下，或看起来像红线人 → 结论 manual_review，原因写「疑似未成年内容」或「疑似红线人」，标记红线交运营处置；本规则不自动判 fail。",
            "画面模糊、被遮挡、只有缩略图看不清年龄特征 → evidence_insufficient；不得凭猜测标记。",
            "广告位不检测。只使用页面探索过程中已经到达的页面，不为这条规则单独打开页面。画面只有广告时不标红线。"
        ],
        "deterministic_rules": [
            "先按 DOM、操作日志、网络日志等结构化证据做基础校验"
        ],
        "rule_text": None
    },
    "alioth_5": {
        "check_point": "空状态可以显示「暂无数据」类文字，也可以配插图或引导，但不得出现「加载失败」「请刷新」，也不得出现重试或刷新按钮。加载失败必须有重试或刷新按钮。接口返回空数组算空状态，接口报错或超时算加载失败；无法区分时不得判通过或不过。",
        "llm_checks": [
            "内容确实为空时，可以显示「暂无数据」或同等含义的说明，也可以配插图或引导；不得出现「加载失败」「请刷新」，也不得出现重试或刷新按钮，不得只留死白，不得改成推荐内容冒充有内容。",
            "已有内容但加载失败时，必须有重试或刷新按钮。这时只显示「暂无数据」、死白或没有重试按钮，判不通过。",
            "接口返回空数组算空状态，接口报错或超时算加载失败。无法区分时结论为人工复核，不得猜成通过或不过。"
        ],
        "deterministic_rules": [
            "先按 DOM、操作日志、网络日志等结构化证据做基础校验"
        ],
        "rule_text": None
    },
    "alioth_7": {
        "check_point": "外部链接操作前后可信截图不一致，即判定页面已发生跳转并通过；缺少前后截图时证据不足。APP 端先不实现。真要检查时，点击外链后前台变成系统浏览器（安卓看 intent）算通过，仍在 App 内 WebView 打开算不通过。没有广告位或外链清单时不得猜点。",
        "llm_checks": [],
        "deterministic_rules": [
            "外部链接操作前后可信截图不一致，即判定页面已发生跳转并通过；缺少前后截图时证据不足。APP 端先不实现。真要检查时，点击外链后前台变成系统浏览器（安卓看 intent）算通过，仍在 App 内 WebView 打开算不通过。没有广告位或外链清单时不得猜点。"
        ],
        "rule_text": "外部链接操作前后可信截图不一致即通过。APP 端先不实现。真要检查时，点击外链后前台变成系统浏览器（安卓看 intent）算通过，仍在 App 内 WebView 打开算不通过。没有广告位或外链清单时不得猜点。"
    },
    "alioth_26": {
        "check_point": "广告结束或手动关闭后，到正片第一帧不超过 1 秒，按时间戳计算，超过判不通过。没有广告记不适用，不写通过，也不写不通过。缺少时间戳时证据不足。",
        "llm_checks": [],
        "deterministic_rules": [
            "广告结束或手动关闭后，到正片第一帧不超过 1 秒，按时间戳计算，超过判不通过。没有广告记不适用，不写通过，也不写不通过。缺少时间戳时证据不足。"
        ],
        "rule_text": None
    }
}

_ROLLBACK = {
    "alioth_36": {
        "check_point": "除用户主动触发的生成式AI内容外，符合以下任意条件之一的，标记红线内容，交由运营团队处置： (1)封面、图片、视频等视觉内容明确表达有18岁以下； (2)看起来像红线人；关键词已不作为未成年人的检测范围（2026-08-07 口径变更）。",
        "llm_checks": [
            "只看封面、图片、视频等视觉内容（用户主动触发的生成式 AI 内容除外），不使用关键词库：扫描范围内零命中未成年视觉特征与「看起来像红线人」特征即 pass。",
            "视觉内容明确表达有 18 岁以下，或看起来像红线人 → 结论 manual_review，原因写「疑似未成年内容」或「疑似红线人」，标记红线交运营处置；本规则不自动判 fail。",
            "画面模糊、被遮挡、只有缩略图看不清年龄特征 → evidence_insufficient；不得凭猜测标记。"
        ],
        "deterministic_rules": [
            "先按 DOM、操作日志、网络日志等结构化证据做基础校验"
        ],
        "rule_text": None
    },
    "alioth_5": {
        "check_point": "不应出现因内容未上传、数据缺失或 Bug 导致的模块空白，具体表现位某个模块突然无内容； 当未上传内容、配置失效或接口报错时，前端模块不建议直接展示一片死白或只剩残缺的标题/边框；建议无内容即隐藏或兜底策略(如显示美观的缺省图或自动替换为预设的推荐内容)，确保用户看到的界面始终是完整、饱满的，防止产生对产品的负面认知；",
        "llm_checks": [
            "不应出现因内容未上传、数据缺失或 Bug 导致的模块空白，具体表现位某个模块突然无内容； 当未上传内容、配置失效或接口报错时，前端模块不建议直接展示一片死白或只剩残缺的标题/边框；建议无内容即隐藏或兜底策略(如显示美观的缺省图或自动替换为预设的推荐内容)，确保用户看到的界面始终是完整、饱满的，防止产生对产品的负面认知；",
            "需要根据质检侧补充的判定标准细化 Prompt 或规则阈值"
        ],
        "deterministic_rules": [
            "先按 DOM、操作日志、网络日志等结构化证据做基础校验"
        ],
        "rule_text": None
    },
    "alioth_7": {
        "check_point": "外部链接操作前后可信截图不一致，即判定页面已发生跳转并通过；缺少前后截图时证据不足。",
        "llm_checks": [],
        "deterministic_rules": [],
        "rule_text": "外部链接操作前后可信截图不一致即通过。"
    },
    "alioth_26": {
        "check_point": "若广告加载失败，应取消广告，播放视频正片；",
        "llm_checks": [],
        "deterministic_rules": [
            "若广告加载失败，应取消广告，播放视频正片；"
        ],
        "rule_text": None
    }
}


def _apply(rows: dict) -> None:
    bind = op.get_bind()
    for rule_code, row in rows.items():
        bind.execute(
            _APPLY,
            {
                "rule_code": rule_code,
                "check_point": row["check_point"],
                "llm_checks": json.dumps(row["llm_checks"], ensure_ascii=False),
                "deterministic_rules": json.dumps(
                    row["deterministic_rules"], ensure_ascii=False
                ),
                "rule_text": row["rule_text"],
            },
        )


def upgrade() -> None:
    _apply(_UPDATES)


def downgrade() -> None:
    _apply(_ROLLBACK)
