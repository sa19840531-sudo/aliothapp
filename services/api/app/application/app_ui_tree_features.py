"""APP JSON 中的 UI Tree 特征加载与底栏匹配（独立于 Web/H5 DOM）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from services.api.app.domain.exploration import CandidateAction

_DOCS_DATA_DIR = Path(__file__).resolve().parents[4] / "docs" / "data"
# 产品独立特征 JSON；契约文件内嵌 ui_tree_features 作 hjll 兜底。
_HJLL_FEATURES_PATH = _DOCS_DATA_DIR / "hjll_ui_tree_features.json"
_HLAPP_FEATURES_PATH = _DOCS_DATA_DIR / "hlapp_ui_tree_features.json"
_DEFAULT_FEATURES_PATH = _DOCS_DATA_DIR / "alioth_app_rule_call_contracts.json"

_PLAYBOOK_FEATURE_PATHS: dict[str, Path] = {
    "hjll": _HJLL_FEATURES_PATH,
    "hlapp": _HLAPP_FEATURES_PATH,
    "jinguo": _DOCS_DATA_DIR / "jinguo_ui_tree_features.json",
    "tangguo": _DOCS_DATA_DIR / "tangguo_ui_tree_features.json",
    "pc51": _DOCS_DATA_DIR / "pc51_ui_tree_features.json",
    "pron51": _DOCS_DATA_DIR / "pron51_ui_tree_features.json",
    "java91": _DOCS_DATA_DIR / "java91_ui_tree_features.json",
    "porn91": _DOCS_DATA_DIR / "porn91_ui_tree_features.json",
    "pzhan": _DOCS_DATA_DIR / "pzhan_ui_tree_features.json",
    "oio": _DOCS_DATA_DIR / "oio_ui_tree_features.json",
    "lal": _DOCS_DATA_DIR / "lal_ui_tree_features.json",
    "xingba": _DOCS_DATA_DIR / "xingba_ui_tree_features.json",
    "yms": _DOCS_DATA_DIR / "yms_ui_tree_features.json",
    "loli2": _DOCS_DATA_DIR / "loli2_ui_tree_features.json",
    "lsapp": _DOCS_DATA_DIR / "lsapp_ui_tree_features.json",
}

_PACKAGE_PLAYBOOK: dict[str, str] = {
    "com.hj.ttthree": "hjll",
    "top.qkjzg.zfiufm": "hlapp",
    "me.fifeg.dtphkj": "hlwapp",
    "tv.tifpv.ydtenm": "51pc",
    "us.ansbp.etrunc": "hlwwhite",
    "uk.trycq.tfkapn": "pc51",
    "com.androidtjdg.hinrqo": "pron51",
    "com.androidtwrb.miolra": "java91",
    "com.androidigtr.nqvefg": "porn91",
    "com.defaultns.base": "pzhan",
    "dark.fivqo.smyui": "oio",
    "com.androiddiek.skbtec": "lal",
    "com.androidcxgy.fosjhc": "xingba",
    "com.androidfmuw.zvgeok": "xingba",
    "com.lnhlz.livzq": "latest",
    "dark.tjvamjwzsw.ohyuqtqzzp": "yms",
    "dark.tozhn.wzmth": "yms",
    "com.xhwj.jmtt": "jmcomic",
    "com.gyfx3404.v1v2v27": "head",
    "dark.nvycy.ambpu": "jinguo",
    "com.dzu4.app": "mama",
    "com.alz7xm2.drama": "tangguo",
    "com.example.hddjxvideoclient": "loli2",
    "com.hdxy.ls": "lsapp",
}


def resolve_product_orchestrator_id(*, package_name: str | None) -> str | None:
    """只按完整 App 包名选择产品编排器，未知包绝不按名称或模糊前缀猜测。"""

    package = str(package_name or "").strip().casefold()
    return _PACKAGE_PLAYBOOK.get(package)

_DEFAULT_BOTTOM_NAV: dict[str, Any] = {
    "match_fields": (
        "semantic_target",
        "metadata.content_desc",
        "metadata.text",
        "metadata.label",
        "metadata.resource_id",
        "metadata.ui_tree_text",
        "metadata.accessibility_text",
    ),
    "bounds": {"min_top": 0.78, "max_bottom": 1.02},
    "discovery_sources": (
        "ui_tree",
        "accessibility",
        "vision",
        "promoted_coverage",
        "promoted_bottom_nav",
    ),
}

_DEFAULT_AGE_GATE: dict[str, Any] = {
    "enter_button_texts": (
        "我已满18岁",
        "我年满18岁",
        "我已满18岁-进入",
        "我年满18岁-进入",
        "确认已满18",
        "确认年满18",
    ),
    "leave_button_texts": (
        "未满18",
        "未满十八",
        "未成年",
        "离开",
        "under 18",
        "under18",
        "exit",
        "leave",
    ),
    "context_markers": (
        "海角影视",
        "未满18岁禁止进入",
        "内有成人内容",
        "未成年禁止观看",
    ),
    "match_fields": (
        "semantic_target",
        "metadata.content_desc",
        "metadata.text",
        "metadata.label",
        "metadata.ui_tree_text",
        "metadata.accessibility_text",
        "metadata.purpose",
        "metadata.expected_state_hint",
    ),
    "priority": "before_startup_ad_and_bottom_tabs",
    "action": "tap",
}


def resolve_ui_tree_playbook_id(
    *,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> str | None:
    """由明确指定的课纲 id 或完整包名解析产品特征 playbook。"""

    if isinstance(playbook_id, str) and playbook_id.strip():
        key = playbook_id.strip().casefold()
        if key in _PLAYBOOK_FEATURE_PATHS:
            return key
    return resolve_product_orchestrator_id(package_name=package_name)


def _extract_ui_tree_features(raw: Mapping[str, Any]) -> dict[str, Any]:
    """兼容「整文件即特征」与「契约内嵌 ui_tree_features」两种形状。"""

    nested = raw.get("ui_tree_features")
    if isinstance(nested, Mapping):
        return dict(nested)
    # 独立特征 JSON：根对象直接含 playbook_id / bottom_navigation 等
    if any(
        key in raw
        for key in (
            "bottom_navigation",
            "age_gate",
            "playbook_id",
            "ordered_tabs",
            "flow_order",
        )
    ):
        return dict(raw)
    return {}


def _load_features_file(target: Path) -> dict[str, Any]:
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return _extract_ui_tree_features(raw)


@lru_cache(maxsize=16)
def load_app_ui_tree_features(
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """读取 APP UI Tree 特征；按产品独立 JSON 选择，契约内嵌作兜底。"""

    payload: dict[str, Any] = {}
    if path:
        payload = _load_features_file(Path(path))
    else:
        resolved = resolve_ui_tree_playbook_id(
            playbook_id=playbook_id,
            package_name=package_name,
        )
        candidates: list[Path] = []
        if resolved and resolved in _PLAYBOOK_FEATURE_PATHS:
            candidates.append(_PLAYBOOK_FEATURE_PATHS[resolved])
        # 未指定产品时不默认绑死 hjll，避免污染其它包底栏顺序
        candidates.append(_DEFAULT_FEATURES_PATH)
        for target in candidates:
            payload = _load_features_file(target)
            if payload:
                break
    bottom = payload.get("bottom_navigation")
    if not isinstance(bottom, Mapping):
        payload["bottom_navigation"] = dict(_DEFAULT_BOTTOM_NAV)
    else:
        merged = dict(_DEFAULT_BOTTOM_NAV)
        merged.update(dict(bottom))
        payload["bottom_navigation"] = merged
    age_gate = payload.get("age_gate")
    if not isinstance(age_gate, Mapping):
        payload["age_gate"] = dict(_DEFAULT_AGE_GATE)
    else:
        merged_age = dict(_DEFAULT_AGE_GATE)
        merged_age.update(dict(age_gate))
        payload["age_gate"] = merged_age
    return payload


def bottom_navigation_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """底栏 UI Tree 特征合同。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    bottom = features.get("bottom_navigation")
    return bottom if isinstance(bottom, Mapping) else dict(_DEFAULT_BOTTOM_NAV)


def payment_channels_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """支付渠道（微信／支付宝）语意图标 + 相对槽合同。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    channels = features.get("payment_channels")
    return channels if isinstance(channels, Mapping) else {}


def home_content_entry_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """首页影片入口特征（清障后先进可点影片，再巡底栏）。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    entry = features.get("home_content_entry")
    return entry if isinstance(entry, Mapping) else {}


def home_content_entry_enabled(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> bool:
    """产品是否声明首页影片入口课纲。"""

    spec = home_content_entry_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    return bool(spec) and str(spec.get("phase") or "").strip() == "home_content_entry"


def home_content_entry_prefer_markers(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, ...]:
    spec = home_content_entry_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = spec.get("prefer_markers")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item or "").strip())


def home_content_entry_forbid_markers(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, ...]:
    spec = home_content_entry_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = spec.get("forbid_markers")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item or "").strip())


def home_content_entry_first_card_bounds(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> dict[str, float] | None:
    """首页影片第一卡相对点击框。"""

    spec = home_content_entry_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = spec.get("first_card")
    if not isinstance(raw, Mapping):
        return None
    bounds = raw.get("normalized_bounds")
    if not isinstance(bounds, Mapping):
        return None
    try:
        left = float(bounds["left"])
        top = float(bounds["top"])
        right = float(bounds["right"])
        bottom = float(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def age_gate_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """年龄门 UI Tree 特征合同（如海角「我已满18岁」／黑料「我年满18岁-进入」）。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    age_gate = features.get("age_gate")
    return age_gate if isinstance(age_gate, Mapping) else dict(_DEFAULT_AGE_GATE)


def profile_credential_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """个人中心账号凭证弹层：先「保存凭证」再「我已保存」。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = features.get("profile_credential")
    return dict(raw) if isinstance(raw, Mapping) else {}


def observation_matches_profile_credential(
    *,
    page_type: str | None,
    page_state: str | None,
    observation_text: object = None,
    features: Mapping[str, Any] | None = None,
    package_name: str | None = None,
    profile_tab_visited: bool = False,
) -> bool:
    """当前帧是否为个人中心账号凭证弹层。

    必须有强凭证语义，禁止把主页底飘「永久官方地址／haijiao.so」或广告弹层
    误判成凭证（d3456809：未点「我的」就签发「我已保存」并提早收口）。
    """

    spec = features or profile_credential_ui_tree_features(package_name=package_name)
    if not spec:
        return False
    joined = " ".join(str(part or "") for part in (observation_text, page_type, page_state))
    # 强标记：仅「账号凭证」本体；不含永久地址／haijiao／我已保存单字按钮（易误伤主页）。
    strong_markers = (
        "账号凭证",
        "帳號憑證",
        "可通过官方客服找回账号",
        "可通过官方客服找回帳號",
        "请勿删除此凭证",
        "請勿刪除此憑證",
        "会员账号掉线",
        "會員帳號掉線",
    )
    save_texts = tuple(spec.get("save_button_texts") or ())
    skip_texts = tuple(spec.get("skip_button_texts") or ())
    has_strong = _marker_hit(joined, strong_markers)
    has_buttons = _marker_hit(joined, save_texts) or _marker_hit(joined, skip_texts)
    page_types = {
        str(item).strip().casefold()
        for item in (spec.get("page_types") or ("profile",))
        if str(item or "").strip()
    }
    page_ok = str(page_type or "").strip().casefold() in page_types
    popup_states = {
        str(item).strip().casefold()
        for item in (spec.get("page_states") or ("popup", "modal", "overlay", "dialog"))
        if str(item or "").strip()
    }
    state_ok = str(page_state or "").strip().casefold() in popup_states

    # 1) 强凭证文案：必须课纲已访「我的」（禁止误点底栏／页型误判后提前收口）。
    if has_strong and profile_tab_visited:
        return True
    # 2) 个人中心页 + 按钮：同样要求已访「我的」，避免 Vision 把主页判成 profile。
    if page_ok and has_buttons and profile_tab_visited:
        return True
    # 3) 已真实点过「我的」+ 弹层态 + 按钮文案（Vision 漏「账号凭证」时的兜底）。
    if profile_tab_visited and state_ok and has_buttons:
        return True
    return False


def observation_shows_raw_profile_credential_markers(
    observation_text: object = None,
) -> bool:
    """画面是否出现账号凭证强文案（不论是否已访「我的」）。

    用于误进个人中心时的逃逸：不得据此签发保存凭证课纲收口。
    """

    joined = " ".join(str(part or "") for part in (observation_text,))
    strong_markers = (
        "账号凭证",
        "帳號憑證",
        "可通过官方客服找回账号",
        "可通过官方客服找回帳號",
        "请勿删除此凭证",
        "請勿刪除此憑證",
        "会员账号掉线",
        "會員帳號掉線",
    )
    return _marker_hit(joined, strong_markers)


def observation_shows_profile_credential_save_success(
    observation_text: object = None,
    *,
    features: Mapping[str, Any] | None = None,
    package_name: str | None = None,
) -> bool:
    """同帧是否已出现「保存成功」toast（保存凭证已生效，可点我已保存）。"""

    spec = features or profile_credential_ui_tree_features(package_name=package_name)
    markers = tuple(spec.get("save_success_markers") or ())
    if not markers:
        markers = ("保存成功",)
    return _marker_hit(str(observation_text or ""), markers)


def observation_shows_profile_credential_save_button(
    observation_text: object = None,
    *,
    features: Mapping[str, Any] | None = None,
    package_name: str | None = None,
) -> bool:
    """同帧文案／关键元素是否仍露出蓝色「保存凭证」按钮。"""

    spec = features or profile_credential_ui_tree_features(package_name=package_name)
    save = tuple(spec.get("save_button_texts") or ("保存凭证", "保存憑證", "立即保存"))
    return _marker_hit(str(observation_text or ""), save)


def profile_credential_save_phase_ready(
    *,
    save_done_flag: bool,
    save_attempt_count: int,
    observation_text: object = None,
    features: Mapping[str, Any] | None = None,
    package_name: str | None = None,
) -> bool:
    """是否允许进入第二步「我已保存」。

    规则：已点过保存／见 toast 才可关层；若 attempt=0 且同帧仍见「保存凭证」、
    又无 toast，即使 flag 误标为已保存也必须打回第一步（257b4aaa）。
    """

    toast = observation_shows_profile_credential_save_success(
        observation_text,
        features=features,
        package_name=package_name,
    )
    if toast:
        return True
    attempts = (
        save_attempt_count
        if isinstance(save_attempt_count, int) and not isinstance(save_attempt_count, bool)
        else 0
    )
    if attempts >= 1 or save_done_flag is True:
        # 已真实点过保存：允许我已保存；toast 常漏检。
        if attempts >= 1:
            return True
        # flag  alone：若同帧仍明显露出保存钮则不信任误标。
        if observation_shows_profile_credential_save_button(
            observation_text,
            features=features,
            package_name=package_name,
        ):
            return False
        return True
    return False


def candidate_matches_profile_credential_save(
    candidate: CandidateAction,
    *,
    features: Mapping[str, Any] | None = None,
    package_name: str | None = None,
) -> bool:
    """候选是否命中账号凭证「保存凭证／立即保存」。"""

    spec = features or profile_credential_ui_tree_features(package_name=package_name)
    if not spec:
        return False
    texts = extract_candidate_ui_tree_texts(
        candidate,
        match_fields=tuple(spec.get("match_fields") or ()),
    )
    if not texts:
        return False
    skip = tuple(spec.get("skip_button_texts") or ())
    save = tuple(spec.get("save_button_texts") or ())
    primary = texts[0]
    if _marker_hit(primary, skip):
        return False
    joined = " ".join(texts)
    if _marker_hit(joined, skip) and not _marker_hit(primary, save):
        return False
    return _marker_hit(joined, save)


def candidate_matches_profile_credential_skip(
    candidate: CandidateAction,
    *,
    features: Mapping[str, Any] | None = None,
    package_name: str | None = None,
) -> bool:
    """候选是否为账号凭证「我已保存」（须先保存成功后再点）。"""

    spec = features or profile_credential_ui_tree_features(package_name=package_name)
    if not spec:
        return False
    texts = extract_candidate_ui_tree_texts(
        candidate,
        match_fields=tuple(spec.get("match_fields") or ()),
    )
    if not texts:
        return False
    skip = tuple(spec.get("skip_button_texts") or ())
    save = tuple(spec.get("save_button_texts") or ())
    primary = texts[0]
    if _marker_hit(primary, save):
        return False
    return _marker_hit(" ".join(texts), skip)


def profile_credential_save_bounds(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, dict[str, float]] | None:
    """账号凭证蓝色保存按钮归一化框，供合成候选兜底。"""

    from services.api.app.domain.exploration import NormalizedBounds

    spec = profile_credential_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw_zone = spec.get("save_button_zone")
    if not isinstance(raw_zone, Mapping):
        return None
    raw_bounds = raw_zone.get("normalized_bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        bounds = NormalizedBounds(
            float(raw_bounds["left"]),
            float(raw_bounds["top"]),
            float(raw_bounds["right"]),
            float(raw_bounds["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    name = str(raw_zone.get("name") or "profile_credential_save").strip()
    return name or "profile_credential_save", bounds.to_dict()


def profile_credential_skip_bounds(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, dict[str, float]] | None:
    """账号凭证灰色「我已保存」归一化框，供保存成功后合成关闭。"""

    from services.api.app.domain.exploration import NormalizedBounds

    spec = profile_credential_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw_zone = spec.get("skip_button_zone")
    if not isinstance(raw_zone, Mapping):
        return None
    raw_bounds = raw_zone.get("normalized_bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        bounds = NormalizedBounds(
            float(raw_bounds["left"]),
            float(raw_bounds["top"]),
            float(raw_bounds["right"]),
            float(raw_bounds["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    name = str(raw_zone.get("name") or "profile_credential_already_saved").strip()
    return name or "profile_credential_already_saved", bounds.to_dict()


def startup_ad_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """开屏广告：先截图再关／跳过。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = features.get("startup_ad")
    return dict(raw) if isinstance(raw, Mapping) else {}


def popup_ads_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """弹窗广告：每个先截图，再点下方 X。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = features.get("popup_ads")
    return dict(raw) if isinstance(raw, Mapping) else {}


def startup_ad_preferred_close_bounds(
    *,
    path: str | None = None,
) -> tuple[str, dict[str, float]] | None:
    """录屏开屏：优先右上「关闭／跳过」区；供合成跳过候选使用。"""

    from services.api.app.domain.exploration import NormalizedBounds

    spec = startup_ad_ui_tree_features(path=path)
    zones = spec.get("close_zones")
    if not isinstance(zones, (list, tuple)):
        return None
    preferred_names = (
        "top_right_close_label",
        "top_right_skip",
        "top_left_skip",
    )
    ordered: list[Mapping[str, Any]] = []
    for name in preferred_names:
        for zone in zones:
            if isinstance(zone, Mapping) and str(zone.get("name") or "") == name:
                ordered.append(zone)
                break
    for zone in zones:
        if isinstance(zone, Mapping) and zone not in ordered:
            ordered.append(zone)
    for zone in ordered:
        raw = zone.get("normalized_bounds")
        if not isinstance(raw, Mapping):
            continue
        try:
            bounds = NormalizedBounds(
                float(raw["left"]),
                float(raw["top"]),
                float(raw["right"]),
                float(raw["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        name = str(zone.get("name") or "startup_close_zone").strip() or "startup_close_zone"
        return name, bounds.to_dict()
    return None


def popup_ads_preferred_close_bounds(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, dict[str, float]] | None:
    """录屏约定的弹窗关闭区（优先下方 X）；供合成关层候选使用。

    必须按 playbook／包名选产品 JSON：未传时契约兜底是 hjll，
    会把黑料关层点到海角框（约 y=0.70–0.86），点不中空心白圆 X。
    """

    from services.api.app.domain.exploration import NormalizedBounds

    spec = popup_ads_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    zones = spec.get("close_zones")
    if not isinstance(zones, (list, tuple)):
        return None
    # 精确定位区优先；hollow／banner 大框仅作兜底，避免中心点偏上点到广告格。
    preferred_names = (
        "system_notice_x_fallback",
        "bottom_center_hollow_white_x",
        "bottom_banner_x",
        "bottom_center_x",
    )
    ordered: list[Mapping[str, Any]] = []
    for name in preferred_names:
        for zone in zones:
            if isinstance(zone, Mapping) and str(zone.get("name") or "") == name:
                ordered.append(zone)
                break
    for zone in zones:
        if isinstance(zone, Mapping) and zone not in ordered:
            ordered.append(zone)
    for zone in ordered:
        raw = zone.get("normalized_bounds")
        if not isinstance(raw, Mapping):
            continue
        try:
            bounds = NormalizedBounds(
                float(raw["left"]),
                float(raw["top"]),
                float(raw["right"]),
                float(raw["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        name = str(zone.get("name") or "popup_close_zone").strip() or "popup_close_zone"
        # 大框（如 bottom_banner_x 高 0.16）几何中心偏上，会点到遮罩空处而不是圆 X。
        # 对下方关闭区收缩到框的下半中心小靶，像素校准仍可再精修。
        height = bounds.bottom - bounds.top
        width = bounds.right - bounds.left
        if (
            height >= 0.10
            and width >= 0.10
            and name
            in {
                "bottom_banner_x",
                "bottom_center_x",
                "bottom_center_hollow_white_x",
            }
        ):
            cx = (bounds.left + bounds.right) / 2.0
            # 落在大框偏下 75% 处（实测宫格圆 X 常贴下沿）
            cy = bounds.top + height * 0.75
            half_w = min(0.04, width * 0.22)
            half_h = min(0.025, height * 0.22)
            bounds = NormalizedBounds(
                max(0.0, cx - half_w),
                max(0.0, cy - half_h),
                min(1.0, cx + half_w),
                min(1.0, cy + half_h),
            )
        return name, bounds.to_dict()
    return None


def home_screenshot_only_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """主页／首页：只截图不点栏（海角）；黑料首页可点。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = features.get("home_screenshot_only")
    return dict(raw) if isinstance(raw, Mapping) else {
        "tab_label": "主页",
        "aliases": ["主页", "首页"],
        "click_tab": False,
        "must_screenshot": True,
    }


def home_top_category_tabs_ui_tree_features(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> Mapping[str, Any]:
    """主页上方横向分类（海角：最新／国产／动漫／漫画；默认推荐不点）。"""

    features = load_app_ui_tree_features(
        path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = features.get("home_top_category_tabs")
    return dict(raw) if isinstance(raw, Mapping) else {}


def ordered_home_top_category_tab_labels(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, ...]:
    """主页上方分类课纲顺序；无产品特征时返回空（不污染其它 App）。"""

    return tuple(
        label
        for label, _slot in ordered_home_top_category_clickable_slots(
            path=path,
            playbook_id=playbook_id,
            package_name=package_name,
        )
    )


def ordered_home_top_category_clickable_slots(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[tuple[str, int], ...]:
    """上方分类可点标签及其 UI Tree slot_index（供像素校准映射）。"""

    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    ordered = spec.get("ordered_tabs")
    if not isinstance(ordered, (list, tuple)):
        return ()
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for index, item in enumerate(ordered):
        if isinstance(item, Mapping):
            if item.get("click") is False:
                continue
            label = str(item.get("label") or "").strip()
            raw_slot = item.get("slot_index")
            try:
                slot_index = int(raw_slot) if raw_slot is not None else index
            except (TypeError, ValueError):
                slot_index = index
        else:
            label = str(item or "").strip()
            slot_index = index
        if not label or label in seen:
            continue
        if home_top_category_label_is_skip(
            label,
            path=path,
            playbook_id=playbook_id,
            package_name=package_name,
        ):
            continue
        seen.add(label)
        result.append((label, slot_index))
    return tuple(result)


def home_top_category_tab_aliases_for_label(
    label: str,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> frozenset[str]:
    """上方分类标签别名袋（含繁简）。"""

    needle = str(label or "").strip()
    if not needle:
        return frozenset()
    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    bag: set[str] = {needle, needle.casefold()}
    ordered = spec.get("ordered_tabs")
    if isinstance(ordered, (list, tuple)):
        for item in ordered:
            if not isinstance(item, Mapping):
                continue
            raw_label = str(item.get("label") or "").strip()
            aliases = item.get("aliases")
            alias_list = (
                [str(a).strip() for a in aliases if isinstance(a, str) and str(a).strip()]
                if isinstance(aliases, (list, tuple))
                else []
            )
            names = {raw_label, *alias_list}
            if needle in names or needle.casefold() in {n.casefold() for n in names if n}:
                bag.update(n for n in names if n)
                bag.update(n.casefold() for n in names if n)
    # 繁简兜底
    trad_map = {
        "推荐": "推薦",
        "国产": "國產",
        "动漫": "動漫",
        "漫画": "漫畫",
        "乱伦": "亂倫",
        "乱伦综": "亂倫綜",
        "精选短剧": "精選短劇",
    }
    for simp, trad in trad_map.items():
        if needle in {simp, trad} or needle.casefold() in {simp.casefold(), trad.casefold()}:
            bag.update({simp, trad, simp.casefold(), trad.casefold()})
    return frozenset(bag)


def home_top_category_skip_labels(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> frozenset[str]:
    """上方分类跳过标签（默认推荐／乱伦／日本AV）：不得记入课纲 visited。"""

    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = spec.get("skip_labels")
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    bag: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        bag.add(text)
        bag.add(text.casefold())
        bag.update(home_top_category_tab_aliases_for_label(text, package_name=package_name))
    return frozenset(bag)


def home_top_category_label_is_skip(
    label: str | None,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> bool:
    """当前选中／命中标签是否属于 skip（不得冒充课纲已访）。"""

    text = str(label or "").strip()
    if not text:
        return False
    skips = home_top_category_skip_labels(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    if not skips:
        # 无产品上下文时仍认常见 skip 名，避免乱伦被记成课纲。
        skips = frozenset({"乱伦", "亂倫", "日本AV", "日本av"})
    aliases = home_top_category_tab_aliases_for_label(
        text,
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    return bool(aliases & skips) or text in skips or text.casefold() in skips


def home_top_category_sample_first_entry_enabled(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> bool:
    """是否启用上方分类「第一片取证再切下一栏」。"""

    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    return spec.get("sample_first_entry") is True


def home_top_category_sample_first_card_bounds(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> dict[str, float] | None:
    """列表左上第一片归一化点击框。"""

    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = spec.get("first_card")
    if not isinstance(raw, Mapping):
        return None
    bounds = raw.get("normalized_bounds")
    if not isinstance(bounds, Mapping):
        return None
    try:
        left = float(bounds["left"])
        top = float(bounds["top"])
        right = float(bounds["right"])
        bottom = float(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def home_top_category_sample_evidence_page_types(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> frozenset[str]:
    """第一片进入后可记取证的页型。"""

    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    raw = spec.get("evidence_page_types")
    if not isinstance(raw, (list, tuple)):
        return frozenset({"payment", "video_detail", "content_detail"})
    return frozenset(
        str(item).strip().casefold()
        for item in raw
        if str(item or "").strip()
    )


def home_top_category_sample_match_texts(
    label: str,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, ...]:
    """某上方栏取证须优先匹配的文案（如漫画栏「3D漫画」）。"""

    needle = str(label or "").strip()
    if not needle:
        return ()
    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    ordered = spec.get("ordered_tabs")
    if not isinstance(ordered, (list, tuple)):
        return ()
    aliases = home_top_category_tab_aliases_for_label(
        needle,
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    for item in ordered:
        if not isinstance(item, Mapping):
            continue
        raw_label = str(item.get("label") or "").strip()
        if not raw_label:
            continue
        item_aliases = {raw_label, raw_label.casefold()}
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, (list, tuple)):
            item_aliases.update(
                str(alias).strip()
                for alias in raw_aliases
                if str(alias or "").strip()
            )
            item_aliases.update(
                str(alias).strip().casefold()
                for alias in raw_aliases
                if str(alias or "").strip()
            )
        if needle not in item_aliases and needle.casefold() not in item_aliases:
            if not (aliases & item_aliases):
                continue
        raw_texts = item.get("sample_match_texts")
        if not isinstance(raw_texts, (list, tuple)):
            return ()
        return tuple(
            str(text).strip()
            for text in raw_texts
            if str(text or "").strip()
        )
    return ()


def home_top_category_sample_target_bounds(
    label: str,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> dict[str, float] | None:
    """某上方栏取证目标（如漫画栏 3D漫画 卡）的归一化点击框。"""

    needle = str(label or "").strip()
    if not needle:
        return None
    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    ordered = spec.get("ordered_tabs")
    if not isinstance(ordered, (list, tuple)):
        return None
    aliases = home_top_category_tab_aliases_for_label(
        needle,
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    for item in ordered:
        if not isinstance(item, Mapping):
            continue
        raw_label = str(item.get("label") or "").strip()
        if not raw_label:
            continue
        item_aliases = {raw_label, raw_label.casefold()}
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, (list, tuple)):
            item_aliases.update(
                str(alias).strip()
                for alias in raw_aliases
                if str(alias or "").strip()
            )
            item_aliases.update(
                str(alias).strip().casefold()
                for alias in raw_aliases
                if str(alias or "").strip()
            )
        if needle not in item_aliases and needle.casefold() not in item_aliases:
            if not (aliases & item_aliases):
                continue
        bounds = item.get("sample_target_bounds")
        if not isinstance(bounds, Mapping):
            return None
        try:
            left = float(bounds["left"])
            top = float(bounds["top"])
            right = float(bounds["right"])
            bottom = float(bounds["bottom"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            return None
        return {"left": left, "top": top, "right": right, "bottom": bottom}
    return None


def home_top_category_tab_slot_bounds(
    *,
    label: str,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> dict[str, float] | None:
    """按 UI Tree 槽位生成上方分类归一化点击框；无槽位则 None。

    点击框收敛到槽位中心，并避开顶部搜索栏与右侧搜索镜，减少点缝／误进搜索页。
    """

    spec = home_top_category_tabs_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    layout = spec.get("layout")
    bounds_spec = spec.get("bounds")
    if not isinstance(layout, Mapping) or not isinstance(bounds_spec, Mapping):
        return None
    try:
        slot_count = max(1, int(layout.get("slot_count") or 7))
        y_center = float(layout.get("y_center") or 0.135)
        slot_height = float(layout.get("slot_height") or 0.04)
        pad = float(layout.get("horizontal_pad") or 0.02)
        usable_right = float(layout.get("usable_right") or 0.88)
        click_width_ratio = float(layout.get("click_width_ratio") or 0.4)
        band_top = float(bounds_spec.get("min_top") or 0.11)
        band_bottom = float(bounds_spec.get("max_bottom") or 0.165)
    except (TypeError, ValueError):
        return None
    usable_right = min(1.0, max(0.5, usable_right))
    click_width_ratio = min(0.85, max(0.2, click_width_ratio))
    ordered = spec.get("ordered_tabs")
    if not isinstance(ordered, (list, tuple)):
        return None
    slot_index: int | None = None
    needle = str(label or "").strip()
    aliases = home_top_category_tab_aliases_for_label(
        needle,
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    for item in ordered:
        if not isinstance(item, Mapping):
            continue
        raw_label = str(item.get("label") or "").strip()
        item_aliases = item.get("aliases")
        names = {raw_label}
        if isinstance(item_aliases, (list, tuple)):
            names.update(
                str(a).strip() for a in item_aliases if isinstance(a, str) and str(a).strip()
            )
        if names & set(aliases) or raw_label in aliases:
            # HJLL Airtest 对每栏的实测落点优先于旧的等宽槽位。
            # 例如新增「精选短剧」后，等宽第六格会落到乱伦综而不是动漫。
            direct = item.get("tap_bounds")
            if isinstance(direct, Mapping):
                try:
                    left = float(direct["left"])
                    top = float(direct["top"])
                    right = float(direct["right"])
                    bottom = float(direct["bottom"])
                except (KeyError, TypeError, ValueError):
                    return None
                if 0.0 <= left < right <= 1.0 and 0.105 <= top < bottom <= 0.17:
                    return {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                    }
            try:
                slot_index = int(item.get("slot_index"))
            except (TypeError, ValueError):
                slot_index = None
            break
    if slot_index is None or slot_index < 0 or slot_index >= slot_count:
        return None
    # 文字栏只分布在左侧 usable_right，右侧留给搜索镜，避免点到「搜索」。
    slot_w = usable_right / float(slot_count)
    slot_left = slot_index * slot_w + pad
    slot_right = (slot_index + 1) * slot_w - pad
    if slot_right <= slot_left:
        return None
    slot_mid = (slot_left + slot_right) / 2.0
    half = (slot_right - slot_left) * click_width_ratio / 2.0
    left = max(0.0, slot_mid - half)
    right = min(usable_right, slot_mid + half)
    if needle in {"漫画", "漫畫"}:
        # 右邻日本AV：只点左半槽。
        right = min(right, slot_mid)
        left = max(0.0, slot_left + (slot_right - slot_left) * 0.08)
        if right <= left:
            return None
    top = max(band_top, y_center - slot_height / 2.0)
    bottom = min(band_bottom, y_center + slot_height / 2.0)
    center_y = (top + bottom) / 2.0
    # 硬约束：中心不得落入搜索栏带（约 y<0.105）或内容卡／圆图标行（约 y>0.17）
    if center_y < 0.105 or center_y > 0.17:
        return None
    if bottom > 0.17:
        bottom = 0.17
        top = max(band_top, bottom - slot_height)
        center_y = (top + bottom) / 2.0
        if center_y < 0.105:
            return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
    }


def _relative_slot_bounds(
    *,
    slot_index: int,
    slot_count: int,
    band_top: float,
    band_bottom: float,
    relative_slot: Mapping[str, Any] | None = None,
    band_min: float = 0.0,
    band_max: float = 1.0,
) -> dict[str, float] | None:
    """按栏位数等分（或显式 relative_slot）生成相对点击框。"""

    if slot_count < 2 or slot_index < 0 or slot_index >= slot_count:
        return None
    if not (band_min <= band_top < band_bottom <= band_max):
        return None
    if isinstance(relative_slot, Mapping):
        try:
            slot_left = float(relative_slot["left"])
            slot_right = float(relative_slot["right"])
        except (KeyError, TypeError, ValueError):
            slot_left = slot_index / slot_count
            slot_right = (slot_index + 1) / slot_count
    else:
        slot_left = slot_index / slot_count
        slot_right = (slot_index + 1) / slot_count
    if not (0.0 <= slot_left < slot_right <= 1.0):
        return None
    width = slot_right - slot_left
    # 槽内水平取中心约 40%，避免点到邻栏。
    inset = width * 0.3
    left = slot_left + inset
    right = slot_right - inset
    height = band_bottom - band_top
    top = band_top + height * 0.15
    bottom = band_bottom - height * 0.15
    if not (0.0 <= left < right <= 1.0 and band_min <= top < bottom <= band_max):
        return None
    return {
        "left": round(left, 6),
        "top": round(top, 6),
        "right": round(right, 6),
        "bottom": round(bottom, 6),
    }


def _relative_bottom_nav_slot_bounds(
    *,
    slot_index: int,
    slot_count: int,
    band_top: float,
    band_bottom: float,
    relative_slot: Mapping[str, Any] | None = None,
) -> dict[str, float] | None:
    """按栏位数等分（或显式 relative_slot）生成底栏相对点击框。"""

    return _relative_slot_bounds(
        slot_index=slot_index,
        slot_count=slot_count,
        band_top=band_top,
        band_bottom=band_bottom,
        relative_slot=relative_slot,
        band_min=0.84,
        band_max=0.98,
    )


def payment_channel_tap_bounds(
    label: str,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> dict[str, float] | None:
    """返回支付渠道（微信／支付宝）语意图标相对点击框。"""

    needle = str(label or "").strip()
    if not needle:
        return None
    channels = payment_channels_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    if str(channels.get("policy") or "").strip() != "semantic_icon_relative":
        return None
    ordered = channels.get("ordered_channels")
    if not isinstance(ordered, (list, tuple)):
        return None
    layout = channels.get("layout") if isinstance(channels.get("layout"), Mapping) else {}
    band = channels.get("visual_band") if isinstance(channels.get("visual_band"), Mapping) else {}
    try:
        band_top = float(band.get("min_top", 0.70))
        band_bottom = float(band.get("max_bottom", 0.82))
    except (TypeError, ValueError):
        band_top, band_bottom = 0.70, 0.82
    try:
        slot_count = int(layout.get("slot_count") or len(ordered))
    except (TypeError, ValueError):
        slot_count = len(ordered)
    for item in ordered:
        if not isinstance(item, Mapping):
            continue
        names = {str(item.get("label") or "").strip()}
        aliases = item.get("aliases")
        if isinstance(aliases, (list, tuple)):
            names.update(str(value).strip() for value in aliases if str(value or "").strip())
        markers = item.get("semantic_markers")
        if isinstance(markers, (list, tuple)):
            names.update(str(value).strip() for value in markers if str(value or "").strip())
        if needle not in names:
            continue
        try:
            slot_index = int(item.get("slot_index"))
        except (TypeError, ValueError):
            continue
        relative = item.get("relative_slot")
        return _relative_slot_bounds(
            slot_index=slot_index,
            slot_count=max(slot_count, 2),
            band_top=band_top,
            band_bottom=band_bottom,
            relative_slot=relative if isinstance(relative, Mapping) else None,
            band_min=0.55,
            band_max=0.90,
        )
    return None


def _agent_bottom_nav_forces_relative(package_name: str | None) -> bool:
    """Agent 通用合同：底栏只用语义图标 + 相对槽，不用绝对 tap_bounds。"""

    if not str(package_name or "").strip():
        return False
    from services.api.app.application.app_product_orchestration_policy import (
        uses_agent_generic_contract,
    )

    return uses_agent_generic_contract(package_name)


def bottom_navigation_tab_tap_bounds(
    label: str,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> dict[str, float] | None:
    """返回产品底栏安全点击框。

    - 有专属编排：优先特征 JSON 的 tap_bounds，否则语义相对槽。
    - Agent 回落：强制语义图标 + 相对等分槽（禁止绝对框覆盖）。
    """

    needle = str(label or "").strip()
    if not needle:
        return None
    bottom = bottom_navigation_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    ordered = bottom.get("ordered_tabs")
    if not isinstance(ordered, (list, tuple)):
        return None
    layout = bottom.get("layout") if isinstance(bottom.get("layout"), Mapping) else {}
    band = bottom.get("visual_band") if isinstance(bottom.get("visual_band"), Mapping) else {}
    bounds_cfg = bottom.get("bounds") if isinstance(bottom.get("bounds"), Mapping) else {}
    try:
        band_top = float(band.get("min_top", bounds_cfg.get("min_top", 0.86)))
        band_bottom = float(band.get("max_bottom", 0.955))
    except (TypeError, ValueError):
        band_top, band_bottom = 0.86, 0.955
    try:
        slot_count = int(layout.get("slot_count") or len(ordered))
    except (TypeError, ValueError):
        slot_count = len(ordered)
    force_relative = _agent_bottom_nav_forces_relative(package_name)
    for item in ordered:
        if not isinstance(item, Mapping):
            continue
        names = {str(item.get("label") or "").strip()}
        aliases = item.get("aliases")
        if isinstance(aliases, (list, tuple)):
            names.update(str(value).strip() for value in aliases if str(value or "").strip())
        markers = item.get("semantic_markers")
        if isinstance(markers, (list, tuple)):
            names.update(str(value).strip() for value in markers if str(value or "").strip())
        if needle not in names:
            continue
        if not force_relative:
            raw = item.get("tap_bounds")
            if isinstance(raw, Mapping):
                try:
                    left = float(raw["left"])
                    top = float(raw["top"])
                    right = float(raw["right"])
                    bottom_value = float(raw["bottom"])
                except (KeyError, TypeError, ValueError):
                    left = top = right = bottom_value = -1.0
                if 0.0 <= left < right <= 1.0 and 0.84 <= top < bottom_value <= 0.96:
                    return {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom_value,
                    }
        # 语义图标 + 相对槽位（Agent 合同强制；编排包无可靠 tap_bounds 时回落）。
        try:
            slot_index = int(item.get("slot_index"))
        except (TypeError, ValueError):
            continue
        relative = item.get("relative_slot")
        return _relative_bottom_nav_slot_bounds(
            slot_index=slot_index,
            slot_count=max(slot_count, 2),
            band_top=band_top,
            band_bottom=min(band_bottom, 0.96),
            relative_slot=relative if isinstance(relative, Mapping) else None,
        )
    return None


def ordered_bottom_tab_labels(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
    clickable_only: bool = True,
) -> tuple[str, ...]:
    """课纲底栏顺序（读产品特征 JSON）。"""

    bottom = bottom_navigation_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    ordered = bottom.get("ordered_tabs")
    defaults = ("主页", "短剧", "暗网", "AI", "社区", "我的")
    if not isinstance(ordered, (list, tuple)) or not ordered:
        resolved = resolve_ui_tree_playbook_id(
            playbook_id=playbook_id,
            package_name=package_name,
        )
        if resolved == "hlapp":
            return ("首页", "精选", "片库", "直播", "AI科技", "圈子")
        if resolved == "jinguo":
            return ("首页", "剧场", "视频", "社区", "福利", "我的")
        if resolved == "tangguo":
            return ("首页", "剧库", "投票", "专题", "我的")
        return defaults
    labels: list[str] = []
    for item in ordered:
        if isinstance(item, Mapping):
            action = str(item.get("action") or "").strip()
            if action == "forbid":
                continue
            # 主页只截不点仍保留在课纲顺序；forbid 的「我的」等不进入可巡检序列
            if (
                clickable_only
                and item.get("click") is False
                and action not in {"screenshot_only", ""}
            ):
                continue
            label = str(item.get("label") or "").strip()
        else:
            label = str(item or "").strip()
        if label:
            labels.append(label)
    if labels:
        return tuple(labels)
    resolved = resolve_ui_tree_playbook_id(
        playbook_id=playbook_id,
        package_name=package_name,
    )
    if resolved == "hlapp":
        return ("首页", "精选", "片库", "直播", "AI科技", "圈子")
    return defaults


def hjll_ordered_bottom_tab_labels(*, path: str | None = None) -> tuple[str, ...]:
    """课纲底栏顺序：主页→短剧→暗网→AI→社区→我的。"""

    return ordered_bottom_tab_labels(path=path, playbook_id="hjll")


def hlapp_ordered_bottom_tab_labels(*, path: str | None = None) -> tuple[str, ...]:
    """课纲底栏顺序：首页→精选→片库→直播→AI科技→圈子（不含我的）。"""

    return ordered_bottom_tab_labels(path=path, playbook_id="hlapp")


def hjll_tab_aliases_for_label(
    label: str,
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> tuple[str, ...]:
    """某栏在 APP JSON 中的别名（短剧／视频等）。"""

    bottom = bottom_navigation_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    ordered = bottom.get("ordered_tabs")
    if not isinstance(ordered, (list, tuple)):
        return (label,)
    needle = str(label or "").strip()
    for item in ordered:
        if not isinstance(item, Mapping):
            continue
        primary = str(item.get("label") or "").strip()
        aliases = item.get("aliases")
        alias_list = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if isinstance(aliases, (list, tuple))
            else []
        )
        bag = {primary, *alias_list}
        if needle in bag or any(needle == a for a in bag):
            return tuple(dict.fromkeys([primary, *alias_list]))
    return (needle,) if needle else ()


def home_tab_is_screenshot_only(
    *,
    path: str | None = None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> bool:
    """主页栏是否禁止点击（录屏：只截不点）。"""

    home = home_screenshot_only_ui_tree_features(
        path=path,
        playbook_id=playbook_id,
        package_name=package_name,
    )
    return home.get("click_tab") is not True



def _dig(mapping: Mapping[str, Any], dotted: str) -> object:
    current: object = mapping
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def extract_candidate_ui_tree_texts(
    candidate: CandidateAction,
    *,
    match_fields: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """从候选语义与 metadata 抽出 UI Tree／a11y 文本特征。"""

    fields = tuple(match_fields or bottom_navigation_ui_tree_features().get("match_fields") or ())
    root = {
        "semantic_target": candidate.semantic_target,
        "metadata": dict(candidate.metadata),
    }
    texts: list[str] = []
    seen: set[str] = set()
    for field in fields:
        value = _dig(root, str(field))
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        texts.append(text)
    return tuple(texts)


def candidate_matches_ui_tree_bottom_tab_label(
    candidate: CandidateAction,
    *,
    next_label: str,
    features: Mapping[str, Any] | None = None,
) -> bool:
    """用 APP JSON UI Tree 特征判断候选是否命中课纲下一栏。"""

    from services.api.app.application.app_semantic_progress import (
        _normalize_bottom_tab_label_for_visit,
        bottom_tab_label_already_visited,
        expand_bottom_tab_label_aliases,
    )

    expected = _normalize_bottom_tab_label_for_visit(next_label)
    if not expected:
        return False
    spec = features or bottom_navigation_ui_tree_features()
    texts = extract_candidate_ui_tree_texts(
        candidate,
        match_fields=tuple(spec.get("match_fields") or ()),
    )
    if not texts:
        return False
    # 合并 JSON ordered_tabs 别名（短剧／视频、主页／首页）。
    expected_bag = {expected, *hjll_tab_aliases_for_label(expected)}
    expected_aliases: set[str] = set()
    for item in expected_bag:
        norm = _normalize_bottom_tab_label_for_visit(item)
        if norm:
            expected_aliases.update(expand_bottom_tab_label_aliases(norm))
    for text in texts:
        probe = _normalize_bottom_tab_label_for_visit(text)
        if not probe:
            continue
        if expand_bottom_tab_label_aliases(probe) & expected_aliases:
            return True
        if bottom_tab_label_already_visited(probe, expected_aliases):
            return True
    return False


def _marker_hit(text: str, markers: Sequence[object]) -> bool:
    folded = text.casefold()
    return any(str(marker).casefold() in folded for marker in markers if str(marker).strip())


def observation_text_matches_age_gate_context(
    *parts: object,
    features: Mapping[str, Any] | None = None,
) -> bool:
    """当前帧文案是否呈现年龄门上下文（海角影视／未满18禁止进入等）。"""

    spec = features or age_gate_ui_tree_features()
    joined = " ".join(str(part or "") for part in parts)
    if not joined.strip():
        return False
    return _marker_hit(joined, tuple(spec.get("context_markers") or ()))


def candidate_matches_age_gate_enter(
    candidate: CandidateAction,
    *,
    features: Mapping[str, Any] | None = None,
) -> bool:
    """候选是否命中 APP JSON 年龄门「进入」按钮（我已满18岁）。"""

    spec = features or age_gate_ui_tree_features()
    texts = extract_candidate_ui_tree_texts(
        candidate,
        match_fields=tuple(spec.get("match_fields") or ()),
    )
    if not texts:
        return False
    leave = tuple(spec.get("leave_button_texts") or ())
    enter = tuple(spec.get("enter_button_texts") or ())
    primary = texts[0]
    if _marker_hit(primary, leave):
        return False
    joined = " ".join(texts)
    if _marker_hit(joined, leave) and not _marker_hit(primary, enter):
        return False
    return _marker_hit(joined, enter)
